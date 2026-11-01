"""通用决策映射工具（decision.py）的单元测试：玩具权重/阈值，与业务无关。"""

from __future__ import annotations

import pytest

from alphabee.pattern.decision import (
    apply_ev_gate,
    band_from_weight,
    layered_scale,
    weighted_expectation,
)

_THRESHOLDS: tuple[tuple[float, str], ...] = (
    (0.15, "core"),
    (0.10, "add"),
    (0.05, "probe"),
)


# ─────────────────────────────────────────────────────────────────────────────
# weighted_expectation
# ─────────────────────────────────────────────────────────────────────────────


def test_weighted_expectation_is_sum_of_probability_times_weight() -> None:
    assert weighted_expectation({"A": 0.5, "B": 0.5}, {"A": 2.0, "B": -1.0}) == pytest.approx(0.5)
    assert weighted_expectation({"A": 1.0, "B": 0.0}, {"A": 3.0}) == pytest.approx(3.0)
    assert weighted_expectation({}, {}) == 0.0


def test_weighted_expectation_requires_weights_for_positive_probabilities() -> None:
    with pytest.raises(KeyError, match="B"):
        weighted_expectation({"A": 0.4, "B": 0.6}, {"A": 1.0})


def test_weighted_expectation_ignores_zero_probability_states_without_weights() -> None:
    # p=0 不贡献期望，不要求权重；missing 状态的权重缺失不应 raise
    assert weighted_expectation({"A": 1.0, "B": 0.0}, {"A": 2.0}) == pytest.approx(2.0)


# ─────────────────────────────────────────────────────────────────────────────
# band_from_weight
# ─────────────────────────────────────────────────────────────────────────────


def test_band_from_weight_hits_first_threshold_in_descending_order() -> None:
    assert band_from_weight(0.20, _THRESHOLDS, fallback="observe") == "core"
    assert band_from_weight(0.15, _THRESHOLDS, fallback="observe") == "core"  # 含等号
    assert band_from_weight(0.12, _THRESHOLDS, fallback="observe") == "add"
    assert band_from_weight(0.05, _THRESHOLDS, fallback="observe") == "probe"
    assert band_from_weight(0.01, _THRESHOLDS, fallback="observe") == "observe"


def test_band_from_weight_handles_none_zero_and_negative() -> None:
    assert band_from_weight(None, _THRESHOLDS, fallback="observe") == "observe"
    assert band_from_weight(None, _THRESHOLDS, fallback="observe", zero_label="exit") == "observe"
    assert band_from_weight(0.0, _THRESHOLDS, fallback="observe", zero_label="exit") == "exit"
    assert band_from_weight(-0.1, _THRESHOLDS, fallback="observe", zero_label="exit") == "exit"
    # 未提供 zero_label 时回退 fallback
    assert band_from_weight(0.0, _THRESHOLDS, fallback="observe") == "observe"


def test_band_from_weight_requires_strictly_descending_thresholds() -> None:
    with pytest.raises(ValueError, match="严格降序"):
        band_from_weight(0.2, ((0.10, "add"), (0.15, "core")), fallback="observe")
    with pytest.raises(ValueError, match="严格降序"):
        band_from_weight(0.2, ((0.10, "add"), (0.10, "core")), fallback="observe")
    # 校验先于 weight 分支：None 也要先暴露结构错误
    with pytest.raises(ValueError, match="严格降序"):
        band_from_weight(None, ((0.10, "add"), (0.15, "core")), fallback="observe")


def test_band_from_weight_with_empty_thresholds_returns_fallback() -> None:
    assert band_from_weight(0.2, (), fallback="observe") == "observe"


# ─────────────────────────────────────────────────────────────────────────────
# layered_scale
# ─────────────────────────────────────────────────────────────────────────────


def test_layered_scale_multiplies_without_clipping() -> None:
    assert layered_scale() == 1.0
    assert layered_scale(2.0, 3.0, 4.0) == pytest.approx(24.0)
    assert layered_scale(10.0, 10.0) == pytest.approx(100.0)  # 不裁剪
    assert layered_scale(0.5, 0.0) == 0.0
    assert layered_scale(2.0, -3.0) == pytest.approx(-6.0)


# ─────────────────────────────────────────────────────────────────────────────
# apply_ev_gate
# ─────────────────────────────────────────────────────────────────────────────


def _gate(weight: float, *, ev: float | None, entropy: float = 0.2, soft_multiplier: float = 0.3) -> tuple[float, str]:
    return apply_ev_gate(
        weight,
        risk_adjusted_ev=ev,
        threshold=0.10,
        soft_multiplier=soft_multiplier,
        hard_zero_below=-0.05,
        entropy=entropy,
        uncertain_entropy=0.5,
    )


def test_apply_ev_gate_no_gate_when_ev_missing() -> None:
    assert _gate(0.2, ev=None) == (0.2, "no_gate")
    assert _gate(0.2, ev=None, entropy=0.9) == (0.2, "no_gate")


def test_apply_ev_gate_passes_when_ev_meets_threshold() -> None:
    assert _gate(0.2, ev=0.10) == (0.2, "pass")  # 含等号
    assert _gate(0.2, ev=0.5) == (0.2, "pass")


def test_apply_ev_gate_hard_zero_requires_low_entropy() -> None:
    assert _gate(0.2, ev=-0.2, entropy=0.2) == (0.0, "hard_zero")
    # 高熵时不清仓，只软折减
    assert _gate(0.2, ev=-0.2, entropy=0.9) == (pytest.approx(0.06), "soft_reduce")


def test_apply_ev_gate_soft_reduces_between_thresholds() -> None:
    assert _gate(0.2, ev=0.0) == (pytest.approx(0.06), "soft_reduce")
    assert _gate(0.2, ev=0.0, soft_multiplier=1.0) == (pytest.approx(0.2), "soft_reduce")
    assert _gate(0.2, ev=0.0, soft_multiplier=0.0) == (0.0, "soft_reduce")


@pytest.mark.parametrize("soft_multiplier", [-0.1, 1.1, float("nan")])
def test_apply_ev_gate_rejects_invalid_soft_multiplier(soft_multiplier: float) -> None:
    with pytest.raises(ValueError, match="soft_multiplier"):
        _gate(0.2, ev=None, soft_multiplier=soft_multiplier)
