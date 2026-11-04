"""F 边际改善评分层的单元测试：设计 §8.2 S1–S7 + §7.1 G-1~G-7。

全部合成 ``FundamentalFactor`` / ``FactorSnapshot``，不发起网络请求、不读外部状态。
参数（§4.1）在测试里以独立常量复核；§7.2 参数冻结后按 ±0.05 容差同步。

口径要点（设计 §4）：

- ``Δ = 单季同比 − 累计同比``（加速度，不是单季水平——否则与 L 重复计分）；
- ``M_raw = mean(saturate(Δ, 25))``（可单腿）、``Q = clip(1 + pp/4, 0.2, 1.0)``、``M = M_raw × Q``；
- ``on``：``f_growth = 0.7·L + 0.3·M``；``shadow``：``f_growth = L``，只写 ``f_shadow``/``ΔF``；
- 降级（两腿都缺 / 质量腿缺失 / 无单季数据）⇒ F 与 off **逐值相等**（G-2/G-3 降级恒等）。

**已知设计内部冲突（本实现按规则实现，测试按实际值钉住）**：§4.4 要求 ``on`` + ``Q<1`` 时
``beat *= 0.5``，而 §8.2 S1 / 附录 B 的 ``on ≈ −0.393`` 是**未折半**口径。600760 帧实测
``on = −0.4933``（折半后），差值恰为 ``beat 折半`` 的 0.10；两者都落在 §7.1 G-1 的
``[−0.55, −0.30]`` 内。S1 同时钉住两个口径（见 ``test_s1_600760_three_modes``）。
"""

from __future__ import annotations

import pytest

from alphabee.midterm import classifier_v2
from alphabee.midterm.factors import derive_marginal
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
    VariableScores,
)
from alphabee.midterm.score_engine import (
    F_ACCEL_SCALE,
    F_MARGIN_DELTA_SCALE,
    F_MARGIN_QUALITY_FLOOR,
    F_MARGINAL_WEIGHT,
    _marginal,
    _marginal_source,
    compress_scores,
    fundamental_breakdown,
)

# 测试内的独立参数副本（§4.1）——与生产常量逐值一致；冻结后按 §7.2 容差同步。
_ACCEL = 25.0
_WEIGHT = 0.30
_DELTA_SCALE = 4.0
_FLOOR = 0.20
_BEAT = 0.2


# ─────────────────────────────────────────────────────────────────────────────
# 合成输入
# ─────────────────────────────────────────────────────────────────────────────


def _600760_fund(**overrides) -> FundamentalFactor:
    """600760 实帧口径（§1.2 / 附录 B）：累计三降、单季量利双升、毛利率同比 −3.4pp。"""
    values: dict = {
        "revenue_yoy": -34.19,
        "net_profit_yoy": -26.0,
        "eps_growth_yoy": -28.0,
        "operating_cashflow": 1.026e09,
        "free_cashflow": -1.671e08,  # 已移出 F，不影响
        "q_revenue_yoy": 23.29,
        "q_net_profit_yoy": 137.0,
        "q_gross_margin": 9.63,
        "q_gross_margin_pp": -3.4,
        # P0-1 期间对齐：本文件聚焦边际层，故与 `_600760_exp` 取同期，保留既有
        # beat 语义（真实帧的错期归属由 test_score_engine 的 600760 用例覆盖）。
        "report_period": "20260930",
    }
    values.update(overrides)
    return FundamentalFactor(**values)


def _600760_exp(**overrides) -> ExpectationFactor:
    values: dict = {
        "profit_forecast_min_change": -58.23,
        "profit_forecast_max_change": -58.23,
        "profit_forecast_period": "20260930",  # P0-1：与 `_600760_fund` 同期
    }
    values.update(overrides)
    return ExpectationFactor(**values)


def _snapshot(fundamental: FundamentalFactor, expectation: ExpectationFactor | None = None) -> FactorSnapshot:
    return FactorSnapshot(
        symbol="600760.SH",
        as_of_date="2026-11-01",
        fundamental=fundamental,
        expectation=expectation or ExpectationFactor(),
        trend=TrendFactor(rs_stock_market_20d=10.0, rs_stock_market_60d=5.0),
        valuation=ValuationFactor(pe_ttm_5y_percentile=0.3, pb_5y_percentile=0.4),
        crowding=CrowdingFactor(holder_count_change=5.0, hot_rank=80, turnover_rate_percentile=0.4),
        risk=RiskFactor(pledge_ratio=10.0, debt_to_assets=40.0, audit=AuditSnapshot(audit_opinion="标准无保留意见")),
        market=MarketFactor(market_score=60.0, regime="震荡"),
    )


