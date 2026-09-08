"""决策模型端到端冒烟测试：真实感 mock FactorSnapshot 走完整四引擎流水线。

不发起网络请求，只用合成数据验证 evaluate/get_decision 把七因子快照
组装成完整的 CompanyStateArtifact（state/variable_scores/expected_value/position）。
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
    StateBelief,
    TrendFactor,
    ValuationFactor,
)


def _s3_snapshot() -> FactorSnapshot:
    """S3 三共振快照：F↑E↑T↑ + 估值中位 + 低拥挤/低风险 + 市场偏多。"""
    return FactorSnapshot(
        symbol="300750.SZ",
        as_of_date="2024-06-30",
        fundamental=FundamentalFactor(revenue_yoy=30.0, net_profit_yoy=40.0, eps_growth_yoy=35.0),
        expectation=ExpectationFactor(eps_fy1_revision_1m=5.0, eps_fy1_revision_3m=4.0, revision_breadth=0.7),
        trend=TrendFactor(rs_stock_market_20d=20.0, rs_stock_market_60d=15.0),
        valuation=ValuationFactor(pe_ttm_5y_percentile=0.3, pb_5y_percentile=0.35),
        crowding=CrowdingFactor(holder_count_change=-5.0, hot_rank=90, turnover_rate_percentile=0.3),
        risk=RiskFactor(pledge_ratio=5.0, debt_to_assets=30.0, audit=AuditSnapshot(audit_opinion="标准无保留意见")),
        market=MarketFactor(market_score=70.0, position_low=0.5, position_high=0.9),
    )


def test_end_to_end_s3_resonance_full_artifact():
    art = evaluate(_s3_snapshot())
    # 软状态
    assert isinstance(art.state, StateBelief)
    assert art.state.argmax_state == "S3"
    # 四引擎产出均被组装，无 dead-end
    assert art.variable_scores is not None
    assert art.expected_value is not None
    assert art.expected_value.ev is not None
    assert art.expected_value.risk_adjusted_ev is not None
    assert art.position is not None
    assert art.position.position_band == "核心"
    assert art.position.actual_weight is not None
    # S3 三共振 → EV 赔率充足（risk_adjusted_ev > 1.0），不被压 0
    assert art.position.stock_weight > 0.0


def test_end_to_end_with_evidence_updates_confidence():
    ev = EvidenceEvent(
        id="e1",
        date="2024-06-15",
        kind="expectation",
        description="财报超预期",
        effect_on_thesis="confirming",
        confidence_delta=0.5,
    )
    art = evaluate(_s3_snapshot(), [ev], prior_confidence=0.5)
    assert art.thesis_confidence == pytest.approx(0.75)
    assert art.expected_value.probability_source == "bayes_posterior"


def test_end_to_end_get_decision(monkeypatch):
    snap = _s3_snapshot()
    import alphabee.midterm.factors as factors_mod

    monkeypatch.setattr(factors_mod, "get_factor_snapshot", lambda symbol, include_market=True: snap)
    art = get_decision("300750.SZ")
    assert art.symbol == "300750.SZ"
    assert art.state.argmax_state == "S3"
    assert art.position is not None
