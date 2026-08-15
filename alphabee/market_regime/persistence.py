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

from alphabee.market_regime.models import MarketIndicatorSnapshot, MarketScoreResult, RegimeTransition

DEFAULT_DATA_DIR = Path("data") / "market_regime"
DEFAULT_CSV = DEFAULT_DATA_DIR / "market_indicator_daily.csv"
DEFAULT_SCORE_HISTORY = DEFAULT_DATA_DIR / "market_score_history.csv"


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

    # 以 date 为主键做 upsert：同一天多次采集（盘中/收盘各跑一次）只保留最后一次，
    # backfill_history 反复执行也不会产生重复行 → 幂等，可直接定期调度。
    if existing.empty:
        frame = pd.DataFrame([new_row])
    else:
        # 剔除同 date 的旧行后再拼接新行 → "最新快照获胜"
        date_mask = existing["date"] != snapshot.date
        frame = pd.concat([existing.loc[date_mask], pd.DataFrame([new_row])], ignore_index=True)

    # 固定列序：date, fetched_at, 其余 canonical 字段按字母序。
    # 好处：列结构稳定可读；新增字段时不漂移；便于 diff 与 CSV 审计。
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


# ── 周级评分历史（market_score_history.csv） ──────────────────────────────
#
# 列：date, total_score, valuation_score, trend_score, liquidity_score,
#     risk_preference_delta, regime, position_low, position_high
# 该表为 position.py 的 prev_week_score（单周仓位限制）提供上一期建议仓位。

DEFAULT_SCORE_COLUMNS = [
    "date",
    "total_score",
    "valuation_score",
    "trend_score",
    "liquidity_score",
    "risk_preference_delta",
    "regime",
    "position_low",
    "position_high",
]


def score_history_path() -> Path:
    return DEFAULT_SCORE_HISTORY


def load_score_history(path: str | Path | None = None) -> pd.DataFrame:
    """Load weekly score history as a DataFrame (empty frame with columns if missing)."""
    csv_path = Path(path) if path else score_history_path()
    if not csv_path.exists():
        return pd.DataFrame(columns=DEFAULT_SCORE_COLUMNS)
    df = pd.read_csv(csv_path)
    for col in DEFAULT_SCORE_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df["date"] = df["date"].astype(str)
    return df[DEFAULT_SCORE_COLUMNS]


def latest_score_row(path: str | Path | None = None) -> pd.Series | None:
    """Latest weekly score row (used to read ``prev_week_score``), or None."""
    df = load_score_history(path)
    if df.empty:
        return None
    df = df.dropna(subset=["date"]).sort_values("date")
    if df.empty:
        return None
    return df.iloc[-1]


def append_score_result(result: MarketScoreResult, path: str | Path | None = None) -> Path:
    """Upsert a scored week into ``market_score_history.csv`` (idempotent by date)."""
    csv_path = Path(path) if path else score_history_path()
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    existing = load_score_history(csv_path)
    row = {
        "date": result.date,
        "total_score": result.scores.total_score,
        "valuation_score": result.scores.valuation_score,
        "trend_score": result.scores.trend_score,
        "liquidity_score": result.scores.liquidity_score,
        "risk_preference_delta": result.scores.risk_preference_delta,
        "regime": result.snapshot.regime if result.snapshot else "",
        "position_low": result.position.position_low if result.position else None,
        "position_high": result.position.position_high if result.position else None,
    }
    # 与 append_snapshot 相同的 upsert 语义：周级评分按 date 幂等写入，
    # 保证 position.py 读 prev_week_score 时拿到的是最新一次评分。
    if existing.empty:
        frame = pd.DataFrame([row])
    else:
        mask = existing["date"] != result.date
        frame = pd.concat([existing.loc[mask], pd.DataFrame([row])], ignore_index=True)
    frame = frame[DEFAULT_SCORE_COLUMNS].sort_values("date").reset_index(drop=True)
    frame.to_csv(csv_path, index=False)
    return csv_path


# ── 阶段状态历史（regime_history.csv） ─────────────────────────────────────
#
# 列：date, phase, confidence, transition_from, transition_valid, suspicious
# 该表为状态机迁移回放与相似历史搜索（Phase 2）提供"哪天处于哪个阶段"的锚点。

DEFAULT_REGIME_HISTORY = DEFAULT_DATA_DIR / "regime_history.csv"

REGIME_COLUMNS = [
    "date",
    "phase",
    "confidence",
    "transition_from",
    "transition_valid",
    "suspicious",
]


def regime_history_path() -> Path:
    return DEFAULT_REGIME_HISTORY


def load_regime_history(path: str | Path | None = None) -> pd.DataFrame:
    """Load regime history as a DataFrame (empty frame with columns if missing)."""
    csv_path = Path(path) if path else regime_history_path()
    if not csv_path.exists():
        return pd.DataFrame(columns=REGIME_COLUMNS)
    df = pd.read_csv(csv_path)
    for col in REGIME_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df["date"] = df["date"].astype(str)
    return df[REGIME_COLUMNS]


def append_regime_transition(entry: RegimeTransition, path: str | Path | None = None) -> Path:
    """Upsert one state-machine step into ``regime_history.csv`` (idempotent by date)."""
    csv_path = Path(path) if path else regime_history_path()
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    existing = load_regime_history(csv_path)
    row = {
        "date": entry.date,
        "phase": entry.phase,
        "confidence": entry.confidence,
        "transition_from": entry.transition_from,
        "transition_valid": entry.transition_valid,
        "suspicious": entry.suspicious,
    }
    if existing.empty:
        frame = pd.DataFrame([row])
    else:
        mask = existing["date"] != entry.date
        frame = pd.concat([existing.loc[mask], pd.DataFrame([row])], ignore_index=True)
    frame = frame[REGIME_COLUMNS].sort_values("date").reset_index(drop=True)
    frame.to_csv(csv_path, index=False)
    return csv_path
