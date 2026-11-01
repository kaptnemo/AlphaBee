"""与业务无关的结果契约类型（数值 → 状态建模模式的 L3 / L4 数据载体）。

本模块只承载数据：字段名只描述「是什么」，不包含任何状态名、轴名、业务阈值或判断逻辑；
状态集合、轴集合、可达上限、温度等全部由调用方注入。缺失统一用 ``None`` 表达（不使用
哨兵值），映射字段只读使用，调用方传 ``dict`` 也会被类型系统当作 ``Mapping`` 消费。

语义声明（对应设计文档 §3.5）：

- ``Belief.distribution`` / ``PatternResult.p_top`` 是「归一化模式证据的 softmax 权重」，
  **不是频率概率、不是贝叶斯后验**，只能用于 argmax 锚点、期望计算与不确定性判定；
- ``Belief.entropy`` / ``PatternResult.entropy_normalized`` 是分布离散度，不是「不确定性
  概率」；
- ``PatternResult.raw_evidence`` / ``normalized_evidence`` 透出上游已算好的中间量，下游
  只读不重算（单一来源纪律）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = ["Belief", "PatternResult", "Scores", "Transition"]

Scores = Mapping[str, float | None]
"""每轴方向分：键为调用方定义的轴名，值为 ``[-1, 1]`` 或 ``None``（显式缺失）。

``None`` 表示「没有这一轴的数据」，由证据层显式处理；禁止静默补 0。
"""


@dataclass(frozen=True)
class Belief:
    """软状态（soft state）：分布 + argmax 锚点 + 熵 + 相对上一帧的漂移。

    - ``distribution``：状态名 → softmax 权重，和约为 1（模式权重，不是概率）；
    - ``argmax_state``：分布最大值对应的状态名，仅作动作类型锚点；
    - ``entropy``：分布香农熵 ``-Σ p·ln p``，值域 ``[0, ln K]``；
    - ``drift``：相对上一帧的质量流向；无上一帧时显式 ``None``。
    """

    distribution: Mapping[str, float]
    argmax_state: str
    entropy: float
    drift: Mapping[str, float] | None = None


@dataclass(frozen=True)
class Transition:
    """一次状态迁移的判定结果，只描述「从哪到哪 / 是否合法 / 为什么」。

    - ``from_state``：迁移前状态名；无上一帧时显式 ``None``（不伪造起点）；
    - ``to_state``：迁移后状态名；
    - ``legal``：该边是否在调用方注入的迁移图上合法；
    - ``kind``：迁移类型（如 stay / forward / downgrade / reopen / illegal，由调用方定义）；
    - ``trigger_factors``：触发迁移的轴名元组，保持顺序以便审计归因。
    """

    from_state: str | None
    to_state: str
    legal: bool
    kind: str
    trigger_factors: tuple[str, ...] = ()


@dataclass(frozen=True)
class PatternResult:
    """一次「数值 → 状态」分类的完整结果载体（含可审计中间量）。

    - ``belief``：软状态（分布 / argmax / 熵 / drift）；
    - ``transition``：迁移判定；无上一帧或无迁移图时显式 ``None``；
    - ``raw_evidence``：归一化前的每状态证据，值域由调用方公式决定（通常 ``>= 0``）；
    - ``normalized_evidence``：``clip01(raw_evidence / cap)``，每状态 ``[0, 1]``；
    - ``p_top``：分布最大值，即「状态确信度」（不是概率）；
    - ``entropy_normalized``：``H / ln K``，分布离散度，值域 ``[0, 1]``；无法计算时 ``None``；
    - ``uncertain``：不确定性标记，由 ``p_top`` 等判据与调用方规则共同决定；
    - ``diagnostics``：可选的诊断信息（缺失轴、降级原因、公式版本等），
      键由调用方定义，无内容时显式 ``None``。
    """

    belief: Belief
    transition: Transition | None
    raw_evidence: Mapping[str, float]
    normalized_evidence: Mapping[str, float]
    p_top: float
    entropy_normalized: float | None
    uncertain: bool
    diagnostics: Mapping[str, object] | None = None
