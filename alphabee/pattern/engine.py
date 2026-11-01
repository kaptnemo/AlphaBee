"""通用引擎：证据 → 归一化 → softmax → 软状态 → drift / 迁移（与业务无关）。

:class:`Engine` 只执行调用方注入的公式与参数，不读取配置、不调用 LLM、不接触 IO：

- 原始证据由 ``spec.states[*].program`` 计算；
- 归一化使用每个状态自己的可达上限 ``cap``（``clip01(E / cap)``），保证状态间可比；
- 分布是 ``softmax(norm / temperature)``，是「归一化模式证据的 softmax 权重」而非频率
  概率或贝叶斯后验（设计文档 §3.5）；动作 / 仓位映射不在这里做；
- 不确定性只由 ``P_top`` 与 ``uncertain_p_top`` 判定。

所有方法都是纯函数且确定：同一 spec + 同一输入 + 同一 ``previous`` 必得同一结果；
argmax 平局按 ``spec.state_names`` 顺序取第一个。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from alphabee.pattern.evidence import build_context, evaluate_evidence
from alphabee.pattern.ops import entropy, entropy_normalized, normalize_evidence, softmax
from alphabee.pattern.spec import PatternSpec
from alphabee.pattern.types import Belief, PatternResult, Transition
from alphabee.pattern.validation import validate_spec

__all__ = ["Engine"]

_DRIFT_EPS = 1e-3
"""漂移显著性阈值：``|new - old| > _DRIFT_EPS`` 的状态才进入 drift。

这是通用的数值容差（用于过滤浮点噪声），不是业务阈值，故固定为模块私有常量。
"""


class Engine:
    """把一份 :class:`PatternSpec` 跑成软状态与迁移的通用引擎。

    构造时做两件事：

    1. 调用 :func:`alphabee.pattern.validation.validate_spec`；报告不通过时 raise
       ``ValueError``，消息包含前 5 条 issue（``code`` + ``message``）；
    2. 要求至少 2 个状态（``softmax`` 的可比性前提），否则 raise ``ValueError``。
    """

    def __init__(self, spec: PatternSpec) -> None:
        report = validate_spec(spec)
        if not report.ok:
            preview = "; ".join(f"[{issue.code}] {issue.message}" for issue in report.issues[:5])
            raise ValueError(f"非法 PatternSpec（共 {len(report.issues)} 条问题）：{preview}")
        if len(spec.states) < 2:
            raise ValueError("Engine 需要至少 2 个状态：单状态无法形成可比较的 softmax 分布")
        self.spec: PatternSpec = spec

    def evidence(
        self,
        scores: Mapping[str, float | None],
        ctx: Mapping[str, Any] | None = None,
    ) -> dict[str, float]:
        """计算原始证据；``ctx`` 缺省为 :func:`build_context`（必含 ``params``）。"""
        context = build_context(self.spec) if ctx is None else ctx
        return evaluate_evidence(self.spec, scores, context)

    def normalize(self, evidence: Mapping[str, float]) -> dict[str, float]:
        """按 ``spec.caps`` 归一化：``clip01(evidence[state] / cap[state])``，值域 ``[0, 1]``。"""
        return normalize_evidence(evidence, self.spec.caps)

    def distribution(self, norm: Mapping[str, float]) -> dict[str, float]:
        """``softmax({state: norm[state] / temperature})``；和为 1，键顺序同 ``state_names``。"""
        temperature = self.spec.params.temperature
        logits = {name: norm[name] / temperature for name in self.spec.state_names}
        return softmax(logits)

    def classify(
        self,
        scores: Mapping[str, float | None],
        *,
        ctx: Mapping[str, Any] | None = None,
        previous: PatternResult | None = None,
    ) -> PatternResult:
        """一次完整分类：证据 → 归一化 → 分布 → 软状态 + 可选迁移 / drift。

        ``diagnostics`` 固定包含三个键：

        - ``"logits"``：归一化证据 / ``temperature`` 后的 logit；
        - ``"temperature"``：本 spec 的温度；
        - ``"state_names"``：状态声明顺序（平局规则与 drift 键顺序的来源）。

        若给出 ``previous``：

        - ``transition.from_state = previous.belief.argmax_state``、``to_state = argmax``；
          ``kind`` 同状态为 ``"same"``，否则取迁移图 ``kind_of``（未知边为 ``"illegal"``）；
        - ``trigger_factors`` 按 ``spec.factors`` 顺序，收集
          ``|scores[f]| >= trigger_threshold`` 的因子（``scores`` 中缺失或为 ``None``
          的因子跳过）；
        - ``belief.drift = {state: new - old}``，仅保留 ``|diff| > 1e-3`` 的状态，无变化为
          ``None``；``previous`` 必须与本 spec 状态集一致（否则 ``KeyError``，不静默补 0）。

        平局规则：``argmax`` 取 ``spec.state_names`` 顺序中的第一个状态。
        program 抛出的异常原样上抛（见 :func:`alphabee.pattern.evidence.evaluate_evidence`）。
        """
        raw_evidence = self.evidence(scores, ctx)
        normalized_evidence = self.normalize(raw_evidence)
        temperature = self.spec.params.temperature
        logits = {name: normalized_evidence[name] / temperature for name in self.spec.state_names}
        distribution = softmax(logits)
        argmax_state = max(distribution, key=distribution.__getitem__)
        p_top = max(distribution.values())

        drift: dict[str, float] | None = None
        transition: Transition | None = None
        if previous is not None:
            previous_state = previous.belief.argmax_state
            legal = self.spec.transitions.legal(previous_state, argmax_state)
            kind = (
                "same"
                if previous_state == argmax_state
                else self.spec.transitions.kind_of(previous_state, argmax_state)
            )
            trigger_factors = tuple(
                factor
                for factor in self.spec.factors
                if (score := scores.get(factor)) is not None and abs(score) >= self.spec.trigger_threshold
            )
            transition = Transition(
                from_state=previous_state,
                to_state=argmax_state,
                legal=legal,
                kind=kind,
                trigger_factors=trigger_factors,
            )
            changes: dict[str, float] = {}
            for state in self.spec.state_names:
                delta = distribution[state] - previous.belief.distribution[state]
                if abs(delta) > _DRIFT_EPS:
                    changes[state] = delta
            drift = changes or None

        belief = Belief(
            distribution=distribution,
            argmax_state=argmax_state,
            entropy=entropy(distribution),
            drift=drift,
        )
        return PatternResult(
            belief=belief,
            transition=transition,
            raw_evidence=raw_evidence,
            normalized_evidence=normalized_evidence,
            p_top=p_top,
            entropy_normalized=entropy_normalized(distribution),
            uncertain=p_top < self.spec.params.uncertain_p_top,
            diagnostics={
                "logits": logits,
                "temperature": temperature,
                "state_names": list(self.spec.state_names),
            },
        )
