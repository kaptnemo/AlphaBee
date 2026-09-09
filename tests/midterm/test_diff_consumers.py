"""diff_consumers.py 消费方接线单测（D4）。

覆盖 check_exit / project_journal / project_narrative / monitor_triggers /
anchor_diff，验证 CompanyStateDiff 被下游真正读取（无 dead-end）。
"""

import pytest

from alphabee.midterm.diff_consumers import (
    anchor_diff,
    check_exit,
    monitor_triggers,
    project_journal,
    project_narrative,
)
from alphabee.midterm.models import (
    ArtifactRef,
    ChangeAttribution,
    CompanyStateArtifact,
    CompanyStateDiff,
    ConfidenceDelta,
    PositionDiff,
    StateShift,
)


def _diff(
    *,
    symbol: str = "000977",
    date: str = "2026-01-05",
    elapsed_days: int = 4,
    exit_conditions_met: list[str] | None = None,
    state_shift: StateShift | None = None,
    position: PositionDiff | None = None,
    confidence: ConfidenceDelta | None = None,
    new_evidence_count: int = 0,
    thesis_delta: str = "",
    attribution: list[ChangeAttribution] | None = None,
) -> CompanyStateDiff:
    from alphabee.midterm.models import EvidenceEvent

    evidence = [
        EvidenceEvent(
            id=f"e{i}", date=date, kind="thesis", description="x", effect_on_thesis="confirming", confidence_delta=0.1
        )
        for i in range(new_evidence_count)
    ]
    return CompanyStateDiff(
        symbol=symbol,
        prev=None,
        curr=ArtifactRef(id=f"{symbol}:{date}", date=date),
        elapsed_days=elapsed_days,
        exit_conditions_met=exit_conditions_met or [],
        state_shift=state_shift,
        position=position,
        confidence=confidence,
        new_evidence=evidence,
        thesis_delta=thesis_delta,
        attribution=attribution or [],
    )


# ─────────────────────────────────────────────────────────────────────────────
# check_exit（ExitEngine 检查投影）
# ─────────────────────────────────────────────────────────────────────────────
def test_check_exit_exit_conditions():
    d = _diff(exit_conditions_met=["revision_stop"])
    sig = check_exit(d)
    assert sig.should_exit is True
    assert any("revision_stop" in r for r in sig.reasons)


def test_check_exit_downgrade():
    d = _diff(
        state_shift=StateShift(argmax_from="S3", argmax_to="S2", tv_distance=0.3, entropy_to=1.0, kind="downgrade")
    )
    sig = check_exit(d)
    assert sig.should_exit is True
    assert any("S3→S2" in r for r in sig.reasons)


def test_check_exit_band_divergence():
    d = _diff(position=PositionDiff(band_weight_divergence=True, band_from="核心", band_to="核心"))
    sig = check_exit(d)
    assert sig.should_exit is True
    assert any("背离" in r for r in sig.reasons)


def test_check_exit_no_signal():
    d = _diff()
    sig = check_exit(d)
    assert sig.should_exit is False
    assert sig.reasons == []


# ─────────────────────────────────────────────────────────────────────────────
# project_journal（决策日志投影）
# ─────────────────────────────────────────────────────────────────────────────
def test_project_journal():
    att = ChangeAttribution(
        evidence_ids=["e1"], factor_deltas=["E"], decision_effects=["state:S1→S2"], note="E 上修驱动"
    )
    d = _diff(
        thesis_delta="E 上修驱动",
        attribution=[att],
        confidence=ConfidenceDelta(prior=0.58, posterior=0.72, delta=0.14, log_odds_delta=0.62, evidence_ids=["e1"]),
    )
    entry = project_journal(d)
    assert entry.symbol == "000977"
    assert entry.date == "2026-01-05"
    assert entry.action == "hold"  # 无退出信号 → hold
    assert entry.thesis_at_time == "E 上修驱动"
    assert entry.evidence_refs == ["e1"]
    assert entry.confidence_at_time == pytest.approx(0.72)


def test_project_journal_action_reduce_on_downgrade():
    d = _diff(
        state_shift=StateShift(argmax_from="S3", argmax_to="S2", tv_distance=0.3, entropy_to=1.0, kind="downgrade")
    )
    entry = project_journal(d)
    assert entry.action == "reduce"


def test_project_journal_action_sell_on_exit():
    d = _diff(exit_conditions_met=["thesis_broken"])
    entry = project_journal(d)
    assert entry.action == "sell"


# ─────────────────────────────────────────────────────────────────────────────
# project_narrative（报告叙事投影）
# ─────────────────────────────────────────────────────────────────────────────
def test_project_narrative():
    d = _diff(
        thesis_delta="Q2 收入与 EPS 上修驱动状态迁移",
        state_shift=StateShift(argmax_from="S1", argmax_to="S2", tv_distance=0.2, entropy_to=1.0, kind="upgrade"),
    )
    text = project_narrative(d)
    assert "000977" in text
    assert "S1→S2" in text
    assert "Q2 收入与 EPS 上修" in text


def test_project_narrative_no_change():
    d = _diff()
    text = project_narrative(d)
    assert "无显著变化" in text


# ─────────────────────────────────────────────────────────────────────────────
# monitor_triggers（监控触发器）
# ─────────────────────────────────────────────────────────────────────────────
def test_monitor_triggers_tv_distance():
    d = _diff(state_shift=StateShift(argmax_from="S1", argmax_to="S2", tv_distance=0.5, entropy_to=1.0))
    trig = monitor_triggers(d)
    assert trig.triggered is True
    assert any("tv_distance" in t for t in trig.triggers)


def test_monitor_triggers_evidence_rate():
    # elapsed_days=4，new_evidence=4 → rate=1.0 > 0.5
    d = _diff(elapsed_days=4, new_evidence_count=4)
    trig = monitor_triggers(d)
    assert trig.triggered is True
    assert any("evidence_arrival_rate" in t for t in trig.triggers)


def test_monitor_triggers_none():
    d = _diff(elapsed_days=4, new_evidence_count=0)
    trig = monitor_triggers(d)
    assert trig.triggered is False
    assert trig.triggers == []


def test_monitor_triggers_no_division_by_zero():
    # 首帧 elapsed_days=0 → 不计算 evidence_arrival_rate（除零保护）
    d = _diff(elapsed_days=0, new_evidence_count=10)
    trig = monitor_triggers(d)
    assert trig.triggered is False


# ─────────────────────────────────────────────────────────────────────────────
# anchor_diff（锚点 diff 便捷入口）
# ─────────────────────────────────────────────────────────────────────────────
def test_anchor_diff_reuses_diff_engine():
    entry = CompanyStateArtifact(schema_version="1", symbol="000977", as_of_date="2026-01-01")
    curr = CompanyStateArtifact(schema_version="1", symbol="000977", as_of_date="2026-01-10")
    d = anchor_diff(entry, curr)
    assert d.prev.id == "000977:2026-01-01"
    assert d.curr.id == "000977:2026-01-10"
    assert d.anchor is not None
    assert d.anchor.id == "000977:2026-01-01"  # anchor 引用记入建仓帧
    assert d.elapsed_days == 9
