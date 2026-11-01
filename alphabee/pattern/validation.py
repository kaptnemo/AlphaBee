"""模式规格的结构校验、网格生成与可达性工具（与业务无关）。

本模块把 :class:`alphabee.pattern.spec.PatternSpec` 的结构问题**一次性收集**为
``ValidationReport``（不遇错即抛），并提供两个纯工具：

- :func:`grid_points`：按固定顺序生成轴取值笛卡尔积；
- :func:`find_witness` / :func:`reachable_states`：在网格上寻找「某状态成为 argmax」的
  witness 帧，用于验证每状态可达（设计文档 §5.1 的 I2 / I4）。

不变量只依赖公式与网格，不依赖真实语料；网格顺序、平局规则与首次命中规则都是确定的。
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from itertools import product
from typing import Any

from alphabee.pattern.ops import normalize_evidence, softmax
from alphabee.pattern.spec import PatternSpec

__all__ = [
    "ValidationIssue",
    "ValidationReport",
    "find_witness",
    "grid_points",
    "reachable_states",
    "validate_spec",
]


@dataclass(frozen=True)
class ValidationIssue:
    """一条结构问题：``code`` 是稳定的机器可读标识，``message`` 给人读。"""

    code: str
    message: str


@dataclass(frozen=True)
class ValidationReport:
    """校验结果集合：``issues`` 为空即通过，``ok`` 是只读便捷属性。"""

    issues: tuple[ValidationIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "issues", tuple(self.issues))

    @property
    def ok(self) -> bool:
        """没有任何 issue 时为 ``True``。"""
        return not self.issues


def validate_spec(spec: PatternSpec) -> ValidationReport:
    """逐条收集 ``spec`` 的结构问题（不遇错即抛）。

    检查项与 issue code：

    - ``states.empty``：状态为空；
    - ``states.too_few``：状态数 ``< 2``（softmax 需要两两可比的分布）；
    - ``state.duplicate_name``：状态名重复；
    - ``state.cap_not_finite`` / ``state.cap_not_positive``：cap 非有限 / ``<= 0``；
    - ``state.program_not_callable``：证据程序不可调用；
    - ``factors.empty`` / ``factors.duplicate``：轴为空 / 轴名重复；
    - ``transition.unknown_endpoint``：迁移端点不在 ``state_names`` 中；
    - ``params.temperature_invalid``：温度不是有限正数；
    - ``params.uncertain_p_top_out_of_range``：``uncertain_p_top`` 不在 ``(0, 1]``；
    - ``pattern.trigger_threshold_negative``：触发阈值为负（或 ``NaN``）。

    所有问题都会被收集，返回的 :class:`ValidationReport` 保留发现顺序。
    """
    issues: list[ValidationIssue] = []

    state_names = [state.name for state in spec.states]
    if not state_names:
        issues.append(ValidationIssue("states.empty", "states 不能为空"))
    elif len(state_names) < 2:
        issues.append(
            ValidationIssue(
                "states.too_few",
                f"状态数必须 >= 2（softmax 需要可比较的分布），实际 {len(state_names)}",
            )
        )

    seen_states: set[str] = set()
    for name in state_names:
        if name in seen_states:
            issues.append(ValidationIssue("state.duplicate_name", f"状态名重复：{name!r}"))
        seen_states.add(name)

    for state in spec.states:
        if not math.isfinite(state.cap):
            issues.append(
                ValidationIssue(
                    "state.cap_not_finite",
                    f"状态 {state.name!r} 的 cap 必须是有限数，实际 {state.cap!r}",
                )
            )
        elif state.cap <= 0.0:
            issues.append(
                ValidationIssue(
                    "state.cap_not_positive",
                    f"状态 {state.name!r} 的 cap 必须 > 0，实际 {state.cap!r}",
                )
            )
        if not callable(state.program):
            issues.append(ValidationIssue("state.program_not_callable", f"状态 {state.name!r} 的 program 不可调用"))

    if not spec.factors:
        issues.append(ValidationIssue("factors.empty", "factors 不能为空"))
    seen_factors: set[str] = set()
    for factor in spec.factors:
        if factor in seen_factors:
            issues.append(ValidationIssue("factors.duplicate", f"轴名重复：{factor!r}"))
        seen_factors.add(factor)

    for from_state, targets in spec.transitions.edges.items():
        if from_state not in seen_states:
            issues.append(
                ValidationIssue(
                    "transition.unknown_endpoint",
                    f"迁移起点 {from_state!r} 不在 state_names 中",
                )
            )
        for to_state in targets:
            if to_state not in seen_states:
                issues.append(
                    ValidationIssue(
                        "transition.unknown_endpoint",
                        f"迁移终点 {to_state!r}（起点 {from_state!r}）不在 state_names 中",
                    )
                )

    temperature = spec.params.temperature
    if not math.isfinite(temperature) or temperature <= 0.0:
        issues.append(
            ValidationIssue(
                "params.temperature_invalid",
                f"params.temperature 必须是有限正数，实际 {temperature!r}",
            )
        )

    uncertain_p_top = spec.params.uncertain_p_top
    if not 0.0 < uncertain_p_top <= 1.0:
        issues.append(
            ValidationIssue(
                "params.uncertain_p_top_out_of_range",
                f"params.uncertain_p_top 必须落在 (0, 1]，实际 {uncertain_p_top!r}",
            )
        )

    if not spec.trigger_threshold >= 0.0:
        issues.append(
            ValidationIssue(
                "pattern.trigger_threshold_negative",
                f"trigger_threshold 必须 >= 0，实际 {spec.trigger_threshold!r}",
            )
        )

    return ValidationReport(tuple(issues))


def grid_points(axes: Mapping[str, Sequence[float | None]]) -> Iterator[dict[str, float | None]]:
    """生成轴取值的笛卡尔积，顺序完全确定。

    轴按 ``axes`` 的插入顺序展开，每个轴内按给定序列顺序展开（``itertools.product``）；
    空 ``axes`` 产出唯一一个空帧 ``{}``；任一轴序列为空则产出零个帧。
    """
    names = tuple(axes)
    value_sequences = tuple(axes[name] for name in names)
    for combination in product(*value_sequences):
        yield dict(zip(names, combination, strict=True))


def _state_evidence(spec: PatternSpec, frame: Mapping[str, float | None]) -> dict[str, float]:
    """对一帧调用全部状态的证据程序；``ctx`` 必含 ``"params"`` 键。"""
    ctx: dict[str, Any] = {"params": spec.params}
    return {state.name: state.program(frame, ctx) for state in spec.states}


def find_witness(
    spec: PatternSpec,
    state: str,
    *,
    axes: Mapping[str, Sequence[float | None]],
    min_p_top: float | None = None,
) -> dict[str, float | None] | None:
    """在网格上寻找「``state`` 成为 argmax」的帧；找不到返回 ``None``。

    判定流程与生产路径一致：对每帧计算全部状态原始证据 → ``clip01(E/cap)`` 归一化 →
    ``softmax(norm / temperature)``。命中条件：argmax 等于 ``state``，且（给出
    ``min_p_top`` 时）``P_top >= min_p_top``。返回按 :func:`grid_points` 顺序的**首个**命中帧。

    平局规则：``max`` 取 ``spec.states`` 声明顺序中的第一个状态（确定性）。未知状态名
    raise ``KeyError``（不静默返回 ``None``）。调用前应先通过 :func:`validate_spec`。
    """
    if state not in spec.state_names:
        raise KeyError(f"模式 {spec.name!r} 中不存在状态 {state!r}")

    caps = spec.caps
    temperature = spec.params.temperature
    for frame in grid_points(axes):
        evidence = _state_evidence(spec, frame)
        normalized = normalize_evidence(evidence, caps)
        logits = {name: normalized[name] / temperature for name in spec.state_names}
        distribution = softmax(logits)
        argmax_state = max(distribution, key=distribution.__getitem__)
        if argmax_state != state:
            continue
        if min_p_top is not None and distribution[state] < min_p_top:
            continue
        return frame
    return None


def reachable_states(
    spec: PatternSpec,
    *,
    axes: Mapping[str, Sequence[float | None]],
    min_p_top: float | None = None,
) -> dict[str, dict[str, float | None] | None]:
    """对每个状态返回 witness 帧（不可达为 ``None``），键顺序与 ``spec.states`` 一致。"""
    return {state: find_witness(spec, state, axes=axes, min_p_top=min_p_top) for state in spec.state_names}
