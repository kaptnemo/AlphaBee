from datetime import datetime, timedelta

from pydantic import BaseModel, Field

from alphabee.collectors.tushare.helper import TuShareHelper
from alphabee.tools.cache import SyncTTLCache


class Quote(BaseModel):
    """实时报价与估值数据"""

    price: float = Field(description="最新收盘价（元）")
    change: float = Field(description="涨跌额（元），正数为上涨，负数为下跌")
    change_pct: float = Field(description="涨跌幅（%），正数为上涨，负数为下跌")
    open: float = Field(description="当日开盘价（元）")
    high: float = Field(description="当日最高价（元）")
    low: float = Field(description="当日最低价（元）")
    prev_close: float = Field(description="昨日收盘价（元）")
    volume: int = Field(description="成交量（手，1手=100股）")
    turnover: float = Field(description="成交额（元）")
    amplitude: float = Field(description="振幅（%），当日价格波动幅度")
    turnover_rate: float = Field(description="换手率（%），衡量股票交易活跃度")
    pe_ttm: float = Field(description="市盈率 TTM（滚动12个月），0表示亏损或暂无数据")
    pb: float = Field(description="市净率（总市值/净资产），0表示暂无数据")
    market_cap: float = Field(description="总市值（万元）")
    circulating_market_cap: float = Field(description="流通市值（万元）")
    timestamp: str = Field(description="数据日期（YYYYMMDD格式）")


class Technical(BaseModel):
    """均线技术指标"""

    ma5: float = Field(description="5日均价（元）")
    ma10: float = Field(description="10日均价（元）")
    ma20: float = Field(description="20日均价（元）")
    ma60: float = Field(description="60日均价（元）")
    ma120: float = Field(description="120日均价（元）")


class CapitalFlow(BaseModel):
    """资金流向数据（单位：万元）"""

    main_force_inflow: float = Field(
        description="主力净流入（万元）= 超大单净额 + 大单净额，正数为流入，负数为流出"
    )
    super_large_order: float = Field(
        description="超大单净流入（万元），通常代表机构或大资金"
    )
    large_order: float = Field(description="大单净流入（万元）")
    retail_flow: float = Field(
        description="散户小单净流入（万元），负数通常代表散户净卖出"
    )
    northbound_flow: float = Field(
        description="北向资金净流入（万元），即外资流向，暂不支持个股粒度，返回0"
    )
    flow_trend: str = Field(
        description="资金流向趋势描述，如'持续流入'、'流出为主'，暂无数据时为空字符串"
    )
    capital_rank_in_sector: int = Field(
        description="在同行业中的资金流入排名，暂无数据时返回0"
    )


class Sector(BaseModel):
    """行业与板块信息"""

    industry: str = Field(description="所属申万行业，如'白酒'、'新能源'、'医药生物'")
    concepts: list[str] = Field(
        description="所属概念板块列表，如['国产替代', 'AI算力']，暂无数据时为空列表"
    )
    sector_change_pct: float = Field(
        description="今日所属行业板块整体涨跌幅（%），暂无数据时返回0"
    )
    sector_rank_today: int = Field(
        description="今日涨跌幅在行业内的排名，暂无数据时返回0"
    )
    leading_stock: bool = Field(description="是否为今日板块龙头股")


class MarketData(BaseModel):
    """股票行情数据汇总"""

    symbol: str = Field(description="股票代码（Tushare格式，如 '600519.SH'）")
    name: str = Field(description="股票名称，如'贵州茅台'")
    quote: Quote = Field(description="实时报价与估值数据")
    capital_flow: CapitalFlow = Field(description="资金流向数据")
    sector: Sector = Field(description="行业与板块信息")


_MARKET_DATA_CACHE = SyncTTLCache(ttl_seconds=300.0)


def _normalize_ts_code(symbol: str) -> str:
    s = symbol.strip().lower()
    if s.startswith("sh"):
        return s[2:].upper() + ".SH"
    if s.startswith("sz"):
        return s[2:].upper() + ".SZ"
    if s.startswith("bj"):
        return s[2:].upper() + ".BJ"
    upper = symbol.strip().upper()
    if upper.endswith((".SH", ".SZ", ".BJ")):
        return upper
    if upper.startswith(("6", "9")):
        return upper + ".SH"
    if upper.startswith(("0", "3")):
        return upper + ".SZ"
    if upper.startswith(("4", "8")):
        return upper + ".BJ"
    raise ValueError(f"Cannot determine exchange for symbol: {symbol}")