def _variant_f(
    f: FundamentalFactor,
    e: ExpectationFactor,
    *,
    use_quality: bool = True,
    weight: float = _WEIGHT,
    use_delta: bool = True,
) -> float:
    """S7 变异体：在测试内局部重算 F（**不修改生产代码**，仅用于证明门会变红）。

    与生产公式的差别只有被变异的那一处：

    - ``use_quality=False``：去掉质量门（Q 恒 1）——G-1 反例；
    - ``weight=0.0``：边际权重置 0（等价于只用水平项）——G-3 反例；
    - ``use_delta=False``：用单季**水平** ``q_yoy`` 替代加速度 Δ——G-3 重复计分反例。
    """
    growth_terms = [v for v in (f.revenue_yoy, f.net_profit_yoy, f.eps_growth_yoy) if v is not None]
    assert growth_terms
    growth = sum(max(-1.0, min(1.0, v / 30.0)) for v in growth_terms) / len(growth_terms)

    if use_delta:
        legs: list[float] = []
        if f.q_revenue_yoy is not None and f.revenue_yoy is not None:
            legs.append(f.q_revenue_yoy - f.revenue_yoy)
        if f.q_net_profit_yoy is not None and f.net_profit_yoy is not None:
            legs.append(f.q_net_profit_yoy - f.net_profit_yoy)
    else:
        legs = [v for v in (f.q_revenue_yoy, f.q_net_profit_yoy) if v is not None]
    if not legs:
        return max(-1.0, min(1.0, growth))
    m_raw = sum(max(-1.0, min(1.0, leg / _ACCEL)) for leg in legs) / len(legs)
    quality = 1.0
    if use_quality and f.q_gross_margin_pp is not None:
        quality = max(_FLOOR, min(1.0, 1.0 + f.q_gross_margin_pp / _DELTA_SCALE))
    m = m_raw * quality

    beat = 0.0
    if f.net_profit_yoy is not None and e.profit_forecast_max_change is not None:
        if f.net_profit_yoy > e.profit_forecast_max_change:
            beat = _BEAT
            if quality < 1.0:  # §4.4 去重
                beat *= 0.5
    f_growth = (1.0 - weight) * growth + weight * m
    return max(-1.0, min(1.0, f_growth + beat))


# ─────────────────────────────────────────────────────────────────────────────
# S1 600760 三档（§0 表 / 附录 B）
# ─────────────────────────────────────────────────────────────────────────────


def test_s1_600760_three_modes():
    """S1：现行 −0.733 / on −0.4933（§4.4 折半）/ Q=1 对照 −0.153（±1e-4）。"""
    f = _600760_fund()
    e = _600760_exp()

    current = fundamental_breakdown(f, e)  # 缺省 off
    shadow = fundamental_breakdown(f, e, marginal_mode="shadow")
    on = fundamental_breakdown(f, e, marginal_mode="on")

    assert current["f"] == pytest.approx(-0.733333, abs=1e-4)
    assert shadow["f"] == pytest.approx(-0.733333, abs=1e-4)  # shadow 不生效
    assert on["f"] == pytest.approx(-0.493333, abs=1e-4)  # §4.4：Q<1 且 beat 生效 ⇒ beat 折半
    # 设计 §8.2/附录 B 的 −0.393 是**未折半**口径：差值恰为 beat 折半的 0.10
    assert on["f"] + 0.1 == pytest.approx(-0.393333, abs=1e-4)
    assert on["beat_bonus"] == pytest.approx(0.1)
    assert current["beat_bonus"] == pytest.approx(0.2)
    assert on["marginal_raw"] == pytest.approx(1.0)  # Δ_rev=57.48 / Δ_np=163 均饱和
    assert on["marginal_quality"] == pytest.approx(0.2)  # 1 + (−3.4)/4 = 0.15 → clip 到 0.2
    assert on["marginal"] == pytest.approx(0.2)

    # Q=1 对照（毛利率同比转正 ⇒ 质量门满格，beat 不折半）⇒ 设计 §0 的 −0.153
    q1 = fundamental_breakdown(f.model_copy(update={"q_gross_margin_pp": 2.0}), e, marginal_mode="on")
    assert q1["marginal_quality"] == pytest.approx(1.0)
    assert q1["f"] == pytest.approx(-0.153333, abs=1e-4)
    # 三档都满足 G-1 的 F 区间（on 的两口径均在 [−0.55, −0.30]）
    assert -0.55 <= on["f"] <= -0.30


