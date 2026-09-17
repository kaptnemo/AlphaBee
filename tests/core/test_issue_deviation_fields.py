"""F0a 契约测试：``DeviationClass`` 枚举 + ``Issue`` 追加 5 字段（§14.7 / §15.10）。

覆盖：
- 新字段默认值，且 ``amplified_by`` 默认 ``[]`` 而非 ``None``；
- 旧版本落盘的 ``Issue`` JSON（无 5 个新字段）可直接 ``model_validate`` 取默认值，
  无需迁移脚本；
- 只 append：实施前的字段名与顺序保持不变（append-only 断言）。
"""

from __future__ import annotations

import json
from typing import Any

from alphabee.core.schemas import (
    DeviationClass,
    Issue,
    IssueScope,
    IssueSeverity,
    IssueStatus,
)

# 实施前（commit c828704）持久化的 Issue JSON 形状：10 个旧字段，无 5 个新字段。
# 用真实任务记录里出现过的 category / severity 取值（outputs/task_records/*.json）。
_LEGACY_ISSUE_JSON: dict[str, Any] = {
    "id": "issue-0123456789ab",
    "severity": "high",
    "category": "conflict",
    "message": "[冲突] 估值严重溢价与基本面全面恶化之间的极端背离",
    "related_step": "explore_conflicts",
    "related_artifact": "artifact-abcdef123456",
    "status": "open",
    "owner_node": "reporter",
    "resolution_evidence": None,
    "scope": "report",
}

# append-only 基线：旧字段序（顺序与名字都不得变），新字段必须追加在尾部。
_LEGACY_FIELD_ORDER: tuple[str, ...] = (
    "id",
    "severity",
    "category",
    "message",
    "related_step",
    "related_artifact",
    "status",
    "owner_node",
    "resolution_evidence",
    "scope",
)
_NEW_FIELDS: tuple[str, ...] = (
    "deviation_class",
    "detected_at_step",
    "recovery_action",
    "recovery_cost",
    "amplified_by",
)


def _minimal_issue(**overrides: Any) -> Issue:
    payload: dict[str, Any] = {
        "id": "issue-minimal",
        "severity": IssueSeverity.LOW,
        "category": "missing_data",
        "message": "m",
    }
    payload.update(overrides)
    return Issue(**payload)


# ── DeviationClass ───────────────────────────────────────────────────────────


def test_deviation_class_members_are_d1_to_d5():
    assert {member.name: member.value for member in DeviationClass} == {
        "D1_DATA": "d1_data",
        "D2_STRUCTURE": "d2_structure",
        "D3_ARGUMENT": "d3_argument",
        "D4_STATE": "d4_state",
        "D5_CONTROL": "d5_control",
    }


def test_deviation_class_is_exported_from_core_package():
    """`alphabee.core` 的公共 API 与 schemas 对齐：`from alphabee.core import DeviationClass` 必须可用。"""
    import alphabee.core as core_package

    assert core_package.DeviationClass is DeviationClass
    assert "DeviationClass" in core_package.__all__


def test_deviation_class_is_str_enum_round_trippable():
    assert isinstance(DeviationClass.D1_DATA, str)
    assert str(DeviationClass.D1_DATA) == "d1_data"
    assert DeviationClass("d5_control") is DeviationClass.D5_CONTROL
    # 落盘后（JSON 值）可直接反序列化回枚举成员
    assert _minimal_issue(deviation_class="d3_argument").deviation_class is DeviationClass.D3_ARGUMENT


# ── 字段追加与默认值 ──────────────────────────────────────────────────────────


def test_issue_new_fields_are_appended_after_legacy_fields():
    field_names = tuple(Issue.model_fields)
    assert field_names[: len(_LEGACY_FIELD_ORDER)] == _LEGACY_FIELD_ORDER
    assert field_names[len(_LEGACY_FIELD_ORDER) :] == _NEW_FIELDS


def test_issue_new_fields_default_values():
    issue = _minimal_issue()
    assert issue.deviation_class is None
    assert issue.detected_at_step is None
    assert issue.recovery_action is None
    assert issue.recovery_cost is None
    # §14.1-A：amplified_by 用 default_factory=list，默认空列表而非 None
    assert issue.amplified_by == []


