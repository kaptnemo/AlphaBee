"""证据层组装（与业务无关）。

把 :class:`alphabee.pattern.spec.PatternSpec` 的每状态证据程序调用起来，不改写公式、
不注入默认值：

- :func:`build_context` 负责生成调用上下文（必含 ``"params"``），``extra`` 只能补充、
  不能覆盖；
- :func:`evaluate_evidence` 按 ``spec.states`` 声明顺序逐状态调用
  ``program(scores, ctx)``，程序抛出的异常原样上抛（不静默吞掉、不转成默认证据）。

纯函数：无 IO、无全局状态、无缓存。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from alphabee.pattern.spec import PatternSpec

__all__ = ["build_context", "evaluate_evidence"]


def build_context(spec: PatternSpec, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """构造证据程序调用上下文：``{"params": spec.params}`` + 可选 ``extra``。

    ``extra`` 只能补充新键；出现 ``"params"`` 键时 raise ``ValueError``——模式参数必须
    来自 ``spec.params``，禁止在证据层临时替换，避免「一半用新参数一半用旧参数」。

    返回新字典，调用方后续修改不会影响 ``spec``。
    """
    context: dict[str, Any] = {"params": spec.params}
    if extra is not None:
        if "params" in extra:
            raise ValueError('extra 不得包含 "params" 键：模式参数只能来自 spec.params')
        context.update(extra)
    return context


def evaluate_evidence(
    spec: PatternSpec,
    scores: Mapping[str, float | None],
    ctx: Mapping[str, Any],
) -> dict[str, float]:
    """按 ``spec.states`` 声明顺序逐状态调用 ``program(scores, ctx)``，返回原始证据。

    - 返回字典保持 ``spec.states`` 的顺序（下游 argmax 平局确定性的来源）；
    - **不捕获、不包装** program 抛出的异常：证据层无法替调用方决定降级口径，异常原样
      上抛，避免把「计算失败」静默变成「0 证据」（设计文档 §7 D10 静默 0 回退）；
    - 纯函数：不修改 ``scores`` / ``ctx``，无 IO、无全局状态。
    """
    return {state.name: state.program(scores, ctx) for state in spec.states}
