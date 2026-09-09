"""classifier.py 的单元测试：§4.2 模式→状态、E>T>F、软状态/熵、迁移合法性、因子一致性。

全部用合成 ``VariableScores``，不发起网络请求，确定性验证。
"""

from __future__ import annotations

import pytest

from alphabee.midterm.classifier import classify_state
from alphabee.midterm.models import Consistency, StateBelief, VariableScores


def _scores(f=None, e=None, t=None, v=None, c=None, r=None) -> VariableScores:
    return VariableScores(
        f_fundamental_trend=f,
        e_revision=e,
        t_relative_strength=t,
        v_valuation_percentile=v,
        c_crowding=c,
        r_risk=r,
    )


def _belief(argmax: str, distribution: dict[str, float] | None = None) -> StateBelief:
    return StateBelief(
        distribution=distribution or {argmax: 1.0},
        argmax_state=argmax,
        entropy=0.0,
    )


# ─────────────────────────────────────────────────────────────────────────────
# §4.2 因子模式 → 状态（argmax）
# ─────────────────────────────────────────────────────────────────────────────


def test_s3_resonance():
    r = classify_state(_scores(f=0.8, e=0.8, t=0.8))
    assert r.state.argmax_state == "S3"


def test_s2_confirmation():
    r = classify_state(_scores(f=0.8, e=0.8, t=-0.5, v=0.5))
    assert r.state.argmax_state == "S2"


def test_s1_expectation_gap():
    r = classify_state(_scores(f=0.6, e=-0.5, t=-0.5, v=0.7))
    assert r.state.argmax_state == "S1"


def test_s4_priced_in():
    r = classify_state(_scores(f=0.1, e=-0.3, t=-0.6, v=-0.6, c=-0.6))
    assert r.state.argmax_state == "S4"


def test_s5_exit():
    r = classify_state(_scores(e=-0.8, f=-0.5))
    assert r.state.argmax_state == "S5"


def test_e_over_t_over_f_top_ordering():
    # F 仍强（0.8）但 E 转负、T 破坏、V 贵、C 拥挤 → S4，而非因 F 强误判 S3（§4.3 E>T>F）
    r = classify_state(_scores(f=0.8, e=-0.5, t=-0.5, v=-0.5, c=-0.5))
    assert r.state.argmax_state == "S4"


# ─────────────────────────────────────────────────────────────────────────────
# 软状态（§2b）
# ─────────────────────────────────────────────────────────────────────────────


def test_distribution_sums_to_one():
    r = classify_state(_scores(f=0.8, e=0.8, t=0.8))
    assert sum(r.state.distribution.values()) == pytest.approx(1.0)


def test_distribution_is_probability():
    r = classify_state(_scores(f=0.8, e=0.8, t=0.8))
    for p in r.state.distribution.values():
        assert 0.0 <= p <= 1.0


def test_entropy_is_finite_nonnegative():
    r = classify_state(_scores(f=0.8, e=0.8, t=0.8))
    assert r.state.entropy >= 0.0


def test_no_signal_uncertain_high_entropy():
    # 全部缺失 → 不硬贴标签，熵高 → uncertain + 研究任务
    r = classify_state(_scores())
    assert r.uncertain is True
    assert r.state.entropy > 1.0
    assert len(r.research_tasks) > 0


def test_only_revision_points_s2_but_uncertain():
    # 仅 E 上修：方向指向 S2，但 F/T 缺失导致熵高 → uncertain（不制造虚假证据）
    r = classify_state(_scores(e=0.8))
    assert r.state.argmax_state == "S2"
    assert r.uncertain is True


def test_drift_none_without_previous():
    r = classify_state(_scores(f=0.8, e=0.8, t=0.8))
    assert r.state.drift is None


def test_drift_computed_with_previous():
    prev = _belief("S2", {"S1": 0.1, "S2": 0.7, "S3": 0.2, "S0": 0.0, "S4": 0.0, "S5": 0.0})
    r = classify_state(_scores(f=0.8, e=0.8, t=0.8), previous=prev)
    assert r.state.drift is not None
    assert "S3" in r.state.drift  # 三共振 → S3 概率质量流入


# ─────────────────────────────────────────────────────────────────────────────
# 共振 / 背离（§4.4）
# ─────────────────────────────────────────────────────────────────────────────


def test_resonant_consistency():
    r = classify_state(_scores(f=0.8, e=0.8, t=0.8))
    deltas = {d.factor: d for d in r.factor_deltas}
    assert deltas["F"].consistency == Consistency.RESONANT
    assert deltas["E"].consistency == Consistency.RESONANT
    assert deltas["T"].consistency == Consistency.RESONANT


