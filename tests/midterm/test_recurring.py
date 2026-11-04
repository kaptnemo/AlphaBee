"""P1-3：扣非保守锚 + ``f_recurring_mode`` 三档（off / shadow / on）的评分层单元测试。

口径依据 ``MIDTERM_F_DATA_GOVERNANCE_DESIGN`` §9：

- ``off``（score_engine 函数缺省）：净利项 = ``saturate(归母 yoy)``，**不写任何 recurring 键**；
- ``shadow``（生产缺省）：算 ``np_term_recurring`` / ``recurring_gap`` / ``f_recurring_shadow``
  并落盘，但 F 仍用归母项（**F 逐值不变**）；
- ``on``：净利项改用 ``saturate(min(归母, 扣非))``（**只压不抬**），``f == f_recurring_shadow``；
  扣非不可用（扣非同比缺失 / 上年同期扣非为负）⇒ 回落归母并写 ``recurring_unavailable=1``。

全部合成数据，无网络；直接调用 score_engine（函数缺省 off），shadow/on 由 keyword 显式开启。
"""

from __future__ import annotations

import pytest

from alphabee.midterm.models import ExpectationFactor, FactorSnapshot, FundamentalFactor
from alphabee.midterm.score_engine import (
    F_MARGINAL_WEIGHT,
    _marginal,
    _saturate,
    compress_scores,
    fundamental_breakdown,
)

_RECURRING_PREFIXES = ("np_term_recurring", "recurring_gap", "recurring_unavailable")


def _factor(**overrides) -> FundamentalFactor:
    """002517-like：归母 50.65% / 扣非 5.75%（正基数）；营收/EPS 各 +30% 饱和。

    L(归母) = mean(1, 1, 1) = 1.0；扣非锚代入后净利项 = saturate(5.75, 30) ≈ 0.191667。
    """
    values: dict = {
        "revenue_yoy": 30.0,
        "net_profit_yoy": 50.65,
        "eps_growth_yoy": 30.0,
        "net_profit_dedt": 5.75e8,
        "net_profit_dedt_yoy": 5.75,
        "net_profit_dedt_base_positive": True,
    }
    values.update(overrides)
    return FundamentalFactor(**values)


def _bd(f: FundamentalFactor, mode: str | None = None, *, e: ExpectationFactor | None = None) -> dict:
    expectation = e or ExpectationFactor()
    if mode is None:
        return fundamental_breakdown(f, expectation)
    return fundamental_breakdown(f, expectation, f_recurring_mode=mode)


def _has_recurring_keys(breakdown: dict) -> bool:
    return any(key.startswith(_RECURRING_PREFIXES) for key in breakdown)


# ─────────────────────────────────────────────────────────────────────────────
# off：现状逐值不变
# ─────────────────────────────────────────────────────────────────────────────


def test_off_writes_no_recurring_keys_and_matches_default():
    """off（缺省）⇒ 不写任何 recurring 键，且与不传参数的分解逐值相同。"""
    f = _factor()
    default = _bd(f)
    explicit_off = _bd(f, "off")

    assert default == explicit_off
    assert not _has_recurring_keys(default)
    assert default["f"] == pytest.approx(1.0)  # 归母项饱和


@pytest.mark.parametrize("mode", ["bogus", "", None, "ON_?"])
def test_invalid_recurring_mode_falls_back_to_off(mode):
    """非法模式串 ⇒ 归一 off（绝不静默升级为会改变 F 的 on）。"""
    f = _factor()
    assert _bd(f, mode) == _bd(f)  # type: ignore[arg-type]


# ─────────────────────────────────────────────────────────────────────────────
# shadow：算但不进 F（F 逐值不变）
# ─────────────────────────────────────────────────────────────────────────────


def test_002517_like_shadow_writes_keys_but_keeps_f():
    """002517-like：shadow 写 np_term_recurring≈0.192 / recurring_gap / f_recurring_shadow，F 不变。"""
    f = _factor()
    off = _bd(f)
    shadow = _bd(f, "shadow")

    assert shadow["f"] == pytest.approx(off["f"])  # F 逐值不变（G-7 同构）
    assert shadow["raw"] == pytest.approx(off["raw"])
    assert shadow["np_term_recurring"] == pytest.approx(5.75 / 30.0, abs=1e-9)  # ≈0.191667
    assert shadow["recurring_gap"] == pytest.approx(50.65 - 5.75)  # pp
    assert shadow["f_recurring_shadow"] == pytest.approx(
        (1.0 + 5.75 / 30.0 + 1.0) / 3.0, abs=1e-9
    )  # 扣非项代入后的 F（shadow 模式不含边际项）
    assert shadow["f_recurring_shadow"] < shadow["f"]
    assert "recurring_unavailable" not in shadow


def test_shadow_f_unchanged_for_broad_fixture():
    """任意夹具下 shadow 的 F/raw/既有键与 off 逐值相同（只多诊断键）。"""
    f = _factor(revenue_yoy=10.0, net_profit_yoy=20.0, eps_growth_yoy=15.0, net_profit_dedt_yoy=5.0)
    off, shadow = _bd(f), _bd(f, "shadow")
    for key in ("f", "raw", "growth_mean", "net_profit_term", "ocf_penalty", "beat_bonus"):
        assert shadow[key] == pytest.approx(off[key])
    assert shadow.keys() - off.keys() == {
        "np_term_recurring",
        "recurring_gap",
        "f_recurring_shadow",
    }


