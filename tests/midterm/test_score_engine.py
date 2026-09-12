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
from alphabee.midterm.score_engine import adjust_market_exposure, compress_scores


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


def test_fundamental_negative_cashflow_penalized():
    """profit_without_cash：高增长但经营/自由现金流双负 → F 被显著惩罚。"""
    snap = _snapshot(
        fundamental=FundamentalFactor(
            revenue_yoy=30.0,
            net_profit_yoy=269.0,
            eps_growth_yoy=30.0,
            operating_cashflow=-7_490_000_000.0,
            free_cashflow=-8_650_000_000.0,
        )
    )
    f = compress_scores(snap).f_fundamental_trend
    assert f < 1.0  # 不再是净利 +269% 拉满的 1.0
    assert f < 0.5  # 双负现金流 → 显著惩罚


def test_fundamental_single_negative_cashflow_penalized():
    """单个负现金流（经营或自由）也惩罚，但轻于双负。"""
    both_neg = _snapshot(
        fundamental=FundamentalFactor(
            revenue_yoy=30.0,
            net_profit_yoy=30.0,
            eps_growth_yoy=30.0,
            operating_cashflow=-1.0,
            free_cashflow=-1.0,
        )
    )
    single_neg = _snapshot(
        fundamental=FundamentalFactor(
            revenue_yoy=30.0,
            net_profit_yoy=30.0,
            eps_growth_yoy=30.0,
            operating_cashflow=-1.0,
            free_cashflow=1.0,
        )
    )
    clean = _snapshot(fundamental=FundamentalFactor(revenue_yoy=30.0, net_profit_yoy=30.0, eps_growth_yoy=30.0))
    f_both = compress_scores(both_neg).f_fundamental_trend
    f_single = compress_scores(single_neg).f_fundamental_trend
    f_clean = compress_scores(clean).f_fundamental_trend
    assert f_both < f_single < f_clean


def test_fundamental_cashflow_missing_no_penalty():
    """现金流缺失 → 不惩罚（不静默假设为负）。"""
    snap = _snapshot(fundamental=FundamentalFactor(revenue_yoy=30.0, net_profit_yoy=30.0, eps_growth_yoy=30.0))
    assert compress_scores(snap).f_fundamental_trend == pytest.approx(1.0)


def test_fundamental_inline_no_beat():
    """net_profit_yoy 落在预告区间内 → in-line，不额外加分。"""
    snap = _snapshot(
        fundamental=FundamentalFactor(revenue_yoy=10.0, net_profit_yoy=20.0, eps_growth_yoy=15.0),
        expectation=ExpectationFactor(profit_forecast_min_change=10.0, profit_forecast_max_change=50.0),
    )
    # 纯增长分 = mean(saturate(10/30), saturate(20/30), saturate(15/30)) = 0.5
    assert compress_scores(snap).f_fundamental_trend == pytest.approx(0.5)


def test_fundamental_beat_bonus():
    """net_profit_yoy 超预告上限 → beat 加分（+_BEAT_BONUS）。"""
    beat = _snapshot(
        fundamental=FundamentalFactor(revenue_yoy=10.0, net_profit_yoy=80.0, eps_growth_yoy=15.0),
        expectation=ExpectationFactor(profit_forecast_min_change=10.0, profit_forecast_max_change=50.0),
    )
    base = _snapshot(fundamental=FundamentalFactor(revenue_yoy=10.0, net_profit_yoy=80.0, eps_growth_yoy=15.0))
    f_beat = compress_scores(beat).f_fundamental_trend
    f_base = compress_scores(base).f_fundamental_trend
    assert f_beat > f_base
    assert f_beat - f_base == pytest.approx(0.2)  # _BEAT_BONUS


# ─────────────────────────────────────────────────────────────────────────────
# F —— 结构性亮点（改造 E：segment divergence 正向修正）
# ─────────────────────────────────────────────────────────────────────────────


