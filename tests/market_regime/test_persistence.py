from alphabee.market_regime.models import CollectorOutput, MarketIndicatorSnapshot, MarketScore, MarketScoreResult
from alphabee.market_regime.persistence import (
    append_score_result,
    append_snapshot,
    drop_date,
    latest_date,
    latest_score_row,
    load_history,
    load_score_history,
)


def _snapshot(date_str: str, **values) -> MarketIndicatorSnapshot:
    return MarketIndicatorSnapshot(date=date_str, values=dict(values), fetched_at="2026-08-07T10:00:00")


class TestLoadHistory:
    def test_returns_empty_frame_when_missing(self, tmp_path) -> None:
        df = load_history(tmp_path / "nope.csv")
        assert df.empty


class TestAppendSnapshot:
    def test_appends_and_upserts_by_date(self, tmp_path) -> None:
        csv = tmp_path / "market_indicator_daily.csv"
        append_snapshot(_snapshot("2026-08-06", hs300_pe_ttm=13.0), path=csv)
        append_snapshot(_snapshot("2026-08-07", hs300_pe_ttm=13.5, cn_10y_yield=1.71), path=csv)
        # upsert same date with new value
        append_snapshot(_snapshot("2026-08-07", hs300_pe_ttm=13.6, cn_10y_yield=1.72), path=csv)

        df = load_history(csv)
        assert len(df) == 2
        row_07 = df[df["date"] == "2026-08-07"].iloc[0]
        assert row_07["hs300_pe_ttm"] == 13.6
        assert row_07["cn_10y_yield"] == 1.72
        assert latest_date(csv) == "2026-08-07"

    def test_merges_columns_across_snapshots(self, tmp_path) -> None:
        csv = tmp_path / "market_indicator_daily.csv"
        append_snapshot(_snapshot("2026-08-07", hs300_pe_ttm=13.0), path=csv)
        append_snapshot(_snapshot("2026-08-08", m1_yoy=3.8), path=csv)
        df = load_history(csv)
        assert {"hs300_pe_ttm", "m1_yoy"} <= set(df.columns)

    def test_date_and_fetched_at_always_present(self, tmp_path) -> None:
        csv = tmp_path / "market_indicator_daily.csv"
        append_snapshot(_snapshot("2026-08-07", x=1.0), path=csv)
        df = load_history(csv)
        assert {"date", "fetched_at"} <= set(df.columns)


class TestDropDate:
    def test_removes_only_requested_date(self, tmp_path) -> None:
        csv = tmp_path / "market_indicator_daily.csv"
        append_snapshot(_snapshot("2026-08-06", a=1.0), path=csv)
        append_snapshot(_snapshot("2026-08-07", a=2.0), path=csv)
        assert drop_date("2026-08-06", csv) is True
        df = load_history(csv)
        assert list(df["date"]) == ["2026-08-07"]
        assert drop_date("2026-08-06", csv) is False


class TestMerge:
    def test_merge_snapshot_collector_output(self) -> None:
        snap = MarketIndicatorSnapshot(date="2026-08-07")
        snap.merge(CollectorOutput(values={"hs300_pe_ttm": 13.5}, source="akshare:test"))
        assert snap.values["hs300_pe_ttm"] == 13.5
        assert snap.sources["hs300_pe_ttm"] == "akshare:test"


def _score_result(date_str: str, total: float) -> MarketScoreResult:
    return MarketScoreResult(
        date=date_str,
        scores=MarketScore(valuation_score=60.0, trend_score=70.0, liquidity_score=50.0, total_score=total),
    )


class TestScoreHistory:
    def test_appends_and_upserts_weekly_scores(self, tmp_path) -> None:
        csv = tmp_path / "market_score_history.csv"
        append_score_result(_score_result("2026-08-07", 72.5), path=csv)
        append_score_result(_score_result("2026-08-14", 65.0), path=csv)
        append_score_result(_score_result("2026-08-14", 66.0), path=csv)  # upsert

        df = load_score_history(csv)
        assert len(df) == 2
        assert list(df["date"]) == ["2026-08-07", "2026-08-14"]
        row = df[df["date"] == "2026-08-14"].iloc[0]
        assert row["total_score"] == 66.0
        assert row["valuation_score"] == 60.0

    def test_latest_score_row_provides_prev_week_position(self, tmp_path) -> None:
        csv = tmp_path / "market_score_history.csv"
        append_score_result(_score_result("2026-08-07", 72.5), path=csv)
        latest = latest_score_row(csv)
        assert latest is not None
        assert latest["total_score"] == 72.5

    def test_empty_history_returns_none(self, tmp_path) -> None:
        csv = tmp_path / "market_score_history.csv"
        assert latest_score_row(csv) is None
        df = load_score_history(csv)
        assert df.empty
