"""参数解析测试：优先级 + fail-open 矩阵（P0；设计 §4.5）。

覆盖：

- 默认解析 == dataclass 默认值（registry 值 == 默认值，P0 无行为变化）；
- 优先级：函数显式 overrides > config ``parameters.overrides`` > registry 值 > dataclass 默认；
- ``frozen`` 拒绝 config 覆盖、允许 experiment（显式）覆盖；
- 未知 id / 非数值 / 越界 / 组约束不满足 / profile 未知 / registry 缺失或损坏 ⇒
  warning + 回退低优先级值，**绝不抛异常**；
- 聚合结果 ``applied_overrides`` / ``ignored_overrides`` / profile / registry_loaded 正确。

全部离线、纯函数：配置读取一律 monkeypatch ``alphabee.config.get_settings``（不依赖仓库
``config.yaml`` 内容），注册表用临时 YAML（不改 ``params/registry.yaml``）。
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

import alphabee.config as config_mod
from alphabee.params import (
    DEFAULT_EVIDENCE_PARAMS,
    DEFAULT_REGISTRY_PATH,
    DEFAULT_REPORT_QUALITY_PARAMS,
    DEFAULT_SCORE_PARAMS,
    compute_params_hash,
    resolve_evidence_params,
    resolve_parameters,
    resolve_report_quality_params,
    resolve_score_params,
)

_RESOLVER_LOGGER = "alphabee.params.resolvers"


def _registry_dict() -> dict[str, Any]:
    return yaml.safe_load(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))


def _write_registry(tmp_path: Path, mutate) -> Path:
    data = copy.deepcopy(_registry_dict())
    mutate(data)
    path = tmp_path / "registry.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _set_entry(data: dict[str, Any], param_id: str, **fields: Any) -> None:
    for entry in data["entries"]:
        if entry["id"] == param_id:
            entry.update(fields)
            return
    raise AssertionError(f"registry 中不存在 {param_id}")


def _patch_settings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    profile: str = "default",
    overrides: dict[str, Any] | None = None,
) -> None:
    fake = SimpleNamespace(
        parameters=SimpleNamespace(profile=profile, overrides=dict(overrides or {})),
    )
    monkeypatch.setattr(config_mod, "get_settings", lambda: fake)


@pytest.fixture(autouse=True)
def _hermetic_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认屏蔽仓库真实 config.yaml（保证测试与运维覆盖无关）。"""
    _patch_settings(monkeypatch)


# ── 1. 默认解析 = dataclass 默认（P0 默认行为零变化）────────────────────


def test_default_resolution_equals_dataclass_defaults() -> None:
    result = resolve_parameters()
    assert result.profile == "default"
    assert result.registry_loaded is True
    assert result.score == DEFAULT_SCORE_PARAMS
    assert result.evidence == DEFAULT_EVIDENCE_PARAMS
    assert result.report_quality == DEFAULT_REPORT_QUALITY_PARAMS
    assert result.applied_overrides == {}
    assert result.ignored_overrides == {}
    assert resolve_score_params() == DEFAULT_SCORE_PARAMS
    assert resolve_evidence_params() == DEFAULT_EVIDENCE_PARAMS
    assert resolve_report_quality_params() == DEFAULT_REPORT_QUALITY_PARAMS


def test_default_hash_is_stable_across_calls() -> None:
    first = resolve_parameters()
    second = resolve_parameters()
    assert first.params_hash == second.params_hash == compute_params_hash(first.score, first.evidence)


# ── 2. 优先级：显式 > config > registry > 默认 ───────────────────────────


def test_config_override_takes_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, overrides={"midterm.score.beat_bonus": 0.1})
    result = resolve_parameters()
    assert result.score.beat_bonus == 0.1
    assert result.applied_overrides == {"midterm.score.beat_bonus": 0.1}
    assert result.ignored_overrides == {}
    assert resolve_score_params().beat_bonus == 0.1


def test_config_override_for_evidence_group(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, overrides={"midterm.evidence.temperature": 0.3})
    assert resolve_evidence_params().temperature == 0.3
    result = resolve_parameters()
    assert result.applied_overrides == {"midterm.evidence.temperature": 0.3}
    # 其它组不受影响
    assert result.score == DEFAULT_SCORE_PARAMS


def test_registry_value_beats_dataclass_default(tmp_path: Path) -> None:
    path = _write_registry(tmp_path, lambda data: _set_entry(data, "midterm.score.beat_bonus", value=0.3))
    assert resolve_score_params(registry_path=path).beat_bonus == 0.3


