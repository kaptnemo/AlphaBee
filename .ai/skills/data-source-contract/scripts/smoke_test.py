#!/usr/bin/env python3
"""外部数据接口冒烟测试模板（tushare / akshare / baostock）。

用法：把要核实的接口填到 CASES 里，运行后逐项打印 columns / head / 行数，
用于在写调用代码前确认入参出参与接口可用性。详见
../SKILL.md「标准流程（冒烟测试工作流）」。
"""

from __future__ import annotations


def smoke_tushare() -> None:
    """Tushare 冒烟：token 失效/积分不足会在这里直接暴露。"""
    from alphabee.collectors.tushare.helper import TuShareHelper

    with TuShareHelper() as h:
        # ── 逐个核实：接口名、入参、出参列 ──
        # df = h.stock_basic(ts_code="600577.SH", fields="ts_code,name,industry").data
        # df = h.index_classify(level="L1", src="SW2021").data
        # df = h.index_member_all(ts_code="600577.SH", is_new="Y").data  # 无 src 参数！
        # df = h.sw_daily(ts_code="801730.SI", start_date="20260801", end_date="20260821").data
        df = h.stock_basic(list_status="L", fields="ts_code,name,industry").data
        _dump("tushare stock_basic", df)


def smoke_akshare() -> None:
    """AkShare 冒烟：免费，无需 token；注意网页源偶发失败。"""
    import akshare as ak

    # ── 逐个核实 ──
    df = ak.index_hist_sw(symbol="801120", period="day")  # 6 位代码，无 .SI
    _dump("akshare index_hist_sw(801120)", df)

    df = ak.sw_index_first_info()  # 乐咕乐股，偶发加载失败可重试
    _dump("akshare sw_index_first_info", df)

    # df = ak.index_analysis_daily_sw(symbol="一级行业", start_date="20260818", end_date="20260821")
    # _dump("akshare index_analysis_daily_sw", df)


def smoke_baostock() -> None:
    """Baostock 冒烟：需先登录；无申万行业指数行情接口。"""
    import baostock as bs

    lg = bs.login()
    if lg.error_code != "0":
        print("baostock login failed:", lg.error_msg)
        return
    try:
        rs = bs.query_stock_industry(code="sh.600577")
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        df = None
        if rows:
            import pandas as pd

            df = pd.DataFrame(rows, columns=rs.fields)
        _dump("baostock query_stock_industry(sh.600577)", df)
    finally:
        bs.logout()


def _dump(label: str, df) -> None:
    print(f"=== {label} ===")
    if df is None:
        print("(None)")
        return
    print("rows:", len(df))
    print("cols:", list(df.columns))
    if not df.empty:
        print(df.head(3).to_string(index=False))
    print()


if __name__ == "__main__":
    smoke_tushare()
    smoke_akshare()
    smoke_baostock()
