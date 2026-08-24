from pathlib import Path
from typing import Any

import pandas as pd

STATIC_DIR = Path(__file__).resolve().parents[2] / "static"

# 申万层级 → canonical 列名（all_stocks.csv 中的 SW 分类列）
SW_NAME_COLS = ("sw_l1_name", "sw_l2_name", "sw_l3_name")


def get_all_stocks() -> pd.DataFrame:
    file_path = STATIC_DIR / "all_stocks.csv"
    return pd.read_csv(file_path)


ALL_STOCKS = get_all_stocks()


def _sw_industry_name(row: dict[str, Any]) -> str:
    """取该行最细的申万行业名（L3 → L2 → L1），无则返回空串。"""
    for col in reversed(SW_NAME_COLS):
        if col in row:
            v = row[col]
            if isinstance(v, str) and v:
                return v
    return ""


def get_stock_basic(stock_code: str) -> dict[str, str] | None:
    """从本地 CSV 文件中获取单只股票的基本信息。

    ``industry`` 与 ``get_industry_fact`` 对齐：优先返回申万行业名（L3 → L2 → L1），
    无申万分类时回退 stock_basic 的证监会口径名。
    """
    stock_info = ALL_STOCKS[ALL_STOCKS["stock_code"] == stock_code]
    if stock_info.empty:
        return None
    row = stock_info.iloc[0].to_dict()
    sw_industry = _sw_industry_name(row)
    if sw_industry:
        row["industry"] = sw_industry
    return row


def get_industry_peers(
    industry: str, exclude_stock_code: str | None = None, max_peers: int = 10
) -> list[dict[str, str]]:
    """从本地 CSV 获取同一申万行业的股票列表（L1/L2/L3 任意层级名称均可匹配）。

    优先按申万列（``sw_l1_name`` / ``sw_l2_name`` / ``sw_l3_name``）匹配；
    旧版 CSV 无申万列时回退 ``industry`` 列（证监会口径）。
    """
    sw_cols = [c for c in SW_NAME_COLS if c in ALL_STOCKS.columns]
    if sw_cols:
        peers = ALL_STOCKS[ALL_STOCKS[sw_cols].eq(industry).any(axis=1)]
    else:
        peers = ALL_STOCKS[ALL_STOCKS["industry"] == industry]
    if exclude_stock_code:
        peers = peers[peers["stock_code"] != exclude_stock_code]
    return peers.head(max_peers).to_dict(orient="records")


# ── all_stocks.csv 生成（对齐申万分类）───────────────────────────────────────


def build_all_stocks_csv(basic_df: pd.DataFrame, sw_member_df: pd.DataFrame, output_path: Path) -> pd.DataFrame:
    """合并 stock_basic 与 index_member_all，生成含申万 L1/L2/L3 的 all_stocks.csv。

    Args:
        basic_df: ``stock_basic(list_status="L")`` 经 adapter 适配后的 DataFrame
            （含 ``stock_code`` / ``company_name`` 等 canonical 列）。
        sw_member_df: ``index_member_all(l1_code=...)`` 拼接后的原始 DataFrame
            （列 ``l1_code / l1_name / l2_code / l2_name / l3_code / l3_name /
            ts_code / in_date / out_date / is_new``，未走 adapter 重命名）。
        output_path: CSV 输出路径。

    Returns:
        合并后的 DataFrame（同时写盘到 output_path）。
    """
    df = basic_df.copy()
    if sw_member_df is None or sw_member_df.empty:
        df.to_csv(output_path, index=False)
        return df

    member = sw_member_df.copy()
    member = member.rename(
        columns={
            "ts_code": "stock_code",
            "l1_code": "sw_l1_code",
            "l1_name": "sw_l1_name",
            "l2_code": "sw_l2_code",
            "l2_name": "sw_l2_name",
            "l3_code": "sw_l3_code",
            "l3_name": "sw_l3_name",
        }
    )
    sw_cols = [
        "sw_l1_code",
        "sw_l1_name",
        "sw_l2_code",
        "sw_l2_name",
        "sw_l3_code",
        "sw_l3_name",
    ]
    # 防御去重：每只股票保留最新一行（in_date 按字符串倒序取最大）
    member = member.sort_values("in_date").drop_duplicates(subset=["stock_code"], keep="last")
    df = df.merge(member[["stock_code", *sw_cols]], on="stock_code", how="left")
    df.to_csv(output_path, index=False)
    return df


def fetch_all_stocks_with_sw() -> tuple[pd.DataFrame, pd.DataFrame]:
    """拉取全市场 stock_basic + 申万 L1/L2/L3 归属。

    申万成分按 ``index_member_all(l1_code=...)`` 逐 L1 行业抓取（约 31 次调用，
    每次返回该 L1 全部成分股的完整 l1/l2/l3 路径）。需有效 Tushare token
    （stock_basic 与 index_member_all 均需 2000 积分）。

    Returns:
        (basic_df, sw_member_df)。
    """
    from alphabee.collectors.tushare.helper import TuShareHelper

    with TuShareHelper() as helper:  # type: ignore[no-untyped-call]
        basic = helper.stock_basic(exchange="", list_status="L").data
        l1 = helper.index_classify(level="L1", src="SW2021").data
        parts: list[pd.DataFrame] = []
        for code in l1["sw_code"]:
            df = helper.index_member_all(l1_code=code, is_new="Y").data
            if df is not None and not df.empty:
                parts.append(df)
    member = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    return basic, member


def rebuild_all_stocks_csv(output_path: Path | None = None) -> Path:
    """拉取并重建 all_stocks.csv（含申万 L1/L2/L3 列）。返回输出路径。"""
    output_path = output_path or (STATIC_DIR / "all_stocks.csv")
    basic, member = fetch_all_stocks_with_sw()
    build_all_stocks_csv(basic, member, output_path)
    return output_path


if __name__ == "__main__":
    stocks = get_all_stocks()
    print(stocks.head())
    print("SW 列:", [c for c in stocks.columns if c.startswith("sw_")])
