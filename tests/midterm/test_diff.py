"""diff.py 引擎 + L1/L2（D2-1）+ L3/L3'/L4（D2-2）单测。

覆盖 design §4 步骤 0/1（校验 + 首帧基线登记）、步骤 2（L1 因子差）、步骤 3
（L2 评分差）、步骤 4（L3 状态漂移）、步骤 5（L3' 置信度差）、步骤 6（L4 赔率差）。
纪律：数值核心纯规则禁 LLM；appeared/disappeared 与 up/down 严格区分；缺失显式
None；软状态漂移与 argmax/熵正确区分。
"""

import pytest

from alphabee.midterm.diff import diff
from alphabee.midterm.models import (
    CompanyStateArtifact,
    CompanyStateDiff,
    EvidenceEvent,
    ExpectedValue,
    FactorSnapshot,
    FieldChange,
    FundamentalFactor,
    ScenarioOutcome,
    StateBelief,
    TrendFactor,
    VariableScores,
)


def _artifact(
    symbol: str = "000977",
    as_of_date: str = "2026-01-01",
    schema_version: str = "1",
    snapshot: FactorSnapshot | None = None,
    scores: VariableScores | None = None,
    state: StateBelief | None = None,
    thesis_confidence: float = 0.0,
    expected_value: ExpectedValue | None = None,
    evidence_log: list[EvidenceEvent] | None = None,
) -> CompanyStateArtifact:
    return CompanyStateArtifact(
        schema_version=schema_version,
        symbol=symbol,
        as_of_date=as_of_date,
        factor_snapshot=snapshot,
        variable_scores=scores or VariableScores(),
        state=state,
        thesis_confidence=thesis_confidence,
        expected_value=expected_value,
        evidence_log=evidence_log or [],
    )


def _fundamental(**kwargs) -> FundamentalFactor:
    return FundamentalFactor(**kwargs)


def _snapshot(
    as_of_date: str = "2026-01-01", fundamental: FundamentalFactor | None = None, trend=None
) -> FactorSnapshot:
    return FactorSnapshot(
        symbol="000977",
        as_of_date=as_of_date,
        fundamental=fundamental or FundamentalFactor(),
        trend=trend or TrendFactor(),
    )


def _belief(distribution: dict[str, float], argmax: str, entropy: float) -> StateBelief:
    return StateBelief(distribution=distribution, argmax_state=argmax, entropy=entropy)


def _ev(ev: float | None, rae: float | None, scenarios: list[ScenarioOutcome], source: str) -> ExpectedValue:
    return ExpectedValue(scenarios=scenarios, ev=ev, risk_adjusted_ev=rae, probability_source=source)


def _scenarios(bull_p=0.4, bull_r=20.0, base_p=0.4, base_r=5.0, bear_p=0.2, bear_r=-10.0) -> list[ScenarioOutcome]:
    return [
        ScenarioOutcome(scenario="bull", probability=bull_p, expected_return=bull_r),
        ScenarioOutcome(scenario="base", probability=base_p, expected_return=base_r),
        ScenarioOutcome(scenario="bear", probability=bear_p, expected_return=bear_r),
    ]


# ─────────────────────────────────────────────────────────────────────────────
# 校验（步骤 0）
# ─────────────────────────────────────────────────────────────────────────────
def test_validation_symbol_mismatch_raises():
    prev = _artifact(symbol="000977", as_of_date="2026-01-01")
    curr = _artifact(symbol="600519", as_of_date="2026-01-05")
    with pytest.raises(ValueError, match="symbol 不一致"):
        diff(prev, curr)


def test_validation_schema_version_mismatch_raises():
    prev = _artifact(as_of_date="2026-01-01", schema_version="1")
    curr = _artifact(as_of_date="2026-01-05", schema_version="2")
    with pytest.raises(ValueError, match="schema_version 不一致"):
        diff(prev, curr)


def test_validation_date_order_raises():
    prev = _artifact(as_of_date="2026-01-05")
    curr = _artifact(as_of_date="2026-01-01")
    with pytest.raises(ValueError, match="curr.date 必须晚于 prev.date"):
        diff(prev, curr)


def test_validation_same_date_raises():
    prev = _artifact(as_of_date="2026-01-05")
    curr = _artifact(as_of_date="2026-01-05")
    with pytest.raises(ValueError):
        diff(prev, curr)


