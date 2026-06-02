"""MarketFact tool — 股票行情、资金流向与技术均线数据。"""

import datetime
from typing import Any

from alphabee.collectors.tushare.helper import TuShareHelper
from alphabee.tools.cache import SyncTTLCache
from alphabee.agents.facts.tools._utils import normalize_ts_code, safe_float, safe_str

_CACHE = SyncTTLCache(ttl_seconds=300.0)


def get_market_fact(symbol: str) -> dict[str, Any]:
    """获取A股股票最新行情数据，包括价格、成交量、换手率、估值指标和资金流向，以及近期均线技术数据。

    适用场景：
    - 查询股票最新收盘价、涨跌幅、成交量
    - 了解当前市盈率（PE）、市净率（PB）和总市值
    - 分析主力资金、大单资金和散户资金的净流入情况
    - 查看5/10/20/60/120日均线价格位置
    - 掌握近期换手率和振幅，判断交投活跃度

    Args:
        symbol: 股票代码，支持多种格式，如 "600519"、"600519.SH"

    Returns:
        包含行情、估值、资金流向和均线数据的字典，所有字段使用 AlphaBee 标准命名。
    """
    ts_code = normalize_ts_code(symbol)

    def _compute() -> dict[str, Any]:
        today = datetime.date.today().strftime("%Y%m%d")
        lookback = (datetime.date.today() - datetime.timedelta(days=180)).strftime("%Y%m%d")
        lookback_10 = (datetime.date.today() - datetime.timedelta(days=10)).strftime("%Y%m%d")

        with TuShareHelper() as helper:
            daily_df = helper.daily(
                ts_code=ts_code, start_date=lookback_10, end_date=today
            ).data
            daily_basic_df = helper.daily_basic(
                ts_code=ts_code, start_date=lookback_10, end_date=today
            ).data
            moneyflow_df = helper.moneyflow(
                ts_code=ts_code, start_date=lookback_10, end_date=today
            ).data
            hist_df = helper.daily(
                ts_code=ts_code, start_date=lookback, end_date=today,
                fields="ts_code,trade_date,close"
            ).data
            stock_basic_df = helper.stock_basic(ts_code=ts_code, fields="ts_code,name").data

        company_name = stock_basic_df.iloc[0]["company_name"] if not stock_basic_df.empty else ts_code

        # Compute moving averages using canonical field names
        ma: dict[str, float] = {}
        if not hist_df.empty and len(hist_df) >= 5:
            close_series = hist_df["close_price"].apply(safe_float)
            for window, label in [(5, "ma5"), (10, "ma10"), (20, "ma20"), (60, "ma60"), (120, "ma120")]:
                if len(close_series) >= window:
                    ma[label] = float(close_series.iloc[:window].mean())

        # Compute canonical moneyflow net-flow fields (adapter layer computation)
        latest_moneyflow: dict[str, Any] | None = None
        if not moneyflow_df.empty:
            mf = moneyflow_df.iloc[0]
            super_large = safe_float(mf.get("_buy_super_large")) - safe_float(mf.get("_sell_super_large"))
            large = safe_float(mf.get("_buy_large")) - safe_float(mf.get("_sell_large"))
            medium = safe_float(mf.get("_buy_medium")) - safe_float(mf.get("_sell_medium"))
            small = safe_float(mf.get("_buy_retail")) - safe_float(mf.get("_sell_retail"))
            latest_moneyflow = {
                "trade_date": safe_str(mf.get("trade_date")),
                "super_large_order_flow": super_large,
                "large_order_flow": large,
                "medium_order_flow": medium,
                "retail_flow": small,
                "main_force_inflow": super_large + large,
            }

        return {
            "stock_code": ts_code,
            "company_name": company_name,
            "latest_daily": daily_df.iloc[0].to_dict() if not daily_df.empty else None,
            "latest_daily_basic": daily_basic_df.iloc[0].to_dict() if not daily_basic_df.empty else None,
            "latest_moneyflow": latest_moneyflow,
            "ma": ma,
            "history": daily_df.head(10).to_dict(orient="records"),
        }

    return _CACHE.get_or_compute(("market_fact", ts_code), _compute)


