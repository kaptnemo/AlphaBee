from alphabee.collectors.market_regime.risk_preference import (
    fetch_margin,
    fetch_market_turnover,
    margin_balance_from,
    market_turnover_from,
    parse_sse_margin,
    parse_sse_turnover,
    parse_szse_margin,
    parse_szse_turnover,
)
from tests.market_regime._fakes import FakeAk, make_df


class TestTurnoverParsers:
    def test_sse_billion_and_szse_yuan(self) -> None:
        sse = make_df(["单日情况", "股票"], [["挂牌数", 2352.0], ["成交金额", 11683.43]])
        szse = make_df(["证券类别", "成交金额"], [["股票", 1.357599e12]])
        assert parse_sse_turnover(sse) == 11683.43
        assert parse_szse_turnover(szse) == 1.357599e12

    def test_none_on_missing_row(self) -> None:
        assert parse_sse_turnover(make_df(["单日情况", "股票"], [["挂牌数", 1.0]])) is None
        assert parse_szse_turnover(make_df(["证券类别", "成交金额"], [["债券", 1.0]])) is None


class TestMarginParsers:
    def test_sse_yuan_to_billion_and_szse_billion(self) -> None:
        sse = make_df(["信用交易日期", "融资余额"], [["20260807", 1_323_258_064_454.0]])
        szse = make_df(["融资余额"], [12696.44])
        assert parse_sse_margin(sse) == round(1_323_258_064_454.0 / 1e8, 2)
        assert parse_szse_margin(szse) == 12696.44


class TestCombinations:
    def test_market_turnover(self) -> None:
        assert market_turnover_from(11683.43, 1.357599e12) == round(11683.43 + 1.357599e12 / 1e8, 2)

    def test_margin_balance(self) -> None:
        sse_bn = round(1_323_258_064_454.0 / 1e8, 2)
        assert margin_balance_from(sse_bn, 12696.44) == round(sse_bn + 12696.44, 2)

    def test_none_when_one_leg_missing(self) -> None:
        assert market_turnover_from(None, 1.0) is None
        assert margin_balance_from(1.0, None) is None


class TestFetchWithWalkback:
    def test_market_turnover_falls_back_to_previous_trading_day(self) -> None:
        fake = FakeAk()
        fake.sse_deal["20260807"] = make_df(["单日情况", "股票"], [["成交金额", 11683.43]])
        fake.szse_summary["20260807"] = make_df(["证券类别", "成交金额"], [["股票", 1.357599e12]])
        out = fetch_market_turnover("2026-08-08", ak_module=fake)
        assert out.values["market_turnover"] == round(11683.43 + 1.357599e12 / 1e8, 2)

    def test_margin_balance(self) -> None:
        fake = FakeAk()
        fake.sse_margin["20260807"] = make_df(["信用交易日期", "融资余额"], [["20260807", 1_323_258_064_454.0]])
        fake.szse_margin["20260807"] = make_df(["融资余额"], [12696.44])
        out = fetch_margin("2026-08-08", ak_module=fake)
        assert out.values["margin_balance"] == round(1_323_258_064_454.0 / 1e8 + 12696.44, 2)

    def test_warns_when_no_valid_date(self) -> None:
        out = fetch_market_turnover("2026-08-01", ak_module=FakeAk())
        assert "market_turnover" not in out.values
        assert any("无法取得" in w for w in out.warnings)