# ─────────────────────────────────────────────────────────────────────────────
# 首帧基线登记（步骤 1）
# ─────────────────────────────────────────────────────────────────────────────
def test_first_frame_baseline_registration():
    curr = _artifact(
        as_of_date="2026-01-01",
        snapshot=_snapshot(fundamental=_fundamental(revenue_yoy=10.0, roe=None)),
    )
    d = diff(None, curr)
    assert isinstance(d, CompanyStateDiff)
    assert d.is_first is True
    assert d.prev is None
    assert d.curr.date == "2026-01-01"
    assert d.elapsed_days == 0
    assert d.state_shift is None  # 首帧无状态漂移
    assert d.scores == {}  # 首帧无 L2 评分差

    # 全部字段 change=appeared（基线登记）
    f_factor = next(f for f in d.factors if f.factor == "F")
    fields = {fd.field: fd for fd in f_factor.fields}
    assert fields["revenue_yoy"].change == FieldChange.APPEARED
    assert fields["revenue_yoy"].prev is None
    assert fields["revenue_yoy"].curr == 10.0
    assert fields["revenue_yoy"].delta is None
    # None 字段不登记 appeared（缺失 ≠ 出现）
    assert "roe" not in fields


def test_first_frame_no_attribution_no_exit():
    curr = _artifact(as_of_date="2026-01-01", snapshot=_snapshot(fundamental=_fundamental(revenue_yoy=10.0)))
    d = diff(None, curr)
    assert d.attribution == []
    assert d.exit_conditions_met == []
    assert d.new_evidence == []


def test_first_frame_seven_factors_present():
    curr = _artifact(as_of_date="2026-01-01", snapshot=_snapshot(fundamental=_fundamental(revenue_yoy=10.0)))
    d = diff(None, curr)
    assert {f.factor for f in d.factors} == {"F", "E", "T", "V", "C", "R", "M"}


# ─────────────────────────────────────────────────────────────────────────────
# L1 字段差（步骤 2）：up/down/appeared/disappeared/unchanged
# ─────────────────────────────────────────────────────────────────────────────
def test_l1_field_delta_up():
    prev = _artifact(as_of_date="2026-01-01", snapshot=_snapshot(fundamental=_fundamental(revenue_yoy=10.0)))
    curr = _artifact(as_of_date="2026-01-05", snapshot=_snapshot(fundamental=_fundamental(revenue_yoy=15.0)))
    d = diff(prev, curr)
    f_factor = next(f for f in d.factors if f.factor == "F")
    fields = {fd.field: fd for fd in f_factor.fields}
    assert fields["revenue_yoy"].change == FieldChange.UP
    assert fields["revenue_yoy"].delta == 5.0
    assert fields["revenue_yoy"].rel_delta == pytest.approx(0.5)


def test_l1_field_delta_down():
    prev = _artifact(as_of_date="2026-01-01", snapshot=_snapshot(fundamental=_fundamental(gross_margin=20.0)))
    curr = _artifact(as_of_date="2026-01-05", snapshot=_snapshot(fundamental=_fundamental(gross_margin=18.0)))
    d = diff(prev, curr)
    f_factor = next(f for f in d.factors if f.factor == "F")
    fields = {fd.field: fd for fd in f_factor.fields}
    assert fields["gross_margin"].change == FieldChange.DOWN
    assert fields["gross_margin"].delta == -2.0
    assert fields["gross_margin"].rel_delta == pytest.approx(-0.1)


def test_l1_field_delta_appeared_not_up():
    # None → 有值 = appeared，绝不得标记为 up
    prev = _artifact(as_of_date="2026-01-01", snapshot=_snapshot(fundamental=_fundamental(revenue_yoy=None)))
    curr = _artifact(as_of_date="2026-01-05", snapshot=_snapshot(fundamental=_fundamental(revenue_yoy=8.0)))
    d = diff(prev, curr)
    f_factor = next(f for f in d.factors if f.factor == "F")
    fields = {fd.field: fd for fd in f_factor.fields}
    assert fields["revenue_yoy"].change == FieldChange.APPEARED
    assert fields["revenue_yoy"].delta is None
    assert fields["revenue_yoy"].prev is None


