"""task_records：状态证据 V2 三字段（落库 + to_summary + 旧记录回落，design §6.9）。

三字段 additive：旧记录反序列化回落默认值；无 midterm artifact / 无顶背离 ⇒ 0.0，绝不抛。
"""

from __future__ import annotations

import pytest

from alphabee.midterm.models import AdverseDivergence, CompanyStateArtifact, StateBelief
from alphabee.task_records.models import TaskRecord
from alphabee.task_records.recorder import TaskRecorder


def _artifact(**overrides) -> CompanyStateArtifact:
    base = CompanyStateArtifact(
        symbol="600760.SH",
        as_of_date="2026-10-30",
        state=StateBelief(distribution={"S5": 0.795}, argmax_state="S5", entropy=0.7993),
        thesis="H",
        thesis_confidence=0.5,
    )
    return base.model_copy(update=overrides)


def _capture(artifacts: list[dict]) -> TaskRecord:
    return TaskRecorder().capture(
        query="分析 600760",
        symbol="600760.SH",
        flags={"enhance": False, "llm_review": False, "midterm": True},
        payload={"final_report": {"title": "报告"}, "issues": []},
        artifacts=artifacts,
        start_ts=0.0,
    )


def test_record_fields_are_additive_with_defaults():
    record = TaskRecord(task_id="task-x", query="q")
    assert record.midterm_formula == ""
    assert record.midterm_uncertain is False
    assert record.midterm_adverse_severity == 0.0
    summary = record.to_summary()
    assert summary["midterm_formula"] == ""
    assert summary["midterm_uncertain"] is False
    assert summary["midterm_adverse_severity"] == 0.0


def test_recorder_captures_v2_fields_from_artifact():
    payload = _artifact(
        classifier_formula="v2",
        classifier_order="tef",
        entropy_normalized=0.4461,
        uncertain=True,
        factor_consistency="divergent",
        divergence_reason="基本面与预期同时实质恶化而价格仍强（顶背离）",
        adverse_divergence=AdverseDivergence(severity=0.7626),
        state_evidence={"S5": 0.7626},
        factor_direction=-0.5826,
    ).model_dump(mode="json")

    record = _capture([{"type": "midterm_decision", "value": payload}])
    assert record.midterm_formula == "v2"
    assert record.midterm_uncertain is True
    assert record.midterm_adverse_severity == pytest.approx(0.7626)
    summary = record.to_summary()
    assert summary["midterm_formula"] == "v2"
    assert summary["midterm_uncertain"] is True
    assert summary["midterm_adverse_severity"] == pytest.approx(0.7626)


def test_recorder_v1_frame_has_no_adverse_severity():
    payload = _artifact(classifier_formula="v1", uncertain=True).model_dump(mode="json")
    record = _capture([{"type": "midterm_decision", "value": payload}])
    assert record.midterm_formula == "v1"
    assert record.midterm_uncertain is True
    assert record.midterm_adverse_severity == 0.0  # 无 adverse ⇒ 0.0


def test_recorder_old_artifact_without_new_fields_falls_back():
    """旧 artifact dict 缺 8 个新字段 ⇒ 校验回落默认值（formula "v1" / uncertain False / 0.0）。"""
    payload = _artifact().model_dump(mode="json")
    for field in ("classifier_formula", "uncertain", "adverse_divergence"):
        payload.pop(field, None)
    record = _capture([{"type": "midterm_decision", "value": payload}])
    assert record.midterm_formula == "v1"
    assert record.midterm_uncertain is False
    assert record.midterm_adverse_severity == 0.0


def test_recorder_without_midterm_artifact_keeps_defaults():
    record = _capture([])
    assert record.midterm_formula == ""
    assert record.midterm_uncertain is False
    assert record.midterm_adverse_severity == 0.0
