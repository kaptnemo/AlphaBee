"""Layer 1 压缩引擎：``FactorSnapshot → VariableScores``（设计文档 §3）。

本模块把七因子快照的**原始 canonical 值**独立压成七个方向分，输出
:class:`~alphabee.midterm.models.VariableScores`。核心纪律：

- **确定性纯函数**：不调 LLM、不发网络请求、不读外部状态；同一输入必得同一输出。
- **不跨因子求和**：每个因子独立压成 ``[-1, 1]`` 方向分（M 复用 ``market_score`` 0-100），
  不产生任何跨因子线性加权 —— State 由 classifier 按「模式」判定，而非求和（§1）。
- **缺失显式 ``None``**：某个因子方向分需要的输入缺失时，该方向分输出 ``None``，
  绝不静默回退为 0 或中性（``alphabee-schema-steward``）。
- **原始值保留**：只读 ``FactorSnapshot``，不修改它；原始 canonical 值保留给
  classifier（读方向一致性）与 EV（读估值分位 / EPS 绝对值）。

方向分符号约定（正 = 对多头有利）：

- F：边际改善 → 正；E：分析师上修 → 正（核心因子）；T：相对走强 → 正；
- V：估值分位低（便宜/赔率高）→ 正；C：越拥挤 → 越负；R：风险升 → 越负；
- M：直接复用 ``market_score``（0-100）。

阈值/饱和 scale 为结构性示意（§6.3 原话：应回测，不是固定数字），集中定义在
模块顶部常量，便于后续调参。
"""

from __future__ import annotations

import math
from typing import Any

from alphabee.midterm.models import (
    CrowdingFactor,
    ExpectationFactor,
    FactorSnapshot,
    FundamentalFactor,
    MarketFactor,
    RiskFactor,
    TrendFactor,
    ValuationFactor,
    VariableScores,
)

# ─────────────────────────────────────────────────────────────────────────────
# 饱和 scale（结构性示意阈值，应回测调参；§6.3）
# ─────────────────────────────────────────────────────────────────────────────

_FUNDAMENTAL_YOY_SCALE = 30.0  # PERCENT 营收/净利/每股 YoY 饱和点（30% → ±1）
# e_revision 反饱和（log1p 压缩，§12）：不再线性 clip 到 ±1，保留 +45% vs +100% 幅度差异。
_REVISION_REF = 100.0  # PERCENT 归一化参考点（100% 上修 → ±1；45% → ≈0.83）
_REVISION_REF_LOG = math.log1p(_REVISION_REF)

# P0 设计 §9.2：E 引擎护栏与锚重调
_MIN_CONSENSUS_COVERAGE = 3  # 分析师一致预期最小覆盖家数（<3 ⇒ 研报修正不可信）
_ANCHOR_WEIGHT = 0.6  # 预告锚权重（业绩预告成为 E 主轴）
_RESEARCH_WEIGHT = 0.4  # 研报修正权重（既有逻辑降权）
_RS_SCALE = 20.0  # PERCENT 相对强度超额收益饱和点（20% → ±1）
_HOLDER_CHANGE_SCALE = 20.0  # PERCENT 股东户数增幅饱和点（20% → ±1）
_PLEDGE_SCALE = 50.0  # PERCENT 质押率饱和点（50% → -1）
_DEBT_NEUTRAL = 50.0  # PERCENT 资产负债率中性点（50% → 0）
_DEBT_SCALE = 50.0  # PERCENT 资产负债率饱和步长（100% → -1，0% → +1）
_GOODWILL_SCALE = 1_000_000_000.0  # CNY 商誉饱和点（10 亿 → -1）

# F 现金质量 / 超预期（§11「S2 是 PositiveEvidenceChange 非 Growth」+ profit_without_cash）
# 2026-11-01（L1 可审计性修复，ROADMAP 登记）：**自由现金流惩罚移出 F**——FCF 为负在扩产/备货期
# 是常态（本案 OCF +10.26 亿、FCF −1.67 亿），与「利润无现金支撑」不同义；现金流质量由
# derived facts 的 cashflow_quality（OCF/净利润）与 capex_intensity 承接，F 不再二次扣分。
# 保留**经营现金流**惩罚（OCF < 0 = 当期利润没有现金支撑，是 profit_without_cash 的核心形态）。
_OCF_NEGATIVE_PENALTY = 0.5  # 经营现金流为负对 F 的惩罚力度（自由现金流不再参与）
# OCF 判定口径（P1-2 三档；**纯函数默认 sign** = 旧行为，生产默认 ttm 由
# decision_model.resolve_ocf_penalty_basis 解析后显式传入）：
#   sign = 报告期累计 OCF < 0 ⇒ −0.5（旧口径，回滚开关）
#   ttm  = 滚动 12 个月 OCF < 0 ⇒ −0.5；金融股跳过；TTM 缺失回落 sign（并留痕 basis=0）
OCF_PENALTY_BASIS_SIGN = "sign"
OCF_PENALTY_BASIS_TTM = "ttm"
KNOWN_OCF_PENALTY_BASES: tuple[str, ...] = (OCF_PENALTY_BASIS_TTM, OCF_PENALTY_BASIS_SIGN)
_BEAT_BONUS = 0.2  # net_profit_yoy 超预告上限的 beat 加分

# 预期差（P0-2）：预告 miss 惩罚——实际净利增速远低于预告下限 → 显著负向。
# 「预告 +30~40% 但实际 +1.54%」这类预期差是 S0–S5 框架的核心信号，必须在 F 层显式捕获，
# 而非只奖励 beat 不惩罚 miss（不对称）。
_MISS_PENALTY = 0.3  # 显著 miss 对 F 的惩罚力度（比 beat 加分 0.2 更重：miss 信息量更大）
_MISS_GAP_THRESHOLD = 20.0  # PERCENT 实际低于预告下限的幅度阈值（>20pp = 显著 miss）

# 改造 E（设计 MIDTERM_INSIGHT_INJECTION_DESIGN.md §4）结构性洞察 / 语境归一化参数：
_SEGMENT_DIVERGENCE_THRESHOLD = 15.0  # PERCENT 最快细分增速跑赢整体阈值（>15pp = 结构性亮点）
_SEGMENT_DIVERGENCE_BONUS = 0.2  # F 方向分正向修正幅度（结构性亮点）
# P1-6 §12.2：最快分部收入占比门槛（PERCENT，与 factors._SEGMENT_MIN_SHARE 同源口径；
# 两处各自定义避免 score_engine 依赖数据层）。share 缺失视为未知放行（fail-open）。
_SEGMENT_MIN_SHARE = 10.0

# 分部 bonus 口径三档（P1-6）：**纯函数默认 legacy**（旧行为，直接调用点零变化）；
# 生产默认 period_aligned 由 decision_model.resolve_segment_bonus_mode 解析后显式传入。
#   legacy        = 只看 divergence（旧行为，无期间/占比检查）
#   period_aligned = 分部报告期与财报报告期均可知且不同 ⇒ 不产；最快分部 share <10% ⇒ 不产；
#                    期间缺失 ⇒ fail-open（旧行为）
SEGMENT_BONUS_MODE_LEGACY = "legacy"
SEGMENT_BONUS_MODE_PERIOD_ALIGNED = "period_aligned"
KNOWN_SEGMENT_BONUS_MODES: tuple[str, ...] = (
    SEGMENT_BONUS_MODE_PERIOD_ALIGNED,
    SEGMENT_BONUS_MODE_LEGACY,
)
_E_REVISION_EXEMPT = 0.3  # E 上修阈值（e_revision > 0.3 = 强催化）
_CROWDING_EXEMPT_HALF = 0.5  # 启动期高换手豁免：拥挤度扣分减半
_REGIME_LIFT = 0.1  # regime 软约束：E↑ + segment divergence 时市场暴露上下限上浮幅度

