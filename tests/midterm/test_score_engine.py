"""score_engine.py 的单元测试：七方向分符号约定、缺失显式 None、纯函数/无跨因子求和。

全部用合成 ``FactorSnapshot``，不发起网络请求；覆盖 §3.3 每个因子的方向分口径。
"""

from __future__ import annotations

import pytest

from alphabee.midterm.models import (
    AuditSnapshot,
    CrowdingFactor,
    ExpectationFactor,
    FactorSnapshot,
    FundamentalFactor,
    MarketFactor,
    RiskFactor,
    TrendFactor,
    ValuationFactor,
)
from alphabee.midterm.score_engine import compress_scores


def _snapshot(**overrides) -> FactorSnapshot:
    """构建带全量非空值的快照；``overrides`` 里的键为因子名，值为字段 dict。"""
    defaults: dict = {
        "fundamental": FundamentalFactor(
            revenue_yoy=10.0,
            net_profit_yoy=20.0,
            eps_growth_yoy=15.0,
            roe=15.0,
            gross_margin=40.0,
            net_margin=15.0,
        ),
        "expectation": ExpectationFactor(
            eps_fy1_revision_1m=3.0,
            eps_fy1_revision_3m=2.0,
            eps_fy2_revision_1m=1.0,
            revision_breadth=0.6,
        ),
        "trend": TrendFactor(
            rs_stock_market_20d=10.0,
            rs_stock_market_60d=5.0,
            rs_stock_industry_20d=8.0,
            rs_stock_industry_60d=4.0,
        ),
        "valuation": ValuationFactor(
            pe_ttm_5y_percentile=0.3,
            pb_5y_percentile=0.4,
        ),
        "crowding": CrowdingFactor(
            holder_count_change=5.0,
            hot_rank=80,
            turnover_rate_percentile=0.4,
        ),
        "risk": RiskFactor(
            pledge_ratio=10.0,
            debt_to_assets=40.0,
            audit=AuditSnapshot(audit_opinion="标准无保留意见"),
        ),
        "market": MarketFactor(market_score=60.0, regime="震荡"),
    }
    defaults.update(overrides)
    return FactorSnapshot(symbol="600519.SH", **defaults)


# ─────────────────────────────────────────────────────────────────────────────
# 结构 / 入口
# ─────────────────────────────────────────────────────────────────────────────


def test_compress_scores_returns_variable_scores_shape():
    scores = compress_scores(_snapshot())
    assert scores.f_fundamental_trend is not None
    assert scores.e_revision is not None
    assert scores.t_relative_strength is not None
    assert scores.v_valuation_percentile is not None
    assert scores.c_crowding is not None
    assert scores.r_risk is not None
    assert scores.m["market_score"] == 60.0


def test_compress_scores_none_snapshot_is_empty():
    scores = compress_scores(None)
    assert scores.f_fundamental_trend is None
    assert scores.m == {}


# ─────────────────────────────────────────────────────────────────────────────
# F —— 边际改善 → 正
# ─────────────────────────────────────────────────────────────────────────────


def test_fundamental_improving_positive():
    snap = _snapshot(fundamental=FundamentalFactor(revenue_yoy=30.0, net_profit_yoy=30.0, eps_growth_yoy=30.0))
    assert compress_scores(snap).f_fundamental_trend == pytest.approx(1.0)


def test_fundamental_deteriorating_negative():
    snap = _snapshot(fundamental=FundamentalFactor(revenue_yoy=-30.0, net_profit_yoy=-30.0, eps_growth_yoy=-30.0))
    assert compress_scores(snap).f_fundamental_trend == pytest.approx(-1.0)


def test_fundamental_missing_is_none():
    snap = _snapshot(fundamental=FundamentalFactor())
    assert compress_scores(snap).f_fundamental_trend is None


# ─────────────────────────────────────────────────────────────────────────────
# E —— 上修 → 正（核心因子）
# ─────────────────────────────────────────────────────────────────────────────


def test_revision_upward_positive():
    snap = _snapshot(expectation=ExpectationFactor(eps_fy1_revision_1m=5.0))
    assert compress_scores(snap).e_revision == pytest.approx(1.0)


def test_revision_downward_negative():
    snap = _snapshot(expectation=ExpectationFactor(eps_fy1_revision_1m=-5.0))
    assert compress_scores(snap).e_revision == pytest.approx(-1.0)


def test_revision_breadth_neutral_zero():
    snap = _snapshot(expectation=ExpectationFactor(revision_breadth=0.5))
    assert compress_scores(snap).e_revision == pytest.approx(0.0)


def test_revision_missing_is_none():
    snap = _snapshot(expectation=ExpectationFactor())
    assert compress_scores(snap).e_revision is None


# ─────────────────────────────────────────────────────────────────────────────
# T —— 相对走强 → 正
# ─────────────────────────────────────────────────────────────────────────────