# ─────────────────────────────────────────────────────────────────────────────
# on：净利项改用 min(归母, 扣非)；f == f_recurring_shadow
# ─────────────────────────────────────────────────────────────────────────────


def test_002517_like_on_uses_recurring_term_and_lowers_f():
    """002517-like：on 净利项降到 0.192 ⇒ F 下降；f == f_recurring_shadow。"""
    f = _factor()
    off, on = _bd(f), _bd(f, "on")

    assert on["np_term_recurring"] == pytest.approx(5.75 / 30.0, abs=1e-9)
    assert on["f"] == pytest.approx((1.0 + 5.75 / 30.0 + 1.0) / 3.0, abs=1e-9)
    assert on["f"] == pytest.approx(on["f_recurring_shadow"])  # 文档：on 下两者恒等
    assert on["f"] < off["f"]
    # 净利项审计键仍是归母口径（np_term_recurring 另列），避免审计混淆
    assert on["net_profit_term"] == pytest.approx(1.0)
    assert "recurring_unavailable" not in on


def test_recurring_anchor_only_presses_never_lifts():
    """min 只压不抬：扣非 yoy 高于归母时仍取归母 ⇒ on 的 F 与 off 相同。"""
    f = _factor(net_profit_yoy=10.0, net_profit_dedt_yoy=20.0)
    off, on = _bd(f), _bd(f, "on")

    assert on["np_term_recurring"] == pytest.approx(10.0 / 30.0, abs=1e-9)  # 取归母 10%
    assert on["f"] == pytest.approx(off["f"])  # 不因扣非更高而抬高


# ─────────────────────────────────────────────────────────────────────────────
# 可用性回落（负基数 / 扣非缺失）
# ─────────────────────────────────────────────────────────────────────────────


def test_000002_like_negative_base_falls_back_to_parent():
    """000002-like：归母 14.53 / 扣非 18.68 但上年同期扣非为负 ⇒ unavailable=1，on 回落归母。"""
    f = _factor(
        net_profit_yoy=14.53,
        net_profit_dedt_yoy=18.68,
        net_profit_dedt_base_positive=False,
    )
    off, shadow, on = _bd(f), _bd(f, "shadow"), _bd(f, "on")

    for breakdown in (shadow, on):
        assert breakdown["recurring_unavailable"] == 1.0
        assert "np_term_recurring" not in breakdown
        assert "f_recurring_shadow" not in breakdown
        # 两者都可算 ⇒ gap 照常落盘（pp，归母 − 扣非）
        assert breakdown["recurring_gap"] == pytest.approx(14.53 - 18.68)
    assert on["f"] == pytest.approx(off["f"])  # 回落归母项


def test_missing_dedt_falls_back_to_parent():
    """扣非同比缺失（base_positive 也 None）⇒ recurring_unavailable=1，on 回落归母。"""
    f = _factor(net_profit_dedt_yoy=None, net_profit_dedt_base_positive=None)
    off, on = _bd(f), _bd(f, "on")

    assert on["recurring_unavailable"] == 1.0
    assert on["f"] == pytest.approx(off["f"])
    assert "np_term_recurring" not in on
    assert "recurring_gap" not in on  # 扣非同比缺失 ⇒ 不可算，不写


# ─────────────────────────────────────────────────────────────────────────────
# 与 P0-1 期间守卫组合：不同期预告 ⇒ beat=0，但扣非照常
# ─────────────────────────────────────────────────────────────────────────────


def test_recurring_composes_with_beat_period_guard():
    """预告不同期 ⇒ beat=0（P0-1 守卫优先）；扣非锚仍正常计算。"""
    f = _factor(report_period="20260930")
    e = ExpectationFactor(
        profit_forecast_min_change=-100.0,
        profit_forecast_max_change=-90.0,  # 同期时 50.65 > -90 ⇒ beat +0.2
        profit_forecast_period="20260630",  # 错期
    )
    shadow = _bd(f, "shadow", e=e)

    assert shadow["forecast_period_matched"] == 0.0
    assert shadow["beat_bonus"] == 0.0  # 守卫优先，不因扣非改变
    assert shadow["np_term_recurring"] == pytest.approx(5.75 / 30.0, abs=1e-9)


# ─────────────────────────────────────────────────────────────────────────────
# 边际项 M 不因扣非替换改变
# ─────────────────────────────────────────────────────────────────────────────