# ─────────────────────────────────────────────────────────────────────────────
# F 边际改善（设计 MIDTERM_F_MARGINAL_DESIGN §4.1；结构性示意，**不进 config**）
#
# 参数冻结留痕（§7.2，2026-11-01 PR-3）：
#
#   | 参数                   | 冻结值 | 设计依据（§4.2）                                          |
#   |------------------------|--------|-----------------------------------------------------------|
#   | F_MARGINAL_WEIGHT      | 0.30   | 水平项抗季节性与单期噪声必须主导，边际只做修正；上限 0.4     |
#   | F_ACCEL_SCALE          | 25.0   | 「一季比趋势快 25pp」已达可信拐点量级，再大用饱和防低基数放大 |
#   | F_MARGIN_DELTA_SCALE   | 4.0    | 制造业毛利率同比 −4pp 已是实质恶化（−2pp 给 0.5 信用）       |
#   | F_MARGIN_QUALITY_FLOOR | 0.20   | 质量门不归零：量/利加速本身仍是信息，只是不配拿满信用         |
#
# 冻结状态：**按设计值冻结、不因语料不足而调整**。影子期语料现状（PR-3 实测）=
# ``data/midterm/state`` 仅 **2 帧**（且为影子层上线前的旧帧，无 marginal 键，
# 覆盖率 0），远低于 §7.1 G-8 要求的 ≥ 50 帧 ⇒ **影子期未完成、G-8 不达标、
# PR-4 开权重（on）不得执行**；上述参数的实盘复核（§7.2 的 ±0.05 聚合门）待影子期
# 语料补齐后，以 ``scripts/f_marginal_shadow.py`` 的 S1–S4 指标判定。
# 观测口径见 ``alphabee.midterm.marginal_shadow``；设计值由
# ``tests/midterm/test_f_marginal_scoring.py`` 以 ±0.05 容差钉住。
# ─────────────────────────────────────────────────────────────────────────────

F_MARGINAL_WEIGHT = 0.30  # 边际项权重（水平项权重 = 1 − 0.30）；上限 0.4（§4.2）
F_ACCEL_SCALE = 25.0  # PERCENT 加速度饱和点（一季比趋势快 25pp → +1）
F_MARGIN_DELTA_SCALE = 4.0  # PERCENT 毛利率变化尺度（−4pp → 质量门触底）
F_MARGIN_QUALITY_FLOOR = 0.20  # 质量门下限（不归零：量/利加速本身仍是信息）

# 三档模式（§5.1）：off = 不算（严格复现旧口径）；shadow = 算但不进 F（缺省）；
# on = 进 F（P1，门禁通过后显式开启）。**纯函数纪律**：本模块不读 config，模式由
# ``decision_model.resolve_f_marginal_mode`` 解析后以 keyword 参数传入（缺省 off）。
MARGINAL_MODE_OFF = "off"
MARGINAL_MODE_SHADOW = "shadow"
MARGINAL_MODE_ON = "on"
KNOWN_MARGINAL_MODES: tuple[str, ...] = (MARGINAL_MODE_OFF, MARGINAL_MODE_SHADOW, MARGINAL_MODE_ON)

# 扣非保守锚三档（P1-3 design §9.3）：**纯函数默认 off**（旧行为，直接调用点零变化）；
# 生产默认 shadow 由 ``decision_model.resolve_f_recurring_mode`` 解析后显式传入。
#   off    = 净利项 = saturate(归母 yoy)，不写任何 recurring 键（现状逐值不变）
#   shadow = 计算 np_term_recurring / recurring_gap / f_recurring_shadow，但 F 仍用归母项
#   on     = 净利项改用 saturate(min(归母 yoy, 扣非 yoy))（只压不抬）；f == f_recurring_shadow
RECURRING_MODE_OFF = "off"
RECURRING_MODE_SHADOW = "shadow"
RECURRING_MODE_ON = "on"
KNOWN_RECURRING_MODES: tuple[str, ...] = (RECURRING_MODE_OFF, RECURRING_MODE_SHADOW, RECURRING_MODE_ON)

# ``f_marginal_source`` 位掩码（§5.2 S4 加速度覆盖率统计口径）：按**可用腿**置位。
# 1 = 营收腿（q_revenue_yoy 与 revenue_yoy 均在）、2 = 净利腿、4 = 质量腿（q_gross_margin_pp）。
# 0 = 降级（两腿都缺或质量腿缺失）——降级帧不写边际键，等价于 source=0。
_MARGINAL_SOURCE_REVENUE = 1
_MARGINAL_SOURCE_NET_PROFIT = 2
_MARGINAL_SOURCE_QUALITY = 4


# ─────────────────────────────────────────────────────────────────────────────
# 通用辅助（纯函数）
# ─────────────────────────────────────────────────────────────────────────────


def _saturate(value: float, scale: float) -> float:
    """把 ``value / scale`` 饱和到 ``[-1, 1]``（正负对称）。"""
    ratio = value / scale
    return max(-1.0, min(1.0, ratio))


def _log1p_saturate(value: float, ref_log: float) -> float:
    """log1p 反饱和：把 PERCENT 修订量压缩到 ``[-1, 1]``，保留大值幅度差异。

    ``sign(x) * log1p(|x|) / ref_log``。相比线性 ``_saturate``，+45% 与 +100% 不再
    都饱和到 1.0（100% → ±1，45% → ≈±0.83，5% → ≈±0.39）。缺失值由调用方过滤。
    """
    sign = 1.0 if value >= 0.0 else -1.0
    magnitude = math.log1p(abs(value)) / ref_log
    return sign * min(1.0, magnitude)


def _mean(values: list[float]) -> float | None:
    """均值；空列表 → ``None``（缺失而非回退 0）。"""
    return sum(values) / len(values) if values else None


def _normalize_ymd(value: Any) -> str:
    """报告期归一为 ``YYYYMMDD``（取数字串前 8 位）；不足 8 位数字 → ``""``。

    ``"20260930"`` / ``"2026-09-30"`` 都可比较；空串/``None``/非日期文本 → ``""``
    （视为「期间未知」，绝不猜测）。
    """
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def _periods_match(a: str, b: str) -> bool:
    """两个报告期是否为**同一** ``YYYYMMDD``；任一为空/非法 → ``False``。

    P0-1 的期间守卫基础：``report_period`` 与 ``profit_forecast_period`` 必须同时
    可知且相等，才允许 beat/miss；否则 fail-closed（无法证明同期 ⇒ 不产）。
    """
    aa, bb = _normalize_ymd(a), _normalize_ymd(b)
    return bool(aa and bb and aa == bb)


def _is_first_quarter(period: str) -> bool:
    """报告期是否为 Q1（归一 ``YYYYMMDD`` 后以 ``0331`` 结尾；缺失/非法 → ``False``）。

    P0-3 的降级判据：Q1 的单季值 = 累计值，``Δ = q_yoy − 累计 yoy ≡ 0``，加速度
    不可识别——不能把 ``M=0`` 当成「没加速」参与加权（否则 ``on`` 把 F 系统性压低 30%，
    ``shadow`` 的 ``ΔF`` 污染 S1）。
    """
    normalized = _normalize_ymd(period)
    return len(normalized) == 8 and normalized.endswith("0331")


# ─────────────────────────────────────────────────────────────────────────────
# 七个方向分（逐一独立产出）
# ─────────────────────────────────────────────────────────────────────────────


def _normalize_ocf_penalty_basis(basis: str | None) -> str:
    """OCF 口径归一：合法值原样返回；缺失/非法 → ``sign``（旧行为，最保守）。

    resolve 层（``decision_model.resolve_ocf_penalty_basis``）已保证合法值；这里只是
    直接调用 ``fundamental_breakdown`` 时的防御：非法值不得被静默升级为 TTM 口径。
    """
    value = str(basis or "").strip().lower()
    return value if value in KNOWN_OCF_PENALTY_BASES else OCF_PENALTY_BASIS_SIGN