def test_l1_field_delta_disappeared_not_down():
    # 有值 → None = disappeared，绝不得标记为 down
    prev = _artifact(as_of_date="2026-01-01", snapshot=_snapshot(fundamental=_fundamental(net_margin=12.0)))
    curr = _artifact(as_of_date="2026-01-05", snapshot=_snapshot(fundamental=_fundamental(net_margin=None)))
    d = diff(prev, curr)
    f_factor = next(f for f in d.factors if f.factor == "F")
    fields = {fd.field: fd for fd in f_factor.fields}
    assert fields["net_margin"].change == FieldChange.DISAPPEARED
    assert fields["net_margin"].delta is None
    assert fields["net_margin"].curr is None


def test_l1_unchanged_excluded_from_fields():
    prev = _artifact(as_of_date="2026-01-01", snapshot=_snapshot(fundamental=_fundamental(roe=10.0)))
    curr = _artifact(as_of_date="2026-01-05", snapshot=_snapshot(fundamental=_fundamental(roe=10.0)))
    d = diff(prev, curr)
    f_factor = next(f for f in d.factors if f.factor == "F")
    assert all(fd.field != "roe" for fd in f_factor.fields)  # 未变化字段不进入 fields


def test_l1_rel_delta_division_by_zero_is_none():
    # prev=0 时 rel_delta 无法定义 → None（不静默回退）
    prev = _artifact(as_of_date="2026-01-01", snapshot=_snapshot(fundamental=_fundamental(operating_cashflow=0.0)))
    curr = _artifact(as_of_date="2026-01-05", snapshot=_snapshot(fundamental=_fundamental(operating_cashflow=5.0)))
    d = diff(prev, curr)
    f_factor = next(f for f in d.factors if f.factor == "F")
    fields = {fd.field: fd for fd in f_factor.fields}
    assert fields["operating_cashflow"].change == FieldChange.UP
    assert fields["operating_cashflow"].delta == 5.0
    assert fields["operating_cashflow"].rel_delta is None


def test_l1_factor_direction_and_consistency():
    prev = _artifact(
        as_of_date="2026-01-01", snapshot=_snapshot(fundamental=_fundamental(revenue_yoy=10.0, gross_margin=20.0))
    )
    curr = _artifact(
        as_of_date="2026-01-05", snapshot=_snapshot(fundamental=_fundamental(revenue_yoy=15.0, gross_margin=22.0))
    )
    d = diff(prev, curr)
    f_factor = next(f for f in d.factors if f.factor == "F")
    assert f_factor.direction == "improving"


# ─────────────────────────────────────────────────────────────────────────────
# L2 评分差（步骤 3）
# ─────────────────────────────────────────────────────────────────────────────
def test_l2_score_delta():
    prev = _artifact(as_of_date="2026-01-01", scores=VariableScores(e_revision=0.3, f_fundamental_trend=0.1))
    curr = _artifact(as_of_date="2026-01-05", scores=VariableScores(e_revision=0.8, f_fundamental_trend=0.4))
    d = diff(prev, curr)
    assert d.scores["e_revision"] == pytest.approx(0.5)
    assert d.scores["f_fundamental_trend"] == pytest.approx(0.3)


def test_l2_score_none_semantics():
    # 任一侧缺失 → 对应项显式 None（不静默回退 0）
    prev = _artifact(as_of_date="2026-01-01", scores=VariableScores(e_revision=None))
    curr = _artifact(as_of_date="2026-01-05", scores=VariableScores(e_revision=0.8))
    d = diff(prev, curr)
    assert d.scores["e_revision"] is None


def test_l2_scores_cover_six_numeric_fields():
    prev = _artifact(as_of_date="2026-01-01")
    curr = _artifact(as_of_date="2026-01-05")
    d = diff(prev, curr)
    assert set(d.scores.keys()) == {
        "f_fundamental_trend",
        "e_revision",
        "t_relative_strength",
        "v_valuation_percentile",
        "c_crowding",
        "r_risk",
    }