def test_divergent_triggers_uncertain_and_research():
    # F 强但 E/T 弱（业绩好股价不涨）→ divergent → uncertain + 研究任务
    r = classify_state(_scores(f=0.8, e=-0.5, t=-0.5))
    deltas = {d.factor: d for d in r.factor_deltas}
    assert deltas["F"].consistency == Consistency.DIVERGENT
    assert r.uncertain is True
    assert any(t.id == "research-divergent" for t in r.research_tasks)


def test_e_up_t_down_named_divergent_conflict():
    """E 强上修 + T 走弱 → 具名 conflict（业绩上修 vs 股价走弱），consistency=divergent。"""
    r = classify_state(_scores(f=0.0, e=0.8, t=-0.5))
    deltas = {d.factor: d for d in r.factor_deltas}
    assert deltas["E"].consistency == Consistency.DIVERGENT
    assert deltas["T"].consistency == Consistency.DIVERGENT
    assert r.uncertain is True
    # 具名 conflict 进入 rationale（不再只是「熵高」一句笼统话）
    assert any("业绩上修" in line and "股价" in line for line in r.rationale)
    # 具名 conflict 进入研究触发，供下游 explore_conflicts 消费
    div_tasks = [t for t in r.research_tasks if t.id == "research-divergent"]
    assert div_tasks
    assert any("业绩上修" in t.unknown and "股价" in t.unknown for t in div_tasks)


def test_e_up_t_down_does_not_mislabel_resonant():
    """E↑+T↓ 不应被误判为共振（S3），而是 divergent。"""
    r = classify_state(_scores(f=0.8, e=0.8, t=-0.5))
    assert r.state.argmax_state != "S3"
    deltas = {d.factor: d for d in r.factor_deltas}
    assert deltas["E"].consistency == Consistency.DIVERGENT


def test_independent_consistency_when_missing():
    # 任一因子缺失 → 无法判定共振/背离 → independent
    r = classify_state(_scores(f=0.8, e=0.8))
    deltas = {d.factor: d for d in r.factor_deltas}
    assert deltas["F"].consistency == Consistency.INDEPENDENT


def test_v_c_r_consistency_independent():
    r = classify_state(_scores(f=0.8, e=0.8, t=0.8))
    deltas = {d.factor: d for d in r.factor_deltas}
    assert deltas["V"].consistency == Consistency.INDEPENDENT
    assert deltas["C"].consistency == Consistency.INDEPENDENT
    assert deltas["R"].consistency == Consistency.INDEPENDENT


def test_factor_deltas_cover_six_factors():
    r = classify_state(_scores(f=0.8, e=0.8, t=0.8))
    assert {d.factor for d in r.factor_deltas} == {"F", "E", "T", "V", "C", "R"}


# ─────────────────────────────────────────────────────────────────────────────
# 迁移合法性（§4.5）
# ─────────────────────────────────────────────────────────────────────────────


def test_transition_none_without_previous():
    r = classify_state(_scores(f=0.8, e=0.8, t=0.8))
    assert r.transition is None


def test_transition_forward_legal():
    prev = _belief("S2")
    r = classify_state(_scores(f=0.8, e=0.8, t=0.8), previous=prev)
    assert r.transition is not None
    assert r.transition.legal is True
    assert r.transition.kind == "forward"
    assert (r.transition.from_state, r.transition.to_state) == ("S2", "S3")


def test_transition_backward_legal():
    prev = _belief("S3")
    r = classify_state(_scores(f=0.8, e=0.8, t=-0.5, v=0.5), previous=prev)
    assert r.transition.legal is True
    assert r.transition.kind == "downgrade"
    assert (r.transition.from_state, r.transition.to_state) == ("S3", "S2")


def test_transition_illegal_jump():
    prev = _belief("S1")
    r = classify_state(_scores(f=0.8, e=0.8, t=0.8), previous=prev)  # S1 → S3 跳级
    assert r.transition.legal is False
    assert r.transition.kind == "illegal"


def test_transition_reopen():
    prev = _belief("S3")
    r = classify_state(_scores(f=0.6, e=-0.5, t=-0.5, v=0.7), previous=prev)  # S3 → S1'
    assert r.transition.kind == "reopen"
    assert r.transition.legal is True


# ─────────────────────────────────────────────────────────────────────────────
# 纯函数 / 确定性
# ─────────────────────────────────────────────────────────────────────────────


def test_deterministic():
    s = _scores(f=0.8, e=0.8, t=0.8)
    assert classify_state(s).state.model_dump() == classify_state(s).state.model_dump()