def get_market_data(symbol: str) -> MarketData:
    """获取指定A股股票的最新行情数据，包括价格、资金流向和行业信息。

    当用户询问某只股票的当前价格、涨跌幅、成交量、换手率、市值、
    市盈率（PE）、市净率（PB）、主力资金流向、所属行业等行情信息时，
    调用此工具。

    Args:
        symbol: 股票代码，支持多种格式，例如：
                "600519"（纯代码）、"600519.SH"（带交易所）、"sh600519"（带前缀）

    Returns:
        MarketData，包含：
        - quote：价格、涨跌幅、成交量、PE/PB、市值等报价数据
        - capital_flow：主力/大单/散户资金净流入情况
        - sector：所属行业及板块信息
    """
    ts_code = _normalize_ts_code(symbol)

    def _compute() -> MarketData:
        today = datetime.today().strftime("%Y%m%d")
        lookback = (datetime.today() - timedelta(days=10)).strftime("%Y%m%d")

        with TuShareHelper() as helper:
            daily_df = helper.daily(
                ts_code=ts_code, start_date=lookback, end_date=today
            ).data
            daily_basic_df = helper.daily_basic(
                ts_code=ts_code, start_date=lookback, end_date=today
            ).data
            moneyflow_df = helper.moneyflow(
                ts_code=ts_code, start_date=lookback, end_date=today
            ).data
            stock_basic_df = helper.stock_basic(
                ts_code=ts_code, fields="ts_code,name,industry"
            ).data

        d = daily_df.iloc[0]
        db = daily_basic_df.iloc[0]
        mf = moneyflow_df.iloc[0] if not moneyflow_df.empty else None

        name = stock_basic_df.iloc[0]["name"] if not stock_basic_df.empty else ""
        industry = (
            stock_basic_df.iloc[0]["industry"] if not stock_basic_df.empty else ""
        )

        prev_close = float(d["pre_close"])
        amplitude = (
            (float(d["high"]) - float(d["low"])) / prev_close * 100
            if prev_close
            else 0.0
        )

        quote = Quote(
            price=float(d["close"]),
            change=float(d["change"]),
            change_pct=float(d["pct_chg"]),
            open=float(d["open"]),
            high=float(d["high"]),
            low=float(d["low"]),
            prev_close=prev_close,
            volume=int(d["vol"]),
            turnover=float(d["amount"]) * 1000,
            amplitude=amplitude,
            turnover_rate=float(db["turnover_rate"]),
            pe_ttm=float(db["pe_ttm"]) if db["pe_ttm"] is not None else 0.0,
            pb=float(db["pb"]) if db["pb"] is not None else 0.0,
            market_cap=float(db["total_mv"]),
            circulating_market_cap=float(db["circ_mv"]),
            timestamp=str(d["trade_date"]),
        )

        if mf is not None:
            super_large = float(mf["buy_elg_amount"]) - float(mf["sell_elg_amount"])
            large = float(mf["buy_lg_amount"]) - float(mf["sell_lg_amount"])
            capital_flow = CapitalFlow(
                main_force_inflow=super_large + large,
                super_large_order=super_large,
                large_order=large,
                retail_flow=float(mf["buy_sm_amount"]) - float(mf["sell_sm_amount"]),
                northbound_flow=0.0,
                flow_trend="",
                capital_rank_in_sector=0,
            )
        else:
            capital_flow = CapitalFlow(
                main_force_inflow=0.0,
                super_large_order=0.0,
                large_order=0.0,
                retail_flow=0.0,
                northbound_flow=0.0,
                flow_trend="",
                capital_rank_in_sector=0,
            )

        sector = Sector(
            industry=industry,
            concepts=[],
            sector_change_pct=0.0,
            sector_rank_today=0,
            leading_stock=False,
        )

        return MarketData(
            symbol=ts_code,
            name=name,
            quote=quote,
            capital_flow=capital_flow,
            sector=sector,
        )

    return _MARKET_DATA_CACHE.get_or_compute(("market_data", ts_code), _compute)
