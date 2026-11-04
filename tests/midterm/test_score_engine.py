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
from alphabee.midterm.score_engine import (
    _ANCHOR_WEIGHT,
    _RESEARCH_WEIGHT,
    _forecast_revision_anchor,
    _research_revision,
    adjust_market_exposure,
    compress_scores,
    fundamental_breakdown,
)


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
            coverage_count=3,  # P0 §9.2：覆盖达标（≥3）才允许研报修正参与 E 方向分
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


def test_fundamental_negative_ocf_penalized():
    """profit_without_cash：经营现金流为负 → F 扣 0.5（与自由现金流无关，L1 后）。"""
    snap = _snapshot(
        fundamental=FundamentalFactor(
            revenue_yoy=30.0,
            net_profit_yoy=269.0,
            eps_growth_yoy=30.0,
            operating_cashflow=-7_490_000_000.0,
            free_cashflow=-8_650_000_000.0,
        )
    )
    # 三因子全部饱和到 +1 ⇒ 均值 1.0；仅经营现金流惩罚 −0.5
    assert compress_scores(snap).f_fundamental_trend == pytest.approx(0.5)


def test_fundamental_free_cashflow_no_longer_affects_f():
    """L1（2026-11-01）：**自由现金流移出 F** —— FCF 正/负对 F 零影响（交给 derived facts）。"""
    common = dict(revenue_yoy=30.0, net_profit_yoy=30.0, eps_growth_yoy=30.0, operating_cashflow=1.0)
    positive = _snapshot(fundamental=FundamentalFactor(**common, free_cashflow=1.0))
    negative = _snapshot(fundamental=FundamentalFactor(**common, free_cashflow=-8_650_000_000.0))
    assert compress_scores(positive).f_fundamental_trend == pytest.approx(1.0)
    assert compress_scores(negative).f_fundamental_trend == pytest.approx(1.0)


def test_fundamental_ocf_penalty_is_single_step():
    """经营现金流为负只扣一档 −0.5（旧口径双负会扣 −1.0）。"""
    neg = _snapshot(
        fundamental=FundamentalFactor(
            revenue_yoy=30.0, net_profit_yoy=30.0, eps_growth_yoy=30.0, operating_cashflow=-1.0
        )
    )
    clean = _snapshot(
        fundamental=FundamentalFactor(
            revenue_yoy=30.0, net_profit_yoy=30.0, eps_growth_yoy=30.0, operating_cashflow=1.0
        )
    )
    delta = compress_scores(clean).f_fundamental_trend - compress_scores(neg).f_fundamental_trend
    assert delta == pytest.approx(0.5)


def test_fundamental_cashflow_missing_no_penalty():
    """现金流缺失 → 不惩罚（不静默假设为负）。"""
    snap = _snapshot(fundamental=FundamentalFactor(revenue_yoy=30.0, net_profit_yoy=30.0, eps_growth_yoy=30.0))
    assert compress_scores(snap).f_fundamental_trend == pytest.approx(1.0)


