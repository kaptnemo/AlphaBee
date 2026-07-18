"""Dynamic Tushare query tool — lets agents call any Tushare API by name."""

import json

from alphabee.collectors.tushare.helper import TuShareHelper
from alphabee.tools.cache import SyncTTLCache

_QUERY_CACHE = SyncTTLCache(ttl_seconds=300.0)


def query_tushare(api_name: str, params: str, max_rows: int = 50) -> str:
    """动态调用任意 Tushare 接口获取数据，供 agent 根据问题自主选择接口和参数。

    当 agent 需要从 Tushare 获取特定数据时调用，包括但不限于：
    - 行情数据：daily, weekly, monthly, pro_bar, daily_basic
    - 财务数据：income, balancesheet, cashflow, fina_indicator, forecast, express
    - 资金流向：moneyflow, moneyflow_hsgt, hsgt_top10, top_list
    - 板块/指数：index_basic, index_daily, sw_daily, ths_index, ths_member, index_classify
    - 基础信息：stock_basic, stock_company, trade_cal
    - 公告/新闻：anns_d, news, major_news, research_report
    - 宏观数据：cn_cpi, cn_ppi, cn_pmi, cn_gdp, cn_m, sf_month, shibor, shibor_lpr

    股票代码须为 Tushare 标准格式，如 "600519.SH"（沪市）、"300750.SZ"（深市）。

    Args:
        api_name: Tushare 接口名称，如 'daily'、'income'、'fina_indicator' 等
        params:   JSON 格式的接口参数字符串，例如：
                  '{"ts_code": "600519.SH", "start_date": "20240101", "end_date": "20241231"}'
                  '{"ts_code": "300750.SZ", "start_date": "20230101"}'
                  日期格式统一为 YYYYMMDD，如 '20240101'
        max_rows: 返回数据最大行数，默认 50，最大 200

    Returns:
        Markdown 格式的数据表格，包含接口名、参数摘要和数据内容。
        若接口返回空数据，将说明可能的原因（非交易日、未上市、权限不足等）。
    """
    max_rows = max(1, min(max_rows, 200))

    try:
        params_dict = json.loads(params)
    except json.JSONDecodeError as e:
        return (
            f"❌ 参数解析失败：{e}\n"
            "请确保 params 是合法的 JSON 字符串，"
            '例如：\'{"ts_code": "600519.SH", "start_date": "20240101"}\''
        )

    # Normalize params for cache key to avoid duplicate queries
    normalized_params = json.dumps(params_dict, sort_keys=True, ensure_ascii=False)
    cache_key = ("query_tushare", api_name, normalized_params, max_rows)

    def _compute() -> str:
        try:
            with TuShareHelper() as helper:
                api_fn = getattr(helper, api_name, None)
                if api_fn is None:
                    return f"❌ 未找到 Tushare 接口：`{api_name}`"
                result = api_fn(**params_dict)
                df = result.data
        except Exception as e:
            return f"❌ 接口 `{api_name}` 调用失败：{e}"

        if df is None or df.empty:
            return (
                f"接口 `{api_name}` 返回空数据\n"
                f"参数：`{normalized_params}`\n"
                "可能原因：非交易日、标的未上市、参数错误或积分/权限不足。"
            )

        total_rows = len(df)
        df_display = df.head(max_rows)

        lines = [
            f"**接口**: `{api_name}` | **共 {total_rows} 行**（显示前 {len(df_display)} 行）",
            f"**参数**: `{normalized_params}`",
            "",
            df_display.to_markdown(index=False),
        ]
        if total_rows > max_rows:
            lines.append(
                f"\n> 共 {total_rows} 行，只显示前 {max_rows} 行。如需更多数据，请缩小时间范围或增大 max_rows。"
            )

        return "\n".join(lines)

    return _QUERY_CACHE.get_or_compute(cache_key, _compute)
