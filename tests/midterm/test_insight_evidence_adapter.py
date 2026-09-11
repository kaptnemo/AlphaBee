"""InsightArtifact → EvidenceEvent 适配器（改造 A）单测。

覆盖设计 ``MIDTERM_INSIGHT_INJECTION_DESIGN.md`` §4 改造 A 的结构映射：

- ``supporting_evidence`` → confirming；``counter_evidence`` → refuting；
- ``weight``（离散）→ 强度：strong→0.5 / moderate→0.3 / weak→0.1，禁连续值；
- ``source`` → ``source_refs``；``statement`` → ``description``；
- statement 为空 → 丢弃（防幻觉）；
- 缺失/未知 weight 默认 moderate；
- 事件签名去重 id=hash(date+kind+主体)（§7）。
"""

from alphabee.midterm.insight_evidence_adapter import (
    _dedup,
    _event_id,
    adapt_insight_evidence,
)
from alphabee.midterm.models import EvidenceEvent
from alphabee.orchestrator.contracts import InsightArtifact

DISCRETE_DELTAS = {0.1, 0.3, 0.5}  # §6：weak/medium/strong，禁连续值


def _insight(supporting=None, counter=None):
    return InsightArtifact(
        core_view="看多核心观点",
        supporting_evidence=supporting or [],
        counter_evidence=counter or [],
        confidence="high",
    )


def _ev(statement="高速通信线+35.44%", source="segment:high_speed_comm", weight="strong"):
    return {"statement": statement, "source": source, "weight": weight}


# ─────────────────────────────────────────────────────────────────────────────
# 方向映射（§4 改造 A）
# ─────────────────────────────────────────────────────────────────────────────


def test_supporting_maps_to_confirming():
    events = adapt_insight_evidence(
        _insight(supporting=[_ev()]), symbol="002130.SZ", date="2026-06-30"
    )
    assert len(events) == 1
    e = events[0]
    assert e.effect_on_thesis == "confirming"
    assert e.kind == "thesis"
    assert e.date == "2026-06-30"
    assert e.description == "高速通信线+35.44%"


def test_counter_maps_to_refuting():
    events = adapt_insight_evidence(
        _insight(counter=[_ev(statement="增收不增利", source="signal:profit_leverage", weight="moderate")])
    )
    assert len(events) == 1
    assert events[0].effect_on_thesis == "refuting"
    assert events[0].description == "增收不增利"


def test_supporting_and_counter_both_injected():
    insight = _insight(
        supporting=[_ev(statement="高速通信线+35.44%", weight="strong")],
        counter=[_ev(statement="增收不增利", weight="moderate")],
    )
    events = adapt_insight_evidence(insight, symbol="002130.SZ", date="2026-06-30")
    effects = {(e.description, e.effect_on_thesis) for e in events}
    assert ("高速通信线+35.44%", "confirming") in effects
    assert ("增收不增利", "refuting") in effects


# ─────────────────────────────────────────────────────────────────────────────
# 强度离散标定（§4：strong→0.5 / moderate→0.3 / weak→0.1）
# ─────────────────────────────────────────────────────────────────────────────


def test_weight_discretized_to_strong_medium_weak():
    strong = adapt_insight_evidence(_insight(supporting=[_ev(weight="strong")]))[0]
    assert strong.confidence_delta == 0.5

    medium = adapt_insight_evidence(_insight(supporting=[_ev(weight="moderate")]))[0]
    assert medium.confidence_delta == 0.3

    weak = adapt_insight_evidence(_insight(supporting=[_ev(weight="weak")]))[0]
    assert weak.confidence_delta == 0.1


def test_deltas_are_discrete():
    insight = _insight(
        supporting=[_ev(weight="strong"), _ev(weight="moderate"), _ev(weight="weak")],
        counter=[_ev(weight="strong")],
    )
    events = adapt_insight_evidence(insight)
    assert events
    for e in events:
        assert e.confidence_delta in DISCRETE_DELTAS


def test_missing_or_unknown_weight_defaults_to_moderate():
    item = _ev()
    item.pop("weight")
    events = adapt_insight_evidence(_insight(supporting=[item]))
    assert events[0].confidence_delta == 0.3  # 缺失 → moderate

    unknown = _ev(weight="definitely-not-a-real-level")
    events = adapt_insight_evidence(_insight(supporting=[unknown]))
    assert events[0].confidence_delta == 0.3  # 未知 → moderate


# ─────────────────────────────────────────────────────────────────────────────
# 引用 → source_refs；防幻觉
# ─────────────────────────────────────────────────────────────────────────────


def test_source_becomes_source_refs():
    events = adapt_insight_evidence(_insight(supporting=[_ev(source="segment:high_speed_comm")]))
    assert events[0].source_refs == ["segment:high_speed_comm"]


def test_empty_source_yields_empty_source_refs():
    # insight 证据的 statement 本身即证据载体（与 verify 适配器「无引用不出数」口径不同）
    events = adapt_insight_evidence(_insight(supporting=[_ev(source="")]))
    assert events[0].source_refs == []


def test_empty_statement_dropped():
    events = adapt_insight_evidence(_insight(supporting=[_ev(statement="")]))
    assert events == []


def test_none_insight_returns_empty():
    assert adapt_insight_evidence(None) == []


# ─────────────────────────────────────────────────────────────────────────────
# 输入形态兼容（InsightArtifact / dict）
# ─────────────────────────────────────────────────────────────────────────────


def test_accepts_plain_dict():
    insight = {"supporting_evidence": [_ev()], "counter_evidence": []}
    events = adapt_insight_evidence(insight, symbol="002130.SZ")
    assert len(events) == 1
    assert events[0].effect_on_thesis == "confirming"


def test_accepts_real_insight_artifact():
    events = adapt_insight_evidence(_insight(supporting=[_ev()]))
    assert len(events) == 1


# ─────────────────────────────────────────────────────────────────────────────
# 去重（§7）
# ─────────────────────────────────────────────────────────────────────────────


def test_event_id_deterministic():
    a = _event_id("2026-06-30", "thesis", "002130.SZ:insight:高速通信线+35.44%")
    b = _event_id("2026-06-30", "thesis", "002130.SZ:insight:高速通信线+35.44%")
    assert a == b


def test_dedup_merges_same_id():
    a = EvidenceEvent(
        id="same",
        date="2026-06-30",
        kind="thesis",
        description="x",
        effect_on_thesis="confirming",
        confidence_delta=0.3,
        source_refs=["s1"],
    )
    b = EvidenceEvent(
        id="same",
        date="2026-06-30",
        kind="thesis",
        description="x",
        effect_on_thesis="confirming",
        confidence_delta=0.3,
        source_refs=["s2"],
    )
    merged = _dedup([a, b])
    assert len(merged) == 1
    assert merged[0].source_refs == ["s1", "s2"]
