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
_RS_SCALE = 20.0  # PERCENT 相对强度超额收益饱和点（20% → ±1）
_HOLDER_CHANGE_SCALE = 20.0  # PERCENT 股东户数增幅饱和点（20% → ±1）
_PLEDGE_SCALE = 50.0  # PERCENT 质押率饱和点（50% → -1）
_DEBT_NEUTRAL = 50.0  # PERCENT 资产负债率中性点（50% → 0）
_DEBT_SCALE = 50.0  # PERCENT 资产负债率饱和步长（100% → -1，0% → +1）
_GOODWILL_SCALE = 1_000_000_000.0  # CNY 商誉饱和点（10 亿 → -1）

# F 现金质量 / 超预期（§11「S2 是 PositiveEvidenceChange 非 Growth」+ profit_without_cash）
_CASH_NEGATIVE_PENALTY = 0.5  # 单个负现金流（经营或自由）对 F 的惩罚力度
_BEAT_BONUS = 0.2  # net_profit_yoy 超预告上限的 beat 加分

# 预期差（P0-2）：预告 miss 惩罚——实际净利增速远低于预告下限 → 显著负向。
# 「预告 +30~40% 但实际 +1.54%」这类预期差是 S0–S5 框架的核心信号，必须在 F 层显式捕获，
# 而非只奖励 beat 不惩罚 miss（不对称）。
_MISS_PENALTY = 0.3  # 显著 miss 对 F 的惩罚力度（比 beat 加分 0.2 更重：miss 信息量更大）
_MISS_GAP_THRESHOLD = 20.0  # PERCENT 实际低于预告下限的幅度阈值（>20pp = 显著 miss）

# 改造 E（设计 MIDTERM_INSIGHT_INJECTION_DESIGN.md §4）结构性洞察 / 语境归一化参数：
_SEGMENT_DIVERGENCE_THRESHOLD = 15.0  # PERCENT 最快细分增速跑赢整体阈值（>15pp = 结构性亮点）
_SEGMENT_DIVERGENCE_BONUS = 0.2  # F 方向分正向修正幅度（结构性亮点）
_E_REVISION_EXEMPT = 0.3  # E 上修阈值（e_revision > 0.3 = 强催化）
_CROWDING_EXEMPT_HALF = 0.5  # 启动期高换手豁免：拥挤度扣分减半
_REGIME_LIFT = 0.1  # regime 软约束：E↑ + segment divergence 时市场暴露上下限上浮幅度


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


# ─────────────────────────────────────────────────────────────────────────────
# 七个方向分（逐一独立产出）
# ─────────────────────────────────────────────────────────────────────────────


def _cash_quality_penalty(f: FundamentalFactor) -> float:
    """现金质量惩罚（profit_without_cash）：经营/自由现金流为负 → 负向。

    每项为负记 ``-_CASH_NEGATIVE_PENALTY``；现金流缺失（``None``）不惩罚（不静默假设）。
    """
    penalty = 0.0
    if f.operating_cashflow is not None and f.operating_cashflow < 0.0:
        penalty += _CASH_NEGATIVE_PENALTY
    if f.free_cashflow is not None and f.free_cashflow < 0.0:
        penalty += _CASH_NEGATIVE_PENALTY
    return -penalty


def _beat_bonus(net_profit_yoy: float | None, max_change: float | None) -> float:
    """超预期 beat（§11「S2 是 PositiveEvidenceChange 非 Growth」）。

    ``net_profit_yoy`` 超预告上限 → ``+_BEAT_BONUS``；in-line（含落区间内）/
    预告区间缺失 → 0（不额外加分）。
    """
    if net_profit_yoy is not None and max_change is not None and net_profit_yoy > max_change:
        return _BEAT_BONUS
    return 0.0


def _miss_penalty(net_profit_yoy: float | None, min_change: float | None) -> float:
    """预告 miss（P0-2 预期差）：实际净利增速远低于预告下限 → 显著负向。

    ``min_change - net_profit_yoy`` 为「低于预告下限」的幅度（pp，正=miss）；
    超过 ``_MISS_GAP_THRESHOLD``（>20pp）判定为显著 miss，扣 ``-_MISS_PENALTY``。
    预告下限缺失 / 实际缺失 → 0（不编造预期差）。
    """
    if net_profit_yoy is not None and min_change is not None:
        gap = min_change - net_profit_yoy
        if gap > _MISS_GAP_THRESHOLD:
            return -_MISS_PENALTY
    return 0.0


def _segment_divergence(f: FundamentalFactor) -> float | None:
    """最快细分增速 − 整体营收增速（pp，改造 E）；任一缺失 → ``None``。"""
    if f.segment_fastest_yoy is None or f.revenue_yoy is None:
        return None
    return f.segment_fastest_yoy - f.revenue_yoy