def test_fundamental_segment_divergence_bonus():
    """最快细分 +35.44% vs 整体 +17.94%（divergence 17.5pp > 15pp）→ F 正向修正 +0.2。"""
    snap = _snapshot(
        fundamental=FundamentalFactor(
            revenue_yoy=17.94,
            net_profit_yoy=17.94,
            eps_growth_yoy=17.94,
            segment_fastest_yoy=35.44,
        )
    )
    base = _snapshot(fundamental=FundamentalFactor(revenue_yoy=17.94, net_profit_yoy=17.94, eps_growth_yoy=17.94))
    f_with = compress_scores(snap).f_fundamental_trend
    f_base = compress_scores(base).f_fundamental_trend
    assert f_with - f_base == pytest.approx(0.2)  # _SEGMENT_DIVERGENCE_BONUS


def test_fundamental_segment_divergence_below_threshold_no_bonus():
    """divergence 不超过 15pp → 不加分。"""
    snap = _snapshot(
        fundamental=FundamentalFactor(
            revenue_yoy=20.0,
            net_profit_yoy=20.0,
            eps_growth_yoy=20.0,
            segment_fastest_yoy=35.0,  # divergence = 15.0（未超过阈值）
        )
    )
    base = _snapshot(fundamental=FundamentalFactor(revenue_yoy=20.0, net_profit_yoy=20.0, eps_growth_yoy=20.0))
    assert compress_scores(snap).f_fundamental_trend == compress_scores(base).f_fundamental_trend


# ─────────────────────────────────────────────────────────────────────────────
# E —— 上修 → 正（核心因子）
# ─────────────────────────────────────────────────────────────────────────────


def test_revision_upward_positive():
    snap = _snapshot(expectation=ExpectationFactor(eps_fy1_revision_1m=5.0))
    assert compress_scores(snap).e_revision == pytest.approx(0.3882, abs=1e-3)


def test_revision_downward_negative():
    snap = _snapshot(expectation=ExpectationFactor(eps_fy1_revision_1m=-5.0))
    assert compress_scores(snap).e_revision == pytest.approx(-0.3882, abs=1e-3)


def test_revision_antisaturation_45_vs_100():
    """反饱和：+45% 与 +100% 不再都 clip 到 1.0，保留幅度差异。"""
    s45 = compress_scores(_snapshot(expectation=ExpectationFactor(eps_fy1_revision_1m=45.0))).e_revision
    s100 = compress_scores(_snapshot(expectation=ExpectationFactor(eps_fy1_revision_1m=100.0))).e_revision
    assert s45 != s100
    assert s100 > s45
    assert s45 < 1.0  # 45% 不再饱和到 1.0
    assert s100 == pytest.approx(1.0)  # 100% → 归一化参考点 ±1


def test_revision_antisaturation_monotonic():
    """反饱和单调：上修幅度越大方向分越高（5% < 45% < 100%）。"""
    s5 = compress_scores(_snapshot(expectation=ExpectationFactor(eps_fy1_revision_1m=5.0))).e_revision
    s45 = compress_scores(_snapshot(expectation=ExpectationFactor(eps_fy1_revision_1m=45.0))).e_revision
    s100 = compress_scores(_snapshot(expectation=ExpectationFactor(eps_fy1_revision_1m=100.0))).e_revision
    assert 0.0 < s5 < s45 < s100


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
# C —— 启动期高换手豁免（改造 E：E 上修或 segment divergence 时扣分减半）
# ─────────────────────────────────────────────────────────────────────────────


def test_crowding_high_turnover_exempt_with_e_revision():
    # 高换手率分位扣分在 E 上修（e_revision>0.3）时减半
    base = _snapshot(crowding=CrowdingFactor(turnover_rate_percentile=0.9))
    up = _snapshot(
        crowding=CrowdingFactor(turnover_rate_percentile=0.9),
        expectation=ExpectationFactor(eps_fy1_revision_1m=45.0),  # e_revision ≈ 0.83 > 0.3
    )
    c_base = compress_scores(base).c_crowding
    c_up = compress_scores(up).c_crowding
    assert c_base < 0  # 高换手拥挤 → 负
    assert c_up > c_base  # 豁免后负向扣分减半
    assert c_up == pytest.approx(c_base * 0.5)  # 只含 turnover 分项 → 扣分减半


