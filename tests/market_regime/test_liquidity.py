from alphabee.collectors.market_regime.liquidity import (
    _month_gap,
    fetch_bond_yields,
    fetch_money_supply,
    fetch_shibor,
    fetch_social_financing,
    history,
    m1_m2_gap,
)
from tests.market_regime._fakes import FakeAk, make_df


class TestM1M2Gap:
    def test_subtracts_yoy_growth(self) -> None:
        assert m1_m2_gap(20.0, 8.0) == 12.0

    def test_none_when_missing(self) -> None:
        assert m1_m2_gap(None, 8.0) is None
        assert m1_m2_gap(20.0, None) is None


class TestMonthGap:
    def test_handles_year_boundary(self) -> None:
        assert _month_gap("202604", "202512") == 4
        assert _month_gap("202604", "202604") == 0


class TestFetchBondYields:
    def test_returns_cn_and_us_yields(self) -> None:
        fake = FakeAk()
        fake.bond_us_rate = make_df(
            ["日期", "中国国债收益率10年", "美国国债收益率10年"],
            [["2026-08-07", 1.71, 4.65]],
        )
        out = fetch_bond_yields("2026-08-07", ak_module=fake)
        assert out.values["cn_10y_yield"] == 1.71
        assert out.values["us_10y_yield"] == 4.65

    def test_warns_when_yield_is_nan(self) -> None:
        fake = FakeAk()
        fake.bond_us_rate = make_df(
            ["日期", "中国国债收益率10年", "美国国债收益率10年"],
            [["2026-08-07", None, 4.65]],
        )
        out = fetch_bond_yields("2026-08-07", ak_module=fake)
        assert "cn_10y_yield" not in out.values
        assert any("中债10年收益率缺失" in w for w in out.warnings)


class TestFetchShibor:
    def test_returns_3m_rate(self) -> None:
        fake = FakeAk()
        fake.shibor_df = make_df(["日期", "3M-定价"], [["2026-08-07", 1.43]])
        assert fetch_shibor("2026-08-07", ak_module=fake).values["shibor_3m"] == 1.43


class TestFetchMoneySupply:
    def test_picks_latest_month_and_derives_gap(self) -> None:
        fake = FakeAk()
        fake.money_supply = make_df(
            ["月份", "货币(M1)-同比增长", "货币和准货币(M2)-同比增长"],
            [
                ["202512", 1.0, 2.0],
                ["202601", 3.0, 4.0],
                ["202602", 3.8, 8.5],
            ],
        )
        out = fetch_money_supply("2026-08-07", ak_module=fake)
        assert out.values["m1_yoy"] == 3.8
        assert out.values["m2_yoy"] == 8.5
        assert out.values["m1_m2_gap"] == round(3.8 - 8.5, 2)

    def test_excludes_months_after_asof(self) -> None:
        fake = FakeAk()
        fake.money_supply = make_df(
            ["月份", "货币(M1)-同比增长", "货币和准货币(M2)-同比增长"],
            [["202601", 3.0, 4.0], ["202612", 9.0, 9.0]],
        )
        out = fetch_money_supply("2026-06-30", ak_module=fake)
        assert out.values["m1_yoy"] == 3.0


class TestFetchSocialFinancing:
    def test_picks_latest_month(self) -> None:
        fake = FakeAk()
        fake.social_financing = make_df(
            ["月份", "社会融资规模增量"],
            [["202601", 72185.0], ["202602", 23837.0], ["202604", 6245.0]],
        )
        out = fetch_social_financing("2026-08-07", ak_module=fake)
        assert out.values["social_financing_increment"] == 6245.0

    def test_warns_when_data_is_stale(self) -> None:
        fake = FakeAk()
        fake.social_financing = make_df(
            ["月份", "社会融资规模增量"],
            [["202601", 72185.0]],
        )
        out = fetch_social_financing("2026-08-07", ak_module=fake)
        assert out.values["social_financing_increment"] == 72185.0
        assert any("落后" in w for w in out.warnings)


class TestLiquidityHistory:
    def test_daily_fields_and_monthly_ffill(self) -> None:
        fake = FakeAk()
        fake.bond_us_rate = make_df(
            ["日期", "中国国债收益率10年", "美国国债收益率10年"],
            [["2026-08-03", 1.7, 4.6], ["2026-08-07", 1.71, 4.65]],
        )
        fake.money_supply = make_df(
            ["月份", "货币(M1)-同比增长", "货币和准货币(M2)-同比增长"],
            [["202601", 3.0, 4.0], ["202602", 3.8, 8.5]],
        )
        df = history("2026-08-01", "2026-08-07", ak_module=fake)
        assert not df.empty
        assert "cn_10y_yield" in df.columns
        assert "m1_yoy" in df.columns
        # 月频字段在 8 月网格上应 forward-fill 为 202602 的值
        assert df.loc["2026-08-03", "m1_yoy"] == 3.8
        assert df.loc["2026-08-03", "m1_m2_gap"] == round(3.8 - 8.5, 2)
