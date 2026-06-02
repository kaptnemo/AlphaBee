from pathlib import Path

from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend

from alphabee.utils import create_chat_model
from alphabee.middleware.common import check_message_limit
from alphabee.middleware.web_search_guard import web_search_guard
from alphabee.tools.common import (
    web_search,
    extract_symbols_from_query,
)
from alphabee.agents.facts.prompts import FACT_COLLECTOR_PROMPT
from alphabee.agents.facts.tools import (
    get_company_profile,
    get_financial_fact,
    get_operation_fact,
    get_industry_fact,
    get_competition_fact,
    get_market_fact,
    get_expectation_fact,
    get_risk_fact,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def fact_collector_agent_factory():
    """事实收集代理工厂：创建并返回一个 FactCollectorAgent 实例。"""
    backend = FilesystemBackend(root_dir=str(_PROJECT_ROOT), virtual_mode=True)
    return create_deep_agent(
        model=create_chat_model("agent.facts"),
        system_prompt=FACT_COLLECTOR_PROMPT,
        tools=[
            extract_symbols_from_query,
            get_company_profile,
            get_financial_fact,
            get_operation_fact,
            get_industry_fact,
            get_competition_fact,
            get_market_fact,
            get_expectation_fact,
            get_risk_fact,
            web_search,
        ],
        middleware=[
            web_search_guard,
            check_message_limit,
        ],
        backend=backend,
    )


# Backward compat alias
facts_agent_factory = fact_collector_agent_factory