def test_marginal_term_unchanged_by_recurring_replacement():
    """同一 marginal_mode 下 M 不因扣非锚改变；只有净利项（及 F）变化。"""
    f = _factor(
        q_revenue_yoy=40.0,
        q_net_profit_yoy=60.0,
        q_gross_margin_pp=2.0,
    )
    assert _marginal(f) is not None

    # off/on 的边际键需在**同一 marginal_mode（on）**下比较：off 档不算边际项。
    base = fundamental_breakdown(f, ExpectationFactor(), marginal_mode="on")
    on = fundamental_breakdown(f, ExpectationFactor(), marginal_mode="on", f_recurring_mode="on")
    for key in ("marginal_raw", "marginal_quality", "marginal", "f_marginal_source"):
        assert on[key] == pytest.approx(base[key])

    # on 的混合增长项 = (1−W)·L_recurring + W·M；与 off 差异仅来自 L 的净利项替换
    growth_recurring = (on["revenue_term"] + on["np_term_recurring"] + on["eps_term"]) / 3.0
    expected_growth = (1.0 - F_MARGINAL_WEIGHT) * growth_recurring + F_MARGINAL_WEIGHT * on["marginal"]
    assert on["f"] == pytest.approx(
        _saturate(
            expected_growth + on["ocf_penalty"] + on["beat_bonus"] + on["miss_penalty"] + on["segment_bonus"],
            1.0,
        ),
        abs=1e-9,
    )


# ─────────────────────────────────────────────────────────────────────────────
# compress_scores 透传（生产路径由 decision_model 显式传入 shadow/on）
# ─────────────────────────────────────────────────────────────────────────────


def test_compress_scores_passes_recurring_mode():
    """compress_scores(f_recurring_mode=...)：off/shadow 的 F 相同，on 用扣非锚后下降。"""
    snap = FactorSnapshot(symbol="002517.SZ", fundamental=_factor())
    f_default = compress_scores(snap).f_fundamental_trend
    f_off = compress_scores(snap, f_recurring_mode="off").f_fundamental_trend
    f_shadow = compress_scores(snap, f_recurring_mode="shadow").f_fundamental_trend
    f_on = compress_scores(snap, f_recurring_mode="on").f_fundamental_trend

    assert f_default == pytest.approx(f_off)  # 函数缺省 off = 旧行为
    assert f_shadow == pytest.approx(f_off)  # shadow 不进 F
    assert f_on == pytest.approx((1.0 + 5.75 / 30.0 + 1.0) / 3.0, abs=1e-9)
    assert f_on < f_off


# ─────────────────────────────────────────────────────────────────────────────
# f_shadow 反事实验证（t12 repair）：边际影子必须用扣非替换后的 growth_for_f
# ─────────────────────────────────────────────────────────────────────────────


def _marginal_ready() -> FundamentalFactor:
    """002517-like + q_* 齐备：M = mean(sat((40−30)/25), sat((60−50.65)/25))×1 = 0.387。"""
    return _factor(
        q_revenue_yoy=40.0,
        q_net_profit_yoy=60.0,
        q_gross_margin_pp=2.0,
    )


def test_recurring_on_marginal_shadow_counterfactual_uses_recurring_term():
    """recurring=on + marginal=shadow：f_shadow 是「在扣非模式下把边际打开」的反事实。

    f_shadow_delta 必须等于「marginal=on 反事实 F − 当前 f」（002517-like + q_* 下
    ≈ −0.103067）；旧口径用归母 growth 会把扣非替换差误算进边际效应（≈ +0.085544）。
    """
    f = _marginal_ready()
    shadow = fundamental_breakdown(f, ExpectationFactor(), marginal_mode="shadow", f_recurring_mode="on")
    on = fundamental_breakdown(f, ExpectationFactor(), marginal_mode="on", f_recurring_mode="on")

    assert shadow["f_shadow"] == pytest.approx(on["f"], abs=1e-12)  # 反事实 = 打开的 F
    assert shadow["f_shadow_delta"] == pytest.approx(on["f"] - shadow["f"], abs=1e-12)
    assert shadow["f_shadow_delta"] == pytest.approx(-0.103067, abs=1e-6)
    assert shadow["f_shadow_delta"] < 0  # M=0.387 < L_recurring=0.7306 ⇒ 打开边际后 F 下降


def test_recurring_on_marginal_on_f_shadow_equals_f():
    """recurring=on + marginal=on：f_shadow 与 f 同源同净利项 ⇒ delta 恰为 0。"""
    f = _marginal_ready()
    on = fundamental_breakdown(f, ExpectationFactor(), marginal_mode="on", f_recurring_mode="on")

    assert on["f_shadow"] == pytest.approx(on["f"], abs=1e-12)
    assert on["f_shadow_delta"] == pytest.approx(0.0, abs=1e-12)


def test_f_shadow_unchanged_when_recurring_off_or_shadow():
    """recurring=off/shadow 时 growth_for_f == growth ⇒ f_shadow 与改动前逐值相同。"""
    f = _marginal_ready()
    baseline = fundamental_breakdown(f, ExpectationFactor(), marginal_mode="shadow")  # recurring 缺省 off
    for mode in ("off", "shadow"):
        breakdown = fundamental_breakdown(f, ExpectationFactor(), marginal_mode="shadow", f_recurring_mode=mode)
        assert breakdown["f_shadow"] == pytest.approx(baseline["f_shadow"])
        assert breakdown["f_shadow_delta"] == pytest.approx(baseline["f_shadow_delta"])
