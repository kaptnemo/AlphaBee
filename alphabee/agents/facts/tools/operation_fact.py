"""OperationFact tool — 主营业务构成与经营分析数据。"""

import datetime
from typing import Any

from alphabee.agents.facts.tools._utils import normalize_ts_code, safe_float, safe_str
from alphabee.collectors.tushare.helper import TuShareHelper
from alphabee.tools.cache import SyncTTLCache

_CACHE = SyncTTLCache(ttl_seconds=3600.0)


def get_operation_fact(symbol: str) -> dict[str, Any]:
    """获取A股公司的主营业务构成数据，按产品类型和地区分解营收来源。

    适用场景：
    - 了解公司核心收入来源（哪类产品/业务贡献最多营收）
    - 分析各业务线的毛利率差异
    - 了解公司收入的地理分布（国内/海外占比）
    - 评估公司业务多元化程度与核心竞争力
    - 跟踪主营业务结构的历史变化趋势

    Args:
        symbol: 股票代码，支持多种格式，如 "600519"、"600519.SH"

    Returns:
        包含主营业务构成数据的字典，所有字段使用 AlphaBee 标准命名。
    """
    ts_code = normalize_ts_code(symbol)

    def _compute() -> dict[str, Any]:
        start = (datetime.date.today() - datetime.timedelta(days=730)).strftime("%Y%m%d")

        with TuShareHelper() as helper:
            bz_df = helper.fina_mainbz(
                ts_code=ts_code,
                start_date=start,
                fields="ts_code,end_date,bz_item,bz_sales,bz_profit,bz_cost,curr_type,update_flag",
            ).data
            basic_df = helper.stock_basic(ts_code=ts_code, fields="ts_code,name").data

        company_name = basic_df.iloc[0]["company_name"] if not basic_df.empty else ts_code

        if bz_df.empty:
            return {
                "stock_code": ts_code,
                "company_name": company_name,
                "latest_period": None,
                "latest_items": [],
                "period_totals": [],
            }

        latest_period = bz_df["period"].max()
        latest_items = bz_df[bz_df["period"] == latest_period].to_dict(orient="records")

        all_periods = sorted(bz_df["period"].unique(), reverse=True)
        period_totals = []
        for p in all_periods[:8]:
            total = bz_df[bz_df["period"] == p]["biz_segment_revenue"].apply(safe_float).sum()
            period_totals.append({"period": p, "biz_total_revenue": total})

        return {
            "stock_code": ts_code,
            "company_name": company_name,
            "latest_period": latest_period,
            "latest_items": latest_items,
            "period_totals": period_totals,
        }

    return _CACHE.get_or_compute(("operation_fact", ts_code), _compute)


def render(data: dict[str, Any]) -> str:
    """将主营业务构成数据渲染为Markdown格式的文本。"""
    stock_code = data.get("stock_code", "")
    company_name = data.get("company_name", stock_code)
    latest_period = data.get("latest_period")
    latest_items = data.get("latest_items", [])
    period_totals = data.get("period_totals", [])

    if not latest_items:
        return f"## {stock_code} 主营业务构成\n\n_暂无主营业务构成数据（fina_mainbz 接口未返回数据）_\n"

    lines = [f"## {stock_code}（{company_name}）主营业务构成\n"]

    lines += [
        f"### 最新报告期：{latest_period}\n",
        "| 业务项目 | 营业收入(亿元) | 营业成本(亿元) | 营业利润(亿元) | 毛利率(%) |",
        "|---------|--------------|--------------|--------------|---------|",
    ]

    total_revenue = 0.0
    for row in latest_items:
        segment = safe_str(row.get("biz_segment_name"), "其他")
        revenue = safe_float(row.get("biz_segment_revenue"))
        cost = safe_float(row.get("biz_segment_cost"))
        profit = safe_float(row.get("biz_segment_profit"))
        gross_margin = (profit / revenue * 100) if revenue > 0 else 0.0
        total_revenue += revenue
        lines.append(
            f"| {segment} | {revenue / 1e8:.2f} | {cost / 1e8:.2f} | {profit / 1e8:.2f} | {gross_margin:.2f} |"
        )

    if total_revenue > 0:
        lines.append(f"| **合计** | **{total_revenue / 1e8:.2f}** | - | - | - |")
    lines.append("")

    if len(period_totals) > 1:
        lines += [
            "### 各期营收合计趋势（亿元）",
            "| 报告期 | 营业收入合计 |",
            "|--------|------------|",
        ]
        for entry in period_totals:
            lines.append(f"| {entry['period']} | {entry['biz_total_revenue'] / 1e8:.2f} |")
        lines.append("")

    return "\n".join(lines)
