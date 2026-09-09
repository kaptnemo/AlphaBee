"""FactorSnapshot 查询与封装 —— 七因子（F/E/T/V/C/R/M）canonical 字段映射层。

本模块是「外部数据源字段 → canonical 字段 → FactorSnapshot」单向链路里，
把已有 fact tools / collectors / market_regime 子系统的 canonical 字段**组装**成
:class:`~alphabee.midterm.models.FactorSnapshot` 的唯一入口：

- ``get_factor_snapshot(symbol) -> FactorSnapshot``：主函数，聚合七个维度。
- ``build_*_factor(...)``：七个维度的 builder，纯映射（无业务评分逻辑），
  便于单测用合成数据喂入验证字段映射正确性。

字段治理约束（``alphabee-schema-steward``）：

- 只读各数据源的 **canonical 字段名**（``alphabee/schemas/*.yaml``），
  绝不直接引用 Tushare / AkShare / 东方财富 外部列名。
- 缺失值显式 ``None``，并登记到 ``FactorSnapshot.missing_facts``；
  绝不静默回退为 0 或中性。数值解析失败也一律 ``None``（本模块的 ``_opt``
  刻意区别于 fact tools 里 ``safe_float`` 的「无效值兜底 0」语义）。
- ``direction`` / ``score`` / ``valuation.percentile`` 等由 ``score_engine`` /
  ``classifier`` 输出的字段**不在本模块计算**，保持模型默认值（本模块只做
  数据映射，不承载业务逻辑）。

管线契约（``alphabee-pipeline-contract-steward``）：

- ``FactorSnapshot`` 是 ``CompanyStateArtifact.factor_snapshot`` 已声明的消费契约；
  ``get_factor_snapshot`` 在 ``alphabee.midterm.__init__`` 导出，下游可直接
  ``from alphabee.midterm import get_factor_snapshot`` 消费，无 dead-end。
- 所有数据源模块采用**函数内惰性导入**：``import alphabee.midterm.factors``
  本身不触发 tushare/akshare 初始化副作用（与 ``market_regime`` 的惰性加载约定一致）。
"""

from __future__ import annotations

import datetime
import math
from typing import Any

from alphabee.midterm.models import (
    AuditSnapshot,
    CrowdingFactor,
    ExpectationFactor,
    FactorSnapshot,
    FundamentalFactor,
    MarketFactor,
    RiskFactor,
    TrendFactor,
    ValuationFactor,
)

# ─────────────────────────────────────────────────────────────────────────────
# 各维度承载的 canonical 数据字段（用于缺失登记；不含 direction/score/percentile
# 等由 score_engine 输出、或纯文本占位的字段）
# ─────────────────────────────────────────────────────────────────────────────

_FUNDAMENTAL_FIELDS: tuple[str, ...] = (
    "revenue_yoy",
    "net_profit_yoy",
    "eps_growth_yoy",
    "roe",
    "gross_margin",
    "net_margin",
    "operating_cashflow",
    "free_cashflow",
    "debt_to_assets",
    "current_ratio",
    "goodwill",
)

_EXPECTATION_FIELDS: tuple[str, ...] = (
    "profit_forecast_min_change",
    "profit_forecast_max_change",
    "express_revenue_yoy",
    "express_net_profit_yoy",
    "eps_fy1",
    "eps_fy2",
    "eps_fy3",
    "target_price",
    "rating_mean",
    "coverage_count",
    "eps_fy1_revision_1m",
    "eps_fy1_revision_3m",
    "eps_fy2_revision_1m",
    "revision_breadth",
    "revision_acceleration",
    "rating_upgrade_1m",
    "rating_downgrade_1m",
)

_TREND_FIELDS: tuple[str, ...] = (
    "rs_stock_market_20d",
    "rs_stock_market_60d",
    "rs_stock_industry_20d",
    "rs_stock_industry_60d",
    "rs_industry_market_20d",
    "rs_industry_market_60d",
    "industry_breadth_20",
    "industry_breadth_60",
    "price_change_pct",
    "ma20",
    "ma60",
    "ma120",
)

_VALUATION_FIELDS: tuple[str, ...] = (
    "pe_ttm",
    "pb_ratio",
    "pe_ttm_5y_avg",
    "pe_ttm_5y_percentile",
    "pb_5y_percentile",
    "industry_pe_ttm",
    "industry_pb",
    "peer_median_pe_ttm",
    "peer_median_pb",
)

