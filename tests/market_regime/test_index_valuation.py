from alphabee.collectors.market_regime.index_valuation import (
    INDEX_SYMBOLS,
    compute_moving_average,
    ep_from_pe,
    extract_index_valuation,
    fetch_all_market_valuation,
    fetch_hs300_trend,
    fetch_index_valuation,
    history,
)
from tests.market_regime._fakes import build_default_valuation_fake, make_df


class TestEpFromPe:
    def test_converts_pe_to_earnings_yield(self) -> None:
        assert ep_from_pe(13.71) == round(100 / 13.71, 4)

    def test_returns_none_for_non_positive_pe(self) -> None:
        assert ep_from_pe(0) is None
        assert ep_from_pe(-5) is None
        assert ep_from_pe(None) is None


class TestComputeMovingAverage:
    def test_average_of_trailing_window(self) -> None:
        series = [1.0, 2.0, 3.0, 4.0, 5.0]
        from pandas import Series

        assert compute_moving_average(Series(series), 3) == 4.0

    def test_none_when_insufficient_data(self) -> None:
        from pandas import Series

        assert compute_moving_average(Series([1.0, 2.0]), 3) is None


class TestExtractIndexValuation:
    def test_returns_latest_row_on_or_before_asof(self) -> None:
        df = make_df(
            ["日期", "滚动市盈率", "市净率"],
            [["2026-07-31", 12.0, 1.2], ["2026-08-07", 13.0, 1.3]],
        )
        pe, pb = extract_index_valuation(df, "日期", "滚动市盈率", "市净率", "2026-08-03")
        assert pe == 12.0
        assert pb == 1.2

    def test_none_when_asof_before_all_data(self) -> None:
        df = make_df(["日期", "滚动市盈率", "市净率"], [["2026-08-07", 13.0, 1.3]])
        pe, pb = extract_index_valuation(df, "日期", "滚动市盈率", "市净率", "2026-01-01")
        assert pe is None
        assert pb is None


class TestFetchIndexValuation:
    def test_populates_all_index_valuation_and_ep(self) -> None:
        out = fetch_index_valuation("2026-08-07", ak_module=build_default_valuation_fake())
        for symbol, prefix in INDEX_SYMBOLS:
            assert out.values[f"{prefix}_pe_ttm"] > 0
            assert out.values[f"{prefix}_pb"] > 0
            assert out.values[f"{prefix}_ep_ttm"] == ep_from_pe(out.values[f"{prefix}_pe_ttm"])

    def test_ep_matches_expected_value(self) -> None:
        out = fetch_index_valuation("2026-08-07", ak_module=build_default_valuation_fake())
        assert out.values["hs300_ep_ttm"] == round(100 / 13.0, 4)


class TestFetchAllMarketValuation:
    def test_pe_pb_and_percentiles(self) -> None:
        out = fetch_all_market_valuation("2026-08-07", ak_module=build_default_valuation_fake())
        assert out.values["all_market_pe_ttm"] == 38.28
        assert out.values["all_market_pe_10y_percentile"] == 0.68894
        assert out.values["all_market_pb"] == 2.7
        assert out.values["all_market_pb_10y_percentile"] == 0.58082


class TestFetchHs300Trend:
    def test_close_and_moving_averages(self) -> None:
        fake = build_default_valuation_fake()
        closes = [c for _, c in fake.hs300_daily[["date", "close"]].to_records(index=False)]
        out = fetch_hs300_trend("2026-08-07", ak_module=fake)
        assert out.values["hs300_close"] == closes[-1]
        for window in (20, 60, 250):
            expected = round(sum(closes[-window:]) / window, 2)
            assert out.values[f"hs300_ma{window}"] == expected


class TestHistory:
    def test_returns_dated_canonical_frame_with_derived_ep(self) -> None:
        df = history("2026-08-01", "2026-08-07", ak_module=build_default_valuation_fake())
        assert not df.empty
        assert df.index.is_monotonic_increasing
        assert "hs300_pe_ttm" in df.columns
        assert "hs300_ep_ttm" in df.columns
        assert "hs300_ma20" in df.columns

        expected = (100.0 / df["hs300_pe_ttm"]).round(4)
        assert df["hs300_ep_ttm"].sub(expected).abs().max() < 1e-3
        assert df["hs300_pe_ttm"].dropna().gt(0).all()
