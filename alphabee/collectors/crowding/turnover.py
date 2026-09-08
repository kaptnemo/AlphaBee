"""Crowding 采集器 — 交易热度（换手率分位 / 成交额占全市场比）。

复用已有数据自算：
- ``turnover_rate_percentile``：当前换手率在自身 5 年日频历史（Tushare ``daily_basic``）
  序列中的分位（0-1）。
- ``amount_pct_of_market``：个股成交额 ÷ 全市场成交额 × 100（PERCENT）。
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from alphabee.collectors.crowding.engine import (
    MIN_PERCENTILE_OBSERVATIONS,
    compute_amount_pct_of_market,
    compute_turnover_rate_percentile,
    normalize_code,
    to_float,
)


def _sorted_series(values: Any, col: str) -> list[float | None]:
    """按 trade_date 升序取某列 float 序列（缺失为 None）。"""
    if values is None or getattr(values, "empty", True) or col not in values.columns:
        return []
    d = values.sort_values("trade_date") if "trade_date" in values.columns else values
    return [to_float(v) for v in d[col]]


def _extract_turnover_amount_cny(mf: dict[str, Any] | None) -> float | None:
    """从 ``get_market_fact()`` 结果提取个股成交额（元）。

    ``turnover_amount`` 位于 ``latest_daily``（daily 日线行）内、单位为千元，×1000 → 元。
    缺失/无该键 → None（不静默回退 0）。
    """
    latest = (mf or {}).get("latest_daily") or {}
    raw = to_float(latest.get("turnover_amount"))
    return raw * 1000.0 if raw is not None else None


def fetch_turnover_rate_percentile(
    code: str,
    *,
    lookback_years: int = 5,
    min_obs: int = MIN_PERCENTILE_OBSERVATIONS,
) -> float | None:
    """拉 5 年 daily_basic 换手率历史并计算当前换手率分位（0-1）。"""
    from alphabee.agents.facts.tools._utils import normalize_ts_code  # noqa: PLC0415
    from alphabee.collectors.tushare.helper import TuShareHelper  # noqa: PLC0415

    try:
        ts_code = normalize_ts_code(code)
    except Exception:
        return None
    today = dt.date.today()
    start = today - dt.timedelta(days=lookback_years * 365)
    try:
        with TuShareHelper() as helper:
            df = helper.daily_basic(
                ts_code=ts_code,
                start_date=start.strftime("%Y%m%d"),
                end_date=today.strftime("%Y%m%d"),
                fields="ts_code,trade_date,turnover_rate",
            ).data
        history = _sorted_series(df, "turnover_rate")
        current = history[-1] if history else None
        return compute_turnover_rate_percentile(history, current, min_obs=min_obs)
    except Exception:
        return None


def fetch_amount_pct_of_market(
    code: str,
    *,
    market_turnover_cny: float | None = None,
) -> float | None:
    """个股成交额占全市场成交额比（PERCENT）。

    ``market_turnover_cny`` 缺省时复用 market_regime 采集器（其返回单位为亿元，此处 ×1e8 换算为元）。
    """
    sec_code = normalize_code(code)
    if not sec_code:
        return None

    turnover_amount_cny: float | None = None
    try:
        from alphabee.agents.facts.tools.market_fact import get_market_fact  # noqa: PLC0415

        mf = get_market_fact(sec_code)
        # turnover_amount 位于 latest_daily（daily 日线行）内、单位为千元，×1000 → 元
        turnover_amount_cny = _extract_turnover_amount_cny(mf)
    except Exception:
        turnover_amount_cny = None

    if market_turnover_cny is None:
        try:
            from alphabee.collectors.market_regime.risk_preference import (  # noqa: PLC0415
                fetch_market_turnover,
            )

            out = fetch_market_turnover()
            market_turnover_cny = to_float(out.values.get("market_turnover"))  # 亿元
            if market_turnover_cny is not None:
                market_turnover_cny *= 1e8  # 亿元 → 元
        except Exception:
            market_turnover_cny = None

    return compute_amount_pct_of_market(turnover_amount_cny, market_turnover_cny)