_CROWDING_FIELDS: tuple[str, ...] = (
    "turnover_rate",
    "turnover_rate_percentile",
    "amount_pct_of_market",
    "holder_count_change",
    "per_capita_holding_change",
    "institutional_holding_ratio",
    "margin_balance_yoy",
    "analyst_coverage_rank",
    "hot_rank",
    "news_heat",
    "leader_concentration",
)

# R 因子数值型 canonical 字段（text 占位字段 repurchase_progress/news_title 用 "" 缺省，
# 不登记为 missing_facts；audit 子对象单独处理）
_RISK_NUMERIC_FIELDS: tuple[str, ...] = ("pledge_ratio", "debt_to_assets", "goodwill")

_AUDIT_FIELDS: tuple[str, ...] = ("audit_opinion", "audit_agency", "audit_fees", "audit_sign")

_MARKET_FIELDS: tuple[str, ...] = (
    "market_score",
    "position_low",
    "position_high",
    "hs300_pe_ttm",
    "hs300_pb",
    "hs300_close",
    "breadth_above_ma60_pct",
    "market_turnover",
    "margin_balance",
)


# ─────────────────────────────────────────────────────────────────────────────
# 值解析 / 通用辅助（缺失→None，绝不静默回退 0）
# ─────────────────────────────────────────────────────────────────────────────


