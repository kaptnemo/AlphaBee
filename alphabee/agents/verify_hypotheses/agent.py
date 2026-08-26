from typing import Any

from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.middleware._tool_exclusion import _ToolExclusionMiddleware
from langchain.agents.middleware import ToolRetryMiddleware
from langgraph.graph.state import CompiledStateGraph

from alphabee import PROJECT_ROOT
from alphabee.agents.schemas import VerificationResultList
from alphabee.agents.verify_hypotheses.prompts import VERIFY_HYPOTHESES_PROMPT
from alphabee.middleware.common import check_message_limit
from alphabee.middleware.web_search_guard import web_search_guard
from alphabee.tools.common import web_search
from alphabee.tools.tushare_query import query_tushare
from alphabee.utils import create_chat_model, json_instruction


def verify_hypotheses_agent_factory() -> CompiledStateGraph[Any, Any, Any, Any]:
    """假设验证代理工厂：创建并返回一个 VerifyHypothesesAgent 实例。"""
    # 延迟导入：alphabee.tools.financial_report 会拉起 deepagents/fetch 全家桶，
    # 导入成本较高，只在真正构建验证代理时才加载。
    from alphabee.tools.financial_report import query_financial_report

    backend = FilesystemBackend(root_dir=str(PROJECT_ROOT), virtual_mode=True)

    system_prompt = VERIFY_HYPOTHESES_PROMPT + "\n\n" + json_instruction(VerificationResultList)
    return create_deep_agent(
        model=create_chat_model("agent.verify_hypotheses"),
        system_prompt=system_prompt,
        middleware=[
            check_message_limit,
            web_search_guard,
            ToolRetryMiddleware(),
            # 硬性禁用文件系统与 shell 工具：验证代理只通过
            # web_search / query_tushare / query_financial_report / eastmoney 取证，
            # 禁止 ls/glob/grep/read_file 自行浏览或读取 reports/ 等本地目录。
            _ToolExclusionMiddleware(
                excluded=frozenset({"ls", "glob", "grep", "read_file", "write_file", "edit_file", "execute"})
            ),
        ],
        tools=[
            web_search,
            query_tushare,
            query_financial_report,
            # get_eastmoney_report_list,
            # get_eastmoney_report_detail_by_encoded_url,
            # get_eastmoney_report_detail_by_info_code,
            # get_eastmoney_report_industry_info_by_info_code,
            # get_eastmoney_industry_reports,
            # download_eastmoney_report_pdf,
            # download_eastmoney_report_pdf_by_info_code,
        ],
        backend=backend,
        skills=[
            "alphabee/skills/tushare",
            "alphabee/skills/eastmoney",
        ],
    )


if __name__ == "__main__":
    agent = verify_hypotheses_agent_factory()
    print("VerifyHypothesesAgent created successfully.")

    import asyncio

    asyncio.run(
        agent.ainvoke(
            {
                "messages": [
                    {"role": "user", "content": "请帮我验证一个假设：贵州茅台的股价在过去一年内上涨了 20% 以上。"}
                ]
            }
        )
    )