def _operating_cashflow_penalty(f: FundamentalFactor, basis: str = OCF_PENALTY_BASIS_SIGN) -> float:
    """经营现金流惩罚（profit_without_cash）：OCF 为负 → ``-_OCF_NEGATIVE_PENALTY``。

    口径（P1-2）：``basis="sign"``（**函数缺省**，旧行为）用报告期累计 OCF 符号；
    ``basis="ttm"``（生产默认，由 decision_model 显式传入）先用滚动 12 个月 OCF：

    - 金融股（``is_financial``）在 TTM 口径下**跳过**（OCF 与利润质量不可比，P1-5）；
    - TTM 可得 ⇒ ``ttm < 0`` 才罚；TTM 缺失（缺 FY(prev)/cum(t−4)）⇒ 回落累计符号
      （不静默免罚，D1 决策），并由 ``score_breakdown.ocf_penalty_basis=0`` 留痕。

    **自由现金流不参与 F**（2026-11-01 移出，见常量区说明）：FCF 为负常来自资本开支/备货，
    与「利润没有现金支撑」不是同一件事，由 derived facts 的 ``capex_intensity`` 承接。
    缺失（``None``）不惩罚（不静默假设）。
    """
    if basis == OCF_PENALTY_BASIS_TTM:
        if f.is_financial:
            return 0.0
        if f.operating_cashflow_ttm is not None:
            return -_OCF_NEGATIVE_PENALTY if f.operating_cashflow_ttm < 0.0 else 0.0
    if f.operating_cashflow is not None and f.operating_cashflow < 0.0:
        return -_OCF_NEGATIVE_PENALTY
    return 0.0


def _ocf_penalty_basis_effective(f: FundamentalFactor, basis: str) -> bool:
    """``score_breakdown.ocf_penalty_basis`` 的 1/0 口径（P1-2 §3.4）。

    ``1`` = TTM 口径实际生效（含金融股跳过：金融股在 TTM 下显式不适用，不是「回落
    sign」）；``0`` = sign 口径或 TTM 缺失回落 sign。
    """
    if basis != OCF_PENALTY_BASIS_TTM:
        return False
    return f.is_financial or f.operating_cashflow_ttm is not None


def _beat_bonus(f: FundamentalFactor, e: ExpectationFactor) -> float:
    """超预期 beat（§11「S2 是 PositiveEvidenceChange 非 Growth」）。

    期间守卫（P0-1，fail-closed）：``f.report_period`` 与 ``e.profit_forecast_period``
    任一为空或不同期 → 0（错期比较（如 H1 预告 vs Q3 财报）不产 beat，宁可不产也不
    保留错期证据）。同期时语义逐字不变：``net_profit_yoy`` 超预告上限 →
    ``+_BEAT_BONUS``；in-line（含落区间内）/ 预告区间缺失 → 0（不额外加分）。
    """
    if not _periods_match(f.report_period, e.profit_forecast_period):
        return 0.0
    if (
        f.net_profit_yoy is not None
        and e.profit_forecast_max_change is not None
        and f.net_profit_yoy > e.profit_forecast_max_change
    ):
        return _BEAT_BONUS
    return 0.0


def _miss_penalty(f: FundamentalFactor, e: ExpectationFactor) -> float:
    """预告 miss（P0-2 预期差）：实际净利增速远低于预告下限 → 显著负向。

    期间守卫（P0-1，fail-closed）：任一期间为空或不同期 → 0（同 :func:`_beat_bonus`）。
    同期时：``min_change - net_profit_yoy`` 为「低于预告下限」的幅度（pp，正=miss）；
    超过 ``_MISS_GAP_THRESHOLD``（>20pp）判定为显著 miss，扣 ``-_MISS_PENALTY``。
    预告下限缺失 / 实际缺失 → 0（不编造预期差）。
    """
    if not _periods_match(f.report_period, e.profit_forecast_period):
        return 0.0
    if f.net_profit_yoy is not None and e.profit_forecast_min_change is not None:
        gap = e.profit_forecast_min_change - f.net_profit_yoy
        if gap > _MISS_GAP_THRESHOLD:
            return -_MISS_PENALTY
    return 0.0


def _segment_divergence(f: FundamentalFactor) -> float | None:
    """最快细分增速 − 整体营收增速（pp，改造 E）；任一缺失 → ``None``。"""
    if f.segment_fastest_yoy is None or f.revenue_yoy is None:
        return None
    return f.segment_fastest_yoy - f.revenue_yoy


def _normalize_segment_bonus_mode(mode: str | None) -> str:
    """分部 bonus 口径归一：合法值原样返回；缺失/非法 → ``legacy``（旧行为）。

    resolve 层（``decision_model.resolve_segment_bonus_mode``）已保证合法值；这里只是
    直接调用 ``fundamental_breakdown`` 时的防御（非法值不得静默启用新口径）。
    """
    value = str(mode or "").strip().lower()
    return value if value in KNOWN_SEGMENT_BONUS_MODES else SEGMENT_BONUS_MODE_LEGACY


def _segment_bonus(f: FundamentalFactor, mode: str = SEGMENT_BONUS_MODE_LEGACY) -> float:
    """结构性亮点（改造 E / P1-6）：最快细分显著跑赢整体（divergence > 15pp）→ +0.2。

    口径（``segment_bonus_mode``）：

    - ``legacy``（**函数缺省**，旧行为）⇒ 只看 divergence；
    - ``period_aligned``（生产默认）⇒ 追加两个门槛（设计 §12.2）：

      * 分部报告期（``segment_period``）与财报报告期（``report_period``）**均可知且不同**
        ⇒ 0（H1 分部对比 Q3 财报不是结构性突变）；任一缺失 ⇒ fail-open（按旧行为）；
      * 最快分部收入占比已知且 <10% ⇒ 0（小分部不构成结构性亮点）；缺失 ⇒ fail-open。
    """
    divergence = _segment_divergence(f)
    if divergence is None or divergence <= _SEGMENT_DIVERGENCE_THRESHOLD:
        return 0.0
    if _normalize_segment_bonus_mode(mode) == SEGMENT_BONUS_MODE_LEGACY:
        return _SEGMENT_DIVERGENCE_BONUS
    segment_period = _normalize_ymd(f.segment_period)
    report_period = _normalize_ymd(f.report_period)
    if segment_period and report_period and segment_period != report_period:
        return 0.0
    if f.segment_fastest_share is not None and f.segment_fastest_share < _SEGMENT_MIN_SHARE:
        return 0.0
    return _SEGMENT_DIVERGENCE_BONUS


# ─────────────────────────────────────────────────────────────────────────────
# F 边际项（设计 §4.1）：加速度 Δ + 质量门 Q
# ─────────────────────────────────────────────────────────────────────────────


def _normalize_marginal_mode(marginal_mode: str | None) -> str:
    """模式归一：合法值原样返回；缺失/非法 → ``off``（最保守，绝不因模式串制造新键）。

    resolve 层（``decision_model.resolve_f_marginal_mode``）已保证合法值，这里只是
    直接调用 ``fundamental_breakdown`` 时的防御：非法值不得被静默升级为 shadow。
    """
    value = str(marginal_mode or "").strip().lower()
    return value if value in KNOWN_MARGINAL_MODES else MARGINAL_MODE_OFF


def _normalize_f_recurring_mode(recurring_mode: str | None) -> str:
    """扣非模式归一：合法值原样返回；缺失/非法 → ``off``（旧行为，绝不静默升级）。

    与 :func:`_normalize_marginal_mode` 同构：resolve 层已保证合法值，这里只是直接
    调用 ``fundamental_breakdown`` 时的防御（非法模式串不得改变 F）。
    """
    value = str(recurring_mode or "").strip().lower()
    return value if value in KNOWN_RECURRING_MODES else RECURRING_MODE_OFF