def _opt(value: Any) -> float | None:
    """str/int/float/空串 → float；NaN / 空串 / 无效值 → None。

    与 ``alphabee.agents.facts.tools._utils.safe_float`` 的区别：本函数在无法解析时
    返回 ``None``（缺失），而不是兜底 0 —— 快照层「缺失即缺失」，不能用 0 冒充数值。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return None if math.isnan(v) else v
    text = str(value).strip()
    if text in ("", "-", "--", "None", "nan", "NaN", "null"):
        return None
    try:
        v = float(text)
        return None if math.isnan(v) else v
    except (TypeError, ValueError):
        return None


def _opt_int(value: Any) -> int | None:
    """同 ``_opt``，但产出 ``int | None``（用于 coverage_count / 排名 / 评级计数）。"""
    v = _opt(value)
    return int(v) if v is not None else None


def _opt_text(value: Any) -> str | None:
    """str → str；空串 / nan / None → None（用于 audit 文本字段的「缺失即 None」）。"""
    if value is None:
        return None
    s = str(value).strip()
    return None if s in ("", "nan", "None") else s


def _text(value: Any) -> str:
    """str → str；空串 / nan / None → ''（用于以 '' 为缺省语义的展示型文本字段）。"""
    if value is None:
        return ""
    s = str(value).strip()
    return "" if s in ("nan", "None", "") else s


def _first(records: Any) -> dict[str, Any]:
    """取列表首条记录（最新一期）；已为 dict 时原样返回；空/None → {}。"""
    if isinstance(records, (list, tuple)):
        for r in records:
            if isinstance(r, dict):
                return r
        return {}
    return records if isinstance(records, dict) else {}


def _missing_fields(obj: Any, fields: tuple[str, ...]) -> list[str]:
    """返回 ``obj`` 上值为 ``None`` 的 canonical 字段名列表。"""
    return [name for name in fields if getattr(obj, name, None) is None]


def _dedupe(items: list[str]) -> list[str]:
    """去重并保持顺序，过滤空串。"""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _normalize_ts_code(symbol: str) -> str:
    """股票代码归一化为 Tushare 标准格式（本地纯函数，避免导入 fact tools 包触发 tushare 初始化）。

    与 ``alphabee.agents.facts.tools._utils.normalize_ts_code`` 同语义：仅做格式归一，
    不产生任何数据源副作用（fact tools 包的 ``tools/__init__`` 会级联导入
    ``collectors.tushare`` 并调用 ``ts.set_token``）。
    """
    s = symbol.strip().lower()
    if s.startswith("sh"):
        return s[2:].upper() + ".SH"
    if s.startswith("sz"):
        return s[2:].upper() + ".SZ"
    if s.startswith("bj"):
        return s[2:].upper() + ".BJ"
    upper = symbol.strip().upper()
    if upper.endswith((".SH", ".SZ", ".BJ")):
        return upper
    if upper.startswith(("6", "9")):
        return upper + ".SH"
    if upper.startswith(("0", "3")):
        return upper + ".SZ"
    if upper.startswith(("4", "8")):
        return upper + ".BJ"
    raise ValueError(f"Cannot determine exchange for symbol: {symbol}")


def _to_pure_code(ts_code: str) -> str:
    """``600519.SH`` → ``600519``（6 位，去交易所后缀，供 collectors 入参）。"""
    return ts_code.split(".")[0]


# ─────────────────────────────────────────────────────────────────────────────
# 七个因子维度 builder（纯映射，输入为已拉取的 canonical 数据，便于单测）
# ─────────────────────────────────────────────────────────────────────────────


def build_fundamental_factor(fin_data: dict[str, Any] | None) -> tuple[FundamentalFactor, list[str]]:
    """F 因子：从 ``get_financial_fact`` 的多期嵌套结构取最新一期 canonical 字段。

    ``get_financial_fact`` 返回 ``{stock_code, ref_dates, income[], balance[],
    cashflow[], fina[]}``，各 record 已是 canonical 字段名（adapter 层完成映射）。
    本 builder 只取 ``fina/balance/cashflow`` 最新一条（offset=0）。
    """
    data = fin_data or {}
    fina = _first(data.get("fina"))
    balance = _first(data.get("balance"))
    cashflow = _first(data.get("cashflow"))

    factor = FundamentalFactor(
        revenue_yoy=_opt(fina.get("revenue_yoy")),
        net_profit_yoy=_opt(fina.get("net_profit_yoy")),
        eps_growth_yoy=_opt(fina.get("eps_growth_yoy")),
        roe=_opt(fina.get("roe")),
        gross_margin=_opt(fina.get("gross_margin")),
        net_margin=_opt(fina.get("net_margin")),
        operating_cashflow=_opt(cashflow.get("operating_cashflow")),
        free_cashflow=_opt(fina.get("free_cashflow")),
        debt_to_assets=_opt(fina.get("debt_to_assets")),
        current_ratio=_opt(fina.get("current_ratio")),
        goodwill=_opt(balance.get("goodwill")),
    )
    return factor, _missing_fields(factor, _FUNDAMENTAL_FIELDS)


def build_expectation_factor(
    exp_data: dict[str, Any] | None,
    consensus: Any | None,
) -> tuple[ExpectationFactor, list[str]]:
    """E 因子：公告类预期（``get_expectation_fact``）+ 分析师一致预期（``build_consensus``）。

    - 预告/快报取最新一条 record（``forecast[0]`` / ``express[0]``）。
    - 一致预期字段来自 ``ConsensusOutput.values``（13 个 canonical 字段）。
    """
    data = exp_data or {}
    forecast = _first(data.get("forecast"))
    express = _first(data.get("express"))
    cvals = getattr(consensus, "values", None) or {}

    factor = ExpectationFactor(
        profit_forecast_min_change=_opt(forecast.get("profit_forecast_min_change")),
        profit_forecast_max_change=_opt(forecast.get("profit_forecast_max_change")),
        express_revenue_yoy=_opt(express.get("express_revenue_yoy")),
        express_net_profit_yoy=_opt(express.get("express_net_profit_yoy")),
        eps_fy1=_opt(cvals.get("eps_fy1")),
        eps_fy2=_opt(cvals.get("eps_fy2")),
        eps_fy3=_opt(cvals.get("eps_fy3")),
        target_price=_opt(cvals.get("target_price")),
        rating_mean=_opt(cvals.get("rating_mean")),
        coverage_count=_opt_int(cvals.get("coverage_count")),
        eps_fy1_revision_1m=_opt(cvals.get("eps_fy1_revision_1m")),
        eps_fy1_revision_3m=_opt(cvals.get("eps_fy1_revision_3m")),
        eps_fy2_revision_1m=_opt(cvals.get("eps_fy2_revision_1m")),
        revision_breadth=_opt(cvals.get("revision_breadth")),
        revision_acceleration=_opt(cvals.get("revision_acceleration")),
        rating_upgrade_1m=_opt_int(cvals.get("rating_upgrade_1m")),
        rating_downgrade_1m=_opt_int(cvals.get("rating_downgrade_1m")),
    )
    return factor, _missing_fields(factor, _EXPECTATION_FIELDS)


def build_trend_factor(
    market_data: dict[str, Any] | None,
    industry_data: dict[str, Any] | None,
) -> tuple[TrendFactor, list[str]]:
    """T 因子：价量/均线（``get_market_fact``）+ 相对强度/行业宽度（``get_industry_fact``）。

    RS 与估值分位是 ``get_market_fact`` / ``get_industry_fact`` 顶层自算字段
    （``rs_stock_market_20d`` 等）；均线在 ``get_market_fact`` 的 ``ma`` 子结构。
    """
    m = market_data or {}
    ind = industry_data or {}
    latest_daily = m.get("latest_daily") or {}
    ma = m.get("ma") or {}

    factor = TrendFactor(
        rs_stock_market_20d=_opt(m.get("rs_stock_market_20d")),
        rs_stock_market_60d=_opt(m.get("rs_stock_market_60d")),
        rs_stock_industry_20d=_opt(ind.get("rs_stock_industry_20d")),
        rs_stock_industry_60d=_opt(ind.get("rs_stock_industry_60d")),
        rs_industry_market_20d=_opt(ind.get("rs_industry_market_20d")),
        rs_industry_market_60d=_opt(ind.get("rs_industry_market_60d")),
        industry_breadth_20=_opt(ind.get("industry_breadth_20")),
        industry_breadth_60=_opt(ind.get("industry_breadth_60")),
        price_change_pct=_opt(latest_daily.get("price_change_pct")),
        ma20=_opt(ma.get("ma20")),
        ma60=_opt(ma.get("ma60")),
        ma120=_opt(ma.get("ma120")),
    )
    return factor, _missing_fields(factor, _TREND_FIELDS)


def build_valuation_factor(
    market_data: dict[str, Any] | None,
    industry_data: dict[str, Any] | None,
) -> tuple[ValuationFactor, list[str]]:
    """V 因子：个股估值（``get_market_fact``）+ 行业估值（``get_industry_fact`` 的 sw_daily）。

    ``peer_median_pe_ttm / peer_median_pb`` 本批无数据源（``get_competition_fact``
    不在本任务声明的数据源内），显式保持 ``None`` 并登记 missing。
    ``percentile``（综合估值分位）是 score_engine 的输出字段，本模块不计算。
    """
    m = market_data or {}
    ind = industry_data or {}
    db = m.get("latest_daily_basic") or {}
    sw_latest = _first(ind.get("sw_daily"))

    factor = ValuationFactor(
        pe_ttm=_opt(db.get("pe_ttm")),
        pb_ratio=_opt(db.get("pb_ratio")),
        pe_ttm_5y_avg=_opt(m.get("pe_ttm_5y_avg")),
        pe_ttm_5y_percentile=_opt(m.get("pe_ttm_5y_percentile")),
        pb_5y_percentile=_opt(m.get("pb_5y_percentile")),
        industry_pe_ttm=_opt(sw_latest.get("industry_pe_ttm")),
        industry_pb=_opt(sw_latest.get("industry_pb")),
        peer_median_pe_ttm=None,
        peer_median_pb=None,
    )
    return factor, _missing_fields(factor, _VALUATION_FIELDS)


def _single_stock_coverage_sample(crowding: Any) -> bool:
    """判断 ``analyst_coverage_rank`` 是否来自单股票样本（无对标样本）。

    单股票样本下 collector 的 ``rank_by_coverage`` 恒返回 1（「覆盖排名第 1」不可信）；
    collector 已在 ``CrowdingOutput.warnings`` 登记单样本 warning，据此把该字段显式
    缺失，而非把 rank=1 当作真实排名。
    """
    warnings = getattr(crowding, "warnings", None) or []
    return any("single-stock sample" in str(w) for w in warnings)


def build_crowding_factor(
    market_data: dict[str, Any] | None,
    crowding: Any | None,
) -> tuple[CrowdingFactor, list[str]]:
    """C 因子：换手率绝对量（``get_market_fact``）+ P0 拥挤度（``build_crowding``）。

    ``build_crowding`` 返回 ``CrowdingOutput``：``values`` 承载 6 个 P0 canonical
    字段（holder/per_capita/hot_rank/coverage_rank/turnover_rate_percentile/
    amount_pct_of_market），``missing`` 记录 ``crowding_missing: <field>``。
    覆盖热度排名仅在传入有效对标样本时才是真实排名；单股票样本（``warnings`` 含
    single-stock sample）时 ``analyst_coverage_rank`` 显式 ``None``（不把 rank=1
    当作可信排名）。其余字段（前十大集中度/两融同比/新闻热度/龙头集中度）本批无数据源 → ``None``。
    """
    m = market_data or {}
    db = m.get("latest_daily_basic") or {}
    cvals = getattr(crowding, "values", None) or {}

    coverage_rank = _opt_int(cvals.get("analyst_coverage_rank"))
    if _single_stock_coverage_sample(crowding):
        coverage_rank = None  # 单股票样本 rank 恒为 1 → 显式缺失（登记进 missing_facts）

    factor = CrowdingFactor(
        turnover_rate=_opt(db.get("turnover_rate")),
        turnover_rate_percentile=_opt(cvals.get("turnover_rate_percentile")),
        amount_pct_of_market=_opt(cvals.get("amount_pct_of_market")),
        holder_count_change=_opt(cvals.get("holder_count_change")),
        per_capita_holding_change=_opt(cvals.get("per_capita_holding_change")),
        institutional_holding_ratio=None,
        margin_balance_yoy=None,
        analyst_coverage_rank=coverage_rank,
        hot_rank=_opt_int(cvals.get("hot_rank")),
        news_heat=None,
        leader_concentration=None,
    )
    return factor, _missing_fields(factor, _CROWDING_FIELDS)


def build_risk_factor(
    risk_data: dict[str, Any] | None,
    fin_data: dict[str, Any] | None,
) -> tuple[RiskFactor, list[str]]:
    """R 因子：个股风险（``get_risk_fact``）+ 财务杠杆/商誉（``get_financial_fact``）。

    ``get_risk_fact`` 返回 ``{stock_code, news[], pledge[], repurchase[], audit[],
    risk_missing[]}``。审计意见映射到 :class:`AuditSnapshot`；产业风险
    （:class:`IndustryRiskSnapshot`）本批无数据源，保持 ``None``（占位缺口）。
    """
    data = risk_data or {}
    fin = fin_data or {}
    pledge = _first(data.get("pledge"))
    repurchase = _first(data.get("repurchase"))
    news = _first(data.get("news"))
    audit_first = _first(data.get("audit"))
    fina = _first(fin.get("fina"))
    balance = _first(fin.get("balance"))

    audit: AuditSnapshot | None = None
    if audit_first:
        audit = AuditSnapshot(
            audit_opinion=_opt_text(audit_first.get("audit_opinion")),
            audit_agency=_opt_text(audit_first.get("audit_agency")),
            audit_fees=_opt(audit_first.get("audit_fees")),
            audit_sign=_opt_text(audit_first.get("audit_sign")),
        )

    factor = RiskFactor(
        pledge_ratio=_opt(pledge.get("pledge_ratio")),
        repurchase_progress=_text(repurchase.get("repurchase_progress")),
        news_title=_text(news.get("news_title")),
        debt_to_assets=_opt(fina.get("debt_to_assets")),
        goodwill=_opt(balance.get("goodwill")),
        audit=audit,
        industry_risk=None,  # 产业风险占位（design §7.3 无数据源）
    )

    missing = _missing_fields(factor, _RISK_NUMERIC_FIELDS)
    if audit is None:
        missing.append("audit_opinion")
    else:
        missing.extend(name for name in _AUDIT_FIELDS if getattr(audit, name, None) is None)
    # 传播 risk_fact 自身的显式缺失标记（canonical 名，如 "audit_opinion"）
    missing.extend(str(name) for name in (data.get("risk_missing") or []) if name)
    return factor, missing


def build_market_factor(
    snapshot_values: dict[str, Any] | None,
    score_result: Any | None,
) -> tuple[MarketFactor, list[str]]:
    """M 因子：市场环境（``market_regime`` 子系统）。

    - 原始 canonical 指标来自 ``MarketIndicatorSnapshot.values``（hs300_pe_ttm /
      hs300_pb / hs300_close / breadth_above_ma60_pct / market_turnover / margin_balance）。
    - ``market_score`` / ``regime`` / ``position_low`` / ``position_high`` 来自
      ``MarketScoreResult``（``scores.total_score`` / ``position.regime`` /
      ``position.position_low`` / ``position.position_high``，即 RegimeSnapshot /
      MarketScore / PositionAdvice 的摘要）。
    """
    values = snapshot_values or {}
    scores = getattr(score_result, "scores", None)  # MarketScore
    position = getattr(score_result, "position", None)  # PositionAdvice
    regime_snapshot = getattr(score_result, "snapshot", None)  # RegimeSnapshot

    # regime 优先取 PositionAdvice（档位名），缺失时回退 RegimeSnapshot.regime
    regime = _text(getattr(position, "regime", None)) or _text(getattr(regime_snapshot, "regime", None))

    factor = MarketFactor(
        market_score=_opt(getattr(scores, "total_score", None)),
        regime=regime,
        position_low=_opt(getattr(position, "position_low", None)),
        position_high=_opt(getattr(position, "position_high", None)),
        hs300_pe_ttm=_opt(values.get("hs300_pe_ttm")),
        hs300_pb=_opt(values.get("hs300_pb")),
        hs300_close=_opt(values.get("hs300_close")),
        breadth_above_ma60_pct=_opt(values.get("breadth_above_ma60_pct")),
        market_turnover=_opt(values.get("market_turnover")),
        margin_balance=_opt(values.get("margin_balance")),
    )
    missing = _missing_fields(factor, _MARKET_FIELDS)
    # 传播 score engine 的显式缺失（规则层 missing_facts，canonical 名）
    for name in getattr(score_result, "missing_facts", None) or []:
        if name and name not in missing:
            missing.append(str(name))
    return factor, missing


# ─────────────────────────────────────────────────────────────────────────────
# 数据源编排（惰性导入 + 显式降级）
# ─────────────────────────────────────────────────────────────────────────────


def _safe_call(fn: Any, args: list[Any], degraded: list[str], label: str) -> Any:
    """调用数据源函数；异常时记降级原因并返回 ``None``（不中断整个快照）。"""
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001 —— 快照层必须吞掉单源失败并显式降级
        degraded.append(f"{label}: {type(e).__name__}: {e}")
        return None


def _collect_market_regime() -> tuple[dict[str, Any] | None, Any | None]:
    """采集市场状态快照并打分，返回 ``(snapshot.values, MarketScoreResult)``。"""
    from alphabee.market_regime.data import collect_snapshot
    from alphabee.market_regime.persistence import load_history
    from alphabee.market_regime.score_engine import MarketScoreEngine

    snapshot = collect_snapshot()
    history = load_history()
    result = MarketScoreEngine().score(snapshot, history=history, asof_date=snapshot.date)
    return snapshot.values, result


def get_factor_snapshot(symbol: str, *, include_market: bool = True) -> FactorSnapshot:
    """查询并封装某个标的当前的七因子快照。

    从已有 fact tools / collectors / market_regime 子系统拉取 canonical 字段，
    组装成 :class:`FactorSnapshot`。任一数据源失败或字段缺失都显式降级为
    ``None`` + 登记 ``missing_facts``（绝不静默回退 0），单个数据源失败不影响
    其他维度。

    Args:
        symbol: 股票代码，支持多种格式，如 ``"600519"`` / ``"600519.SH"`` /
            ``"300750.SZ"``。
        include_market: 是否采集 M 因子（市场环境）。M 需要 market_regime 全市场
            采集 + 打分，开销较大；置 ``False`` 时跳过 M，``snapshot.market``
            保持默认（各字段为 ``None`` 但不登记 missing，因为显式未请求）。

    Returns:
        ``FactorSnapshot``：``symbol`` / ``as_of_date`` 已填充；七个维度子模型
        按 canonical 字段映射；``missing_facts`` 登记所有缺失 canonical 字段；
        任一上游失败时 ``degraded=True`` 并记录 ``degraded_reason``。
    """
    ts_code = _normalize_ts_code(symbol)
    pure_code = _to_pure_code(ts_code)
    snapshot = FactorSnapshot(
        symbol=ts_code,
        as_of_date=datetime.date.today().isoformat(),
    )
    missing: list[str] = []
    degraded: list[str] = []

    # F —— 财务多期数据
    fin_data = _safe_call(_get_financial_fact, [ts_code], degraded, "fundamental")
    fundamental, f_missing = build_fundamental_factor(fin_data)
    snapshot.fundamental = fundamental
    missing.extend(f_missing)

    # E —— 公告类预期 + 分析师一致预期（collector 入参为 6 位代码）
    exp_data = _safe_call(_get_expectation_fact, [ts_code], degraded, "expectation")
    consensus = _safe_call(_build_consensus, [pure_code], degraded, "consensus")
    expectation, e_missing = build_expectation_factor(exp_data, consensus)
    snapshot.expectation = expectation
    missing.extend(e_missing)

    # T / V —— 行情 + 行业
    market_data = _safe_call(_get_market_fact, [ts_code], degraded, "market")
    industry_data = _safe_call(_get_industry_fact, [ts_code], degraded, "industry")
    trend, t_missing = build_trend_factor(market_data, industry_data)
    snapshot.trend = trend
    missing.extend(t_missing)
    valuation, v_missing = build_valuation_factor(market_data, industry_data)
    snapshot.valuation = valuation
    missing.extend(v_missing)

    # C —— 拥挤度（collector 入参为 6 位代码）
    crowding_out = _safe_call(_build_crowding, [pure_code], degraded, "crowding")
    crowding_factor, c_missing = build_crowding_factor(market_data, crowding_out)
    snapshot.crowding = crowding_factor
    missing.extend(c_missing)

    # R —— 个股风险 + 财务杠杆/商誉
    risk_data = _safe_call(_get_risk_fact, [ts_code], degraded, "risk")
    risk, r_missing = build_risk_factor(risk_data, fin_data)
    snapshot.risk = risk
    missing.extend(r_missing)

    # M —— 市场环境（market_regime），可按 include_market 显式跳过
    if include_market:
        regime_result = _safe_call(_collect_market_regime, [], degraded, "market_regime")
        if regime_result is None:
            regime_values, score_result = None, None
        else:
            regime_values, score_result = regime_result
        market_factor, m_missing = build_market_factor(regime_values, score_result)
        snapshot.market = market_factor
        missing.extend(m_missing)

    snapshot.missing_facts = _dedupe(missing)
    if degraded:
        snapshot.degraded = True
        snapshot.degraded_reason = "; ".join(degraded)
    return snapshot


# ─────────────────────────────────────────────────────────────────────────────
# 惰性数据源绑定（延迟导入，避免模块级 import 触发 tushare/akshare 初始化）
# ─────────────────────────────────────────────────────────────────────────────


def _get_financial_fact(symbol: str) -> dict[str, Any]:
    from alphabee.agents.facts.tools.financial_fact import get_financial_fact

    return get_financial_fact(symbol)


def _get_expectation_fact(symbol: str) -> dict[str, Any]:
    from alphabee.agents.facts.tools.expectation_fact import get_expectation_fact

    return get_expectation_fact(symbol)


def _get_market_fact(symbol: str) -> dict[str, Any]:
    from alphabee.agents.facts.tools.market_fact import get_market_fact

    return get_market_fact(symbol)


def _get_industry_fact(symbol: str) -> dict[str, Any]:
    from alphabee.agents.facts.tools.industry_fact import get_industry_fact

    return get_industry_fact(symbol)


def _get_risk_fact(symbol: str) -> dict[str, Any]:
    from alphabee.agents.facts.tools.risk_fact import get_risk_fact

    return get_risk_fact(symbol)


def _build_consensus(code: str) -> Any:
    from alphabee.collectors.consensus.eastmoney import build_consensus

    return build_consensus(code)


def _build_crowding(code: str) -> Any:
    from alphabee.collectors.crowding.collect import build_crowding

    return build_crowding(code)


if __name__ == "__main__":
    # 本模块可直接执行单个标的测试，输出 FactorSnapshot JSON
    import sys

    if len(sys.argv) < 2:
        print("Usage: python factors.py <symbol> [include_market]")
        sys.exit(1)

    symbol = sys.argv[1]
    include_market = True if len(sys.argv) < 3 else sys.argv[2].lower() in ("1", "true", "yes")
    snapshot = get_factor_snapshot(symbol, include_market=include_market)
    print(snapshot.model_dump_json(indent=2, ensure_ascii=False))