def test_config_override_beats_registry_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_registry(tmp_path, lambda data: _set_entry(data, "midterm.score.beat_bonus", value=0.3))
    _patch_settings(monkeypatch, overrides={"midterm.score.beat_bonus": 0.4})
    assert resolve_score_params(registry_path=path).beat_bonus == 0.4


def test_explicit_override_beats_config_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, overrides={"midterm.score.beat_bonus": 0.1})
    resolved = resolve_score_params(overrides={"midterm.score.beat_bonus": 0.2})
    assert resolved.beat_bonus == 0.2
    result = resolve_parameters(overrides={"midterm.score.beat_bonus": 0.2})
    assert result.applied_overrides == {"midterm.score.beat_bonus": 0.2}
    assert result.score.beat_bonus == 0.2


def test_int_field_override_coerces_to_int(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, overrides={"midterm.data.min_consensus_coverage": 5})
    resolved = resolve_score_params()
    assert resolved.min_consensus_coverage == 5
    assert isinstance(resolved.min_consensus_coverage, int)


# ── 3. frozen 语义 ──────────────────────────────────────────────────────


def test_frozen_rejects_config_override(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    _patch_settings(monkeypatch, overrides={"midterm.score.f_marginal_weight": 0.2})
    with caplog.at_level(logging.WARNING, logger=_RESOLVER_LOGGER):
        result = resolve_parameters()
    assert result.score.f_marginal_weight == 0.30
    assert result.applied_overrides == {}
    assert result.ignored_overrides == {"midterm.score.f_marginal_weight": "frozen"}
    assert "frozen" in caplog.text


def test_frozen_allows_explicit_experiment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, overrides={"midterm.score.f_marginal_weight": 0.2})
    result = resolve_parameters(overrides={"midterm.score.f_marginal_weight": 0.25})
    assert result.score.f_marginal_weight == 0.25
    assert result.applied_overrides == {"midterm.score.f_marginal_weight": 0.25}
    # config 层的 frozen 拒绝仍然留痕（禁止静默），但 experiment/显式覆盖生效
    assert result.ignored_overrides == {"midterm.score.f_marginal_weight": "frozen"}


# ── 4. 值校验失败 ⇒ 回低优先级值 + warn ─────────────────────────────────


def test_unknown_id_is_ignored_with_warning(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    _patch_settings(monkeypatch, overrides={"midterm.score.not_a_param": 1.0})
    with caplog.at_level(logging.WARNING, logger=_RESOLVER_LOGGER):
        result = resolve_parameters()
    assert result.score == DEFAULT_SCORE_PARAMS
    assert result.applied_overrides == {}
    assert result.ignored_overrides == {"midterm.score.not_a_param": "unknown id"}
    assert "未知" in caplog.text


def test_out_of_bounds_override_falls_back(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    _patch_settings(monkeypatch, overrides={"midterm.score.beat_bonus": 0.9})
    with caplog.at_level(logging.WARNING, logger=_RESOLVER_LOGGER):
        result = resolve_parameters()
    assert result.score.beat_bonus == 0.2
    assert result.applied_overrides == {}
    assert "out of bounds" in result.ignored_overrides["midterm.score.beat_bonus"]
    assert "越界" in caplog.text or "out of bounds" in caplog.text


def test_non_numeric_override_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, overrides={"midterm.score.beat_bonus": "0.1"})
    result = resolve_parameters()
    assert result.score.beat_bonus == 0.2
    assert result.ignored_overrides == {"midterm.score.beat_bonus": "not numeric"}


def test_non_integral_override_for_int_field_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, overrides={"midterm.data.min_consensus_coverage": 4.5})
    result = resolve_parameters()
    assert result.score.min_consensus_coverage == 3
    assert result.ignored_overrides == {"midterm.data.min_consensus_coverage": "not integral"}


def test_deprecated_registry_entry_falls_back_to_default(tmp_path: Path) -> None:
    path = _write_registry(
        tmp_path,
        lambda data: _set_entry(data, "midterm.score.beat_bonus", status="deprecated", value=0.4),
    )
    assert resolve_score_params(registry_path=path).beat_bonus == 0.2


def test_shadow_registry_entry_does_not_enter_decision(tmp_path: Path) -> None:
    path = _write_registry(
        tmp_path,
        lambda data: _set_entry(data, "midterm.score.beat_bonus", status="shadow", value=0.4),
    )
    assert resolve_score_params(registry_path=path).beat_bonus == 0.2


# ── 5. 组约束（e_blend sum=1.0）────────────────────────────────────────


