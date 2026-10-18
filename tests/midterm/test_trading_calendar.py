"""交易日历（P0 设计 §4）单测：提供者链逐级 monkeypatch、兜底、交易日计数、停牌检测。"""

from __future__ import annotations

import pandas as pd

from alphabee.midterm import trading_calendar as tc

DATES = frozenset({"2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"})


def _reset(monkeypatch):
    monkeypatch.setattr(tc, "_CALENDAR_DATES", None)
    monkeypatch.setattr(tc, "_SOURCE", "")


def test_provider_chain_uses_tushare_first(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(tc, "_tushare_dates", lambda: DATES)
    called: list[str] = []

    def _ak():
        called.append("akshare")
        return frozenset({"2024-01-02"})

    monkeypatch.setattr(tc, "_akshare_dates", _ak)
    assert tc.calendar_source() == "tushare"
    assert called == []  # tushare 成功后不再尝试 akshare
    assert tc.trading_days_between("2024-01-02", "2024-01-08") == 4  # (start, end]


def test_provider_chain_falls_back_to_akshare(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(tc, "_tushare_dates", lambda: None)
    monkeypatch.setattr(tc, "_akshare_dates", lambda: DATES)
    assert tc.calendar_source() == "akshare"


def test_fallback_returns_none_trading_days_and_fallback_source(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(tc, "_tushare_dates", lambda: None)
    monkeypatch.setattr(tc, "_akshare_dates", lambda: None)
    assert tc.calendar_source() == "fallback"
    assert tc.trading_days_between("2024-01-02", "2024-01-08") is None  # 自然日兜底口径


def test_trading_days_between_counts_open_days_exclusive_start(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(tc, "_tushare_dates", lambda: DATES)
    assert tc.trading_days_between("2024-01-02", "2024-01-03") == 1
    assert tc.trading_days_between("2024-01-02", "2024-01-05") == 3


def test_trading_days_between_end_le_start_is_zero(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(tc, "_tushare_dates", lambda: DATES)
    assert tc.trading_days_between("2024-01-05", "2024-01-02") == 0


def test_trading_days_between_unparseable_date_is_none(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(tc, "_tushare_dates", lambda: DATES)
    assert tc.trading_days_between("bad", "2024-01-05") is None


def test_last_trading_day_returns_most_recent_open_day(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(tc, "_tushare_dates", lambda: DATES)
    assert tc.last_trading_day("2024-01-07") == "2024-01-05"  # 1/6-1/7 非交易日


def test_last_trading_day_fallback_is_none(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(tc, "_tushare_dates", lambda: None)
    monkeypatch.setattr(tc, "_akshare_dates", lambda: None)
    assert tc.last_trading_day("2024-01-07") is None


def test_suspension_days_returns_none_when_calendar_not_tushare(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(tc, "_tushare_dates", lambda: None)
    monkeypatch.setattr(tc, "_akshare_dates", lambda: DATES)  # source = akshare
    assert tc.suspension_days("600519.SH", "2024-01-02", "2024-01-05") is None


def test_suspension_days_returns_none_when_no_token(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(tc, "_tushare_dates", lambda: DATES)  # source = tushare
    monkeypatch.setattr(tc, "_tushare_token", lambda: "")
    assert tc.suspension_days("600519.SH", "2024-01-02", "2024-01-05") is None


def _install_fake_tushare(monkeypatch, suspend_rows):
    import sys
    import types

    class _Pro:
        def suspend_d(self, **kwargs):
            return pd.DataFrame(suspend_rows)

    fake = types.ModuleType("tushare")
    fake.set_token = lambda token: None  # noqa: E731
    fake.pro_api = lambda: _Pro()  # noqa: E731
    monkeypatch.setitem(sys.modules, "tushare", fake)


def test_suspension_days_counts_suspend_trade_dates(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(tc, "_tushare_dates", lambda: DATES)  # source = tushare
    monkeypatch.setattr(tc, "_tushare_token", lambda: "tok")
    _install_fake_tushare(
        monkeypatch,
        [
            {"ts_code": "600519.SH", "trade_date": "20240103", "suspend_timing": "全天", "suspend_type": "停牌"},
            {"ts_code": "600519.SH", "trade_date": "20240104", "suspend_timing": "全天", "suspend_type": "停牌"},
        ],
    )
    assert tc.suspension_days("600519.SH", "2024-01-02", "2024-01-05") == 2


def test_suspension_days_empty_is_zero(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(tc, "_tushare_dates", lambda: DATES)
    monkeypatch.setattr(tc, "_tushare_token", lambda: "tok")
    _install_fake_tushare(monkeypatch, [])
    assert tc.suspension_days("600519.SH", "2024-01-02", "2024-01-05") == 0