def _marginal_legs(f: FundamentalFactor) -> list[float]:
    """两条加速度腿（pp）：``Δ_rev = q_revenue_yoy − revenue_yoy``、``Δ_np`` 同理。

    Q1 降级（P0-3）：``marginal_period`` 为 Q1（``0331``）⇒ 单季=累计、Δ 结构性为 0，
    加速度不可识别，返回空列表（``_marginal`` 随之 ``None``，不写任何边际键）。
    可单腿（另一腿缺失只用可用腿，**不补 0**）；两腿都缺 ⇒ 空列表。
    """
    if _is_first_quarter(f.marginal_period):
        return []  # Q1：单季=累计，加速度不可识别（不能把 M=0 当「没加速」加权）
    deltas: list[float] = []
    if f.q_revenue_yoy is not None and f.revenue_yoy is not None:
        deltas.append(f.q_revenue_yoy - f.revenue_yoy)
    if f.q_net_profit_yoy is not None and f.net_profit_yoy is not None:
        deltas.append(f.q_net_profit_yoy - f.net_profit_yoy)
    return deltas


def _marginal_source(f: FundamentalFactor) -> int:
    """``f_marginal_source`` 位掩码：1=营收腿、2=净利腿、4=质量腿（按**可用**置位）。

    只有 ``_marginal`` 非 ``None`` 时才会写进分解（值 ∈ {5, 6, 7}）；降级帧不写该键，
    语义等价于 ``0``（两腿都缺 / 质量腿缺失）。
    """
    source = 0
    if f.q_revenue_yoy is not None and f.revenue_yoy is not None:
        source |= _MARGINAL_SOURCE_REVENUE
    if f.q_net_profit_yoy is not None and f.net_profit_yoy is not None:
        source |= _MARGINAL_SOURCE_NET_PROFIT
    if f.q_gross_margin_pp is not None:
        source |= _MARGINAL_SOURCE_QUALITY
    return source


def _marginal(f: FundamentalFactor) -> tuple[float, float, float] | None:
    """F 边际项 ``(M_raw, Q, M)``（设计 §4.1）；不可算 → ``None``（降级，F 回落水平项 L）。

    - ``Δ_rev = q_revenue_yoy − revenue_yoy``、``Δ_np = q_net_profit_yoy − net_profit_yoy``（pp）：
      量的是**加速度**而非「单季水平」——直接用 ``q_yoy`` 水平会与 L 重复计分（一家常年
      +30% 的公司会被算两次，§4.1 口径说明）；
    - ``M_raw = mean(saturate(Δ, F_ACCEL_SCALE))``：可单腿（不把缺失腿当 0 稀释）；
    - ``Q = clip(1 + q_gross_margin_pp / F_MARGIN_DELTA_SCALE, F_MARGIN_QUALITY_FLOOR, 1.0)``；
    - ``M = M_raw × Q``。

    **降级规则（§4.3 未逐字规定处的取舍）**：两条加速度腿都缺，**或**质量腿
    （``q_gross_margin_pp``）缺失 ⇒ ``None``。理由：质量门是边际项成立的必要条件——
    没有质地的「加速」正是 §0 第三行要防的失败模式（一次低基数季度把「证伪」抹成
    「没信号」），所以不拿半边数据报满额信用。

    金融股（P1-5）显式降级：``is_financial=True`` ⇒ ``None``（OCF/边际层对银行、保险、
    证券不可比；不依赖「毛利率缺失」的偶然性来触发降级）。
    """
    if f.is_financial:
        return None
    deltas = _marginal_legs(f)
    if not deltas or f.q_gross_margin_pp is None:
        return None
    m_raw = _mean([_saturate(delta, F_ACCEL_SCALE) for delta in deltas])
    if m_raw is None:  # 理论上不可达（deltas 非空），防御保留
        return None
    quality = max(
        F_MARGIN_QUALITY_FLOOR,
        min(1.0, 1.0 + f.q_gross_margin_pp / F_MARGIN_DELTA_SCALE),
    )
    return m_raw, quality, m_raw * quality


