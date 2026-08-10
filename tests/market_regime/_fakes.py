"""Shared test fixtures: fake akshare-like modules and crafted DataFrames.

All market-regime unit tests are network-free: they inject a ``FakeAk`` into the
collectors via the ``ak_module`` parameter, so no external API is touched.
"""

from __future__ import annotations

import pandas as pd


def make_df(columns: list[str], rows: list[list]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=columns)


class FakeAk:
    """Minimal stand-in for the akshare module surface used by the collectors."""

    def __init__(self) -> None:
        self.pe_by_index: dict[str, pd.DataFrame] = {}
        self.pb_by_index: dict[str, pd.DataFrame] = {}
        self.ttm_df: pd.DataFrame | None = None
        self.all_pb_df: pd.DataFrame | None = None
        self.hs300_daily: pd.DataFrame | None = None
        self.bond_us_rate: pd.DataFrame | None = None
        self.shibor_df: pd.DataFrame | None = None
        self.money_supply: pd.DataFrame | None = None
        self.social_financing: pd.DataFrame | None = None
        self.activity: pd.DataFrame | None = None
        self.sse_deal: dict[str, pd.DataFrame] = {}
        self.szse_summary: dict[str, pd.DataFrame] = {}
        self.sse_margin: dict[str, pd.DataFrame] = {}
        self.szse_margin: dict[str, pd.DataFrame] = {}

    # ── valuation ────────────────────────────────────────────
    def stock_index_pe_lg(self, symbol: str) -> pd.DataFrame:
        return self.pe_by_index.get(symbol, make_df(["日期", "滚动市盈率"], []))

    def stock_index_pb_lg(self, symbol: str) -> pd.DataFrame:
        return self.pb_by_index.get(symbol, make_df(["日期", "市净率"], []))

    def stock_a_ttm_lyr(self) -> pd.DataFrame:
        return self.ttm_df if self.ttm_df is not None else make_df(["date", "middlePETTM"], [])

    def stock_a_all_pb(self) -> pd.DataFrame:
        return self.all_pb_df if self.all_pb_df is not None else make_df(["date", "middlePB"], [])

    def stock_zh_index_daily(self, symbol: str = "sh000300") -> pd.DataFrame:
        assert symbol == "sh000300"
        return self.hs300_daily if self.hs300_daily is not None else make_df(["date", "close"], [])

    # ── liquidity ────────────────────────────────────────────
    def bond_zh_us_rate(self) -> pd.DataFrame:
        return self.bond_us_rate if self.bond_us_rate is not None else make_df(["日期"], [])

    def macro_china_shibor_all(self) -> pd.DataFrame:
        return self.shibor_df if self.shibor_df is not None else make_df(["日期", "3M-定价"], [])

    def macro_china_money_supply(self) -> pd.DataFrame:
        return self.money_supply if self.money_supply is not None else make_df(["月份"], [])

    def macro_china_shrzgm(self) -> pd.DataFrame:
        return self.social_financing if self.social_financing is not None else make_df(["月份"], [])

    # ── breadth ──────────────────────────────────────────────
    def stock_market_activity_legu(self) -> pd.DataFrame:
        return self.activity if self.activity is not None else make_df(["item", "value"], [])

    # ── risk preference ──────────────────────────────────────
    def stock_sse_deal_daily(self, date: str) -> pd.DataFrame:
        return self.sse_deal.get(date, make_df(["单日情况", "股票"], []))

    def stock_szse_summary(self, date: str) -> pd.DataFrame:
        return self.szse_summary.get(date, make_df(["证券类别", "成交金额"], []))

    def stock_margin_sse(self, start_date: str, end_date: str) -> pd.DataFrame:
        return self.sse_margin.get(start_date, make_df(["信用交易日期", "融资余额"], []))

    def stock_margin_szse(self, date: str) -> pd.DataFrame:
        return self.szse_margin.get(date, make_df(["融资余额"], []))


def default_pe_rows(symbol: str) -> list[list]:
    """Craft a stable PE history for the given symbol (values differ per index)."""
    base = {"沪深300": 13.0, "中证500": 30.0, "中证1000": 32.0}[symbol]
    return [["2026-07-31", base - 0.2], ["2026-08-03", base - 0.1], ["2026-08-07", base]]


def default_pb_rows(symbol: str) -> list[list]:
    base = {"沪深300": 1.4, "中证500": 2.5, "中证1000": 2.4}[symbol]
    return [["2026-07-31", base - 0.1], ["2026-08-03", base], ["2026-08-07", base + 0.1]]


