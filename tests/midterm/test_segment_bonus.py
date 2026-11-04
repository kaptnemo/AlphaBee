"""P1-6：分部 bonus 期间对齐 + 10% 收入占比门槛 + 按产品分项 的单元测试。

口径依据 ``MIDTERM_F_DATA_GOVERNANCE_DESIGN`` §12：

- ``legacy``（score_engine 函数缺省）：只看 divergence（旧行为，无期间/占比检查）；
- ``period_aligned``（生产默认，由 decision_model 显式传入）：分部报告期与财报报告期
  均可知且不同 ⇒ 0；最快分部收入占比已知且 <10% ⇒ 0；任一期间/占比缺失 ⇒ fail-open
  （旧行为）。

全部合成数据，无网络；直接调用 score_engine（缺省 legacy），生产口径由 keyword 显式开启。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from alphabee.midterm.factors import _SEGMENT_MIN_SHARE, _segment_extremes, _segment_extremes_v2
from alphabee.midterm.models import ExpectationFactor, FactorSnapshot, FundamentalFactor
from alphabee.midterm.score_engine import (
    SEGMENT_BONUS_MODE_LEGACY,
    SEGMENT_BONUS_MODE_PERIOD_ALIGNED,
    _segment_bonus,
    compress_scores,
    fundamental_breakdown,
)

_BONUS = 0.2


def _factor(**overrides) -> FundamentalFactor:
    """002130-like 基础帧：整体营收 +22.34%，最快分部 +42.34%（divergence=20pp）。"""
    values: dict = {
        "revenue_yoy": 22.34,
        "net_profit_yoy": 10.0,
        "eps_growth_yoy": 10.0,
        "segment_fastest_yoy": 42.34,
        "segment_period": "20260630",
        "report_period": "20260930",
        "segment_fastest_share": 50.0,
    }
    values.update(overrides)
    return FundamentalFactor(**values)


def _bd(f: FundamentalFactor, mode: str | None = None) -> dict:
    if mode is None:
        return fundamental_breakdown(f, ExpectationFactor())
    return fundamental_breakdown(f, ExpectationFactor(), segment_bonus_mode=mode)


# ─────────────────────────────────────────────────────────────────────────────
# 期间对齐：错期不产 bonus（002130-like：H1 分部 vs Q3 财报）
# ─────────────────────────────────────────────────────────────────────────────


def test_002130_like_period_mismatch_suppresses_bonus():
    """002130-like：segment_period=20260630、report_period=20260930、divergence=20 ⇒ 0。"""
    f = _factor()
    legacy = _bd(f, SEGMENT_BONUS_MODE_LEGACY)
    aligned = _bd(f, SEGMENT_BONUS_MODE_PERIOD_ALIGNED)

    assert legacy["segment_bonus"] == pytest.approx(_BONUS)  # 旧行为（错期也加）
    assert aligned["segment_bonus"] == 0.0  # P1-6：错期不产
    assert aligned["segment_period_mismatch"] == 1.0
    assert aligned["segment_share_below_min"] == 0.0
    assert aligned["f"] == pytest.approx(legacy["f"] - _BONUS)


def test_same_period_still_gets_bonus():
    """同期 H1（segment_period == report_period）+ divergence 20 ⇒ +0.2（保留旧语义）。"""
    f = _factor(report_period="20260630")
    bd = _bd(f, SEGMENT_BONUS_MODE_PERIOD_ALIGNED)

    assert bd["segment_bonus"] == pytest.approx(_BONUS)
    assert bd["segment_period_mismatch"] == 0.0
    assert bd["segment_share_below_min"] == 0.0


def test_missing_period_fails_open_to_legacy_behaviour():
    """任一期间缺失 ⇒ fail-open（旧行为）：period_aligned 仍加 0.2。"""
    for overrides in ({"segment_period": ""}, {"report_period": ""}, {"segment_period": "", "report_period": ""}):
        bd = _bd(_factor(**overrides), SEGMENT_BONUS_MODE_PERIOD_ALIGNED)
        assert bd["segment_bonus"] == pytest.approx(_BONUS)
        assert bd["segment_period_mismatch"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 收入占比门槛：小分部不构成结构性亮点
# ─────────────────────────────────────────────────────────────────────────────


def test_small_fastest_share_suppresses_bonus():
    """fastest_share=2%、yoy=50、整体=10 ⇒ 0（占比 <10）；legacy 仍 +0.2。"""
    f = _factor(
        segment_fastest_yoy=60.0,
        revenue_yoy=10.0,  # divergence = 50pp
        segment_fastest_share=2.0,
        report_period="20260630",  # 期间对齐，只让占比门槛触发
    )
    legacy = _bd(f, SEGMENT_BONUS_MODE_LEGACY)
    aligned = _bd(f, SEGMENT_BONUS_MODE_PERIOD_ALIGNED)

    assert legacy["segment_bonus"] == pytest.approx(_BONUS)
    assert aligned["segment_bonus"] == 0.0
    assert aligned["segment_share_below_min"] == 1.0
    assert aligned["segment_period_mismatch"] == 0.0


def test_unknown_share_fails_open():
    """share 缺失（None）⇒ fail-open：不因占比未知而误杀 bonus。"""
    bd = _bd(_factor(segment_fastest_share=None, report_period="20260630"), SEGMENT_BONUS_MODE_PERIOD_ALIGNED)
    assert bd["segment_bonus"] == pytest.approx(_BONUS)
    assert bd["segment_share_below_min"] == 0.0


def test_divergence_threshold_unchanged_in_both_modes():
    """divergence ≤ 15 ⇒ 两口径都不产 bonus（阈值逻辑不变）。"""
    f = _factor(revenue_yoy=22.0, segment_fastest_yoy=37.0, report_period="20260630")  # divergence = 15.0
    for mode in (SEGMENT_BONUS_MODE_LEGACY, SEGMENT_BONUS_MODE_PERIOD_ALIGNED):
        assert _bd(f, mode)["segment_bonus"] == 0.0
    assert _segment_bonus(f, SEGMENT_BONUS_MODE_PERIOD_ALIGNED) == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 提取层：按产品分类优先 / 无产品回落全部 / 返回 period/basis/share
# ─────────────────────────────────────────────────────────────────────────────


def _collection() -> SimpleNamespace:
    items = [
        {"category": "按产品分类", "report_date": "20260630", "revenue_yoy": 42.34, "revenue_share": 50.0},
        {"category": "按地区分类", "report_date": "20260630", "revenue_yoy": 70.0, "revenue_share": 60.0},
        {"category": "按产品分类", "report_date": "20260630", "revenue_yoy": 80.0, "revenue_share": 2.0},
    ]
    return SimpleNamespace(
        latest_period="20260630",
        segments=items,
        latest_segments=lambda: [s for s in items if s["report_date"] == "20260630"],
    )


def test_segment_extremes_v2_prefers_product_category():
    """category 混合：只取「按产品分类」池（最快 42.34），不采用按地区的 70。"""
    fastest, slowest, period, basis, share = _segment_extremes_v2(_collection())

    assert basis == "product"
    assert period == "20260630"
    assert fastest == pytest.approx(42.34)  # 产品池内 42.34；地区 70 不采用
    assert slowest == pytest.approx(42.34)  # 产品池内 2% 小分部被剔除，只剩一条
    assert share == pytest.approx(50.0)


def test_segment_extremes_v2_falls_back_to_all_without_product():
    """无「按产品分类」分项 ⇒ 回落全部（basis=all），不静默丢信息。"""
    segments = SimpleNamespace(
        latest_period="20260630",
        segments=[{"category": "按地区分类", "report_date": "20260630", "revenue_yoy": 30.0, "revenue_share": 60.0}],
    )
    fastest, slowest, period, basis, share = _segment_extremes_v2(segments)

    assert basis == "all"
    assert period == "20260630"
    assert fastest == pytest.approx(30.0)
    assert slowest == pytest.approx(30.0)
    assert share == pytest.approx(60.0)


def test_segment_extremes_v2_min_share_constant_and_empty_result():
    """占比常量=10；空结果 ⇒ (None, None, latest_period, \"all\", None)（不伪造）。"""
    assert _SEGMENT_MIN_SHARE == pytest.approx(10.0)
    empty = SimpleNamespace(latest_period="20260630", segments=[{"category": "按产品分类", "revenue_yoy": None}])
    assert _segment_extremes_v2(empty) == (None, None, "20260630", "all", None)
    assert _segment_extremes_v2(None) == (None, None, "", "all", None)


def test_segment_extremes_wrapper_is_compatible():
    """兼容包装：`_segment_extremes` 返回 v2 的前两项（旧调用不破坏）。"""
    fastest, slowest = _segment_extremes(_collection())
    assert (fastest, slowest) == (_segment_extremes_v2(_collection())[:2])


# ─────────────────────────────────────────────────────────────────────────────
# compress_scores 链路透传（生产路径由 decision_model 显式传入 period_aligned）
# ─────────────────────────────────────────────────────────────────────────────


def test_compress_scores_passes_segment_bonus_mode():
    """compress_scores(segment_bonus_mode=...)：默认 legacy 与 period_aligned 的 F 差 0.2。"""
    snap = FactorSnapshot(symbol="002130.SZ", fundamental=_factor())

    f_default = compress_scores(snap).f_fundamental_trend
    f_legacy = compress_scores(snap, segment_bonus_mode="legacy").f_fundamental_trend
    f_aligned = compress_scores(snap, segment_bonus_mode="period_aligned").f_fundamental_trend

    assert f_default == pytest.approx(f_legacy)  # 函数缺省旧行为
    assert f_legacy - f_aligned == pytest.approx(_BONUS)  # 错期帧不再加 bonus