def test_g1_600760_on_mode_state_and_adverse_divergence():
    """G-1：on 模式下 600760 帧仍是 S5 且顶背离存在（不得被低基数季度抹成「没信号」）。"""
    on = fundamental_breakdown(_600760_fund(), _600760_exp(), marginal_mode="on")
    assert -0.55 <= on["f"] <= -0.30

    # 其余五因子取 600760 实帧方向分（与 test_decision_model v2 用例同源）
    scores = VariableScores(
        f_fundamental_trend=on["f"],
        e_revision=-0.5169,
        t_relative_strength=1.0,
        v_valuation_percentile=0.6015,
        c_crowding=-0.1902,
        r_risk=-0.08741,
    )
    result = classifier_v2.classify_state(scores, order="tef")
    assert result.state.argmax_state == "S5"
    assert result.adverse_divergence is not None


# ─────────────────────────────────────────────────────────────────────────────
# S2 降级恒等 / G-2
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["shadow", "on"])
def test_s2_no_single_quarter_data_degrades_identically(mode: str):
    """S2/G-2：无单季数据 ⇒ on/shadow 的分解与 F 与 off **逐键逐值**相同。"""
    e = _600760_exp()
    f_no_q = FundamentalFactor(revenue_yoy=-34.19, net_profit_yoy=-26.0, eps_growth_yoy=-28.0)
    assert fundamental_breakdown(f_no_q, e, marginal_mode=mode) == fundamental_breakdown(f_no_q, e)
    assert compress_scores(_snapshot(f_no_q, e), marginal_mode=mode).f_fundamental_trend == pytest.approx(
        compress_scores(_snapshot(f_no_q, e)).f_fundamental_trend
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"q_gross_margin_pp": None},  # 质量腿缺失 ⇒ 降级（质量门是边际项成立的必要条件）
        {"q_revenue_yoy": None, "q_net_profit_yoy": None},  # 两条加速度腿都缺 ⇒ 降级
    ],
)
def test_s2_partial_single_quarter_data_still_degrades(updates: dict):
    f = _600760_fund(**updates)
    e = _600760_exp()
    assert fundamental_breakdown(f, e, marginal_mode="on") == fundamental_breakdown(f, e)
    assert _marginal(f) is None


def test_q1_marginal_degrades_to_none():
    """P0-3：Q1 单季=累计 ⇒ 加速度不可识别；shadow/on 均无边际键，on 与 off 逐值相等。"""
    f = FundamentalFactor(
        revenue_yoy=25.0,
        net_profit_yoy=20.0,
        eps_growth_yoy=10.0,
        q_revenue_yoy=25.0,  # == revenue_yoy（Q1 的「单季」就是累计）
        q_net_profit_yoy=20.0,  # == net_profit_yoy
        q_gross_margin=30.0,
        q_gross_margin_pp=1.0,  # 质量腿本身有值，仍不得抵消「加速度不可识别」
        report_period="20240331",
        marginal_period="20240331",  # Q1
    )
    e = ExpectationFactor()

    off = fundamental_breakdown(f, e)
    shadow = fundamental_breakdown(f, e, marginal_mode="shadow")
    on = fundamental_breakdown(f, e, marginal_mode="on")
    assert off is not None and shadow is not None and on is not None

    assert _marginal(f) is None  # Q1：Δ 结构性为 0 ⇒ 降级而非「M=0」
    marginal_prefixes = ("marginal", "f_shadow", "f_marginal_source")
    for breakdown in (shadow, on):
        assert not any(key.startswith(marginal_prefixes) for key in breakdown), "Q1 帧不得写任何边际键"
    # shadow 显式留痕；on 不写该键（保证 on 与 off 逐值相等，F 回落水平项 L）
    assert shadow["q1_marginal_degraded"] == 1.0
    assert "q1_marginal_degraded" not in off
    assert "q1_marginal_degraded" not in on
    assert on == off
    assert shadow["f"] == pytest.approx(off["f"])
    assert shadow["raw"] == pytest.approx(off["raw"])
    assert compress_scores(_snapshot(f, e), marginal_mode="on").f_fundamental_trend == pytest.approx(off["f"])


