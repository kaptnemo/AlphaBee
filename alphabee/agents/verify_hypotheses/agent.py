from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend
from langchain.agents.middleware import ToolRetryMiddleware

from alphabee.agents.schemas import VerificationResultList
from alphabee.agents.verify_hypotheses.prompts import VERIFY_HYPOTHESES_PROMPT
from alphabee.middleware.common import check_message_limit
from alphabee.middleware.web_search_guard import web_search_guard
from alphabee.tools.common import web_search
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
from alphabee.utils import create_chat_model, json_instruction
from alphabee.utils.paths import PROJECT_ROOT


def verify_hypotheses_agent_factory():
    """假设验证代理工厂：创建并返回一个 VerifyHypothesesAgent 实例。"""
    backend = FilesystemBackend(root_dir=str(PROJECT_ROOT), virtual_mode=True)

    system_prompt = VERIFY_HYPOTHESES_PROMPT + "\n\n" + json_instruction(VerificationResultList)
    return create_deep_agent(
        model=create_chat_model("agent.verify_hypotheses"),
        system_prompt=system_prompt,
        middleware=[
            check_message_limit,
            web_search_guard,
            ToolRetryMiddleware(),
        ],
        tools=[
            web_search,
            query_tushare,
            get_eastmoney_report_list,
            get_eastmoney_report_detail_by_encoded_url,
            get_eastmoney_report_detail_by_info_code,
            get_eastmoney_report_industry_info_by_info_code,
            get_eastmoney_industry_reports,
            download_eastmoney_report_pdf,
            download_eastmoney_report_pdf_by_info_code,
        ],
        backend=backend,
        skills=[
            str(PROJECT_ROOT / ".github" / "skills" / "tushare"),
            str(PROJECT_ROOT / "alphabee" / "skills" / "eastmoney"),
        ],
    )
