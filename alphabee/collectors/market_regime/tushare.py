"""Collector: tushare-based market-regime data (valuation / liquidity / margin).

Covers fields that akshare cannot fully provide (notably per-index PE/PB for
创业板), plus authoritative SHIBOR / US 10y yield / M1-M2 / margin. Fields that
tushare cannot serve under the current token (``cn_10y_yield`` via ``yield_cnbd``,
``social_financing_increment`` via ``cn_sf``) stay on akshare — ``data.py`` merges
both sources with tushare priority (``source="auto"``).
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from alphabee.collectors.market_regime._utils import month_key, select_latest, walk_back_dates
from alphabee.market_regime.models import CollectorOutput

SOURCE = "tushare:index_dailybasic/shibor/us_tycr/cn_m/margin"

# (ts_code, canonical field prefix)
INDEX_CODES: list[tuple[str, str]] = [
    ("000300.SH", "hs300"),
    ("000905.SH", "cs500"),
    ("000852.SH", "cs1000"),
    ("399006.SZ", "cyb"),
]


def _get_ts(ts_module: Any = None):
    """Return a tushare Pro API client; ``ts_module`` allows test injection."""
    if ts_module is not None:
        return ts_module
    import tushare as ts  # noqa: PLC0415

    token = os.getenv("TUSHARE_TOKEN")
    if not token:
        try:
            from alphabee.config.loader import ConfigLoader

            token = ConfigLoader().load_config().get("tushare", {}).get("api_key", "")
        except Exception:  # noqa: BLE001
            token = ""
    if token:
        ts.set_token(token)
    return ts.pro_api()


def _compact(date_value: str) -> str:
    """``YYYY-MM-DD`` → ``YYYYMMDD`` (digits only)."""
    return "".join(ch for ch in str(date_value) if ch.isdigit())[:8]


def _range(asof_date: str | None, lookback_days: int = 40) -> tuple[str, str]:
    """Start/end ``YYYYMMDD`` for a recent window ending at ``asof_date``."""
    anchor = datetime.fromisoformat(asof_date).date() if asof_date else date.today()
    start = anchor - timedelta(days=lookback_days)
    return start.strftime("%Y%m%d"), anchor.strftime("%Y%m%d")


def _ep_from_pe(pe: float | None) -> float | None:
    if pe is None or pd.isna(pe) or pe <= 0:
        return None
    return round(100.0 / float(pe), 4)


def fetch_index_valuation(asof_date: str | None = None, *, ts_module: Any = None) -> CollectorOutput:
    """Per-index PE-TTM / PB (incl. 创业板) and derived earnings yields."""
    pro = _get_ts(ts_module)
    values: dict[str, float] = {}
    warnings: list[str] = []
    start, end = _range(asof_date)

    for ts_code, prefix in INDEX_CODES:
        try:
            df = pro.index_dailybasic(
                ts_code=ts_code,
                start_date=start,
                end_date=end,
                fields="ts_code,trade_date,pe_ttm,pb",
            )
            row = select_latest(df, "trade_date", asof_date)
            if row is None:
                warnings.append(f"{ts_code} 无估值数据（窗口 {start}~{end}）")
                continue
            pe = float(row["pe_ttm"]) if pd.notna(row.get("pe_ttm")) else None
            pb = float(row["pb"]) if pd.notna(row.get("pb")) else None
            if pe is not None:
                values[f"{prefix}_pe_ttm"] = pe
                ep = _ep_from_pe(pe)
                if ep is not None:
                    values[f"{prefix}_ep_ttm"] = ep
            if pb is not None:
                values[f"{prefix}_pb"] = pb
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{ts_code} index_dailybasic 获取失败: {exc}")

    return CollectorOutput(values=values, source=SOURCE, warnings=warnings)


def _daily_rate_fetch(
    pro,
    api_name: str,
    value_col: str,
    asof_date: str | None,
    field: str,
) -> float | None:
    start, end = _range(asof_date)
    df = getattr(pro, api_name)(start_date=start, end_date=end)
    if df is None or df.empty or value_col not in df.columns:
        return None
    row = select_latest(df, "date", asof_date)
    if row is None or pd.isna(row.get(value_col)):
        return None
    return float(row[value_col])


def fetch_liquidity(asof_date: str | None = None, *, ts_module: Any = None) -> CollectorOutput:
    """SHIBOR 3M, US 10y yield, M1/M2 YoY and the M1-M2 gap."""
    pro = _get_ts(ts_module)
    values: dict[str, float] = {}
    warnings: list[str] = []

    try:
        shibor_3m = _daily_rate_fetch(pro, "shibor", "3m", asof_date, "shibor")
        if shibor_3m is not None:
            values["shibor_3m"] = shibor_3m
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"shibor 获取失败: {exc}")

    try:
        us_10y = _daily_rate_fetch(pro, "us_tycr", "y10", asof_date, "us_tycr")
        if us_10y is not None:
            values["us_10y_yield"] = us_10y
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"us_tycr 获取失败: {exc}")

    asof_month = "".join(ch for ch in (asof_date or "") if ch.isdigit())[:6]
    if not asof_month:
        now = date.today()
        asof_month = f"{now.year * 100 + now.month:06d}"
    month_start = f"{int(asof_month) - 35:06d}" if int(asof_month) > 35 else "200101"
    try:
        df = pro.cn_m(start_m=month_start, end_m=asof_month)
        if df is not None and not df.empty:
            df = df.copy()
            df["_month"] = df["month"].map(month_key)
            candidates = df[df["_month"].notna()].sort_values("_month")
            candidates = candidates[candidates["_month"] <= asof_month]
            if not candidates.empty:
                row = candidates.iloc[-1]
                if pd.notna(row.get("m1_yoy")):
                    values["m1_yoy"] = float(row["m1_yoy"])
                if pd.notna(row.get("m2_yoy")):
                    values["m2_yoy"] = float(row["m2_yoy"])
                if "m1_yoy" in values and "m2_yoy" in values:
                    values["m1_m2_gap"] = round(values["m1_yoy"] - values["m2_yoy"], 2)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"cn_m 获取失败: {exc}")

    return CollectorOutput(values=values, source=SOURCE, warnings=warnings)


def fetch_margin(asof_date: str | None = None, *, ts_module: Any = None) -> CollectorOutput:
    """沪深两市融资余额合计（亿元）。"""
    pro = _get_ts(ts_module)
    values: dict[str, float] = {}
    warnings: list[str] = []

    try:
        for date_str in walk_back_dates(asof_date):
            df = pro.margin(trade_date=date_str, fields="trade_date,exchange_id,rzye")
            if df is None or df.empty:
                continue
            total = pd.to_numeric(df["rzye"], errors="coerce").sum()
            if pd.notna(total) and total > 0:
                values["margin_balance"] = round(float(total) / 1e8, 2)
                break
        if "margin_balance" not in values:
            warnings.append("连续多个日期无法取得融资余额")
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"margin 获取失败: {exc}")

    return CollectorOutput(values=values, source=SOURCE, warnings=warnings)


def fetch(asof_date: str | None = None, *, ts_module: Any = None) -> CollectorOutput:
    """Merge all tushare collector outputs."""
    merged = CollectorOutput(source=SOURCE)
    for collector in (fetch_index_valuation, fetch_liquidity, fetch_margin):
        part = collector(asof_date, ts_module=ts_module)
        merged.values.update(part.values)
        merged.warnings.extend(part.warnings)
    return merged


def history(
    start: str | None = None,
    end: str | None = None,
    *,
    ts_module: Any = None,
) -> pd.DataFrame:
    """Historical tushare valuation + liquidity series (date-indexed canonical frame).

    Used by ``data.backfill_history`` alongside akshare history; tushare columns
    take priority for overlapping fields.
    """
    pro = _get_ts(ts_module)
    start_c = _compact(start) if start else "20160101"
    end_c = _compact(end) if end else ""
    frames: dict[str, pd.Series] = {}

    for ts_code, prefix in INDEX_CODES:
        try:
            df = pro.index_dailybasic(
                ts_code=ts_code,
                start_date=start_c,
                end_date=end_c,
                fields="ts_code,trade_date,pe_ttm,pb",
            )
            if df is not None and not df.empty:
                s = df.set_index(pd.to_datetime(df["trade_date"])).sort_index()
                frames[f"{prefix}_pe_ttm"] = pd.to_numeric(s["pe_ttm"], errors="coerce")
                frames[f"{prefix}_pb"] = pd.to_numeric(s["pb"], errors="coerce")
        except Exception:  # noqa: BLE001
            pass

    try:
        df = pro.shibor(start_date=start_c, end_date=end_c)
        if df is not None and not df.empty:
            s = df.set_index(pd.to_datetime(df["date"])).sort_index()
            frames["shibor_3m"] = pd.to_numeric(s["3m"], errors="coerce")
    except Exception:  # noqa: BLE001
        pass

    try:
        df = pro.us_tycr(start_date=start_c, end_date=end_c)
        if df is not None and not df.empty:
            s = df.set_index(pd.to_datetime(df["date"])).sort_index()
            frames["us_10y_yield"] = pd.to_numeric(s["y10"], errors="coerce")
    except Exception:  # noqa: BLE001
        pass

    daily = pd.DataFrame(frames).sort_index()

    # 月频 M1/M2 前向填充到日频网格
    try:
        month_start = f"{int(start_c[:6]) - 1:06d}"
        df = pro.cn_m(start_m=month_start, end_m=end_c[:6] or "")
        if df is not None and not df.empty:
            m = df.copy()
            m["_month"] = m["month"].map(month_key)
            m = m[m["_month"].notna()].sort_values("_month")
            month_index = pd.to_datetime(m["_month"], format="%Y%m")
            m1 = pd.Series(pd.to_numeric(m["m1_yoy"], errors="coerce").values, index=month_index)
            m2 = pd.Series(pd.to_numeric(m["m2_yoy"], errors="coerce").values, index=month_index)
            if daily.empty:
                daily = pd.DataFrame(index=m1.index)
            for name, series in (("m1_yoy", m1), ("m2_yoy", m2)):
                daily[name] = series.reindex(daily.index, method="ffill")
            daily["m1_m2_gap"] = (daily["m1_yoy"] - daily["m2_yoy"]).round(2)
    except Exception:  # noqa: BLE001
        pass

    for _, prefix in INDEX_CODES:
        pe = daily.get(f"{prefix}_pe_ttm")
        if pe is not None:
            daily[f"{prefix}_ep_ttm"] = (100.0 / pe).where(pe > 0)

    if start:
        daily = daily[daily.index >= pd.to_datetime(start)]
    if end:
        daily = daily[daily.index <= pd.to_datetime(end)]
    return daily.round(4)
