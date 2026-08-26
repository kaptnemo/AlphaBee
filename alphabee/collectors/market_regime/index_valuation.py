"""Collector: index & all-market valuation (PE / PB / earnings yield / trend MA).

Sources (akshare):
- ``stock_index_pe_lg`` / ``stock_index_pb_lg`` — per-index PE/PB (沪深300/中证500/中证1000)
- ``stock_a_ttm_lyr`` / ``stock_a_all_pb`` — all-market PE/PB + 10y percentiles
- ``stock_zh_index_daily`` (sina) — CSI300 close for moving averages
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from alphabee.collectors.market_regime._utils import _coerce_date, select_latest
from alphabee.market_regime.models import CollectorOutput

SOURCE_PE = "akshare:stock_index_pe_lg"
SOURCE_PB = "akshare:stock_index_pb_lg"
SOURCE_ALL_MARKET = "akshare:stock_a_ttm_lyr/stock_a_all_pb"
SOURCE_INDEX_DAILY = "akshare:stock_zh_index_daily"

# (akshare symbol, canonical field prefix)
INDEX_SYMBOLS: list[tuple[str, str]] = [
    ("沪深300", "hs300"),
    ("中证500", "cs500"),
    ("中证1000", "cs1000"),
]

HS300_SINA_SYMBOL = "sh000300"


def _get_ak(ak_module: Any = None) -> Any:
    if ak_module is not None:
        return ak_module
    import akshare as ak  # noqa: PLC0415

    return ak


def ep_from_pe(pe: float | None) -> float | None:
    """Earnings yield (%) = 100 / PE-TTM; ``None`` when PE is not a positive number."""
    if pe is None or not pd.notna(pe) or pe <= 0:
        return None
    return round(100.0 / float(pe), 4)


def extract_index_valuation(
    df: pd.DataFrame,
    date_col: str,
    pe_col: str,
    pb_col: str,
    asof_date: str | None,
) -> tuple[float | None, float | None]:
    """Return ``(pe, pb)`` from the latest row on/before ``asof_date``."""
    row = select_latest(df, date_col, asof_date)
    if row is None:
        return None, None
    pe = float(row[pe_col]) if pe_col in df.columns and pd.notna(row[pe_col]) else None
    pb = float(row[pb_col]) if pb_col in df.columns and pd.notna(row[pb_col]) else None
    return pe, pb


def compute_moving_average(close_series: pd.Series, window: int) -> float | None:
    """Rolling simple moving average of the trailing ``window`` close values."""
    if len(close_series) < window:
        return None
    return round(float(close_series.tail(window).mean()), 2)


def fetch_index_valuation(asof_date: str | None = None, *, ak_module: Any = None) -> CollectorOutput:
    """Per-index PE/PB for 沪深300/中证500/中证1000 plus derived earnings yields."""
    ak = _get_ak(ak_module)
    values: dict[str, float] = {}
    warnings: list[str] = []

    for symbol, prefix in INDEX_SYMBOLS:
        try:
            pe_df = ak.stock_index_pe_lg(symbol=symbol)
            pe, _ = extract_index_valuation(pe_df, "日期", "滚动市盈率", "滚动市盈率", asof_date)
            values[f"{prefix}_pe_ttm"] = pe  # type: ignore[assignment]
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{symbol} PE 获取失败: {exc}")

        try:
            pb_df = ak.stock_index_pb_lg(symbol=symbol)
            _, pb = extract_index_valuation(pb_df, "日期", "市净率", "市净率", asof_date)
            values[f"{prefix}_pb"] = pb  # type: ignore[assignment]
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{symbol} PB 获取失败: {exc}")

    # 盈利收益率（%）= 100 / PE-TTM，供 ERP 计算使用
    for _, prefix in INDEX_SYMBOLS:
        pe = values.get(f"{prefix}_pe_ttm")
        ep = ep_from_pe(pe)
        if ep is not None:
            values[f"{prefix}_ep_ttm"] = ep

    return CollectorOutput(values=values, source=f"{SOURCE_PE}/{SOURCE_PB}", warnings=warnings)


def fetch_all_market_valuation(asof_date: str | None = None, *, ak_module: Any = None) -> CollectorOutput:
    """All-market PE/PB and 10-year historical percentiles (source-provided)."""
    ak = _get_ak(ak_module)
    values: dict[str, float] = {}
    warnings: list[str] = []

    try:
        ttm = ak.stock_a_ttm_lyr()
        row = select_latest(ttm, "date", asof_date)
        if row is not None:
            values["all_market_pe_ttm"] = float(row["middlePETTM"])
            if pd.notna(row.get("quantileInRecent10YearsMiddlePeTtm")):
                values["all_market_pe_10y_percentile"] = float(row["quantileInRecent10YearsMiddlePeTtm"])
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"全A PE 获取失败: {exc}")

    try:
        all_pb = ak.stock_a_all_pb()
        row = select_latest(all_pb, "date", asof_date)
        if row is not None:
            values["all_market_pb"] = float(row["middlePB"])
            if pd.notna(row.get("quantileInRecent10YearsMiddlePB")):
                values["all_market_pb_10y_percentile"] = float(row["quantileInRecent10YearsMiddlePB"])
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"全A PB 获取失败: {exc}")

    return CollectorOutput(values=values, source=SOURCE_ALL_MARKET, warnings=warnings)


def fetch_hs300_trend(asof_date: str | None = None, *, ak_module: Any = None) -> CollectorOutput:
    """CSI300 close plus 20/60/250-day moving averages (trend inputs)."""
    ak = _get_ak(ak_module)
    values: dict[str, float] = {}
    warnings: list[str] = []

    try:
        daily = ak.stock_zh_index_daily(symbol=HS300_SINA_SYMBOL)
        if daily is not None and not daily.empty and "close" in daily.columns:
            parsed = pd.to_datetime(daily["date"], errors="coerce")
            asof = _coerce_date(asof_date) if asof_date else None
            mask = parsed.notna() & (parsed.dt.date <= asof) if asof is not None else parsed.notna()
            if mask.any():
                idx = daily.index[mask][-1]
                close_series = daily.loc[:idx, "close"]
                values["hs300_close"] = float(daily.loc[idx, "close"])
                for window in (20, 60, 250):
                    ma = compute_moving_average(close_series, window)
                    if ma is not None:
                        values[f"hs300_ma{window}"] = ma
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"沪深300 日线/均线获取失败: {exc}")

    return CollectorOutput(values=values, source=SOURCE_INDEX_DAILY, warnings=warnings)


def fetch(asof_date: str | None = None, *, ak_module: Any = None) -> CollectorOutput:
    """Merge all valuation/trent outputs into one collector result."""
    merged = CollectorOutput(source=f"{SOURCE_PE}/{SOURCE_PB}/{SOURCE_ALL_MARKET}/{SOURCE_INDEX_DAILY}")
    for collector in (fetch_index_valuation, fetch_all_market_valuation, fetch_hs300_trend):
        part = collector(asof_date, ak_module=ak_module)
        merged.values.update(part.values)
        merged.warnings.extend(part.warnings)
    return merged


def history(
    start: str | None = None,
    end: str | None = None,
    *,
    ak_module: Any = None,
) -> pd.DataFrame:
    """Full historical valuation series as a date-indexed DataFrame of canonical fields.

    Fetching each source exactly once and joining on the trading-day index keeps
    the backfill cheap (a fixed number of API calls regardless of window size).
    Columns contain canonical names; per-index PE/PB percentiles are *not* computed
    here (that belongs to the Phase 1 score engine, which uses this history).
    """
    ak = _get_ak(ak_module)
    frames: dict[str, pd.Series] = {}

    for symbol, prefix in INDEX_SYMBOLS:
        try:
            pe_df = ak.stock_index_pe_lg(symbol=symbol)
            pe = pe_df.set_index(pd.to_datetime(pe_df["日期"]))["滚动市盈率"].astype(float)
            frames[f"{prefix}_pe_ttm"] = pe
        except Exception:  # noqa: BLE001
            pass
        try:
            pb_df = ak.stock_index_pb_lg(symbol=symbol)
            pb = pb_df.set_index(pd.to_datetime(pb_df["日期"]))["市净率"].astype(float)
            frames[f"{prefix}_pb"] = pb
        except Exception:  # noqa: BLE001
            pass

    try:
        ttm = ak.stock_a_ttm_lyr()
        ttm = ttm.set_index(pd.to_datetime(ttm["date"]))
        frames["all_market_pe_ttm"] = ttm["middlePETTM"].astype(float)
        if "quantileInRecent10YearsMiddlePeTtm" in ttm.columns:
            frames["all_market_pe_10y_percentile"] = ttm["quantileInRecent10YearsMiddlePeTtm"].astype(float)
    except Exception:  # noqa: BLE001
        pass

    try:
        all_pb = ak.stock_a_all_pb()
        all_pb = all_pb.set_index(pd.to_datetime(all_pb["date"]))
        frames["all_market_pb"] = all_pb["middlePB"].astype(float)
        if "quantileInRecent10YearsMiddlePB" in all_pb.columns:
            frames["all_market_pb_10y_percentile"] = all_pb["quantileInRecent10YearsMiddlePB"].astype(float)
    except Exception:  # noqa: BLE001
        pass

    try:
        daily = ak.stock_zh_index_daily(symbol=HS300_SINA_SYMBOL)
        daily = daily.set_index(pd.to_datetime(daily["date"])).sort_index()
        close = daily["close"].astype(float)
        frames["hs300_close"] = close
        for window in (20, 60, 250):
            frames[f"hs300_ma{window}"] = close.rolling(window).mean()
    except Exception:  # noqa: BLE001
        pass

    if not frames:
        return pd.DataFrame()

    df = pd.DataFrame(frames).sort_index()
    # 派生盈利收益率（%）= 100 / PE-TTM
    for _, prefix in INDEX_SYMBOLS:
        pe = df.get(f"{prefix}_pe_ttm")
        if pe is not None:
            df[f"{prefix}_ep_ttm"] = (100.0 / pe).where(pe > 0)

    if start:
        df = df[df.index >= pd.to_datetime(start)]
    if end:
        df = df[df.index <= pd.to_datetime(end)]
    return df.round(4)
