"""包门面与公共 API 的测试：只允许依赖标准库与 alphabee.pattern。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

import alphabee.pattern as pattern
from alphabee.pattern import (
    Engine,
    Params,
    PatternSpec,
    StateSpec,
    TransitionGraph,
    dn,
    up,
)

_EXPECTED_PUBLIC_NAMES = {
    "AxisSpec",
    "Belief",
    "Engine",
    "EvidenceProgram",
    "MissingPolicy",
    "Params",
    "PatternResult",
    "PatternSpec",
    "Scores",
    "StateSpec",
    "Transition",
    "TransitionGraph",
    "ValidationIssue",
    "ValidationReport",
    "apply_ev_gate",
    "assert_bounds",
    "assert_monotone",
    "assert_no_uniform",
    "assert_reachable",
    "band_from_weight",
    "build_context",
    "clip01",
    "dn",
    "entropy",
    "entropy_normalized",
    "evaluate_evidence",
    "find_witness",
    "grid_points",
    "iter_grid_results",
    "layered_scale",
    "log1p_saturate",
    "mean_missing",
    "neutral_band",
    "normalize_evidence",
    "not_yet",
    "reachable_states",
    "saturate",
    "severe",
    "softmax",
    "up",
    "validate_spec",
    "weighted_expectation",
}


def _up_program(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
    return up(scores.get("a"))


def _down_program(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
    return dn(scores.get("a"))


def _minimal_spec() -> PatternSpec:
    return PatternSpec(
        name="minimal",
        factors=("a",),
        states=(
            StateSpec(name="Up", cap=1.0, program=_up_program),
            StateSpec(name="Down", cap=1.0, program=_down_program),
        ),
        transitions=TransitionGraph.chain(("Up", "Down")),
        params=Params(temperature=1.0, uncertain_p_top=0.6),
        trigger_threshold=0.3,
    )


def test_package_exposes_a_sorted_unique_public_api() -> None:
    assert pattern.__doc__
    assert pattern.__doc__.strip()
    assert pattern.__all__
    assert pattern.__all__ == sorted(pattern.__all__)
    assert len(set(pattern.__all__)) == len(pattern.__all__)
    assert all(not name.startswith("_") for name in pattern.__all__)


def test_every_declared_public_name_resolves() -> None:
    missing = [name for name in pattern.__all__ if not hasattr(pattern, name)]
    assert missing == []


def test_expected_public_names_are_exported() -> None:
    assert _EXPECTED_PUBLIC_NAMES <= set(pattern.__all__)


def test_engine_and_spec_are_usable_through_the_facade() -> None:
    engine = Engine(_minimal_spec())
    result = engine.classify({"a": 1.0})
    assert result.belief.argmax_state == "Up"
    assert sum(result.belief.distribution.values()) == pytest.approx(1.0, abs=1e-12)
    expected_top = math.exp(1.0) / (math.exp(1.0) + 1.0)
    assert result.p_top == pytest.approx(expected_top)


def test_decision_and_testing_tools_are_exported() -> None:
    assert pattern.weighted_expectation({"A": 1.0}, {"A": 2.0}) == pytest.approx(2.0)
    assert pattern.layered_scale(2.0, 3.0) == pytest.approx(6.0)
    assert pattern.softmax({"A": 0.0, "B": 0.0}) == pytest.approx({"A": 0.5, "B": 0.5})
    assert callable(pattern.assert_bounds)
    assert callable(pattern.iter_grid_results)


def test_py_typed_marker_exists_and_is_empty() -> None:
    package_dir = Path(pattern.__file__).parent
    marker = package_dir / "py.typed"
    assert marker.is_file()
    assert marker.read_bytes() == b""
