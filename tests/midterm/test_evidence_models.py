"""证据抽取 typed contracts（FactEvent / EvidenceJudgment）单测。

覆盖设计 ``MIDTERM_EVIDENCE_EXTRACTION.md`` §4 的 Stage A/B 输出契约与 §6 的
离散等级标定常量：

- ``FactEvent``：Stage A 客观事实（无方向判定，可跨 thesis 复用/缓存）；
- ``EvidenceJudgment``：Stage B 相对 thesis 的方向与强度（离散等级，禁连续值）；
- ``STRENGTH_DELTA`` / ``MAX_CONFIDENCE_DELTA``：discrete 等级 → confidence_delta
  数值的唯一映射（E1 规则标定与 E3 Stage B 共享，防口径分裂）。

纪律（alphabee-schema-steward / alphabee-pipeline-contract-steward）：
- 缺失显式 None / 空容器默认，绝不静默回退 0；
- FactEvent.numbers 只承载原文可溯源的 canonical 数值（缺失字段置 None）；
- confidence_delta 只允许 weak=0.1 / medium=0.3 / strong=0.5，单条上限 0.7。
"""

import pytest
from pydantic import ValidationError

from alphabee.midterm.models import (
    MAX_CONFIDENCE_DELTA,
    STRENGTH_DELTA,
    STRENGTH_LEVELS,
    EffectOnThesis,
    EvidenceEvent,
    EvidenceJudgment,
    FactEvent,
    Strength,
)


# ─────────────────────────────────────────────────────────────────────────────
# FactEvent（Stage A 客观事实）
# ─────────────────────────────────────────────────────────────────────────────
def test_fact_event_minimal():
    ev = FactEvent(id="f1", date="2026-01-05", kind="expectation", description="Q2 营收恢复")
    assert ev.id == "f1"
    assert ev.date == "2026-01-05"
    assert ev.kind == "expectation"
    assert ev.numbers == {}  # 默认空容器，绝不静默回退 0
    assert ev.quotes == []
    assert ev.source_refs == []
    assert ev.source_type == ""


def test_fact_event_full():
    ev = FactEvent(
        id="f1",
        date="2026-01-05",
        kind="expectation",
        description="Q2 营收同比 +5.2%",
        numbers={"revenue_yoy": 5.2, "net_profit_yoy": None},  # 原文缺失字段显式 None
        quotes=["Q2 营收同比增长 5.2%"],
        source_refs=["https://example.com/report"],
        source_type="forecast",
    )
    assert ev.numbers["revenue_yoy"] == 5.2
    assert ev.numbers["net_profit_yoy"] is None  # 原文没有的数值不得补全
    assert ev.quotes == ["Q2 营收同比增长 5.2%"]
    assert ev.source_refs == ["https://example.com/report"]
    assert ev.source_type == "forecast"


def test_fact_event_no_direction_field():
    # FactEvent 是客观层，不承载方向判定（方向由 Stage B EvidenceJudgment 产出）
    ev = FactEvent(id="f1", date="2026-01-05", kind="expectation", description="X")
    assert not hasattr(ev, "effect_on_thesis")
    assert not hasattr(ev, "strength")


# ─────────────────────────────────────────────────────────────────────────────
# EvidenceJudgment（Stage B 方向判定）
# ─────────────────────────────────────────────────────────────────────────────
def test_evidence_judgment_minimal():
    j = EvidenceJudgment(fact_id="f1", effect_on_thesis="confirming", strength="medium", reasoning="营收恢复支撑 H")
    assert j.fact_id == "f1"
    assert j.effect_on_thesis == "confirming"
    assert j.strength == "medium"
    assert j.reasoning == "营收恢复支撑 H"


def test_strength_and_effect_are_strenum():
    # 契约要求：strength / effect_on_thesis 用 StrEnum，禁连续值（§4.3 / §6）
    assert isinstance(Strength.WEAK, str)  # StrEnum 是 str 子类
    assert {s.value for s in Strength} == {"weak", "medium", "strong"}
    assert {e.value for e in EffectOnThesis} == {"confirming", "refuting", "neutral"}


