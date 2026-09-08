"""Layer 1'/2b 贝叶斯引擎：``EvidenceEvent → Confidence + ScenarioProbability``（设计文档 §2b / §5）。

本模块把证据日志与软状态转化为两个下游可直接消费的连续量：

- **Confidence**（P(H|E)，0-1）：对 ``EvidenceEvent`` 日志做 log-odds 贝叶斯更新得到
  后验；无证据时显式返回先验或 ``None``（绝不静默假设 0.5）。
- **ScenarioProbability**（P_bull / P_base / P_bear，和≈1）：由 State
  （``StateBelief.argmax_state``，§5.2 表）与 Confidence 联合驱动的三情景概率，
  供 ``ExpectedValue`` 计算 EV（``Evidence → BeliefUpdate → ScenarioProbability
  → ExpectedReturn → Position``）。

核心纪律（``alphabee-schema-steward`` / ``alphabee-pipeline-contract-steward``）：

- **确定性纯函数**：不调 LLM、不读外部状态；同一输入必得同一输出。
- **缺失显式 ``None``**：无证据且无先验 → 后验 ``None``；无证据但有先验 → 先验；
  不把「不知道」冒充成 0.5 后验。
- **概率来源可溯源**：``probability_source`` 标注 ``bayes_posterior``（有证据后验）
  或 ``state_prior``（无证据、只用状态先验），与 ``ExpectedValue.probability_source``
  取值域对齐，避免 dead-end。

阈值/锚定值为结构性示意（§6.3），集中在模块顶部常量，便于回测调参。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from alphabee.midterm.models import EvidenceEvent, ScenarioOutcome

# ─────────────────────────────────────────────────────────────────────────────
# 结构性示意参数 / 状态锚定（§5.2 表，应回测）
# ─────────────────────────────────────────────────────────────────────────────

_NEUTRAL_PRIOR = 0.5  # 有证据但未提供先验时的 log-odds 起点（中性先验）

# §5.2：State → P_bull / P_bear 基准锚定（confidence=0.5 中性时）
_STATE_BULL = {
    "S0": 0.33,  # 研究候选（未知）
    "S1": 0.30,  # 低（未验证，高赔率）
    "S2": 0.45,  # 快速上升
    "S3": 0.55,  # 最高
    "S4": 0.35,  # 开始下降
    "S5": 0.10,  # 退出
}
_STATE_BEAR = {
    "S0": 0.33,
    "S1": 0.30,
    "S2": 0.20,
    "S3": 0.15,
    "S4": 0.32,
    "S5": 0.60,
}
# 状态的多空方向（confidence 升高时按此方向极化 bull）：+1 看多 / -1 看空 / 0 中性
_STATE_BULL_DIRECTION = {
    "S0": 0.0,
    "S1": 0.0,
    "S2": 1.0,
    "S3": 1.0,
    "S4": -1.0,
    "S5": -1.0,
}

_CONFIDENCE_MAGNITUDE = 0.35  # 高确信（confidence→1）时 bull/bear 极化幅度
_PROB_MIN, _PROB_MAX = 0.02, 0.98  # 情景概率夹紧边界（避免退化到 0/1）


# ─────────────────────────────────────────────────────────────────────────────
# log-odds 辅助（纯函数）
# ─────────────────────────────────────────────────────────────────────────────


def _logit(p: float) -> float:
    """概率 → log-odds；夹紧到开区间 (0,1) 避免 ±∞。"""
    p = max(1e-9, min(1.0 - 1e-9, p))
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    """log-odds → 概率（数值稳定）。"""
    if x >= 0.0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _event_logodds(event: EvidenceEvent) -> float:
    """单条证据的 log-odds 增量（likelihood ratio 的对数）。

    ``confidence_delta`` ∈ [0,1) 为证据强度：confirming → LR=(1+d)/(1-d)（>1），
    refuting → LR=(1-d)/(1+d)（<1），neutral → LR=1（不变）。
    """
    d = abs(event.confidence_delta)
    d = max(0.0, min(0.999, d))
    effect = event.effect_on_thesis
    if effect == "confirming":
        return math.log((1.0 + d) / (1.0 - d))
    if effect == "refuting":
        return math.log((1.0 - d) / (1.0 + d))
    return 0.0  # neutral / 未知 effect


def _clip_prob(p: float) -> float:
    return max(_PROB_MIN, min(_PROB_MAX, p))


# ─────────────────────────────────────────────────────────────────────────────
# Confidence 更新（log-odds，§2b / §5.2 Evidence → BeliefUpdate）
# ─────────────────────────────────────────────────────────────────────────────


def update_confidence(
    events: list[EvidenceEvent] | None,
    *,
    prior: float | None = None,
) -> float | None:
    """对证据日志做 log-odds 贝叶斯更新，返回后验 P(H|E)（0-1）。

    - 无证据（``events`` 为空/``None``）→ 返回 ``prior``（未给先验则为 ``None``），
      绝不静默假设 0.5；
    - 有证据：从 ``prior`` 出发（未给先验时用中性先验 0.5 作为 log-odds 起点），
      逐条累计 ``_event_logodds`` 后 sigmoid 回概率空间。

    Args:
        events: 证据日志（confirming/refuting/neutral + confidence_delta）。
        prior: 先验 P(H)（0-1）；``None`` 且无证据时后验为 ``None``。

    Returns:
        后验 P(H|E)（0-1）；无证据且无先验时为 ``None``。
    """
    if not events:
        return prior
    lo = _logit(prior if prior is not None else _NEUTRAL_PRIOR)
    for event in events:
        lo += _event_logodds(event)
    return _sigmoid(lo)


# ─────────────────────────────────────────────────────────────────────────────
# ScenarioProbability（§5.2：State × Confidence → P_bull/base/bear）
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ScenarioProbability:
    """三情景概率（和≈1），供 ``ExpectedValue.scenarios`` 计算 EV 消费。"""

    p_bull: float
    p_base: float
    p_bear: float
    probability_source: str  # "bayes_posterior" / "state_prior"

    def as_scenarios(self) -> list[ScenarioOutcome]:
        """转成 ``ExpectedValue.scenarios`` 的 ``ScenarioOutcome`` 列表（收益字段留 ``None``）。

        概率来源与 ``ExpectedValue.probability_source`` 对齐，收益 R 由下游 EV 层
        （V + EPS 情景估值）补充，本引擎只负责概率。
        """
        return [
            ScenarioOutcome(scenario="bull", probability=self.p_bull),
            ScenarioOutcome(scenario="base", probability=self.p_base),
            ScenarioOutcome(scenario="bear", probability=self.p_bear),
        ]


def scenario_probability(
    state: str,
    *,
    confidence: float | None = None,
) -> ScenarioProbability:
    """由 State（``StateBelief.argmax_state``）与 Confidence 联合驱动三情景概率。

    §5.2 表：S1 低 / S2 快升 / S3 最高 / S4 下降。``confidence`` 升高时按状态多空
    方向极化 bull/bear（看多态更 bull、看空态更 bear），``confidence=None`` 时退回
    状态先验（``probability_source="state_prior"``）。

    Args:
        state: 名义状态（``argmax_state``，S0–S5）。
        confidence: 后验 P(H|E)（0-1，来自 ``update_confidence``）；``None`` 表示
            无证据，只用状态先验。

    Returns:
        :class:`ScenarioProbability`（三情景概率和≈1 + 概率来源）。
    """
    b0 = _STATE_BULL.get(state)
    be0 = _STATE_BEAR.get(state)
    if b0 is None or be0 is None:
        raise ValueError(f"未知状态: {state!r}（应为 S0–S5 之一）")

    if confidence is None:
        return ScenarioProbability(
            p_bull=b0,
            p_base=1.0 - b0 - be0,
            p_bear=be0,
            probability_source="state_prior",
        )

    c = 2.0 * confidence - 1.0  # confidence 0-1 → 确信方向强度 [-1,1]
    direction = _STATE_BULL_DIRECTION[state]
    p_bull = _clip_prob(b0 + c * _CONFIDENCE_MAGNITUDE * direction)
    p_bear = _clip_prob(be0 - c * _CONFIDENCE_MAGNITUDE * direction)
    p_base = 1.0 - p_bull - p_bear
    if p_base < 0.0:
        # 夹紧后仍溢出时按 bull/bear 比例重归一（保持和=1）
        total = p_bull + p_bear
        p_bull /= total
        p_bear /= total
        p_base = 0.0

    return ScenarioProbability(
        p_bull=p_bull,
        p_base=p_base,
        p_bear=p_bear,
        probability_source="bayes_posterior",
    )
