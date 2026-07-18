"""CompanyProfile tool — 公司基本信息与股东结构。"""

from typing import Any

from pandas import DataFrame

from alphabee.agents.facts.tools._utils import normalize_ts_code, safe_float, safe_str
from alphabee.collectors.tushare.helper import TuShareHelper
from alphabee.tools.cache import SyncTTLCache

_CACHE = SyncTTLCache(ttl_seconds=3600.0)


def get_company_profile(symbol: str) -> dict[str, Any]:
    """获取A股公司的基本档案信息，包括公司概况、注册资本、管理层、员工规模、主营业务简介及前十大股东。

    适用场景：
    - 了解公司基本情况（成立时间、上市日期、注册地、行业归属）
    - 查询公司官网、联系方式、法人代表、董事长
    - 了解公司员工规模、注册资本、公司性质
    - 查看前十大股东持股情况及股权集中度

    Args:
        symbol: 股票代码，支持多种格式，如 "600519"、"600519.SH"、"sh600519"

    Returns:
        包含公司档案数据的字典，字段使用 AlphaBee 标准命名。
    """
    ts_code = normalize_ts_code(symbol)

    def _compute() -> dict[str, Any]:
        with TuShareHelper() as helper:
            basic_df = helper.stock_basic(
                ts_code=ts_code,
                fields="ts_code,name,area,industry,market,list_date,exchange,curr_type,list_status",
            ).data
            company_df = helper.stock_company(
                ts_code=ts_code,
                fields="ts_code,chairman,manager,secretary,reg_capital,province,city,"
                "introduction,website,email,tel,employees,main_business,business_scope",
            ).data
            holders_df = helper.top10_holders(
                ts_code=ts_code, fields="ts_code,ann_date,end_date,holder_name,hold_amount,hold_ratio"
            ).data

        return {
            "basic": basic_df.to_dict(orient="dict"),
            "company": company_df.to_dict(orient="dict"),
            "holders": holders_df.to_dict(orient="records"),
        }

    return _CACHE.get_or_compute(("company_profile", ts_code), _compute)


def render(data: dict[str, Any]) -> str:
    """将公司档案数据渲染为Markdown格式的文本。"""
    basic_df = DataFrame(data.get("basic", {}))
    company_df = DataFrame(data.get("company", {}))
    holders_df = DataFrame(data.get("holders", []))

    stock_code = basic_df["stock_code"].iloc[0] if not basic_df.empty else "未知代码"
    lines = [f"## {stock_code} 公司档案\n"]

    if not basic_df.empty:
        r = basic_df.iloc[0]
        lines += [
            "### 基本信息",
            "| 项目 | 内容 |",
            "|------|------|",
            f"| 股票代码 | {safe_str(r.get('stock_code'))} |",
            f"| 公司名称 | {safe_str(r.get('company_name'))} |",
            f"| 所属行业 | {safe_str(r.get('industry'))} |",
            f"| 市场板块 | {safe_str(r.get('market'))} |",
            f"| 上市交易所 | {safe_str(r.get('exchange'))} |",
            f"| 地区 | {safe_str(r.get('area'))} |",
            f"| 上市日期 | {safe_str(r.get('list_date'))} |",
            "",
        ]

    if not company_df.empty:
        c = company_df.iloc[0]
        lines += [
            "### 公司详情",
            "| 项目 | 内容 |",
            "|------|------|",
            f"| 董事长 | {safe_str(c.get('chairman'))} |",
            f"| 总经理 | {safe_str(c.get('manager'))} |",
            f"| 注册资本(万元) | {safe_str(c.get('registered_capital'))} |",
            f"| 员工人数 | {safe_str(c.get('employees'))} |",
            f"| 省份/城市 | {safe_str(c.get('province'))}/{safe_str(c.get('city'))} |",
            f"| 官方网站 | {safe_str(c.get('website'))} |",
            f"| 联系电话 | {safe_str(c.get('tel'))} |",
            "",
        ]
        intro = safe_str(c.get("introduction"))
        if intro:
            lines += ["### 公司简介", intro, ""]
        main_biz = safe_str(c.get("main_business"))
        if main_biz:
            lines += ["### 主营业务", main_biz, ""]

    if not holders_df.empty:
        latest_period = holders_df["period"].max()
        top = holders_df[holders_df["period"] == latest_period].head(10)
        lines += [
            f"### 前十大股东（报告期：{latest_period}）",
            "| 股东名称 | 持股数量（万股） | 持股比例（%） |",
            "|----------|-----------------|--------------|",
        ]
        for _, row in top.iterrows():
            name = safe_str(row.get("top10_holder_name"))
            amount = safe_float(row.get("top10_holder_amount")) / 10000
            ratio = safe_float(row.get("top10_holder_ratio"))
            lines.append(f"| {name} | {amount:,.2f} | {ratio:.2f} |")
        lines.append("")
    else:
        lines.append("_前十大股东数据暂不可用_\n")

    return "\n".join(lines)