# ─────────────────────────────────────────────────────────────────────────────
# S3 影子不生效 / G-7
# ─────────────────────────────────────────────────────────────────────────────


def test_s3_shadow_has_f_shadow_but_f_unchanged():
    """S3/G-7：shadow 下 F 与 off 逐值相等，但 f_shadow 有值且 ΔF 正确。"""
    f = _600760_fund()
    e = _600760_exp()
    off = fundamental_breakdown(f, e)
    shadow = fundamental_breakdown(f, e, marginal_mode="shadow")
    on = fundamental_breakdown(f, e, marginal_mode="on")

    assert shadow["f"] == off["f"]
    assert shadow["f_shadow"] == pytest.approx(on["f"])  # shadow 是 on 的预演
    assert shadow["f_shadow_delta"] == pytest.approx(shadow["f_shadow"] - shadow["f"], abs=1e-12)
    assert shadow["f_shadow_delta"] == pytest.approx(0.24, abs=1e-4)


def test_g7_shadow_mode_does_not_change_any_score():
    """G-7：shadow 模式下七方向分与 off 逐值相等（只有分解多键）。"""
    snap = _snapshot(_600760_fund(), _600760_exp())
    off_scores = compress_scores(snap)
    shadow_scores = compress_scores(
        _snapshot(_600760_fund(), _600760_exp()),
        marginal_mode="shadow",
    )
    assert off_scores.model_dump() == shadow_scores.model_dump()


# ─────────────────────────────────────────────────────────────────────────────
# S4 beat 去重（§4.4）
# ─────────────────────────────────────────────────────────────────────────────


def test_s4_beat_dedup_only_in_on_mode_with_quality_deterioration():
    """S4：on + marginal 非 None + Q<1 ⇒ beat 折半；shadow/off 不折半；Q=1 不折半。"""
    f = _600760_fund()
    e = _600760_exp()
    off = fundamental_breakdown(f, e)
    shadow = fundamental_breakdown(f, e, marginal_mode="shadow")
    on = fundamental_breakdown(f, e, marginal_mode="on")
    q1 = fundamental_breakdown(f.model_copy(update={"q_gross_margin_pp": 2.0}), e, marginal_mode="on")

    assert off["beat_bonus"] == pytest.approx(_BEAT)
    assert shadow["beat_bonus"] == pytest.approx(_BEAT)
    assert on["beat_bonus"] == pytest.approx(_BEAT * 0.5)
    assert q1["beat_bonus"] == pytest.approx(_BEAT)  # Q=1 ⇒ 不去重

    # shadow 的 F 必须仍是「未折半」口径（否则 F 会变，违反 G-7）
    assert shadow["f"] == off["f"]
    # on 的 F 仍被边际项抬高（折半的 beat 只少 0.1，边际项带来 +0.24）
    assert on["f"] > shadow["f"]


def test_s4_beat_dedup_requires_beat():
    """无 beat（in-line / 无预告区间）时 §4.4 不产生任何影响。"""
    # 实际净利同比 −26 落预告区间 [−30, −20] 内 ⇒ in-line，无 beat 也无 miss
    e = ExpectationFactor(profit_forecast_min_change=-30.0, profit_forecast_max_change=-20.0)
    f = _600760_fund()
    on = fundamental_breakdown(f, e, marginal_mode="on")
    shadow = fundamental_breakdown(f, e, marginal_mode="shadow")
    assert on["beat_bonus"] == pytest.approx(0.0)
    assert on["f"] == shadow["f_shadow"]  # 无 beat ⇒ on 与 shadow 预演一致


