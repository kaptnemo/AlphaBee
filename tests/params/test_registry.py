"""``params/registry.yaml`` v0 合法性 + 三方常量一致性（P0；设计 §4.3/§4.4/§4.6.2/附录 B）。

覆盖：

- 恰好 15 条、id 唯一且小写点分、kind/status/unit/domain/bounds/组约束合法；
- **design 表逐条对照**：值/单位/状态/bounds/wired_in/复核条件与附录 B 一致；
- **三方一致**：registry 值 == dataclass 默认值 == 生产模块常量（防"改一处漏一处"漂移）；
- 非法注册表（重复 id / kind=structure / 非数值 / 越界 / 组约束不满足 / 缺字段 /
  wired_in 字段不存在 / 坏 YAML / 文件缺失）必须抛明确的 ``ParamRegistryError``。

全部离线、纯函数；只读真实 registry + 临时目录。
"""

from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from alphabee.midterm import classifier_v2, evidence_rules, score_engine
from alphabee.params import (
    DEFAULT_EVIDENCE_PARAMS,
    DEFAULT_REGISTRY_PATH,
    DEFAULT_REPORT_QUALITY_PARAMS,
    DEFAULT_SCORE_PARAMS,
    ParamRegistryError,
    load_registry,
    parse_wired_in,
    validate_entries,
)
from alphabee.params.models import EvidenceParams, ReportQualityParams, ScoreParams

#: 设计 §4.4 / 附录 B 的 v0 清单（逐条钉住：值/单位/状态/bounds/wired_in/复核条件）。
DESIGN_TABLE: dict[str, dict[str, Any]] = {
    "midterm.score.fundamental_yoy_scale": {
        "value": 30.0,
        "unit": "pp",
        "status": "placeholder",
        "bounds": (10.0, 60.0),
        "wired_in": "ScoreParams.fundamental_yoy_scale",
        "review_condition": "真实帧 >= 50 且饱和率统计完成",
    },
    "midterm.score.ocf_negative_penalty": {
        "value": 0.5,
        "unit": "score",
        "status": "placeholder",
        "bounds": (0.0, 1.0),
        "wired_in": "ScoreParams.ocf_negative_penalty",
        "review_condition": "真实帧 >= 50 且 OCF 分组收益统计完成",
    },
    "midterm.score.beat_bonus": {
        "value": 0.2,
        "unit": "score",
        "status": "placeholder",
        "bounds": (0.0, 0.5),
        "wired_in": "ScoreParams.beat_bonus",
        "review_condition": "真实帧 >= 50 且 beat/miss 可结算",
    },
    "midterm.score.miss_penalty": {
        "value": 0.3,
        "unit": "score",
        "status": "placeholder",
        "bounds": (0.0, 0.5),
        "wired_in": "ScoreParams.miss_penalty",
        "review_condition": "真实帧 >= 50 且 beat/miss 可结算",
    },
    "midterm.score.miss_gap_threshold": {
        "value": 20.0,
        "unit": "pp",
        "status": "placeholder",
        "bounds": (5.0, 50.0),
        "wired_in": "ScoreParams.miss_gap_threshold",
        "review_condition": "真实帧 >= 50 且 gap 分布统计完成",
    },
    "midterm.score.segment_divergence_threshold": {
        "value": 15.0,
        "unit": "pp",
        "status": "placeholder",
        "bounds": (5.0, 50.0),
        "wired_in": "ScoreParams.segment_divergence_threshold",
        "review_condition": "真实帧 >= 50 且分部样本足够",
    },
    "midterm.score.segment_divergence_bonus": {
        "value": 0.2,
        "unit": "score",
        "status": "placeholder",
        "bounds": (0.0, 0.5),
        "wired_in": "ScoreParams.segment_divergence_bonus",
        "review_condition": "真实帧 >= 50 且分部样本足够",
    },
    "midterm.score.f_marginal_weight": {
        "value": 0.30,
        "unit": "ratio",
        "status": "frozen",
        "bounds": (0.0, 0.4),
        "wired_in": "ScoreParams.f_marginal_weight",
        "review_condition": "F 边际开权 G-8 全绿（真实帧 >= 50 + 四项翻转门槛）",
    },
    "midterm.score.anchor_weight": {
        "value": 0.6,
        "unit": "ratio",
        "status": "active",
        "bounds": (0.0, 1.0),
        "wired_in": "ScoreParams.anchor_weight",
        "review_condition": "与 research_weight 组约束复核",
    },
    "midterm.score.research_weight": {
        "value": 0.4,
        "unit": "ratio",
        "status": "active",
        "bounds": (0.0, 1.0),
        "wired_in": "ScoreParams.research_weight",
        "review_condition": "与 anchor_weight 组约束复核",
    },
    "midterm.data.min_consensus_coverage": {
        "value": 3,
        "unit": "count",
        "status": "placeholder",
        "bounds": (1, 10),
        "wired_in": "ScoreParams.min_consensus_coverage + build_numeric_evidence(min_consensus_coverage=)",
        "review_condition": "真实帧 >= 50 且 coverage 分布统计完成",
    },
    "midterm.evidence.temperature": {
        "value": 0.22,
        "unit": "nats",
        "status": "active",
        "bounds": (0.05, 1.0),
        "wired_in": "EvidenceParams.temperature",
        "review_condition": "真实帧 >= 50 后复核语料分位数门",
    },
    "midterm.evidence.uncertain_p_top": {
        "value": 0.55,
        "unit": "ratio",
        "status": "placeholder",
        "bounds": (0.3, 0.9),
        "wired_in": "EvidenceParams.uncertain_p_top",
        "review_condition": "真实帧 >= 50 后复核",
    },
    "midterm.evidence.break_lambda": {
        "value": 2.0,
        "unit": "score",
        "status": "active",
        "bounds": (1.0, 3.0),
        "wired_in": "EvidenceParams.break_lambda",
        "review_condition": "关键格比例复核（两序均 >= 90%）",
    },
    "midterm.evidence.s0_floor": {
        "value": 0.10,
        "unit": "score",
        "status": "active",
        "bounds": (0.0, 0.3),
        "wired_in": "EvidenceParams.s0_floor",
        "review_condition": "偏多帧 S0 占比与无信号帧 P(S0) 复核",
    },
}

