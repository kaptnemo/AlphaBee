from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend
from langchain.agents.middleware import ToolRetryMiddleware
from langchain_mcp_adapters.client import MultiServerMCPClient

from alphabee.agents.research_reports.prompts import RESEARCH_REPORTS_PROMPT
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
from alphabee.utils.paths import PROJECT_ROOT

_RETURN_SCHEMA_HINTS: dict[str, str] = {
    "ocr_pdf_to_markdown": """

返回结构 (OCRMarkdownResult):
  - markdown: str  ← OCR 提取的完整 Markdown 文本，**读取此字段获取内容**
  - metadata.task_id: str
  - metadata.source: dict(pdf_path, pdf_name, file_id, file_size, page_count)""",
    "ocr_pdf_to_documents": """

返回结构: LangChain Document 对象列表，每个包含:
  - page_content: str  ← 该页的文本内容
  - metadata: dict(page, source, ...)""",
    "ocr_pdf_to_jsonl": """

返回结构:
  - output_path: str  ← JSONL 文件的保存路径""",
}


def _enhance_mcp_tool_descriptions(tools: list) -> list:
    """将已知 MCP 工具的返回结构说明追加到 description 中，供 LLM 读取。"""
    for tool in tools:
        hint = _RETURN_SCHEMA_HINTS.get(tool.name)
        if hint:
            tool.description += hint
    return tools


def save_ocr_markdown(file_path: str, content: str) -> str:
    """将 OCR 提取的 Markdown 文本保存到磁盘文件，返回保存路径。

    调用时机：在 ocr_pdf_to_markdown 返回结果后，立即调用此工具将内容持久化。

    Args:
        file_path: 保存路径，建议使用 PDF 路径替换扩展名，如 /data/.../xxx.md
        content:   OCR 返回的 markdown 文本内容（从 OCR 返回结果的 markdown 字段中读取）
    """
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"✅ Markdown 已保存到 {path}（{path.stat().st_size} 字节）"


async def research_reports_fetch_agent_factory():
    """研究报告抓取代理工厂：创建并返回一个 ResearchReportsFetchAgent 实例。"""
    backend = FilesystemBackend(root_dir=str(PROJECT_ROOT), virtual_mode=True)

    servers = {
        "pdf_ocr": {
            "transport": "streamable-http",
            "url": "http://localhost:9999/mcp",
        }
    }
    client = MultiServerMCPClient(servers)
    mcp_tools = await client.get_tools()
    mcp_tools = _enhance_mcp_tool_descriptions(mcp_tools)

    return create_deep_agent(
        model=create_chat_model("agent.research_reports"),
        system_prompt=RESEARCH_REPORTS_PROMPT,
        middleware=[
            ToolRetryMiddleware(),
        ],
        tools=[
            query_tushare,
            get_eastmoney_report_list,
            get_eastmoney_report_detail_by_encoded_url,
            get_eastmoney_report_detail_by_info_code,
            get_eastmoney_report_industry_info_by_info_code,
            get_eastmoney_industry_reports,
            download_eastmoney_report_pdf,
            download_eastmoney_report_pdf_by_info_code,
            save_ocr_markdown,
            *mcp_tools,
        ],
        backend=backend,
        skills=[
            str(PROJECT_ROOT / ".github" / "skills" / "tushare"),
            str(PROJECT_ROOT / "alphabee" / "skills" / "eastmoney"),
        ],
    )


if __name__ == "__main__":
    import asyncio

    async def main():
        agent = await research_reports_fetch_agent_factory()
        print("ResearchReportsFetchAgent created successfully.")

        # await agent.ainvoke(
        #     {
        #         "messages": [
        #             {
        #                 "role": "user",
        #                 "content": "请帮我获取最近一个月关于贵州茅台的研报列表，并下载其中一份研报的 PDF。",
        #             }
        #         ]
        #     }
        # )

        async for chunk in agent.astream(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "请帮我获取最近一个月关于阳光电源的研报列表，并下载其中一份研报的 PDF。",
                    }
                ]
            }
        ):
            print(chunk, end="", flush=True)

    asyncio.run(main())