def test_fundamental_breakdown_decomposes_600760_case():
    """L1：600760 实帧的 F 分解逐项可复核 —— F=−0.933（错期 beat 不产、FCF 已移出、OCF 为正无惩罚）。

    该帧财报期 2026Q3（``report_period=20260930``）与最新预告期 2026H1
    （``profit_forecast_period=20260630``）不同期 ⇒ P0-1 期间守卫 fail-closed：
    ``beat_bonus=0``（旧口径错期 +0.2 的 F=−0.733 消失）。
    """
    f = FundamentalFactor(
        revenue_yoy=-34.19,
        net_profit_yoy=-26.0,
        eps_growth_yoy=-28.0,
        operating_cashflow=1.026e09,
        free_cashflow=-1.671e08,  # 旧口径本会再扣 0.5，现已移出
        report_period="20260930",
    )
    e = ExpectationFactor(
        profit_forecast_min_change=-58.23,
        profit_forecast_max_change=-58.23,
        profit_forecast_period="20260630",  # H1 预告 ≠ Q3 财报
    )
    bd = fundamental_breakdown(f, e)
    assert bd is not None
    assert bd["revenue_term"] == pytest.approx(-1.0)  # −34.19 越过 ±30 饱和点
    assert bd["revenue_saturated"] == 1.0
    assert bd["net_profit_term"] == pytest.approx(-26.0 / 30.0)
    assert bd["eps_term"] == pytest.approx(-28.0 / 30.0)
    assert bd["growth_mean"] == pytest.approx((-1.0 - 26.0 / 30.0 - 28.0 / 30.0) / 3.0)
    assert bd["ocf_penalty"] == 0.0  # OCF 为正
    assert bd["forecast_period_matched"] == 0.0  # 20260930 vs 20260630
    assert bd["beat_bonus"] == 0.0  # 错期 ⇒ 不产 beat（fail-closed）
    assert bd["miss_penalty"] == 0.0
    assert bd["segment_bonus"] == 0.0  # −26.33 − (−34.19) = 7.86pp < 15
    assert bd["raw"] == pytest.approx(-0.933333, abs=1e-4)  # 去掉错期 beat 的 0.2
    assert bd["f"] == pytest.approx(-0.933333, abs=1e-4)  # 未越过 ±1，不饱和
    assert bd["input_saturated"] == 1.0  # 至少一项原始 YoY 越过饱和点
    assert bd["output_saturated"] == 0.0  # 合计未越过 ±1
    # 单一来源：方向分必须等于分解里的 f
    snap = _snapshot(fundamental=f, expectation=e)
    assert compress_scores(snap).f_fundamental_trend == pytest.approx(bd["f"])


def test_fundamental_breakdown_600760_same_period_yields_beat():
    """P0-1 同期夹具：2026Q3 财报 + 2026Q3 预告 ⇒ forecast_period_matched=1、beat=+0.2。

    同期时 beat 语义逐字不变（+_BEAT_BONUS）：F 从 −0.933333 回到 −0.733333。
    """
    f = FundamentalFactor(
        revenue_yoy=-34.19,
        net_profit_yoy=-26.0,
        eps_growth_yoy=-28.0,
        operating_cashflow=1.026e09,
        report_period="20260930",
    )
    e = ExpectationFactor(
        profit_forecast_min_change=-58.23,
        profit_forecast_max_change=-58.23,
        profit_forecast_period="2026-09-30",  # 归一后同期（带分隔符也可比）
    )
    bd = fundamental_breakdown(f, e)
    assert bd is not None
    assert bd["forecast_period_matched"] == 1.0
    assert bd["beat_bonus"] == pytest.approx(0.2)  # −26 > 预告上限 −58.23
    assert bd["raw"] == pytest.approx(-0.933333 + 0.2, abs=1e-4)
    assert bd["f"] == pytest.approx(-0.733333, abs=1e-4)


@pytest.mark.parametrize(
    ("report_period", "forecast_period"),
    [
        ("", ""),  # 手工构造快照：两处期间都缺
        ("20260930", ""),  # 预告期缺失
        ("", "20260930"),  # 财报期缺失
        ("20260930", "20260630"),  # 错期（H1 预告 vs Q3 财报）
    ],
)
@pytest.mark.parametrize(
    ("min_change", "max_change"),
    [
        (-58.23, -58.23),  # 同期时本应 beat（−26 > −58.23）
        (50.0, 100.0),  # 同期时本应 miss（50 − (−26) = 76pp > 20）
    ],
)
def test_fundamental_beat_miss_fail_closed_without_matching_periods(
    report_period: str,
    forecast_period: str,
    min_change: float,
    max_change: float,
):
    """P0-1：任一期间为空或不同期 ⇒ beat/miss 均为 0（fail-closed），且 matched=0。

    两个预告区间分别覆盖「同期会产 beat」与「同期会产 miss」两种方向，确保守卫对
    beat/miss 都生效（而不是只挡住其中一侧）。
    """
    f = FundamentalFactor(
        revenue_yoy=-34.19,
        net_profit_yoy=-26.0,
        eps_growth_yoy=-28.0,
        report_period=report_period,
    )
    e = ExpectationFactor(
        profit_forecast_min_change=min_change,
        profit_forecast_max_change=max_change,
        profit_forecast_period=forecast_period,
    )
    bd = fundamental_breakdown(f, e)
    assert bd is not None
    assert bd["forecast_period_matched"] == 0.0
    assert bd["beat_bonus"] == 0.0
    assert bd["miss_penalty"] == 0.0