# ─────────────────────────────────────────────────────────────────────────────
# S5 边界（亏损转盈 / 单腿 / Q1 与年报桥接）
# ─────────────────────────────────────────────────────────────────────────────


def test_g5_loss_to_profit_uses_revenue_leg_only():
    """G-5：上年同期净利 ≤ 0（q_net_profit_yoy=None）⇒ 只用营收腿，净利百分比不得产生 M。"""
    f = FundamentalFactor(
        revenue_yoy=0.0,
        net_profit_yoy=-100.0,
        eps_growth_yoy=-100.0,
        q_revenue_yoy=30.0,
        q_net_profit_yoy=None,
        q_gross_margin=10.0,
        q_gross_margin_pp=1.0,
    )
    bd = fundamental_breakdown(f, ExpectationFactor(), marginal_mode="on")
    assert bd is not None
    assert bd["f_marginal_source"] == 5.0  # 1（营收腿）| 4（质量腿）
    assert bd["marginal_raw"] == pytest.approx(1.0)  # Δ_rev=+30pp（单腿，不补 0）
    assert _marginal(f) == (1.0, 1.0, 1.0)  # Q=clip(1+1/4,0.2,1)=1.0


def test_s5_single_leg_can_be_net_profit_only():
    """净利腿单独可用（营收同比缺失）⇒ source=6，M_raw 只用净利腿。"""
    f = FundamentalFactor(
        revenue_yoy=None,
        net_profit_yoy=0.0,
        eps_growth_yoy=10.0,
        q_revenue_yoy=30.0,
        q_net_profit_yoy=25.0,
        q_gross_margin=10.0,
        q_gross_margin_pp=0.0,
    )
    bd = fundamental_breakdown(f, ExpectationFactor(), marginal_mode="shadow")
    assert bd is not None
    assert bd["f_marginal_source"] == 6.0  # 2（净利腿）| 4（质量腿）
    assert bd["marginal_raw"] == pytest.approx(1.0)  # Δ_np=25 → 饱和 +1
    assert bd["marginal_quality"] == pytest.approx(1.0)


def test_s5_q1_and_annual_data_bridge_into_scoring():
    """S5：数据层仍产出 Q1/年报的 4 个 q_*；评分层 Q1 降级、年报照常消费（source=7）。"""
    q1 = derive_marginal(
        {
            "income": [
                {"period": "20230331", "revenue": 80.0, "net_profit_attr_p": 10.0},
                {"period": "20240331", "revenue": 100.0, "net_profit_attr_p": 12.0},
            ],
            "fina": [{"period": "20230331", "gross_margin": 30.0}, {"period": "20240331", "gross_margin": 32.0}],
        }
    )
    annual = derive_marginal(
        {
            "income": [
                {"period": "20230930", "revenue": 300.0, "net_profit_attr_p": 30.0},
                {"period": "20231231", "revenue": 500.0, "net_profit_attr_p": 50.0},
                {"period": "20240930", "revenue": 450.0, "net_profit_attr_p": 45.0},
                {"period": "20241231", "revenue": 700.0, "net_profit_attr_p": 70.0},
            ],
            "fina": [
                {"period": "20230930", "gross_margin": 20.0},
                {"period": "20231231", "gross_margin": 21.0},
                {"period": "20240930", "gross_margin": 22.0},
                {"period": "20241231", "gross_margin": 24.0},
            ],
        }
    )
    # 数据层不变：Q1 / 年报都产出完整 4 个 q_*（降级只发生在评分层）
    for q in (q1, annual):
        assert all(value is not None for value in q.values())

    q_keys = ("q_revenue_yoy", "q_net_profit_yoy", "q_gross_margin", "q_gross_margin_pp")
    # Q1 帧：q_rev==rev、q_np==np、q_gm_pp 有值 ⇒ 评分层仍降级（不写边际键、on==off）
    q1_factor = FundamentalFactor(
        revenue_yoy=q1["q_revenue_yoy"],
        net_profit_yoy=q1["q_net_profit_yoy"],
        eps_growth_yoy=10.0,
        **{name: q1[name] for name in q_keys},
        marginal_period="20240331",
    )
    off = fundamental_breakdown(q1_factor, ExpectationFactor())
    on = fundamental_breakdown(q1_factor, ExpectationFactor(), marginal_mode="on")
    assert _marginal(q1_factor) is None
    assert on == off
    assert off is not None and not any(key.startswith(("marginal", "f_shadow", "f_marginal_source")) for key in off)

    # 年报帧：边际项正常参与（两条加速度腿 + 质量腿齐备 ⇒ source=7）
    annual_factor = FundamentalFactor(
        revenue_yoy=10.0,
        net_profit_yoy=10.0,
        eps_growth_yoy=10.0,
        **{name: annual[name] for name in q_keys},
        marginal_period="20241231",
    )
    bd = fundamental_breakdown(annual_factor, ExpectationFactor(), marginal_mode="shadow")
    assert bd is not None
    assert bd["f_marginal_source"] == 7.0  # 两条加速度腿 + 质量腿齐备
    assert "marginal" in bd and "f_shadow" in bd


