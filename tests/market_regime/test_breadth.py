from alphabee.collectors.market_regime.breadth import fetch, parse_activity
from tests.market_regime._fakes import FakeAk, make_df


def _activity_rows() -> list[list]:
    return [
        ["上涨", 3878.0],
        ["涨停", 103.0],
        ["下跌", 1253.0],
        ["跌停", 6.0],
        ["平盘", 74.0],
        ["活跃度", 74.49],
        ["统计日期", "2026-08-10 15:00:00"],
    ]


class TestParseActivity:
    def test_computes_up_stock_ratio(self) -> None:
        values, stat_date, warnings = parse_activity(make_df(["item", "value"], _activity_rows()))
        expected = round(3878.0 / (3878.0 + 1253.0 + 74.0) * 100, 2)
        assert values["up_stock_ratio"] == expected
        assert values["limit_up_count"] == 103.0
        assert values["limit_down_count"] == 6.0
        assert stat_date == "2026-08-10 15:00:00"
        assert warnings == []

    def test_warns_when_counts_missing(self) -> None:
        values, _, warnings = parse_activity(make_df(["item", "value"], [["上涨", 1.0]]))
        assert "up_stock_ratio" not in values
        assert any("上涨/下跌/平盘" in w for w in warnings)


class TestFetch:
    def test_fetch_populates_canonical_fields(self) -> None:
        fake = FakeAk()
        fake.activity = make_df(["item", "value"], _activity_rows())
        out = fetch(asof_date="2026-08-10", ak_module=fake)
        assert out.values["up_stock_ratio"] > 0
        assert out.values["limit_up_count"] == 103.0

    def test_warns_when_asof_date_mismatch(self) -> None:
        fake = FakeAk()
        fake.activity = make_df(["item", "value"], _activity_rows())
        out = fetch(asof_date="2026-08-01", ak_module=fake)
        assert any("晚于 asof_date" in w for w in out.warnings)