def test_fundamental_miss_penalty_same_period_keeps_semantics():
    """P0-1：同期时 miss 语义逐字不变（实际低于预告下限 >20pp ⇒ −0.3）。"""
    f = FundamentalFactor(
        revenue_yoy=-10.0,
        net_profit_yoy=-100.0,
        eps_growth_yoy=-10.0,
        report_period="20260930",
    )
    e = ExpectationFactor(
        profit_forecast_min_change=-20.0,
        profit_forecast_max_change=-10.0,
        profit_forecast_period="20260930",
    )
    bd = fundamental_breakdown(f, e)
    assert bd is not None
    assert bd["forecast_period_matched"] == 1.0
    assert bd["miss_penalty"] == pytest.approx(-0.3)  # −20 − (−100) = 80pp > 20
    assert bd["beat_bonus"] == 0.0


def test_fundamental_breakdown_output_saturation_flag():
    """输出饱和标志：合计越过 ±1 时为 1.0（越界部分被 clip 掉）。"""
    f = FundamentalFactor(
        revenue_yoy=30.0,
        net_profit_yoy=200.0,
        eps_growth_yoy=30.0,
        report_period="20241231",  # P0-1：与预告同期才保留下面的 beat 断言
    )
    e = ExpectationFactor(
        profit_forecast_min_change=100.0,
        profit_forecast_max_change=150.0,
        profit_forecast_period="20241231",
    )
    bd = fundamental_breakdown(f, e)
    assert bd is not None
    assert bd["growth_mean"] == pytest.approx(1.0)  # 三项均饱和到 +1
    assert bd["forecast_period_matched"] == 1.0
    assert bd["beat_bonus"] == pytest.approx(0.2)  # 200 > 预告上限 150
    assert bd["raw"] == pytest.approx(1.2)
    assert bd["f"] == pytest.approx(1.0)  # clip
    assert bd["output_saturated"] == 1.0
    assert bd["ocf_penalty"] == 0.0  # 缺失（None）不惩罚


def test_fundamental_breakdown_all_missing_is_none():
    assert fundamental_breakdown(FundamentalFactor(), ExpectationFactor()) is None


def test_fundamental_inline_no_beat():
    """net_profit_yoy 落在预告区间内 → in-line，不额外加分（同期，P0-1）。"""
    snap = _snapshot(
        fundamental=FundamentalFactor(
            revenue_yoy=10.0,
            net_profit_yoy=20.0,
            eps_growth_yoy=15.0,
            report_period="20241231",
        ),
        expectation=ExpectationFactor(
            profit_forecast_min_change=10.0,
            profit_forecast_max_change=50.0,
            profit_forecast_period="20241231",
        ),
    )
    # 纯增长分 = mean(saturate(10/30), saturate(20/30), saturate(15/30)) = 0.5
    assert compress_scores(snap).f_fundamental_trend == pytest.approx(0.5)