def fundamental_breakdown(
    f: FundamentalFactor,
    e: ExpectationFactor,
    *,
    marginal_mode: str = MARGINAL_MODE_OFF,
    ocf_penalty_basis: str = OCF_PENALTY_BASIS_SIGN,
    f_recurring_mode: str = RECURRING_MODE_OFF,
    segment_bonus_mode: str = SEGMENT_BONUS_MODE_LEGACY,
) -> dict[str, float] | None:
    """F 方向分的**可审计分解**（L1）：返回逐项与饱和标志；全部 YoY 缺失 → ``None``。

    键（全部 ``float``，缺失项不出现）：

    - ``revenue_yoy`` / ``net_profit_yoy`` / ``eps_growth_yoy``：原始 PERCENT；
    - ``revenue_term`` / ``net_profit_term`` / ``eps_term``：``_saturate(yoy, _FUNDAMENTAL_YOY_SCALE)`` 后的项；
    - ``revenue_saturated`` / ...：该项是否越过 ±30 饱和点（1.0/0.0）；
    - ``growth_mean``：水平项 L = 三项均值；``ocf_penalty`` / ``beat_bonus`` / ``miss_penalty`` /
      ``segment_bonus``：修正项；
    - ``forecast_period_matched``：财报期与预告期是否同期（1/0，P0-1）——0 时
      ``beat_bonus`` / ``miss_penalty`` 恒为 0（fail-closed）；
    - ``q1_marginal_degraded``：Q1 边际降级留痕（仅 ``shadow`` 且 Q1 帧写 1.0，P0-3）；
    - ``report_period_mismatch``：``report_period`` 与 ``marginal_period`` 均可知且
      不同期（1.0；P2-1 诊断，缺失/同期不写）；
    - ``ocf_ttm``：滚动 12 个月经营现金流（仅值可得时写）；``ocf_penalty_basis``：
      1 = TTM 口径实际生效（含金融股跳过）/ 0 = sign 或 TTM 缺失回落（P1-2）；
      ``financial_sector``：1 = 金融股（P1-5）；
    - ``segment_period_mismatch``：分部报告期与财报报告期均可知且不同（1/0，P1-6）；
      ``segment_share_below_min``：最快分部 share 已知且 <10%（1/0，P1-6）；
    - ``raw``：修正后合计；``f``：最终方向分（clip 后）；
    - ``input_saturated``：至少一项 YoY 越过饱和点（原始幅度已丢失）；
    - ``output_saturated``：合计越过 ±1（越界部分被 clip）。

    F 边际改善（§6.1，``marginal_mode`` 三档；缺省 ``off`` ⇒ 键与值逐项不变）：

    - ``off``：不算边际项，**输出与改造前逐值相同**（所有既有调用点零变化）；
    - ``shadow``：算 ``M`` 并写 6 个边际键，但 F 仍只用水平项（``f_growth = L``）；
    - ``on``：``f_growth = (1 − F_MARGINAL_WEIGHT)·L + F_MARGINAL_WEIGHT·M``；
    - **Q1 降级（P0-3）**：``marginal_period`` 为 Q1（``0331``）⇒ 单季=累计、加速度
      不可识别，``marginal=None``，``on``/``shadow`` 都**不写任何边际键**；``on`` 档
      ``f_growth=L``（F 与 ``off`` 逐值相同，不把 ``M=0`` 当「没加速」压低 30%），
      ``shadow`` 档写 ``q1_marginal_degraded=1.0`` 显式留痕（``on`` 不写该键，保证
      ``on`` 与 ``off`` 的分解逐值相等）；
    - 边际键：``marginal_raw``(M_raw) / ``marginal_quality``(Q) / ``marginal``(M) /
      ``f_shadow``（**在当前 ``f_recurring_mode`` 下把 M 打开的反事实 F**；净利项用
      ``growth_for_f``，故 recurring=on 帧的 ``f_shadow_delta`` 是真实边际效应、不含
      扣非替换差）/ ``f_shadow_delta``(= f_shadow − f) /
      ``f_marginal_source``（可用腿位掩码，见 :func:`_marginal_source`）。
      降级帧（``_marginal`` 为 ``None``）**不写边际键**：等价于 source=0，且保证
      ``on``/``shadow`` 的分解与 ``off`` 逐键相同（降级恒等）；
    - ``f_shadow`` 在 ``marginal_mode=on`` 时恒等于 ``f``（delta=0），且在
      ``f_recurring_mode=on`` 时使用扣非替换后的 ``growth_for_f``：shadow 因此是真正的
      **开权重预演**（S1 ``ΔF`` 直接可用，且不把扣非替换差混入边际效应）。

    §4.4 去重：仅 ``on`` 且 ``marginal`` 非 ``None`` 且 ``Q < 1.0`` 时 ``beat *= 0.5``
    （避免「超预期」与「加速」对同一改善双重奖励）；``shadow``/``off`` 不折半
    （否则 F 会变，违反 G-7）。``f_shadow`` 按 on 口径计算，故 shadow 模式下的
    ``f_shadow_delta`` 已包含该去重效应。

    本函数是 F 的**唯一计算入口**：:func:`_fundamental_trend` 只取 ``["f"]``，报告/记录消费
    ``breakdown``——避免"决策算一份、报告再算一份"的漂移（``alphabee-schema-steward`` 单一来源）。

    ``ocf_penalty_basis``（P1-2）：keyword 缺省 ``"sign"``（旧行为，直接调用点零变化）；
    生产路径由 ``decision_model.resolve_ocf_penalty_basis`` 解析配置后显式传入 ``"ttm"``。

    分部 bonus 口径（P1-6，``segment_bonus_mode``；缺省 ``legacy`` ⇒ 旧行为）：
    ``period_aligned`` 时要求分部报告期与财报报告期一致（两期均可知且不同 ⇒ 不产）且
    最快分部收入占比 ≥10%（已知且 <10 ⇒ 不产）；任一期间缺失 ⇒ fail-open 旧行为。

    扣非保守锚（P1-3，``f_recurring_mode`` 三档；缺省 ``off`` ⇒ 键与值逐项不变）：

    - ``off``：净利项 = ``saturate(归母 yoy)``，**不写任何 recurring 键**（现状逐值不变）；
    - ``shadow``：算 ``np_term_recurring`` / ``recurring_gap`` / ``f_recurring_shadow`` 并落盘，
      但 F 仍用归母项（**F 逐值不变**）；
    - ``on``：净利项改用 ``np_term_recurring``（``min(归母, 扣非)`` 的饱和值），F 与
      ``f_recurring_shadow`` 逐值相等；
    - 可用性：``net_profit_dedt_yoy`` 非 None 且 ``net_profit_dedt_base_positive is True``
      才用扣非锚；否则**回落归母项**并写 ``recurring_unavailable=1``（负基数不参与百分比
      比较，缺失不猜测）；
    - ``recurring_gap = net_profit_yoy − net_profit_dedt_yoy``（pp，两者都可算时写）；
    - **min 只压不抬**（口径披露）：扣非高于归母时仍取归母（一次性收益被扣非压低）；
      一次性损失（扣非低于归母）会压低 F——这是保守取向，不是错误；
    - 边际项 M 不因扣非替换改变（同一 ``marginal_mode`` 下 M 相同），但 ``f_shadow``
      的净利项使用 ``growth_for_f``（含扣非替换）——recurring=on 帧的 ``f_shadow_delta``
      是真实边际效应；P0-1 期间守卫仍优先（beat/miss 口径不受扣非影响）。
    """
    mode = _normalize_marginal_mode(marginal_mode)
    recurring_mode = _normalize_f_recurring_mode(f_recurring_mode)
    normalized_ocf_basis = _normalize_ocf_penalty_basis(ocf_penalty_basis)
    normalized_segment_mode = _normalize_segment_bonus_mode(segment_bonus_mode)
    marginal = _marginal(f) if mode != MARGINAL_MODE_OFF else None
    # P0-3：Q1 单季=累计 ⇒ 边际项降级（marginal 已为 None）；shadow 写留痕，on 不写
    # （保证 on 与 off 的分解逐值相等，见 ``test_q1_marginal_degrades_to_none``）。
    q1_marginal_degraded = mode != MARGINAL_MODE_OFF and _is_first_quarter(f.marginal_period)
    # P2-1 诊断：单季还原目标期与 L 的报告期不同（两者都可知且不同才写，缺失不猜）。
    report_period = _normalize_ymd(f.report_period)
    marginal_period = _normalize_ymd(f.marginal_period)
    report_period_mismatch = bool(report_period and marginal_period and report_period != marginal_period)

    yoys = {
        "revenue_yoy": f.revenue_yoy,
        "net_profit_yoy": f.net_profit_yoy,
        "eps_growth_yoy": f.eps_growth_yoy,
    }
    terms = {
        "revenue_term": f.revenue_yoy,
        "net_profit_term": f.net_profit_yoy,
        "eps_term": f.eps_growth_yoy,
    }
    present = {key: value for key, value in yoys.items() if value is not None}
    if not present:
        return None
    saturated = {
        "revenue_saturated": f.revenue_yoy is not None and abs(f.revenue_yoy) > _FUNDAMENTAL_YOY_SCALE,
        "net_profit_saturated": f.net_profit_yoy is not None and abs(f.net_profit_yoy) > _FUNDAMENTAL_YOY_SCALE,
        "eps_saturated": f.eps_growth_yoy is not None and abs(f.eps_growth_yoy) > _FUNDAMENTAL_YOY_SCALE,
    }
    term_values = {name: _saturate(value, _FUNDAMENTAL_YOY_SCALE) for name, value in terms.items() if value is not None}
    growth = _mean(list(term_values.values()))
    if growth is None:  # 理论上不可达（present 非空 ⇒ term_values 非空），防御保留
        return None

    ocf_penalty = _operating_cashflow_penalty(f, normalized_ocf_basis)
    # P0-1 期间守卫：财报期与预告期同期才产 beat/miss；不同期/任一为空 ⇒ 0（fail-closed）。
    forecast_period_matched = _periods_match(f.report_period, e.profit_forecast_period)
    beat_bonus = _beat_bonus(f, e)
    # §4.4 去重：仅 on + 有边际项 + 质量门未满（Q<1）时 beat 折半；shadow/off 不折半。
    beat_deduped = beat_bonus
    if marginal is not None and marginal[1] < 1.0:
        beat_deduped = beat_bonus * 0.5
    miss_penalty = _miss_penalty(f, e)
    segment_bonus = _segment_bonus(f, normalized_segment_mode)
    # P1-6 诊断：分部期与财报期均可知且不同；最快分部 share 已知且 <10%。
    segment_period = _normalize_ymd(f.segment_period)
    segment_report_period = _normalize_ymd(f.report_period)
    segment_period_mismatch = bool(segment_period and segment_report_period and segment_period != segment_report_period)
    segment_share_below_min = bool(f.segment_fastest_share is not None and f.segment_fastest_share < _SEGMENT_MIN_SHARE)

    # 扣非保守锚（P1-3）：可用性 = 扣非同比可得 且 上年同期扣非 > 0；否则回落归母项。
    np_term_recurring: float | None = None
    recurring_gap: float | None = None
    recurring_unavailable = False
    if recurring_mode != RECURRING_MODE_OFF:
        if f.net_profit_yoy is not None and f.net_profit_dedt_yoy is not None:
            recurring_gap = f.net_profit_yoy - f.net_profit_dedt_yoy
        if (
            f.net_profit_yoy is not None
            and f.net_profit_dedt_yoy is not None
            and f.net_profit_dedt_base_positive is True
        ):
            # min 只压不抬：扣非高于归母时仍取归母（一次性收益被压低；一次性损失会压低 F）
            np_term_recurring = _saturate(min(f.net_profit_yoy, f.net_profit_dedt_yoy), _FUNDAMENTAL_YOY_SCALE)
        else:
            recurring_unavailable = True

    # 净利项：on 档且扣非可用 ⇒ 用扣非锚；否则归母项（off/shadow/不可用）。
    use_recurring = recurring_mode == RECURRING_MODE_ON and np_term_recurring is not None
    net_term_for_f = np_term_recurring if use_recurring else term_values.get("net_profit_term")
    term_values_for_f = dict(term_values)
    if net_term_for_f is not None:
        term_values_for_f["net_profit_term"] = net_term_for_f
    elif "net_profit_term" in term_values_for_f:
        del term_values_for_f["net_profit_term"]
    growth_for_f = _mean(list(term_values_for_f.values())) if term_values_for_f else growth
    if growth_for_f is None:  # 理论上不可达（term_values 非空 ⇒ mean 非 None），防御保留
        growth_for_f = growth

    # 边际影子：在当前 ``f_recurring_mode`` 下把边际项 M 打开的反事实 F（t12 repair）。
    # 净利项必须用 ``growth_for_f``（recurring=on 且可用时已替换为扣非锚），否则
    # recurring=on 帧的 f_shadow_delta 会把「扣非替换差」误算成边际效应。
    # recurring=off/shadow 时 growth_for_f == growth ⇒ 既有口径逐值不变；
    # marginal_mode=on 时本式与最终 F 同源，f_shadow == f（delta=0）。
    f_shadow: float | None = None
    if marginal is not None:
        m = marginal[2]
        growth_mixed = (1.0 - F_MARGINAL_WEIGHT) * growth_for_f + F_MARGINAL_WEIGHT * m
        f_shadow = _saturate(growth_mixed + ocf_penalty + beat_deduped + miss_penalty + segment_bonus, 1.0)

    beat_for_f = beat_deduped if mode == MARGINAL_MODE_ON else beat_bonus
    f_growth = growth_for_f
    if marginal is not None and mode == MARGINAL_MODE_ON:
        f_growth = (1.0 - F_MARGINAL_WEIGHT) * growth_for_f + F_MARGINAL_WEIGHT * marginal[2]
    raw = f_growth + ocf_penalty + beat_for_f + miss_penalty + segment_bonus
    final = _saturate(raw, 1.0)

    # 把「扣非项代入后的 F」按**当前模式**计算（与非扣非 F 同一组修正项/同一边际模式）：
    # shadow 档 F 不含 M、beat 不折半；on 档与最终 F 完全一致（f == f_recurring_shadow）。
    f_recurring_shadow: float | None = None
    if recurring_mode != RECURRING_MODE_OFF and np_term_recurring is not None:
        recurring_terms = dict(term_values)
        recurring_terms["net_profit_term"] = np_term_recurring
        growth_recurring = _mean(list(recurring_terms.values()))
        if growth_recurring is None:  # 理论上不可达（净利项存在 ⇒ 非空），防御保留
            growth_recurring = growth
        recurring_growth_for_f = growth_recurring
        if marginal is not None and mode == MARGINAL_MODE_ON:
            recurring_growth_for_f = (1.0 - F_MARGINAL_WEIGHT) * growth_recurring + F_MARGINAL_WEIGHT * marginal[2]
        f_recurring_shadow = _saturate(
            recurring_growth_for_f + ocf_penalty + beat_for_f + miss_penalty + segment_bonus, 1.0
        )

    breakdown: dict[str, float] = {**present, **term_values}
    breakdown.update({key: 1.0 if flag else 0.0 for key, flag in saturated.items()})
    breakdown.update(
        {
            "growth_mean": growth,
            "ocf_penalty": ocf_penalty,
            "ocf_penalty_basis": 1.0 if _ocf_penalty_basis_effective(f, normalized_ocf_basis) else 0.0,
            "financial_sector": 1.0 if f.is_financial else 0.0,
            "beat_bonus": beat_for_f,
            "miss_penalty": miss_penalty,
            "segment_bonus": segment_bonus,
            "segment_period_mismatch": 1.0 if segment_period_mismatch else 0.0,
            "segment_share_below_min": 1.0 if segment_share_below_min else 0.0,
            "forecast_period_matched": 1.0 if forecast_period_matched else 0.0,
            "raw": raw,
            "f": final,
            "input_saturated": 1.0 if any(saturated.values()) else 0.0,
            "output_saturated": 1.0 if abs(raw) > 1.0 else 0.0,
        }
    )
    if marginal is not None and f_shadow is not None:
        m_raw, quality, m = marginal
        breakdown.update(
            {
                "marginal_raw": m_raw,
                "marginal_quality": quality,
                "marginal": m,
                "f_shadow": f_shadow,
                "f_shadow_delta": f_shadow - final,
                "f_marginal_source": float(_marginal_source(f)),
            }
        )
    # P1-3 扣非诊断键（仅 shadow/on 写；off 逐键不变）：可用性、缺口、扣非代入后的 F。
    if recurring_mode != RECURRING_MODE_OFF:
        if np_term_recurring is not None:
            breakdown["np_term_recurring"] = np_term_recurring
        if recurring_gap is not None:
            breakdown["recurring_gap"] = recurring_gap
        if f_recurring_shadow is not None:
            breakdown["f_recurring_shadow"] = f_recurring_shadow
        if recurring_unavailable:
            breakdown["recurring_unavailable"] = 1.0
    # P0-3 留痕：仅 shadow 写（on 不写，保证 on==off 逐值相等）；Q1 降级没有边际键。
    if q1_marginal_degraded and mode == MARGINAL_MODE_SHADOW:
        breakdown["q1_marginal_degraded"] = 1.0
    if report_period_mismatch:
        breakdown["report_period_mismatch"] = 1.0
    # P1-2：TTM OCF 诊断值（缺失不写，避免把 None 伪装成数值）。
    if f.operating_cashflow_ttm is not None:
        breakdown["ocf_ttm"] = f.operating_cashflow_ttm
    return breakdown


