"""verify_hypotheses → EvidenceEvent 适配器（E2）单测。

覆盖设计 ``MIDTERM_EVIDENCE_EXTRACTION.md`` §9 的结构映射：

- status（验证结论）→ effect_on_thesis（verified→confirming / rejected→refuting /
  partial→neutral / unknown→丢弃）；
- confidence（连续）→ 离散等级（>=0.7 strong / >=0.4 medium / else weak），禁连续值；
- supporting/refuting evidence → source_refs；
- 无引用/无结论丢弃（§3 铁律「无引用不出数」）；
- 事件签名去重 id=hash(date+kind+主体+数值)（§7）。
"""

from alphabee.agents.schemas import VerificationResultItem, VerificationResultList
from alphabee.midterm.evidence_adapter import (
    _coerce_items,
    _dedup,
    _event_id,
    adapt_verification,
)
from alphabee.midterm.models import EvidenceEvent

DISCRETE_DELTAS = {0.1, 0.3, 0.5}  # §6：weak/medium/strong，禁连续值


def _vr(status="verified", hypothesis_id="h1", confidence=0.8, support=None, refute=None, summary="假设被证实"):
    return {
        "id": "v1",
        "hypothesis_id": hypothesis_id,
        "status": status,
        "support_score": 0.85,
        "contradiction_score": 0.1,
        "confidence": confidence,
        "supporting_evidence": support if support is not None else ["应收账款周转天数连续3期上升"],
        "refuting_evidence": refute or [],
        "gaps": [],
        "summary": summary,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 方向映射（§9）
# ─────────────────────────────────────────────────────────────────────────────
def test_verified_maps_to_confirming():
    events = adapt_verification([_vr(status="verified")], symbol="300750", date="2026-04-01")
    assert len(events) == 1
    e = events[0]
    assert e.effect_on_thesis == "confirming"
    assert e.kind == "thesis"
    assert e.date == "2026-04-01"


def test_rejected_maps_to_refuting():
    events = adapt_verification([_vr(status="rejected")])
    assert len(events) == 1
    assert events[0].effect_on_thesis == "refuting"


def test_partial_maps_to_neutral_zero_delta():
    events = adapt_verification([_vr(status="partial")])
    assert len(events) == 1
    e = events[0]
    assert e.effect_on_thesis == "neutral"
    assert e.confidence_delta == 0.0  # neutral 无方向证据，bayes no-op


def test_unknown_dropped():
    assert adapt_verification([_vr(status="unknown")]) == []


# ─────────────────────────────────────────────────────────────────────────────
# 强度离散标定（§6 禁连续值）
# ─────────────────────────────────────────────────────────────────────────────
def test_confidence_discretized_to_strong_medium_weak():
    strong = adapt_verification([_vr(confidence=0.85)])[0]
    assert strong.confidence_delta == 0.5  # >=0.7 → strong

    medium = adapt_verification([_vr(confidence=0.5)])[0]
    assert medium.confidence_delta == 0.3  # >=0.4 → medium

    weak = adapt_verification([_vr(confidence=0.2)])[0]
    assert weak.confidence_delta == 0.1  # else → weak


def test_confidence_missing_defaults_to_weak():
    item = _vr()
    item.pop("confidence")
    events = adapt_verification([item])
    assert events[0].confidence_delta == 0.1  # §11 缺失 → 默认 weak


def test_directional_deltas_are_discrete():
    items = [
        _vr(status="verified", confidence=0.99),
        _vr(status="verified", confidence=0.45),
        _vr(status="rejected", confidence=0.1),
    ]
    events = adapt_verification(items)
    assert events
    for e in events:
        assert e.effect_on_thesis in ("confirming", "refuting")
        assert e.confidence_delta in DISCRETE_DELTAS


# ─────────────────────────────────────────────────────────────────────────────
# 引用 → source_refs；防幻觉
# ─────────────────────────────────────────────────────────────────────────────
def test_source_refs_merge_supporting_and_refuting():
    item = _vr(support=["支持证据A"], refute=["反对证据B"])
    events = adapt_verification([item])
    assert events[0].source_refs == ["支持证据A", "反对证据B"]


def test_no_evidence_dropped():
    # 无引用不出数（§3 铁律）
    item = _vr(support=[], refute=[])
    assert adapt_verification([item]) == []


def test_description_is_summary():
    item = _vr(summary="应收账款恶化趋势被证实")
    events = adapt_verification([item])
    assert events[0].description == "应收账款恶化趋势被证实"


# ─────────────────────────────────────────────────────────────────────────────
# 输入形态兼容（list / dict / VerificationResultList）
# ─────────────────────────────────────────────────────────────────────────────
def test_accepts_single_dict():
    events = adapt_verification(_vr(status="verified"))
    assert len(events) == 1


def test_accepts_real_model_list():
    item = VerificationResultItem(
        id="v1",
        hypothesis_id="h1",
        status="verified",
        support_score=0.85,
        contradiction_score=0.1,
        confidence=0.8,
        supporting_evidence=["证据1"],
        summary="假设被证实",
    )
    events = adapt_verification([item])
    assert len(events) == 1
    assert events[0].effect_on_thesis == "confirming"


def test_accepts_verification_result_list_object():
    vlist = VerificationResultList(
        results=[
            VerificationResultItem(
                id="v1",
                hypothesis_id="h1",
                status="rejected",
                support_score=0.1,
                contradiction_score=0.9,
                confidence=0.85,
                refuting_evidence=["反证1"],
                summary="假设被推翻",
            )
        ]
    )
    events = adapt_verification(vlist)
    assert len(events) == 1
    assert events[0].effect_on_thesis == "refuting"
    assert events[0].confidence_delta == 0.5


def test_accepts_dict_with_results_key():
    events = adapt_verification({"results": [_vr(status="verified")]})
    assert len(events) == 1


# ─────────────────────────────────────────────────────────────────────────────
# 去重（§7）
# ─────────────────────────────────────────────────────────────────────────────
def test_event_id_deterministic():
    a = _event_id("2026-04-01", "thesis", "300750:verify:h1", 0.8)
    b = _event_id("2026-04-01", "thesis", "300750:verify:h1", 0.8)
    assert a == b


def test_dedup_merges_same_id():
    a = EvidenceEvent(
        id="same",
        date="2026-04-01",
        kind="thesis",
        description="x",
        effect_on_thesis="confirming",
        confidence_delta=0.1,
        source_refs=["s1"],
    )
    b = EvidenceEvent(
        id="same",
        date="2026-04-01",
        kind="thesis",
        description="x",
        effect_on_thesis="confirming",
        confidence_delta=0.1,
        source_refs=["s2"],
    )
    merged = _dedup([a, b])
    assert len(merged) == 1
    assert merged[0].source_refs == ["s1", "s2"]


def test_coerce_items_various_shapes():
    assert len(_coerce_items(None)) == 0
    assert len(_coerce_items(_vr())) == 1  # 单个 dict
    assert len(_coerce_items([_vr(), _vr(hypothesis_id="h2")])) == 2
