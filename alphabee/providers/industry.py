"""Industry data provider — unified interface with source fallback chain.

Priority order for each data domain:
  industry daily (行情+估值): sw_daily → index_daily + akshare snapshot
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any


@dataclass
class IndustryDailyResult:
    daily: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    source: str = ""  # "sw_daily" | "index_daily+akshare" | "none"


def get_industry_daily(
    sw_code: str,
    industry: str,
    lookback_days: int = 90,
) -> IndustryDailyResult:
    """获取申万行业指数的日行情数据，含 PE/PB。

    按优先级尝试：
    1. Tushare ``sw_daily`` — 完整数据（close / pct_change / PE / PB）
    2. Tushare ``index_daily`` + AkShare 快照 — 趋势 + 估值分别获取

    Args:
        sw_code: 申万行业指数代码，如 ``801010.SI``。
        industry: 行业名称（如 ``白酒``），用于 AkShare 名称匹配。
        lookback_days: 回溯天数。
    """
    today = datetime.date.today().strftime("%Y%m%d")
    start = (datetime.date.today() - datetime.timedelta(days=lookback_days)).strftime("%Y%m%d")

    # 1. Tushare sw_daily
    result = _try_sw_daily(sw_code, start, today)
    if result is not None:
        return result

    # 2. Tushare index_daily + AkShare PE/PB snapshot
    result = _try_index_daily_plus_akshare(sw_code, industry, start, today)
    if result is not None:
        return result

    return IndustryDailyResult(source="none", error="All sources exhausted")


# ── primary: sw_daily ──────────────────────────────────────────────────


def _try_sw_daily(sw_code: str, start: str, end: str) -> IndustryDailyResult | None:
    try:
        from alphabee.collectors.tushare.helper import TuShareHelper

        with TuShareHelper() as helper:
            df = helper.sw_daily(
                ts_code=sw_code,
                start_date=start,
                end_date=end,
                fields="ts_code,trade_date,close,pct_change,pe,pb,float_mv",
            ).data

        if df.empty:
            return None

        rows = _to_rows(df, extra=True)
        return IndustryDailyResult(daily=rows, source="sw_daily")

    except Exception:
        return None


# ── fallback: index_daily + akshare ────────────────────────────────────


def _try_index_daily_plus_akshare(sw_code: str, industry: str, start: str, end: str) -> IndustryDailyResult | None:
    """index_daily gives close + pct_chg; akshare snapshot fills PE/PB."""

    # Step A: Tushare index_daily
    try:
        from alphabee.collectors.tushare.helper import TuShareHelper

        with TuShareHelper() as helper:
            df = helper.index_daily(
                ts_code=sw_code,
                start_date=start,
                end_date=end,
                fields="ts_code,trade_date,close,pct_chg",
            ).data

        if df.empty:
            return None

        rows = _to_rows(df, extra=False)

    except Exception:
        return None

    # Step B: AkShare PE/PB snapshot
    pe_val, pb_val = _get_akshare_pe_pb(industry)

    if pe_val is not None or pb_val is not None:
        for row in rows:
            if pe_val is not None:
                row["industry_pe_ttm"] = pe_val
            if pb_val is not None:
                row["industry_pb"] = pb_val

    return IndustryDailyResult(daily=rows, source="index_daily+akshare")


def _get_akshare_pe_pb(industry: str) -> tuple[float | None, float | None]:
    """从 AkShare 行业板块快照获取 PE/PB（东方财富，同花顺无等效接口）。"""
    if not industry:
        return None, None

    try:
        from alphabee.collectors.akshare.helper import AkShareHelper

        with AkShareHelper() as helper:
            result = helper.stock_board_industry_name_em()
            df = result.to_dataframe()

        if df.empty:
            return None, None

        # 字段治理（Phase 2）：adapter 重命名后只剩 canonical 列名，
        # 此处不回退外部列名（"板块名称"/"市盈率-动态" 只存在于 adapter mapping）。
        if "industry_name" not in df.columns:
            return None, None

        # 匹配：精确优先，前缀兜底（避免 industry[:2] contains 误中"半导体设备"等相近板块）
        names = df["industry_name"].astype(str)
        exact = df[names == industry]
        if exact.empty:
            pref = df[names.str.startswith(industry, na=False)]
            if pref.empty:
                return None, None
            row = pref.iloc[0]
        else:
            row = exact.iloc[0]
        pe = _safe_float(row.get("industry_pe_ttm"))
        pb = _safe_float(row.get("industry_pb"))
        return pe, pb

    except Exception:
        return None, None


# ── helpers ────────────────────────────────────────────────────────────


def _to_rows(df, extra: bool) -> list[dict[str, Any]]:
    """Convert DataFrame to canonical row dicts.

    After TuShare adapter renaming, columns are already canonical names.
    """
    rows: list[dict[str, Any]] = []
    for _, row in df.head(10).iterrows():
        item: dict[str, Any] = {
            "trade_date": _safe_str(row, "trade_date"),
            "industry_close": _safe_float(row, "industry_close"),
            "industry_change_pct": _safe_float(row, "industry_change_pct"),
        }
        if extra:
            item["industry_pe_ttm"] = _safe_float(row, "industry_pe_ttm")
            item["industry_pb"] = _safe_float(row, "industry_pb")
        rows.append(item)
    return rows


def _safe_float(row_or_val, col: str | None = None) -> float:
    import math

    val = row_or_val
    try:
        if col is not None:
            val = row_or_val.get(col, row_or_val)
        f = float(val)
        return f if not math.isnan(f) else 0.0
    except (ValueError, TypeError, AttributeError):
        return 0.0


def _safe_str(row_or_val, col: str | None = None) -> str:
    try:
        if col is not None and hasattr(row_or_val, "get"):
            val = row_or_val.get(col, row_or_val)
        else:
            val = row_or_val
        if val is None or (isinstance(val, float) and val != val):
            return ""
        return str(val)
    except (ValueError, TypeError):
        return ""
