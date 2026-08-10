from alphabee.collectors.market_regime._utils import month_key, select_latest, walk_back_dates
from tests.market_regime._fakes import make_df


class TestMonthKey:
    def test_normalizes_formats(self) -> None:
        assert month_key("202604") == "202604"
        assert month_key("2008年01月份") == "200801"
        assert month_key(202604) == "202604"

    def test_none_for_invalid(self) -> None:
        assert month_key(None) is None
        assert month_key("") is None


class TestSelectLatest:
    def test_returns_latest_row_within_asof(self) -> None:
        df = make_df(
            ["日期", "值"],
            [["2026-07-31", 1.0], ["2026-08-05", 2.0], ["2026-08-07", 3.0]],
        )
        row = select_latest(df, "日期", "2026-08-06")
        assert row["值"] == 2.0
        assert row["日期"] == "2026-08-05"

    def test_none_when_nothing_on_or_before_asof(self) -> None:
        df = make_df(["日期", "值"], [["2026-08-07", 3.0]])
        assert select_latest(df, "日期", "2026-01-01") is None

    def test_none_on_empty_frame(self) -> None:
        assert select_latest(make_df(["日期", "值"], []), "日期", "2026-08-07") is None


class TestWalkBackDates:
    def test_generates_descending_dates(self) -> None:
        dates = walk_back_dates("2026-08-07", max_days=3)
        assert dates == ["20260807", "20260806", "20260805"]