def test_fundamental_beat_bonus():
    """net_profit_yoy 超预告上限 → beat 加分（+_BEAT_BONUS，同期，P0-1）。"""
    beat = _snapshot(
        fundamental=FundamentalFactor(
            revenue_yoy=10.0,
            net_profit_yoy=80.0,
            eps_growth_yoy=15.0,
            report_period="20241231",
        ),
        expectation=ExpectationFactor(
            profit_forecast_min_change=10.0,
            profit_forecast_max_change=50.0,
            profit_forecast_period="20241231",
        ),
    )
    base = _snapshot(
        fundamental=FundamentalFactor(
            revenue_yoy=10.0,
            net_profit_yoy=80.0,
            eps_growth_yoy=15.0,
            report_period="20241231",
        )
    )
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
    snap = _snapshot(expectation=ExpectationFactor(eps_fy1_revision_1m=5.0, coverage_count=3))
    assert compress_scores(snap).e_revision == pytest.approx(0.3882, abs=1e-3)


def test_revision_downward_negative():
    snap = _snapshot(expectation=ExpectationFactor(eps_fy1_revision_1m=-5.0, coverage_count=3))
    assert compress_scores(snap).e_revision == pytest.approx(-0.3882, abs=1e-3)


def test_revision_antisaturation_45_vs_100():
    """反饱和：+45% 与 +100% 不再都 clip 到 1.0，保留幅度差异。"""
    s45 = compress_scores(
        _snapshot(expectation=ExpectationFactor(eps_fy1_revision_1m=45.0, coverage_count=3))
    ).e_revision
    s100 = compress_scores(
        _snapshot(expectation=ExpectationFactor(eps_fy1_revision_1m=100.0, coverage_count=3))
    ).e_revision
    assert s45 != s100
    assert s100 > s45
    assert s45 < 1.0  # 45% 不再饱和到 1.0
    assert s100 == pytest.approx(1.0)  # 100% → 归一化参考点 ±1


def test_revision_antisaturation_monotonic():
    """反饱和单调：上修幅度越大方向分越高（5% < 45% < 100%）。"""
    s5 = compress_scores(_snapshot(expectation=ExpectationFactor(eps_fy1_revision_1m=5.0, coverage_count=3))).e_revision
    s45 = compress_scores(
        _snapshot(expectation=ExpectationFactor(eps_fy1_revision_1m=45.0, coverage_count=3))
    ).e_revision
    s100 = compress_scores(
        _snapshot(expectation=ExpectationFactor(eps_fy1_revision_1m=100.0, coverage_count=3))
    ).e_revision
    assert 0.0 < s5 < s45 < s100


def test_revision_breadth_neutral_zero():
    snap = _snapshot(expectation=ExpectationFactor(revision_breadth=0.5, coverage_count=3))
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
        expectation=ExpectationFactor(eps_fy1_revision_1m=45.0, coverage_count=3),  # e_revision ≈ 0.83 > 0.3
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
        expectation=ExpectationFactor(eps_fy1_revision_1m=45.0, coverage_count=3),  # e_revision ≈ 0.83 > 0.3
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
        expectation=ExpectationFactor(eps_fy1_revision_1m=45.0, coverage_count=3),  # e_revision ≈ 0.83 > 0.3
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


# ─────────────────────────────────────────────────────────────────────────────
# P0-6 §9.2：coverage 护栏 + E 锚重调
# ─────────────────────────────────────────────────────────────────────────────


def test_revision_coverage_below_threshold_returns_none_and_registers():
    snap = _snapshot(expectation=ExpectationFactor(eps_fy1_revision_1m=45.0, coverage_count=2))
    assert compress_scores(snap).e_revision is None
    assert "consensus_coverage_low" in snap.missing_facts


def test_revision_coverage_missing_returns_none_and_registers():
    snap = _snapshot(expectation=ExpectationFactor(eps_fy1_revision_1m=45.0))  # coverage None
    assert compress_scores(snap).e_revision is None
    assert "consensus_coverage_low" in snap.missing_facts


def test_revision_coverage_ok_not_registered():
    snap = _snapshot()  # coverage_count=3
    compress_scores(snap)
    assert "consensus_coverage_low" not in snap.missing_facts


