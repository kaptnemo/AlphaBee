"""通用不变量测试工具（testing.py）的单元测试：玩具 spec，与业务无关。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest

from alphabee.pattern.ops import dn, neutral_band, up
from alphabee.pattern.spec import Params, PatternSpec, StateSpec, TransitionGraph
from alphabee.pattern.testing import (
    assert_bounds,
    assert_monotone,
    assert_no_uniform,
    assert_reachable,
    iter_grid_results,
)
from alphabee.pattern.types import PatternResult
from alphabee.pattern.validation import grid_points

# ─────────────────────────────────────────────────────────────────────────────
# 玩具 spec（与业务无关）：
#   safe：A/B/C 三状态在本网格上均有正底噪、互不相同、全部可达
#   mono：Long 随 a 上升、Short 随 a 下降、Noise 恒定，用于单调性
# ─────────────────────────────────────────────────────────────────────────────


def _program_a(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
    return 0.6 + 0.4 * up(scores.get("x"))


def _program_b(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
    return 0.6 + 0.4 * dn(scores.get("x"))


def _program_c(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
    return 0.6 + 0.4 * neutral_band(scores.get("x"), 1.0)


def _safe_spec() -> PatternSpec:
    return PatternSpec(
        name="safe",
        factors=("x", "y"),
        states=(
            StateSpec(name="A", cap=1.0, program=_program_a),
            StateSpec(name="B", cap=1.0, program=_program_b),
            StateSpec(name="C", cap=1.0, program=_program_c),
        ),
        transitions=TransitionGraph.chain(("A", "B", "C")),
        params=Params(temperature=1.0, uncertain_p_top=0.5),
        trigger_threshold=0.3,
    )


def _long_program(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
    return up(scores.get("a"))


def _short_program(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
    return dn(scores.get("a"))


def _noise_program(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
    return 0.5


def _mono_spec() -> PatternSpec:
    return PatternSpec(
        name="mono",
        factors=("a", "b"),
        states=(
            StateSpec(name="Long", cap=1.0, program=_long_program),
            StateSpec(name="Short", cap=1.0, program=_short_program),
            StateSpec(name="Noise", cap=1.0, program=_noise_program),
        ),
        transitions=TransitionGraph.chain(("Long", "Short", "Noise")),
        params=Params(temperature=1.0, uncertain_p_top=0.5),
        trigger_threshold=0.3,
    )


def _constant_program(value: float):
    def program(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
        return value

    return program


_SAFE_AXES: dict[str, tuple[float, ...]] = {"x": (-1.0, 0.0, 1.0), "y": (0.0, 1.0)}
_MONO_AXES: dict[str, tuple[float, ...]] = {"a": (-1.0, 0.0, 1.0), "b": (0.0,)}


# ─────────────────────────────────────────────────────────────────────────────
# iter_grid_results
# ─────────────────────────────────────────────────────────────────────────────


def test_iter_grid_results_is_ordered_and_deterministic() -> None:
    first = list(iter_grid_results(_safe_spec(), _SAFE_AXES))
    second = list(iter_grid_results(_safe_spec(), _SAFE_AXES))

    assert [frame for frame, _ in first] == list(grid_points(_SAFE_AXES))
    assert [frame for frame, _ in first] == [frame for frame, _ in second]
    assert len(first) == 3 * 2
    assert all(isinstance(result, PatternResult) for _, result in first)


# ─────────────────────────────────────────────────────────────────────────────
# assert_bounds
# ─────────────────────────────────────────────────────────────────────────────


def test_assert_bounds_passes_on_well_formed_spec() -> None:
    assert_bounds(_safe_spec(), _SAFE_AXES)


def test_assert_bounds_raises_on_negative_raw_evidence_with_location() -> None:
    def negative_program(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
        return -0.5

    bad = replace(
        _safe_spec(),
        states=(
            StateSpec(name="A", cap=1.0, program=negative_program),
            StateSpec(name="B", cap=1.0, program=_program_b),
            StateSpec(name="C", cap=1.0, program=_program_c),
        ),
    )
    with pytest.raises(AssertionError, match="raw evidence") as excinfo:
        assert_bounds(bad, _SAFE_AXES)
    message = str(excinfo.value)
    assert "A" in message  # 状态可定位
    assert "frame=" in message  # 帧可定位


# ─────────────────────────────────────────────────────────────────────────────
# assert_reachable
# ─────────────────────────────────────────────────────────────────────────────


def test_assert_reachable_passes_when_every_state_can_win() -> None:
    assert_reachable(_safe_spec(), _SAFE_AXES)


def test_assert_reachable_lists_unreachable_states() -> None:
    def zero_program(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
        return 0.0

    spec = _safe_spec()
    unreachable = replace(
        spec,
        states=spec.states + (StateSpec(name="D", cap=1.0, program=zero_program),),
    )
    with pytest.raises(AssertionError, match="不可达") as excinfo:
        assert_reachable(unreachable, _SAFE_AXES)
    assert "D" in str(excinfo.value)


# ─────────────────────────────────────────────────────────────────────────────
# assert_no_uniform
# ─────────────────────────────────────────────────────────────────────────────


def test_assert_no_uniform_passes_when_distributions_differ() -> None:
    assert_no_uniform(_safe_spec(), _SAFE_AXES)


def test_assert_no_uniform_raises_on_constant_states() -> None:
    flat = PatternSpec(
        name="flat",
        factors=("x",),
        states=(
            StateSpec(name="A", cap=1.0, program=_constant_program(0.5)),
            StateSpec(name="B", cap=1.0, program=_constant_program(0.5)),
            StateSpec(name="C", cap=1.0, program=_constant_program(0.5)),
        ),
        transitions=TransitionGraph(),
        params=Params(temperature=1.0, uncertain_p_top=0.5),
        trigger_threshold=0.0,
    )
    with pytest.raises(AssertionError, match="假均分") as excinfo:
        assert_no_uniform(flat, {"x": (0.0, 1.0)})
    assert "frame=" in str(excinfo.value)


# ─────────────────────────────────────────────────────────────────────────────
# assert_monotone
# ─────────────────────────────────────────────────────────────────────────────


def test_assert_monotone_passes_for_declared_sides() -> None:
    # a 越低越差：Long 证据不增、Short 证据不降
    assert_monotone(
        _mono_spec(),
        axis="a",
        worsening="lower",
        long_states=("Long",),
        short_states=("Short",),
        axes=_MONO_AXES,
    )
    # a 越高越差：侧别互换后同样成立
    assert_monotone(
        _mono_spec(),
        axis="a",
        worsening="higher",
        long_states=("Short",),
        short_states=("Long",),
        axes=_MONO_AXES,
    )


def test_assert_monotone_raises_with_direction_and_state() -> None:
    # 把 Short 声明为多头：a 越低 Short 证据越高 → 违反单调性
    with pytest.raises(AssertionError, match="单调性失败") as excinfo:
        assert_monotone(
            _mono_spec(),
            axis="a",
            worsening="lower",
            long_states=("Short",),
            short_states=(),
            axes=_MONO_AXES,
        )
    message = str(excinfo.value)
    assert "worsening='lower'" in message
    assert "Short" in message


def test_assert_monotone_rejects_invalid_direction() -> None:
    with pytest.raises(ValueError, match="worsening"):
        assert_monotone(
            _mono_spec(),
            axis="a",
            worsening="sideways",  # type: ignore[arg-type]
            long_states=("Long",),
            short_states=("Short",),
            axes=_MONO_AXES,
        )
