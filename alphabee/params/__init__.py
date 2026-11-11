"""参数治理包（P0：只定义与解析，不接线；生产默认行为零变化）。

对外唯一入口：

- 类型面：:class:`ScoreParams` / :class:`EvidenceParams` / :class:`ReportQualityParams`
  与 ``DEFAULT_*``（设计 §4.6.2）；
- 注册表：:func:`load_registry` / :func:`validate_entries` / :class:`ParamEntry` /
  :class:`ParamRegistryError` / :data:`DEFAULT_REGISTRY_PATH`（``params/registry.yaml`` v0）；
- 指纹：:func:`compute_params_hash`（设计 §4.7，score + evidence 生效值 → 16 位 sha256）；
- 解析：:func:`resolve_score_params` / :func:`resolve_evidence_params` /
  :func:`resolve_report_quality_params` / :func:`resolve_parameters` /
  :func:`get_parameters_config`（优先级与 fail-open 见设计 §4.5）。

**默认值来源仍是 dataclass**：registry 缺失/损坏时 resolver 回内置默认，绝不把异常
抛给生产调用方。P1 才把 ``resolve_*`` 接进 ``decision_model`` / ``classifier_dispatch``。
"""

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
    DEFAULT_REGISTRY_PATH,
    PARAMS_CLASSES,
    ParamEntry,
    ParamRegistryError,
    compute_params_hash,
    load_registry,
    parse_wired_in,
    validate_entries,
)
from alphabee.params.resolvers import (
    DEFAULT_PROFILE,
    KNOWN_PROFILES,
    ParametersConfig,
    ResolvedParameters,
    get_parameters_config,
    resolve_evidence_params,
    resolve_parameters,
    resolve_report_quality_params,
    resolve_score_params,
)

__all__ = [
    "DEFAULT_EVIDENCE_PARAMS",
    "DEFAULT_PROFILE",
    "DEFAULT_REGISTRY_PATH",
    "DEFAULT_REPORT_QUALITY_PARAMS",
    "DEFAULT_SCORE_PARAMS",
    "KNOWN_PROFILES",
    "PARAMS_CLASSES",
    "EvidenceParams",
    "ParamEntry",
    "ParamRegistryError",
    "ParametersConfig",
    "ParametersSettings",
    "ReportQualityParams",
    "ResolvedParameters",
    "ScoreParams",
    "compute_params_hash",
    "get_parameters_config",
    "load_registry",
    "parse_wired_in",
    "resolve_evidence_params",
    "resolve_parameters",
    "resolve_report_quality_params",
    "resolve_score_params",
    "validate_entries",
]


def __getattr__(name: str) -> Any:
    """延迟导出 ``ParametersSettings``（避免包导入期触发 ``alphabee.config`` 构造）。

    ``config.yaml`` 的 ``parameters`` 段类型定义在 ``alphabee.config``；本包只在被显式
    请求时才转出它，保持 ``from alphabee.params import ScoreParams`` 无配置依赖。
    """
    if name == "ParametersSettings":
        from alphabee.config import ParametersSettings

        return ParametersSettings
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
