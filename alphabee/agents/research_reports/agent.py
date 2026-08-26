from __future__ import annotations

from typing import Any, cast

import structlog
from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend
from langchain.agents.middleware import ToolRetryMiddleware
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.graph.state import CompiledStateGraph

from alphabee import PROJECT_ROOT
from alphabee.agents.research_reports.prompts import RESEARCH_REPORTS_PROMPT
from alphabee.mcp.server_manager import PdfOcrMCPServerManager
from alphabee.tools.eastmoney import (
    download_eastmoney_report_pdf,
    download_eastmoney_report_pdf_by_info_code,
    get_eastmoney_industry_reports,
    get_eastmoney_report_detail_by_encoded_url,
    get_eastmoney_report_detail_by_info_code,
    get_eastmoney_report_industry_info_by_info_code,
    get_eastmoney_report_list,
)
from alphabee.tools.tushare_query import query_tushare
from alphabee.utils.llm import create_chat_model

logger = structlog.get_logger(__name__)

_RETURN_SCHEMA_HINTS: dict[str, str] = {
    "ocr_pdf_to_markdown": """

返回结构 (OCRMarkdownResult):
  - markdown_path: str  ← 清洗后 Markdown 的**落盘路径**（主交付物，直接读取该文件获得全文）
  - char_count / page_count: int
  - markdown: str|null  ← 仅当调用时传 include_content=True 才内联正文，否则为 null
  - metadata.task_id: str  ← 后续可用 get_ocr_task(task_id) 找回全部产物""",
    "ocr_pdf_to_documents": """

返回结构 (OCRDocumentsResult):
  - documents_path: str|null  ← 完整文档块 JSONL 的落盘路径
  - document_count: int
  - documents: list  ← 预览（前 preview_size 条），完整数据读取 documents_path""",
    "ocr_pdf_to_jsonl": """

返回结构:
  - output_path: str  ← JSONL 文件的保存路径""",
    "publish_report_sections": """

返回结构 (PublishReportResult):
  - report_dir: str  ← 发布后的章节目录（reports/<公司名>(<代码>)/财报/<报告期>/，未提供
    公司信息时为 reports/<报告名>/）
  - section_count / file_count: int
  - 发布后可用 AlphaBee 的 query_financial_report 工具对该报告做章节级问答""",
    "submit_pdf_ocr": """

返回结构 (SubmitOCRJobResult):
  - task_id: str  ← 任务 id（立即返回，OCR 在后台异步执行）
  - status: "queued"
  - pdf_path / markdown_path: str
  - 后续必须：wait_pdf_ocr_task(task_id) 阻塞等到成功 → get_pdf_ocr_result(task_id) 取结果""",
    "wait_pdf_ocr_task": """

阻塞等待 OCR 任务进入终态（succeeded/failed/cancelled），最多等 timeout_seconds 秒；
超时返回当前（running）状态，可再次调用继续等。返回结构同 get_pdf_ocr_status。
**不要自己反复调用 get_pdf_ocr_status 轮询——用本工具一次性等待。**""",
    "get_pdf_ocr_status": """

返回结构 (TaskStatus):
  - status: queued|running|succeeded|failed|cancelled
  - progress: float(0-100)  ← 进度百分比
  - message: str  ← 当前进度描述（如 "OCR 12/50 页"）
  - error: str|null  ← 失败原因""",
    "get_pdf_ocr_result": """

返回结构:
  - markdown_path: str  ← 清洗后 Markdown 路径（主交付物）
  - page_count / char_count: int
  - task_id: str""",
    "cancel_pdf_ocr_task": """

返回结构:
  - task_id / cancelled: bool / status: str""",
}


def _enhance_mcp_tool_descriptions(tools: list[Any]) -> list[Any]:
    """将已知 MCP 工具的返回结构说明追加到 description 中，供 LLM 读取。"""
    for tool in tools:
        hint = _RETURN_SCHEMA_HINTS.get(tool.name)
        if hint:
            tool.description += hint
    return tools