# ─────────────────────────────────────────────────────────────────────────────
# L3 状态漂移（步骤 4）
# ─────────────────────────────────────────────────────────────────────────────
def test_state_shift_upgrade_mass_delta_and_tv():
    prev = _artifact(as_of_date="2026-01-01", state=_belief({"S1": 0.7, "S2": 0.3}, argmax="S1", entropy=0.6))
    curr = _artifact(as_of_date="2026-01-05", state=_belief({"S1": 0.3, "S2": 0.7}, argmax="S2", entropy=0.6))
    d = diff(prev, curr)
    ss = d.state_shift
    assert ss.argmax_from == "S1"
    assert ss.argmax_to == "S2"
    assert ss.kind == "upgrade"
    assert ss.legal is True
    assert ss.mass_delta["S1"] == pytest.approx(-0.4)
    assert ss.mass_delta["S2"] == pytest.approx(0.4)
    assert ss.tv_distance == pytest.approx(0.4)  # 0.5·(|-0.4|+|0.4|)


def test_state_shift_downgrade():
    prev = _artifact(as_of_date="2026-01-01", state=_belief({"S3": 0.8, "S2": 0.2}, argmax="S3", entropy=0.5))
    curr = _artifact(as_of_date="2026-01-05", state=_belief({"S3": 0.4, "S2": 0.6}, argmax="S2", entropy=0.5))
    d = diff(prev, curr)
    assert d.state_shift.kind == "downgrade"
    assert d.state_shift.legal is True


def test_state_shift_reopen():
    prev = _artifact(as_of_date="2026-01-01", state=_belief({"S3": 0.9, "S1": 0.1}, argmax="S3", entropy=0.4))
    curr = _artifact(as_of_date="2026-01-05", state=_belief({"S3": 0.3, "S1": 0.7}, argmax="S1", entropy=0.5))
    d = diff(prev, curr)
    assert d.state_shift.kind == "reopen"
    assert d.state_shift.legal is True


def test_state_shift_illegal_jump():
    prev = _artifact(as_of_date="2026-01-01", state=_belief({"S1": 0.9, "S3": 0.1}, argmax="S1", entropy=0.4))
    curr = _artifact(as_of_date="2026-01-05", state=_belief({"S1": 0.2, "S3": 0.8}, argmax="S3", entropy=0.4))
    d = diff(prev, curr)
    assert d.state_shift.legal is False  # S1→S3 跳级非法


def test_state_shift_same_argmax_but_mass_drift():
    # argmax 不变但质量漂移（S2:0.6→0.8 的"半只脚进 S3"）——argmax 相同不表示没变
    prev = _artifact(as_of_date="2026-01-01", state=_belief({"S2": 0.6, "S3": 0.4}, argmax="S2", entropy=0.7))
    curr = _artifact(as_of_date="2026-01-05", state=_belief({"S2": 0.8, "S3": 0.2}, argmax="S2", entropy=0.7))
    d = diff(prev, curr)
    ss = d.state_shift
    assert ss.argmax_from == ss.argmax_to == "S2"
    assert ss.kind == "same"
    assert ss.tv_distance > 0  # 质量漂移非零
    assert ss.mass_delta["S2"] == pytest.approx(0.2)


def test_state_shift_entropy_delta_sign():
    # 熵下降 = 变确定（负）；熵上升 = 变模糊（正）
    prev = _artifact(as_of_date="2026-01-01", state=_belief({"S2": 0.6, "S3": 0.4}, argmax="S2", entropy=1.2))
    curr = _artifact(as_of_date="2026-01-05", state=_belief({"S2": 0.9, "S3": 0.1}, argmax="S2", entropy=0.9))
    d = diff(prev, curr)
    assert d.state_shift.entropy_from == pytest.approx(1.2)
    assert d.state_shift.entropy_to == pytest.approx(0.9)
    assert d.state_shift.entropy_delta == pytest.approx(-0.3)  # 变确定


def test_state_shift_no_curr_state_returns_none():
    prev = _artifact(as_of_date="2026-01-01")
    curr = _artifact(as_of_date="2026-01-05")
    d = diff(prev, curr)
    assert d.state_shift is None


# ─────────────────────────────────────────────────────────────────────────────
# L3' 置信度差（步骤 5）
# ─────────────────────────────────────────────────────────────────────────────
def test_confidence_delta_and_log_odds():
    prev = _artifact(as_of_date="2026-01-01", thesis_confidence=0.58)
    curr = _artifact(as_of_date="2026-01-05", thesis_confidence=0.72)
    d = diff(prev, curr)
    assert d.confidence.prior == pytest.approx(0.58)
    assert d.confidence.posterior == pytest.approx(0.72)
    assert d.confidence.delta == pytest.approx(0.14)
    import math

    expected = math.log(0.72 / 0.28) - math.log(0.58 / 0.42)
    assert d.confidence.log_odds_delta == pytest.approx(expected)


