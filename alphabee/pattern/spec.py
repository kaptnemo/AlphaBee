"""数值 → 状态建模模式的规格契约（与业务无关）。

本模块只定义「模式长什么样」的数据契约：状态、每状态的可达上限与证据程序、轴元数据、
迁移图、通用参数。所有状态名、轴名、宽度 / 阈值均由调用方注入；模块内不含任何业务状态
语义，也不硬编码业务常量。

校验分层（与设计文档 §3.6 的两层口径一致）：

- **构造期只做数据规格化**，不逐条抛错：状态非空、名字唯一、cap 合法、迁移端点存在等结构
  问题由 :func:`alphabee.pattern.validation.validate_spec` 一次性收集成
  ``ValidationReport``（抛第一条会掩盖其余问题，也不利于批量修复）；
- **查表类轻量检查**保留：:meth:`PatternSpec.state` 找不到状态时 raise ``KeyError``。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

__all__ = [
    "AxisSpec",
    "EvidenceProgram",
    "MissingPolicy",
    "Params",
    "PatternSpec",
    "StateSpec",
    "TransitionGraph",
]

MissingPolicy = Literal["zero", "half", "none", "propagate"]
"""单轴缺失值处理策略的元数据取值（由证据层解释，本模块不实现具体处理）。"""


class EvidenceProgram(Protocol):
    """单状态证据程序：``(scores, ctx) -> 原始证据``。

    - ``scores``：轴名 → 方向分 ``[-1, 1]`` 或 ``None``（显式缺失）；
    - ``ctx``：调用上下文，约定必含 ``"params"`` 键（:class:`Params` 实例）；其余键由调用方
      自行扩展，本包不读取。
    - 返回值是归一化前的原始证据，值域由程序自身语义决定（通常 ``>= 0``），cap 由
      :class:`StateSpec` 声明。
    """

    def __call__(self, scores: Mapping[str, float | None], ctx: Mapping[str, Any]) -> float: ...


@dataclass(frozen=True)
class AxisSpec:
    """单轴元数据声明：只描述「这条轴叫什么、缺失怎么声明」，不含业务策略。

    ``missing_policy`` 是给证据层与审计消费的声明，合法取值见 :data:`MissingPolicy`；
    本类不据此做任何计算，也不校验取值合法性（类型层约束）。
    """

    name: str
    missing_policy: MissingPolicy


@dataclass(frozen=True)
class StateSpec:
    """单状态规格：状态名 + 可达上限 cap + 证据程序。

    ``cap`` 是该状态公式在输入域上的**可达最大值**（结构常数，不是超参），用于把原始证据
    归一化到 ``[0, 1]`` 使状态间可比。``cap > 0`` 与 ``program`` 可调用性由
    :func:`alphabee.pattern.validation.validate_spec` 统一校验。
    """

    name: str
    cap: float
    program: EvidenceProgram


@dataclass(frozen=True)
class Params:
    """通用模式参数（校准参数，不可变、整包透传）。

    - ``temperature``：softmax 温度，必须 ``> 0``；
    - ``uncertain_p_top``：不确定性阈值，``P_top`` 低于它即标记 uncertain，取值
      ``(0, 1]``。

    两个字段**都显式必填、无缺省值**：不同领域的校准结果不同，静态默认值会把「未校准」
    伪装成「已校准」。范围校验由 :func:`alphabee.pattern.validation.validate_spec` 负责。
    """

    temperature: float
    uncertain_p_top: float


@dataclass(frozen=True)
class TransitionGraph:
    """有向迁移图：``from -> {to -> kind}``。

    - 自环不写入 ``edges``：``legal(a, a) is True``、``kind_of(a, a) == "same"``；
    - 未知边：``legal`` 为 ``False``、``kind_of`` 为 ``"illegal"``；
    - :meth:`chain` / :meth:`add` / :meth:`with_backward` 均**返回新图，不修改原图**；
    - 构造时复制边表，调用方后续修改自己传入的字典不会影响本对象。
    """

    edges: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "edges",
            {from_state: dict(targets) for from_state, targets in self.edges.items()},
        )

    @classmethod
    def chain(cls, states: Sequence[str], kind: str = "forward") -> TransitionGraph:
        """按给定顺序把相邻状态连成链：``states[i] -> states[i+1]``，边类型为 ``kind``。

        相邻重复（自环）不写入；空序列 / 单状态得到空图。
        """
        edges: dict[str, dict[str, str]] = {}
        for from_state, to_state in zip(states, states[1:], strict=False):
            if from_state == to_state:
                continue
            edges.setdefault(from_state, {})[to_state] = kind
        return cls(edges=edges)

    def with_backward(self, kind: str = "backward") -> TransitionGraph:
        """返回「正向边 + 全部反向边」的新图；已存在的反向边保持原 ``kind``。

        对已含反向边的图幂等。自环不写入。
        """
        edges = {from_state: dict(targets) for from_state, targets in self.edges.items()}
        for from_state, targets in self.edges.items():
            for to_state in targets:
                if from_state == to_state:
                    continue
                edges.setdefault(to_state, {}).setdefault(from_state, kind)
        return TransitionGraph(edges=edges)

    def add(self, from_state: str, to_state: str, kind: str) -> TransitionGraph:
        """返回新增一条边的新图；自环不写入（直接返回本对象）。"""
        if from_state == to_state:
            return self
        edges = {src: dict(targets) for src, targets in self.edges.items()}
        edges.setdefault(from_state, {})[to_state] = kind
        return TransitionGraph(edges=edges)

    def legal(self, a: str, b: str) -> bool:
        """判断 ``a -> b`` 是否合法：同状态恒合法，其余看边表。"""
        if a == b:
            return True
        return b in self.edges.get(a, {})

    def kind_of(self, a: str, b: str) -> str:
        """返回 ``a -> b`` 的类型：自环 ``"same"``、未知边 ``"illegal"``。"""
        if a == b:
            return "same"
        return self.edges.get(a, {}).get(b, "illegal")

    @property
    def states(self) -> frozenset[str]:
        """边表中出现过的全部状态（起点 ∪ 终点）。"""
        names: set[str] = set()
        for from_state, targets in self.edges.items():
            names.add(from_state)
            names.update(targets)
        return frozenset(names)


@dataclass(frozen=True)
class PatternSpec:
    """一个完整的「数值 → 状态」模式规格。

    - ``name``：模式名（仅标识，不参与计算）；
    - ``factors``：轴名元组，非空且唯一（完整校验见 validation）；
    - ``states``：状态元组，非空且名字唯一（完整校验见 validation）；
    - ``transitions``：迁移图；
    - ``params``：通用模式参数；
    - ``trigger_threshold``：迁移归因时判定「活跃因子」的阈值 ``>= 0``。

    只读便捷属性：:attr:`state_names`（保持声明顺序）、:attr:`caps`、:meth:`state`。
    """

    name: str
    factors: tuple[str, ...]
    states: tuple[StateSpec, ...]
    transitions: TransitionGraph
    params: Params
    trigger_threshold: float

    @property
    def state_names(self) -> tuple[str, ...]:
        """状态名元组，顺序与 ``states`` 声明一致（决定 argmax 平局的确定性）。"""
        return tuple(state.name for state in self.states)

    @property
    def caps(self) -> dict[str, float]:
        """状态名 → 可达上限 cap。"""
        return {state.name: state.cap for state in self.states}

    def state(self, name: str) -> StateSpec:
        """按名查状态；找不到 raise ``KeyError``（不返回默认对象）。"""
        for state in self.states:
            if state.name == name:
                return state
        raise KeyError(f"模式 {self.name!r} 中不存在状态 {name!r}")
