"""Shared helpers for market-regime collectors (date handling, row selection).

Collectors are the only place allowed to touch external-source column names
(see ``alphabee/adapters/*/market_regime_mapping.yaml`` for the source↔canonical
map). Everything downstream only reads canonical field names.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
from pandas import Series


def _coerce_date(value) -> date | None:
    """Coerce common source date representations to a ``date``."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, pd.Timestamp):
        return value.date()
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt).date()
        except ValueError:
            continue
    return None


def select_latest(df: pd.DataFrame, date_col: str, asof_date: str | None = None) -> Series | None:
    """Return the last row whose ``date_col`` value is on or before ``asof_date``.

    ``asof_date`` may be ``None`` (use the latest available row) or a
    ``YYYY-MM-DD`` string. Rows whose date cannot be parsed are skipped.
    """
    if df is None or df.empty or date_col not in df.columns:
        return None

    asof: date | None = _coerce_date(asof_date) if asof_date else None
    parsed = pd.to_datetime(df[date_col], errors="coerce")

    if asof is not None:
        mask = parsed.notna() & (parsed.dt.date <= asof)
        if not mask.any():
            return None
        valid = df.loc[mask]
    else:
        valid = df.loc[parsed.notna()]

    if valid.empty:
        return None
    return valid.iloc[-1]


def month_key(value) -> str | None:
    """Normalize a ``月份`` value to ``YYYYMM`` (supports ``202604`` / ``2008年01月份``)."""
    if value is None:
        return None
    text = str(value).strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) == 6:
        return digits
    if len(digits) >= 6:
        return digits[:6]
    return None


def walk_back_dates(start: str | None = None, max_days: int = 8) -> list[str]:
    """Generate ``YYYYMMDD`` candidates walking backwards from ``start`` (default today)."""
    anchor = _coerce_date(start) or date.today()
    return [(anchor - timedelta(days=i)).strftime("%Y%m%d") for i in range(max_days)]
