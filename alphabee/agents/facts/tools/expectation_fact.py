"""ExpectationFact tool — 业绩预告、业绩快报与分析师一致预期。"""

import datetime
from typing import Any

from alphabee.collectors.tushare.helper import TuShareHelper
from alphabee.tools.cache import SyncTTLCache
from alphabee.agents.facts.tools._utils import normalize_ts_code, safe_float, safe_str

_CACHE = SyncTTLCache(ttl_seconds=1800.0)


def get_expectation_fact(symbol: str) -> dict[str, Any]:
    """获取A股公司的业绩预告、业绩快报及市场预期数据，反映公司未来业绩走向的早期信号。

    适用场景：
    - 查看公司最新业绩预告（增减幅预期、净利润上下限预测）
    - 获取业绩快报（正式财报前的预估数据）
    - 了解公司是否存在业绩预喜或业绩雷暴风险
    - 判断分析师对公司盈利预期的方向
    - 作为投资决策中的前瞻性参考依据

    Args:
        symbol: 股票代码，支持多种格式，如 "600519"、"600519.SH"

    Returns:
        包含业绩预期数据的字典，所有字段使用 AlphaBee 标准命名。
    """
    ts_code = normalize_ts_code(symbol)

    def _compute() -> dict[str, Any]:
        start = (datetime.date.today() - datetime.timedelta(days=730)).strftime("%Y%m%d")

        result: dict[str, Any] = {"stock_code": ts_code}

        # 1. 业绩预告
        try:
            with TuShareHelper() as helper:
                forecast_df = helper.forecast(
                    ts_code=ts_code,
                    start_date=start,
                    fields="ts_code,ann_date,end_date,type,p_change_min,p_change_max,"
                           "net_profit_min,net_profit_max,last_parent_net,summary,change_reason",
                ).data
            result["forecast"] = forecast_df.head(8).to_dict(orient="records") if not forecast_df.empty else []
            result["forecast_error"] = None
        except Exception as e:
            result["forecast"] = []
            result["forecast_error"] = str(e)

        # 2. 业绩快报
        try:
            with TuShareHelper() as helper:
                express_df = helper.express(
                    ts_code=ts_code,
                    start_date=start,
                    fields="ts_code,ann_date,end_date,revenue,operate_profit,total_profit,"
                           "n_income,total_assets,total_hldr_eqy_exc_min_int,"
                           "diluted_eps,diluted_roe,or_last_year,op_last_year,"
                           "tp_last_year,np_last_year,eps_last_year,open_net_assets,"
                           "bps_last_year,yoy_sales,yoy_op,yoy_tp,yoy_dedu_np,"
                           "yoy_eps,yoy_roe,growth_assets,yoy_equity,growth_bps,perf_summary",
                ).data
            result["express"] = express_df.head(8).to_dict(orient="records") if not express_df.empty else []
            result["express_error"] = None
        except Exception as e:
            result["express"] = []
            result["express_error"] = str(e)

        return result

    return _CACHE.get_or_compute(("expectation_fact", ts_code), _compute)


def render(data: dict[str, Any]) -> str:
    """将业绩预期数据渲染为Markdown格式的文本。"""
    stock_code = data.get("stock_code", "")
    forecast = data.get("forecast", [])
    forecast_error = data.get("forecast_error")
    express = data.get("express", [])
    express_error = data.get("express_error")

    lines = [f"## {stock_code} 业绩预期事实数据\n"]

    # 1. 业绩预告
    if forecast_error:
        lines.append(f"_业绩预告数据获取失败：{forecast_error}_\n")
    elif forecast:
        lines += [
            "### 业绩预告（forecast）",
            "| 公告日 | 报告期 | 预告类型 | 净利润下限(亿元) | 净利润上限(亿元) | 同比变动下限(%) | 同比变动上限(%) |",
            "|--------|--------|---------|----------------|----------------|--------------|--------------|",
        ]
        for row in forecast:
            ann_date = safe_str(row.get("ann_date"))
            period = safe_str(row.get("period"))
            f_type = safe_str(row.get("forecast_type"))
            p_min = safe_float(row.get("profit_forecast_min_change"))
            p_max = safe_float(row.get("profit_forecast_max_change"))
            np_min = safe_float(row.get("forecast_net_profit_min")) / 1e8
            np_max = safe_float(row.get("forecast_net_profit_max")) / 1e8
            lines.append(
                f"| {ann_date} | {period} | {f_type} "
                f"| {np_min:.2f} | {np_max:.2f} "
                f"| {p_min:.2f} | {p_max:.2f} |"
            )

        lines += ["", "**预告摘要：**"]
        for row in forecast[:4]:
            summary = safe_str(row.get("forecast_summary"))
            reason = safe_str(row.get("change_reason"))
            period = safe_str(row.get("period"))
            if summary:
                lines.append(f"- [{period}] {summary}")
            elif reason:
                lines.append(f"- [{period}] {reason}")
        lines.append("")
    else:
        lines.append("_近两年无业绩预告数据_\n")

    # 2. 业绩快报
    if express_error:
        lines.append(f"_业绩快报数据获取失败：{express_error}_\n")
    elif express:
        lines += [
            "### 业绩快报（express）",
            "| 公告日 | 报告期 | 营收(亿元) | 归母净利润(亿元) | 营收同比(%) | 净利润同比(%) | EPS(元) | ROE(%) |",
            "|--------|--------|-----------|----------------|-----------|-------------|--------|-------|",
        ]
        for row in express:
            ann_date = safe_str(row.get("ann_date"))
            period = safe_str(row.get("period"))
            revenue = safe_float(row.get("express_revenue")) / 1e8
            n_income = safe_float(row.get("express_net_profit")) / 1e8
            yoy_sales = safe_float(row.get("express_revenue_yoy"))
            yoy_np = safe_float(row.get("express_net_profit_yoy"))
            eps = safe_float(row.get("express_diluted_eps"))
            roe = safe_float(row.get("express_diluted_roe"))
            lines.append(
                f"| {ann_date} | {period} "
                f"| {revenue:.2f} | {n_income:.2f} "
                f"| {yoy_sales:.2f} | {yoy_np:.2f} "
                f"| {eps:.4f} | {roe:.2f} |"
            )

        lines += ["", "**快报业绩摘要：**"]
        for row in express[:4]:
            perf = safe_str(row.get("express_perf_summary"))
            period = safe_str(row.get("period"))
            if perf:
                lines.append(f"- [{period}] {perf}")
        lines.append("")
    else:
        lines.append("_近两年无业绩快报数据_\n")

    return "\n".join(lines)