def test_crowding_high_turnover_exempt_with_segment_divergence():
    # 高换手率分位扣分在 segment divergence>15pp 时减半（结构性亮点豁免）
    base = _snapshot(crowding=CrowdingFactor(turnover_rate_percentile=0.9))
    seg = _snapshot(
        crowding=CrowdingFactor(turnover_rate_percentile=0.9),
        fundamental=FundamentalFactor(
            revenue_yoy=10.0, net_profit_yoy=10.0, eps_growth_yoy=10.0, segment_fastest_yoy=30.0
        ),
    )
    c_base = compress_scores(base).c_crowding
    c_seg = compress_scores(seg).c_crowding
    assert c_seg > c_base
    assert c_seg == pytest.approx(c_base * 0.5)


def test_crowding_low_turnover_positive_not_attenuated():
    """P1E-4：低换手（crowding_pct>0，正向）即使豁免也不衰减（豁免只作用于拥挤惩罚）。"""
    base = _snapshot(crowding=CrowdingFactor(turnover_rate_percentile=0.1))
    up = _snapshot(
        crowding=CrowdingFactor(turnover_rate_percentile=0.1),
        expectation=ExpectationFactor(eps_fy1_revision_1m=45.0),  # e_revision ≈ 0.83 > 0.3
    )
    c_base = compress_scores(base).c_crowding
    c_up = compress_scores(up).c_crowding
    assert c_base > 0  # 低换手 → 正（冷门利好）
    assert c_up == pytest.approx(c_base)  # 正向贡献不衰减


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
# M —— regime 软约束（改造 E：E↑ + segment divergence → 暴露上下限上浮）
# ─────────────────────────────────────────────────────────────────────────────


def test_adjust_market_exposure_lift_when_both_conditions():
    market = MarketFactor(regime="熊市", position_low=0.0, position_high=0.2)
    low, high = adjust_market_exposure(market, e_revision=0.5, segment_divergence=20.0)
    assert low == pytest.approx(0.1)  # 0.0 → 0.1（下限上浮）
    assert high == pytest.approx(0.3)  # 0.2 → 0.3


def test_adjust_market_exposure_lift_when_either_condition():
    """P1 放宽：E↑ **或** segment divergence 任一成立即可上浮（原为 AND）。"""
    market = MarketFactor(regime="熊市", position_low=0.0, position_high=0.2)
    # 仅 E↑ → lift
    low, high = adjust_market_exposure(market, e_revision=0.5, segment_divergence=5.0)
    assert low == pytest.approx(0.1)
    assert high == pytest.approx(0.3)
    # 仅 segment divergence → lift
    low, high = adjust_market_exposure(market, e_revision=0.1, segment_divergence=20.0)
    assert low == pytest.approx(0.1)
    assert high == pytest.approx(0.3)


def test_adjust_market_exposure_no_lift_when_neither_condition():
    market = MarketFactor(regime="熊市", position_low=0.0, position_high=0.2)
    # 都缺失 / 都不达标 → 不 lift
    assert adjust_market_exposure(market, e_revision=None, segment_divergence=None) == (0.0, 0.2)
    assert adjust_market_exposure(market, e_revision=0.1, segment_divergence=5.0) == (0.0, 0.2)


def test_compress_scores_market_summary_regime_lift():
    snap = _snapshot(
        fundamental=FundamentalFactor(
            revenue_yoy=10.0, net_profit_yoy=10.0, eps_growth_yoy=10.0, segment_fastest_yoy=30.0
        ),
        expectation=ExpectationFactor(eps_fy1_revision_1m=45.0),  # e_revision ≈ 0.83 > 0.3
        market=MarketFactor(market_score=40.0, regime="熊市", position_low=0.0, position_high=0.2),
    )
    m = compress_scores(snap).m
    assert m["position_low"] == pytest.approx(0.1)
    assert m["position_high"] == pytest.approx(0.3)


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