def render(data: dict[str, Any]) -> str:
    """将行情事实数据渲染为Markdown格式的文本。"""
    stock_code = data.get("stock_code", "")
    company_name = data.get("company_name", stock_code)
    d = data.get("latest_daily")
    db = data.get("latest_daily_basic")
    mf = data.get("latest_moneyflow")
    ma = data.get("ma", {})
    history = data.get("history", [])

    lines = [f"## {stock_code}（{company_name}）市场行情事实数据\n"]

    if d is None:
        lines.append("_行情数据暂不可用（可能为非交易日）_\n")
        return "\n".join(lines)

    prev_close = safe_float(d.get("prev_close_price"))
    amplitude = (
        (safe_float(d.get("high_price")) - safe_float(d.get("low_price"))) / prev_close * 100
        if prev_close else 0.0
    )

    lines += [
        f"### 最新报价（交易日：{safe_str(d.get('trade_date'))}）",
        "| 指标 | 数值 |",
        "|------|------|",
        f"| 收盘价（元） | {safe_float(d.get('close_price')):.2f} |",
        f"| 涨跌额（元） | {safe_float(d.get('price_change')):.2f} |",
        f"| 涨跌幅（%） | {safe_float(d.get('price_change_pct')):.2f} |",
        f"| 开盘价（元） | {safe_float(d.get('open_price')):.2f} |",
        f"| 最高价（元） | {safe_float(d.get('high_price')):.2f} |",
        f"| 最低价（元） | {safe_float(d.get('low_price')):.2f} |",
        f"| 昨收价（元） | {prev_close:.2f} |",
        f"| 成交量（万手） | {safe_float(d.get('volume'))/10000:.2f} |",
        f"| 成交额（亿元） | {safe_float(d.get('turnover_amount'))*1000/1e8:.2f} |",
        f"| 振幅（%） | {amplitude:.2f} |",
    ]

    if db is not None:
        lines += [
            f"| 换手率（%） | {safe_float(db.get('turnover_rate')):.2f} |",
            f"| 市盈率PE(TTM) | {safe_float(db.get('pe_ttm')):.2f} |",
            f"| 市净率PB | {safe_float(db.get('pb_ratio')):.2f} |",
            f"| 总市值（亿元） | {safe_float(db.get('market_cap'))/10000:.2f} |",
            f"| 流通市值（亿元） | {safe_float(db.get('circulating_market_cap'))/10000:.2f} |",
        ]
    lines.append("")

    if mf is not None:
        lines += [
            "### 资金流向（当日，单位：万元）",
            "| 资金类型 | 净流入 |",
            "|---------|-------|",
            f"| 主力（超大单+大单） | {safe_float(mf.get('main_force_inflow')):,.2f} |",
            f"| 超大单 | {safe_float(mf.get('super_large_order_flow')):,.2f} |",
            f"| 大单 | {safe_float(mf.get('large_order_flow')):,.2f} |",
            f"| 中单 | {safe_float(mf.get('medium_order_flow')):,.2f} |",
            f"| 小单（散户） | {safe_float(mf.get('retail_flow')):,.2f} |",
            "",
        ]

    if ma:
        lines += ["### 均线技术数据（元）", "| 均线 | 价格 |", "|------|------|"]
        ma_labels = {"ma5": "MA5", "ma10": "MA10", "ma20": "MA20", "ma60": "MA60", "ma120": "MA120"}
        for key, label in ma_labels.items():
            if key in ma:
                lines.append(f"| {label} | {ma[key]:.2f} |")
        lines.append("")

    lines += [
        "### 近期行情（最近10个交易日）",
        "| 日期 | 收盘价 | 涨跌幅(%) | 成交量(万手) |",
        "|------|--------|---------|-----------|",
    ]
    for row in history:
        lines.append(
            f"| {safe_str(row.get('trade_date'))} "
            f"| {safe_float(row.get('close_price')):.2f} "
            f"| {safe_float(row.get('price_change_pct')):.2f} "
            f"| {safe_float(row.get('volume'))/10000:.2f} |"
        )
    lines.append("")

    return "\n".join(lines)


def get_market_fact(symbol: str) -> dict[str, Any]:
    """获取A股股票最新行情数据，包括价格、成交量、换手率、估值指标和资金流向，以及近期均线技术数据。

    适用场景：
    - 查询股票最新收盘价、涨跌幅、成交量
    - 了解当前市盈率（PE）、市净率（PB）和总市值
    - 分析主力资金、大单资金和散户资金的净流入情况
    - 查看5/10/20/60/120日均线价格位置
    - 掌握近期换手率和振幅，判断交投活跃度

    Args:
        symbol: 股票代码，支持多种格式，如 "600519"、"600519.SH"

    Returns:
        包含行情、估值、资金流向和均线数据的字典。
    """
    ts_code = normalize_ts_code(symbol)

    def _compute() -> dict[str, Any]:
        today = datetime.date.today().strftime("%Y%m%d")
        lookback = (datetime.date.today() - datetime.timedelta(days=180)).strftime("%Y%m%d")
        lookback_10 = (datetime.date.today() - datetime.timedelta(days=10)).strftime("%Y%m%d")

        with TuShareHelper() as helper:
            daily_df = helper.daily(
                ts_code=ts_code, start_date=lookback_10, end_date=today
            ).data
            daily_basic_df = helper.daily_basic(
                ts_code=ts_code, start_date=lookback_10, end_date=today
            ).data
            moneyflow_df = helper.moneyflow(
                ts_code=ts_code, start_date=lookback_10, end_date=today
            ).data
            hist_df = helper.daily(
                ts_code=ts_code, start_date=lookback, end_date=today,
                fields="ts_code,trade_date,close"
            ).data
            stock_basic_df = helper.stock_basic(ts_code=ts_code, fields="ts_code,name").data

        name = stock_basic_df.iloc[0]["name"] if not stock_basic_df.empty else ts_code

        # Compute moving averages
        ma: dict[str, float] = {}
        if not hist_df.empty and len(hist_df) >= 5:
            close_series = hist_df["close"].apply(safe_float)
            for window, label in [(5, "MA5"), (10, "MA10"), (20, "MA20"), (60, "MA60"), (120, "MA120")]:
                if len(close_series) >= window:
                    ma[label] = float(close_series.iloc[:window].mean())

        return {
            "ts_code": ts_code,
            "name": name,
            "latest_daily": daily_df.iloc[0].to_dict() if not daily_df.empty else None,
            "latest_daily_basic": daily_basic_df.iloc[0].to_dict() if not daily_basic_df.empty else None,
            "latest_moneyflow": moneyflow_df.iloc[0].to_dict() if not moneyflow_df.empty else None,
            "ma": ma,
            "history": daily_df.head(10).to_dict(orient="records"),
        }

    return _CACHE.get_or_compute(("market_fact", ts_code), _compute)


