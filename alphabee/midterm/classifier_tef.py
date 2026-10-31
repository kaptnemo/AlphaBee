"""Layer 2a 分类器变体：T→E→F 顶部判定顺序（design MIDTERM_CLASSIFIER_TEF_DESIGN §3）。

与 :mod:`alphabee.midterm.classifier`（E→T→F 基准）完全独立：本模块只 import
``alphabee.midterm.models``，不 import classifier；阈值常量与中性纯函数为**刻意复制**
（对比隔离），由 ``tests/midterm/test_classifier_parity.py`` 钉住与 ETF 的中性口径相等。

顺序差异落点（§3.2/§3.3）：

- S2 门控 E→T、S4 门控 E→T、S5 加 T 门控；
- 背离 case ② 镜像为 T↑+E↓；E↑+T↓ 不再具名背离（T 破位由 S4 路径承接）。

已披露取舍（§3.2 S5）：TEF S5 要求价格确认（``t_down>0``），代价是「非价格证伪
（造假/政策突变，价格尚未反应）」时退出**更慢**；收益是主题退潮式破位退出**更早**、
且大幅抑制「E 单因子噪声触发 S5」的假退出。这是 T→E→F 哲学的忠实表达。
"""

from __future__ import annotations

import math

from alphabee.midterm.models import (
    ClassifierResult,
    Consistency,
    FactorScoreDelta,
    ResearchTask,
    StateBelief,
    StateTransition,
    VariableScores,
)

# ─────────────────────────────────────────────────────────────────────────────
# 结构性示意阈值 / 参数（与 classifier 同值，parity 钉；刻意复制，对比隔离）
# ─────────────────────────────────────────────────────────────────────────────

_S0_BASELINE = 0.30
_SOFTMAX_TEMPERATURE = 0.5
_ENTROPY_UNCERTAIN = 1.0
_UP_T = 0.3
_DOWN_T = -0.3
_ACTIVE_T = 0.3
_DRIFT_EPS = 1e-3

_STATES = ("S0", "S1", "S2", "S3", "S4", "S5")
_FACTOR_ORDER = ("F", "E", "T", "V", "C", "R")

_FORWARD = {("S0", "S1"), ("S1", "S2"), ("S2", "S3"), ("S3", "S4"), ("S4", "S5")}
_BACKWARD = {("S1", "S0"), ("S2", "S1"), ("S3", "S2"), ("S4", "S3"), ("S5", "S4")}
_REOPEN = {("S3", "S1")}


# ─────────────────────────────────────────────────────────────────────────────
# 纯函数辅助（与 classifier 同语义）
# ─────────────────────────────────────────────────────────────────────────────


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _up(x: float | None) -> float:
    return _clip01(x) if x is not None else 0.0


def _down(x: float | None) -> float:
    return _clip01(-x) if x is not None else 0.0


def _not_up(x: float | None) -> float:
    if x is None:
        return 0.5
    return 1.0 - _up(x)


def _softmax(evidence: dict[str, float], temperature: float) -> dict[str, float]:
    keys = list(evidence)
    scaled = [evidence[k] / temperature for k in keys]
    m = max(scaled)
    exps = [math.exp(v - m) for v in scaled]
    total = sum(exps)
    return {k: e / total for k, e in zip(keys, exps)}


def _entropy(distribution: dict[str, float]) -> float:
    return -sum(p * math.log(p) for p in distribution.values() if p > 0.0)


def _triad_consistency(f: float | None, e: float | None, t: float | None) -> Consistency:
    """F/E/T 三因子一致性（TEF §3.3 镜像）。

    - ``resonant``：F↑E↑T↑ 同向（顺序无关，不变）；
    - ``divergent`` ①：F 强但 E/T 弱（业绩好股价不涨，顺序无关，不变）；
    - ``divergent`` ②：**T↑ + E↓**（价格先行转强 vs 预期未跟上，A 股题材行情常态）；
      ``E↑ + T↓`` 在 TEF 判 ``independent``（T 破位由 S4 路径直接承接，不再具名背离）；
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
    if t_up and e_dn:
        return Consistency.DIVERGENT  # T↑ + E↓ 背离（镜像）
    return Consistency.INDEPENDENT


def _divergence_reason(f: float | None, e: float | None, t: float | None) -> str | None:
    """具名背离描述（TEF §3.3 镜像文案）。"""
    if f is None or e is None or t is None:
        return None
    f_up = f > _UP_T
    e_up = e > _UP_T
    t_up = t > _UP_T
    e_dn = e < _DOWN_T
    t_dn = t < _DOWN_T
    if f_up and not e_up and not t_up and (e_dn or t_dn):
        return "业绩好但股价不涨：F 强而 E/T 弱"
    if t_up and e_dn:
        return "价格先行转强 vs 预期未跟上：T 转强而 E 未上修"
    return None


def _active_factors(scores: VariableScores) -> list[str]:
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
# 状态模式证据（TEF §3.2）
# ─────────────────────────────────────────────────────────────────────────────


def _state_evidence(scores: VariableScores) -> dict[str, float]:
    """按 §3.2 TEF 公式为 S0–S5 计算模式匹配证据。

    与 ETF 的差异只在顺序决定性位置：S2 门控 E→T、S4 门控 E→T、S5 加 T 门控；
    S0/S1/S3 与 ETF 逐值相等。
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
        "S0": _S0_BASELINE,
        "S1": e_not_up * (f_up + 0.5 * t_down + 0.5 * v_up),
        # S2 证据确认：T 先行（门控 T），F 改善 + E 尚未共振
        "S2": t_up * (f_up + _not_up(e)),
        "S3": 3.0 * min(f_up, e_up, t_up),
        # S4 充分定价：T 破位先导（门控 T），不等 E 转平
        "S4": t_down * (e_not_up + v_down + c_down),
        # S5 退出：T 破位先导（加 T 门控），再看 E 下修 + F 恶化
        "S5": t_down * (e_down + 0.5 * f_down),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 迁移合法性（与 classifier 同构）
# ─────────────────────────────────────────────────────────────────────────────


def _transition(
    from_state: str,
    to_state: str,
    scores: VariableScores,
    consistency: Consistency,
) -> StateTransition:
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
# 主入口
# ─────────────────────────────────────────────────────────────────────────────


def classify_state(
    scores: VariableScores,
    *,
    previous: StateBelief | None = None,
) -> ClassifierResult:
    """与 classifier.classify_state 同签名、同返回类型；状态证据公式按 §3.2 TEF 列。"""
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

    factor_scores = {
        "F": scores.f_fundamental_trend,
        "E": scores.e_revision,
        "T": scores.t_relative_strength,
        "V": scores.v_valuation_percentile,
        "C": scores.c_crowding,
        "R": scores.r_risk,
    }
    factor_deltas = [
        FactorScoreDelta(
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
        # 状态证据 V2 design §5.4（additive）：透出内部已算出的一致性/具名背离（v1 之前丢掉）。
        # 只透出、不参与任何计算 ⇒ v1 公式与数值行为零变化（parity 由 test_classifier_parity.py 钉）。
        factor_consistency=consistency.value,
        divergence_reason=divergence_reason or "",
    )
