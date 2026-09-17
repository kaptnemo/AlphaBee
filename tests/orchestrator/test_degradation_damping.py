"""F2 单测：降级传导阻尼 ``damp_confidence`` + ``degraded_inputs``（§7.2 规则 2、§16）。

覆盖目标（t9 acceptance）：

* 离散 confidence **下调一档**（``high→medium``、``medium→low``、``low→low``）；
* 连续 confidence **×0.85**（沿用 insight → thesis 先例），并夹在 ``[0, 1]``；
* ★ **一档封顶**：多次/多个降级输入**不叠加**（防 §16「保守化螺旋」）；
* 无降级输入 → **原样返回**（零改动）；
* ``None`` / 未知取值 / ``bool`` 的边界；
* ``degraded_inputs`` 只读、去重、兼容 ``fallback_tier`` 形态；
* 与 ``agents/thesis/engine.py`` 的接入一致性（insight 降温走同一阻尼语义）。
"""

from __future__ import annotations

from alphabee.core.schemas import Artifact, ArtifactType
from alphabee.orchestrator.services.degradation import (
    CONFIDENCE_DOWNGRADE,
    DAMPING_FACTOR,
    damp_confidence,
    degraded_inputs,
)


def _artifact(
    artifact_id: str,
    *,
    degraded: bool = False,
    fallback_tier: int = 0,
    artifact_type: ArtifactType = ArtifactType.INSIGHT_ANALYSIS,
) -> Artifact:
    return Artifact(
        id=artifact_id,
        type=artifact_type,
        producer_step="synthesize_insights",
        value={"degraded": degraded, "fallback_tier": fallback_tier, "core_view": "x"},
    )


# ── degraded_inputs ─────────────────────────────────────────────────────────


def test_degraded_inputs_empty_when_none_degraded():
    assert degraded_inputs([_artifact("a1"), _artifact("a2")]) == []
    assert degraded_inputs([]) == []
    assert degraded_inputs(None) == []


def test_degraded_inputs_detects_flag_and_dedupes():
    arts = [_artifact("a1", degraded=True), _artifact("a2"), _artifact("a3", degraded=True)]
    assert degraded_inputs(arts) == ["a1", "a3"]


def test_degraded_inputs_recognizes_fallback_tier():
    """``fallback_tier > 0`` 与 ``degraded=True`` 同级语义（insight 降级先例）。"""
    assert degraded_inputs([_artifact("a1", fallback_tier=2)]) == ["a1"]


def test_degraded_inputs_is_read_only():
    art = _artifact("a1", degraded=True)
    before = art.model_dump(mode="json")
    degraded_inputs([art])
    assert art.model_dump(mode="json") == before


# ── damp_confidence：离散 ───────────────────────────────────────────────────


def test_discrete_downgrade_moves_exactly_one_notch():
    assert damp_confidence("high", ["a1"]) == "medium"
    assert damp_confidence("medium", ["a1"]) == "low"
    assert damp_confidence("low", ["a1"]) == "low"  # 已到底，不再下调


def test_discrete_mapping_table_matches_literal():
    assert CONFIDENCE_DOWNGRADE == {"high": "medium", "medium": "low", "low": "low"}


def test_no_degraded_input_returns_value_unchanged():
    assert damp_confidence("high", []) == "high"
    assert damp_confidence("low", None) == "low"
    assert damp_confidence("medium", []) == "medium"


def test_discrete_is_case_and_space_tolerant():
    assert damp_confidence(" HIGH ", ["a1"]) == "medium"


def test_unknown_discrete_value_is_returned_as_is():
    """未登记档位不猜测语义（与分类法"保守回退"相反：这里必须原样返回）。"""
    assert damp_confidence("very_high", ["a1"]) == "very_high"


# ── damp_confidence：连续 ───────────────────────────────────────────────────


def test_continuous_multiplies_by_085():
    assert damp_confidence(1.0, ["a1"]) == 0.85
    assert damp_confidence(0.8, ["a1"]) == 0.68


def test_continuous_is_clamped_to_unit_interval():
    assert damp_confidence(2.0, ["a1"]) == 1.0
    assert damp_confidence(0.0, ["a1"]) == 0.0


def test_continuous_no_input_unchanged():
    assert damp_confidence(0.8, []) == 0.8


# ── ★ 一档封顶（防保守化螺旋） ────────────────────────────────────────────


def test_single_notch_cap_for_discrete_with_many_inputs():
    """多个降级输入**不叠加**：仍是下调一档。"""
    many = ["a1", "a2", "a3", "a4", "a5"]
    assert damp_confidence("high", many) == "medium"
    assert damp_confidence("medium", many) == "low"


def test_single_notch_cap_for_continuous_with_many_inputs():
    """连续值只乘**一次** ×0.85，与输入个数无关（若叠加会得到 0.85^n）。"""
    many = ["a1", "a2", "a3", "a4", "a5"]
    assert damp_confidence(1.0, many) == 0.85
    assert damp_confidence(1.0, many) != round(1.0 * DAMPING_FACTOR**5, 4)


def test_repeated_damping_is_idempotent_for_bottom_tier():
    """反复阻尼不会无限下降（``low`` 是固定点）。"""
    value: str = "high"
    for _ in range(5):
        value = damp_confidence(value, ["a1"])
    assert value == "low"


# ── 边界 ────────────────────────────────────────────────────────────────────


def test_none_passes_through():
    assert damp_confidence(None, ["a1"]) is None
    assert damp_confidence(None, []) is None


def test_bool_is_not_treated_as_continuous_number():
    """``bool`` 是 ``int`` 子类，必须显式排除，避免 True→0.85。"""
    assert damp_confidence(True, ["a1"]) is True
    assert damp_confidence(False, ["a1"]) is False


def test_int_zero_is_treated_as_continuous():
    assert damp_confidence(0, ["a1"]) == 0.0


# ── 与 thesis engine 的接入一致性 ──────────────────────────────────────────


def test_thesis_engine_uses_shared_damping_factor():
    """engine 的 insight 降温必须复用同一阻尼语义（不再内联另一张系数表）。"""
    import inspect

    from alphabee.agents.thesis.engine import ThesisEngine

    source = inspect.getsource(ThesisEngine._apply_insight)
    assert "degraded_inputs" in source, "engine 未接入 services.degradation.degraded_inputs"
    assert "DAMPING_FACTOR" in source, "engine 未复用 DAMPING_FACTOR 常量"
    # 触发条件必须是"消费了降级产物"，而非"档位较低"——混用会让 high 档也被误降一档
    assert "DAMPING_FACTOR, degraded_inputs" in source


def test_engine_insight_temper_behavior_unchanged():
    """历史行为保持：insight=low → 维度 confidence ×0.85；insight=high → 不变。"""
    from alphabee.agents.thesis.engine import ThesisEngine

    engine = ThesisEngine.__new__(ThesisEngine)  # 不构造完整引擎（避免读盘/建模型）

    class _Dim:
        def __init__(self) -> None:
            self.confidence = 1.0
            self.context_notes: list[str] = []
            self.counter_evidence: list[str] = []

    dims = {"growth": _Dim()}
    engine._apply_insight(dimensions=dims, insight={"confidence": "low"})  # type: ignore[arg-type]
    assert dims["growth"].confidence == 0.85

    dims_high = {"growth": _Dim()}
    engine._apply_insight(dimensions=dims_high, insight={"confidence": "high"})  # type: ignore[arg-type]
    assert dims_high["growth"].confidence == 1.0