def test_revision_anchor_research_blend_0_6_0_4():
    e = ExpectationFactor(
        forecast_revision_anchor=20.0,  # 相邻期预告区间中点差 +20% → anchor
        eps_fy1_revision_1m=10.0,  # research
        coverage_count=3,
    )
    anchor = _forecast_revision_anchor(e)
    research = _research_revision(e)
    assert anchor is not None and research is not None
    expected = _ANCHOR_WEIGHT * anchor + _RESEARCH_WEIGHT * research
    assert compress_scores(_snapshot(expectation=e)).e_revision == pytest.approx(expected)


def test_revision_anchor_only_when_research_missing():
    e = ExpectationFactor(forecast_revision_anchor=-10.0, coverage_count=3)
    assert compress_scores(_snapshot(expectation=e)).e_revision == pytest.approx(_forecast_revision_anchor(e))


def test_revision_research_only_when_anchor_missing():
    e = ExpectationFactor(eps_fy1_revision_1m=5.0, coverage_count=3)
    assert compress_scores(_snapshot(expectation=e)).e_revision == pytest.approx(_research_revision(e))


def test_revision_both_missing_returns_none():
    e = ExpectationFactor(coverage_count=3)
    assert compress_scores(_snapshot(expectation=e)).e_revision is None


def test_revision_forecast_level_without_adjacent_period_does_not_contribute():
    """无相邻期 ⇒ anchor=None ⇒ research 单独承载；E 不因预告水平乐观而无条件为正。"""
    e = ExpectationFactor(
        profit_forecast_min_change=100.0,
        profit_forecast_max_change=200.0,  # 预告水平乐观，但无 forecast_revision_anchor
        coverage_count=3,
    )
    assert _forecast_revision_anchor(e) is None
    assert compress_scores(_snapshot(expectation=e)).e_revision is None  # research 也为空


def test_revision_anchor_positive_from_adjacent_period_upgrade():
    """相邻期预告区间中点上修 ⇒ 锚为正 ⇒ 正 E 贡献。"""
    e = ExpectationFactor(forecast_revision_anchor=20.0, coverage_count=3)
    assert _forecast_revision_anchor(e) > 0
    assert compress_scores(_snapshot(expectation=e)).e_revision > 0


def test_revision_anchor_negative_from_adjacent_period_downgrade():
    e = ExpectationFactor(forecast_revision_anchor=-20.0, coverage_count=3)
    assert _forecast_revision_anchor(e) < 0
    assert compress_scores(_snapshot(expectation=e)).e_revision < 0


# ─────────────────────────────────────────────────────────────────────────────
# F 边际改善三档（F-Marginal §6.3）：off 缺省零变化 / shadow 只算不生效 / on 混合
# ─────────────────────────────────────────────────────────────────────────────


def _marginal_fund(**overrides) -> FundamentalFactor:
    """600760 口径的单季派生输入（PR-1 数据层产物）。"""
    values: dict = {
        "revenue_yoy": -34.19,
        "net_profit_yoy": -26.0,
        "eps_growth_yoy": -28.0,
        "q_revenue_yoy": 23.29,
        "q_net_profit_yoy": 137.0,
        "q_gross_margin": 9.63,
        "q_gross_margin_pp": -3.4,
        # P0-1：与 `_marginal_exp` 同期，保留本组用例的 beat 语义（+0.2）。
        "report_period": "20260930",
    }
    values.update(overrides)
    return FundamentalFactor(**values)


def _marginal_exp() -> ExpectationFactor:
    return ExpectationFactor(
        profit_forecast_min_change=-58.23,
        profit_forecast_max_change=-58.23,
        profit_forecast_period="20260930",  # P0-1：与 `_marginal_fund` 同期
    )


_MARGINAL_KEY_PREFIXES = ("marginal", "f_shadow", "f_marginal_source")


def test_fundamental_breakdown_off_default_has_no_marginal_keys():
    """off（缺省）⇒ 完全没有边际键：既有直接调用点逐值/逐键不变。"""
    f = _marginal_fund()
    e = _marginal_exp()
    default = fundamental_breakdown(f, e)
    explicit_off = fundamental_breakdown(f, e, marginal_mode="off")
    assert default == explicit_off
    assert not any(key.startswith(_MARGINAL_KEY_PREFIXES) for key in default)


