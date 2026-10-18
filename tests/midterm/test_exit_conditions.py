"""退出条件生产点（P0 设计 §6.2）单测：两类新 kind、数据缺失 → []。"""

from __future__ import annotations

from alphabee.midterm.exit_conditions import build_exit_conditions
from alphabee.midterm.models import FactorSnapshot


def _snapshot(symbol: str = "600519.SH") -> FactorSnapshot:
    return FactorSnapshot(symbol=symbol)


def _bars(closes_pcts):
    return [{"close": c, "pct_chg": p} for c, p in closes_pcts]


def _kinds(conditions):
    return {c.kind for c in conditions}


def test_no_bars_returns_empty(monkeypatch):
    import alphabee.midterm.exit_conditions as ec

    monkeypatch.setattr(ec, "_fetch_daily_bars", lambda symbol, as_of: [])
    assert build_exit_conditions("600519.SH", _snapshot(), daily_bars=None, as_of="2024-01-05") == []
    assert build_exit_conditions("600519.SH", _snapshot(), daily_bars=[], as_of="2024-01-05") == []


def test_missing_close_skipped_no_conditions():
    bars = [{"close": None, "pct_chg": -5.0}, {"close": 2.0, "pct_chg": None}]
    assert build_exit_conditions("600519.SH", _snapshot(), daily_bars=bars) == []


def test_cumulative_drop_trigger_price_risk_stop():
    # 5 日内累计跌幅 25% ≤ −20%（2×10%）⇒ price_risk_stop
    bars = _bars([(100.0, -1.0), (95.0, -5.0), (90.0, -5.2), (85.0, -5.5), (75.0, -11.7)])
    conditions = build_exit_conditions("600519.SH", _snapshot(), daily_bars=bars)
    assert "price_risk_stop" in _kinds(conditions)


def test_two_consecutive_limit_down_trigger_price_risk_stop():
    # 连续 2 日跌停：|pct_chg| ≥ 10−0.5=9.5 ⇒ price_risk_stop
    bars = _bars([(100.0, -1.0), (99.0, -1.0), (98.0, -9.5), (88.0, -10.2)])
    conditions = build_exit_conditions("600519.SH", _snapshot(), daily_bars=bars)
    assert "price_risk_stop" in _kinds(conditions)


def test_no_price_risk_stop_without_edge():
    bars = _bars([(100.0, -1.0), (99.0, -1.0), (98.0, -2.0), (97.0, -1.0), (96.0, -1.0)])
    assert build_exit_conditions("600519.SH", _snapshot(), daily_bars=bars) == []


def test_delist_warning_latest_close_below_one():
    bars = _bars([(1.2, -1.0), (1.1, -1.0), (0.95, -13.6)])
    conditions = build_exit_conditions("600519.SH", _snapshot(), daily_bars=bars)
    assert "delist_warning" in _kinds(conditions)


def test_delist_warning_continuous_below_one_in_available_window():
    # 窗口不足 20 日，但可得窗口内全部 < 1 元 ⇒ delist_warning
    bars = _bars([(0.9, -5.0), (0.8, -5.0), (0.7, -5.0)])
    conditions = build_exit_conditions("600519.SH", _snapshot(), daily_bars=bars)
    assert "delist_warning" in _kinds(conditions)


def test_delist_warning_twenty_consecutive_below_one():
    bars = _bars([(0.99, -1.0) for _ in range(20)])
    conditions = build_exit_conditions("600519.SH", _snapshot(), daily_bars=bars)
    assert "delist_warning" in _kinds(conditions)


def test_no_delist_warning_when_latest_above_one():
    bars = _bars([(1.5, -1.0), (1.6, -1.0), (1.7, -1.0)])
    assert build_exit_conditions("600519.SH", _snapshot(), daily_bars=bars) == []


def test_delist_warning_20_consecutive_below_one_then_recovered_above_one():
    """修复钉：此前连续 20 日 <1 元、最新已回升 ≥1 元 ⇒ 独立判定仍置 delist_warning.met=True。"""
    bars = _bars([(0.9, -1.0) for _ in range(20)] + [(1.5, 5.0)])
    conditions = build_exit_conditions("600519.SH", _snapshot(), daily_bars=bars)
    assert "delist_warning" in _kinds(conditions)


def test_delist_warning_run_shorter_than_window_no_trigger():
    # 最长连续 <1 元只有 19 日（< 20）⇒ 不触发独立判定
    bars = _bars([(0.9, -1.0) for _ in range(19)] + [(1.5, 5.0)])
    assert build_exit_conditions("600519.SH", _snapshot(), daily_bars=bars) == []


def test_both_kinds_can_coexist():
    bars = _bars([(0.9, -9.5), (0.8, -10.2)])  # 连续 2 日跌停 + 最新 < 1 元
    conditions = build_exit_conditions("600519.SH", _snapshot(), daily_bars=bars)
    assert _kinds(conditions) == {"price_risk_stop", "delist_warning"}


def test_chinext_uses_20_percent_limit():
    # 创业板 20%：|−19.5| ≥ 20−0.5 连续两日 ⇒ price_risk_stop
    bars = _bars([(10.0, -19.5), (8.0, -20.0)])
    conditions = build_exit_conditions("300750.SZ", _snapshot("300750.SZ"), daily_bars=bars)
    assert "price_risk_stop" in _kinds(conditions)


def test_build_exit_conditions_only_returns_met_conditions():
    bars = _bars([(10.0, -1.0), (10.1, 1.0)])
    conditions = build_exit_conditions("600519.SH", _snapshot(), daily_bars=bars)
    assert conditions == []