async def research_reports_fetch_agent_factory(
    mcp_server_url: str | None = None,
    *,
    mcp_transport: str = "stdio",
    require_mcp: bool = True,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """研究报告抓取代理工厂：创建并返回一个 ResearchReportsFetchAgent 实例。

    Args:
        mcp_server_url: 外部 PDF OCR MCP 服务 URL（如 ``http://127.0.0.1:8765/mcp``）。
            传 ``None`` 时**自动在创建时启动**一个内置的 MCP 服务
            （传输方式由 ``mcp_transport`` 决定），用完后调用
            ``alphabee.mcp.server_manager.stop_all_pdf_ocr_servers()`` 回收
            （进程退出时也会经 ``atexit`` 自动回收）。
        mcp_transport: 内置 MCP 服务的传输模式：
            - ``"stdio"``（默认）：每次工具调用以 stdio 拉起服务子进程，
              进程间直接管道通信，无 HTTP 序列化开销，传输效率更高；
              上传 PDF 与 OCR 产物持久化在磁盘，跨调用不丢状态。
            - ``"streamable-http"``：常驻服务子进程 + HTTP 连接，一次拉起多次复用。
            仅在 ``mcp_server_url`` 为 None 时生效。
        require_mcp: MCP 服务启动失败时是否直接抛错（True 抛错，False 降级为
            不带 OCR 工具的普通抓取代理）。

    Returns:
        配置好 MCP OCR 工具的 deep agent。可通过
        ``alphabee.mcp.server_manager.get_active_pdf_ocr_servers()`` 拿到
        本工厂启动的服务管理器句柄（用于提前 stop / 查询连接配置）。
    """
    backend = FilesystemBackend(root_dir=str(PROJECT_ROOT), virtual_mode=True)

    tools: list[Any] = [
        query_tushare,
        get_eastmoney_report_list,
        get_eastmoney_report_detail_by_encoded_url,
        get_eastmoney_report_detail_by_info_code,
        get_eastmoney_report_industry_info_by_info_code,
        get_eastmoney_industry_reports,
        download_eastmoney_report_pdf,
        download_eastmoney_report_pdf_by_info_code,
    ]

    if mcp_server_url:
        servers = {
            "pdf_ocr": {
                "transport": "streamable-http",
                "url": mcp_server_url,
            }
        }
        client = MultiServerMCPClient(cast(dict[str, Any], servers))
        mcp_tools = await client.get_tools()
        tools.extend(_enhance_mcp_tool_descriptions(mcp_tools))
    else:
        # ── 创建 agent 时自动启动内置 PDF OCR MCP 服务 ───────────────────
        manager = PdfOcrMCPServerManager(transport=mcp_transport)
        try:
            manager.start()
        except Exception as exc:
            if require_mcp:
                raise
            logger.warning("PDF OCR MCP server failed to start; agent created without OCR tools: %s", exc)
        else:
            client = MultiServerMCPClient(manager.servers_config)
            mcp_tools = await client.get_tools()
            tools.extend(_enhance_mcp_tool_descriptions(mcp_tools))
            logger.info("pdf_ocr_mcp_started", transport=mcp_transport, url=manager.url)

    return create_deep_agent(
        model=create_chat_model("agent.research_reports"),
        system_prompt=RESEARCH_REPORTS_PROMPT,
        middleware=[
            ToolRetryMiddleware(),
        ],
        tools=tools,
        backend=backend,
        skills=[
            "alphabee/skills/tushare",
            "alphabee/skills/eastmoney",
        ],
    )


if __name__ == "__main__":
    import asyncio

    async def main() -> None:
        agent = await research_reports_fetch_agent_factory()
        print("ResearchReportsFetchAgent created successfully.")

        async for chunk in agent.astream(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "请帮我获取最近三个月关于工业富联的研报列表，并下载其中一份研报的 PDF。",
                    }
                ]
            }
        ):
            print(chunk, end="", flush=True)

    asyncio.run(main())
