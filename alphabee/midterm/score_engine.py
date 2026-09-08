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
_REVISION_SCALE = 5.0  # PERCENT EPS 上修饱和点（5% → ±1）
_RS_SCALE = 20.0  # PERCENT 相对强度超额收益饱和点（20% → ±1）
_HOLDER_CHANGE_SCALE = 20.0  # PERCENT 股东户数增幅饱和点（20% → ±1）
_PLEDGE_SCALE = 50.0  # PERCENT 质押率饱和点（50% → -1）
_DEBT_NEUTRAL = 50.0  # PERCENT 资产负债率中性点（50% → 0）
_DEBT_SCALE = 50.0  # PERCENT 资产负债率饱和步长（100% → -1，0% → +1）
_GOODWILL_SCALE = 1_000_000_000.0  # CNY 商誉饱和点（10 亿 → -1）


# ─────────────────────────────────────────────────────────────────────────────
# 通用辅助（纯函数）
# ─────────────────────────────────────────────────────────────────────────────


def _saturate(value: float, scale: float) -> float:
    """把 ``value / scale`` 饱和到 ``[-1, 1]``（正负对称）。"""
    ratio = value / scale
    return max(-1.0, min(1.0, ratio))


def _mean(values: list[float]) -> float | None:
    """均值；空列表 → ``None``（缺失而非回退 0）。"""
    return sum(values) / len(values) if values else None


# ─────────────────────────────────────────────────────────────────────────────
# 七个方向分（逐一独立产出）
# ─────────────────────────────────────────────────────────────────────────────


def _fundamental_trend(f: FundamentalFactor) -> float | None:
    """F 方向分：边际改善 → 正。

    §3.3 口径 ``gross_margin_trend + revenue_yoy + net_profit_yoy`` 的边际方向；
    ``FactorSnapshot`` 是单帧、无 ``gross_margin_trend`` 时序，故用营收/净利/EPS
    三组 YoY（本身即边际变化）作为「改善」代理。全部缺失 → ``None``。
    """
    yoys = [f.revenue_yoy, f.net_profit_yoy, f.eps_growth_yoy]
    contribs = [_saturate(v, _FUNDAMENTAL_YOY_SCALE) for v in yoys if v is not None]
    return _mean(contribs)


def _revision(e: ExpectationFactor) -> float | None:
    """E 方向分（核心因子）：分析师上修 → 正。

    §3.3 口径 ``eps_fy1_revision_1m/3m + revision_breadth``。revision 字段为 PERCENT
    （上修为正），``revision_breadth`` 为上调占比 0-1（0.5 中性 → 映射到 0）。
    全部缺失 → ``None``。
    """
    contribs: list[float] = []
    for rev in (e.eps_fy1_revision_1m, e.eps_fy1_revision_3m, e.eps_fy2_revision_1m):
        if rev is not None:
            contribs.append(_saturate(rev, _REVISION_SCALE))
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


def _crowding(c: CrowdingFactor) -> float | None:
    """C 方向分：越拥挤 → 越负。

    §3.3 口径 ``holder_count_change + hot_rank + turnover_rate_percentile``：

    - ``holder_count_change``（股东户数增幅）：户数↓ → 筹码集中 → 负（§3.3 原注），
      故户数↑（筹码分散）→ 正；
    - ``hot_rank``：人气榜排名（1 最热）；排名越低越拥挤 → 越负；
    - ``turnover_rate_percentile``：换手率分位越高越拥挤 → 越负。

    全部缺失 → ``None``。
    """
    contribs: list[float] = []
    if c.holder_count_change is not None:
        contribs.append(_saturate(c.holder_count_change, _HOLDER_CHANGE_SCALE))
    if c.hot_rank is not None:
        # 人气榜 top100：rank=1 最热 → 最负；rank=100 最冷 → 0。
        contribs.append(-_saturate(100.0 - float(c.hot_rank), 100.0))
    if c.turnover_rate_percentile is not None:
        contribs.append(1.0 - 2.0 * c.turnover_rate_percentile)  # 分位 1 → -1，0 → +1
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


def _market_summary(m: MarketFactor) -> dict[str, Any]:
    """M：直接复用 ``market_score`` 摘要（0-100），透传 MarketFactor 关键字段。

    缺失字段以显式 ``None`` 保留（不静默回退 0）；``regime`` 沿用其 ``""`` 缺省语义。
    """
    return {
        "market_score": m.market_score,  # 0-100
        "regime": m.regime,
        "position_low": m.position_low,
        "position_high": m.position_high,
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

    return VariableScores(
        m=_market_summary(snapshot.market),
        f_fundamental_trend=_fundamental_trend(snapshot.fundamental),
        e_revision=_revision(snapshot.expectation),
        t_relative_strength=_relative_strength(snapshot.trend),
        v_valuation_percentile=_valuation_percentile(snapshot.valuation),
        c_crowding=_crowding(snapshot.crowding),
        r_risk=_risk(snapshot.risk),
    )
