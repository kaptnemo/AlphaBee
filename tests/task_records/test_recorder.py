"""task_records：状态证据 V2 字段（落库 + to_summary + 旧记录回落，design §6.9）。

字段 additive：旧记录反序列化回落默认值；无 midterm artifact / 无顶背离 ⇒ 0.0，绝不抛。
**计算口径成对记录**：`midterm_classifier_order`（artifact `classifier_order`）+
`midterm_formula_revision`（artifact `classifier_formula` = 配置 `midterm.formula_revision`）。
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
    assert record.midterm_classifier_order == ""
    assert record.midterm_formula_revision == ""
    assert record.midterm_uncertain is False
    assert record.midterm_adverse_severity == 0.0
    summary = record.to_summary()
    assert summary["midterm_classifier_order"] == ""
    assert summary["midterm_formula_revision"] == ""
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
    # 口径成对：顺序 + 公式版本都要落库（只记其一无法定位分布来源）
    assert record.midterm_classifier_order == "tef"
    assert record.midterm_formula_revision == "v2"
    assert record.midterm_uncertain is True
    assert record.midterm_adverse_severity == pytest.approx(0.7626)
    summary = record.to_summary()
    assert summary["midterm_classifier_order"] == "tef"
    assert summary["midterm_formula_revision"] == "v2"
    assert summary["midterm_uncertain"] is True
    assert summary["midterm_adverse_severity"] == pytest.approx(0.7626)


def test_recorder_captures_etf_order():
    """顺序为 etf 时同样落库（不得因等于默认值而被省略/误判为空）。"""
    payload = _artifact(classifier_order="etf", classifier_formula="v1").model_dump(mode="json")
    record = _capture([{"type": "midterm_decision", "value": payload}])
    assert record.midterm_classifier_order == "etf"
    assert record.midterm_formula_revision == "v1"


def test_recorder_v1_frame_has_no_adverse_severity():
    payload = _artifact(classifier_order="tef", classifier_formula="v1", uncertain=True).model_dump(mode="json")
    record = _capture([{"type": "midterm_decision", "value": payload}])
    assert record.midterm_classifier_order == "tef"
    assert record.midterm_formula_revision == "v1"
    assert record.midterm_uncertain is True
    assert record.midterm_adverse_severity == 0.0  # 无 adverse ⇒ 0.0


def test_recorder_old_artifact_without_new_fields_falls_back():
    """旧 artifact dict 缺新字段 ⇒ 校验回落默认值（order "etf" / formula "v1" / uncertain False / 0.0）。

    ``classifier_order`` 是 TEF 设计期就有的字段（历史帧确由 ETF 产出），故旧帧回落 ``"etf"`` 是语义正确的；
    这里只剔除 V2 新增的三项，模拟升级前落盘的 artifact。
    """
    payload = _artifact().model_dump(mode="json")
    for field in ("classifier_formula", "uncertain", "adverse_divergence"):
        payload.pop(field, None)
    record = _capture([{"type": "midterm_decision", "value": payload}])
    assert record.midterm_classifier_order == "etf"
    assert record.midterm_formula_revision == "v1"
    assert record.midterm_uncertain is False
    assert record.midterm_adverse_severity == 0.0


def test_recorder_without_midterm_artifact_keeps_defaults():
    record = _capture([])
    assert record.midterm_classifier_order == ""
    assert record.midterm_formula_revision == ""
    assert record.midterm_uncertain is False
    assert record.midterm_adverse_severity == 0.0


def test_record_migrates_legacy_formula_field():
    """改名兼容：旧落库 JSON 的 ``midterm_formula`` 在读取时搬迁为 ``midterm_formula_revision``。

    改名前的记录（``data/task_records/**/*.json``）键名是 ``midterm_formula``；Pydantic 默认
    ``extra="ignore"`` 会静默丢键 ⇒ 公式版本丢失。before-validator 做只读搬迁，写侧只写新名。
    """
    legacy = TaskRecord.model_validate({"task_id": "task-old", "query": "q", "midterm_formula": "v2"})
    assert legacy.midterm_formula_revision == "v2"
    assert legacy.to_summary()["midterm_formula_revision"] == "v2"
    assert "midterm_formula" not in legacy.model_dump(mode="json")  # 写侧不再产出旧名

    # 两个键同时存在（不应发生）⇒ 新名优先，不静默覆盖
    both = TaskRecord.model_validate(
        {"task_id": "task-both", "query": "q", "midterm_formula": "v1", "midterm_formula_revision": "v2"}
    )
    assert both.midterm_formula_revision == "v2"