def test_confidence_evidence_ids_are_new_window_ids():
    prev = _artifact(
        as_of_date="2026-01-01",
        evidence_log=[
            EvidenceEvent(
                id="e1",
                date="2026-01-01",
                kind="thesis",
                description="旧证据",
                effect_on_thesis="confirming",
                confidence_delta=0.1,
            )
        ],
    )
    curr = _artifact(
        as_of_date="2026-01-05",
        evidence_log=[
            EvidenceEvent(
                id="e1",
                date="2026-01-01",
                kind="thesis",
                description="旧证据",
                effect_on_thesis="confirming",
                confidence_delta=0.1,
            ),
            EvidenceEvent(
                id="e2",
                date="2026-01-03",
                kind="expectation",
                description="EPS 上修",
                effect_on_thesis="confirming",
                confidence_delta=0.2,
            ),
        ],
    )
    d = diff(prev, curr)
    assert d.confidence.evidence_ids == ["e2"]


def test_confidence_first_frame_none_delta():
    curr = _artifact(as_of_date="2026-01-01", thesis_confidence=0.7)
    d = diff(None, curr)
    assert d.confidence.prior is None
    assert d.confidence.posterior == pytest.approx(0.7)
    assert d.confidence.delta is None
    assert d.confidence.log_odds_delta is None
    assert d.confidence.evidence_ids == []


# ─────────────────────────────────────────────────────────────────────────────
# L4 赔率差（步骤 6）
# ─────────────────────────────────────────────────────────────────────────────
def test_ev_diff():
    prev = _artifact(
        as_of_date="2026-01-01",
        expected_value=_ev(12.0, 2.0, _scenarios(bull_p=0.3, bull_r=15.0), "state_prior"),
    )
    curr = _artifact(
        as_of_date="2026-01-05",
        expected_value=_ev(15.0, 2.5, _scenarios(bull_p=0.35, bull_r=20.0), "bayes_posterior"),
    )
    d = diff(prev, curr)
    evd = d.ev
    assert evd.ev_from == pytest.approx(12.0)
    assert evd.ev_to == pytest.approx(15.0)
    assert evd.ev_delta == pytest.approx(3.0)
    assert evd.risk_adjusted_ev_delta == pytest.approx(0.5)
    assert evd.scenario_probability_delta["bull"] == pytest.approx(0.05)
    assert evd.scenario_probability_delta["base"] == pytest.approx(0.0)
    assert evd.scenario_return_delta["bull"] == pytest.approx(5.0)
    assert evd.scenario_return_delta["base"] == pytest.approx(0.0)
    assert evd.probability_source_change == "state_prior → bayes_posterior"


def test_ev_diff_source_unchanged_is_empty():
    prev = _artifact(as_of_date="2026-01-01", expected_value=_ev(12.0, 2.0, _scenarios(), "bayes_posterior"))
    curr = _artifact(as_of_date="2026-01-05", expected_value=_ev(15.0, 2.5, _scenarios(), "bayes_posterior"))
    d = diff(prev, curr)
    assert d.ev.probability_source_change == ""


def test_ev_first_frame_baseline():
    curr = _artifact(as_of_date="2026-01-01", expected_value=_ev(15.0, 2.5, _scenarios(), "bayes_posterior"))
    d = diff(None, curr)
    evd = d.ev
    assert evd.ev_from is None
    assert evd.ev_to == pytest.approx(15.0)
    assert evd.ev_delta is None
    assert evd.risk_adjusted_ev_delta is None
    assert evd.scenario_probability_delta["bull"] == pytest.approx(0.4)  # 基线登记 = 当前值
    assert evd.probability_source_change == ""


def test_ev_none_when_curr_expected_value_missing():
    prev = _artifact(as_of_date="2026-01-01", expected_value=_ev(12.0, 2.0, _scenarios(), "bayes_posterior"))
    curr = _artifact(as_of_date="2026-01-05", expected_value=None)
    d = diff(prev, curr)
    assert d.ev is None
