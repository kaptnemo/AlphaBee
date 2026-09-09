"""Layer 2a 分类器：``VariableScores → StateBelief + StateTransition``（设计文档 §4 + §2b）。

把七因子方向分（因子**模式**）确定性分类为**软状态**概率分布，而非硬标签点估计。
核心纪律：

- **确定性纯函数**：不调 LLM、不读外部状态；同一输入必得同一输出。
- **不求和定状态**：状态由因子模式判定（§1/§29），每个状态用「模式匹配证据」打分，
  再 softmax 成概率分布（软状态 §2b），而非线性加权。
- **软状态**：输出 ``StateBelief``（distribution / argmax_state / entropy / drift）。
  ``argmax_state`` 只作为动作类型锚点，熵高时显式标 ``uncertain`` 并触发研究任务，
  不硬贴标签（§2b.3 / §2b.5）。
- **E > T > F**（§4.3）：S3→S4 判定优先看 ``e_revision`` 减速/转负，再看
  ``t_relative_strength`` 破坏，最后才看 ``f_fundamental_trend``（财报滞后）。
- **共振/背离**（§4.4）：F↑E↑T↑ → ``resonant``；F 强但 E/T 弱（业绩好股价不涨）
  → ``divergent``（触发研究）；背离是 S3→S4 与 EmergencyRiskStop 的前置信号。
- **迁移合法性**（§4.5）：相邻前进 / 反向降级合法，跳级 ``legal=False``；
  S3 Late → S1' 第二曲线标 ``kind="reopen"``。

阈值均为结构性示意（§6.3），集中在模块顶部常量，便于回测调参。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from alphabee.midterm.models import (
    Consistency,
    FactorDelta,
    ResearchTask,
    StateBelief,
    StateTransition,
    VariableScores,
)

# ─────────────────────────────────────────────────────────────────────────────
# 结构性示意阈值 / 参数（§6.3，应回测）
# ─────────────────────────────────────────────────────────────────────────────

_S0_BASELINE = 0.30  # S0（研究候选）基线证据
_SOFTMAX_TEMPERATURE = 0.5  # softmax 温度（越小分布越尖）
_ENTROPY_UNCERTAIN = 1.0  # 熵（nats）超过此值 → uncertain
_UP_T = 0.3  # 方向分「上行」阈值
_DOWN_T = -0.3  # 方向分「下行」阈值
_ACTIVE_T = 0.3  # trigger_factors：|方向分| 超过此值才计入「活跃因子」
_DRIFT_EPS = 1e-3  # drift 忽略阈（概率质量变化小于此值不计入）

_STATES = ("S0", "S1", "S2", "S3", "S4", "S5")
_FACTOR_ORDER = ("F", "E", "T", "V", "C", "R")

# 合法迁移：相邻前进 + 反向降级 + S3→S1'（第二曲线 reopen，§4.5 / §8）
_FORWARD = {("S0", "S1"), ("S1", "S2"), ("S2", "S3"), ("S3", "S4"), ("S4", "S5")}
_BACKWARD = {("S1", "S0"), ("S2", "S1"), ("S3", "S2"), ("S4", "S3"), ("S5", "S4")}
_REOPEN = {("S3", "S1")}  # S3 Late → S3 Acceleration → S1'（第二增长曲线）


# ─────────────────────────────────────────────────────────────────────────────
# 纯函数辅助
# ─────────────────────────────────────────────────────────────────────────────


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _up(x: float | None) -> float:
    """方向分「上行」强度（缺失 → 0，不制造虚假证据）。"""
    return _clip01(x) if x is not None else 0.0


def _down(x: float | None) -> float:
    """方向分「下行」强度（缺失 → 0）。"""
    return _clip01(-x) if x is not None else 0.0


def _not_up(x: float | None) -> float:
    """方向分「非上行」强度（缺失 → 0.5 中性；x≤0 → 1；x→1 → 0）。"""
    if x is None:
        return 0.5
    return 1.0 - _up(x)


def _softmax(evidence: dict[str, float], temperature: float) -> dict[str, float]:
    """数值稳定的 softmax → 概率分布（和为 1）。"""
    keys = list(evidence)
    scaled = [evidence[k] / temperature for k in keys]
    m = max(scaled)
    exps = [math.exp(v - m) for v in scaled]
    total = sum(exps)
    return {k: e / total for k, e in zip(keys, exps)}


def _entropy(distribution: dict[str, float]) -> float:
    """分布熵（nats）。"""
    return -sum(p * math.log(p) for p in distribution.values() if p > 0.0)


def _triad_consistency(f: float | None, e: float | None, t: float | None) -> Consistency:
    """F/E/T 三因子一致性（§4.4）。

    - ``resonant``：F↑E↑T↑ 同向；
    - ``divergent``：两类背离——
      ① F 强但 E/T 弱（业绩好股价不涨，本身即 Conflict）；
      ② E 强上修但 T 走弱（业绩上修 vs 价格走弱，§20 顶部顺序 E→T→F 的第二步「T 弱」）；
    - ``independent``：其余（含任一因子缺失 → 无法判定）。
    """
    if f is None or e is None or t is None:
        return Consistency.INDEPENDENT
    f_up = f > _UP_T
    e_up = e > _UP_T
    t_up = t > _UP_T
    e_dn = e < _DOWN_T
    t_dn = t < _DOWN_T
    if f_up and e_up and t_up:
        return Consistency.RESONANT
    if f_up and not e_up and not t_up and (e_dn or t_dn):
        return Consistency.DIVERGENT
    if e_up and t_dn:
        return Consistency.DIVERGENT  # E↑ + T↓ 背离（具名 conflict）
    return Consistency.INDEPENDENT


def _divergence_reason(f: float | None, e: float | None, t: float | None) -> str | None:
    """返回具名背离描述（无背离 → ``None``），供 rationale / 研究触发 / explore_conflicts 消费。

    与 :func:`_triad_consistency` 的两类 ``divergent`` 分支一一对应，产出可读的具名冲突，
    避免把 E↑T↓ 这类背离埋进笼统的「熵高」。
    """
    if f is None or e is None or t is None:
        return None
    f_up = f > _UP_T
    e_up = e > _UP_T
    t_up = t > _UP_T
    e_dn = e < _DOWN_T
    t_dn = t < _DOWN_T
    if f_up and not e_up and not t_up and (e_dn or t_dn):
        return "业绩好但股价不涨：F 强而 E/T 弱"
    if e_up and t_dn:
        return "业绩上修 vs 股价相对走弱：E 强上修而 T 短期走弱"
    return None


def _active_factors(scores: VariableScores) -> list[str]:
    """返回 |方向分| 超过阈值的活跃因子（按 F/E/T/V/C/R 顺序），供 trigger_factors。"""
    values = {
        "F": scores.f_fundamental_trend,
        "E": scores.e_revision,
        "T": scores.t_relative_strength,
        "V": scores.v_valuation_percentile,
        "C": scores.c_crowding,
        "R": scores.r_risk,
    }
    return [k for k in _FACTOR_ORDER if (v := values[k]) is not None and abs(v) > _ACTIVE_T]


# ─────────────────────────────────────────────────────────────────────────────
# 状态模式证据（§4.2）——不求和，按模式打分
# ─────────────────────────────────────────────────────────────────────────────


def _state_evidence(scores: VariableScores) -> dict[str, float]:
    """按 §4.2 因子模式为 S0–S5 计算模式匹配证据。

    方向分缺失时对应项为 0（不制造虚假证据），但「非上行」门控对缺失取 0.5 中性；
    缺失导致的模糊最终通过熵升高 → ``uncertain`` 体现，而非硬贴标签。
    """
    f = scores.f_fundamental_trend
    e = scores.e_revision
    t = scores.t_relative_strength
    v = scores.v_valuation_percentile
    c = scores.c_crowding

    f_up, f_down = _up(f), _down(f)
    e_up, e_down = _up(e), _down(e)
    t_up, t_down = _up(t), _down(t)
    v_up, v_down = _up(v), _down(v)
    _, c_down = _up(c), _down(c)

    e_not_up = _not_up(e)

    return {
        # S0：研究候选基线（无明确可交易预期差）
        "S0": _S0_BASELINE,
        # S1 预期差：E 尚未上修（门控）+ F 先行改善 + 价格弱 + 估值便宜
        "S1": e_not_up * (f_up + 0.5 * t_down + 0.5 * v_up),
        # S2 证据确认：E 上修（门控）+ F 改善 + T 尚未共振
        "S2": e_up * (f_up + _not_up(t)),
        # S3 共识扩散：F↑E↑T↑ 三共振（强度 = 最弱一环 × 3）
        "S3": 3.0 * min(f_up, e_up, t_up),
        # S4 充分定价：E 停上修/转负（门控）+ T 弱 + V 贵 + C 拥挤（不等 F，§4.3）
        "S4": e_not_up * (t_down + v_down + c_down),
        # S5 退出：E 持续下修 + F 恶化
        "S5": e_down + 0.5 * f_down,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 迁移合法性（§4.5）
# ─────────────────────────────────────────────────────────────────────────────


def _transition(
    from_state: str,
    to_state: str,
    scores: VariableScores,
    consistency: Consistency,
) -> StateTransition:
    """由上一帧 argmax 到本帧 argmax 的迁移判定（合法 / 跳级 / reopen）。"""
    if from_state == to_state:
        legal, kind = True, "stay"
    elif (from_state, to_state) in _REOPEN:
        legal, kind = True, "reopen"
    elif (from_state, to_state) in _FORWARD:
        legal, kind = True, "forward"
    elif (from_state, to_state) in _BACKWARD:
        legal, kind = True, "downgrade"
    else:
        legal, kind = False, "illegal"
    return StateTransition(
        from_state=from_state,
        to_state=to_state,
        legal=legal,
        trigger_factors=_active_factors(scores),
        kind=kind,
        consistency=consistency,
    )


def _drift(distribution: dict[str, float], previous: StateBelief | None) -> dict[str, float] | None:
    """相比上一帧的概率质量流向（无上一帧 / 变化可忽略 → ``None``）。"""
    if previous is None:
        return None
    drift = {
        s: distribution[s] - previous.distribution.get(s, 0.0)
        for s in _STATES
        if abs(distribution[s] - previous.distribution.get(s, 0.0)) > _DRIFT_EPS
    }
    return drift or None


def _research_tasks(
    uncertain: bool,
    divergent: bool,
    entropy: float,
    divergence_reason: str | None = None,
) -> list[ResearchTask]:
    """熵高 / 方向冲突（divergent）时触发的研究任务（§2b.5 / §20）。

    ``divergence_reason`` 为具名背离描述（``_divergence_reason``），落入研究任务的
    ``unknown``，供下游 ``explore_conflicts`` 消费（不再只是一句笼统话）。
    """
    tasks: list[ResearchTask] = []
    if divergent:
        unknown = divergence_reason or "业绩好但股价不涨：F 强而 E/T 弱"
        tasks.append(
            ResearchTask(
                id="research-divergent",
                unknown=f"{unknown}，市场知道什么我们不知道（§20/§8）",
                importance="high",
                decides=["thesis_state"],
                status="open",
            )
        )
    if uncertain and entropy > _ENTROPY_UNCERTAIN:
        tasks.append(
            ResearchTask(
                id="research-uncertain",
                unknown="因子模式不标准、状态无法唯一归类（熵高），需补充证据后再定动作",
                importance="medium",
                decides=["thesis_state"],
                status="open",
            )
        )
    return tasks


# ─────────────────────────────────────────────────────────────────────────────
# 结果载体
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ClassifierResult:
    """classify_state 的结果：软状态 + 迁移 + 因子一致性 + 不确定信号。"""

    state: StateBelief
    transition: StateTransition | None
    factor_deltas: list[FactorDelta]
    uncertain: bool
    research_tasks: list[ResearchTask] = field(default_factory=list)
    rationale: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────────────


def classify_state(
    scores: VariableScores,
    *,
    previous: StateBelief | None = None,
) -> ClassifierResult:
    """把七因子方向分（因子模式）确定性分类为软状态 + 状态迁移。

    Args:
        scores: 七方向分（``alphabee.midterm.score_engine.compress_scores`` 产物）。
        previous: 上一帧软状态（``StateBelief``）；提供时计算 ``drift`` 与
            ``StateTransition``（含迁移合法性判定）。

    Returns:
        :class:`ClassifierResult`：``state`` 为软状态概率分布；``transition`` 为
        迁移判定（无 ``previous`` 时为 ``None``）；``factor_deltas`` 为逐因子
        一致性标注；``uncertain`` 为熵高或方向冲突信号；``research_tasks`` 为
        触发的关键未知（无 dead-end，由 decision_model 消费）。
    """
    evidence = _state_evidence(scores)
    distribution = _softmax(evidence, _SOFTMAX_TEMPERATURE)
    argmax = max(_STATES, key=lambda s: distribution.get(s, 0.0))
    entropy = _entropy(distribution)

    consistency = _triad_consistency(scores.f_fundamental_trend, scores.e_revision, scores.t_relative_strength)
    divergent = consistency == Consistency.DIVERGENT
    divergence_reason = (
        _divergence_reason(scores.f_fundamental_trend, scores.e_revision, scores.t_relative_strength)
        if divergent
        else None
    )
    uncertain = divergent or entropy > _ENTROPY_UNCERTAIN

    # 逐因子一致性标注（§4.4）：F/E/T 用三因子一致性，V/C/R 为 independent
    factor_scores = {
        "F": scores.f_fundamental_trend,
        "E": scores.e_revision,
        "T": scores.t_relative_strength,
        "V": scores.v_valuation_percentile,
        "C": scores.c_crowding,
        "R": scores.r_risk,
    }
    factor_deltas = [
        FactorDelta(
            factor=name,
            delta=factor_scores[name],
            consistency=consistency if name in ("F", "E", "T") else Consistency.INDEPENDENT,
        )
        for name in _FACTOR_ORDER
    ]

    transition = _transition(previous.argmax_state, argmax, scores, consistency) if previous else None

    state = StateBelief(
        distribution=distribution,
        argmax_state=argmax,
        entropy=entropy,
        drift=_drift(distribution, previous),
    )

    rationale = [
        f"argmax={argmax}（因子模式：F={scores.f_fundamental_trend}, E={scores.e_revision}, "
        f"T={scores.t_relative_strength}）",
        f"entropy={entropy:.3f}, consistency={consistency.value}",
    ]
    if divergence_reason:
        rationale.append(f"具名冲突={divergence_reason}（consistency=divergent）")
    if uncertain:
        rationale.append("uncertain=True（熵高或方向冲突，触发研究而非直接加仓）")

    return ClassifierResult(
        state=state,
        transition=transition,
        factor_deltas=factor_deltas,
        uncertain=uncertain,
        research_tasks=_research_tasks(uncertain, divergent, entropy, divergence_reason),
        rationale=rationale,
    )
