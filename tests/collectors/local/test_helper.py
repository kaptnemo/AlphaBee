"""collectors/local/helper 单元测试：ALL_STOCKS 申万对齐的生成与消费逻辑。"""

from pathlib import Path

import pandas as pd

import alphabee.collectors.local.helper as helper


def _basic_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "stock_code": ["600577.SH", "600519.SH", "300750.SZ", "000001.SZ"],
            "company_name": ["精达股份", "贵州茅台", "宁德时代", "平安银行"],
            "industry": ["电气设备", "白酒", "电气设备", "银行"],
        }
    )


def _sw_member_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "l1_code": ["801730.SI", "801120.SI", "801730.SI", "801780.SI"],
            "l1_name": ["电力设备", "食品饮料", "电力设备", "银行"],
            "l2_code": ["801738.SI", "801125.SI", "801736.SI", "801781.SI"],
            "l2_name": ["电网设备", "白酒Ⅱ", "电池", "国有大型银行Ⅱ"],
            "l3_code": ["857344.SI", None, "857123.SI", None],
            "l3_name": ["线缆部件及其他", None, "锂电池", None],
            "ts_code": ["600577.SH", "600519.SH", "300750.SZ", "000001.SZ"],
            "name": ["精达股份", "贵州茅台", "宁德时代", "平安银行"],
            "in_date": ["20210101", "20210101", "20210101", "20210101"],
            "out_date": [None, None, None, None],
            "is_new": ["Y", "Y", "Y", "Y"],
        }
    )


def _merged_df() -> pd.DataFrame:
    return helper.build_all_stocks_csv(_basic_df(), _sw_member_df(), Path("/tmp/x.csv"))


# ── 生成：合并后含 SW 列 ────────────────────────────────────────────────────


def test_build_all_stocks_csv_merges_sw_columns(tmp_path):
    out = tmp_path / "all_stocks.csv"
    df = helper.build_all_stocks_csv(_basic_df(), _sw_member_df(), out)
    assert "sw_l1_code" in df.columns
    assert "sw_l3_name" in df.columns
    row = df[df["stock_code"] == "600577.SH"].iloc[0]
    assert row["sw_l1_name"] == "电力设备"
    assert row["sw_l2_name"] == "电网设备"
    assert row["sw_l3_name"] == "线缆部件及其他"
    # 无申万归属的股票 SW 列为空，但仍在表内
    assert df[df["stock_code"] == "000001.SZ"].iloc[0]["sw_l1_name"] == "银行"


def test_build_all_stocks_csv_empty_member_keeps_basic(tmp_path):
    out = tmp_path / "all_stocks.csv"
    df = helper.build_all_stocks_csv(_basic_df(), pd.DataFrame(), out)
    assert len(df) == 4
    assert "sw_l1_code" not in df.columns


# ── 消费：get_stock_basic / get_industry_peers 按申万口径 ───────────────────


def test_get_stock_basic_returns_sw_industry(monkeypatch):
    monkeypatch.setattr(helper, "ALL_STOCKS", _merged_df())
    info = helper.get_stock_basic("600577.SH")
    assert info["industry"] == "线缆部件及其他"  # 最细申万名，非 stock_basic 的"电气设备"


def test_get_stock_basic_falls_back_when_no_sw(monkeypatch):
    monkeypatch.setattr(helper, "ALL_STOCKS", _basic_df())  # 旧版 CSV：无 sw 列
    info = helper.get_stock_basic("600519.SH")
    assert info["industry"] == "白酒"


def test_get_industry_peers_matches_sw_l3(monkeypatch):
    monkeypatch.setattr(helper, "ALL_STOCKS", _merged_df())
    peers = helper.get_industry_peers("线缆部件及其他")
    assert [p["stock_code"] for p in peers] == ["600577.SH"]


def test_get_industry_peers_matches_sw_l1(monkeypatch):
    monkeypatch.setattr(helper, "ALL_STOCKS", _merged_df())
    peers = helper.get_industry_peers("电力设备")
    codes = {p["stock_code"] for p in peers}
    assert codes == {"600577.SH", "300750.SZ"}


def test_get_industry_peers_excludes_and_limits(monkeypatch):
    monkeypatch.setattr(helper, "ALL_STOCKS", _merged_df())
    peers = helper.get_industry_peers("电力设备", exclude_stock_code="600577.SH", max_peers=1)
    assert [p["stock_code"] for p in peers] == ["300750.SZ"]


def test_get_industry_peers_falls_back_to_old_industry_col(monkeypatch):
    monkeypatch.setattr(helper, "ALL_STOCKS", _basic_df())  # 旧版 CSV：无 sw 列
    peers = helper.get_industry_peers("银行")
    assert [p["stock_code"] for p in peers] == ["000001.SZ"]
