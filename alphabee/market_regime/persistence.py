"""Persistence for market-regime snapshots (CSV-based daily indicator store).

Layout (mirrors the design doc's ``market_indicator_daily`` table):

    data/market_regime/market_indicator_daily.csv
    columns: date (YYYY-MM-DD), fetched_at, <canonical fields...>

``append_snapshot`` upserts by ``date`` (newest snapshot wins), so both the
daily radar and ``backfill_history`` can run repeatedly without duplicating rows.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from alphabee.market_regime.models import MarketIndicatorSnapshot

DEFAULT_DATA_DIR = Path("data") / "market_regime"
DEFAULT_CSV = DEFAULT_DATA_DIR / "market_indicator_daily.csv"


def default_csv_path() -> Path:
    """Path of the daily indicator CSV (created on demand)."""
    return DEFAULT_CSV


def load_history(path: str | Path | None = None) -> pd.DataFrame:
    """Load the daily indicator CSV as a DataFrame (empty frame if missing)."""
    csv_path = Path(path) if path else default_csv_path()
    if not csv_path.exists():
        return pd.DataFrame(columns=["date"])
    df = pd.read_csv(csv_path)
    df["date"] = df["date"].astype(str)
    return df


def latest_date(path: str | Path | None = None) -> str | None:
    """Return the latest stored snapshot date (``YYYY-MM-DD``) or ``None``."""
    df = load_history(path)
    if df.empty or "date" not in df.columns:
        return None
    return str(df["date"].max())


def _snapshot_row(snapshot: MarketIndicatorSnapshot) -> dict:
    row = {"date": snapshot.date, "fetched_at": snapshot.fetched_at}
    for name, value in snapshot.values.items():
        row[name] = value
    return row


def append_snapshot(snapshot: MarketIndicatorSnapshot, path: str | Path | None = None) -> Path:
    """Upsert a snapshot into the daily CSV, replacing any existing row for its date."""
    csv_path = Path(path) if path else default_csv_path()
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    existing = load_history(csv_path)
    new_row = _snapshot_row(snapshot)

    if existing.empty:
        frame = pd.DataFrame([new_row])
    else:
        date_mask = existing["date"] != snapshot.date
        frame = pd.concat([existing.loc[date_mask], pd.DataFrame([new_row])], ignore_index=True)

    # 固定列序：date, fetched_at, 其余 canonical 字段按字母序
    base_cols = ["date", "fetched_at"]
    value_cols = [col for col in sorted(frame.columns) if col not in base_cols]
    frame = frame[base_cols + value_cols].sort_values("date").reset_index(drop=True)

    frame.to_csv(csv_path, index=False)
    return csv_path


def drop_date(date_str: str, path: str | Path | None = None) -> bool:
    """Remove a snapshot date from the CSV (used by tests / data repair)."""
    csv_path = Path(path) if path else default_csv_path()
    existing = load_history(csv_path)
    if existing.empty:
        return False
    dropped = existing[existing["date"] != date_str]
    if len(dropped) == len(existing):
        return False
    dropped.to_csv(csv_path, index=False)
    return True
