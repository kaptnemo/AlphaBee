"""驱动画像研究代理的构造契约（非集成测试）：schema / prompt 硬约束 / factory 接线。

这些断言钉住的是"设计稿写了、代码必须照做"的形态，不需要 LLM 也不需要联网：
- §D1.2 的四个模型字段与 ``extra="ignore"`` + ``json_schema_extra.example``；
- §D1.4 的 prompt 三条硬约束（证据纪律 / 标的特异反例 / agenda 可执行性）；
- §D1.3 的 factory 接线（工具集、中间件、skills、**不设 response_format**）；
- 无真实 LLM key 时 factory 仍可构造（只要求 key 非空，不要求可用）。
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain.agents.middleware import ToolRetryMiddleware
from langgraph.graph.state import CompiledStateGraph

from alphabee.agents.driver_profile import agent as agent_module
from alphabee.agents.driver_profile.agent import EXCLUDED_TOOLS, driver_profile_research_agent_factory
from alphabee.agents.driver_profile.output import (
    DriverResearchOutput,
    ResearchAgendaItem,
    ResearchEvidence,
    ResearchHypothesis,
)
from alphabee.agents.driver_profile.prompts import DRIVER_PROFILE_RESEARCH_PROMPT
from alphabee.middleware.common import check_message_limit
from alphabee.middleware.web_search_guard import web_search_guard
from alphabee.tools.common import web_search
from alphabee.tools.financial_report import query_financial_report
from alphabee.tools.industry_fundamentals import get_industry_fundamentals
from alphabee.tools.tushare_query import query_tushare

# ══════════════════════════════════════════════════════════════════════════
# §D1.2 数据契约
# ══════════════════════════════════════════════════════════════════════════


def test_four_models_ignore_extra_fields_and_carry_json_examples() -> None:
    for model in (ResearchEvidence, ResearchHypothesis, ResearchAgendaItem, DriverResearchOutput):
        assert model.model_config["extra"] == "ignore", model.__name__
        assert model.model_json_schema().get("example"), model.__name__


def test_driver_research_output_field_contract() -> None:
    # P1-3/P1-4（t1 revision 1，captain 授权同步）：candidates 之后为 proposed_frameworks
    # （新框架提案）与 novel_concepts（原 novel_drivers 更名，语义收敛为"沉淀概念"）。
    assert list(DriverResearchOutput.model_fields) == [
        "profit_formula",
        "driver_hypotheses",
        "research_agenda",
        "candidates",
        "proposed_frameworks",
        "novel_concepts",
        "open_questions",
    ]
    assert list(ResearchHypothesis.model_fields) == [
        "variable",
        "role",
        "mechanism",
        "company_form",
        "observables",
        "falsifiers",
        "evidence",
        "confidence",
    ]
    assert list(ResearchAgendaItem.model_fields) == [
        "question",
        "why_matters",
        "decisive_evidence",
        "preferred_sources",
        "priority",
    ]
    assert list(ResearchEvidence.model_fields) == ["kind", "ref", "quote"]

    # 默认值口径：只有 variable / question 必填，其余带默认
    assert ResearchHypothesis.model_fields["role"].default == "primary"
    assert ResearchHypothesis.model_fields["confidence"].default == 0.0
    assert ResearchAgendaItem.model_fields["priority"].default == "high"
    assert DriverResearchOutput.model_fields["profit_formula"].default == ""


def test_output_tolerates_extra_fields_from_llm() -> None:
    parsed = DriverResearchOutput.model_validate(
        {"profit_formula": "收入 = 销量 × 单价", "unexpected_field": "LLM 多给的字段"}
    )
    assert parsed.profit_formula == "收入 = 销量 × 单价"


# ══════════════════════════════════════════════════════════════════════════
# §D1.4 Prompt 三条硬约束
# ══════════════════════════════════════════════════════════════════════════


def test_prompt_carries_evidence_discipline_hard_constraints() -> None:
    prompt = DRIVER_PROFILE_RESEARCH_PROMPT
    assert "证据纪律" in prompt
    assert "query_financial_report" in prompt
    assert "管理层讨论与分析" in prompt
    assert "盈利公式" in prompt
    assert "分部结构必须与盈利公式自洽" in prompt
    assert "搜索结果的数字一律不得当财务数据使用" in prompt
    assert "open_questions" in prompt


def test_prompt_carries_company_specific_counterexample() -> None:
    prompt = DRIVER_PROFILE_RESEARCH_PROMPT
    assert "❌ 不合格：「驱动：毛利率」" in prompt
    assert "✅ 合格：「驱动：高频高速 CCL 收入占比 × 铜箔-树脂价差」" in prompt
    assert "不是行业常量" in prompt


def test_prompt_requires_actionable_agenda() -> None:
    prompt = DRIVER_PROFILE_RESEARCH_PROMPT
    assert "research_agenda" in prompt
    assert "什么证据能定论" in prompt
    assert "去哪拿" in prompt
    assert "写得没法执行的议程等于没写" in prompt


# ══════════════════════════════════════════════════════════════════════════
# §D1.3 factory 接线
# ══════════════════════════════════════════════════════════════════════════


def test_excluded_tools_cover_shell_and_filesystem() -> None:
    """``create_deep_agent`` 的 ``tools=`` 是增量的：不排除就等于把 shell/fs 交给 agent。"""
    assert EXCLUDED_TOOLS == frozenset({"ls", "glob", "grep", "read_file", "write_file", "edit_file", "execute"})


def test_factory_wires_tools_middleware_skills_without_response_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alphabee.utils import llm as llm_module

    monkeypatch.setattr(llm_module.settings.llm, "api_key", "sk-test-dummy")

    captured: dict[str, Any] = {}
    sentinel = object()

    def spy_create_deep_agent(**kwargs: Any) -> object:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(agent_module, "create_deep_agent", spy_create_deep_agent)
    assert driver_profile_research_agent_factory() is sentinel

    assert captured["tools"] == [query_financial_report, query_tushare, web_search, get_industry_fundamentals]
    middleware = captured["middleware"]
    assert middleware[0] is check_message_limit
    assert middleware[1] is web_search_guard
    assert isinstance(middleware[2], ToolRetryMiddleware)
    assert isinstance(middleware[3], agent_module._ToolExclusionMiddleware)
    assert frozenset(middleware[3]._excluded) == EXCLUDED_TOOLS

    assert captured["skills"] == ["alphabee/skills/tushare", "alphabee/skills/eastmoney"]
    assert "response_format" not in captured
    # json_instruction 自己拼一次（工厂不会自动拼）
    assert DRIVER_PROFILE_RESEARCH_PROMPT in captured["system_prompt"]
    assert "输出要求" in captured["system_prompt"]
    assert captured["model"] is not None


def test_factory_constructs_real_graph_without_real_llm_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """无真实 LLM key 也能构造出图（只有 dummy key，不发任何请求）。"""
    from alphabee.utils import llm as llm_module

    monkeypatch.setattr(llm_module.settings.llm, "api_key", "sk-test-dummy")
    graph = driver_profile_research_agent_factory()
    assert isinstance(graph, CompiledStateGraph)