def build_default_valuation_fake() -> FakeAk:
    fake = FakeAk()
    for symbol in ("沪深300", "中证500", "中证1000"):
        fake.pe_by_index[symbol] = make_df(["日期", "滚动市盈率"], default_pe_rows(symbol))
        fake.pb_by_index[symbol] = make_df(["日期", "市净率"], default_pb_rows(symbol))
    fake.ttm_df = make_df(
        ["date", "middlePETTM", "quantileInRecent10YearsMiddlePeTtm"],
        [
            ["2026-07-31", 37.9, 0.68],
            ["2026-08-07", 38.28, 0.68894],
        ],
    )
    fake.all_pb_df = make_df(
        ["date", "middlePB", "quantileInRecent10YearsMiddlePB"],
        [
            ["2026-07-31", 2.6, 0.57],
            ["2026-08-07", 2.7, 0.58082],
        ],
    )
    closes = [4500 + i for i in range(260)]
    dates = _trading_dates(260)
    fake.hs300_daily = make_df(["date", "close"], [[d, c] for d, c in zip(dates, closes)])
    return fake


def _trading_dates(n: int, end: str = "2026-08-07") -> list[str]:
    import datetime as dt

    anchor = dt.date.fromisoformat(end)
    out: list[str] = []
    day = anchor
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day -= dt.timedelta(days=1)
    return list(reversed(out))


class FakeTs:
    """Minimal stand-in for a tushare ``pro_api()`` surface used by the collectors."""

    def __init__(self) -> None:
        self.index_dailybasic_data: dict[str, pd.DataFrame] = {}
        self.shibor_df: pd.DataFrame | None = None
        self.us_tycr_df: pd.DataFrame | None = None
        self.cn_m_df: pd.DataFrame | None = None
        self.sf_month_df: pd.DataFrame | None = None
        self.margin_by_date: dict[str, pd.DataFrame] = {}

    # ── index valuation ─────────────────────────────────────
    def index_dailybasic(
        self,
        ts_code: str,
        start_date: str,
        end_date: str,
        fields: str | None = None,
    ) -> pd.DataFrame:
        return self.index_dailybasic_data.get(ts_code, make_df(["trade_date", "pe_ttm", "pb"], []))

    # ── liquidity ───────────────────────────────────────────
    def shibor(self, start_date: str, end_date: str) -> pd.DataFrame:
        return self.shibor_df if self.shibor_df is not None else make_df(["date", "3m"], [])

    def us_tycr(self, start_date: str, end_date: str) -> pd.DataFrame:
        return self.us_tycr_df if self.us_tycr_df is not None else make_df(["date", "y10"], [])

    def cn_m(self, start_m: str, end_m: str) -> pd.DataFrame:
        return self.cn_m_df if self.cn_m_df is not None else make_df(["month", "m1_yoy", "m2_yoy"], [])

    def sf_month(self, start_m: str, end_m: str) -> pd.DataFrame:
        return self.sf_month_df if self.sf_month_df is not None else make_df(["month", "inc_month"], [])

    # ── margin ──────────────────────────────────────────────
    def margin(self, trade_date: str, fields: str | None = None) -> pd.DataFrame:
        return self.margin_by_date.get(trade_date, make_df(["trade_date", "exchange_id", "rzye"], []))


def build_default_tushare_fake() -> FakeTs:
    fake = FakeTs()
    # (latest_pe, latest_pb) per ts_code; previous-day rows are slightly lower
    data = {
        "000300.SH": (14.5, 1.46),
        "000905.SH": (41.0, 2.76),
        "000852.SH": (32.5, 2.45),
        "399006.SZ": (53.0, 7.12),
    }
    for code, (pe, pb) in data.items():
        fake.index_dailybasic_data[code] = make_df(
            ["trade_date", "pe_ttm", "pb"],
            [
                ["20260805", round(pe - 0.1, 4), pb],
                ["20260807", pe, pb],
            ],
        )
    fake.shibor_df = make_df(["date", "3m"], [["20260803", 1.43], ["20260807", 1.437]])
    fake.us_tycr_df = make_df(["date", "y10"], [["20260803", 4.3], ["20260807", 4.38]])
    fake.cn_m_df = make_df(
        ["month", "m1_yoy", "m2_yoy"],
        [["202606", 3.0, 7.0], ["202607", 4.0, 8.0]],
    )
    fake.sf_month_df = make_df(
        ["month", "inc_month"],
        [["202606", 22000.0], ["202607", 6245.0]],
    )
    fake.margin_by_date["20260807"] = make_df(
        ["trade_date", "exchange_id", "rzye"],
        [
            ["20260807", "SSE", 1_500_000_000_000.0],
            ["20260807", "SZSE", 1_100_000_000_000.0],
        ],
    )
    return fake
