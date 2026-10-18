"""A 股交易日历（P0 设计 §4）：交易日计数 + 停牌检测，fail-open。

提供者链（按序尝试，全部失败 → 自然日兜底，**绝不抛**）：

1. tushare ``trade_cal``（token 可用时，缓存全量日历到模块级）；
2. akshare ``tool_trade_date_hist_sina``（无 token）；
3. 兜底：自然日（行为 = 现状，``trading_days_between`` 返回 ``None``）。

停牌检测用 tushare ``suspend_d``（可选，缺失/失败 → ``None`` = 未知）。

纪律（P0 设计 §4）：

- 函数内**延迟 import**（不拉 tushare / akshare 链）；
- 模块级只缓存**日期集合**（无 IO 副作用）；
- ``calendar_source`` 供帧报告自证口径：``"tushare"`` / ``"akshare"`` / ``"fallback"``，
  fail-open 时不静默——报告标注 ``trading_days=None`` 即「按日历日」。
"""

from __future__ import annotations

import datetime as _dt
import os

# 模块级只缓存日期集合（无 IO 副作用）。
_CALENDAR_DATES: frozenset[str] | None = None
_SOURCE: str = ""


def _tushare_token() -> str:
    """tushare token：环境变量优先，其次 ``config.yaml`` 的 ``tushare.api_key``。"""
    token = os.getenv("TUSHARE_TOKEN")
    if token:
        return token
    try:
        from alphabee.config.loader import ConfigLoader

        return str(ConfigLoader().load_config().get("tushare", {}).get("api_key", "") or "")
    except Exception:
        return ""


def _norm_day(value: object) -> str:
    """把日期对象 / ``YYYYMMDD`` / ISO 字符串归一为 ``YYYY-MM-DD``；失败 → ``""``。"""
    if value is None:
        return ""
    if isinstance(value, _dt.datetime):
        return value.date().isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if len(digits) >= 8:
        d = digits[:8]
        return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
    return ""


def _tushare_dates() -> frozenset[str] | None:
    """tushare ``trade_cal`` → 交易日集合；token 缺失 / 失败 → ``None``。"""
    token = _tushare_token()
    if not token:
        return None
    try:
        import tushare as ts
    except Exception:
        return None
    try:
        ts.set_token(token)
        df = ts.pro_api().trade_cal(exchange="SSE", start_date="19900101", end_date="21000101", is_open="1")
    except Exception:
        return None
    if df is None or getattr(df, "empty", True) or "cal_date" not in df.columns:
        return None
    dates = {_norm_day(v) for v in df["cal_date"]}
    dates.discard("")
    return frozenset(dates) if dates else None


def _akshare_dates() -> frozenset[str] | None:
    """akshare ``tool_trade_date_hist_sina`` → 交易日集合；失败 → ``None``。"""
    try:
        import akshare as ak
    except Exception:
        return None
    try:
        df = ak.tool_trade_date_hist_sina()
    except Exception:
        return None
    if df is None or getattr(df, "empty", True) or "trade_date" not in df.columns:
        return None
    dates = {_norm_day(v) for v in df["trade_date"]}
    dates.discard("")
    return frozenset(dates) if dates else None


def _ensure_calendar() -> frozenset[str]:
    """惰性加载交易日集合（按提供者链；全部失败 → 空集 + ``fallback``）。"""
    global _CALENDAR_DATES, _SOURCE
    if _CALENDAR_DATES is not None:
        return _CALENDAR_DATES
    dates = _tushare_dates()
    if dates:
        _CALENDAR_DATES, _SOURCE = dates, "tushare"
        return dates
    dates = _akshare_dates()
    if dates:
        _CALENDAR_DATES, _SOURCE = dates, "akshare"
        return dates
    _CALENDAR_DATES, _SOURCE = frozenset(), "fallback"
    return _CALENDAR_DATES


def calendar_source() -> str:
    """可观测口径：``"tushare"`` / ``"akshare"`` / ``"fallback"``。"""
    _ensure_calendar()
    return _SOURCE


def trading_days_between(start: str, end: str) -> int | None:
    """``(start, end]`` 内的交易日数；日历不可得 → ``None``（调用方回落日历日）。

    ``end <= start`` → 0；日期无法解析 → ``None``（不猜测）。
    """
    calendar = _ensure_calendar()
    if not calendar:
        return None  # 自然日兜底口径
    s = _norm_day(start)
    e = _norm_day(end)
    if not s or not e:
        return None
    if e <= s:
        return 0
    return sum(1 for d in calendar if s < d <= e)


def last_trading_day(as_of: str) -> str | None:
    """``as_of`` 及之前最近一个交易日；日历不可得 / 无法解析 → ``None``。"""
    calendar = _ensure_calendar()
    if not calendar:
        return None
    target = _norm_day(as_of)
    if not target:
        return None
    candidates = [d for d in calendar if d <= target]
    return max(candidates) if candidates else None


def suspension_days(symbol: str, start: str, end: str) -> int | None:
    """停牌交易日数（tushare ``suspend_d``）；缺失/失败 → ``None``（未知）。

    仅当日历提供者为 tushare（token 可用）时才尝试；否则直接 ``None``，避免无谓
    网络往返。返回 ``None`` 表示「未知」，调用方不得把它当 0 使用。
    """
    _ensure_calendar()
    if _SOURCE != "tushare":
        return None
    token = _tushare_token()
    if not token:
        return None
    s = _norm_day(start)
    e = _norm_day(end)
    if not s or not e:
        return None
    try:
        import tushare as ts
    except Exception:
        return None
    try:
        ts.set_token(token)
        df = ts.pro_api().suspend_d(ts_code=symbol, start_date=s.replace("-", ""), end_date=e.replace("-", ""))
    except Exception:
        return None
    if df is None or getattr(df, "empty", True):
        return 0
    if "trade_date" not in df.columns:
        return None
    dates = {_norm_day(v) for v in df["trade_date"]}
    dates.discard("")
    return len(dates)
