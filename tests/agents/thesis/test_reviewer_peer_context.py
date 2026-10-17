"""F4：reviewer 同行基准缺口检查（Layer 1 Rule 6）与 peer_context 模板渲染的回归钉。

设计依据：``docs/design/THESIS_CONTEXT_MISMATCH_FIX_PLAN.md`` §3.4 / §5.2。

行为：capital_efficiency / valuation_fit 维度在「有证据、非 insufficient、peer_benchmarks 缺失」
时报「缺少同行业可比基准」并建议 reconsider_with_context；有基准或 peer_context=None 时行为
与现状一致。Layer 2 的 ``_call_llm`` 把 ``peer_context_json`` 渲染进 REVIEWER_USER_TEMPLATE。
"""

from __future__ import annotations

import pytest

from alphabee.agents.thesis.models import CompanyContext, EvidenceItem, InvestmentThesis, ThesisDimension
from alphabee.agents.thesis.reviewer import ThesisReviewer


def _dim(dim_id: str, *, evidence_count: int = 2) -> ThesisDimension:
    evidence = [
        EvidenceItem(signal_id=f"sig_{i}", signal_name=f"sig_{i}", level="high", impact="positive")
        for i in range(evidence_count)
    ]
    return ThesisDimension(id=dim_id, name=dim_id, judgment="neutral", score=0.0, evidence=evidence, confidence=0.8)


def _check(dim_id: str, peer_context):
    return ThesisReviewer()._layer1_check(dim_id, _dim(dim_id), {}, CompanyContext(), peer_context)


@pytest.mark.parametrize("dim_id", ["capital_efficiency", "valuation_fit"])
def test_missing_peer_benchmarks_reports_rule6_issue(dim_id: str):
    """有证据 + peer_benchmarks 缺失 → Rule 6 issue + suggested_action=reconsider_with_context。"""
    verdict = _check(dim_id, {"peer_benchmarks": {}})
    assert any("缺少同行业可比基准" in issue for issue in verdict.issues)
    assert verdict.suggested_action == "reconsider_with_context"


@pytest.mark.parametrize("dim_id", ["capital_efficiency", "valuation_fit"])
def test_with_peer_benchmarks_no_rule6_issue(dim_id: str):
    """有 peer_benchmarks → 不报缺基准。"""
    verdict = _check(dim_id, {"peer_benchmarks": {"roe": 0.24}})
    assert not any("缺少同行业可比基准" in issue for issue in verdict.issues)


@pytest.mark.parametrize("dim_id", ["capital_efficiency", "valuation_fit"])
def test_peer_context_none_keeps_old_behavior(dim_id: str):
    """peer_context=None → 行为与现状一致（不新增缺基准 issue）。"""
    verdict = _check(dim_id, None)
    assert not any("缺少同行业可比基准" in issue for issue in verdict.issues)


def test_call_llm_renders_peer_context_json(monkeypatch):
    """_call_llm 渲染 REVIEWER_USER_TEMPLATE 时包含 peer_context_json（mock LLM 捕获输入）。"""

    class FakeResponse:
        content = '{"dimension_reviews": {}}'

    class FakeModel:
        def __init__(self) -> None:
            self.messages = None

        def invoke(self, messages):
            self.messages = messages
            return FakeResponse()

    reviewer = ThesisReviewer()
    fake = FakeModel()
    reviewer._model = fake

    thesis = InvestmentThesis(symbol="600519.SH", period="2024Q4")
    peer_context = {
        "track_label": "存储芯片",
        "peer_group": ["北京君正"],
        "peer_benchmarks": {"roe": 0.24},
    }
    result = reviewer._call_llm(thesis, {}, CompanyContext(), peer_context)

    human = fake.messages[1]
    content = human.content
    assert "同行基准" in content
    assert "存储芯片" in content
    assert '"peer_group"' in content
    assert '"peer_benchmarks"' in content
    assert result == {"dimension_reviews": {}}