def _segment_bonus(f: FundamentalFactor) -> float:
    """结构性亮点（改造 E）：最快细分显著跑赢整体（divergence > 15pp）→ F 正向修正 +0.2。"""
    divergence = _segment_divergence(f)
    if divergence is not None and divergence > _SEGMENT_DIVERGENCE_THRESHOLD:
        return _SEGMENT_DIVERGENCE_BONUS
    return 0.0


def _fundamental_trend(f: FundamentalFactor, e: ExpectationFactor) -> float | None:
    """F 方向分：边际改善 + 现金质量惩罚 + 超预期 beat + 结构性亮点（改造 E）。

    §3.3 口径 ``gross_margin_trend + revenue_yoy + net_profit_yoy`` 的边际方向；
    ``FactorSnapshot`` 是单帧、无 ``gross_margin_trend`` 时序，故用营收/净利/EPS
    三组 YoY（本身即边际变化）作为「改善」代理，并叠加：

    - 现金质量（profit_without_cash）：经营/自由现金流为负 → 显著惩罚（避免
      net_profit_yoy 高增长掩盖现金流失血）；
    - 超预期（beat）：net_profit_yoy 超预告上限才加分，in-line 不加分；
    - 预告 miss（P0-2 预期差）：net_profit_yoy 远低于预告下限 → 显著扣分
      （预告 +30~40% 但实际 +1.54% 的预期差是框架核心信号，不能只奖 beat 不罚 miss）；
    - 结构性亮点（改造 E）：最快细分增速显著跑赢整体（segment divergence > 15pp）
      → 正向修正（整体 +17.94% 但高速通信线 +35.44% 的「结构性突变」）。

    全部 YoY 缺失 → ``None``；最终结果 clip 到 ``[-1, 1]``。
    """
    yoys = [f.revenue_yoy, f.net_profit_yoy, f.eps_growth_yoy]
    contribs = [_saturate(v, _FUNDAMENTAL_YOY_SCALE) for v in yoys if v is not None]
    growth = _mean(contribs)
    if growth is None:
        return None

    score = (
        growth
        + _cash_quality_penalty(f)
        + _beat_bonus(f.net_profit_yoy, e.profit_forecast_max_change)
        + _miss_penalty(f.net_profit_yoy, e.profit_forecast_min_change)
        + _segment_bonus(f)
    )
    return _saturate(score, 1.0)


def _revision(e: ExpectationFactor) -> float | None:
    """E 方向分（核心因子）：分析师上修 → 正。

    §3.3 口径 ``eps_fy1_revision_1m/3m + revision_breadth``。revision 字段为 PERCENT
    （上修为正），用 ``_log1p_saturate`` 反饱和（保留 +45% vs +100% 幅度差异）；
    ``revision_breadth`` 为上调占比 0-1（0.5 中性 → 映射到 0）。全部缺失 → ``None``。
    """
    contribs: list[float] = []
    for rev in (e.eps_fy1_revision_1m, e.eps_fy1_revision_3m, e.eps_fy2_revision_1m):
        if rev is not None:
            contribs.append(_log1p_saturate(rev, _REVISION_REF_LOG))
    if e.revision_breadth is not None:
        contribs.append(2.0 * e.revision_breadth - 1.0)  # 0.5 → 0，1 → +1，0 → -1
    return _mean(contribs)


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


def compress_scores(snapshot: FactorSnapshot | None) -> VariableScores:
    """把七因子快照压缩为七方向分（``FactorSnapshot → VariableScores``）。

    七个方向分独立产出、不跨因子求和；任一因子方向分输入缺失时对应字段为 ``None``。
    只读 ``snapshot``，不修改其原始 canonical 值。

    Args:
        snapshot: 七因子快照（``alphabee.midterm.factors.get_factor_snapshot`` 产物）。

    Returns:
        ``VariableScores``：``m`` 为市场摘要（复用 ``market_score`` 0-100），
        其余六个为 ``[-1, 1]`` 方向分（缺失 → ``None``）。
    """
    if snapshot is None:
        return VariableScores()

    e_revision = _revision(snapshot.expectation)
    segment_divergence = _segment_divergence(snapshot.fundamental)

    return VariableScores(
        m=_market_summary(snapshot.market, e_revision=e_revision, segment_divergence=segment_divergence),
        f_fundamental_trend=_fundamental_trend(snapshot.fundamental, snapshot.expectation),
        e_revision=e_revision,
        t_relative_strength=_relative_strength(snapshot.trend),
        v_valuation_percentile=_valuation_percentile(snapshot.valuation),
        c_crowding=_crowding(snapshot.crowding, e_revision=e_revision, segment_divergence=segment_divergence),
        r_risk=_risk(snapshot.risk),
    )
