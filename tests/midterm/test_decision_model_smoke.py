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


def _langchao_000977_snapshot() -> FactorSnapshot:
    """000977 浪潮信息画像：净利高增 + 双负现金流 + 熊市 + 零证据（复现端到端实跑暴露的问题）。"""
    return FactorSnapshot(
        symbol="000977.SZ",
        as_of_date="2024-12-31",
        fundamental=FundamentalFactor(
            revenue_yoy=20.0,
            net_profit_yoy=269.0,
            eps_growth_yoy=200.0,
            gross_margin=8.75,
            operating_cashflow=-7_490_000_000.0,
            free_cashflow=-8_650_000_000.0,
            debt_to_assets=77.2,
        ),
        expectation=ExpectationFactor(
            eps_fy1_revision_1m=45.0,
            profit_forecast_min_change=100.0,
            profit_forecast_max_change=200.0,
        ),
        trend=TrendFactor(
            rs_stock_market_20d=-25.0,
            rs_stock_market_60d=-10.0,
            rs_stock_industry_20d=-15.0,
            rs_stock_industry_60d=-5.0,
        ),
        valuation=ValuationFactor(pe_ttm_5y_percentile=0.5, pb_5y_percentile=0.5),
        crowding=CrowdingFactor(),
        risk=RiskFactor(debt_to_assets=77.2),
        market=MarketFactor(market_score=30.0, regime="熊市", position_low=0.1, position_high=0.3),
    )


def test_end_to_end_s3_resonance_full_artifact():
    """S3 三共振 + 证据 → 完整 artifact：EV 充足、仓位非零、快照 state 已回写。"""
    ev = EvidenceEvent(
        id="e1",
        date="2024-06-15",
        kind="expectation",
        description="财报超预期",
        effect_on_thesis="confirming",
        confidence_delta=0.5,
    )
    art = evaluate(_s3_snapshot(), [ev], prior_confidence=0.5)
    # 软状态
    assert isinstance(art.state, StateBelief)
    assert art.state.argmax_state == "S3"
    # 四引擎产出均被组装，无 dead-end
    assert art.variable_scores is not None
    assert art.expected_value is not None
    assert art.expected_value.ev is not None  # 有证据 + 估值 → EV 可算
    assert art.expected_value.risk_adjusted_ev is not None
    assert art.expected_value.probability_source == "bayes_posterior"
    assert art.position is not None
    assert art.position.stock_weight > 0.0
    assert art.position.actual_weight is not None
    # 快照回写：state 与顶层 argmax_state 一致（不再残留 S0）
    assert art.factor_snapshot.state == "S3"


def test_end_to_end_s3_no_evidence_ev_degraded():
    """S3 三共振 + 零证据 → EV 显式降级、仓位保守（不再激进核心）。"""
    art = evaluate(_s3_snapshot())
    assert art.state.argmax_state == "S3"
    assert art.expected_value.ev is None  # 零证据 → 降级
    assert art.expected_value.probability_source == "state_prior"
    assert art.position.position_band != "核心"  # 折减后实际仓位小 → 不标核心
    assert art.factor_snapshot.state == "S3"  # 快照 state 已回写


def test_langchao_000977_fixes_end_to_end():
    """端到端复验 000977 浪潮信息：6 个修复逐一确认生效。"""
    art = evaluate(_langchao_000977_snapshot())

    # ① factor_snapshot.state 已回写，与顶层 argmax_state 一致（不再残留 S0）
    assert art.factor_snapshot.state == art.state.argmax_state
    assert art.factor_snapshot.state != "S0"

    # ② position_band 与折减后实际仓位一致（≈0.7% → 观察，不再标核心）
    assert art.position.actual_weight is not None
    assert art.position.actual_weight < 0.05
    assert art.position.position_band in ("观察", "试探")
    assert art.position.position_band != "核心"

    # ③ e_revision 未饱和（45% 上修 → ≈0.83，不是 1.0）
    assert art.variable_scores.e_revision is not None
    assert 0.0 < art.variable_scores.e_revision < 1.0

    # ④ F 反映现金质量：净利 +269% 但双负现金流 → F 显著被惩罚（不是 1.0）
    assert art.variable_scores.f_fundamental_trend is not None
    assert art.variable_scores.f_fundamental_trend < 0.5

    # ⑤ 零证据 + 熊市 → EV 显式降级（不再 +20.8% 激进值）
    assert art.expected_value.ev is None
    assert art.expected_value.probability_source == "state_prior"
    assert "零证据" in art.expected_value.note

    # ⑥ E↑T↓ 具名 conflict 存在（E 强上修 + T 走弱）
    assert any("业绩上修" in t and "股价" in t for t in art.next_evidence_to_watch)


def test_coverage_rank_single_sample_missing():
    """coverage_rank 无对标样本 → 显式 None（factors 层已修复，不再恒为 1）。"""
    from types import SimpleNamespace

    from alphabee.midterm.factors import build_crowding_factor

    crowding = SimpleNamespace(
        values={"analyst_coverage_rank": 1, "hot_rank": 10},
        warnings=["analyst_coverage_rank computed over single-stock sample (rank=1 means covered)"],
    )
    factor, missing = build_crowding_factor({}, crowding)
    assert factor.analyst_coverage_rank is None
    assert "analyst_coverage_rank" in missing


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