def test_fundamental_breakdown_shadow_adds_keys_but_keeps_f():
    """shadow ⇒ 追加 6 个边际键，F/raw/beat 等 L1 键逐值不变（G-7）。"""
    f = _marginal_fund()
    e = _marginal_exp()
    off = fundamental_breakdown(f, e)
    shadow = fundamental_breakdown(f, e, marginal_mode="shadow")

    assert shadow["f"] == pytest.approx(off["f"])
    assert shadow["beat_bonus"] == pytest.approx(off["beat_bonus"])  # shadow 不折半
    assert shadow["raw"] == pytest.approx(off["raw"])
    assert shadow["marginal_raw"] == pytest.approx(1.0)
    assert shadow["marginal_quality"] == pytest.approx(0.2)
    assert shadow["marginal"] == pytest.approx(0.2)
    assert shadow["f_marginal_source"] == 7.0
    # f_shadow 是「on 模式下的 F」⇒ 已包含 §4.4 的 beat 折半（0.2 → 0.1）
    assert shadow["f_shadow"] == pytest.approx(0.7 * off["growth_mean"] + 0.3 * 0.2 + 0.1, abs=1e-9)
    assert shadow["f_shadow_delta"] == pytest.approx(shadow["f_shadow"] - shadow["f"], abs=1e-12)


def test_fundamental_breakdown_on_mixes_growth_and_shadow_equals_f():
    """on ⇒ f_growth=(1−W)·L+W·M；f_shadow 与 f 相同（delta=0），beat 按 §4.4 折半。"""
    f = _marginal_fund()
    e = _marginal_exp()
    off = fundamental_breakdown(f, e)
    on = fundamental_breakdown(f, e, marginal_mode="on")

    expected_growth = 0.7 * off["growth_mean"] + 0.3 * 0.2
    expected = expected_growth + on["ocf_penalty"] + on["beat_bonus"] + on["miss_penalty"] + on["segment_bonus"]
    assert on["f"] == pytest.approx(expected, abs=1e-9)
    assert on["beat_bonus"] == pytest.approx(0.1)  # Q<1 ⇒ 折半
    assert on["f_shadow"] == pytest.approx(on["f"])
    assert on["f_shadow_delta"] == pytest.approx(0.0, abs=1e-12)
    assert on["f"] > off["f"]


def test_fundamental_breakdown_invalid_mode_falls_back_to_off():
    """直接调用传非法模式 ⇒ 最保守 off（绝不静默升级为 shadow/on）。"""
    f = _marginal_fund()
    e = _marginal_exp()
    assert fundamental_breakdown(f, e, marginal_mode="bogus") == fundamental_breakdown(f, e)
    assert fundamental_breakdown(f, e, marginal_mode=None) == fundamental_breakdown(f, e)


def test_compress_scores_passes_marginal_mode():
    """compress_scores 透传：off/shadow 的 F 相同，on 用混合后 F（等于分解里的 f）。"""
    snap_off = _snapshot(fundamental=_marginal_fund(), expectation=_marginal_exp())
    snap_shadow = _snapshot(fundamental=_marginal_fund(), expectation=_marginal_exp())
    snap_on = _snapshot(fundamental=_marginal_fund(), expectation=_marginal_exp())

    off = compress_scores(snap_off)
    shadow = compress_scores(snap_shadow, marginal_mode="shadow")
    on = compress_scores(snap_on, marginal_mode="on")

    assert shadow.f_fundamental_trend == pytest.approx(off.f_fundamental_trend)
    assert on.f_fundamental_trend == pytest.approx(
        fundamental_breakdown(_marginal_fund(), _marginal_exp(), marginal_mode="on")["f"]
    )
    assert on.f_fundamental_trend > off.f_fundamental_trend
