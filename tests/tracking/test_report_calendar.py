"""披露日历触发器（P0 设计 §7.2）单测：窗口 / 该出未出 / 预告提醒 / 日历失败 → []。"""

from __future__ import annotations

from alphabee.midterm import trading_calendar
from alphabee.tracking import report_calendar as rc
from alphabee.tracking.triggers import TriggerKind


def _patch_calendar(monkeypatch, source="tushare", days=None):
    monkeypatch.setattr(trading_calendar, "calendar_source", lambda: source)
    if days is not None:
        monkeypatch.setattr(trading_calendar, "trading_days_between", lambda start, end: days)
    return trading_calendar


def _sub_kinds(triggers):
    return [t.payload.get("sub_kind") for t in triggers]


def test_calendar_unavailable_returns_empty(monkeypatch):
    _patch_calendar(monkeypatch, source="fallback", days=5)
    assert rc.detect_report_calendar_triggers("600519.SH", as_of="2026-05-06", latest_period="20250930") == []


def test_invalid_as_of_returns_empty(monkeypatch):
    _patch_calendar(monkeypatch, source="tushare", days=5)
    assert rc.detect_report_calendar_triggers("600519.SH", as_of="not-a-date", latest_period=None) == []


def test_report_overdue_after_3_trading_days(monkeypatch):
    _patch_calendar(monkeypatch, source="tushare", days=5)
    triggers = rc.detect_report_calendar_triggers("600519.SH", as_of="2026-05-06", latest_period="20250930")
    overdue = [t for t in triggers if t.payload.get("sub_kind") == "report_overdue"]
    assert len(overdue) == 1
    assert set(overdue[0].payload["periods"]) == {"20251231", "20260331"}
    assert overdue[0].kind is TriggerKind.REPORT_CALENDAR


def test_report_overdue_respects_observed_latest_period(monkeypatch):
    _patch_calendar(monkeypatch, source="tushare", days=5)
    triggers = rc.detect_report_calendar_triggers("600519.SH", as_of="2026-05-06", latest_period="20251231")
    overdue = [t for t in triggers if t.payload.get("sub_kind") == "report_overdue"]
    assert overdue and overdue[0].payload["periods"] == ["20260331"]


def test_report_overdue_not_fired_before_3_trading_days(monkeypatch):
    _patch_calendar(monkeypatch, source="tushare", days=2)
    triggers = rc.detect_report_calendar_triggers("600519.SH", as_of="2026-05-06", latest_period="20250930")
    assert "report_overdue" not in _sub_kinds(triggers)  # 尚未到「到期后第 3 个交易日」


def test_forecast_due_annual_window_without_forecast(monkeypatch):
    _patch_calendar(monkeypatch, source="tushare", days=100)
    triggers = rc.detect_report_calendar_triggers("600519.SH", as_of="2026-01-15", latest_period="20250930")
    due = [t for t in triggers if t.payload.get("sub_kind") == "forecast_due"]
    assert due and due[0].payload["period"] == "20251231"


def test_forecast_due_h1_window_without_forecast(monkeypatch):
    _patch_calendar(monkeypatch, source="tushare", days=100)
    triggers = rc.detect_report_calendar_triggers("600519.SH", as_of="2026-07-10", latest_period=None)
    due = [t for t in triggers if t.payload.get("sub_kind") == "forecast_due"]
    assert due and due[0].payload["period"] == "20260630"


def test_forecast_due_not_fired_when_forecast_present(monkeypatch):
    _patch_calendar(monkeypatch, source="tushare", days=100)
    triggers = rc.detect_report_calendar_triggers("600519.SH", as_of="2026-01-15", latest_period="20251231")
    assert _sub_kinds(triggers) == []


def test_report_due_within_5_trading_days(monkeypatch):
    _patch_calendar(monkeypatch, source="tushare", days=3)
    triggers = rc.detect_report_calendar_triggers(
        "600519.SH", as_of="2026-04-27", latest_period="20251231", expected_periods=[]
    )
    due = [t for t in triggers if t.payload.get("sub_kind") == "report_due"]
    assert due and due[0].payload["deadline"] == "2026-04-30"


def test_report_due_not_fired_when_far(monkeypatch):
    _patch_calendar(monkeypatch, source="tushare", days=10)
    triggers = rc.detect_report_calendar_triggers(
        "600519.SH", as_of="2026-04-01", latest_period="20251231", expected_periods=[]
    )
    assert _sub_kinds(triggers) == []


def test_no_triggers_when_nothing_due(monkeypatch):
    _patch_calendar(monkeypatch, source="tushare", days=100)
    triggers = rc.detect_report_calendar_triggers(
        "600519.SH", as_of="2026-03-15", latest_period="20251231", expected_periods=[]
    )
    assert triggers == []


def test_expected_periods_helper_derivation():
    periods = rc._expected_periods("2026-05-06")
    assert "20251231" in periods
    assert "20260331" in periods
    assert "20260630" not in periods  # 半年报窗口 8/31 未到


def test_expected_periods_helper_before_deadline():
    periods = rc._expected_periods("2026-04-01")
    assert "20250930" in periods
    assert "20251231" not in periods  # 年报窗口 4/30 未到


def test_forecast_target_windows():
    import datetime as dt

    assert rc._forecast_target(dt.date(2026, 1, 15)) == "20251231"
    assert rc._forecast_target(dt.date(2026, 7, 10)) == "20260630"
    assert rc._forecast_target(dt.date(2026, 3, 15)) is None
