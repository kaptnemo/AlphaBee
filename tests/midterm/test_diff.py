"""diff.py 引擎框架 + L1 因子差 + L2 评分差（D2-1）单测。

覆盖 design §4 步骤 0/1（校验 + 首帧基线登记）、步骤 2（L1 因子差，纯规则）、
步骤 3（L2 评分差）。纪律：数值核心纯规则禁 LLM；appeared/disappeared 与 up/down
严格区分；缺失显式 None。
"""

import pytest

from alphabee.midterm.diff import diff
from alphabee.midterm.models import (
    CompanyStateArtifact,
    CompanyStateDiff,
    FactorSnapshot,
    FieldChange,
    FundamentalFactor,
    TrendFactor,
    VariableScores,
)


def _artifact(
    symbol: str = "000977",
    as_of_date: str = "2026-01-01",
    schema_version: str = "1",
    snapshot: FactorSnapshot | None = None,
    scores: VariableScores | None = None,
) -> CompanyStateArtifact:
    return CompanyStateArtifact(
        schema_version=schema_version,
        symbol=symbol,
        as_of_date=as_of_date,
        factor_snapshot=snapshot,
        variable_scores=scores or VariableScores(),
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
