from alphabee.collectors.market_regime.tushare import (
    fetch_index_valuation,
    fetch_liquidity,
    fetch_margin,
    history,
)
from tests.market_regime._fakes import FakeTs, build_default_tushare_fake, make_df


class TestFetchIndexValuation:
    def test_populates_all_indices_including_chinext(self) -> None:
        out = fetch_index_valuation("2026-08-07", ts_module=build_default_tushare_fake())
        assert out.values["hs300_pe_ttm"] == 14.5
        assert out.values["cyb_pe_ttm"] == 53.0
        assert out.values["cyb_pb"] == 7.12
        assert out.values["cyb_ep_ttm"] == round(100 / 53.0, 4)

    def test_warns_when_index_missing(self) -> None:
        fake = build_default_tushare_fake()
        del fake.index_dailybasic_data["000852.SH"]
        out = fetch_index_valuation("2026-08-07", ts_module=fake)
        assert "cs1000_pe_ttm" not in out.values
        assert any("无估值数据" in w for w in out.warnings)

    def test_respects_asof_date(self) -> None:
        out = fetch_index_valuation("2026-08-06", ts_module=build_default_tushare_fake())
        assert out.values["hs300_pe_ttm"] == 14.4  # 08-05 行


class TestFetchLiquidity:
    def test_rates_and_money_supply(self) -> None:
        out = fetch_liquidity("2026-08-07", ts_module=build_default_tushare_fake())
        assert out.values["shibor_3m"] == 1.437
        assert out.values["us_10y_yield"] == 4.38
        assert out.values["m1_yoy"] == 4.0
        assert out.values["m2_yoy"] == 8.0
        assert out.values["m1_m2_gap"] == round(4.0 - 8.0, 2)

    def test_excludes_months_after_asof(self) -> None:
        out = fetch_liquidity("2026-06-30", ts_module=build_default_tushare_fake())
        assert out.values["m1_yoy"] == 3.0  # 07 月数据被排除


class TestFetchMargin:
    def test_sums_exchanges_and_converts_to_billion(self) -> None:
        out = fetch_margin("2026-08-07", ts_module=build_default_tushare_fake())
        expected = round((1_500_000_000_000.0 + 1_100_000_000_000.0) / 1e8, 2)
        assert out.values["margin_balance"] == expected

    def test_walks_back_to_valid_trading_day(self) -> None:
        fake = FakeTs()
        fake.margin_by_date["20260806"] = make_df(
            ["trade_date", "exchange_id", "rzye"],
            [["20260806", "SSE", 2_000_000_000_000.0]],
        )
        out = fetch_margin("2026-08-08", ts_module=fake)
        assert out.values["margin_balance"] == round(2_000_000_000_000.0 / 1e8, 2)

    def test_warns_when_no_margin_data(self) -> None:
        out = fetch_margin("2026-08-01", ts_module=FakeTs())
        assert "margin_balance" not in out.values
        assert any("无法取得" in w for w in out.warnings)


class TestTushareHistory:
    def test_returns_dated_frame_with_ep_and_ffill(self) -> None:
        df = history("2026-08-01", "2026-08-07", ts_module=build_default_tushare_fake())
        assert not df.empty
        assert df.index.is_monotonic_increasing
        assert {"hs300_pe_ttm", "cyb_pe_ttm", "shibor_3m", "us_10y_yield", "m1_yoy"} <= set(df.columns)
        assert "hs300_ep_ttm" in df.columns
        # 月频 M1 在 8 月日频网格上前向填充
        assert df.loc["2026-08-07", "m1_yoy"] == 4.0
