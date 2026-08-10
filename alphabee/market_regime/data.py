"""Market-regime data orchestration: normalize external data into snapshots.

``collect_snapshot`` merges every collector into a single dated
``MarketIndicatorSnapshot`` (canonical fields only). ``backfill_history`` replays
full historical series (valuation + liquidity) into the daily CSV so percentile
windows (ERP / PE / PB 分位) have history to draw on in Phase 1.

数据源策略（``source``）：
- ``"auto"``（默认）：tushare 优先，缺失字段由 akshare 补齐（如中债收益率、社融、
  市场宽度）；tushare 提供的字段（如创业板估值、融资余额）覆盖 akshare。
- ``"tushare"`` / ``"akshare"``：强制只用单一数据源。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pandas as pd

from alphabee.collectors.market_regime import (
    breadth,
    index_valuation,
    liquidity,
    risk_preference,
    tushare,
)
from alphabee.market_regime.models import CollectorOutput, MarketIndicatorSnapshot
from alphabee.market_regime.persistence import append_snapshot

# 各主题下 akshare 采集器（breadth 仅有 akshare 数据源）
AKSHARE_COLLECTORS: dict[str, Any] = {
    "valuation": index_valuation.fetch,
    "liquidity": liquidity.fetch,
    "breadth": breadth.fetch,
    "risk_preference": risk_preference.fetch,
}

# 各主题下 tushare 采集器（无 breadth）
TUSHARE_COLLECTORS: dict[str, Any] = {
    "valuation": tushare.fetch_index_valuation,
    "liquidity": tushare.fetch_liquidity,
    "risk_preference": tushare.fetch_margin,
}

ALL_THEMES = ("valuation", "liquidity", "breadth", "risk_preference")


def collect_snapshot(
    asof_date: str | None = None,
    *,
    enabled: list[str] | None = None,
    source: str = "auto",
    ak_module: Any = None,
    ts_module: Any = None,
) -> MarketIndicatorSnapshot:
    """Collect a single dated market snapshot.

    Args:
        asof_date:  ``YYYY-MM-DD``; default is the latest available data date.
        enabled:    subset of collector themes to run; default runs all.
        source:     ``"auto"`` | ``"tushare"`` | ``"akshare"``.
        ak_module:  injectable akshare-like module for testing.
        ts_module:  injectable tushare pro_api-like module for testing.

    Returns:
        A ``MarketIndicatorSnapshot`` with merged canonical values and provenance.
        With ``source="auto"``, tushare values win over akshare for overlapping
        fields; akshare fills fields tushare cannot serve.
    """
    if source not in ("auto", "tushare", "akshare"):
        raise ValueError(f"未知数据源: {source!r}（可选 auto/tushare/akshare）")

    snapshot = MarketIndicatorSnapshot(
        date=asof_date or datetime.now(UTC).date().isoformat(),
        fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    selected = set(enabled) if enabled else set(ALL_THEMES)
    use_tushare = source in ("auto", "tushare")
    use_akshare = source in ("auto", "akshare")

    for theme in ALL_THEMES:
        if theme not in selected:
            continue
        if use_akshare and theme in AKSHARE_COLLECTORS:
            output: CollectorOutput = AKSHARE_COLLECTORS[theme](asof_date, ak_module=ak_module)
            snapshot.merge(output)
        if use_tushare and theme in TUSHARE_COLLECTORS:
            output = TUSHARE_COLLECTORS[theme](asof_date, ts_module=ts_module)
            snapshot.merge(output)
    return snapshot


def collect_and_persist(
    asof_date: str | None = None,
    path: str | None = None,
    *,
    source: str = "auto",
    ak_module: Any = None,
    ts_module: Any = None,
) -> MarketIndicatorSnapshot:
    """Collect a snapshot and append it to the daily indicator CSV (idempotent by date)."""
    snapshot = collect_snapshot(asof_date, source=source, ak_module=ak_module, ts_module=ts_module)
    append_snapshot(snapshot, path=path)
    return snapshot


def backfill_history(
    start: str,
    end: str | None = None,
    path: str | None = None,
    *,
    source: str = "auto",
    ak_module: Any = None,
    ts_module: Any = None,
) -> int:
    """Backfill historical valuation + liquidity series into the daily CSV.

    With ``source="auto"`` the tushare history (per-index PE/PB incl. 创业板,
    SHIBOR, US yield, M1/M2) is combined with akshare history (中债收益率、社融、
    全A估值分位); tushare wins on overlapping columns.

    Returns the number of rows written.
    """
    use_tushare = source in ("auto", "tushare")
    use_akshare = source in ("auto", "akshare")

    frames: list[pd.DataFrame] = []
    if use_akshare:
        frames.append(index_valuation.history(start, end, ak_module=ak_module))
        frames.append(liquidity.history(start, end, ak_module=ak_module))
    if use_tushare:
        frames.append(tushare.history(start, end, ts_module=ts_module))

    frames = [f for f in frames if f is not None and not f.empty]
    if not frames:
        return 0

    merged = frames[0]
    for frame in frames[1:]:
        # 后加入的 frame 覆盖同名列（tushare 放在最后 → 优先级最高）
        merged = frame.combine_first(merged)
    merged = merged.sort_index()

    written = 0
    for idx, row in merged.iterrows():
        values = {name: float(value) for name, value in row.items() if pd.notna(value)}
        if not values:
            continue
        snapshot = MarketIndicatorSnapshot(
            date=idx.date().isoformat(),
            values=values,
            sources={name: "backfill" for name in values},
            fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        append_snapshot(snapshot, path=path)
        written += 1
    return written