# ─────────────────────────────────────────────────────────────────────────────
# S6 有界 / 连续 / 单调（§4.6）
# ─────────────────────────────────────────────────────────────────────────────


def test_s6_bounded():
    """S6-1 / §7.1 G-6（有界）：M ∈ [-1,1]、Q ∈ [0.2,1]、由 M 混合后的 F ∈ [-1,1]。"""
    for delta in (-500.0, -100.0, -25.0, 0.0, 25.0, 100.0, 500.0):
        for pp in (-500.0, -4.0, 0.0, 4.0, 500.0):
            f = FundamentalFactor(
                revenue_yoy=-delta,
                net_profit_yoy=-delta,
                eps_growth_yoy=-delta,
                q_revenue_yoy=0.0,
                q_net_profit_yoy=0.0,
                q_gross_margin_pp=pp,
            )
            marginal = _marginal(f)
            assert marginal is not None
            m_raw, quality, m = marginal
            assert -1.0 <= m_raw <= 1.0
            assert _FLOOR - 1e-12 <= quality <= 1.0
            assert -1.0 <= m <= 1.0
            assert abs(m) <= abs(m_raw) + 1e-12
            bd = fundamental_breakdown(f, ExpectationFactor(), marginal_mode="on")
            assert bd is not None and -1.0 <= bd["f"] <= 1.0


def test_s6_continuous_in_single_quarter_and_pp():
    """S6-2 / §7.1 G-6（连续）：M_raw 对 Δ、Q 对 pp 连续（在饱和点/夹取点上仍连续）。"""
    eps = 1e-6

    def _bd(q_revenue_yoy: float, pp: float) -> dict:
        f = FundamentalFactor(
            revenue_yoy=0.0,
            net_profit_yoy=0.0,
            eps_growth_yoy=0.0,
            q_revenue_yoy=q_revenue_yoy,
            q_net_profit_yoy=q_revenue_yoy,
            q_gross_margin_pp=pp,
        )
        bd = fundamental_breakdown(f, ExpectationFactor(), marginal_mode="on")
        assert bd is not None
        return bd

    # Δ 在饱和点 F_ACCEL_SCALE 两侧：M_raw 连续
    below, above = _bd(_ACCEL - eps, 0.0), _bd(_ACCEL + eps, 0.0)
    assert above["marginal_raw"] - below["marginal_raw"] < 1e-4
    # Q 在质量门下限（pp = (FLOOR−1)×scale = −3.2）两侧：Q 连续（被夹到 0.2）
    pp_floor = (_FLOOR - 1.0) * _DELTA_SCALE
    left, right = _bd(0.0, pp_floor - eps), _bd(0.0, pp_floor + eps)
    assert right["marginal_quality"] - left["marginal_quality"] < 1e-4
    assert left["marginal_quality"] == pytest.approx(_FLOOR)


def test_s6_monotonic_in_single_quarter_values():
    """S6-3 / §7.1 G-6（单调）：固定其余项、只抬**单季**值 ⇒ Δ↑ ⇒ M_raw/F 不降。"""

    def _f(q: float) -> FundamentalFactor:
        return FundamentalFactor(
            revenue_yoy=0.0,
            net_profit_yoy=0.0,
            eps_growth_yoy=0.0,
            q_revenue_yoy=q,
            q_net_profit_yoy=q,
            q_gross_margin_pp=0.0,
        )

    values = [-40.0, -25.0, -10.0, 0.0, 10.0, 25.0, 40.0]
    breakdowns = [fundamental_breakdown(_f(q), ExpectationFactor(), marginal_mode="on") for q in values]
    m_raws = [bd["marginal_raw"] for bd in breakdowns if bd is not None]
    fs = [bd["f"] for bd in breakdowns if bd is not None]
    assert m_raws == sorted(m_raws)
    assert fs == sorted(fs)


