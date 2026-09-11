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

# 改造 C（设计 MIDTERM_INSIGHT_INJECTION_DESIGN.md §4）置信度校准参数：
_CONFIDENCE_CLAMP = (0.05, 0.95)  # 后验夹紧边界：没有证据能让人 100% 确信未证实的 thesis
_REFUTING_WEIGHT = 1.2  # refuting 的 log-odds 放大倍数（反证比佐证更有信息量，不对称）

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

# 改造 B（设计 MIDTERM_INSIGHT_INJECTION_DESIGN.md §4）证据条件化权重：
# direction = (1-w)×state_dir + w×ev_tilt，证据净方向与状态先验方向各占一半，
# 使证据能够部分扭转硬编码状态方向（而非只由状态表极化）。
_EVIDENCE_DIRECTION_WEIGHT = 0.5

# 无证据（state_prior）时的保守先验收缩：把 §5.2 状态锚点向均匀先验 1/3 收缩，
# 避免「因子→S3→bull 0.55→EV」的状态先验自我引用给激进 EV（S3 bull 0.55 → 0.44）。
_STATE_PRIOR_SHRINK = 0.5


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

    改造 C：refuting 放大 ``_REFUTING_WEIGHT``（1.2×）——反证在贝叶斯里比佐证
    更有信息量（不对称），使「多条 confirming 累积」不再轻易压倒少量 refuting。
    """
    d = abs(event.confidence_delta)
    d = max(0.0, min(0.999, d))
    effect = event.effect_on_thesis
    if effect == "confirming":
        return math.log((1.0 + d) / (1.0 - d))
    if effect == "refuting":
        return _REFUTING_WEIGHT * math.log((1.0 - d) / (1.0 + d))
    return 0.0  # neutral / 未知 effect


def _clip_confidence(p: float) -> float:
    """把后验概率夹紧到 ``_CONFIDENCE_CLAMP``（改造 C：防 sigmoid 饱和到 0/1）。"""
    lo, hi = _CONFIDENCE_CLAMP
    return max(lo, min(hi, p))


def _clip_prob(p: float) -> float:
    return max(_PROB_MIN, min(_PROB_MAX, p))


def _conservative_prior(bull: float, bear: float) -> tuple[float, float]:
    """无证据时的保守先验：把 §5.2 状态 bull/bear 锚点向均匀先验 1/3 收缩。

    S3：bull 0.55 → 0.44、bear 0.15 → 0.24；S5：bull 0.10 → 0.21、bear 0.60 → 0.47。
    零证据下状态先验不再「无脑」给高 bull，避免自我引用给激进 EV。
    """
    flat = 1.0 / 3.0
    b = flat + (bull - flat) * _STATE_PRIOR_SHRINK
    be = flat + (bear - flat) * _STATE_PRIOR_SHRINK
    return b, be


def _evidence_tilt(events: list[EvidenceEvent] | None) -> float:
    """净证据方向（改造 B）：``Σ(confirming·d − refuting·d)``，clip 到 ``[-1, 1]``。

    正 = 证据整体确认 thesis（推高 bull），负 = 证据整体反驳（推低 bull）；
    ``neutral`` 不计。多条证据叠加后 clip 到 ``[-1, 1]``，作为与状态先验方向各占
    一半的证据方向分量（``_EVIDENCE_DIRECTION_WEIGHT``），使证据能够**部分扭转**
    硬编码状态方向（§4 改造 B）。
    """
    tilt = 0.0
    for e in events or []:
        if e.effect_on_thesis == "confirming":
            tilt += e.confidence_delta
        elif e.effect_on_thesis == "refuting":
            tilt -= e.confidence_delta
    return max(-1.0, min(1.0, tilt))


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
      逐条累计 ``_event_logodds`` 后 sigmoid 回概率空间；
    - 改造 C：refuting 按 ``_REFUTING_WEIGHT``（1.2×）放大 log-odds（反证更有信息量），
      最终后验夹紧到 ``_CONFIDENCE_CLAMP``（[0.05, 0.95]）防饱和到 0/1。

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
    return _clip_confidence(_sigmoid(lo))


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
    has_evidence: bool | None = None,
    events: list[EvidenceEvent] | None = None,
) -> ScenarioProbability:
    """由 State（``StateBelief.argmax_state``）、Confidence 与证据方向联合驱动三情景概率。

    §5.2 表：S1 低 / S2 快升 / S3 最高 / S4 下降。``confidence`` 升高时按**证据条件化
    方向**（改造 B）极化 bull/bear：``direction = (1-w)×state_dir + w×ev_tilt``，
    其中 ``state_dir`` 为状态硬编码方向（``_STATE_BULL_DIRECTION``），``ev_tilt`` 为
    证据净方向（``_evidence_tilt``）——证据可部分扭转状态先验方向，而非只由状态表锁定。

    概率来源语义（§5.2 Evidence → BeliefUpdate → ScenarioProbability）：

    - **有证据**（``has_evidence=True``，或未显式传 ``has_evidence`` 且 ``confidence``
      非 ``None``）→ ``probability_source="bayes_posterior"``，用置信后验极化；
    - **无证据**（``has_evidence=False``，或 ``confidence is None``）→
      ``probability_source="state_prior"``，用**保守先验**（§5.2 锚点向均匀先验收缩，
      S3 bull 0.55 → 0.44），不再无脑给高 bull。

    Args:
        state: 名义状态（``argmax_state``，S0–S5）。
        confidence: 后验 P(H|E)（0-1，来自 ``update_confidence``）；``None`` 表示
            无证据。
        has_evidence: 是否有证据日志；``None`` 时按 ``confidence is not None`` 推断
            （向后兼容）。显式 ``False`` 时即使给了 ``confidence`` 也走保守先验。
        events: 证据日志（``EvidenceEvent`` 列表，改造 B）；用于推导证据净方向
            ``_evidence_tilt``。``None``/空 → ``ev_tilt=0`` → ``direction = 0.5×state_dir``
            （状态方向减半，确定性、向后兼容；与 decision_model.evaluate 的接线
            ``events=evidence`` 一致——无证据时即为该退化行为，而非回到纯状态表极化）。

    Returns:
        :class:`ScenarioProbability`（三情景概率和≈1 + 概率来源）。
    """
    b0 = _STATE_BULL.get(state)
    be0 = _STATE_BEAR.get(state)
    if b0 is None or be0 is None:
        raise ValueError(f"未知状态: {state!r}（应为 S0–S5 之一）")

    evidence = has_evidence if has_evidence is not None else confidence is not None

    if not evidence:
        b, be = _conservative_prior(b0, be0)
        return ScenarioProbability(
            p_bull=b,
            p_base=1.0 - b - be,
            p_bear=be,
            probability_source="state_prior",
        )

    if confidence is None:  # 防御：has_evidence=True 但 confidence 缺失
        confidence = _NEUTRAL_PRIOR

    c = 2.0 * confidence - 1.0  # confidence 0-1 → 确信方向强度 [-1,1]
    state_dir = _STATE_BULL_DIRECTION[state]
    ev_tilt = _evidence_tilt(events)
    # 改造 B：证据条件化方向 = 状态先验方向 × (1-w) + 证据净方向 × w（证据可部分扭转方向）
    direction = (1.0 - _EVIDENCE_DIRECTION_WEIGHT) * state_dir + _EVIDENCE_DIRECTION_WEIGHT * ev_tilt
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
