"""Forward-return computation + similar-history search (Phase 2.2).

Independent of the scoring pipeline: given the daily indicator history, compute for
each date the index forward return and max drawdown over the next ``horizon_days``
(6 months ≈ 126 calendar days), then — for a given current feature vector
(ERP percentile + trend score + liquidity score) — find the closest historical
weeks **within the same regime phase** by normalized Euclidean distance.

Look-ahead safety:
- forward returns are only computed for dates whose window is fully inside the
  available history (rows near "now" are excluded);
- each historical feature vector only uses indicator data on/before its own date
  (via ``compute_features`` + the scoring engine restricted to that date).
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

from alphabee.market_regime.models import SimilarityHit, SimilarityResult
from alphabee.market_regime.score_engine import MarketScoreEngine, compute_features

DEFAULT_HORIZON_DAYS = 126  # 6 months × 21 trading days

# 特征归一化尺度：erp_percentile 本身在 [0,1]，分数在 [0,100] → 统一除以各自尺度。
_FEATURE_SCALES: dict[str, float] = {
    "erp_percentile": 1.0,
    "trend_score": 100.0,
    "liquidity_score": 100.0,
}

_ENGINE: MarketScoreEngine | None = None


def _get_engine() -> MarketScoreEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = MarketScoreEngine()
    return _ENGINE


def _date_series(history: pd.DataFrame, price_col: str) -> pd.Series:
    """Return ``price_col`` indexed by datetime, sorted ascending."""
    df = history.copy()
    df["_date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["_date", price_col])
    series = pd.Series(
        pd.to_numeric(df[price_col], errors="coerce").astype(float).values,
        index=df["_date"].values,
    )
    return series.sort_index()


def compute_forward_returns(
    history: pd.DataFrame,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    price_col: str = "hs300_close",
) -> pd.DataFrame:
    """Forward return + max drawdown for every date with a full forward window.

    Args:
        history:      daily indicator history (must contain ``date`` + ``price_col``).
        horizon_days: forward window length in calendar days.
        price_col:    index close column used for returns.

    Returns:
        DataFrame with columns ``date`` (YYYY-MM-DD), ``forward_return``,
        ``max_drawdown`` — only dates whose forward window is fully realized.
    """
    series = _date_series(history, price_col)
    if series.empty:
        return pd.DataFrame(columns=["date", "forward_return", "max_drawdown"])

    max_date = series.index.max()
    rows: list[dict[str, Any]] = []
    for day, price in series.items():
        end_target = day + pd.Timedelta(days=horizon_days)
        if end_target > max_date:
            # 前视窗口尚未走完：跳过，避免用"未实现的未来"计算收益（防前视）。
            continue
        window = series[(series.index > day) & (series.index <= end_target)]
        if window.empty:
            continue
        end_price = window.iloc[-1]
        peak = window.cummax()
        rows.append(
            {
                "date": day.date().isoformat(),
                "forward_return": round(float(end_price / price - 1.0), 4),
                "max_drawdown": round(float(((window - peak) / peak).min()), 4),
            }
        )
    return pd.DataFrame(rows)


def build_feature_vector(
    history: pd.DataFrame,
    asof_date: str,
    snapshot_values: dict[str, float] | None = None,
    engine: MarketScoreEngine | None = None,
) -> dict[str, float | None]:
    """Normalized feature vector for one date: ERP percentile, trend, liquidity.

    Only data on/before ``asof_date`` is used (no look-ahead). ``snapshot_values``
    may be passed to avoid re-reading the row.
    """
    restricted = history[history["date"] <= asof_date] if "date" in history.columns else history
    if snapshot_values is None:
        row = history[history["date"] == asof_date]
        if row.empty:
            return {}
        snapshot_values = {k: v for k, v in row.iloc[0].items() if k not in ("date", "fetched_at")}
    score_engine = engine or _get_engine()
    features = compute_features(snapshot_values, restricted, asof_date)
    result = score_engine.score(snapshot_values, history=restricted, asof_date=asof_date)
    return {
        "erp_percentile": features.get("erp_percentile"),
        "trend_score": result.scores.trend_score,
        "liquidity_score": result.scores.liquidity_score,
    }


def _euclidean(current: dict[str, float | None], historical: dict[str, float | None]) -> float | None:
    """Normalized Euclidean distance over the feature components both sides have."""
    pairs: list[float] = []
    for key, scale in _FEATURE_SCALES.items():
        c = current.get(key)
        h = historical.get(key)
        if c is None or h is None:
            continue
        pairs.append(((c / scale) - (h / scale)) ** 2)
    if not pairs:
        return None
    return math.sqrt(sum(pairs))


def _summary(hits: list[SimilarityHit]) -> dict[str, float | None]:
    if not hits:
        return {
            "positive_probability": None,
            "median_forward_return": None,
            "median_max_drawdown": None,
        }
    fwd = [h.forward_return for h in hits if h.forward_return is not None]
    dd = [h.max_drawdown for h in hits if h.max_drawdown is not None]
    return {
        "positive_probability": round(sum(1 for v in fwd if v > 0) / len(fwd), 3) if fwd else None,
        "median_forward_return": round(float(sorted(fwd)[len(fwd) // 2]), 4) if fwd else None,
        "median_max_drawdown": round(float(sorted(dd)[len(dd) // 2]), 4) if dd else None,
    }


def search_similar(
    history: pd.DataFrame,
    regime_history: pd.DataFrame,
    features: dict[str, float | None],
    phase: str,
    date: str = "",
    k: int = 5,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    price_col: str = "hs300_close",
) -> SimilarityResult:
    """Return the ``k`` closest historical weeks in ``phase`` with forward-return stats.

    Args:
        history:        daily indicator history (for forward returns + features).
        regime_history: regime classification history (``date`` + ``phase`` columns).
        features:       current normalized feature vector.
        phase:          only historical weeks in this phase are considered.
        date:           current evaluation date (for the result payload).
        k:              number of nearest neighbors to return.
        horizon_days:   forward window length for return/drawdown stats.

    Returns:
        ``SimilarityResult`` with the top-``k`` hits sorted by distance and the
        aggregated positive probability / median forward return / median drawdown.
        Always carries a limitation note (statistical reference, not a promise).
    """
    fwd = compute_forward_returns(history, horizon_days=horizon_days, price_col=price_col)
    fwd_by_date = {row["date"]: row for _, row in fwd.iterrows()}

    engine = _get_engine()
    scored: list[tuple[float, SimilarityHit]] = []
    for _, row in regime_history.iterrows():
        if row.get("phase") != phase:
            continue
        day = str(row["date"])
        if day not in fwd_by_date:
            # 该历史周的前视窗口尚未走完 → 排除（防前视偏差）。
            continue
        historical_features = build_feature_vector(history, day, engine=engine)
        distance = _euclidean(features, historical_features)
        if distance is None:
            continue
        scored.append(
            (
                distance,
                SimilarityHit(
                    date=day,
                    phase=phase,
                    distance=round(distance, 4),
                    forward_return=fwd_by_date[day]["forward_return"],
                    max_drawdown=fwd_by_date[day]["max_drawdown"],
                ),
            )
        )

    scored.sort(key=lambda pair: pair[0])
    hits = [hit for _, hit in scored[:k]]
    return SimilarityResult(
        date=date,
        phase=phase,
        features={key: features.get(key) for key in _FEATURE_SCALES},
        hits=hits,
        sample_size=len(scored),
        **_summary(hits),
        limitation_note=(
            "相似历史仅作统计参考，不构成预测承诺；特征用欧氏距离衡量，样本需覆盖完整牛熊周期才有统计意义。"
        ),
    )
