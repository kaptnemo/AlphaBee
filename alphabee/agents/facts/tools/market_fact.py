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
        lookback_5y = (datetime.date.today() - datetime.timedelta(days=5*365)).strftime("%Y%m%d")
        lookback_10 = (datetime.date.today() - datetime.timedelta(days=10)).strftime("%Y%m%d")

        with TuShareHelper() as helper:
            daily_df = helper.daily(
                ts_code=ts_code, start_date=lookback_10, end_date=today
            ).data
            daily_basic_df = helper.daily_basic(
                ts_code=ts_code, start_date=lookback_10, end_date=today
            ).data
            # 5年历史 PE/PB，用于计算 pe_ttm_5y_avg
            daily_basic_history_df = helper.daily_basic(
                ts_code=ts_code, start_date=lookback_5y, end_date=today,
                fields="ts_code,trade_date,pe_ttm,pb",
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
            "daily_basic_history": (
                daily_basic_history_df.to_dict(orient="records")
                if not daily_basic_history_df.empty else []
            ),
        }

    return _CACHE.get_or_compute(("market_fact", ts_code), _compute)


# ═══════════════════════════════════════════════════════════════════════════
# Canonical 字段提取层
# 从 get_market_fact() 返回的行情数据中提取单值 canonical 字段，
# 供 DerivedFacts Engine / Signal Engine 使用。
# ═══════════════════════════════════════════════════════════════════════════


def extract_market_facts(
    data: dict,
    fields: list[str] | None = None,
) -> dict[str, float]:
    """从 get_market_fact() 结果中提取 canonical 行情字段值。

    Args:
        data: get_market_fact() 的返回结果。
        fields: 需要提取的字段列表；为 None 时提取全部可用字段。

    Returns:
        ``{canonical_field_name: float_value}``，缺失字段不出现在结果中。
    """
    all_fields = {"pe_ttm", "pb_ratio", "pe_ttm_5y_avg"}
    target = set(fields) if fields is not None else all_fields

    result: dict[str, float] = {}

    if "pe_ttm" in target or "pb_ratio" in target:
        db = data.get("latest_daily_basic")
        if db is not None:
            if "pe_ttm" in target:
                v = safe_float(db.get("pe_ttm"))
                if v is not None:
                    result["pe_ttm"] = v
            if "pb_ratio" in target:
                v = safe_float(db.get("pb_ratio"))
                if v is not None:
                    result["pb_ratio"] = v

    if "pe_ttm_5y_avg" in target:
        val = _extract_pe_ttm_5y_avg(data)
        if val is not None:
            result["pe_ttm_5y_avg"] = val

    return result


def _extract_pe_ttm_5y_avg(data: dict) -> float | None:
    """从 daily_basic_history 中计算近 5 年 PE(TTM) 均值。

    对历史日频 PE 取平均，近似作为 5 年估值中枢参考。
    """
    history = data.get("daily_basic_history", [])
    if not history:
        return None
    pe_values = []
    for row in history:
        v = safe_float(row.get("pe_ttm"))
        if v is not None and v > 0:
            pe_values.append(v)
    if not pe_values:
        return None
    return sum(pe_values) / len(pe_values)


def get_market_facts_model(symbol: str) -> "MarketFacts":
    """获取 A 股行情数据并返回 MarketFacts Pydantic 模型。

    封装 get_market_fact() 的返回值，将行情字典映射到类型化的
    MarketFacts / MoneyFlow 模型，供 DerivedFacts 引擎直接消费。

    Args:
        symbol: 股票代码，支持多种格式，如 "600519"、"600519.SH"

    Returns:
        MarketFacts 模型，含报价、估值、资金流向和均线数据。
    """
    from alphabee.agents.facts.models import MarketFacts, MoneyFlow

    data = get_market_fact(symbol)
    ts_code = data.get("stock_code", normalize_ts_code(symbol))
    company_name = data.get("company_name", ts_code)

    d = data.get("latest_daily") or {}
    db = data.get("latest_daily_basic") or {}
    mf_raw = data.get("latest_moneyflow") or {}
    ma = data.get("ma", {})

    moneyflow: MoneyFlow | None = None
    if mf_raw:
        moneyflow = MoneyFlow(
            trade_date=safe_str(mf_raw.get("trade_date")),
            super_large_order_flow=safe_float(mf_raw.get("super_large_order_flow")),
            large_order_flow=safe_float(mf_raw.get("large_order_flow")),
            medium_order_flow=safe_float(mf_raw.get("medium_order_flow")),
            retail_flow=safe_float(mf_raw.get("retail_flow")),
            main_force_inflow=safe_float(mf_raw.get("main_force_inflow")),
        )

    return MarketFacts(
        stock_code=ts_code,
        company_name=company_name,
        trade_date=safe_str(d.get("trade_date")),
        close_price=safe_float(d.get("close_price")),
        open_price=safe_float(d.get("open_price")),
        high_price=safe_float(d.get("high_price")),
        low_price=safe_float(d.get("low_price")),
        prev_close_price=safe_float(d.get("prev_close_price")),
        price_change=safe_float(d.get("price_change")),
        price_change_pct=safe_float(d.get("price_change_pct")),
        volume=safe_float(d.get("volume")),
        turnover_amount=safe_float(d.get("turnover_amount")),
        pe_ttm=safe_float(db.get("pe_ttm")),
        pb_ratio=safe_float(db.get("pb_ratio")),
        market_cap=safe_float(db.get("market_cap")),
        circulating_market_cap=safe_float(db.get("circulating_market_cap")),
        turnover_rate=safe_float(db.get("turnover_rate")),
        pe_ttm_5y_avg=_extract_pe_ttm_5y_avg(data),
        moneyflow=moneyflow,
        ma5=ma.get("ma5"),
        ma10=ma.get("ma10"),
        ma20=ma.get("ma20"),
        ma60=ma.get("ma60"),
        ma120=ma.get("ma120"),
    )


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
