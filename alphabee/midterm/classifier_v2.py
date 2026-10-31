"""Layer 2a' 分类器 V2：修正状态证据的形式缺陷（design MIDTERM_STATE_EVIDENCE_V2_DESIGN §4/§5）。

顺序哲学（T→E→F / E→T→F）不变，只改「证据 → 归一化 → softmax」的形式。与 v1
（:mod:`alphabee.midterm.classifier` / :mod:`alphabee.midterm.classifier_tef`）的差异：

- **D1**：S1/S2 用双侧 :func:`neutral_band` 取代单边 ``_not_up`` —— v1 里 E 越负、S1/S2
  证据反而越高（「恶化 = 确认」），这是符号方向错误而非调参问题；
- **D2**：``S5 = max(价格路径, F×E 双击证伪路径)``，退出不再被 ``t_down`` 独占 —— v1-tef
  在 ``T ≥ 0`` 时 ``S5 ≡ 0``，退出在数学上不可达；
- **D3**：证据按状态可达上限 :data:`EVIDENCE_CAPS` 归一化后再 softmax —— v1 各状态上限
  不齐（S3=3.0 / S5=1.5 / S0=0.30），softmax 内建未文档化的状态先验，熵不可跨状态比较；
- **D4**：``uncertain`` 改由 ``P_top`` 判定 + 结构化 :class:`AdverseDivergence`（顶背离）。

**语义声明（§4.5 / §11.2 R1）**：``P`` 是「归一化模式证据的 softmax 权重」，**不是频率
概率**，也**不是 ``P(H|E)`` 后验**；``P_top = max(P)`` 叫「状态确信度」只是沿用 v1 命名，
不可当置信区间。``H_norm = H_nats / ln(6)`` ∈[0,1] 仅是分布离散度。

**治理约定（§3.2 / §11.2 R4）**：v1（``classifier.py`` / ``classifier_tef.py``）**冻结**，
不再加功能、不改数值；本模块是**唯一演进分支**。v1/v2 共享的阈值常量与迁移合法性集合由
``tests/midterm/test_classifier_v2_parity.py`` 钉同值（防双实现漂移）。

自包含（§5.1）：本模块只 import ``alphabee.midterm.models``，不 import v1 两个分类器模块。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from alphabee.midterm.models import (
    AdverseDivergence,
    ClassifierResult,
    Consistency,
    FactorScoreDelta,
    ResearchTask,
    StateBelief,
    StateTransition,
    VariableScores,
)

FORMULA_REVISION = "v2"

# ─────────────────────────────────────────────────────────────────────────────
# 结构性参数（§4.6 / 附录 A）：集中在模块顶部，不暴露给 config（§7.3 校准纪律）
#
# 校准证据（§7.3 校准规程 = 唯一允许调参的通道；2026-10-31 复核）：
#   语料：``data/midterm/state/*.jsonl`` 的 **2 帧**真实帧（300760.SZ / 603259.SH，
#         as_of=2026-09-24；均为 v1 帧、无 ``classifier_order`` ⇒ 按 "etf" 离线复算 v2）。
#         语料 < 50 帧 ⇒ 按 §7.3「语料不足时降级」：只跑步骤 2/4 的形状约束，
#         ``UNCERTAIN_P_TOP`` 保留初值并登记「待校准」；网格只用于不变量/关键格比例。
#   TEMPERATURE（步骤 2，只看形状、不看 uncertain 率）：候选 {0.18,0.20,0.22,0.24,0.26} 实测
#         (a) 完美多空 P_top 差 = 0.0728 / 0.0761 / 0.0767 / 0.0755 / 0.0729（均 ≤ 0.10）；
#         (b) 600760 实帧 P_top = 0.8763 / 0.8358 / 0.7945 / 0.7540 / 0.7153（≥ 0.75 到 0.24）；
#         (c) 语料 H_norm 中位数 = 0.7260 / 0.7686 / 0.8035 / 0.8321 / 0.8555。
#         0.22 满足 (a)(b)（差 0.0767、P_top 0.7945）；(c) 在本 2 帧语料上为 0.8035 > 0.75，
#         但 2 帧中位数无统计意义（单帧 0.9403 拉高），且 §7.3「满足约束后取 (b) 裕度最大者」
#         对 T 单调（T 越小 ⇒ 裕度越大、H_norm 越低），字面执行会恒取最小候选、无法复现设计
#         冻结值 0.22。故按 §7.3 降级：0.22（设计冻结初值）经形状约束 (a)(b) 确认后保留，
#         (c) 标记「待校准」（真实语料 ≥ 50 帧后复核）。
#   UNCERTAIN_P_TOP（步骤 3，须在 TEMPERATURE 冻结后）：2 帧 P_top = {0.3168, 0.6284}，
#         (1−30%) 最近秩分位 ≈ 0.6284；在 0.55 下的实际 uncertain 率 = 50%（1/2，§5.3 判据）。
#         语料 < 50 ⇒ 保留初值 0.55 并登记「待校准」（不以 2 帧分位数替代）。
#   BREAK_LAMBDA（步骤 4）：关键格（F≤−0.5 ∧ E≤−0.3 ∧ T≥0.3，5^6 网格 1000 格）S5 argmax 占比
#         λ=1.0→75.0%/66.0%、1.2→87.5%/75.5%、1.3→100%/83.0%、1.5→100%/95.0%、
#         2.0→100%/99.0%（tef/etf）；「仅 F 崩」（F∈{−1,−0.5}、E≥−0.2、T≥0，2250 点）
#         max P(S5)=0.1122 ≤ 0.2，且对 λ 不敏感（E≥−0.2 ⇒ se=0 ⇒ 证伪项恒 0；若价格破位
#         T<0 则该集合 max P(S5)=0.2826，但那是价格路径退出、不属「仅 F 崩」）。
#         两约束可行下界 λ≈1.31（tef/etf 关键格均 ≥90%），默认 2.0 位于可行域内 ⇒ 保留。
#   结论：TEMPERATURE / UNCERTAIN_P_TOP / BREAK_LAMBDA 均保留设计默认值——
#         **默认值经语料校准确认**（TEMPERATURE 由形状约束 (a)(b) 确认、BREAK_LAMBDA 由关键格
#         与「仅 F 崩」约束确认；UNCERTAIN_P_TOP 与 TEMPERATURE 的语料分位数门因语料仅 2 帧
#         （<50，§7.3 降级）标记「待校准」，待真实语料 ≥50 帧后复核）。
# ─────────────────────────────────────────────────────────────────────────────

S0_BASELINE = 0.30  # S0 基线证据（沿用 v1 ``_S0_BASELINE``，parity 钉）
S0_FLOOR = 0.30  # S0 底噪下限：neutrality=0 时仍保留 30% 基线（「矛盾信号 ⇒ 研究候选」）
W_E = 0.60  # E 双侧中性带半宽（≈ 一次「中性偏空」的 E）
W_T = 0.80  # T 双侧中性带半宽（仅 etf 的 S2 用）
W_F = 1.00  # F 双侧中性带半宽（仅 S0 的 neutrality 用）
SEVERE_LO = 0.25  # 负侧严重度死区下界：|x| ≤ LO 不计证伪 / 顶背离
SEVERE_HI = 0.60  # 负侧严重度饱和上界
BREAK_LAMBDA = 2.0  # F×E 双击证伪通路权重（S5 第二条路径；关键格/仅 F 崩约束确认，见上）
TEMPERATURE = 0.22  # 归一化证据上的 softmax 温度（§7.3 形状约束确认，非网格调参，见上）
UNCERTAIN_P_TOP = 0.55  # uncertain 阈值：P_top < 阈值 ⇒ 无状态主导（语料 2 帧<50 ⇒ 待校准）

# 与 v1 逐值同构的共享阈值 / 迁移集合（§5.4 / §11.2 R4，由 parity 测试钉同值）
_S0_BASELINE = S0_BASELINE  # v1 私有名同值别名（仅供 parity 测试钉 R4）
_UP_T = 0.3  # 方向分「上行」阈值（与 v1 同值）
_DOWN_T = -0.3  # 方向分「下行」阈值（与 v1 同值）
_ACTIVE_T = 0.3  # trigger_factors：|方向分| 超过此值才计入「活跃因子」（与 v1 同值）
_DRIFT_EPS = 1e-3  # drift 忽略阈（概率质量变化小于此值不计入，与 v1 同值）

_STATES = ("S0", "S1", "S2", "S3", "S4", "S5")
_FACTOR_ORDER = ("F", "E", "T", "V", "C", "R")
_ORDERS = ("etf", "tef")

# 合法迁移：相邻前进 + 反向降级 + S3→S1'（第二曲线 reopen），与 v1 逐值同构（§4.5 / §8）
_FORWARD = {("S0", "S1"), ("S1", "S2"), ("S2", "S3"), ("S3", "S4"), ("S4", "S5")}
_BACKWARD = {("S1", "S0"), ("S2", "S1"), ("S3", "S2"), ("S4", "S3"), ("S5", "S4")}
_REOPEN = {("S3", "S1")}  # S3 Late → S3 Acceleration → S1'（第二增长曲线）


@dataclass(frozen=True)
class EvidenceParams:
    """V2 证据层参数（§4.6）；字段默认值即 :data:`DEFAULT_PARAMS`（§7.3 校准冻结后写入常量区）。

    参数只在此处集中，``classify_state`` / ``state_evidence`` / ``distribution`` 均以
    ``params`` 透传，便于回测与 §7.3 校准（不改模块常量即可复算）。
    """

    s0_baseline: float = S0_BASELINE
    s0_floor: float = S0_FLOOR
    w_e: float = W_E
    w_t: float = W_T
    w_f: float = W_F
    severe_lo: float = SEVERE_LO
    severe_hi: float = SEVERE_HI
    break_lambda: float = BREAK_LAMBDA
    temperature: float = TEMPERATURE
    uncertain_p_top: float = UNCERTAIN_P_TOP


DEFAULT_PARAMS = EvidenceParams()

# §4.4 证据上限 = 各状态公式在 [-1,1]^6 上的**可达最大值**（结构性常量，不参与调参）。
EVIDENCE_CAPS: dict[str, float] = {"S0": 0.30, "S1": 2.0, "S2": 2.0, "S3": 3.0, "S4": 3.0, "S5": 2.0}

# S5 的可达上限 = max(价格路径上限 1.5, 证伪通路权重 λ)；导入期断言固化，防「改了参数而 cap 未同步」
# 导致归一化静默把 S5 压到 < 1（cap 漂移会直接破坏 I4 的「每状态可达 1.0」不变量）。
_S5_PRICE_PATH_MAX = 1.5
assert EVIDENCE_CAPS["S5"] == max(_S5_PRICE_PATH_MAX, DEFAULT_PARAMS.break_lambda), (
    "EVIDENCE_CAPS['S5'] 必须等于 max(1.5, BREAK_LAMBDA)：改 BREAK_LAMBDA 时必须同步改 cap"
)

# §5.2 具名背离文案：① 为 v2 新增（顶背离），②③ 逐字沿用 v1（下游可能按文案做关键词匹配）。
_REASON_ADVERSE = "基本面与预期同时实质恶化而价格仍强（顶背离）"
_REASON_F_STRONG = "业绩好但股价不涨：F 强而 E/T 弱"
_REASON_TEF_PRICE_LEAD = "价格先行转强 vs 预期未跟上：T 转强而 E 未上修"
_REASON_ETF_EARNINGS_UP = "业绩上修 vs 股价相对走弱：E 强上修而 T 短期走弱"


# ─────────────────────────────────────────────────────────────────────────────
# §4.2 算子（纯函数；缺失值口径与 v1 对齐）
# ─────────────────────────────────────────────────────────────────────────────


def clip01(x: float) -> float:
    """把数值夹到 [0,1]。"""
    return max(0.0, min(1.0, x))


def up(x: float | None) -> float:
    """方向分「上行」强度；缺失 → 0（不制造虚假证据）。"""
    return clip01(x) if x is not None else 0.0


def dn(x: float | None) -> float:
    """方向分「下行」强度；缺失 → 0。"""
    return clip01(-x) if x is not None else 0.0


def neutral_band(x: float | None, w: float) -> float:
    """双侧中性带（§4.2）：``|x| ≥ w → 0``；``x = 0 → 1``；中间线性；缺失 → 0.5。

    两侧都衰减是本算子与单边 ``not_yet`` 的关键差别：只有它才能修掉「E 越负、S1/S2 证据
    越高」（D1）。缺失给 0.5 与 v1 ``_not_up(None) = 0.5`` 同口径 —— 给「信息不足」半分
    信用，既不判死也不满额确认；缺失本身仍通过熵与 ``degraded`` / ``missing_facts`` 暴露。
    """
    if x is None:
        return 0.5
    return clip01(1.0 - abs(x) / w)


def not_yet(x: float | None, w: float) -> float:
    """单侧「尚未上修」（§4.2）：``1 − max(0, x)/w``；缺失 → 0.5。

    刻意保留单边语义：它只用于 S4 的「预期停止上修」，E 转负完全满足该语义。若改成双侧，
    S4 会在 E 崩盘时反而下降（语义反转，§11.1 已拒绝）。``x > w`` 时结果为负值（反证据），
    由 :func:`normalized_evidence` 的 ``clip01`` 归零，故不影响分布的有界性。
    """
    if x is None:
        return 0.5
    return 1.0 - max(0.0, x) / w


def severe(x: float | None, *, lo: float = SEVERE_LO, hi: float = SEVERE_HI) -> float:
    """仅负侧的严重度（§4.2）：``x ≥ 0 → 0``；``|x| ≤ lo → 0``；``|x| ≥ hi → 1``；中间线性；缺失 → 0。

    用严重度而非符号，是为了不把「单季轻微负增长」判成证伪 / 顶背离（§5.2 注）；缺失取 0
    而不是 0.5 —— 不制造证伪证据（与 ``neutral_band`` 的缺失口径刻意不同）。
    """
    if x is None or x >= 0.0:
        return 0.0
    magnitude = -x
    if magnitude <= lo:
        return 0.0
    if magnitude >= hi:
        return 1.0
    return (magnitude - lo) / (hi - lo)


def _validate_order(order: str) -> None:
    """``order`` 只接受 ``"etf" | "tef"``；非法值 raise（dispatch 已 fail-open，模块内不重复兜底）。"""
    if order not in _ORDERS:
        raise ValueError(f"未知 order={order!r}，仅支持 {_ORDERS}")


# ─────────────────────────────────────────────────────────────────────────────
# §4.3 逐状态证据（不求和，按模式打分）
# ─────────────────────────────────────────────────────────────────────────────


def state_evidence(scores: VariableScores, *, order: str, params: EvidenceParams = DEFAULT_PARAMS) -> dict[str, float]:
    """按 §4.3 计算 S0–S5 的模式匹配证据（未归一化）。

    记 ``f,e,t,v,c`` 为方向分；``f_up=up(f)``、``e_dn=dn(e)`` …；``nf/ne/nt`` 为三因子
    双侧中性带；``sf=severe(f)``、``se=severe(e)``：

    - ``S0 = S0_BASELINE × (S0_FLOOR + (1−S0_FLOOR) × nf·ne·nt)``（研究候选，含「无信号」）
    - ``S1 = ne × (f_up + 0.5·t_dn + 0.5·v_up) × (1−sf)``（F 严重恶化时不叫预期差）
    - ``S2 = t_up × (f_up + ne)``（tef，价格先确认）/ ``e_up × (f_up + nt)``（etf）
    - ``S3 = 3 × min(f_up, e_up, t_up)``（三共振，min 天然幅度渐变，保持 v1）
    - ``S4 = t_dn × (not_yet(e) + v_dn + c_dn)``（tef）/ ``not_yet(e) × (t_dn + v_dn + c_dn)``（etf）
    - ``S5 = max(t_dn × (e_dn + 0.5·f_dn), BREAK_LAMBDA × se × sf)``（价格破位 OR 双击证伪）

    S5 用 ``max`` 而非加法：避免「价格破位 + 双击」同时满足时证据被重复放大，且两条路径各自单调。
    """
    _validate_order(order)

    f = scores.f_fundamental_trend
    e = scores.e_revision
    t = scores.t_relative_strength
    v = scores.v_valuation_percentile
    c = scores.c_crowding

    f_up, f_dn = up(f), dn(f)
    e_up, e_dn = up(e), dn(e)
    t_up, t_dn = up(t), dn(t)
    v_up, v_dn = up(v), dn(v)
    _, c_dn = up(c), dn(c)

    nf = neutral_band(f, params.w_f)
    ne = neutral_band(e, params.w_e)
    nt = neutral_band(t, params.w_t)
    sf = severe(f, lo=params.severe_lo, hi=params.severe_hi)
    se = severe(e, lo=params.severe_lo, hi=params.severe_hi)

    if order == "tef":
        s2 = t_up * (f_up + ne)  # 价格先确认，预期尚未共振
        s4 = t_dn * (not_yet(e, params.w_e) + v_dn + c_dn)  # 价格破位先导，不等 E 转平
    else:
        s2 = e_up * (f_up + nt)  # 业绩先确认，价格尚未共振
        s4 = not_yet(e, params.w_e) * (t_dn + v_dn + c_dn)  # 预期停上修先导

    return {
        "S0": params.s0_baseline * (params.s0_floor + (1.0 - params.s0_floor) * nf * ne * nt),
        "S1": ne * (f_up + 0.5 * t_dn + 0.5 * v_up) * (1.0 - sf),
        "S2": s2,
        "S3": 3.0 * min(f_up, e_up, t_up),
        "S4": s4,
        "S5": max(t_dn * (e_dn + 0.5 * f_dn), params.break_lambda * se * sf),
    }


# ─────────────────────────────────────────────────────────────────────────────
# §4.4 / §4.5 归一化 → 分布 → 熵
# ─────────────────────────────────────────────────────────────────────────────


def normalized_evidence(evidence: dict[str, float]) -> dict[str, float]:
    """按状态可达上限归一化：``norm[s] = clip01(evidence[s] / EVIDENCE_CAPS[s])`` ∈ [0,1]（§4.4）。

    归一化后各状态上限可比，softmax 不再内建「S3 天生 3.0、S5 最多 1.5」的状态先验（D3）；
    ``clip01`` 同时吸收 ``not_yet`` 在 ``x > w`` 时可能产生的负值（反证据 = 0）。
    """
    return {state: clip01(evidence.get(state, 0.0) / EVIDENCE_CAPS[state]) for state in _STATES}


def distribution(norm: dict[str, float], *, params: EvidenceParams = DEFAULT_PARAMS) -> dict[str, float]:
    """归一化证据 → softmax 权重 ``P``（§4.5）：``logits = norm / TEMPERATURE``。

    **语义声明**：``P`` 是「归一化模式证据的 softmax 权重」，**不是频率概率**、**不是
    ``P(H|E)`` 后验**；均匀先验等价于在 logits 上加常数，故省去（沿用 v1 口径）。
    实现上减去 ``max(logits)`` 做数值稳定，不改变结果。
    """
    logits = {state: norm.get(state, 0.0) / params.temperature for state in _STATES}
    peak = max(logits.values())
    weights = {state: math.exp(value - peak) for state, value in logits.items()}
    total = sum(weights.values())
    return {state: weight / total for state, weight in weights.items()}


def entropy_nats(dist: dict[str, float]) -> float:
    """分布熵（nats）；``H_norm = H_nats / ln(6)`` ∈ [0,1]（§4.5）。"""
    return -sum(p * math.log(p) for p in dist.values() if p > 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# §5.2 背离分类学（含新增 adverse 格）+ §6.2 顶背离对象
# ─────────────────────────────────────────────────────────────────────────────


def _divergence(
    scores: VariableScores, *, order: str, params: EvidenceParams
) -> tuple[Consistency, str, AdverseDivergence | None]:
    """F/E/T 三因子一致性 + 具名背离文案 + 顶背离对象（§5.2 优先级从高到低，命中即返回）。

    优先级：

    1. ``sf>0 ∧ se>0 ∧ t_up``（价格仍强而 F/E 同时实质恶化）→ ``divergent`` + 顶背离；
    2. ``f_up ∧ ¬e_up ∧ (e_dn∨t_dn) ∧ ¬t_up`` → ``divergent``（业绩好股价不涨，沿用 v1）；
    3. 顺序型：``tef`` → ``t_up ∧ e_dn``；``etf`` → ``e_up ∧ t_dn`` → ``divergent``；
    4. ``f_up ∧ e_up ∧ t_up`` → ``resonant``；
    5. 其余 → ``independent``。

    优先级 1 最高：它是否定「加仓」的证据强度最大（``S5=0.923`` 帧也是它），必须在报告与
    ``next_evidence_to_watch`` 里排在最前。用 ``severe``（负侧严重度）而非符号判 ①，是为了
    不把「单季轻微负增长」判成顶背离（§5.2 注）。任一 F/E/T 缺失 → ``independent``（与 v1 同口径，
    不制造背离）；此时 ``severe`` 也恒为 0，故不会产出顶背离。

    Returns:
        ``(consistency, reason, adverse)``：``reason`` 无背离时为 ``""``；``adverse`` 仅 ① 非 ``None``。
    """
    f = scores.f_fundamental_trend
    e = scores.e_revision
    t = scores.t_relative_strength
    if f is None or e is None or t is None:
        return Consistency.INDEPENDENT, "", None

    f_up = f > _UP_T
    e_up = e > _UP_T
    t_up = t > _UP_T
    e_dn = e < _DOWN_T
    t_dn = t < _DOWN_T
    sf = severe(f, lo=params.severe_lo, hi=params.severe_hi)
    se = severe(e, lo=params.severe_lo, hi=params.severe_hi)
    price_strength = up(t)

    if sf > 0.0 and se > 0.0 and t_up:
        adverse = AdverseDivergence(
            severity=sf * se * price_strength,  # §6.2：severity = sf × se × t_up
            fundamental_severity=sf,
            expectation_severity=se,
            price_strength=price_strength,
            description=_REASON_ADVERSE,
        )
        return Consistency.DIVERGENT, _REASON_ADVERSE, adverse
    if f_up and not e_up and not t_up and (e_dn or t_dn):
        return Consistency.DIVERGENT, _REASON_F_STRONG, None
    if order == "tef":
        if t_up and e_dn:
            return Consistency.DIVERGENT, _REASON_TEF_PRICE_LEAD, None
    elif e_up and t_dn:
        return Consistency.DIVERGENT, _REASON_ETF_EARNINGS_UP, None
    if f_up and e_up and t_up:
        return Consistency.RESONANT, "", None
    return Consistency.INDEPENDENT, "", None


# ─────────────────────────────────────────────────────────────────────────────
# 迁移合法性 / drift / 活跃因子（与 v1 逐值同构，§5.4）
# ─────────────────────────────────────────────────────────────────────────────


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


def _transition(from_state: str, to_state: str, scores: VariableScores, consistency: Consistency) -> StateTransition:
    """由上一帧 argmax 到本帧 argmax 的迁移判定（合法 / 跳级 / reopen），与 v1 逐值同构。"""
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


def _drift(distribution_map: dict[str, float], previous: StateBelief | None) -> dict[str, float] | None:
    """相比上一帧的概率质量流向（无上一帧 / 变化可忽略 → ``None``），与 v1 逐值同构。"""
    if previous is None:
        return None
    drift = {
        s: distribution_map[s] - previous.distribution.get(s, 0.0)
        for s in _STATES
        if abs(distribution_map[s] - previous.distribution.get(s, 0.0)) > _DRIFT_EPS
    }
    return drift or None


def _research_tasks(
    no_dominant_state: bool,
    divergent: bool,
    divergence_reason: str,
    adverse: AdverseDivergence | None,
) -> list[ResearchTask]:
    """研究任务（§5.4）：``research-adverse`` 新增且排最前，另两个 id / 结构保持 v1 不变。

    ``research-adverse`` 的 ``unknown`` 为背离描述（含 severity），``importance="high"``：
    顶背离是「确定的坏消息」，必须比「不确定」更高优先级地进入研究队列。

    触发口径（与实现逐条对应，不是「每个 uncertain 来源各对应一个任务」的一对一映射）：

    - ``research-divergent`` 对**任何** ``divergent`` 触发（含顶背离，与 v1 同口径、结构不变）；
      顶背离帧**同时**产出 ``research-adverse`` 与 ``research-divergent``，前者排最前。
    - ``research-uncertain`` **仅**由「无状态主导」（``P_top < UNCERTAIN_P_TOP``）触发，不由
      「有背离但不是顶背离」触发：该类帧的出口是 ``research-divergent``，即便其 ``uncertain=True``
      （例：``F=1, E=−0.5, T=−1, V=−1, C=−1`` 的「业绩好股价不涨」帧，``P_top≈0.90`` 但仍
      ``uncertain=True``，任务只有 ``research-divergent``）。

    刻意**不**沿用 v1 的 ``entropy > 1.0`` 强度门：v2 的 uncertain 已不由熵定义，沿用会出现
    「``uncertain=True`` 却没有任何研究任务」的孤儿帧（实测 ``F=E=T=V=C=R=−1``：两个状态各
    0.482 ⇒ ``P_top<0.55`` 但 ``H=0.86<1.0``）。
    """
    tasks: list[ResearchTask] = []
    if adverse is not None:
        tasks.append(
            ResearchTask(
                id="research-adverse",
                unknown=f"{adverse.description}（severity={adverse.severity:.3f}），不作为加仓证据，需核实恶化可持续性",
                importance="high",
                decides=["thesis_state"],
                status="open",
            )
        )
    if divergent:
        unknown = divergence_reason or _REASON_F_STRONG
        tasks.append(
            ResearchTask(
                id="research-divergent",
                unknown=f"{unknown}，市场知道什么我们不知道（§20/§8）",
                importance="high",
                decides=["thesis_state"],
                status="open",
            )
        )
    if no_dominant_state:
        tasks.append(
            ResearchTask(
                id="research-uncertain",
                unknown="因子模式不标准、状态无法唯一归类（无状态主导：P_top 低于阈值），需补充证据后再定动作",
                importance="medium",
                decides=["thesis_state"],
                status="open",
            )
        )
    return tasks


# ─────────────────────────────────────────────────────────────────────────────
# 主入口（§5.1）
# ─────────────────────────────────────────────────────────────────────────────


def classify_state(
    scores: VariableScores,
    *,
    order: str,
    previous: StateBelief | None = None,
    params: EvidenceParams = DEFAULT_PARAMS,
) -> ClassifierResult:
    """把七因子方向分确定性分类为软状态 + 状态迁移（V2 公式）。

    Args:
        scores: 七方向分（``alphabee.midterm.score_engine.compress_scores`` 产物）。
        order: 顺序哲学 ``"tef"``（T→E→F）或 ``"etf"``（E→T→F）；非法值 ``raise ValueError``。
        previous: 上一帧软状态；提供时计算 ``drift`` 与 ``transition``（迁移合法性用
            ``previous.argmax_state``）。
        params: 证据层参数（默认 :data:`DEFAULT_PARAMS`）。

    Returns:
        :class:`ClassifierResult`：``state`` 为软状态分布；``uncertain`` 按 §5.3 判定
        （``P_top < UNCERTAIN_P_TOP`` 或「有背离但不是顶背离」；顶背离算「确定的坏消息」，
        不算 uncertain）；另透出 ``factor_consistency`` / ``divergence_reason`` /
        ``entropy_normalized`` / ``adverse_divergence`` / ``state_evidence``（归一化证据）。
    """
    _validate_order(order)

    evidence = state_evidence(scores, order=order, params=params)
    norm = normalized_evidence(evidence)
    dist = distribution(norm, params=params)
    argmax = max(_STATES, key=lambda s: dist.get(s, 0.0))
    entropy = entropy_nats(dist)
    entropy_normalized = entropy / math.log(len(_STATES))  # §4.5：H_norm = H / ln(6) ∈ [0,1]
    p_top = max(dist.values())

    consistency, divergence_reason, adverse = _divergence(scores, order=order, params=params)
    divergent = consistency == Consistency.DIVERGENT
    no_dominant_state = p_top < params.uncertain_p_top
    # §5.3：顶背离不是「不确定」，而是「确定的坏消息」（产出 research-adverse、允许减仓），
    # 故只在「有背离但不是顶背离」时才并入 uncertain。
    uncertain = no_dominant_state or (divergent and adverse is None)

    # 逐因子一致性标注：F/E/T 用三因子一致性，V/C/R 为 independent（与 v1 同构）
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
        distribution=dist,
        argmax_state=argmax,
        entropy=entropy,
        drift=_drift(dist, previous),
    )

    rationale = [
        f"formula={FORMULA_REVISION}, order={order}",
        f"argmax={argmax}（因子模式：F={scores.f_fundamental_trend}, E={scores.e_revision}, "
        f"T={scores.t_relative_strength}）",
        f"P_top={p_top:.3f}, entropy={entropy:.3f}（归一化 {entropy_normalized:.3f}）, consistency={consistency.value}",
    ]
    if divergence_reason:
        rationale.append(f"具名冲突={divergence_reason}（consistency=divergent）")
    if adverse is not None:
        rationale.append(
            f"顶背离 severity={adverse.severity:.3f}（sf×se×t_up = {adverse.fundamental_severity:.3f}×"
            f"{adverse.expectation_severity:.3f}×{adverse.price_strength:.3f}），不作为加仓证据"
        )
    if uncertain:
        rationale.append("uncertain=True（P_top 低或非顶背离的背离，触发研究而非直接加仓）")

    return ClassifierResult(
        state=state,
        transition=transition,
        factor_deltas=factor_deltas,
        uncertain=uncertain,
        research_tasks=_research_tasks(no_dominant_state, divergent, divergence_reason, adverse),
        rationale=rationale,
        factor_consistency=consistency.value,
        divergence_reason=divergence_reason,
        entropy_normalized=entropy_normalized,
        adverse_divergence=adverse,
        state_evidence=norm,
    )
