"""证据层组装与通用引擎的单元测试：玩具状态 A/B/C、轴 x/y，与业务无关。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest

from alphabee.pattern.engine import Engine
from alphabee.pattern.evidence import build_context, evaluate_evidence
from alphabee.pattern.ops import dn, neutral_band, up
from alphabee.pattern.spec import Params, PatternSpec, StateSpec, TransitionGraph
from alphabee.pattern.types import Belief, PatternResult

# ─────────────────────────────────────────────────────────────────────────────
# 玩具 spec：A/B/C 三个状态、x/y 两条轴
#   A = up(x)                                             cap 1.0
#   B = dn(x)                                             cap 1.0
#   C = 0.6 · neutral_band(x, 1.0) · neutral_band(y, 1.0) cap 0.6
# 迁移图：A → B → C（仅正向）
# ─────────────────────────────────────────────────────────────────────────────


def _program_a(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
    return up(scores.get("x"))


def _program_b(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
    return dn(scores.get("x"))


def _program_c(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
    return 0.6 * neutral_band(scores.get("x"), 1.0) * neutral_band(scores.get("y"), 1.0)


def _state_a(cap: float = 1.0) -> StateSpec:
    return StateSpec(name="A", cap=cap, program=_program_a)


def _state_b(cap: float = 1.0) -> StateSpec:
    return StateSpec(name="B", cap=cap, program=_program_b)


def _state_c(cap: float = 0.6) -> StateSpec:
    return StateSpec(name="C", cap=cap, program=_program_c)


def _toy_spec(
    *,
    temperature: float = 0.5,
    uncertain_p_top: float = 0.7,
    trigger_threshold: float = 0.3,
) -> PatternSpec:
    return PatternSpec(
        name="toy",
        factors=("x", "y"),
        states=(_state_a(), _state_b(), _state_c()),
        transitions=TransitionGraph.chain(("A", "B", "C")),
        params=Params(temperature=temperature, uncertain_p_top=uncertain_p_top),
        trigger_threshold=trigger_threshold,
    )


_FRAME_A: dict[str, float | None] = {"x": 1.0, "y": 0.0}
_FRAME_B: dict[str, float | None] = {"x": -1.0, "y": 0.0}
_FRAME_C: dict[str, float | None] = {"x": 0.0, "y": 0.0}
_FRAME_UNIFORM: dict[str, float | None] = {"x": 0.0, "y": 1.0}  # A=B=C=0 → 均匀分布


def _previous_with_shifted_state(current: PatternResult, state: str, shift: float) -> PatternResult:
    """以 ``current`` 的分布为副本构造 previous：仅 ``state`` 减去 ``shift``，其余逐值不变。

    只用于 drift 阈值判别：分离「证据/迁移」与「上一帧分布」两个输入，避免用真实帧差异
    间接验证阈值（那样无法精确控制 |Δ|）。
    """
    distribution = dict(current.belief.distribution)
    distribution[state] -= shift
    return PatternResult(
        belief=Belief(
            distribution=distribution,
            argmax_state=current.belief.argmax_state,
            entropy=current.belief.entropy,
            drift=None,
        ),
        transition=None,
        raw_evidence=current.raw_evidence,
        normalized_evidence=current.normalized_evidence,
        p_top=current.p_top,
        entropy_normalized=current.entropy_normalized,
        uncertain=current.uncertain,
        diagnostics=None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 证据层组装
# ─────────────────────────────────────────────────────────────────────────────


def test_build_context_contains_params_and_merges_extra() -> None:
    spec = _toy_spec()
    assert build_context(spec) == {"params": spec.params}

    merged = build_context(spec, {"marker": 1, "window": 0.25})
    assert merged == {"params": spec.params, "marker": 1, "window": 0.25}
    assert merged["params"] is spec.params


def test_build_context_rejects_params_override() -> None:
    spec = _toy_spec()
    with pytest.raises(ValueError, match="params"):
        build_context(spec, {"params": Params(temperature=1.0, uncertain_p_top=0.5)})


def test_evaluate_evidence_preserves_state_order_and_values() -> None:
    spec = _toy_spec()
    evidence = evaluate_evidence(spec, _FRAME_A, build_context(spec))
    assert list(evidence) == ["A", "B", "C"]
    assert evidence == {"A": 1.0, "B": 0.0, "C": 0.0}


def test_evaluate_evidence_propagates_program_errors() -> None:
    def boom(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
        raise RuntimeError("boom")

    spec = replace(_toy_spec(), states=(StateSpec(name="A", cap=1.0, program=boom),))
    with pytest.raises(RuntimeError, match="boom"):
        evaluate_evidence(spec, _FRAME_A, build_context(spec))


# ─────────────────────────────────────────────────────────────────────────────
# Engine 构造校验
# ─────────────────────────────────────────────────────────────────────────────


def test_engine_rejects_invalid_spec_with_issue_preview() -> None:
    with pytest.raises(ValueError, match="temperature"):
        Engine(replace(_toy_spec(), params=Params(temperature=0.0, uncertain_p_top=0.7)))

    bad_cap = replace(_toy_spec(), states=(_state_a(cap=0.0), _state_b(), _state_c()))
    with pytest.raises(ValueError, match="cap"):
        Engine(bad_cap)

    multiple = replace(_toy_spec(), factors=("x", "x"), params=Params(temperature=0.0, uncertain_p_top=2.0))
    with pytest.raises(ValueError) as excinfo:
        Engine(multiple)
    message = str(excinfo.value)
    assert "factors.duplicate" in message
    assert "params.temperature_invalid" in message
    assert "params.uncertain_p_top_out_of_range" in message


def test_engine_rejects_single_state_spec() -> None:
    single = replace(_toy_spec(), states=(_state_a(),), transitions=TransitionGraph())
    # 校验层先报 states.too_few；Engine 仍 raise ValueError（行为不变，口径前移）
    with pytest.raises(ValueError, match="states.too_few"):
        Engine(single)


# ─────────────────────────────────────────────────────────────────────────────
# 分布 / 软状态
# ─────────────────────────────────────────────────────────────────────────────


def test_distribution_is_normalized_and_matches_pipeline() -> None:
    engine = Engine(_toy_spec())
    result = engine.classify(_FRAME_A)
    distribution = result.belief.distribution

    assert list(distribution) == ["A", "B", "C"]
    assert sum(distribution.values()) == pytest.approx(1.0, abs=1e-12)
    expected_peak = math.exp(2.0) / (math.exp(2.0) + 2.0)  # logits: A=2, B=C=0
    assert distribution["A"] == pytest.approx(expected_peak)
    assert distribution["B"] == pytest.approx(distribution["C"])

    assert result.belief.argmax_state == "A"
    assert result.p_top == pytest.approx(max(distribution.values()))
    expected_entropy = -sum(p * math.log(p) for p in distribution.values())
    assert result.belief.entropy == pytest.approx(expected_entropy)
    assert result.entropy_normalized == pytest.approx(expected_entropy / math.log(3))
    assert 0.0 <= result.entropy_normalized <= 1.0
    assert result.uncertain is False

    # classify 的中间量与各独立方法逐值一致
    assert result.raw_evidence == engine.evidence(_FRAME_A)
    assert result.normalized_evidence == engine.normalize(result.raw_evidence)
    assert distribution == engine.distribution(result.normalized_evidence)


def test_temperature_changes_sharpness_not_argmax() -> None:
    sharp = Engine(_toy_spec(temperature=0.5)).classify(_FRAME_A)
    flat = Engine(_toy_spec(temperature=2.0)).classify(_FRAME_A)

    assert sharp.p_top > flat.p_top
    assert sharp.belief.argmax_state == flat.belief.argmax_state == "A"
    assert sharp.belief.entropy < flat.belief.entropy


def test_uncertain_uses_strict_margin_below_threshold() -> None:
    uniform = Engine(_toy_spec(uncertain_p_top=0.7)).classify(_FRAME_UNIFORM)
    assert uniform.belief.argmax_state == "A"  # 平局按 state_names 顺序取第一个
    assert uniform.p_top == pytest.approx(1.0 / 3.0)
    assert uniform.belief.entropy == pytest.approx(math.log(3))
    assert uniform.entropy_normalized == pytest.approx(1.0)
    assert uniform.uncertain is True

    # 严格小于：P_top 恰好等于阈值时不算 uncertain
    boundary = Engine(_toy_spec(uncertain_p_top=1.0 / 3.0)).classify(_FRAME_UNIFORM)
    assert boundary.uncertain is False

    confident = Engine(_toy_spec(uncertain_p_top=0.3)).classify(_FRAME_UNIFORM)
    assert confident.uncertain is False

    sharp = Engine(_toy_spec(uncertain_p_top=0.7)).classify(_FRAME_A)
    assert sharp.uncertain is False


def test_raw_and_normalized_evidence_are_exposed() -> None:
    engine = Engine(_toy_spec())
    result = engine.classify(_FRAME_C)

    assert list(result.raw_evidence) == ["A", "B", "C"]
    assert result.raw_evidence["A"] == 0.0
    assert result.raw_evidence["C"] == pytest.approx(0.6)
    assert result.normalized_evidence["A"] == 0.0
    assert result.normalized_evidence["C"] == pytest.approx(1.0)

    assert result.raw_evidence == engine.evidence(_FRAME_C)
    assert result.normalized_evidence == engine.normalize(result.raw_evidence)
    assert result.belief.distribution == engine.distribution(result.normalized_evidence)


def test_normalize_clips_evidence_above_cap() -> None:
    spec = replace(_toy_spec(), states=(_state_a(), _state_b(), _state_c(cap=0.3)))
    result = Engine(spec).classify(_FRAME_C)

    assert result.raw_evidence["C"] == pytest.approx(0.6)
    assert result.normalized_evidence["C"] == 1.0


def test_cap_normalization_can_flip_argmax() -> None:
    """同一帧、同一证据，只有 A 的 cap 不同 → argmax 翻转（cap 是结构上限，不是装饰）。

    判别力自证：若把 ``wide_cap_spec`` 的 A cap 改回 1.0（与 ``base`` 相同），归一化证据回到
    ``A=0.6 > C=0.4``，下方 ``flipped == "C"`` 会立即失败。``same_cap`` 控制组把这一点显式钉住。
    """
    frame: dict[str, float | None] = {"x": 0.6, "y": 0.0}
    base = Engine(_toy_spec()).classify(frame)
    assert base.raw_evidence["A"] == pytest.approx(0.6)
    assert base.raw_evidence["C"] == pytest.approx(0.24)
    assert base.belief.argmax_state == "A"  # norm: A=0.6, C=0.4

    wide_cap_spec = replace(_toy_spec(), states=(_state_a(cap=2.0), _state_b(), _state_c()))
    flipped = Engine(wide_cap_spec).classify(frame)
    assert flipped.raw_evidence == base.raw_evidence  # 原始证据不随 cap 变化
    assert flipped.normalized_evidence["A"] == pytest.approx(0.3)  # 0.6 / 2.0
    assert flipped.normalized_evidence["C"] == pytest.approx(0.4)  # 0.24 / 0.6
    assert flipped.belief.argmax_state == "C"

    # 把 A cap 改回与 base 相同（1.0）后 argmax 回到 A：上方翻转断言对 cap 敏感
    same_cap = Engine(replace(wide_cap_spec, states=(_state_a(cap=1.0), _state_b(), _state_c())))
    assert same_cap.classify(frame).belief.argmax_state == "A"


# ─────────────────────────────────────────────────────────────────────────────
# 迁移 / drift / trigger_factors
# ─────────────────────────────────────────────────────────────────────────────


def test_no_previous_means_no_transition_and_no_drift() -> None:
    result = Engine(_toy_spec()).classify(_FRAME_A)
    assert result.transition is None
    assert result.belief.drift is None


def test_transition_same_state() -> None:
    engine = Engine(_toy_spec())
    first = engine.classify(_FRAME_A)
    second = engine.classify(_FRAME_A, previous=first)

    transition = second.transition
    assert transition is not None
    assert (transition.from_state, transition.to_state) == ("A", "A")
    assert transition.legal is True
    assert transition.kind == "same"
    assert transition.trigger_factors == ("x",)
    assert second.belief.drift is None


def test_transition_legal_forward_with_drift() -> None:
    engine = Engine(_toy_spec())
    first = engine.classify(_FRAME_A)
    second = engine.classify(_FRAME_B, previous=first)

    transition = second.transition
    assert transition is not None
    assert (transition.from_state, transition.to_state) == ("A", "B")
    assert transition.legal is True
    assert transition.kind == "forward"

    drift = second.belief.drift
    assert drift is not None
    assert drift["A"] < 0.0 < drift["B"]
    assert "C" not in drift  # 对称帧下 C 的质量不变，不进入 drift
    assert sum(drift.values()) == pytest.approx(0.0, abs=1e-12)


def test_drift_threshold_is_strict_and_significant_only() -> None:
    engine = Engine(_toy_spec())
    current = engine.classify(_FRAME_A)

    # 1) 仅 A 的上一帧质量低 0.0015（> 1e-3）：只有 A 进入 drift，差值 = new - old。
    above = engine.classify(
        _FRAME_A,
        previous=_previous_with_shifted_state(current, "A", 0.0015),
    )
    assert above.belief.drift is not None
    assert set(above.belief.drift) == {"A"}
    assert above.belief.drift["A"] == pytest.approx(0.0015)

    # 2) 仅 A 的上一帧质量低 0.0005（< 1e-3）：全低于阈值 → drift 为 None。
    below = engine.classify(
        _FRAME_A,
        previous=_previous_with_shifted_state(current, "A", 0.0005),
    )
    assert below.belief.drift is None

    # 3) 严格 '>'：|Δ| 恰等于 1e-3 不纳入。取小概率状态（C≈0.0016，与阈值同 binade）
    #    使得 c - (c - 1e-3) 精确等于 1e-3，从而真正钉住边界而不是「略大于 1e-3」。
    boundary_engine = Engine(_toy_spec(temperature=0.14))
    boundary_frame: dict[str, float | None] = {"x": -0.9, "y": 0.999}
    boundary_current = boundary_engine.classify(boundary_frame)
    boundary_previous = _previous_with_shifted_state(boundary_current, "C", 1e-3)
    exact_delta = boundary_current.belief.distribution["C"] - boundary_previous.belief.distribution["C"]
    assert exact_delta == 1e-3

    at_threshold = boundary_engine.classify(boundary_frame, previous=boundary_previous)
    assert at_threshold.belief.drift is None


def test_transition_illegal_jump_marks_legal_false() -> None:
    engine = Engine(_toy_spec())
    first = engine.classify(_FRAME_A)
    jumped = engine.classify(_FRAME_C, previous=first)

    transition = jumped.transition
    assert transition is not None
    assert (transition.from_state, transition.to_state) == ("A", "C")
    assert transition.legal is False
    assert transition.kind == "illegal"


def test_trigger_factors_follow_factor_order_and_threshold() -> None:
    engine = Engine(_toy_spec(trigger_threshold=0.3))
    previous = engine.classify(_FRAME_UNIFORM)
    both = engine.classify({"x": 1.0, "y": -0.8}, previous=previous)
    assert both.transition is not None
    assert both.transition.trigger_factors == ("x", "y")

    # 顺序由 spec.factors 决定，不是固定字母序
    reordered_spec = replace(_toy_spec(), factors=("y", "x"))
    reordered = Engine(reordered_spec)
    both_reordered = reordered.classify({"x": 1.0, "y": -0.8}, previous=reordered.classify(_FRAME_UNIFORM))
    assert both_reordered.transition is not None
    assert both_reordered.transition.trigger_factors == ("y", "x")

    # 阈值生效（>= 边界纳入，低于阈值排除）
    threshold_engine = Engine(_toy_spec(trigger_threshold=0.5))
    at_threshold = threshold_engine.classify(
        {"x": -0.5, "y": 0.1},
        previous=threshold_engine.classify(_FRAME_UNIFORM),
    )
    assert at_threshold.transition is not None
    assert at_threshold.transition.trigger_factors == ("x",)

    # scores[f] 为 None 时跳过
    none_engine = Engine(_toy_spec())
    skipped = none_engine.classify(
        {"x": None, "y": 0.8},
        previous=none_engine.classify(_FRAME_UNIFORM),
    )
    assert skipped.transition is not None
    assert skipped.transition.trigger_factors == ("y",)


# ─────────────────────────────────────────────────────────────────────────────
# 确定性 / 诊断 / ctx 注入
# ─────────────────────────────────────────────────────────────────────────────


def test_classify_is_deterministic() -> None:
    engine = Engine(_toy_spec())
    assert engine.classify({"x": 0.4, "y": -0.7}) == engine.classify({"x": 0.4, "y": -0.7})

    previous = engine.classify(_FRAME_UNIFORM)
    first = engine.classify(_FRAME_A, previous=previous)
    second = engine.classify(_FRAME_A, previous=previous)
    assert first == second
    assert first.transition == second.transition
    assert first.belief.drift == second.belief.drift


def test_diagnostics_contain_logits_temperature_and_state_names() -> None:
    engine = Engine(_toy_spec())
    result = engine.classify(_FRAME_A)

    assert result.diagnostics is not None
    assert result.diagnostics["temperature"] == 0.5
    assert result.diagnostics["state_names"] == ["A", "B", "C"]
    logits = result.diagnostics["logits"]
    assert isinstance(logits, dict)
    assert list(logits) == ["A", "B", "C"]
    assert logits["A"] == pytest.approx(2.0)  # 归一化证据 1.0 / τ=0.5
    assert logits["B"] == pytest.approx(0.0)


def test_engine_evidence_uses_params_context_by_default() -> None:
    seen: list[Mapping[str, Any]] = []

    def probe(scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float:
        seen.append(ctx)
        return up(scores.get("x"))

    spec = replace(_toy_spec(), states=(StateSpec(name="A", cap=1.0, program=probe), _state_b(), _state_c()))
    engine = Engine(spec)

    assert engine.evidence(_FRAME_A)["A"] == 1.0
    assert seen
    assert seen[0]["params"] is spec.params

    custom = {"params": spec.params, "marker": "custom"}
    engine.evidence(_FRAME_A, custom)
    assert seen[-1]["marker"] == "custom"