def render(data: dict[str, Any]) -> str:
    """将行情事实数据渲染为Markdown格式的文本。"""
    ts_code = data.get("ts_code", "")
    name = data.get("name", ts_code)
    d = data.get("latest_daily")
    db = data.get("latest_daily_basic")
    mf = data.get("latest_moneyflow")
    ma = data.get("ma", {})
    history = data.get("history", [])

    lines = [f"## {ts_code}（{name}）市场行情事实数据\n"]

    if d is None:
        lines.append("_行情数据暂不可用（可能为非交易日）_\n")
        return "\n".join(lines)

    prev_close = safe_float(d.get("pre_close"))
    amplitude = (
        (safe_float(d.get("high")) - safe_float(d.get("low"))) / prev_close * 100
        if prev_close else 0.0
    )

    lines += [
        f"### 最新报价（交易日：{safe_str(d.get('trade_date'))}）",
        "| 指标 | 数值 |",
        "|------|------|",
        f"| 收盘价（元） | {safe_float(d.get('close')):.2f} |",
        f"| 涨跌额（元） | {safe_float(d.get('change')):.2f} |",
        f"| 涨跌幅（%） | {safe_float(d.get('pct_chg')):.2f} |",
        f"| 开盘价（元） | {safe_float(d.get('open')):.2f} |",
        f"| 最高价（元） | {safe_float(d.get('high')):.2f} |",
        f"| 最低价（元） | {safe_float(d.get('low')):.2f} |",
        f"| 昨收价（元） | {prev_close:.2f} |",
        f"| 成交量（万手） | {safe_float(d.get('vol'))/10000:.2f} |",
        f"| 成交额（亿元） | {safe_float(d.get('amount'))*1000/1e8:.2f} |",
        f"| 振幅（%） | {amplitude:.2f} |",
    ]

    if db is not None:
        lines += [
            f"| 换手率（%） | {safe_float(db.get('turnover_rate')):.2f} |",
            f"| 市盈率PE(TTM) | {safe_float(db.get('pe_ttm')):.2f} |",
            f"| 市净率PB | {safe_float(db.get('pb')):.2f} |",
            f"| 总市值（亿元） | {safe_float(db.get('total_mv'))/10000:.2f} |",
            f"| 流通市值（亿元） | {safe_float(db.get('circ_mv'))/10000:.2f} |",
        ]
    lines.append("")

    if mf is not None:
        super_large = safe_float(mf.get("buy_elg_amount")) - safe_float(mf.get("sell_elg_amount"))
        large = safe_float(mf.get("buy_lg_amount")) - safe_float(mf.get("sell_lg_amount"))
        medium = safe_float(mf.get("buy_md_amount")) - safe_float(mf.get("sell_md_amount"))
        small = safe_float(mf.get("buy_sm_amount")) - safe_float(mf.get("sell_sm_amount"))
        main = super_large + large
        lines += [
            "### 资金流向（当日，单位：万元）",
            "| 资金类型 | 净流入 |",
            "|---------|-------|",
            f"| 主力（超大单+大单） | {main:,.2f} |",
            f"| 超大单 | {super_large:,.2f} |",
            f"| 大单 | {large:,.2f} |",
            f"| 中单 | {medium:,.2f} |",
            f"| 小单（散户） | {small:,.2f} |",
            "",
        ]

    if ma:
        lines += ["### 均线技术数据（元）", "| 均线 | 价格 |", "|------|------|"]
        for label, val in ma.items():
            lines.append(f"| {label} | {val:.2f} |")
        lines.append("")

    lines += [
        "### 近期行情（最近10个交易日）",
        "| 日期 | 收盘价 | 涨跌幅(%) | 成交量(万手) |",
        "|------|--------|---------|-----------|",
    ]
    for row in history:
        lines.append(
            f"| {safe_str(row.get('trade_date'))} "
            f"| {safe_float(row.get('close')):.2f} "
            f"| {safe_float(row.get('pct_chg')):.2f} "
            f"| {safe_float(row.get('vol'))/10000:.2f} |"
        )
    lines.append("")

    return "\n".join(lines)
