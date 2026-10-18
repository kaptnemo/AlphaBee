"""退出条件生产（P0 设计 §6.2）：确定性规则，禁 LLM，fail-open。

现状：``ExitCondition`` 只有模型无任何生产点。本模块建立第一个生产点：

- ``price_risk_stop``：最近 N=5 交易日累计跌幅 ≤ −(2 × board_limit) 或 连续 2 日跌停
  （|pct_chg| ≥ board_limit_pct − 0.5pp）；
- ``delist_warning``：最新收盘 < 1.0 元（面值退市观察线）或窗口内连续 20 日收盘 < 1 元
  （窗口不足 20 日时按可得窗口判定「已连续」）。

数据失败（``bars=None``/空）→ ``[]``，绝不编造。阈值集中模块顶部常量（结构性示意）。
日线来源：tushare ``daily``（近 30 交易日，close/pct_chg），函数内延迟 import、fail-open；
``daily_bars`` 参数供测试注入替身。``is_st`` v1 不接（ST 名称识别顺延 RC-ST-NAME）。
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from alphabee.midterm.board_limits import board_limit_pct
from alphabee.midterm.models import ExitCondition

# 结构性示意阈值（P0 设计 §6.2，应回测）
_PRICE_WINDOW = 5  # 累计跌幅观察窗口（交易日）
_DELIST_WINDOW = 20  # 面值退市连续观察窗口（交易日）
_DELIST_PRICE = 1.0  # 面值退市观察线（元）
_LIMIT_EDGE_EPS = 0.5  # 封板判定边距（百分比点）：|change| ≥ limit − 0.5pp 视为涨跌停
_DAILY_LOOKBACK_DAYS = 30  # 内部取日线的日历日回看窗口


def _to_float(value: Any) -> float | None:
    """结构化数值归一；空/NaN/inf/无效 → ``None``（缺失不编造）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
    else:
        text = str(value).strip()
        if text in ("", "-", "--", "None", "null", "nan", "NaN"):
            return None
        try:
            f = float(text)
        except (TypeError, ValueError):
            return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _normalize_bars(daily_bars: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """把注入的日线记录归一为 ``[{close, pct_chg}]``（按 ``trade_date`` 升序；缺失则保持原序）。"""
    bars: list[dict[str, Any]] = []
    for bar in daily_bars or []:
        if not isinstance(bar, dict):
            continue
        close = _to_float(bar.get("close"))
        if close is None:
            continue
        bars.append(
            {
                "trade_date": str(bar.get("trade_date") or ""),
                "close": close,
                "pct_chg": _to_float(bar.get("pct_chg")),
            }
        )
    if bars and all(b["trade_date"] for b in bars):
        bars.sort(key=lambda b: b["trade_date"])
    return bars


def _fetch_daily_bars(symbol: str, as_of: str) -> list[dict[str, Any]]:
    """tushare ``daily``（近 ``_DAILY_LOOKBACK_DAYS`` 日历日）→ 日线；失败 → ``[]``。"""
    from alphabee.midterm.trading_calendar import _tushare_token

    token = _tushare_token()
    if not token:
        return []
    try:
        end = _dt.date.fromisoformat(as_of[:10]) if as_of else _dt.date.today()
    except ValueError:
        end = _dt.date.today()
    start = end - _dt.timedelta(days=_DAILY_LOOKBACK_DAYS)
    try:
        import tushare as ts

        ts.set_token(token)
        df = ts.pro_api().daily(
            ts_code=symbol,
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
    except Exception:
        return []
    if df is None or getattr(df, "empty", True):
        return []
    bars = [
        {
            "trade_date": str(getattr(row, "trade_date", "") or ""),
            "close": getattr(row, "close", None),
            "pct_chg": getattr(row, "pct_chg", None),
        }
        for row in df.itertuples(index=False)
    ]
    return bars


def _price_risk_stop(bars: list[dict[str, Any]], limit: float) -> bool:
    """5 交易日累计跌幅 ≤ −(2×limit)，或连续 2 日跌停。"""
    if limit <= 0.0:
        return False
    # ① 5 交易日累计跌幅
    window = bars[-_PRICE_WINDOW:]
    if len(window) >= 2:
        first = window[0]["close"]
        last = window[-1]["close"]
        if first > 0.0 and (last - first) / first * 100.0 <= -(2.0 * limit):
            return True
    # ② 连续 2 日跌停（|pct_chg| ≥ limit − 0.5pp 且为负）
    edge = limit - _LIMIT_EDGE_EPS
    if len(bars) >= 2:
        last_two = bars[-2:]
        pcts = [b["pct_chg"] for b in last_two]
        if all(p is not None and p <= -edge for p in pcts):
            return True
    return False


def _delist_warning(bars: list[dict[str, Any]]) -> bool:
    """最新收盘 < 1 元，或可得窗口内**最长连续**收盘 < 1 元达到 :data:`_DELIST_WINDOW`。

    连续 run 在全部可得日线上求（``run >= min(_DELIST_WINDOW, len(bars))``），因此
    「此前连续 20 日 < 1 元、最新已回升 ≥ 1 元」这一情形也能独立判出——旧实现只查
    ``all(末 20 根 < 1)``，当最新 bar ≥ 1 元时该分支数学上不可达。
    """
    if not bars:
        return False
    if bars[-1]["close"] < _DELIST_PRICE:
        return True
    longest = 0
    current = 0
    for bar in bars:
        if bar["close"] < _DELIST_PRICE:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    needed = min(_DELIST_WINDOW, len(bars))
    return longest >= needed


def build_exit_conditions(
    symbol: str,
    snapshot: object,
    *,
    as_of: str = "",
    daily_bars: list[dict[str, Any]] | None = None,
) -> list[ExitCondition]:
    """产出两类新退出条件（``met=True`` 才产出，避免噪声）。

    Args:
        symbol: 股票代码（用于板块涨跌停幅度与日线取数）。
        snapshot: 七因子快照（当前仅保留契约，不参与判定）。
        as_of: 判定时点（``YYYY-MM-DD``）；缺省今天。
        daily_bars: 注入的最近 N 个交易日日线（``[{close, pct_chg}]``，测试可替身）；
            ``None`` → 内部取 tushare daily（失败 → ``[]``）。

    Returns:
        ``met=True`` 的 :class:`ExitCondition` 列表；数据失败 → ``[]``（绝不编造）。
    """
    bars = _normalize_bars(daily_bars if daily_bars is not None else _fetch_daily_bars(symbol, as_of))
    if not bars:
        return []

    conditions: list[ExitCondition] = []
    limit = board_limit_pct(symbol)
    if _price_risk_stop(bars, limit):
        conditions.append(
            ExitCondition(
                kind="price_risk_stop",
                condition=f"5 交易日累计跌幅 ≤ −{2 * limit:g}% 或连续 2 日跌停（|涨跌幅| ≥ {limit - _LIMIT_EDGE_EPS:g}%）",
                met=True,
            )
        )
    if _delist_warning(bars):
        conditions.append(
            ExitCondition(
                kind="delist_warning",
                condition=f"最新收盘 < {_DELIST_PRICE:g} 元（面值退市观察线）或窗口内连续收盘 < {_DELIST_PRICE:g} 元",
                met=True,
            )
        )
    return conditions