def _fundamental_trend(
    f: FundamentalFactor,
    e: ExpectationFactor,
    *,
    marginal_mode: str = MARGINAL_MODE_OFF,
    ocf_penalty_basis: str = OCF_PENALTY_BASIS_SIGN,
    f_recurring_mode: str = RECURRING_MODE_OFF,
    segment_bonus_mode: str = SEGMENT_BONUS_MODE_LEGACY,
) -> float | None:
    """F 方向分：边际改善 + 经营现金流惩罚 + 超预期 beat + 结构性亮点（改造 E）。

    §3.3 口径 ``gross_margin_trend + revenue_yoy + net_profit_yoy`` 的边际方向；
    ``FactorSnapshot`` 是单帧、无 ``gross_margin_trend`` 时序，故用营收/净利/EPS
    三组 YoY（本身即边际变化）作为「改善」代理，并叠加：

    - 经营现金流（profit_without_cash）：**OCF < 0** → 显著惩罚（避免 net_profit_yoy 高增长掩盖
      利润不兑现）。**自由现金流不再参与**（2026-11-01 移出，见常量区；由 derived facts 承接）；
      ``ocf_penalty_basis="ttm"`` 时用滚动 12 个月 OCF，金融股跳过（P1-2/P1-5；缺省 sign 为旧行为）；
    - 超预期（beat）：net_profit_yoy 超预告上限才加分，in-line 不加分；
    - 预告 miss（P0-2 预期差）：net_profit_yoy 远低于预告下限 → 显著扣分
      （预告 +30~40% 但实际 +1.54% 的预期差是框架核心信号，不能只奖 beat 不罚 miss）；
    - beat/miss 均带 P0-1 期间守卫：财报期（``f.report_period``）与预告期
      （``e.profit_forecast_period``）不同期/任一为空 ⇒ 不产（fail-closed）；
    - 结构性亮点（改造 E）：最快细分增速显著跑赢整体（segment divergence > 15pp）
      → 正向修正（整体 +17.94% 但高速通信线 +35.44% 的「结构性突变」）；
    - 边际改善（F-Marginal §4.3，``marginal_mode``）：``on`` 时把水平项 L 与边际项 M 混合；
      ``shadow`` 只落盘不生效；``off``/无单季数据 ⇒ 逐值等于改造前公式（G3 降级恒等）；
    - 扣非保守锚（P1-3，``f_recurring_mode``）：``on`` 时净利项改用 ``min(归母, 扣非)``
      的饱和值（只压不抬）；``shadow`` 只落盘诊断键、F 仍用归母项；``off``（缺省）逐值不变；
    - 分部 bonus 口径（P1-6，``segment_bonus_mode``）：``period_aligned`` 追加期间对齐与
      10% 收入占比门槛；``legacy``（缺省）只看 divergence（旧行为）。

    全部 YoY 缺失 → ``None``；最终结果 clip 到 ``[-1, 1]``。逐项口径见
    :func:`fundamental_breakdown`（本函数只取其中的 ``f``，单一来源）。
    """
    breakdown = fundamental_breakdown(
        f,
        e,
        marginal_mode=marginal_mode,
        ocf_penalty_basis=ocf_penalty_basis,
        f_recurring_mode=f_recurring_mode,
        segment_bonus_mode=segment_bonus_mode,
    )
    return breakdown["f"] if breakdown is not None else None


