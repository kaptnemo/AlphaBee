"""Collector: risk-preference indicators (market turnover, margin balance).

Sources (akshare, per-date APIs — walk back to the latest trading day):
- ``stock_sse_deal_daily`` — 上交所当日成交金额（亿元）
- ``stock_szse_summary`` — 深交所股票成交金额（元）
- ``stock_margin_sse`` / ``stock_margin_szse`` — 沪深融资余额

ETF 资金流（etf_net_inflow）无稳定免费数据源，登记为 schema field_gaps。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from alphabee.collectors.market_regime._utils import walk_back_dates
from alphabee.market_regime.models import CollectorOutput

SOURCE_TURNOVER = "akshare:stock_sse_deal_daily/stock_szse_summary"
SOURCE_MARGIN = "akshare:stock_margin_sse/stock_margin_szse"


def _get_ak(ak_module: Any = None):
    if ak_module is not None:
        return ak_module
    import akshare as ak  # noqa: PLC0415

    return ak


# ── 解析函数（纯函数，便于单测） ─────────────────────────────────────────


def parse_sse_turnover(df: pd.DataFrame) -> float | None:
    """上交所股票成交金额（亿元）。"""
    if df is None or df.empty or "单日情况" not in df.columns:
        return None
    row = df[df["单日情况"] == "成交金额"]
    if row.empty or pd.isna(row.iloc[0].get("股票")):
        return None
    return float(row.iloc[0]["股票"])


def parse_szse_turnover(df: pd.DataFrame) -> float | None:
    """深交所股票成交金额（元）。"""
    if df is None or df.empty or "证券类别" not in df.columns:
        return None
    row = df[df["证券类别"] == "股票"]
    if row.empty or pd.isna(row.iloc[0].get("成交金额")):
        return None
    return float(row.iloc[0]["成交金额"])


def parse_sse_margin(df: pd.DataFrame) -> float | None:
    """上交所融资余额（亿元）。源单位为元，除以 1e8。"""
    if df is None or df.empty or "融资余额" not in df.columns:
        return None
    value = df.iloc[-1]["融资余额"]
    if pd.isna(value):
        return None
    return round(float(value) / 1e8, 2)


def parse_szse_margin(df: pd.DataFrame) -> float | None:
    """深交所融资余额（亿元）。"""
    if df is None or df.empty or "融资余额" not in df.columns:
        return None
    value = df.iloc[-1]["融资余额"]
    if pd.isna(value):
        return None
    return round(float(value), 2)


def market_turnover_from(sse: float | None, szse: float | None) -> float | None:
    """沪深两市成交额（亿元）= 上交所亿元 + 深交所元 / 1e8。"""
    if sse is None or szse is None:
        return None
    return round(sse + szse / 1e8, 2)


def margin_balance_from(sse: float | None, szse: float | None) -> float | None:
    """沪深两市融资余额（亿元）= 上交所亿元 + 深交所亿元。"""
    if sse is None or szse is None:
        return None
    return round(sse + szse, 2)


# ── 按日拉取（带最近交易日回退） ─────────────────────────────────────────


def _fetch_with_walkback(per_date_fn, asof_date: str | None, max_days: int = 8):
    """Try ``per_date_fn(YYYYMMDD)`` on successive dates until it returns a value."""
    for date_str in walk_back_dates(asof_date, max_days=max_days):
        result = per_date_fn(date_str)
        if result is not None:
            return result
    return None


def fetch_market_turnover(asof_date: str | None = None, *, ak_module: Any = None) -> CollectorOutput:
    """两市股票成交额（亿元）。"""
    ak = _get_ak(ak_module)
    values: dict[str, float] = {}
    warnings: list[str] = []

    try:

        def _per_date(date_str: str) -> float | None:
            sse = parse_sse_turnover(ak.stock_sse_deal_daily(date=date_str))
            szse = parse_szse_turnover(ak.stock_szse_summary(date=date_str))
            return market_turnover_from(sse, szse)

        turnover = _fetch_with_walkback(_per_date, asof_date)
        if turnover is not None:
            values["market_turnover"] = turnover
        else:
            warnings.append("连续多个日期无法取得两市成交额")
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"两市成交额获取失败: {exc}")

    return CollectorOutput(values=values, source=SOURCE_TURNOVER, warnings=warnings)


def fetch_margin(asof_date: str | None = None, *, ak_module: Any = None) -> CollectorOutput:
    """沪深两市融资余额（亿元）。"""
    ak = _get_ak(ak_module)
    values: dict[str, float] = {}
    warnings: list[str] = []

    try:

        def _per_date(date_str: str) -> float | None:
            sse = parse_sse_margin(ak.stock_margin_sse(start_date=date_str, end_date=date_str))
            szse = parse_szse_margin(ak.stock_margin_szse(date=date_str))
            return margin_balance_from(sse, szse)

        balance = _fetch_with_walkback(_per_date, asof_date)
        if balance is not None:
            values["margin_balance"] = balance
        else:
            warnings.append("连续多个日期无法取得融资余额")
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"融资余额获取失败: {exc}")

    return CollectorOutput(values=values, source=SOURCE_MARGIN, warnings=warnings)


def fetch(asof_date: str | None = None, *, ak_module: Any = None) -> CollectorOutput:
    """Merge turnover and margin into one collector result."""
    merged = CollectorOutput(source=f"{SOURCE_TURNOVER}/{SOURCE_MARGIN}")
    for collector in (fetch_market_turnover, fetch_margin):
        part = collector(asof_date, ak_module=ak_module)
        merged.values.update(part.values)
        merged.warnings.extend(part.warnings)
    return merged
