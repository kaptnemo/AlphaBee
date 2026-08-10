from alphabee.market_regime.data import backfill_history, collect_and_persist, collect_snapshot
from alphabee.market_regime.persistence import latest_date, load_history
from tests.market_regime._fakes import (
    FakeAk,
    build_default_tushare_fake,
    build_default_valuation_fake,
    make_df,
)


def _build_full_fake() -> FakeAk:
    fake = build_default_valuation_fake()
    fake.bond_us_rate = make_df(
        ["日期", "中国国债收益率10年", "美国国债收益率10年"],
        [["2026-08-07", 1.71, 4.65]],
    )
    fake.shibor_df = make_df(["日期", "3M-定价"], [["2026-08-07", 1.43]])
    fake.money_supply = make_df(
        ["月份", "货币(M1)-同比增长", "货币和准货币(M2)-同比增长"],
        [["202602", 3.8, 8.5]],
    )
    fake.social_financing = make_df(["月份", "社会融资规模增量"], [["202604", 6245.0]])
    fake.activity = make_df(
        ["item", "value"],
        [["上涨", 3878.0], ["下跌", 1253.0], ["平盘", 74.0], ["涨停", 103.0], ["跌停", 6.0]],
    )
    fake.sse_deal["20260807"] = make_df(["单日情况", "股票"], [["成交金额", 11683.43]])
    fake.szse_summary["20260807"] = make_df(["证券类别", "成交金额"], [["股票", 1.357599e12]])
    fake.sse_margin["20260807"] = make_df(["信用交易日期", "融资余额"], [["20260807", 1_323_258_064_454.0]])
    fake.szse_margin["20260807"] = make_df(["融资余额"], [12696.44])
    return fake


class TestCollectSnapshot:
    def test_merges_all_akshare_collectors_into_canonical_fields(self) -> None:
        snap = collect_snapshot("2026-08-07", source="akshare", ak_module=_build_full_fake())
        assert snap.date == "2026-08-07"
        assert snap.values["hs300_pe_ttm"] > 0
        assert snap.values["cn_10y_yield"] == 1.71
        assert snap.values["up_stock_ratio"] > 0
        assert snap.values["margin_balance"] > 0
        assert "hs300_pe_ttm" in snap.sources

    def test_enabled_subset(self) -> None:
        snap = collect_snapshot("2026-08-07", source="akshare", enabled=["valuation"], ak_module=_build_full_fake())
        assert "hs300_pe_ttm" in snap.values
        assert "cn_10y_yield" not in snap.values

    def test_auto_prefers_tushare_and_fills_gaps_with_akshare(self) -> None:
        fake_ak = _build_full_fake()
        fake_ts = build_default_tushare_fake()
        # akshare 提供 hs300_pe_ttm=13.0（legu），tushare 提供 14.5 → tushare 应胜出
        snap = collect_snapshot("2026-08-07", source="auto", ak_module=fake_ak, ts_module=fake_ts)
        assert snap.values["hs300_pe_ttm"] == 14.5  # tushare 优先
        assert "cyb_pe_ttm" in snap.values  # 创业板仅 tushare 提供
        assert snap.values["cn_10y_yield"] == 1.71  # akshare 兜底
        assert snap.values["up_stock_ratio"] > 0  # 宽度仅 akshare

    def test_rejects_unknown_source(self) -> None:
        try:
            collect_snapshot("2026-08-07", source="bogus")
        except ValueError:
            return
        raise AssertionError("应抛出 ValueError")


class TestCollectAndPersist:
    def test_writes_csv_and_is_idempotent(self, tmp_path) -> None:
        csv = tmp_path / "market_indicator_daily.csv"
        snap1 = collect_and_persist("2026-08-07", path=csv, source="akshare", ak_module=_build_full_fake())
        df1 = load_history(csv)
        assert len(df1) == 1
        assert df1.iloc[0]["hs300_pe_ttm"] == snap1.values["hs300_pe_ttm"]
        # 同日期再次写入只更新，不新增行
        collect_and_persist("2026-08-07", path=csv, source="akshare", ak_module=_build_full_fake())
        assert len(load_history(csv)) == 1


class TestBackfillHistory:
    def test_merges_valuation_and_liquidity_series(self, tmp_path) -> None:
        csv = tmp_path / "market_indicator_daily.csv"
        n = backfill_history("2026-08-01", "2026-08-07", path=csv, source="akshare", ak_module=_build_full_fake())
        df = load_history(csv)
        assert n == len(df)
        assert n >= 1
        assert {"hs300_pe_ttm", "cn_10y_yield", "hs300_ma20"} <= set(df.columns)
        assert latest_date(csv) == "2026-08-07"
        # 回填后的快照可被读回
        row = df[df["date"] == "2026-08-07"].iloc[0]
        assert row["hs300_pe_ttm"] > 0

    def test_auto_merges_tushare_and_akshare_histories(self, tmp_path) -> None:
        csv = tmp_path / "market_indicator_daily.csv"
        n = backfill_history(
            "2026-08-01",
            "2026-08-07",
            path=csv,
            source="auto",
            ak_module=_build_full_fake(),
            ts_module=build_default_tushare_fake(),
        )
        df = load_history(csv)
        assert n == len(df)
        assert n >= 1
        row = df[df["date"] == "2026-08-07"].iloc[0]
        # tushare 的 hs300_pe_ttm 优先于 akshare legu
        assert row["hs300_pe_ttm"] == 14.5
        assert "cyb_pe_ttm" in row  # 创业板估值来自 tushare
        assert row["cn_10y_yield"] == 1.71  # 中债收益率仅 akshare