def _forecast_revision_anchor(e: ExpectationFactor) -> float | None:
    """预告锚（P0-6 重调）：相邻两个报告期预告区间中点差（PERCENT，上修为正）的方向分。

    ``ExpectationFactor.forecast_revision_anchor`` 由 ``factors.build_expectation_factor``
    按修正后末条口径计算（同报告期取 ``ann_date`` 末条、period 降序取前两期）；本函数只做
    ``_log1p_saturate`` 反饱和（100% → ±1，保留幅度差异）。无相邻期（字段 ``None``）→
    ``None`` ⇒ ``_revision`` 回落研报单独承载（E **不**因预告水平乐观而无条件为正）。
    """
    delta = e.forecast_revision_anchor
    if delta is None:
        return None
    return _log1p_saturate(delta, _REVISION_REF_LOG)


def _research_revision(e: ExpectationFactor) -> float | None:
    """研报修正（既有逻辑，P0-6 降权 0.4）：revision 字段 + revision_breadth。"""
    contribs: list[float] = []
    for rev in (e.eps_fy1_revision_1m, e.eps_fy1_revision_3m, e.eps_fy2_revision_1m):
        if rev is not None:
            contribs.append(_log1p_saturate(rev, _REVISION_REF_LOG))
    if e.revision_breadth is not None:
        contribs.append(2.0 * e.revision_breadth - 1.0)  # 0.5 → 0，1 → +1，0 → -1
    return _mean(contribs)


def _revision(e: ExpectationFactor) -> float | None:
    """E 方向分（P0-6 重调）：预告锚 0.6 + 研报修正 0.4；coverage<3 → None（护栏）。

    - coverage 护栏：``coverage_count`` 缺失或 < :data:`_MIN_CONSENSUS_COVERAGE` ⇒
      ``None``（调用方 :func:`compress_scores` 把 ``consensus_coverage_low`` 登记进
      ``FactorSnapshot.missing_facts``）；
    - 合成：两者皆有时 ``0.6×anchor + 0.4×research``（clip [-1,1]）；单侧缺失时另一侧
      **单独承载**（full，不缩放）；两侧全缺 → ``None``。
    """
    if e.coverage_count is None or e.coverage_count < _MIN_CONSENSUS_COVERAGE:
        return None
    anchor = _forecast_revision_anchor(e)
    research = _research_revision(e)
    if anchor is None and research is None:
        return None
    if anchor is None:
        return research
    if research is None:
        return anchor
    return _saturate(_ANCHOR_WEIGHT * anchor + _RESEARCH_WEIGHT * research, 1.0)


def _relative_strength(t: TrendFactor) -> float | None:
    """T 方向分：相对走强 → 正。

    §3.3 口径 ``rs_stock_market_20d/60d + 均线结构``。``TrendFactor`` 无绝对收盘价，
    均线结构不可直接计算；用个股相对全市场 / 相对行业的超额收益（RS，PERCENT）承载
    「相对走强」。全部缺失 → ``None``。
    """
    rs = [
        t.rs_stock_market_20d,
        t.rs_stock_market_60d,
        t.rs_stock_industry_20d,
        t.rs_stock_industry_60d,
    ]
    contribs = [_saturate(v, _RS_SCALE) for v in rs if v is not None]
    return _mean(contribs)


def _valuation_percentile(v: ValuationFactor) -> float | None:
    """V 方向分：估值分位低（便宜/赔率高）→ 正。

    §3.3 口径 ``pe_ttm_5y_percentile / pb_5y_percentile``，低分位 → 正。
    映射 ``p → 1 - 2p``（p=0 最便宜 → +1，p=0.5 → 0，p=1 最贵 → -1）。
    两个分位全部缺失 → ``None``。
    """
    percentiles = [v.pe_ttm_5y_percentile, v.pb_5y_percentile]
    contribs = [1.0 - 2.0 * p for p in percentiles if p is not None]
    return _mean(contribs)


def _crowding(
    c: CrowdingFactor,
    *,
    e_revision: float | None = None,
    segment_divergence: float | None = None,
) -> float | None:
    """C 方向分：越拥挤 → 越负。

    §3.3 口径 ``holder_count_change + hot_rank + turnover_rate_percentile``：

    - ``holder_count_change``（股东户数增幅）：户数↓ → 筹码集中 → 负（§3.3 原注），
      故户数↑（筹码分散）→ 正；
    - ``hot_rank``：人气榜排名（1 最热）；排名越低越拥挤 → 越负；
    - ``turnover_rate_percentile``：换手率分位越高越拥挤 → 越负。

    改造 E（语境归一化）：强催化（``e_revision > 0.3`` 上修）或结构性亮点
    （``segment_divergence > 15pp``）时，换手率分位的**负向扣分**（``crowding_pct<0``，
    高换手）减半——「催化剂驱动的启动期高换手」≠「高位派发的拥挤」；正向（低换手）
    不衰减（豁免只作用于拥挤惩罚，不削弱冷门利好）。

    全部缺失 → ``None``。
    """
    exempt = (e_revision is not None and e_revision > _E_REVISION_EXEMPT) or (
        segment_divergence is not None and segment_divergence > _SEGMENT_DIVERGENCE_THRESHOLD
    )

    contribs: list[float] = []
    if c.holder_count_change is not None:
        contribs.append(_saturate(c.holder_count_change, _HOLDER_CHANGE_SCALE))
    if c.hot_rank is not None:
        # 人气榜 top100：rank=1 最热 → 最负；rank=100 最冷 → 0。
        contribs.append(-_saturate(100.0 - float(c.hot_rank), 100.0))
    if c.turnover_rate_percentile is not None:
        crowding_pct = 1.0 - 2.0 * c.turnover_rate_percentile  # 分位 1 → -1，0 → +1
        if exempt and crowding_pct < 0.0:
            crowding_pct *= _CROWDING_EXEMPT_HALF  # 仅对负向（高换手扣分）减半；正向不衰减
        contribs.append(crowding_pct)
    return _mean(contribs)