def test_relative_strength_positive():
    snap = _snapshot(trend=TrendFactor(rs_stock_market_20d=20.0))
    assert compress_scores(snap).t_relative_strength == pytest.approx(1.0)


def test_relative_strength_negative():
    snap = _snapshot(trend=TrendFactor(rs_stock_market_20d=-20.0))
    assert compress_scores(snap).t_relative_strength == pytest.approx(-1.0)


def test_relative_strength_missing_is_none():
    snap = _snapshot(trend=TrendFactor())
    assert compress_scores(snap).t_relative_strength is None


# ─────────────────────────────────────────────────────────────────────────────
# V —— 低分位 → 正（便宜/赔率高）
# ─────────────────────────────────────────────────────────────────────────────


def test_valuation_cheap_positive():
    snap = _snapshot(valuation=ValuationFactor(pe_ttm_5y_percentile=0.0, pb_5y_percentile=0.0))
    assert compress_scores(snap).v_valuation_percentile == pytest.approx(1.0)


def test_valuation_expensive_negative():
    snap = _snapshot(valuation=ValuationFactor(pe_ttm_5y_percentile=1.0, pb_5y_percentile=1.0))
    assert compress_scores(snap).v_valuation_percentile == pytest.approx(-1.0)


def test_valuation_missing_is_none():
    snap = _snapshot(valuation=ValuationFactor())
    assert compress_scores(snap).v_valuation_percentile is None


# ─────────────────────────────────────────────────────────────────────────────
# C —— 越拥挤 → 越负
# ─────────────────────────────────────────────────────────────────────────────


def test_crowding_hot_and_high_turnover_negative():
    snap = _snapshot(crowding=CrowdingFactor(holder_count_change=5.0, hot_rank=1, turnover_rate_percentile=1.0))
    assert compress_scores(snap).c_crowding < 0


def test_crowding_holder_concentration_negative():
    # 户数↓（负增幅）→ 筹码集中 → 负（§3.3 原注）
    snap = _snapshot(crowding=CrowdingFactor(holder_count_change=-20.0))
    assert compress_scores(snap).c_crowding == pytest.approx(-1.0)


def test_crowding_missing_is_none():
    snap = _snapshot(crowding=CrowdingFactor())
    assert compress_scores(snap).c_crowding is None


# ─────────────────────────────────────────────────────────────────────────────
# R —— 风险升 → 越负
# ─────────────────────────────────────────────────────────────────────────────


def test_risk_high_pledge_and_leverage_negative():
    snap = _snapshot(risk=RiskFactor(pledge_ratio=50.0, debt_to_assets=100.0))
    assert compress_scores(snap).r_risk == pytest.approx(-1.0)


def test_risk_nonstandard_audit_negative():
    snap = _snapshot(risk=RiskFactor(audit=AuditSnapshot(audit_opinion="保留意见")))
    assert compress_scores(snap).r_risk == pytest.approx(-1.0)


def test_risk_low_is_positive():
    snap = _snapshot(
        risk=RiskFactor(pledge_ratio=0.0, debt_to_assets=0.0, audit=AuditSnapshot(audit_opinion="标准无保留意见"))
    )
    assert compress_scores(snap).r_risk > 0


def test_risk_missing_is_none():
    snap = _snapshot(risk=RiskFactor())
    assert compress_scores(snap).r_risk is None


# ─────────────────────────────────────────────────────────────────────────────
# M —— 复用 market_score 0-100
# ─────────────────────────────────────────────────────────────────────────────


def test_market_summary_passthrough():
    snap = _snapshot(market=MarketFactor(market_score=72.5, regime="牛市", position_low=0.5, position_high=0.9))
    m = compress_scores(snap).m
    assert m["market_score"] == 72.5
    assert m["regime"] == "牛市"
    assert m["position_low"] == 0.5
    assert m["position_high"] == 0.9


# ─────────────────────────────────────────────────────────────────────────────
# 纯函数 / 不跨因子求和 / 不修改输入
# ─────────────────────────────────────────────────────────────────────────────


def test_no_cross_factor_summation():
    base = _snapshot()
    changed = _snapshot(fundamental=FundamentalFactor(revenue_yoy=30.0))
    s1 = compress_scores(base)
    s2 = compress_scores(changed)
    # 只动 F，其它方向分必须保持不变
    assert s1.e_revision == s2.e_revision
    assert s1.t_relative_strength == s2.t_relative_strength
    assert s1.v_valuation_percentile == s2.v_valuation_percentile
    assert s1.c_crowding == s2.c_crowding
    assert s1.r_risk == s2.r_risk
    assert s1.m == s2.m
    # F 确实变了
    assert s1.f_fundamental_trend != s2.f_fundamental_trend


def test_deterministic():
    snap = _snapshot()
    assert compress_scores(snap).model_dump() == compress_scores(snap).model_dump()


def test_does_not_mutate_snapshot():
    snap = _snapshot()
    before = snap.model_dump()
    compress_scores(snap)
    assert snap.model_dump() == before
