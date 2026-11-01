"""与业务无关的「数值 → 状态」建模模式实现。

本包提供可复用的数据契约、纯函数算子、模式规格与校验、证据聚合 / 软状态引擎、
决策映射工具与不变量测试工具。状态名、轴名、宽度、阈值、cap、温度等全部由调用方注入；
包内不包含任何业务状态语义，也不 import 任何业务模块。

典型用法::

    spec = PatternSpec(...)          # 状态 / 轴 / cap / 参数由调用方定义
    engine = Engine(spec)
    result = engine.classify(scores)  # 证据 → 归一化 → softmax → 软状态
"""

from alphabee.pattern.decision import (
    apply_ev_gate,
    band_from_weight,
    layered_scale,
    weighted_expectation,
)
from alphabee.pattern.engine import Engine
from alphabee.pattern.evidence import build_context, evaluate_evidence
from alphabee.pattern.ops import (
    clip01,
    dn,
    entropy,
    entropy_normalized,
    log1p_saturate,
    mean_missing,
    neutral_band,
    normalize_evidence,
    not_yet,
    saturate,
    severe,
    softmax,
    up,
)
from alphabee.pattern.spec import (
    AxisSpec,
    EvidenceProgram,
    MissingPolicy,
    Params,
    PatternSpec,
    StateSpec,
    TransitionGraph,
)
from alphabee.pattern.testing import (
    assert_bounds,
    assert_monotone,
    assert_no_uniform,
    assert_reachable,
    iter_grid_results,
)
from alphabee.pattern.types import Belief, PatternResult, Scores, Transition
from alphabee.pattern.validation import (
    ValidationIssue,
    ValidationReport,
    find_witness,
    grid_points,
    reachable_states,
    validate_spec,
)

__all__ = [
    "AxisSpec",
    "Belief",
    "Engine",
    "EvidenceProgram",
    "MissingPolicy",
    "Params",
    "PatternResult",
    "PatternSpec",
    "Scores",
    "StateSpec",
    "Transition",
    "TransitionGraph",
    "ValidationIssue",
    "ValidationReport",
    "apply_ev_gate",
    "assert_bounds",
    "assert_monotone",
    "assert_no_uniform",
    "assert_reachable",
    "band_from_weight",
    "build_context",
    "clip01",
    "dn",
    "entropy",
    "entropy_normalized",
    "evaluate_evidence",
    "find_witness",
    "grid_points",
    "iter_grid_results",
    "layered_scale",
    "log1p_saturate",
    "mean_missing",
    "neutral_band",
    "normalize_evidence",
    "not_yet",
    "reachable_states",
    "saturate",
    "severe",
    "softmax",
    "up",
    "validate_spec",
    "weighted_expectation",
]
