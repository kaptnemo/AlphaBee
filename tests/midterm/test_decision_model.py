"""decision_model.py 的单元测试：四引擎编排、artifact 组装、无 dead-end、get_decision、导出。

全部用合成 ``FactorSnapshot`` / ``EvidenceEvent``，get_decision 用 monkeypatch 替换数据源。
"""

from __future__ import annotations

import pytest

from alphabee.midterm.decision_model import evaluate, get_decision
from alphabee.midterm.models import (
    AuditSnapshot,
    CrowdingFactor,
    EvidenceEvent,
    ExpectationFactor,
    FactorSnapshot,
    FundamentalFactor,
    MarketFactor,
    RiskFactor,
    TrendFactor,
    ValuationFactor,
)


def _snapshot(**overrides) -> FactorSnapshot:
    """构建带全量非空值的快照；``overrides`` 里的键为因子名，值为字段 dict。"""
    defaults: dict = {
        "fundamental": FundamentalFactor(revenue_yoy=10.0, net_profit_yoy=20.0, eps_growth_yoy=15.0),
        "expectation": ExpectationFactor(eps_fy1_revision_1m=3.0, revision_breadth=0.6),
        "trend": TrendFactor(rs_stock_market_20d=10.0, rs_stock_market_60d=5.0),
        "valuation": ValuationFactor(pe_ttm_5y_percentile=0.3, pb_5y_percentile=0.4),
        "crowding": CrowdingFactor(holder_count_change=5.0, hot_rank=80, turnover_rate_percentile=0.4),
        "risk": RiskFactor(pledge_ratio=10.0, debt_to_assets=40.0, audit=AuditSnapshot(audit_opinion="标准无保留意见")),
        "market": MarketFactor(market_score=60.0, position_high=0.8),
    }
    defaults.update(overrides)
    return FactorSnapshot(symbol="600519.SH", **defaults)


def _ev(effect: str, delta: float) -> EvidenceEvent:
    return EvidenceEvent(
        id="e1", date="2024-01-01", kind="expectation", description="", effect_on_thesis=effect, confidence_delta=delta
    )


# ─────────────────────────────────────────────────────────────────────────────
# 编排入口：四引擎串接 + artifact 组装
# ─────────────────────────────────────────────────────────────────────────────


def test_evaluate_assembles_artifact():
    art = evaluate(_snapshot())
    assert art.symbol == "600519.SH"
    assert art.state is not None  # StateBelief
    assert art.state.argmax_state in ("S0", "S1", "S2", "S3", "S4", "S5")
    assert art.variable_scores.f_fundamental_trend is not None
    assert art.factor_snapshot is not None
    assert art.position is not None
    assert art.expected_value is not None
    assert art.expected_value.ev is not None  # 估值存在 → EV 可算


def test_evaluate_with_evidence_updates_confidence():
    art = evaluate(_snapshot(), [_ev("confirming", 0.5)], prior_confidence=0.5)
    assert art.thesis_confidence == pytest.approx(0.75)
    assert art.expected_value.probability_source == "bayes_posterior"


def test_evaluate_no_evidence_no_prior():
    art = evaluate(_snapshot(), evidence=None, prior_confidence=None)
    assert art.thesis_confidence == 0.0  # 无证据无先验 → 0（字段非空，无法 None）
    assert art.expected_value.probability_source == "state_prior"


def test_evaluate_missing_valuation_ev_none():
    art = evaluate(_snapshot(valuation=ValuationFactor()))
    assert art.expected_value is not None
    assert art.expected_value.ev is None  # 估值缺失 → EV 显式 None，不静默回退


def test_uncertain_adds_research_to_next_evidence():
    # 空快照 → 全方向分缺失 → 熵高 uncertain → 研究任务落入 next_evidence_to_watch
    empty = FactorSnapshot(symbol="000001.SZ")
    art = evaluate(empty)
    assert len(art.next_evidence_to_watch) > 0


def test_degraded_passthrough():
    snap = _snapshot()
    snap.degraded = True
    snap.degraded_reason = "upstream missing"
    art = evaluate(snap)
    assert art.degraded is True
    assert art.degraded_reason == "upstream missing"


def test_evidence_log_passthrough():
    evs = [_ev("confirming", 0.3), _ev("refuting", 0.1)]
    art = evaluate(_snapshot(), evs)
    assert len(art.evidence_log) == 2


# ─────────────────────────────────────────────────────────────────────────────
# 便捷入口 get_decision（monkeypatch 数据源）
# ─────────────────────────────────────────────────────────────────────────────


def test_get_decision_calls_snapshot_then_evaluate(monkeypatch):
    snap = _snapshot()
    import alphabee.midterm.factors as factors_mod

    monkeypatch.setattr(factors_mod, "get_factor_snapshot", lambda symbol, include_market=True: snap)
    art = get_decision("600519.SH")
    assert art.symbol == "600519.SH"
    assert art.state is not None


# ─────────────────────────────────────────────────────────────────────────────
# __init__ 导出四引擎关键入口
# ─────────────────────────────────────────────────────────────────────────────


def test_init_exports_engines():
    import alphabee.midterm as midterm

    for name in (
        "evaluate",
        "get_decision",
        "compress_scores",
        "classify_state",
        "update_confidence",
        "scenario_probability",
        "build_position",
    ):
        assert hasattr(midterm, name), name


# ─────────────────────────────────────────────────────────────────────────────
# 纯函数 / 确定性
# ─────────────────────────────────────────────────────────────────────────────


def test_deterministic():
    snap = _snapshot()
    a = evaluate(snap, [_ev("confirming", 0.2)], prior_confidence=0.5)
    b = evaluate(snap, [_ev("confirming", 0.2)], prior_confidence=0.5)
    assert a.model_dump() == b.model_dump()