def _risk(r: RiskFactor) -> float | None:
    """R 方向分：风险升 → 越负（质押 + 杠杆 + 商誉/审计 三层合成）。

    三层子分（均在 ``[-1, 1]``，负 = 风险）：

    1. 质押：``pledge_ratio`` 越高越负；
    2. 杠杆：``debt_to_assets`` 越高越负（50% 中性）；
    3. 商誉 + 审计意见：非标准审计意见 → -1；商誉越大越负（按 10 亿饱和）。

    对三层可用子分取均值；全部缺失 → ``None``。
    """
    layers: list[float] = []

    if r.pledge_ratio is not None:
        layers.append(-_saturate(r.pledge_ratio, _PLEDGE_SCALE))
    if r.debt_to_assets is not None:
        layers.append(-_saturate(r.debt_to_assets - _DEBT_NEUTRAL, _DEBT_SCALE))

    # 第三层：商誉 + 审计意见（二者都缺失才整层缺失）
    audit_goodwill: list[float] = []
    if r.audit is not None and r.audit.audit_opinion:
        opinion = r.audit.audit_opinion
        audit_goodwill.append(0.0 if "标准无保留" in opinion else -1.0)
    if r.goodwill is not None:
        audit_goodwill.append(-_saturate(r.goodwill, _GOODWILL_SCALE))
    layer3 = _mean(audit_goodwill)
    if layer3 is not None:
        layers.append(layer3)

    return _mean(layers)


def adjust_market_exposure(
    market: MarketFactor,
    *,
    e_revision: float | None = None,
    segment_divergence: float | None = None,
) -> tuple[float | None, float | None]:
    """市场 regime 软约束（改造 E + P1 放宽）：E↑ **或** segment divergence 成立时暴露上下限上浮。

    熊市仍压低暴露（保留原 ``position_low/high``），但当「E 上修（``e_revision > 0.3``）
    **或** 结构性亮点（``segment_divergence > 15pp``）」任一成立时，暴露上下限各上浮
    ``_REGIME_LIFT``（如 [0, 0.2] → [0.1, 0.3]），体现「熊市里的结构性主线」。
    P1 放宽：从「AND」改为「OR」——结构性亮点（细分增速显著跑赢整体）单独成立即可
    触发上浮，不必强求同时存在分析师上修（很多结构主线标的 E 修订滞后甚至缺失）。
    确定性纯函数：只读输入，不改外部状态。
    """
    lift = (e_revision is not None and e_revision > _E_REVISION_EXEMPT) or (
        segment_divergence is not None and segment_divergence > _SEGMENT_DIVERGENCE_THRESHOLD
    )
    if not lift:
        return market.position_low, market.position_high
    low = min(1.0, market.position_low + _REGIME_LIFT) if market.position_low is not None else None
    high = min(1.0, market.position_high + _REGIME_LIFT) if market.position_high is not None else None
    return low, high


def _market_summary(
    m: MarketFactor,
    *,
    e_revision: float | None = None,
    segment_divergence: float | None = None,
) -> dict[str, Any]:
    """M：直接复用 ``market_score`` 摘要（0-100），透传 MarketFactor 关键字段。

    改造 E：``position_low`` / ``position_high`` 经 ``adjust_market_exposure`` 软约束
    （E↑ + segment divergence 时上下限上浮）。缺失字段以显式 ``None`` 保留
    （不静默回退 0）；``regime`` 沿用其 ``""`` 缺省语义。
    """
    low, high = adjust_market_exposure(m, e_revision=e_revision, segment_divergence=segment_divergence)
    return {
        "market_score": m.market_score,  # 0-100
        "regime": m.regime,
        "position_low": low,
        "position_high": high,
        "hs300_pe_ttm": m.hs300_pe_ttm,
        "hs300_pb": m.hs300_pb,
        "hs300_close": m.hs300_close,
        "breadth_above_ma60_pct": m.breadth_above_ma60_pct,
        "market_turnover": m.market_turnover,
        "margin_balance": m.margin_balance,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────────────


def compress_scores(
    snapshot: FactorSnapshot | None,
    *,
    marginal_mode: str = MARGINAL_MODE_OFF,
    ocf_penalty_basis: str = OCF_PENALTY_BASIS_SIGN,
    f_recurring_mode: str = RECURRING_MODE_OFF,
    segment_bonus_mode: str = SEGMENT_BONUS_MODE_LEGACY,
) -> VariableScores:
    """把七因子快照压缩为七方向分（``FactorSnapshot → VariableScores``）。

    七个方向分独立产出、不跨因子求和；任一因子方向分输入缺失时对应字段为 ``None``。
    只读 ``snapshot`` 的原始 canonical 值，**唯一例外**：P0 设计 §9.2 的 coverage 护栏
    会把 ``consensus_coverage_low`` 登记进 ``snapshot.missing_facts``（复用 factors 的
    缺失登记通道），供 ``resolve_midterm_decision`` 产出 ``consensus_missing`` Issue。

    Args:
        snapshot: 七因子快照（``alphabee.midterm.factors.get_factor_snapshot`` 产物）。
        marginal_mode: F 边际模式（F-Marginal §6.3）：``off``（缺省，逐值等于改造前）/
            ``shadow``（算但不进 F）/ ``on``（进 F）。缺省 ``off`` 保证所有既有直接调用点
            （含单测 ``compress_scores(snap)``）行为逐值不变；``decision_model.evaluate``
            会 resolve 配置后显式传入。
        ocf_penalty_basis: OCF 惩罚口径（P1-2）：``sign``（**缺省**，旧行为：累计 OCF 符号）/
            ``ttm``（滚动 12 个月；金融股跳过，TTM 缺失回落 sign）。缺省 ``sign`` 保证既有
            直接调用点零变化；``decision_model.evaluate`` 会 resolve 配置后显式传入。
        f_recurring_mode: 扣非保守锚（P1-3）：``off``（**缺省**，旧行为）/``shadow``（算但
            不进 F）/``on``（净利项用 ``min(归母, 扣非)``）。缺省 ``off`` 保证直接调用点零变化；
            ``decision_model.evaluate`` 会 resolve 配置（缺省 ``shadow``）后显式传入。
        segment_bonus_mode: 分部 bonus 口径（P1-6）：``legacy``（**缺省**，旧行为）/
            ``period_aligned``（期间对齐 + 10% 收入占比门槛）。缺省 ``legacy`` 保证直接调用点
            零变化；``decision_model.evaluate`` 会 resolve 配置（缺省 ``period_aligned``）后显式传入。

    Returns:
        ``VariableScores``：``m`` 为市场摘要（复用 ``market_score`` 0-100），
        其余六个为 ``[-1, 1]`` 方向分（缺失 → ``None``）。
    """
    if snapshot is None:
        return VariableScores()

    e_revision = _revision(snapshot.expectation)
    # P0 §9.2：覆盖不足 → E 研报修正不可靠（_revision 已返回 None），并把护栏触发
    # 显式登记进 missing_facts（只留痕，不静默）。
    if snapshot.expectation.coverage_count is None or snapshot.expectation.coverage_count < _MIN_CONSENSUS_COVERAGE:
        if "consensus_coverage_low" not in snapshot.missing_facts:
            snapshot.missing_facts.append("consensus_coverage_low")
    segment_divergence = _segment_divergence(snapshot.fundamental)

    return VariableScores(
        m=_market_summary(snapshot.market, e_revision=e_revision, segment_divergence=segment_divergence),
        f_fundamental_trend=_fundamental_trend(
            snapshot.fundamental,
            snapshot.expectation,
            marginal_mode=marginal_mode,
            ocf_penalty_basis=ocf_penalty_basis,
            f_recurring_mode=f_recurring_mode,
            segment_bonus_mode=segment_bonus_mode,
        ),
        e_revision=e_revision,
        t_relative_strength=_relative_strength(snapshot.trend),
        v_valuation_percentile=_valuation_percentile(snapshot.valuation),
        c_crowding=_crowding(snapshot.crowding, e_revision=e_revision, segment_divergence=segment_divergence),
        r_risk=_risk(snapshot.risk),
    )
