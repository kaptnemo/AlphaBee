"""FactCollectorAgent tools package.

注意：consensus（E 因子）与 crowding（C 因子）采集器**不是** fact 工具（无 LLM 直接
调用入口），它们作为 source-specific collector 位于 ``alphabee.collectors.consensus`` /
``alphabee.collectors.crowding``，由其 ``build_consensus`` / ``build_crowding`` 产出
canonical 字段，供后续 midterm 决策层消费（见 INDEX.yaml 的 field_consumers）。
此处保持独立、不在此 re-export，避免与 get_*_fact 工具族混淆。
"""

from alphabee.agents.facts.tools.company_profile import get_company_profile
from alphabee.agents.facts.tools.competition_fact import get_competition_fact
from alphabee.agents.facts.tools.expectation_fact import get_expectation_fact
from alphabee.agents.facts.tools.financial_fact import (
    extract_financial_facts,
    get_financial_fact,
)
from alphabee.agents.facts.tools.industry_fact import get_industry_fact
from alphabee.agents.facts.tools.market_fact import (
    extract_market_facts,
    get_market_fact,
)
from alphabee.agents.facts.tools.operation_fact import get_operation_fact
from alphabee.agents.facts.tools.risk_fact import get_risk_fact

__all__ = [
    "get_company_profile",
    "get_financial_fact",
    "extract_financial_facts",
    "get_operation_fact",
    "get_industry_fact",
    "get_competition_fact",
    "get_market_fact",
    "extract_market_facts",
    "get_expectation_fact",
    "get_risk_fact",
]
