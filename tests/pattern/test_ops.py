"""通用纯函数算子与结果契约的单元测试（与业务无关）。"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import FrozenInstanceError

import pytest

from alphabee.pattern.ops import (
    clip01,
    dn,
    entropy,
    entropy_normalized,
    log1p_saturate,
    mean_missing,
    neutral_band,
    normalize_evidence,
    not_yet,
    saturate,
    severe,
    softmax,
    up,
)
from alphabee.pattern.types import Belief, PatternResult, Transition

# ─────────────────────────────────────────────────────────────────────────────
# 值域 / 符号族
# ─────────────────────────────────────────────────────────────────────────────


def test_clip01_clamps_to_unit_interval() -> None:
    assert clip01(-1.0) == 0.0
    assert clip01(0.0) == 0.0
    assert clip01(0.5) == 0.5
    assert clip01(1.0) == 1.0
    assert clip01(2.0) == 1.0


@pytest.mark.parametrize(
    ("x", "expected"),
    [(None, 0.0), (-2.0, 0.0), (0.0, 0.0), (0.4, 0.4), (1.0, 1.0), (2.0, 1.0)],
)
def test_up_takes_positive_part(x: float | None, expected: float) -> None:
    assert up(x) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("x", "expected"),
    [(None, 0.0), (2.0, 0.0), (0.0, 0.0), (-0.4, 0.4), (-1.0, 1.0), (-2.0, 1.0)],
)
def test_dn_takes_negative_part(x: float | None, expected: float) -> None:
    assert dn(x) == pytest.approx(expected)


def test_saturate_is_symmetric_and_saturating() -> None:
    assert saturate(0.0, 10.0) == 0.0
    assert saturate(5.0, 10.0) == pytest.approx(0.5)
    assert saturate(10.0, 10.0) == pytest.approx(1.0)
    assert saturate(20.0, 10.0) == pytest.approx(1.0)
    assert saturate(-5.0, 10.0) == pytest.approx(-0.5)
    assert saturate(-10.0, 10.0) == pytest.approx(-1.0)
    assert saturate(-20.0, 10.0) == pytest.approx(-1.0)


def test_log1p_saturate_keeps_large_value_resolution() -> None:
    assert log1p_saturate(0.0, 100.0) == 0.0
    assert log1p_saturate(100.0, 100.0) == pytest.approx(1.0)
    assert log1p_saturate(-100.0, 100.0) == pytest.approx(-1.0)
    assert log1p_saturate(200.0, 100.0) == pytest.approx(1.0)
    assert log1p_saturate(-200.0, 100.0) == pytest.approx(-1.0)
    # 文档 §4.1 的示例：ref=100 时 45% → ≈0.83、5% → ≈0.39（不是线性饱和的 0.45 / 0.05）
    assert log1p_saturate(45.0, 100.0) == pytest.approx(0.83, abs=0.01)
    assert log1p_saturate(5.0, 100.0) == pytest.approx(0.39, abs=0.01)
    assert log1p_saturate(-45.0, 100.0) == pytest.approx(-0.83, abs=0.01)


# ─────────────────────────────────────────────────────────────────────────────
# 接近度 / 距离族（含 None 口径与反证据）
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("x", "expected"),
    [(0.0, 1.0), (0.2, 0.5), (-0.2, 0.5), (0.4, 0.0), (-0.4, 0.0), (0.8, 0.0), (None, 0.5)],
)
def test_neutral_band_decays_on_both_sides(x: float | None, expected: float) -> None:
    assert neutral_band(x, 0.4) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("x", "expected"),
    [(-0.6, 1.0), (0.0, 1.0), (0.15, 0.5), (0.3, 0.0), (0.6, -1.0), (None, 0.5)],
)
def test_not_yet_is_single_sided_and_can_go_negative(x: float | None, expected: float) -> None:
    assert not_yet(x, 0.3) == pytest.approx(expected)


def test_not_yet_negative_value_is_refuting_evidence() -> None:
    # 单侧语义：强上修 ⇒ 「尚未上修」不成立 ⇒ 负证据；双侧算子在同一输入上已衰减到 0。
    assert not_yet(0.9, 0.3) == pytest.approx(-2.0)
    assert neutral_band(0.9, 0.3) == pytest.approx(0.0)
    # 负向输入：单侧算子完全满足，双侧算子判为「不接近中性」。
    assert not_yet(-0.9, 0.3) == pytest.approx(1.0)
    assert neutral_band(-0.9, 0.3) == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("x", "expected"),
    [(0.0, 0.0), (1.0, 0.0), (-0.1, 0.0), (-0.2, 0.0), (-0.4, 0.5), (-0.6, 1.0), (-1.0, 1.0), (None, 0.0)],
)
def test_severe_only_penalizes_negative_side_with_dead_zone(x: float | None, expected: float) -> None:
    assert severe(x, 0.2, 0.6) == pytest.approx(expected)


# ─────────────────────────────────────────────────────────────────────────────
# 缺失口径（文档 §4.10 逐算子）
# ─────────────────────────────────────────────────────────────────────────────


def test_missing_value_semantics_follow_missing_matrix() -> None:
    assert up(None) == 0.0
    assert dn(None) == 0.0
    assert neutral_band(None, 0.3) == 0.5
    assert not_yet(None, 0.3) == 0.5
    assert severe(None, 0.2, 0.6) == 0.0
    assert mean_missing([None, None]) is None


def test_mean_missing_does_not_fill_zero() -> None:
    assert mean_missing([]) is None
    assert mean_missing([None]) is None
    assert mean_missing([None, None, None]) is None
    # 缺失不参与均值：0.6 与 [0.6, None] 结果相同，不被稀释成 0.3。
    assert mean_missing([0.6, None]) == pytest.approx(0.6)
    assert mean_missing([0.2, None, 0.4]) == pytest.approx(0.3)
    assert mean_missing([-1.0, 0.0, 1.0]) == pytest.approx(0.0)


# ─────────────────────────────────────────────────────────────────────────────
# 归一化 / 分布族
# ─────────────────────────────────────────────────────────────────────────────


def test_normalize_evidence_divides_by_cap_and_clips() -> None:
    evidence = {"reachable": 2.0, "over": 5.0, "negative": -1.0, "zero": 0.0}
    caps = {"reachable": 2.0, "over": 2.0, "negative": 2.0, "zero": 2.0, "unused": 1.0}
    assert normalize_evidence(evidence, caps) == {
        "reachable": 1.0,
        "over": 1.0,
        "negative": 0.0,
        "zero": 0.0,
    }


@pytest.mark.parametrize(
    "logits",
    [
        {"a": 0.0, "b": 0.0},
        {"a": 1.0, "b": 2.0, "c": 3.0},
        {"a": 1000.0, "b": -1000.0},
        {"a": 710.0, "b": 709.0, "c": 708.0},
        {"a": -1e300, "b": 1e300},
    ],
)
def test_softmax_is_numerically_stable_and_sums_to_one(logits: dict[str, float]) -> None:
    distribution = softmax(logits)
    assert sum(distribution.values()) == pytest.approx(1.0, abs=1e-12)
    assert set(distribution) == set(logits)
    assert all(math.isfinite(p) and p >= 0.0 for p in distribution.values())


def test_softmax_preserves_order_and_returns_uniform_for_equal_logits() -> None:
    uniform = softmax({"a": 0.0, "b": 0.0})
    assert uniform == pytest.approx({"a": 0.5, "b": 0.5})
    skewed = softmax({"low": 0.0, "mid": 1.0, "high": 2.0})
    assert skewed["low"] < skewed["mid"] < skewed["high"]


def test_entropy_boundaries() -> None:
    assert entropy({"a": 1.0, "b": 0.0}) == 0.0
    assert entropy({"a": 1.0, "b": -0.5}) == 0.0  # 非正项按 0 处理，不触发 ln(0)
    assert entropy({"a": 0.5, "b": 0.5}) == pytest.approx(math.log(2))
    assert entropy({state: 1 / 6 for state in "abcdef"}) == pytest.approx(math.log(6))


def test_entropy_normalized_stays_in_unit_interval() -> None:
    assert entropy_normalized({"a": 1.0, "b": 0.0}) == pytest.approx(0.0)
    assert entropy_normalized({"a": 0.5, "b": 0.5}) == pytest.approx(1.0)
    assert entropy_normalized({state: 1 / 6 for state in "abcdef"}) == pytest.approx(1.0, abs=1e-12)
    skewed = {"a": 0.9, "b": 0.05, "c": 0.03, "d": 0.02}
    assert 0.0 <= entropy_normalized(skewed) <= 1.0


# ─────────────────────────────────────────────────────────────────────────────
# 非法参数一律 raise ValueError
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("func", "args"),
    [
        (saturate, (1.0, 0.0)),
        (saturate, (1.0, -2.0)),
        (log1p_saturate, (1.0, 0.0)),
        (log1p_saturate, (1.0, -2.0)),
        (neutral_band, (0.1, 0.0)),
        (neutral_band, (None, 0.0)),
        (not_yet, (0.1, -0.5)),
        (not_yet, (None, 0.0)),
        (severe, (-0.5, -0.1, 0.3)),
        (severe, (-0.5, 0.3, 0.3)),
        (severe, (-0.5, 0.5, 0.2)),
    ],
)
def test_invalid_parameters_raise_value_error(func: Callable[..., float], args: tuple[float | None, ...]) -> None:
    with pytest.raises(ValueError):
        func(*args)


def test_normalize_evidence_rejects_non_positive_caps() -> None:
    with pytest.raises(ValueError):
        normalize_evidence({"a": 1.0}, {"a": 0.0})
    with pytest.raises(ValueError):
        normalize_evidence({"a": 1.0}, {"a": -1.0})


def test_normalize_evidence_requires_cap_for_every_state() -> None:
    with pytest.raises(ValueError):
        normalize_evidence({"a": 1.0, "b": 1.0}, {"a": 1.0})


def test_softmax_requires_at_least_two_states() -> None:
    with pytest.raises(ValueError):
        softmax({"a": 1.0})
    with pytest.raises(ValueError):
        softmax({})


def test_entropy_normalized_requires_at_least_two_states() -> None:
    with pytest.raises(ValueError):
        entropy_normalized({"a": 1.0})
    with pytest.raises(ValueError):
        entropy_normalized({})


# ─────────────────────────────────────────────────────────────────────────────
# 结果契约类型
# ─────────────────────────────────────────────────────────────────────────────


def test_belief_and_transition_use_none_for_missing() -> None:
    belief = Belief(distribution={"a": 1.0}, argmax_state="a", entropy=0.0)
    assert belief.drift is None
    transition = Transition(from_state=None, to_state="a", legal=True, kind="initial")
    assert transition.trigger_factors == ()


def test_pattern_result_carries_auditable_intermediates() -> None:
    belief = Belief(distribution={"a": 0.7, "b": 0.3}, argmax_state="a", entropy=0.6)
    result = PatternResult(
        belief=belief,
        transition=Transition(from_state="a", to_state="a", legal=True, kind="stay", trigger_factors=("axis_a",)),
        raw_evidence={"a": 1.4, "b": 0.6},
        normalized_evidence={"a": 0.7, "b": 0.3},
        p_top=0.7,
        entropy_normalized=0.6,
        uncertain=False,
    )
    assert result.diagnostics is None
    assert result.transition is not None
    assert result.transition.trigger_factors == ("axis_a",)


def test_result_types_are_frozen() -> None:
    belief = Belief(distribution={"a": 1.0}, argmax_state="a", entropy=0.0)
    with pytest.raises(FrozenInstanceError):
        belief.entropy = 0.5
