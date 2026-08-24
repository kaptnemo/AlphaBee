"""get_industry_daily 的 AkShare 申万回退路径单元测试（mock AkShareHelper，不触网）。"""

import pandas as pd
import pytest

import alphabee.collectors.akshare.helper as akshare_helper_module
from alphabee.providers.industry import (
    _get_sw_index_pe_pb,
    _iso_date,
    _opt_float,
    _try_akshare_sw_daily,
)


class _FakeResult:
    def __init__(self, df: pd.DataFrame):
        self._df = df

    def to_dataframe(self) -> pd.DataFrame:
        return self._df


def _fake_helper(hist: pd.DataFrame, info: dict[str, pd.DataFrame]):
    class FakeHelper:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def index_hist_sw(self, symbol: str, period: str = "day"):
            return _FakeResult(hist)

        def sw_index_first_info(self):
            return _FakeResult(info.get("first", pd.DataFrame()))

        def sw_index_second_info(self):
            return _FakeResult(info.get("second", pd.DataFrame()))

        def sw_index_third_info(self):
            return _FakeResult(info.get("third", pd.DataFrame()))

    return FakeHelper()


def _hist_df() -> pd.DataFrame:
    # 日期升序，覆盖窗口内外各一天，便于验证过滤与涨跌幅推导
    return pd.DataFrame(
        {
            "代码": ["801730", "801730", "801730", "801730"],
            "日期": pd.to_datetime(["2026-08-17", "2026-08-18", "2026-08-19", "2026-08-21"]).date,
            "收盘": [9000.0, 9100.0, 8500.0, 8700.0],
        }
    )


def _info_dfs() -> dict[str, pd.DataFrame]:
    return {
        "first": pd.DataFrame(
            {
                "行业代码": ["801730.SI"],
                "行业名称": ["电力设备"],
                "TTM(滚动)市盈率": [20.0],
                "市净率": [2.0],
            }
        ),
        "third": pd.DataFrame(
            {
                "行业代码": ["857344.SI"],
                "行业名称": ["线缆部件及其他"],
                "TTM(滚动)市盈率": [40.0],
                "市净率": [3.0],
            }
        ),
    }


def test_akshare_sw_daily_returns_rows_with_pe_pb(monkeypatch):
    helper = _fake_helper(_hist_df(), _info_dfs())
    monkeypatch.setattr(akshare_helper_module, "AkShareHelper", lambda: helper)

    result = _try_akshare_sw_daily("801730.SI", "20260817", "20260821")
    assert result is not None
    assert result.source == "akshare_sw_daily"
    # 4 行全在窗口内
    assert len(result.daily) == 4
    # 涨跌幅由收盘价逐日推导：(9100-9000)/9000*100 ≈ 1.11
    assert result.daily[0]["industry_change_pct"] == 0.0
    assert result.daily[1]["industry_change_pct"] == pytest.approx(100 / 90, abs=0.01)
    # PE/PB 快照来自 sw_index_first_info
    assert result.daily[0]["industry_pe_ttm"] == 20.0
    assert result.daily[0]["industry_pb"] == 2.0


def test_akshare_sw_daily_filters_outside_window(monkeypatch):
    helper = _fake_helper(_hist_df(), _info_dfs())
    monkeypatch.setattr(akshare_helper_module, "AkShareHelper", lambda: helper)

    result = _try_akshare_sw_daily("801730.SI", "20260818", "20260819")
    assert result is not None
    assert [r["trade_date"] for r in result.daily] == ["2026-08-18", "2026-08-19"]
    # 窗口外首日也带入前一日收盘价计算涨跌幅
    assert result.daily[0]["industry_change_pct"] == pytest.approx(100 / 90, abs=0.01)


def test_akshare_sw_daily_empty_hist_returns_none(monkeypatch):
    helper = _fake_helper(pd.DataFrame(), _info_dfs())
    monkeypatch.setattr(akshare_helper_module, "AkShareHelper", lambda: helper)
    assert _try_akshare_sw_daily("801730.SI", "20260817", "20260821") is None


def test_get_sw_index_pe_pb_matches_third_level(monkeypatch):
    helper = _fake_helper(_hist_df(), _info_dfs())
    monkeypatch.setattr(akshare_helper_module, "AkShareHelper", lambda: helper)
    assert _get_sw_index_pe_pb("857344.SI") == (40.0, 3.0)


def test_get_sw_index_pe_pb_unknown_returns_none(monkeypatch):
    helper = _fake_helper(_hist_df(), _info_dfs())
    monkeypatch.setattr(akshare_helper_module, "AkShareHelper", lambda: helper)
    assert _get_sw_index_pe_pb("999999.SI") == (None, None)


def test_iso_date_and_opt_float():
    assert _iso_date("20260821") == "2026-08-21"
    assert _opt_float(1.5) == 1.5
    assert _opt_float("1.5") == 1.5
    assert _opt_float(float("nan")) is None
    assert _opt_float("abc") is None