_PARAMS_CLASSES = {
    "ScoreParams": (ScoreParams, DEFAULT_SCORE_PARAMS),
    "EvidenceParams": (EvidenceParams, DEFAULT_EVIDENCE_PARAMS),
    "ReportQualityParams": (ReportQualityParams, DEFAULT_REPORT_QUALITY_PARAMS),
}


@pytest.fixture(scope="module")
def entries() -> list[Any]:
    return load_registry()


def _registry_dict() -> dict[str, Any]:
    return yaml.safe_load(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))


def _write_registry(tmp_path: Path, mutate) -> Path:
    data = copy.deepcopy(_registry_dict())
    mutate(data)
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _entry_by_id(data: dict[str, Any], param_id: str) -> dict[str, Any]:
    return next(entry for entry in data["entries"] if entry["id"] == param_id)


# ── 1. v0 清单与元数据合法性 ──────────────────────────────────────────────


def test_registry_has_exactly_15_design_entries(entries) -> None:
    ids = [entry.id for entry in entries]
    assert len(entries) == 15
    assert ids == list(DESIGN_TABLE)  # 顺序也与附录 B 一致
    assert len(set(ids)) == 15


def test_registry_ids_are_unique_and_lowercase_dotted(entries) -> None:
    import re

    pattern = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){2,}$")
    for entry in entries:
        assert pattern.match(entry.id), entry.id
        assert entry.id.split(".")[1] in {"score", "data", "evidence"}