def test_s6_cumulative_tilt_is_not_claimed_monotonic():
    """S6-3 口径说明：抬**累计**值会同时抬高 L 与压低 Δ（基准效应）⇒ 不得断言严格单调。"""
    base = FundamentalFactor(
        revenue_yoy=0.0,
        net_profit_yoy=0.0,
        eps_growth_yoy=0.0,
        q_revenue_yoy=30.0,
        q_net_profit_yoy=30.0,
        q_gross_margin_pp=0.0,
    )
    higher_cumulative = base.model_copy(update={"revenue_yoy": 25.0, "net_profit_yoy": 25.0})
    base_bd = fundamental_breakdown(base, ExpectationFactor(), marginal_mode="on")
    cum_bd = fundamental_breakdown(higher_cumulative, ExpectationFactor(), marginal_mode="on")
    assert base_bd is not None and cum_bd is not None
    # 累计值上升 ⇒ 加速度变小（水平项 L 上升）——两种力量方向相反
    assert cum_bd["marginal_raw"] < base_bd["marginal_raw"]


# ─────────────────────────────────────────────────────────────────────────────
# S7 变异杀伤（局部重算实现，不落地生产代码）
# ─────────────────────────────────────────────────────────────────────────────


def test_s7_mutation_no_quality_gate_fails_g1():
    """S7-①：去掉质量门（Q 恒 1）⇒ 600760 的 on-F 跑出 G-1 区间（−0.153 ⇒ 顶背离被抹掉）。"""
    f = _600760_fund()
    e = _600760_exp()
    production = fundamental_breakdown(f, e, marginal_mode="on")
    mutated = _variant_f(f, e, use_quality=False)
    assert production is not None
    assert -0.55 <= production["f"] <= -0.30  # G-1 绿
    assert mutated == pytest.approx(-0.153333, abs=1e-4)  # 设计 §0 第三行反例
    assert not (-0.55 <= mutated <= -0.30)  # G-1 红
    # 该反例在状态机侧同样成立：F=−0.153 时不再是 S5 顶背离口径
    assert mutated - production["f"] > 0.3


def test_s7_mutation_zero_weight_fails_g3():
    """S7-②：边际权重置 0 ⇒ G-3 帧 F 退回水平项 0.3（< 0.45）⇒ 门红。"""
    f = FundamentalFactor(
        revenue_yoy=9.0,
        net_profit_yoy=9.0,
        eps_growth_yoy=9.0,  # L = 0.3
        q_revenue_yoy=39.0,  # Δ = +30pp
        q_net_profit_yoy=39.0,
        q_gross_margin_pp=2.0,  # Q = 1
    )
    e = ExpectationFactor()
    production = fundamental_breakdown(f, e, marginal_mode="on")
    assert production is not None
    assert production["f"] > 0.45  # G-3 绿（0.7×0.3 + 0.3×1 = 0.51）
    assert _variant_f(f, e, weight=0.0) == pytest.approx(0.3)  # 门红


def test_s7_mutation_level_instead_of_acceleration_double_counts():
    """S7-③：用单季水平 q_yoy 替代 Δ ⇒ 常年 +30% 被 L 与 M 各算一次（重复计分）。"""
    steady = FundamentalFactor(
        revenue_yoy=30.0,
        net_profit_yoy=30.0,
        eps_growth_yoy=30.0,  # L = 1.0（水平项已满）
        q_revenue_yoy=30.0,  # 无加速：Δ = 0
        q_net_profit_yoy=30.0,
        q_gross_margin_pp=0.0,
    )
    e = ExpectationFactor()
    production = fundamental_breakdown(steady, e, marginal_mode="on")
    assert production is not None
    assert production["marginal_raw"] == pytest.approx(0.0)  # 加速度为 0
    assert production["f"] == pytest.approx(0.7)  # 只有水平项
    mutated = _variant_f(steady, e, use_delta=False)
    assert mutated == pytest.approx(1.0)  # 单季水平 30% 再计一次 ⇒ 满格
    assert mutated - production["f"] > 0.25  # 重复计分幅度显眼 ⇒ 断言红


