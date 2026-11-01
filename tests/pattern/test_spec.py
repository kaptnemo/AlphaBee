"""模式规格契约（spec.py）的单元测试：玩具状态 A/B/C，与业务无关。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from typing import Any, get_args

import pytest

from alphabee.pattern.spec import (
    AxisSpec,
    EvidenceProgram,
    MissingPolicy,
    Params,
    PatternSpec,
    StateSpec,
    TransitionGraph,
)


def _constant_program(value: float) -> EvidenceProgram:
    def program(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
        return value

    return program


def _state(name: str, value: float = 1.0, cap: float = 1.0) -> StateSpec:
    return StateSpec(name=name, cap=cap, program=_constant_program(value))


def _toy_spec() -> PatternSpec:
    return PatternSpec(
        name="toy",
        factors=("a", "b"),
        states=(_state("A"), _state("B"), _state("C")),
        transitions=TransitionGraph.chain(("A", "B", "C")).with_backward(),
        params=Params(temperature=0.5, uncertain_p_top=0.6),
        trigger_threshold=0.3,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 轴 / 参数
# ─────────────────────────────────────────────────────────────────────────────


def test_axis_spec_declares_missing_policy_metadata() -> None:
    axis = AxisSpec(name="a", missing_policy="half")
    assert axis.name == "a"
    assert axis.missing_policy == "half"
    with pytest.raises(FrozenInstanceError):
        axis.name = "b"


def test_missing_policy_alias_lists_generic_policies() -> None:
    assert get_args(MissingPolicy) == ("zero", "half", "none", "propagate")


def test_params_fields_are_explicit_and_frozen() -> None:
    params = Params(temperature=0.5, uncertain_p_top=0.6)
    assert params.temperature == 0.5
    assert params.uncertain_p_top == 0.6
    with pytest.raises(FrozenInstanceError):
        params.temperature = 1.0
    with pytest.raises(TypeError):
        Params()  # type: ignore[call-arg]


# ─────────────────────────────────────────────────────────────────────────────
# 迁移图
# ─────────────────────────────────────────────────────────────────────────────


def test_transition_graph_chain_builds_forward_chain() -> None:
    graph = TransitionGraph.chain(("A", "B", "C"))

    assert graph.edges == {"A": {"B": "forward"}, "B": {"C": "forward"}}
    assert graph.states == frozenset({"A", "B", "C"})
    assert graph.legal("A", "B") is True
    assert graph.legal("B", "A") is False
    assert graph.kind_of("A", "B") == "forward"
    assert graph.kind_of("B", "A") == "illegal"
    assert graph.kind_of("A", "A") == "same"
    assert graph.legal("A", "A") is True


def test_transition_graph_chain_accepts_custom_kind_and_skips_self_loops() -> None:
    downgrade = TransitionGraph.chain(("A", "B"), kind="downgrade")
    assert downgrade.kind_of("A", "B") == "downgrade"

    with_self_loop = TransitionGraph.chain(("A", "A", "B"))
    assert with_self_loop.edges == {"A": {"B": "forward"}}
    assert "A" not in with_self_loop.edges.get("A", {})

    assert TransitionGraph.chain(()).edges == {}
    assert TransitionGraph.chain(("A",)).edges == {}


def test_transition_graph_with_backward_returns_new_graph() -> None:
    forward = TransitionGraph.chain(("A", "B", "C"))
    bidirectional = forward.with_backward()

    # 原图不变
    assert forward.edges == {"A": {"B": "forward"}, "B": {"C": "forward"}}
    assert forward.legal("B", "A") is False
    # 新图包含全部反向边
    assert bidirectional.kind_of("A", "B") == "forward"
    assert bidirectional.kind_of("B", "A") == "backward"
    assert bidirectional.kind_of("C", "B") == "backward"
    assert bidirectional.legal("C", "B") is True
    # 幂等：再次 with_backward 不改变已有边
    assert bidirectional.with_backward() == bidirectional
    # 自定义 kind 生效
    assert forward.with_backward(kind="downgrade").kind_of("B", "A") == "downgrade"


def test_transition_graph_add_is_immutable_and_skips_self_loops() -> None:
    base = TransitionGraph.chain(("A", "B"))
    extended = base.add("B", "A", "reopen")

    assert base.legal("B", "A") is False
    assert extended.kind_of("B", "A") == "reopen"
    assert extended.kind_of("A", "B") == "forward"

    loop = base.add("A", "A", "same")
    assert loop.edges == base.edges
    assert loop.legal("A", "A") is True
    assert loop.kind_of("A", "A") == "same"


def test_transition_graph_copies_caller_edges() -> None:
    edges: dict[str, dict[str, str]] = {"A": {"B": "forward"}}
    graph = TransitionGraph(edges=edges)
    edges["A"]["B"] = "mutated"
    assert graph.kind_of("A", "B") == "forward"


# ─────────────────────────────────────────────────────────────────────────────
# 模式规格
# ─────────────────────────────────────────────────────────────────────────────


def test_pattern_spec_readonly_properties() -> None:
    spec = _toy_spec()
    assert spec.state_names == ("A", "B", "C")
    assert spec.caps == {"A": 1.0, "B": 1.0, "C": 1.0}
    assert spec.state("B").name == "B"
    with pytest.raises(KeyError):
        spec.state("missing")


def test_pattern_spec_state_names_keep_declaration_order() -> None:
    spec = PatternSpec(
        name="ordered",
        factors=("a",),
        states=(_state("C"), _state("A"), _state("B")),
        transitions=TransitionGraph(),
        params=Params(temperature=1.0, uncertain_p_top=0.5),
        trigger_threshold=0.0,
    )
    # 顺序是 argmax 平局确定性的来源，不能被排序或集合化
    assert spec.state_names == ("C", "A", "B")
    assert list(spec.caps) == ["C", "A", "B"]