def test_registry_entries_match_design_table(entries) -> None:
    for entry in entries:
        expected = DESIGN_TABLE[entry.id]
        assert entry.value == expected["value"], entry.id
        assert entry.unit == expected["unit"], entry.id
        assert entry.status == expected["status"], entry.id
        assert tuple(entry.bounds) == expected["bounds"], entry.id
        assert entry.wired_in == expected["wired_in"], entry.id
        assert entry.review_condition == expected["review_condition"], entry.id
        # 附录 B 的公共字段
        assert entry.domain == "midterm", entry.id
        assert entry.kind == "policy", entry.id
        assert entry.version == 1 and entry.owner == "captain", entry.id
        assert entry.overridable is True, entry.id
        assert entry.notes.strip(), entry.id


def test_registry_contains_no_structure_kind() -> None:
    raw = DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8")
    assert "kind: structure" not in raw
    assert "kind:structure" not in raw


def test_registry_status_matrix_matches_design(entries) -> None:
    statuses = {entry.id: entry.status for entry in entries}
    assert statuses["midterm.score.f_marginal_weight"] == "frozen"
    for param_id in (
        "midterm.score.anchor_weight",
        "midterm.score.research_weight",
        "midterm.evidence.temperature",
        "midterm.evidence.break_lambda",
        "midterm.evidence.s0_floor",
    ):
        assert statuses[param_id] == "active", param_id
    remaining = (
        set(statuses)
        - {"midterm.score.f_marginal_weight"}
        - {
            "midterm.score.anchor_weight",
            "midterm.score.research_weight",
            "midterm.evidence.temperature",
            "midterm.evidence.break_lambda",
            "midterm.evidence.s0_floor",
        }
    )
    assert remaining
    assert {statuses[param_id] for param_id in remaining} == {"placeholder"}


def test_group_constraint_e_blend(entries) -> None:
    by_id = {entry.id: entry for entry in entries}
    anchor = by_id["midterm.score.anchor_weight"]
    research = by_id["midterm.score.research_weight"]
    for entry in (anchor, research):
        assert entry.group == "e_blend"
        assert entry.constraint == {"sum": 1.0}
    assert anchor.value + research.value == pytest.approx(1.0)


# ── 2. registry 值 == dataclass 默认值 == 模块常量 ────────────────────────


def test_registry_values_equal_dataclass_defaults(entries) -> None:
    checked = 0
    for entry in entries:
        class_name, field_name = parse_wired_in(entry.wired_in)
        params_cls, default_instance = _PARAMS_CLASSES[class_name]
        assert field_name in params_cls.__dataclass_fields__, entry.id
        assert getattr(default_instance, field_name) == entry.value, entry.id
        checked += 1
    assert checked == 15


def test_score_defaults_equal_score_engine_module_constants() -> None:
    assert DEFAULT_SCORE_PARAMS.fundamental_yoy_scale == score_engine._FUNDAMENTAL_YOY_SCALE
    assert DEFAULT_SCORE_PARAMS.ocf_negative_penalty == score_engine._OCF_NEGATIVE_PENALTY
    assert DEFAULT_SCORE_PARAMS.beat_bonus == score_engine._BEAT_BONUS
    assert DEFAULT_SCORE_PARAMS.miss_penalty == score_engine._MISS_PENALTY
    assert DEFAULT_SCORE_PARAMS.miss_gap_threshold == score_engine._MISS_GAP_THRESHOLD
    assert DEFAULT_SCORE_PARAMS.segment_divergence_threshold == score_engine._SEGMENT_DIVERGENCE_THRESHOLD
    assert DEFAULT_SCORE_PARAMS.segment_divergence_bonus == score_engine._SEGMENT_DIVERGENCE_BONUS
    assert DEFAULT_SCORE_PARAMS.f_marginal_weight == score_engine.F_MARGINAL_WEIGHT
    assert DEFAULT_SCORE_PARAMS.anchor_weight == score_engine._ANCHOR_WEIGHT
    assert DEFAULT_SCORE_PARAMS.research_weight == score_engine._RESEARCH_WEIGHT
    assert DEFAULT_SCORE_PARAMS.min_consensus_coverage == score_engine._MIN_CONSENSUS_COVERAGE


