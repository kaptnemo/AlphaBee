"""Snapshot Diff 子模型（D1 typed contracts）单测。

覆盖 design ``MIDTERM_STATE_DIFF_DESIGN.md`` §3 的 8 个子模型：
ArtifactRef / FieldDelta / FactorDelta / StateShift / ConfidenceDelta /
EVDiff / PositionDiff / ChangeAttribution。

纪律（alphabee-schema-steward）：
- ``FieldDelta.change`` 严格区分 appeared / disappeared 与 up / down / unchanged；
- 缺失显式 None，绝不静默回退 0；
- L1 的 ``FactorDelta`` 与 L2 的 ``FactorScoreDelta`` 口径分离。
"""

from alphabee.midterm.models import (
    ArtifactRef,
    ChangeAttribution,
    ConfidenceDelta,
    EVDiff,
    FactorDelta,
    FactorScoreDelta,
    FieldChange,
    FieldDelta,
    PositionDiff,
    StateShift,
)


# ─────────────────────────────────────────────────────────────────────────────
# ArtifactRef
# ─────────────────────────────────────────────────────────────────────────────
def test_artifact_ref_minimal():
    ref = ArtifactRef(id="cs-001", date="2026-01-01")
    assert ref.symbol == ""
    assert ref.id == "cs-001"
    assert ref.date == "2026-01-01"


# ─────────────────────────────────────────────────────────────────────────────
# FieldChange（StrEnum：appeared/disappeared 与 up/down 严格区分）
# ─────────────────────────────────────────────────────────────────────────────
def test_field_change_is_strenum_with_five_values():
    values = {FieldChange.APPEARED, FieldChange.DISAPPEARED, FieldChange.UP, FieldChange.DOWN, FieldChange.UNCHANGED}
    assert {v.value for v in values} == {"appeared", "disappeared", "up", "down", "unchanged"}
    # StrEnum 成员是 str 子类，可与字符串字面量直接比较
    assert FieldChange.APPEARED == "appeared"
    assert FieldChange.DISAPPEARED == "disappeared"


# ─────────────────────────────────────────────────────────────────────────────
# FieldDelta（appeared/disappeared 与 up/down 严格区分）
# ─────────────────────────────────────────────────────────────────────────────
def test_field_delta_up():
    d = FieldDelta(field="revenue_yoy", prev=10.0, curr=15.0, delta=5.0, rel_delta=0.5, change=FieldChange.UP)
    assert d.change == FieldChange.UP
    assert d.change == "up"  # StrEnum 与 str 字面量等价
    assert d.delta == 5.0
    assert d.rel_delta == 0.5


def test_field_delta_appeared_is_not_up():
    # None → 有值 = appeared，绝不得标记为 up
    d = FieldDelta(
        field="eps_fy1_revision_1m",
        prev=None,
        curr=8.0,
        delta=None,
        rel_delta=None,
        change=FieldChange.APPEARED,
    )
    assert d.change == FieldChange.APPEARED
    assert d.change != FieldChange.UP  # appeared 与 up 严格区分
    assert d.delta is None  # 基线登记不产数值差
    assert d.prev is None


def test_field_delta_disappeared_is_not_down():
    # 有值 → None = disappeared，绝不得标记为 down
    d = FieldDelta(
        field="turnover_rate",
        prev=3.2,
        curr=None,
        delta=None,
        rel_delta=None,
        change=FieldChange.DISAPPEARED,
    )
    assert d.change == FieldChange.DISAPPEARED
    assert d.change != FieldChange.DOWN  # disappeared 与 down 严格区分
    assert d.curr is None


def test_field_delta_string_coercion_to_enum():
    # 传入字符串 "up" 被 Pydantic 归一化为 FieldChange.UP
    d = FieldDelta(field="roe", prev=10.0, curr=12.0, delta=2.0, rel_delta=0.2, change="up")
    assert isinstance(d.change, FieldChange)
    assert d.change == FieldChange.UP


# ─────────────────────────────────────────────────────────────────────────────
# FactorDelta（L1 因子级聚合）
# ─────────────────────────────────────────────────────────────────────────────
def test_factor_delta_aggregates_fields():
    fields = [
        FieldDelta(field="revenue_yoy", prev=10.0, curr=15.0, delta=5.0, rel_delta=0.5, change="up"),
        FieldDelta(field="gross_margin", prev=20.0, curr=22.0, delta=2.0, rel_delta=0.1, change="up"),
    ]
    fd = FactorDelta(factor="F", direction="improving", fields=fields, consistency="resonant")
    assert fd.factor == "F"
    assert fd.direction == "improving"
    assert len(fd.fields) == 2
    assert fd.consistency == "resonant"


def test_factor_delta_defaults():
    fd = FactorDelta(factor="M", direction="neutral")
    assert fd.fields == []
    assert fd.consistency == ""


