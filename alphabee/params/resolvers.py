"""参数解析：``config parameters.overrides`` > ``registry 值`` > ``dataclass 默认``。

公开 API（设计 §4.5 / §4.6.1）：

- :func:`resolve_score_params` / :func:`resolve_evidence_params` /
  :func:`resolve_report_quality_params`：返回对应 frozen dataclass；
- :func:`resolve_parameters`：返回带 provenance 的聚合结果（profile、实际生效的
  overrides、被忽略的 overrides 及原因、registry 是否可用、``params_hash``），
  供 P1 写入 ``CompanyStateArtifact`` / ``TaskRecord`` / 报告元信息。

**fail-open 契约（设计 §4.5 矩阵）**：下列任一情况都只 warning、绝不抛异常——
registry 缺失/解析失败 ⇒ 内置默认；registry 未知 id ⇒ 忽略；config 覆盖未知 id ⇒
忽略；值类型非法/越界 ⇒ 回退低优先级值（默认）；``frozen`` + config 覆盖 ⇒ 忽略；
组约束不满足 ⇒ 整组回内置默认；profile 未知 ⇒ default。函数显式传入的 ``overrides``
代表 experiment/离线回放（设计优先级 1–2），允许覆盖 ``frozen`` 条目。

**P0 只解析、不接线**：生产决策代码仍读模块常量；本模块只保证"可解析、可复现、
默认值零变化"。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from alphabee.params.models import (
    DEFAULT_EVIDENCE_PARAMS,
    DEFAULT_REPORT_QUALITY_PARAMS,
    DEFAULT_SCORE_PARAMS,
    EvidenceParams,
    ReportQualityParams,
    ScoreParams,
)
from alphabee.params.registry import (
    CLASS_ID_PREFIXES,
    ParamEntry,
    ParamRegistryError,
    compute_params_hash,
    load_registry,
    parse_wired_in,
)

logger = logging.getLogger(__name__)

#: v0 只支持 ``default`` profile（设计 §4.5：profile 未知 ⇒ 回 default + warn）。
DEFAULT_PROFILE = "default"
KNOWN_PROFILES: tuple[str, ...] = (DEFAULT_PROFILE,)

_GROUP_EPS = 1e-9


@dataclass(frozen=True)
class ParametersConfig:
    """``config.yaml`` 的 ``parameters`` 段只读视图（读取失败时 ``error`` 非空）。"""

    profile: str = DEFAULT_PROFILE
    overrides: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass(frozen=True)
class ResolvedParameters:
    """一次参数解析的聚合结果（P1 据此写 ``params_hash`` / profile / overrides）。"""

    profile: str
    score: ScoreParams
    evidence: EvidenceParams
    report_quality: ReportQualityParams
    applied_overrides: dict[str, float]
    ignored_overrides: dict[str, str]
    registry_loaded: bool
    params_hash: str


def get_parameters_config() -> ParametersConfig:
    """读取 ``config.yaml`` 的 ``parameters`` 段；任何异常都 fail-open 回默认。

    延迟 ``import alphabee.config``：本包导入期不得触发 settings 构造（旧 config /
    无 config 的测试环境也要能 ``from alphabee.params import ScoreParams``）。
    """
    try:
        from alphabee.config import get_settings

        settings = get_settings()
        raw = getattr(settings, "parameters", None)
        if raw is None:
            return ParametersConfig()
        profile = getattr(raw, "profile", DEFAULT_PROFILE) or DEFAULT_PROFILE
        overrides = getattr(raw, "overrides", {}) or {}
        if not isinstance(overrides, dict):
            logger.warning("parameters.overrides 不是映射（%r），已忽略", type(overrides).__name__)
            return ParametersConfig(profile=str(profile), error="overrides 不是映射")
        return ParametersConfig(profile=str(profile), overrides=dict(overrides))
    except Exception as exc:  # fail-open：配置读取/校验失败一律回默认
        logger.warning("读取 parameters 配置失败，回默认（%s）", exc)
        return ParametersConfig(error=str(exc))


def resolve_score_params(
    overrides: dict[str, float] | None = None,
    profile: str | None = None,
    *,
    registry_path: str | Path | None = None,
) -> ScoreParams:
    """解析 F/E 方向分参数；任何异常都回 :data:`DEFAULT_SCORE_PARAMS`。"""
    return _safe_resolve(overrides=overrides, profile=profile, registry_path=registry_path).score


def resolve_evidence_params(
    overrides: dict[str, float] | None = None,
    profile: str | None = None,
    *,
    registry_path: str | Path | None = None,
) -> EvidenceParams:
    """解析 V2 证据层参数；任何异常都回 :data:`DEFAULT_EVIDENCE_PARAMS`。"""
    return _safe_resolve(overrides=overrides, profile=profile, registry_path=registry_path).evidence


def resolve_report_quality_params(
    overrides: dict[str, float] | None = None,
    profile: str | None = None,
    *,
    registry_path: str | Path | None = None,
) -> ReportQualityParams:
    """解析报告质量参数；任何异常都回 :data:`DEFAULT_REPORT_QUALITY_PARAMS`。"""
    return _safe_resolve(overrides=overrides, profile=profile, registry_path=registry_path).report_quality


def resolve_parameters(
    overrides: dict[str, float] | None = None,
    profile: str | None = None,
    *,
    registry_path: str | Path | None = None,
) -> ResolvedParameters:
    """解析三组参数并返回 provenance 聚合结果；任何异常都回全默认（不抛）。"""
    return _safe_resolve(overrides=overrides, profile=profile, registry_path=registry_path)


def _safe_resolve(
    *,
    overrides: dict[str, float] | None,
    profile: str | None,
    registry_path: str | Path | None,
) -> ResolvedParameters:
    try:
        return _resolve_all(overrides=overrides, profile=profile, registry_path=registry_path)
    except Exception as exc:  # fail-open 契约：调用方永不看到异常
        logger.warning("参数解析异常，全部回内置默认：%s", exc)
        return _default_resolved()


def _default_resolved() -> ResolvedParameters:
    return ResolvedParameters(
        profile=DEFAULT_PROFILE,
        score=DEFAULT_SCORE_PARAMS,
        evidence=DEFAULT_EVIDENCE_PARAMS,
        report_quality=DEFAULT_REPORT_QUALITY_PARAMS,
        applied_overrides={},
        ignored_overrides={},
        registry_loaded=False,
        params_hash=compute_params_hash(DEFAULT_SCORE_PARAMS, DEFAULT_EVIDENCE_PARAMS),
    )


def _resolve_all(
    *,
    overrides: dict[str, float] | None,
    profile: str | None,
    registry_path: str | Path | None,
) -> ResolvedParameters:
    config = get_parameters_config()
    effective_profile = _resolve_profile(profile, config)
    entries, registry_loaded = _load_entries(registry_path)

    config_overrides: dict[str, Any] = dict(config.overrides)
    explicit_overrides: dict[str, Any] = {}
    if overrides:
        if isinstance(overrides, dict):
            explicit_overrides = dict(overrides)
        else:
            logger.warning("显式 overrides 不是映射（%r），已忽略", type(overrides).__name__)

    config_overrides, explicit_overrides, ignored_unknown = _drop_unknown_overrides(
        config_overrides, explicit_overrides, entries, registry_loaded
    )

    score, score_applied, score_ignored = _resolve_group(
        ScoreParams,
        entries,
        config_overrides=config_overrides,
        explicit_overrides=explicit_overrides,
    )
    evidence, evidence_applied, evidence_ignored = _resolve_group(
        EvidenceParams,
        entries,
        config_overrides=config_overrides,
        explicit_overrides=explicit_overrides,
    )
    report_quality, rq_applied, rq_ignored = _resolve_group(
        ReportQualityParams,
        entries,
        config_overrides=config_overrides,
        explicit_overrides=explicit_overrides,
    )

    applied: dict[str, float] = {**score_applied, **evidence_applied, **rq_applied}
    ignored: dict[str, str] = {**score_ignored, **evidence_ignored, **rq_ignored, **ignored_unknown}
    return ResolvedParameters(
        profile=effective_profile,
        score=score,
        evidence=evidence,
        report_quality=report_quality,
        applied_overrides=applied,
        ignored_overrides=ignored,
        registry_loaded=registry_loaded,
        params_hash=compute_params_hash(score, evidence),
    )


def _resolve_profile(profile: str | None, config: ParametersConfig) -> str:
    requested = profile if profile is not None else config.profile
    if requested not in KNOWN_PROFILES:
        logger.warning("未知参数 profile %r，回 default（v0 只支持 %s）", requested, KNOWN_PROFILES)
        return DEFAULT_PROFILE
    return str(requested)


def _load_entries(registry_path: str | Path | None) -> tuple[list[ParamEntry], bool]:
    try:
        return load_registry(registry_path), True
    except ParamRegistryError as exc:
        logger.warning("参数注册表不可用（%s），全部回内置默认", exc)
        return [], False
    except Exception as exc:  # 防御：任何意外都不阻断调用方
        logger.warning("参数注册表加载异常（%s），全部回内置默认", exc)
        return [], False


def _drop_unknown_overrides(
    config_overrides: dict[str, Any],
    explicit_overrides: dict[str, Any],
    entries: list[ParamEntry],
    registry_loaded: bool,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    """统一处理"覆盖 id 不可解析"：warn 一次并记入 ignored（不重复按组告警）。

    两种不可解析：id 不在注册表（``unknown id``），id 在注册表但 ``wired_in`` 为空、
    无法绑定 typed 字段（``not wired``，设计 §4.5「禁止静默」）。
    """
    merged_ids = {**config_overrides, **explicit_overrides}
    if not merged_ids:
        return config_overrides, explicit_overrides, {}

    ignored: dict[str, str] = {}
    if not registry_loaded:
        for param_id in merged_ids:
            logger.warning("参数注册表不可用，覆盖 %s 已忽略（回内置默认）", param_id)
            ignored[param_id] = "registry unavailable"
        return {}, {}, ignored

    known_ids = {entry.id for entry in entries}
    bound_ids = {entry.id for entry in entries if parse_wired_in(entry.wired_in) is not None}
    for param_id in merged_ids:
        if param_id not in known_ids:
            logger.warning("参数覆盖 %s 未知（不在注册表），已忽略", param_id)
            ignored[param_id] = "unknown id"
        elif param_id not in bound_ids:
            logger.warning("参数覆盖 %s 未绑定 typed 字段（wired_in 为空），已忽略", param_id)
            ignored[param_id] = "not wired"
    return (
        {key: value for key, value in config_overrides.items() if key in bound_ids},
        {key: value for key, value in explicit_overrides.items() if key in bound_ids},
        ignored,
    )


def _resolve_group(
    params_cls: type,
    entries: list[ParamEntry],
    *,
    config_overrides: dict[str, Any],
    explicit_overrides: dict[str, Any],
) -> tuple[Any, dict[str, float], dict[str, str]]:
    """解析一组参数：默认值 → registry 值 → config 覆盖 → 显式（experiment）覆盖。"""
    defaults = params_cls()
    fields_of_cls = {item.name: item for item in fields(params_cls)}
    values: dict[str, Any] = {name: getattr(defaults, name) for name in fields_of_cls}
    applicable: list[tuple[ParamEntry, str]] = []

    for entry in entries:
        binding = parse_wired_in(entry.wired_in)
        if binding is None:
            if entry.id.startswith(CLASS_ID_PREFIXES[params_cls.__name__]):
                logger.warning("注册表参数 %s 未绑定 typed 字段（wired_in 为空），解析时忽略", entry.id)
            continue
        class_name, field_name = binding
        if class_name != params_cls.__name__:
            if entry.id.startswith(CLASS_ID_PREFIXES[params_cls.__name__]):
                logger.warning("注册表参数 %s 的 id 前缀属于本组但绑定到 %s，解析时忽略", entry.id, class_name)
            continue
        applicable.append((entry, field_name))

    # 1) registry 值（优先级最低的真值来源；dataclass 默认是兜底）
    for entry, field_name in applicable:
        if entry.status == "deprecated":
            logger.warning("注册表参数 %s 已 deprecated，生效值回内置默认", entry.id)
            continue
        if entry.status == "shadow":
            logger.debug("注册表参数 %s 为 shadow（只记录不参与决策），生效值回内置默认", entry.id)
            continue
        coerced = _coerce_value(entry.value, fields_of_cls[field_name].type)
        if coerced is None:
            logger.warning(
                "注册表参数 %s 的 value %r 与字段 %s 类型不兼容，生效值回内置默认",
                entry.id,
                entry.value,
                field_name,
            )
            continue
        values[field_name] = coerced

    applied: dict[str, float] = {}
    ignored: dict[str, str] = {}
    bound_by_id = {entry.id: (entry, field_name) for entry, field_name in applicable}
    other_group_ids = {entry.id for entry in entries} - set(bound_by_id)

    def _apply(raw_overrides: dict[str, Any], *, source: str, allow_frozen: bool) -> None:
        for param_id, raw_value in raw_overrides.items():
            bound = bound_by_id.get(param_id)
            if bound is None:
                if param_id in other_group_ids:
                    continue  # 属于其它参数组，由对应 resolver 处理
                logger.warning("参数覆盖 %s（%s）未知，已忽略", param_id, source)
                ignored[param_id] = "unknown id"
                continue
            entry, field_name = bound
            if entry.status == "frozen" and not allow_frozen:
                logger.warning("参数 %s 已冻结（frozen），config 覆盖被忽略：只允许 experiment 覆盖", param_id)
                ignored[param_id] = "frozen"
                continue
            coerced, reason = _validate_override_value(entry, raw_value, fields_of_cls[field_name].type)
            if coerced is None:
                logger.warning("参数 %s（%s）覆盖值 %r 非法（%s），回退低优先级值", param_id, source, raw_value, reason)
                ignored[param_id] = reason
                continue
            values[field_name] = coerced
            applied[param_id] = coerced

    # 2) config 覆盖（frozen 拒绝）→ 3) 显式/experiment 覆盖（frozen 允许）
    _apply(config_overrides, source="config", allow_frozen=False)
    _apply(explicit_overrides, source="explicit", allow_frozen=True)

    _enforce_group_constraints(defaults, applicable, values, applied, ignored)
    return params_cls(**values), applied, ignored


def _enforce_group_constraints(
    defaults: Any,
    applicable: list[tuple[ParamEntry, str]],
    values: dict[str, Any],
    applied: dict[str, float],
    ignored: dict[str, str],
) -> None:
    """组约束（如 ``sum=1.0``）不满足 ⇒ 整组回内置默认 + warn（设计 §4.5）。"""
    groups: dict[str, list[tuple[ParamEntry, str]]] = {}
    for entry, field_name in applicable:
        if entry.group:
            groups.setdefault(entry.group, []).append((entry, field_name))

    for group_name, members in groups.items():
        target = members[0][0].constraint.get("sum")
        if target is None:
            continue
        total = sum(float(values[field_name]) for _, field_name in members)
        if abs(total - float(target)) <= _GROUP_EPS:
            continue
        logger.warning(
            "参数组 %s 约束不满足（当前 sum=%.12g，要求 %.12g），整组回内置默认",
            group_name,
            total,
            float(target),
        )
        for entry, field_name in members:
            values[field_name] = getattr(defaults, field_name)
            if entry.id in applied:
                applied.pop(entry.id)
                ignored[entry.id] = "group constraint violated"


def _coerce_value(value: Any, field_type: Any) -> float | int | None:
    """把 registry 值压到字段类型；非数值/非整数（int 字段）返回 None。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if field_type is int:
        if float(value) != int(value):
            return None
        return int(value)
    return float(value)


def _validate_override_value(entry: ParamEntry, raw_value: Any, field_type: Any) -> tuple[float | int | None, str]:
    """校验覆盖值：数值类型 + 字段类型 + bounds；返回 ``(值, "")`` 或 ``(None, 原因)``。"""
    if not isinstance(raw_value, (int, float)) or isinstance(raw_value, bool):
        return None, "not numeric"
    if field_type is int and float(raw_value) != int(raw_value):
        return None, "not integral"
    value: float | int = int(raw_value) if field_type is int else float(raw_value)
    if entry.bounds is not None:
        low, high = entry.bounds
        if not (float(low) <= float(value) <= float(high)):
            return None, f"out of bounds [{low}, {high}]"
    return value, ""