def test_min_consensus_coverage_has_single_value_in_two_modules() -> None:
    # 设计 §4.1 的重复定义缺口：两处同值由本测试钉住；P1 统一透传后才允许删常量。
    assert score_engine._MIN_CONSENSUS_COVERAGE == 3
    assert evidence_rules._MIN_CONSENSUS_COVERAGE == 3
    assert score_engine._MIN_CONSENSUS_COVERAGE == evidence_rules._MIN_CONSENSUS_COVERAGE
    assert DEFAULT_SCORE_PARAMS.min_consensus_coverage == 3


def test_evidence_defaults_equal_classifier_v2_module_constants() -> None:
    assert DEFAULT_EVIDENCE_PARAMS.s0_baseline == classifier_v2.S0_BASELINE
    assert DEFAULT_EVIDENCE_PARAMS.s0_floor == classifier_v2.S0_FLOOR
    assert DEFAULT_EVIDENCE_PARAMS.w_e == classifier_v2.W_E
    assert DEFAULT_EVIDENCE_PARAMS.w_t == classifier_v2.W_T
    assert DEFAULT_EVIDENCE_PARAMS.w_f == classifier_v2.W_F
    assert DEFAULT_EVIDENCE_PARAMS.severe_lo == classifier_v2.SEVERE_LO
    assert DEFAULT_EVIDENCE_PARAMS.severe_hi == classifier_v2.SEVERE_HI
    assert DEFAULT_EVIDENCE_PARAMS.break_lambda == classifier_v2.BREAK_LAMBDA
    assert DEFAULT_EVIDENCE_PARAMS.temperature == classifier_v2.TEMPERATURE
    assert DEFAULT_EVIDENCE_PARAMS.uncertain_p_top == classifier_v2.UNCERTAIN_P_TOP
    # 与 classifier_v2 现有类型化参数逐字段同值（P0 不做迁移；两者是不同 dataclass 类，
    # 不能直接 ==，故按字段字典对照，防止任一字段漂移）
    import dataclasses

    assert dataclasses.asdict(DEFAULT_EVIDENCE_PARAMS) == dataclasses.asdict(classifier_v2.DEFAULT_PARAMS)


def test_report_quality_defaults_match_design_section_5_1() -> None:
    params = DEFAULT_REPORT_QUALITY_PARAMS
    assert params.main_max_chars == 10000
    assert params.exec_summary_max_chars == 800
    assert params.claims_min_count == 3
    assert params.claims_max_count == 5
    assert params.max_chars_per_claim == 500
    assert params.min_grounds == 1
    assert params.numeric_rel_tolerance == 0.005
    assert params.numeric_max_unmatched_ratio == 0.0


def test_models_have_designed_field_counts() -> None:
    import dataclasses

    assert len(dataclasses.fields(ScoreParams)) == 11
    assert len(dataclasses.fields(EvidenceParams)) == 10
    assert len(dataclasses.fields(ReportQualityParams)) == 8
    assert set(ScoreParams.__dataclass_fields__) == {
        "fundamental_yoy_scale",
        "ocf_negative_penalty",
        "beat_bonus",
        "miss_penalty",
        "miss_gap_threshold",
        "segment_divergence_threshold",
        "segment_divergence_bonus",
        "f_marginal_weight",
        "anchor_weight",
        "research_weight",
        "min_consensus_coverage",
    }


