"""Crowding 采集器编排 — 汇总股东户数/人气榜/覆盖热度/交易热度为 canonical 字段。"""

from __future__ import annotations

from datetime import date

from alphabee.collectors.crowding.coverage import fetch_analyst_coverage_rank
from alphabee.collectors.crowding.engine import CROWDING_P0_FIELDS, CrowdingOutput, normalize_code
from alphabee.collectors.crowding.holders import fetch_holder_changes
from alphabee.collectors.crowding.hot_rank import fetch_hot_rank
from alphabee.collectors.crowding.turnover import (
    fetch_amount_pct_of_market,
    fetch_turnover_rate_percentile,
)


def build_crowding(
    code: str,
    *,
    sample_codes: list[str] | None = None,
    holder_date: str | None = None,
    market_turnover_cny: float | None = None,
) -> CrowdingOutput:
    """采集个股 P0 拥挤度字段（股东户数/人气榜/覆盖热度/交易热度）。

    Args:
        code: 6 位股票代码（支持 ``300750.SZ``）。
        sample_codes: 覆盖热度排名参照样本（同行业/对标组代码列表）；缺省仅自身。
        holder_date: 股东户数季度末披露日（``YYYYMMDD``）；缺省自动回退最近季度末。
        market_turnover_cny: 全市场成交额（元），缺省时复用 market_regime 采集器。

    Returns:
        :class:`CrowdingOutput`：``values`` 承载 6 个 P0 canonical 字段，
        ``missing`` 记录显式 ``crowding_missing: <field>`` issue（绝不静默回退 0）。
    """
    sec_code = normalize_code(code)
    out = CrowdingOutput(as_of_date=date.today().isoformat(), source="akshare/eastmoney/tushare")

    holder_change, per_capita_change = fetch_holder_changes(sec_code, date_=holder_date)
    hot_rank = fetch_hot_rank(sec_code)
    coverage_rank = fetch_analyst_coverage_rank(sec_code, sample_codes)
    turnover_pct = fetch_turnover_rate_percentile(sec_code)
    amount_pct = fetch_amount_pct_of_market(sec_code, market_turnover_cny=market_turnover_cny)

    out.values = {
        "holder_count_change": holder_change,
        "per_capita_holding_change": per_capita_change,
        "hot_rank": hot_rank,
        "analyst_coverage_rank": coverage_rank,
        "turnover_rate_percentile": turnover_pct,
        "amount_pct_of_market": amount_pct,
    }
    for field in CROWDING_P0_FIELDS:
        if out.values[field] is None:
            out.missing.append(f"crowding_missing: {field}")
    if not sample_codes:
        out.warnings.append("analyst_coverage_rank computed over single-stock sample (rank=1 means covered)")
    return out