def test_amplified_by_default_is_fresh_list_per_instance():
    first = _minimal_issue()
    second = _minimal_issue()
    first.amplified_by.append("insight_to_thesis")
    assert second.amplified_by == []
    assert first.amplified_by is not second.amplified_by


def test_issue_new_fields_accept_explicit_values():
    issue = _minimal_issue(
        deviation_class=DeviationClass.D3_ARGUMENT,
        detected_at_step="review_thesis",
        recovery_action="rerun_round=1",
        recovery_cost=1,
        amplified_by=["insight_to_thesis", "thesis_to_report"],
    )
    assert issue.deviation_class is DeviationClass.D3_ARGUMENT
    assert issue.detected_at_step == "review_thesis"
    assert issue.recovery_action == "rerun_round=1"
    assert issue.recovery_cost == 1
    assert issue.amplified_by == ["insight_to_thesis", "thesis_to_report"]
    # recovery_cost=0 表示"未恢复"，必须与 None（未尝试）区分
    assert _minimal_issue(recovery_cost=0).recovery_cost == 0


# ── 旧 JSON 向后兼容 ─────────────────────────────────────────────────────────


def test_legacy_issue_json_validates_and_takes_defaults():
    issue = Issue.model_validate(_LEGACY_ISSUE_JSON)

    assert issue.id == "issue-0123456789ab"
    assert issue.severity is IssueSeverity.HIGH
    assert issue.category == "conflict"
    assert issue.related_step == "explore_conflicts"
    assert issue.related_artifact == "artifact-abcdef123456"
    assert issue.status is IssueStatus.OPEN
    assert issue.owner_node == "reporter"
    assert issue.resolution_evidence is None
    assert issue.scope is IssueScope.REPORT

    assert issue.deviation_class is None
    assert issue.detected_at_step is None
    assert issue.recovery_action is None
    assert issue.recovery_cost is None
    assert issue.amplified_by == []


def test_legacy_issue_json_string_validates():
    """落盘的 JSON 文本（无新字段）直接反序列化，不需要迁移脚本。"""
    issue = Issue.model_validate_json(json.dumps(_LEGACY_ISSUE_JSON, ensure_ascii=False))
    assert issue.deviation_class is None
    assert issue.amplified_by == []


def test_legacy_issue_dump_keeps_legacy_values():
    dumped = Issue.model_validate(_LEGACY_ISSUE_JSON).model_dump()
    for key, value in _LEGACY_ISSUE_JSON.items():
        assert dumped[key] == value, f"旧字段 {key} 的语义/取值不得变化"
    assert set(dumped) == set(_LEGACY_FIELD_ORDER) | set(_NEW_FIELDS)


def test_issue_round_trips_with_new_fields():
    issue = _minimal_issue(
        deviation_class=DeviationClass.D1_DATA,
        detected_at_step="run_analysis_engines",
        recovery_action="degraded_tier=2",
        recovery_cost=2,
        amplified_by=["thesis_to_report"],
    )
    restored = Issue.model_validate_json(issue.model_dump_json())
    assert restored == issue


# ── JSON Schema 契约 ────────────────────────────────────────────────────────


def test_issue_json_schema_declares_new_fields_as_optional():
    schema = Issue.model_json_schema()
    properties = schema["properties"]

    assert set(_NEW_FIELDS) <= set(properties)
    # 新字段全部可选：只有 4 个旧必填字段留在 required 里
    assert sorted(schema["required"]) == ["category", "id", "message", "severity"]

    amplified = properties["amplified_by"]
    assert amplified["type"] == "array"
    assert amplified["items"] == {"type": "string"}
    assert Issue.model_fields["amplified_by"].default_factory is list  # [] 而非 None

    assert properties["deviation_class"]["default"] is None
    assert properties["recovery_cost"]["default"] is None
    # 枚举成员值域进入 schema（下游契约可见）
    assert schema["$defs"]["DeviationClass"]["enum"] == [member.value for member in DeviationClass]