def test_models_are_frozen_and_do_not_import_business_modules() -> None:
    import dataclasses
    import inspect

    import alphabee.params.models as models_mod

    for params_cls in (ScoreParams, EvidenceParams, ReportQualityParams):
        assert dataclasses.is_dataclass(params_cls)
        with pytest.raises(dataclasses.FrozenInstanceError):
            params_cls().__setattr__("__dict__", {})  # type: ignore[attr-defined]
    source = inspect.getsource(models_mod)
    for forbidden in ("alphabee.midterm", "alphabee.orchestrator", "alphabee.config", "alphabee.apps"):
        assert forbidden not in source


# ── 3. wired_in 解析 ─────────────────────────────────────────────────────


def test_parse_wired_in_forms() -> None:
    assert parse_wired_in("ScoreParams.beat_bonus") == ("ScoreParams", "beat_bonus")
    assert parse_wired_in("EvidenceParams.temperature") == ("EvidenceParams", "temperature")
    # 附录 B 第 11 条：主绑定 + 说明性文字，只解析首个 + 段
    assert parse_wired_in("ScoreParams.min_consensus_coverage + build_numeric_evidence(x=)") == (
        "ScoreParams",
        "min_consensus_coverage",
    )
    assert parse_wired_in("") is None
    assert parse_wired_in("   ") is None
    assert parse_wired_in("not-a-binding") is None


# ── 4. 非法注册表必须报明确错误 ──────────────────────────────────────────


def _mut_duplicate_id(data: dict[str, Any]) -> None:
    data["entries"].append(copy.deepcopy(data["entries"][0]))


def _mut_structure_kind(data: dict[str, Any]) -> None:
    data["entries"][0]["kind"] = "structure"


def _mut_non_numeric_value(data: dict[str, Any]) -> None:
    data["entries"][0]["value"] = "30.0"


def _mut_bool_value(data: dict[str, Any]) -> None:
    data["entries"][0]["value"] = True


def _mut_value_out_of_bounds(data: dict[str, Any]) -> None:
    data["entries"][0]["value"] = 100.0


def _mut_missing_required(data: dict[str, Any]) -> None:
    del data["entries"][0]["owner"]


def _mut_invalid_status(data: dict[str, Any]) -> None:
    data["entries"][0]["status"] = "unknown"


def _mut_invalid_unit(data: dict[str, Any]) -> None:
    data["entries"][0]["unit"] = "percent"


def _mut_invalid_domain(data: dict[str, Any]) -> None:
    data["entries"][0]["domain"] = "portfolio"


def _mut_invalid_source(data: dict[str, Any]) -> None:
    data["entries"][0]["source"] = "guess"


def _mut_bad_version(data: dict[str, Any]) -> None:
    data["entries"][0]["version"] = 0


def _mut_wired_in_missing_field(data: dict[str, Any]) -> None:
    data["entries"][0]["wired_in"] = "ScoreParams.not_a_field"


def _mut_wired_in_unknown_class(data: dict[str, Any]) -> None:
    data["entries"][0]["wired_in"] = "UnknownParams.beat_bonus"


def _mut_wired_in_unparsable(data: dict[str, Any]) -> None:
    data["entries"][0]["wired_in"] = "@@@"


def _mut_id_prefix_mismatch(data: dict[str, Any]) -> None:
    data["entries"][0]["id"] = "report.score.fundamental_yoy_scale"


def _mut_group_sum_violation(data: dict[str, Any]) -> None:
    _entry_by_id(data, "midterm.score.research_weight")["value"] = 0.5


def _mut_constraint_without_group(data: dict[str, Any]) -> None:
    del _entry_by_id(data, "midterm.score.anchor_weight")["group"]


def _mut_group_constraint_inconsistent(data: dict[str, Any]) -> None:
    _entry_by_id(data, "midterm.score.research_weight")["constraint"] = {"sum": 0.9}


def _mut_unknown_top_level(data: dict[str, Any]) -> None:
    data["owner"] = "captain"


def _mut_unknown_entry_key(data: dict[str, Any]) -> None:
    data["entries"][0]["wreu"] = "typo"


def _mut_missing_entries(data: dict[str, Any]) -> None:
    del data["entries"]


