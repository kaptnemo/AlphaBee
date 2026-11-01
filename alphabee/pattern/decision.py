"""通用决策映射工具（与业务无关）。

把「动作类型 × 力度 × 门槛」三轴拆成可组合的纯函数：本模块只做数学映射，标签、阈值、
带宽全部由调用方注入；不出现任何业务状态名，也不包含 ``if state == X: buy()`` 式硬编码。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

__all__ = ["apply_ev_gate", "band_from_weight", "layered_scale", "weighted_expectation"]


def weighted_expectation(distribution: Mapping[str, float], weights: Mapping[str, float]) -> float:
    """分布期望：``Σ p·w``（分布 → 动作力度的唯一通道）。

    - 对 ``p > 0`` 的状态，``weights`` 必须提供同名键；缺失时 raise ``KeyError``，
      **不静默按 0 处理**（否则「没给权重」会被伪装成「权重为 0」）；
    - ``p <= 0`` 的项对期望无贡献，不要求权重；
    - ``weights`` 中多余（分布里没有）的键被忽略。

    值域随权重（典型为实数）；不做 clip。
    """
    total = 0.0
    for state, probability in distribution.items():
        if probability <= 0.0:
            continue
        if state not in weights:
            raise KeyError(f"状态 {state!r} 概率为 {probability!r}，但 weights 缺少对应权重")
        total += probability * weights[state]
    return total


def band_from_weight(
    weight: float | None,
    thresholds: Sequence[tuple[float, str]],
    *,
    fallback: str,
    zero_label: str | None = None,
) -> str:
    """由「折减后的实际仓位」反推仓位标签（不是由 argmax 状态直接映射）。

    - ``weight is None`` → ``fallback``（显式缺失，不猜）；
    - ``weight <= 0`` → ``zero_label``（未提供时 ``fallback``）；
    - 否则按 ``thresholds`` 顺序取第一个 ``weight >= threshold`` 的标签；都不满足 →
      ``fallback``；
    - ``thresholds`` 必须按阈值**严格降序**排列，否则 raise ``ValueError``（乱序 / 重复会
      让「第一个命中」依赖输入顺序，必须显式报错，不静默排序）。

    值域：返回调用方注入的标签字符串。阈值比较为闭区间（命中含等号）。
    """
    for index in range(len(thresholds) - 1):
        if thresholds[index][0] <= thresholds[index + 1][0]:
            raise ValueError(f"thresholds 必须严格降序，实际 {thresholds!r}")

    if weight is None:
        return fallback
    if weight <= 0.0:
        return zero_label if zero_label is not None else fallback
    for threshold, label in thresholds:
        if weight >= threshold:
            return label
    return fallback


def layered_scale(*factors: float) -> float:
    """乘性链：``Π factors``（类型 × 力度 × 门槛的乘法语义）。

    不裁剪、不夹紧：边界处理由调用方决定。无参数 → ``1.0``（乘性单位元）；任一因子为
    ``0`` → ``0.0``；负因子保持符号（调用方应自行保证非负）。
    """
    scale = 1.0
    for factor in factors:
        scale *= factor
    return scale


def apply_ev_gate(
    weight: float,
    *,
    risk_adjusted_ev: float | None,
    threshold: float,
    soft_multiplier: float,
    hard_zero_below: float,
    entropy: float,
    uncertain_entropy: float,
) -> tuple[float, str]:
    """赔率门槛：不设门槛 / 通过 / 软折减 / 硬清零。

    判定顺序与返回的 ``reason`` 取值（固定集合）：

    - ``risk_adjusted_ev is None`` → ``(weight, "no_gate")``：没有赔率就不设门槛；
    - ``ev >= threshold`` → ``(weight, "pass")``；
    - ``ev < hard_zero_below`` **且** ``entropy <= uncertain_entropy`` →
      ``(0.0, "hard_zero")``：只有「低熵确认负面」才清仓；
    - 其余 → ``(weight * soft_multiplier, "soft_reduce")``：高熵时不确定性只降低力度，
      不硬清仓。

    ``soft_multiplier`` 必须在 ``[0, 1]`` 内（``NaN`` 也视为非法），否则 raise
    ``ValueError``；该参数在每次调用时显式传入，不做模块级默认。
    """
    if not 0.0 <= soft_multiplier <= 1.0:
        raise ValueError(f"soft_multiplier 必须落在 [0, 1]，实际 {soft_multiplier!r}")

    if risk_adjusted_ev is None:
        return weight, "no_gate"
    if risk_adjusted_ev >= threshold:
        return weight, "pass"
    if risk_adjusted_ev < hard_zero_below and entropy <= uncertain_entropy:
        return 0.0, "hard_zero"
    return weight * soft_multiplier, "soft_reduce"