def test_group_constraint_satisfied_by_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(
        monkeypatch,
        overrides={"midterm.score.anchor_weight": 0.7, "midterm.score.research_weight": 0.3},
    )
    result = resolve_parameters()
    assert result.score.anchor_weight == 0.7
    assert result.score.research_weight == 0.3
    assert result.applied_overrides == {
        "midterm.score.anchor_weight": 0.7,
        "midterm.score.research_weight": 0.3,
    }
    assert result.ignored_overrides == {}


def test_group_constraint_violation_reverts_whole_group(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_settings(monkeypatch, overrides={"midterm.score.anchor_weight": 0.9})
    with caplog.at_level(logging.WARNING, logger=_RESOLVER_LOGGER):
        result = resolve_parameters()
    # anchor 0.9 + research 0.4 违反 sum=1.0 ⇒ 整组回内置默认
    assert result.score.anchor_weight == 0.6
    assert result.score.research_weight == 0.4
    assert result.applied_overrides == {}
    assert result.ignored_overrides == {"midterm.score.anchor_weight": "group constraint violated"}
    assert "e_blend" in caplog.text


def test_group_constraint_violation_reverts_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    result = resolve_parameters(overrides={"midterm.score.research_weight": 0.8})
    assert result.score.anchor_weight == 0.6
    assert result.score.research_weight == 0.4
    assert result.ignored_overrides == {"midterm.score.research_weight": "group constraint violated"}


# ── 6. registry / 配置读取 fail-open ──────────────────────────────────


def test_missing_registry_falls_back_to_defaults(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=_RESOLVER_LOGGER):
        result = resolve_parameters(registry_path=tmp_path / "missing.yaml")
    assert result.registry_loaded is False
    assert result.score == DEFAULT_SCORE_PARAMS
    assert result.evidence == DEFAULT_EVIDENCE_PARAMS
    assert result.report_quality == DEFAULT_REPORT_QUALITY_PARAMS
    assert result.profile == "default"
    assert "注册表不可用" in caplog.text


def test_missing_registry_also_ignores_config_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, overrides={"midterm.score.beat_bonus": 0.1})
    result = resolve_parameters(registry_path=tmp_path / "missing.yaml")
    assert result.score.beat_bonus == 0.2
    assert result.applied_overrides == {}
    assert result.ignored_overrides == {"midterm.score.beat_bonus": "registry unavailable"}


def test_broken_yaml_falls_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "registry.yaml"
    path.write_text("entries: [unclosed", encoding="utf-8")
    result = resolve_parameters(registry_path=path)
    assert result.registry_loaded is False
    assert result.score == DEFAULT_SCORE_PARAMS


def test_semantically_invalid_registry_falls_back_to_defaults(tmp_path: Path) -> None:
    path = _write_registry(tmp_path, lambda data: _set_entry(data, "midterm.score.research_weight", value=0.5))
    result = resolve_parameters(registry_path=path)
    assert result.registry_loaded is False
    assert result.score == DEFAULT_SCORE_PARAMS