def _mut_wrong_schema_version(data: dict[str, Any]) -> None:
    data["schema_version"] = 2


def _mut_empty_entries(data: dict[str, Any]) -> None:
    data["entries"] = []


def _mut_bad_bounds_order(data: dict[str, Any]) -> None:
    data["entries"][0]["bounds"] = [60.0, 10.0]


@pytest.mark.parametrize(
    ("mutate", "message_part"),
    [
        (_mut_duplicate_id, "id 重复"),
        (_mut_structure_kind, "kind 非法"),
        (_mut_non_numeric_value, "value 必须是数值"),
        (_mut_bool_value, "value 必须是数值"),
        (_mut_value_out_of_bounds, "越界"),
        (_mut_missing_required, "缺必填字段"),
        (_mut_invalid_status, "status 非法"),
        (_mut_invalid_unit, "unit 非法"),
        (_mut_invalid_domain, "domain 非法"),
        (_mut_invalid_source, "source 非法"),
        (_mut_bad_version, "version"),
        (_mut_wired_in_missing_field, "wired_in 字段"),
        (_mut_wired_in_unknown_class, "未知 params 类"),
        (_mut_wired_in_unparsable, "wired_in 无法解析"),
        (_mut_id_prefix_mismatch, "未知 id"),
        (_mut_group_sum_violation, "组"),
        (_mut_constraint_without_group, "constraint 必须与 group"),
        (_mut_group_constraint_inconsistent, "constraint 声明不一致"),
        (_mut_unknown_top_level, "未知顶层字段"),
        (_mut_unknown_entry_key, "未知字段"),
        (_mut_missing_entries, "entries"),
        (_mut_wrong_schema_version, "schema_version"),
        (_mut_empty_entries, "entries 必须是非空列表"),
        (_mut_bad_bounds_order, "bounds 下界"),
    ],
    ids=[
        "duplicate_id",
        "structure_kind",
        "non_numeric_value",
        "bool_value",
        "value_out_of_bounds",
        "missing_required",
        "invalid_status",
        "invalid_unit",
        "invalid_domain",
        "invalid_source",
        "bad_version",
        "wired_in_missing_field",
        "wired_in_unknown_class",
        "wired_in_unparsable",
        "id_prefix_mismatch",
        "group_sum_violation",
        "constraint_without_group",
        "group_constraint_inconsistent",
        "unknown_top_level",
        "unknown_entry_key",
        "missing_entries",
        "wrong_schema_version",
        "empty_entries",
        "bad_bounds_order",
    ],
)
def test_invalid_registry_reports_explicit_error(tmp_path: Path, mutate, message_part: str) -> None:
    path = _write_registry(tmp_path, mutate)
    with pytest.raises(ParamRegistryError) as excinfo:
        load_registry(path)
    assert message_part in str(excinfo.value)


def test_malformed_yaml_reports_explicit_error(tmp_path: Path) -> None:
    path = tmp_path / "registry.yaml"
    path.write_text("entries: [unclosed", encoding="utf-8")
    with pytest.raises(ParamRegistryError, match="YAML 解析失败"):
        load_registry(path)


def test_missing_registry_file_reports_explicit_error(tmp_path: Path) -> None:
    with pytest.raises(ParamRegistryError, match="无法读取"):
        load_registry(tmp_path / "nope.yaml")


def test_validate_entries_rejects_empty_and_duplicate_and_bad_bounds(entries) -> None:
    with pytest.raises(ParamRegistryError, match="为空"):
        validate_entries([])
    with pytest.raises(ParamRegistryError, match="id 重复"):
        validate_entries([entries[0], entries[0]])
    with pytest.raises(ParamRegistryError, match="bounds 下界"):
        validate_entries([replace(entries[0], bounds=(50.0, 10.0))])
    with pytest.raises(ParamRegistryError, match="越界"):
        validate_entries([replace(entries[0], value=0.0)])