# ─────────────────────────────────────────────────────────────────────────────
# StateShift（L3 软状态漂移）
# ─────────────────────────────────────────────────────────────────────────────
def test_state_shift_quality_flow():
    shift = StateShift(
        argmax_from="S1",
        argmax_to="S2",
        mass_delta={"S1": -0.2, "S2": 0.2},
        tv_distance=0.2,
        entropy_from=1.2,
        entropy_to=1.0,
        entropy_delta=-0.2,
        drift={"S2": 0.2},
        legal=True,
        kind="upgrade",
    )
    assert shift.argmax_from == "S1"
    assert shift.argmax_to == "S2"
    assert shift.mass_delta["S2"] == 0.2
    assert shift.tv_distance == 0.2
    assert shift.entropy_delta == -0.2  # 负 = 变确定
    assert shift.legal is True
    assert shift.kind == "upgrade"


def test_state_shift_first_frame_argmax_from_none():
    shift = StateShift(argmax_from=None, argmax_to="S2", tv_distance=0.0, entropy_to=1.0)
    assert shift.argmax_from is None
    assert shift.entropy_from is None
    assert shift.drift is None


# ─────────────────────────────────────────────────────────────────────────────
# ConfidenceDelta（L3' 置信度差）
# ─────────────────────────────────────────────────────────────────────────────
def test_confidence_delta():
    cd = ConfidenceDelta(prior=0.58, posterior=0.72, delta=0.14, log_odds_delta=0.62, evidence_ids=["e1", "e2"])
    assert cd.delta == 0.14
    assert cd.log_odds_delta == 0.62
    assert cd.evidence_ids == ["e1", "e2"]


def test_confidence_delta_default_evidence():
    cd = ConfidenceDelta(prior=None, posterior=None, delta=None, log_odds_delta=None)
    assert cd.evidence_ids == []


# ─────────────────────────────────────────────────────────────────────────────
# EVDiff（L4 赔率差）
# ─────────────────────────────────────────────────────────────────────────────
def test_ev_diff_scenario_deltas():
    ev = EVDiff(
        ev_from=12.0,
        ev_to=15.0,
        ev_delta=3.0,
        risk_adjusted_ev_delta=0.5,
        scenario_probability_delta={"bull": 0.05, "base": 0.0, "bear": -0.05},
        scenario_return_delta={"bull": 5.0, "base": None, "bear": -3.0},
        probability_source_change="state_prior → bayes_posterior",
    )
    assert ev.ev_delta == 3.0
    assert ev.scenario_probability_delta["bull"] == 0.05
    assert ev.scenario_return_delta["base"] is None
    assert ev.probability_source_change == "state_prior → bayes_posterior"


# ─────────────────────────────────────────────────────────────────────────────
# PositionDiff（L5 仓位差）
# ─────────────────────────────────────────────────────────────────────────────
def test_position_diff_band_divergence():
    pd = PositionDiff(
        stock_weight_delta=0.05,
        actual_weight_delta=-0.2,
        exposure_delta=None,
        band_from="核心",
        band_to="核心",
        band_weight_divergence=True,
        drivers=["portfolio"],
    )
    assert pd.band_from == "核心"
    assert pd.band_to == "核心"
    assert pd.band_weight_divergence is True
    assert pd.drivers == ["portfolio"]


def test_position_diff_defaults():
    pd = PositionDiff()
    assert pd.stock_weight_delta is None
    assert pd.band_from == ""
    assert pd.band_weight_divergence is False
    assert pd.drivers == []


# ─────────────────────────────────────────────────────────────────────────────
# ChangeAttribution（归因）
# ─────────────────────────────────────────────────────────────────────────────
def test_change_attribution():
    a = ChangeAttribution(
        evidence_ids=["e1", "e3"],
        factor_deltas=["E", "F"],
        decision_effects=["state:S1→S2", "confidence:+0.14"],
        note="Q2 收入与 EPS 上修驱动状态迁移",
    )
    assert a.evidence_ids == ["e1", "e3"]
    assert a.factor_deltas == ["E", "F"]
    assert a.decision_effects == ["state:S1→S2", "confidence:+0.14"]


def test_change_attribution_defaults():
    a = ChangeAttribution()
    assert a.evidence_ids == []
    assert a.factor_deltas == []
    assert a.decision_effects == []
    assert a.note == ""


# ─────────────────────────────────────────────────────────────────────────────
# 口径分离：L1 FactorDelta vs L2 FactorScoreDelta（不互相污染）
# ─────────────────────────────────────────────────────────────────────────────
def test_factor_delta_and_score_delta_are_distinct_contracts():
    # L2 评分层：classifier 的方向分 Δ（有 delta 字段，无 fields）
    score_delta = FactorScoreDelta(factor="E", delta=0.8)
    assert score_delta.delta == 0.8
    assert not hasattr(score_delta, "fields")

    # L1 数据层：diff 的字段级聚合（有 fields，无 delta 字段）
    l1_delta = FactorDelta(factor="E", direction="improving")
    assert l1_delta.fields == []
    assert not hasattr(l1_delta, "delta")