# ─────────────────────────────────────────────────────────────────────────────
# G-3 / G-4 校准门（固定其余项只动单季值，§4.6 第 3 条口径）
# ─────────────────────────────────────────────────────────────────────────────


def test_g3_acceleration_with_quality_scores_above_threshold():
    """G-3：L=+0.3、Δ_rev=Δ_np=+30pp、margin_pp=+2 ⇒ on-F > 0.45。"""
    f = FundamentalFactor(
        revenue_yoy=9.0,
        net_profit_yoy=9.0,
        eps_growth_yoy=9.0,
        q_revenue_yoy=39.0,
        q_net_profit_yoy=39.0,
        q_gross_margin_pp=2.0,
    )
    bd = fundamental_breakdown(f, ExpectationFactor(), marginal_mode="on")
    assert bd is not None
    assert bd["growth_mean"] == pytest.approx(0.3)
    assert bd["marginal_raw"] == pytest.approx(1.0)
    assert bd["marginal_quality"] == pytest.approx(1.0)
    expected = (1.0 - _WEIGHT) * 0.3 + _WEIGHT * 1.0
    assert bd["f"] == pytest.approx(expected, abs=1e-9)
    assert bd["f"] > 0.45


def test_g4_high_level_deceleration_scores_below_threshold():
    """G-4：L=+1.0、Δ_rev=Δ_np=−40pp ⇒ on-F < 0.55（off 下仍是 1.0 ⇒ 门会红）。"""
    f = FundamentalFactor(
        revenue_yoy=30.0,
        net_profit_yoy=30.0,
        eps_growth_yoy=30.0,
        q_revenue_yoy=-10.0,  # Δ = −40pp
        q_net_profit_yoy=-10.0,
        q_gross_margin_pp=0.0,
    )
    on = fundamental_breakdown(f, ExpectationFactor(), marginal_mode="on")
    off = fundamental_breakdown(f, ExpectationFactor())
    assert on is not None and off is not None
    assert off["f"] == pytest.approx(1.0)
    assert on["marginal_raw"] == pytest.approx(-1.0)
    assert on["f"] == pytest.approx(1.0 - _WEIGHT * 2.0)  # 0.7×1 + 0.3×(−1) = 0.4
    assert on["f"] < 0.55


# ─────────────────────────────────────────────────────────────────────────────
# 常量与位掩码契约
# ─────────────────────────────────────────────────────────────────────────────


def test_marginal_constants_match_design():
    """§4.1 参数与附录 A 速查一致（冻结后按 §7.2 ±0.05 容差复核聚合门）。"""
    assert F_MARGINAL_WEIGHT == pytest.approx(0.30, abs=0.05)
    assert F_ACCEL_SCALE == pytest.approx(25.0, abs=5.0)
    assert F_MARGIN_DELTA_SCALE == pytest.approx(4.0, abs=1.0)
    assert F_MARGIN_QUALITY_FLOOR == pytest.approx(0.20, abs=0.05)


def test_marginal_source_bitmask():
    """f_marginal_source 位掩码：1=营收腿、2=净利腿、4=质量腿；缺腿不置位。"""
    both = _600760_fund()
    assert _marginal_source(both) == 7
    revenue_only = both.model_copy(update={"q_net_profit_yoy": None})
    assert _marginal_source(revenue_only) == 5
    net_only = both.model_copy(update={"q_revenue_yoy": None})
    assert _marginal_source(net_only) == 6
    no_quality = both.model_copy(update={"q_gross_margin_pp": None})
    assert _marginal_source(no_quality) == 3  # 只有两条加速度腿，不足以成立边际项
    assert _marginal(no_quality) is None
    nothing = FundamentalFactor(revenue_yoy=-10.0, net_profit_yoy=-10.0, eps_growth_yoy=-10.0)
    assert _marginal_source(nothing) == 0  # 0=降级
    assert _marginal(nothing) is None
