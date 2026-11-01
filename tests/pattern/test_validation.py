"""模式规格校验与网格 / 可达性工具的单元测试：玩具状态 A/B/C，与业务无关。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest

from alphabee.pattern.ops import dn, neutral_band, up
from alphabee.pattern.spec import Params, PatternSpec, StateSpec, TransitionGraph
from alphabee.pattern.validation import (
    ValidationIssue,
    ValidationReport,
    find_witness,
    grid_points,
    reachable_states,
    validate_spec,
)

# ─────────────────────────────────────────────────────────────────────────────
# 玩具 spec：A/B/C 三个状态、a/b 两条轴，公式完全通用
#   A = up(a)                                    cap 1.0
#   B = dn(a)                                    cap 1.0
#   C = 0.6 · neutral_band(a, 1.0) · neutral_band(b, 1.0)  cap 0.6
# ─────────────────────────────────────────────────────────────────────────────


def _a_program(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
    return up(scores["a"])


def _b_program(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
    return dn(scores["a"])


def _c_program(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
    return 0.6 * neutral_band(scores["a"], 1.0) * neutral_band(scores["b"], 1.0)


def _zero_program(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
    return 0.0


def _state_a(cap: float = 1.0) -> StateSpec:
    return StateSpec(name="A", cap=cap, program=_a_program)


def _state_b(cap: float = 1.0) -> StateSpec:
    return StateSpec(name="B", cap=cap, program=_b_program)


def _state_c(cap: float = 0.6) -> StateSpec:
    return StateSpec(name="C", cap=cap, program=_c_program)


def _toy_spec() -> PatternSpec:
    return PatternSpec(
        name="toy",
        factors=("a", "b"),
        states=(_state_a(), _state_b(), _state_c()),
        transitions=TransitionGraph.chain(("A", "B", "C")).with_backward(),
        params=Params(temperature=0.5, uncertain_p_top=0.6),
        trigger_threshold=0.3,
    )


_TOY_AXES: dict[str, tuple[float, ...]] = {"a": (-1.0, 0.0, 1.0), "b": (0.0, 0.5)}


# ─────────────────────────────────────────────────────────────────────────────
# 结构校验
# ─────────────────────────────────────────────────────────────────────────────


def test_valid_toy_spec_has_no_issues() -> None:
    report = validate_spec(_toy_spec())
    assert report.ok is True
    assert report.issues == ()


def test_empty_states_are_reported() -> None:
    spec = PatternSpec(
        name="empty",
        factors=("a",),
        states=(),
        transitions=TransitionGraph(),
        params=Params(temperature=1.0, uncertain_p_top=0.5),
        trigger_threshold=0.0,
    )
    codes = [issue.code for issue in validate_spec(spec).issues]
    assert codes == ["states.empty"]


def test_duplicate_state_names_are_reported_without_raising() -> None:
    spec = replace(
        _toy_spec(),
        states=(_state_a(), _state_a(), _state_c()),
        transitions=TransitionGraph(),
    )
    report = validate_spec(spec)
    assert report.ok is False
    assert [issue.code for issue in report.issues] == ["state.duplicate_name"]
    assert "A" in report.issues[0].message


@pytest.mark.parametrize(
    ("cap", "expected_code"),
    [
        (0.0, "state.cap_not_positive"),
        (-1.0, "state.cap_not_positive"),
        (math.inf, "state.cap_not_finite"),
        (math.nan, "state.cap_not_finite"),
    ],
)
def test_cap_must_be_finite_and_positive(cap: float, expected_code: str) -> None:
    spec = replace(_toy_spec(), states=(_state_a(cap=cap),), transitions=TransitionGraph())
    report = validate_spec(spec)
    assert [issue.code for issue in report.issues] == [expected_code]


def test_non_callable_program_is_reported() -> None:
    spec = replace(
        _toy_spec(),
        states=(StateSpec(name="A", cap=1.0, program=42),),  # type: ignore[arg-type]
        transitions=TransitionGraph(),
    )
    assert [issue.code for issue in validate_spec(spec).issues] == ["state.program_not_callable"]


def test_empty_or_duplicate_factors_are_reported() -> None:
    empty = replace(_toy_spec(), factors=())
    assert [issue.code for issue in validate_spec(empty).issues] == ["factors.empty"]

    duplicate = replace(_toy_spec(), factors=("a", "a"))
    report = validate_spec(duplicate)
    assert [issue.code for issue in report.issues] == ["factors.duplicate"]
    assert "a" in report.issues[0].message


def test_unknown_transition_endpoints_are_reported() -> None:
    graph = TransitionGraph(edges={"A": {"B": "forward", "Z": "forward"}, "Y": {"A": "backward"}})
    spec = replace(_toy_spec(), transitions=graph)
    report = validate_spec(spec)
    assert [issue.code for issue in report.issues] == [
        "transition.unknown_endpoint",
        "transition.unknown_endpoint",
    ]
    messages = " | ".join(issue.message for issue in report.issues)
    assert "Z" in messages
    assert "Y" in messages


@pytest.mark.parametrize("temperature", [0.0, -1.0, math.nan, math.inf])
def test_temperature_must_be_finite_positive(temperature: float) -> None:
    spec = replace(_toy_spec(), params=Params(temperature=temperature, uncertain_p_top=0.6))
    assert [issue.code for issue in validate_spec(spec).issues] == ["params.temperature_invalid"]


@pytest.mark.parametrize("uncertain_p_top", [0.0, -0.1, 1.0001, math.nan])
def test_uncertain_p_top_must_be_in_open_closed_unit_interval(uncertain_p_top: float) -> None:
    spec = replace(_toy_spec(), params=Params(temperature=0.5, uncertain_p_top=uncertain_p_top))
    assert [issue.code for issue in validate_spec(spec).issues] == ["params.uncertain_p_top_out_of_range"]


@pytest.mark.parametrize("threshold", [-0.1, math.nan])
def test_trigger_threshold_must_be_non_negative(threshold: float) -> None:
    spec = replace(_toy_spec(), trigger_threshold=threshold)
    assert [issue.code for issue in validate_spec(spec).issues] == ["pattern.trigger_threshold_negative"]


def test_validation_collects_all_issues_instead_of_stopping_at_first() -> None:
    spec = replace(
        _toy_spec(),
        factors=("a", "a"),
        states=(_state_a(cap=0.0), _state_a(), _state_b(), _state_c()),
        transitions=TransitionGraph(edges={"A": {"B": "forward", "Z": "forward"}}),
        params=Params(temperature=0.0, uncertain_p_top=2.0),
        trigger_threshold=-0.1,
    )
    report = validate_spec(spec)
    codes = {issue.code for issue in report.issues}
    assert report.ok is False
    assert codes == {
        "state.duplicate_name",
        "state.cap_not_positive",
        "factors.duplicate",
        "transition.unknown_endpoint",
        "params.temperature_invalid",
        "params.uncertain_p_top_out_of_range",
        "pattern.trigger_threshold_negative",
    }


# ─────────────────────────────────────────────────────────────────────────────
# 校验结果对象
# ─────────────────────────────────────────────────────────────────────────────


def test_validation_report_ok_and_issue_normalization() -> None:
    empty = ValidationReport()
    assert empty.ok is True
    assert empty.issues == ()

    issue = ValidationIssue(code="x", message="y")
    report = ValidationReport([issue])  # type: ignore[arg-type]
    assert report.ok is False
    assert isinstance(report.issues, tuple)
    assert report.issues == (issue,)


# ─────────────────────────────────────────────────────────────────────────────
# 网格生成
# ─────────────────────────────────────────────────────────────────────────────


def test_grid_points_are_ordered_cartesian_product() -> None:
    points = list(grid_points({"a": (0.0, 1.0, 2.0), "b": (10.0, 20.0)}))
    assert points == [
        {"a": 0.0, "b": 10.0},
        {"a": 0.0, "b": 20.0},
        {"a": 1.0, "b": 10.0},
        {"a": 1.0, "b": 20.0},
        {"a": 2.0, "b": 10.0},
        {"a": 2.0, "b": 20.0},
    ]
    assert len(points) == 6


def test_grid_points_handle_none_empty_and_no_axes() -> None:
    assert list(grid_points({"a": (None, -1.0)})) == [{"a": None}, {"a": -1.0}]
    assert list(grid_points({})) == [{}]
    assert list(grid_points({"a": ()})) == []


def test_grid_points_are_deterministic() -> None:
    axes = {"a": (-1.0, 0.0, 1.0), "b": (None, 0.5)}
    assert list(grid_points(axes)) == list(grid_points(axes))


# ─────────────────────────────────────────────────────────────────────────────
# 可达性 witness
# ─────────────────────────────────────────────────────────────────────────────


def test_find_witness_returns_first_matching_frame() -> None:
    spec = _toy_spec()
    assert find_witness(spec, "A", axes=_TOY_AXES) == {"a": 1.0, "b": 0.0}
    assert find_witness(spec, "B", axes=_TOY_AXES) == {"a": -1.0, "b": 0.0}
    assert find_witness(spec, "C", axes=_TOY_AXES) == {"a": 0.0, "b": 0.0}


def test_find_witness_respects_min_p_top() -> None:
    spec = _toy_spec()
    # A 的 witness 帧 P_top ≈ 0.787
    assert find_witness(spec, "A", axes=_TOY_AXES, min_p_top=0.7) == {"a": 1.0, "b": 0.0}
    assert find_witness(spec, "A", axes=_TOY_AXES, min_p_top=0.8) is None


def test_find_witness_returns_none_for_unreachable_state() -> None:
    spec = replace(
        _toy_spec(),
        states=(_state_a(), _state_b(), _state_c(), StateSpec(name="D", cap=1.0, program=_zero_program)),
    )
    assert find_witness(spec, "D", axes=_TOY_AXES) is None


def test_find_witness_rejects_unknown_state() -> None:
    with pytest.raises(KeyError):
        find_witness(_toy_spec(), "missing", axes=_TOY_AXES)


def test_find_witness_passes_params_in_ctx() -> None:
    captured: list[Mapping[str, Any]] = []

    def probe(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
        captured.append(ctx)
        return up(scores["a"])

    spec = replace(
        _toy_spec(),
        states=(StateSpec(name="A", cap=1.0, program=probe), _state_b()),
        transitions=TransitionGraph(),
    )
    witness = find_witness(spec, "A", axes={"a": (1.0,)})
    assert witness == {"a": 1.0}
    assert captured
    assert captured[0]["params"] is spec.params


def test_reachable_states_maps_every_state() -> None:
    spec = _toy_spec()
    result = reachable_states(spec, axes=_TOY_AXES)
    assert list(result) == ["A", "B", "C"]
    assert result == {
        "A": {"a": 1.0, "b": 0.0},
        "B": {"a": -1.0, "b": 0.0},
        "C": {"a": 0.0, "b": 0.0},
    }

    with_unreachable = replace(
        spec,
        states=spec.states + (StateSpec(name="D", cap=1.0, program=_zero_program),),
    )
    result_with_d = reachable_states(with_unreachable, axes=_TOY_AXES)
    assert result_with_d["D"] is None


def test_witness_queries_are_deterministic() -> None:
    spec = _toy_spec()
    assert find_witness(spec, "C", axes=_TOY_AXES) == find_witness(spec, "C", axes=_TOY_AXES)
    assert reachable_states(spec, axes=_TOY_AXES) == reachable_states(spec, axes=_TOY_AXES)