def test_evidence_judgment_string_coercion_to_enum():
    # 传入字符串被 Pydantic 归一化为 StrEnum 成员
    j = EvidenceJudgment(fact_id="f1", effect_on_thesis="confirming", strength="medium", reasoning="r")
    assert isinstance(j.effect_on_thesis, EffectOnThesis)
    assert j.effect_on_thesis is EffectOnThesis.CONFIRMING
    assert isinstance(j.strength, Strength)
    assert j.strength is Strength.MEDIUM


def test_evidence_judgment_rejects_continuous_strength():
    # 禁连续值：strength="0.42" 在契约层即被拒绝（Stage B LLM 自由出连续值 → 报错）
    with pytest.raises(ValidationError):
        EvidenceJudgment(fact_id="f1", effect_on_thesis="confirming", strength="0.42", reasoning="r")


def test_evidence_judgment_rejects_unknown_effect():
    # effect_on_thesis 只允许 confirming/refuting/neutral 三个方向
    with pytest.raises(ValidationError):
        EvidenceJudgment(fact_id="f1", effect_on_thesis="maybe", strength="weak", reasoning="r")


def test_evidence_judgment_round_trip():
    j = EvidenceJudgment(fact_id="f1", effect_on_thesis="refuting", strength="strong", reasoning="海外需求偏弱，证伪 H")
    data = j.model_dump()
    assert data["effect_on_thesis"] == "refuting"
    assert data["strength"] == "strong"
    assert EvidenceJudgment(**data) == j


# ─────────────────────────────────────────────────────────────────────────────
# 离散等级标定常量（§6，禁 LLM 连续值）
# ─────────────────────────────────────────────────────────────────────────────
def test_strength_levels_are_exactly_three_discrete_values():
    assert STRENGTH_LEVELS == ("weak", "medium", "strong")


def test_strength_delta_mapping_matches_design():
    # §6：weak=0.1 / medium=0.3 / strong=0.5，全部离散，无连续值
    assert STRENGTH_DELTA == {"weak": 0.1, "medium": 0.3, "strong": 0.5}


def test_max_confidence_delta_caps_single_event():
    # 单条证据强度上限 0.7（d>0.7 后 log((1+d)/(1-d)) 爆炸，§6）
    assert MAX_CONFIDENCE_DELTA == 0.7


def test_strength_delta_values_within_cap():
    # 所有离散等级数值都不得超过单条上限，且只取允许的三个等级
    assert set(STRENGTH_DELTA.keys()) == set(STRENGTH_LEVELS)
    assert all(0.0 < d <= MAX_CONFIDENCE_DELTA for d in STRENGTH_DELTA.values())


# ─────────────────────────────────────────────────────────────────────────────
# 组装链路：FactEvent + EvidenceJudgment → EvidenceEvent（下游 bayes 消费）
# ─────────────────────────────────────────────────────────────────────────────
def test_fact_plus_judgment_assembles_evidence_event():
    fact = FactEvent(
        id="f1",
        date="2026-01-05",
        kind="expectation",
        description="Q2 营收同比 +5.2%",
        numbers={"revenue_yoy": 5.2},
        quotes=["Q2 营收同比增长 5.2%"],
        source_refs=["https://example.com/report"],
        source_type="forecast",
    )
    judgment = EvidenceJudgment(
        fact_id="f1", effect_on_thesis="confirming", strength="medium", reasoning="营收恢复支撑 H"
    )
    # 组装规则（E1/E3 落地）：confidence_delta 只能来自离散等级映射，绝不使用连续值
    delta = STRENGTH_DELTA[judgment.strength]
    event = EvidenceEvent(
        id=fact.id,
        date=fact.date,
        kind=fact.kind,
        description=fact.description,
        effect_on_thesis=judgment.effect_on_thesis,
        confidence_delta=delta,
        source_refs=fact.source_refs,
    )
    assert event.confidence_delta == 0.3  # medium
    assert event.effect_on_thesis == "confirming"
    assert event.source_refs == fact.source_refs


def test_invalid_strength_is_not_a_discrete_level():
    # 连续值 / 未知等级不在离散映射中（组装层必须拒绝，禁止自由出数）
    with pytest.raises(KeyError):
        _ = STRENGTH_DELTA["0.42"]
    with pytest.raises(KeyError):
        _ = STRENGTH_DELTA["very_strong"]