def test_config_read_failure_falls_back_to_defaults(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _raise() -> Any:
        raise RuntimeError("config.yaml 不可读")

    monkeypatch.setattr(config_mod, "get_settings", _raise)
    with caplog.at_level(logging.WARNING, logger=_RESOLVER_LOGGER):
        result = resolve_parameters()
    assert result.score == DEFAULT_SCORE_PARAMS
    assert result.profile == "default"
    assert result.registry_loaded is True  # registry 仍可用，只是没有覆盖
    assert "读取 parameters 配置失败" in caplog.text


# ── 7. profile 语义 ──────────────────────────────────────────────────


def test_parameters_settings_defaults() -> None:
    from alphabee.config import ParametersSettings

    settings = ParametersSettings()
    assert settings.profile == "default"
    assert settings.overrides == {}


def test_parameters_settings_lazy_export_from_params_package() -> None:
    # 包级 ``ParametersSettings`` 是延迟导出（避免包导入期触发 config 构造），且与 config 同类。
    from alphabee.config import ParametersSettings as ConfigParametersSettings
    from alphabee.params import ParametersSettings

    assert ParametersSettings is ConfigParametersSettings


def test_old_config_without_parameters_section_still_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # 旧 config.yaml 没有 parameters 段 ⇒ Settings 用默认段；resolver 行为与 P0 之前一致。
    monkeypatch.setattr(config_mod, "get_settings", lambda: SimpleNamespace())
    result = resolve_parameters()
    assert result.profile == "default"
    assert result.score == DEFAULT_SCORE_PARAMS
    assert result.applied_overrides == {}
    assert result.registry_loaded is True


def test_unwired_registry_entry_is_ignored_with_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    # "registry 有未知 id ⇒ 忽略该条 + warn"（设计 §4.5）：合法但未接线的条目不得影响生效值。
    def _add_unwired_entry(data: dict[str, Any]) -> None:
        extra = copy.deepcopy(data["entries"][0])
        extra["id"] = "midterm.score.future_param"
        extra["wired_in"] = ""
        extra["notes"] = "未接线占位（等 P1 绑定）"
        data["entries"].append(extra)

    path = _write_registry(tmp_path, _add_unwired_entry)
    with caplog.at_level(logging.WARNING, logger=_RESOLVER_LOGGER):
        resolved = resolve_score_params(registry_path=path)
    assert resolved == DEFAULT_SCORE_PARAMS
    assert "future_param" in caplog.text
    assert "未绑定 typed 字段" in caplog.text


def test_override_for_unwired_registry_entry_is_ignored_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # 覆盖目标存在但未接线：必须 warn + 记入 ignored（设计 §4.5「禁止静默」），不得静默丢弃。
    def _add_unwired_entry(data: dict[str, Any]) -> None:
        extra = copy.deepcopy(data["entries"][0])
        extra["id"] = "midterm.score.future_param"
        extra["wired_in"] = ""
        extra["notes"] = "未接线占位（等 P1 绑定）"
        data["entries"].append(extra)

    path = _write_registry(tmp_path, _add_unwired_entry)
    _patch_settings(monkeypatch, overrides={"midterm.score.future_param": 0.1})
    with caplog.at_level(logging.WARNING, logger=_RESOLVER_LOGGER):
        result = resolve_parameters(registry_path=path)
    assert result.score == DEFAULT_SCORE_PARAMS
    assert result.applied_overrides == {}
    assert result.ignored_overrides == {"midterm.score.future_param": "not wired"}
    assert "未绑定 typed 字段" in caplog.text


def test_unknown_profile_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _patch_settings(monkeypatch, profile="experiment")
    with caplog.at_level(logging.WARNING, logger=_RESOLVER_LOGGER):
        result = resolve_parameters()
    assert result.profile == "default"
    assert "未知参数 profile" in caplog.text


def test_explicit_default_profile_overrides_unknown_config_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(monkeypatch, profile="experiment")
    assert resolve_parameters(profile="default").profile == "default"


# ── 8. 聚合结果与异常安全 ────────────────────────────────────────────


def test_aggregate_applied_and_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_settings(
        monkeypatch,
        overrides={
            "midterm.score.beat_bonus": 0.1,
            "midterm.evidence.temperature": 0.3,
            "midterm.score.f_marginal_weight": 0.2,  # frozen ⇒ ignored
            "midterm.score.not_a_param": 1.0,  # unknown ⇒ ignored
            "midterm.evidence.s0_floor": 99.0,  # 越界 ⇒ ignored
        },
    )
    result = resolve_parameters()
    assert result.applied_overrides == {
        "midterm.score.beat_bonus": 0.1,
        "midterm.evidence.temperature": 0.3,
    }
    assert set(result.ignored_overrides) == {
        "midterm.score.f_marginal_weight",
        "midterm.score.not_a_param",
        "midterm.evidence.s0_floor",
    }
    assert result.ignored_overrides["midterm.score.f_marginal_weight"] == "frozen"
    assert result.ignored_overrides["midterm.score.not_a_param"] == "unknown id"
    assert "out of bounds" in result.ignored_overrides["midterm.evidence.s0_floor"]
    assert result.score.beat_bonus == 0.1
    assert result.evidence.temperature == 0.3
    assert result.params_hash == compute_params_hash(result.score, result.evidence)


@pytest.mark.parametrize(
    "overrides",
    [
        {"midterm.score.beat_bonus": None},
        {"midterm.score.beat_bonus": [0.1]},
        {"midterm.score.beat_bonus": object()},
        {"midterm.data.min_consensus_coverage": "5"},
    ],
)
def test_illegal_override_types_never_raise(monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]) -> None:
    _patch_settings(monkeypatch, overrides=overrides)
    result = resolve_parameters()
    assert result.score == DEFAULT_SCORE_PARAMS
    assert overrides and set(result.ignored_overrides) == set(overrides)


def test_non_mapping_overrides_never_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    result = resolve_parameters(overrides=["not", "a", "mapping"])  # type: ignore[arg-type]
    assert result.score == DEFAULT_SCORE_PARAMS
    assert result.applied_overrides == {}


def test_invalid_registry_path_type_never_raise() -> None:
    result = resolve_parameters(registry_path=123)  # type: ignore[arg-type]
    assert result.score == DEFAULT_SCORE_PARAMS
    assert result.registry_loaded is False
