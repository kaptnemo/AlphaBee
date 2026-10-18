"""A 股披露日历触发器（P0 设计 §7.2）：纯规则、零新数据源、禁 LLM。

强制披露窗口（常量表，结构性示意）：

- 年报（报告期 ``1231``）/ 一季报（``0331``）：4/30；
- 半年报（``0630``）：8/31；
- 三季报（``0930``）：10/31；
- 业绩预告强制窗口：年报预告 1/1–1/31、半年报预告 7/1–7/15。

三类触发（``kind=REPORT_CALENDAR``，``payload.sub_kind`` 区分）：

- ``report_overdue``：窗口到期后第 3 个交易日仍未观察到预期报告期（「该出未出」）；
- ``forecast_due``：进入预告强制窗口且无对应预告记录；
- ``report_due``：下一个披露窗口前 5 个交易日的「预告提醒」（低噪提示，payload 带窗口日）。

日历不可得（无法算交易日）→ 返回 ``[]``（fail-open）。本模块只调用
:mod:`alphabee.midterm.trading_calendar`，不自算第二份交易日口径。
"""

from __future__ import annotations

import datetime as _dt

from alphabee.midterm import trading_calendar
from alphabee.tracking.triggers import Trigger, TriggerKind

# 强制披露窗口（月, 日, 报告期 MMDD）；报告期 YYYYMMDD 的 deadline 由 _period_deadline 推导。
_REPORT_PERIODS: tuple[str, ...] = ("0331", "0630", "0930", "1231")

#: 该出未出：窗口到期后第 N 个交易日仍未观察到。
_OVERDUE_TRADING_DAYS = 3
#: 披露提醒：下一个窗口前 N 个交易日。
_REPORT_DUE_LEAD_DAYS = 5

# 预告强制窗口（start 月/日, end 月/日, 目标报告期 MMDD）。
_FORECAST_WINDOWS: tuple[tuple[int, int, int, int, str], ...] = (
    (1, 1, 1, 31, "1231"),  # 年报预告：1/1–1/31
    (7, 1, 7, 15, "0630"),  # 半年报预告：7/1–7/15
)


def _parse_date(value: str | None) -> _dt.date | None:
    if not value:
        return None
    try:
        return _dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _period_deadline(period: str) -> _dt.date:
    """报告期 ``YYYYMMDD`` → 法定披露截止日。"""
    year = int(period[:4])
    mmdd = period[4:]
    if mmdd == "0331":
        return _dt.date(year, 4, 30)
    if mmdd == "0630":
        return _dt.date(year, 8, 31)
    if mmdd == "0930":
        return _dt.date(year, 10, 31)
    if mmdd == "1231":
        return _dt.date(year + 1, 4, 30)
    return _dt.date(year, 12, 31)


def _candidate_periods(as_of: _dt.date) -> list[str]:
    """``as_of`` 附近可能涉及的披露窗口报告期（当年 + 前一年）。"""
    out: list[str] = []
    for year in (as_of.year - 1, as_of.year):
        for mmdd in _REPORT_PERIODS:
            out.append(f"{year}{mmdd}")
    return sorted(out)


def _expected_periods(as_of: str) -> list[str]:
    """``as_of`` 时点**应已披露**的报告期列表（截止日 ≤ as_of）。"""
    d = _parse_date(as_of)
    if d is None:
        return []
    return sorted(p for p in _candidate_periods(d) if _period_deadline(p) <= d)


def _forecast_target(as_of: _dt.date) -> str | None:
    """``as_of`` 落入预告强制窗口时，返回该窗口对应的目标报告期 ``YYYYMMDD``。"""
    for sm, sd, em, ed, mmdd in _FORECAST_WINDOWS:
        start = _dt.date(as_of.year, sm, sd)
        end = _dt.date(as_of.year, em, ed)
        if start <= as_of <= end:
            if mmdd == "1231":
                return f"{as_of.year - 1}1231"
            return f"{as_of.year}{mmdd}"
    return None


def detect_report_calendar_triggers(
    symbol: str,
    *,
    as_of: str,
    latest_period: str | None = None,
    expected_periods: list[str] | None = None,
) -> list[Trigger]:
    """披露日历触发（纯规则、禁 LLM、零新数据源）。

    Args:
        symbol: 标的代码。
        as_of: 判定时点（``YYYY-MM-DD``）。
        latest_period: 已观察到的最新报告期（``YYYYMMDD``，来自 forecast/express 记录的
            ``end_date`` 最大者）；``None`` = 尚无任何预告/快报记录。
        expected_periods: 应已披露的报告期列表；``None`` → 由 ``as_of`` 按窗口推导。

    Returns:
        ``TriggerKind.REPORT_CALENDAR`` 触发列表；日历不可得 / 日期非法 → ``[]``。
    """
    d = _parse_date(as_of)
    if d is None:
        return []
    periods = list(expected_periods) if expected_periods is not None else _expected_periods(as_of)

    # 日历不可得（无法算交易日）→ 整个来源返回 []（fail-open，不自算第二份口径）。
    if trading_calendar.calendar_source() not in ("tushare", "akshare"):
        return []

    triggers: list[Trigger] = []

    # 1) report_overdue：窗口到期后第 N 个交易日仍未观察到预期报告期
    missing: list[str] = []
    for period in periods:
        if latest_period is not None and period <= latest_period:
            continue  # 已观察到（latest_period ≥ period）
        deadline = _period_deadline(period)
        elapsed = trading_calendar.trading_days_between(deadline.isoformat(), d.isoformat())
        if elapsed is not None and elapsed >= _OVERDUE_TRADING_DAYS:
            missing.append(period)
    if missing:
        triggers.append(
            Trigger(
                kind=TriggerKind.REPORT_CALENDAR,
                symbol=symbol,
                reason=f"财报披露该出未出：{'、'.join(missing)}（窗口到期后已过 {_OVERDUE_TRADING_DAYS} 个交易日）",
                payload={
                    "sub_kind": "report_overdue",
                    "periods": missing,
                    "overdue_trading_days": _OVERDUE_TRADING_DAYS,
                },
            )
        )

    # 2) forecast_due：进入预告强制窗口且无对应预告记录
    target = _forecast_target(d)
    if target is not None and (latest_period is None or latest_period < target):
        triggers.append(
            Trigger(
                kind=TriggerKind.REPORT_CALENDAR,
                symbol=symbol,
                reason=f"进入业绩预告强制窗口但无对应预告：{target}",
                payload={"sub_kind": "forecast_due", "period": target},
            )
        )

    # 3) report_due：下一个披露窗口前 N 个交易日提醒
    upcoming: list[tuple[str, _dt.date]] = []
    for period in _candidate_periods(d):
        deadline = _period_deadline(period)
        if deadline < d:
            continue
        lead = trading_calendar.trading_days_between(d.isoformat(), deadline.isoformat())
        if lead is not None and lead <= _REPORT_DUE_LEAD_DAYS:
            upcoming.append((period, deadline))
    if upcoming:
        period, deadline = min(upcoming, key=lambda item: item[1])
        triggers.append(
            Trigger(
                kind=TriggerKind.REPORT_CALENDAR,
                symbol=symbol,
                reason=f"临近披露窗口提醒：{period} 截止 {deadline.isoformat()}（{_REPORT_DUE_LEAD_DAYS} 个交易日内）",
                payload={"sub_kind": "report_due", "period": period, "deadline": deadline.isoformat()},
            )
        )

    return triggers
