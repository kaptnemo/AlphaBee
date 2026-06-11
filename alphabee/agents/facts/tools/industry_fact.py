"""IndustryFact tool — 行业分类、行业指数行情与申万行业估值。"""

import datetime
from typing import Any

from alphabee.collectors.tushare.helper import TuShareHelper
from alphabee.tools.cache import SyncTTLCache
from alphabee.agents.facts.tools._utils import normalize_ts_code, safe_float, safe_str

_CACHE = SyncTTLCache(ttl_seconds=600.0)


def get_industry_fact(symbol: str) -> dict[str, Any]:
    """获取A股公司所属行业的分类信息与行业整体行情，包括申万行业指数表现和近期估值水平。

    适用场景：
    - 确认公司所属行业分类（申万一级/二级行业）
    - 了解所属行业近期整体涨跌情况
    - 查看行业PE/PB历史估值水平
    - 评估个股相对行业的位置

    Args:
        symbol: 股票代码，支持多种格式，如 "600519"、"600519.SH"

    Returns:
        包含行业信息的字典，所有字段使用 AlphaBee 标准命名。
    """
    ts_code = normalize_ts_code(symbol)

    def _compute() -> dict[str, Any]:
        today = datetime.date.today().strftime("%Y%m%d")
        lookback_90 = (datetime.date.today() - datetime.timedelta(days=90)).strftime("%Y%m%d")

        with TuShareHelper() as helper:
            basic_df = helper.stock_basic(
                ts_code=ts_code,
                fields="ts_code,name,industry,sector",
            ).data
            sw_class_df = helper.index_classify(
                level="L1", src="SW2021"
            ).data

        industry = ""
        sector = ""
        if not basic_df.empty:
            r = basic_df.iloc[0]
            industry = safe_str(r.get("industry"))
            sector = safe_str(r.get("sector"))

        # Find matching SW index (adapt() already renamed index_code → sw_code)
        matched_sw_code: str | None = None
        if industry and not sw_class_df.empty:
            name_col = next(
                (col for col in ["industry_name", "name", "index_name"] if col in sw_class_df.columns),
                None,
            )
            if name_col:
                matched_rows = sw_class_df[
                    sw_class_df[name_col].str.contains(industry[:2], na=False)
                ]
                if not matched_rows.empty:
                    matched_sw_code = safe_str(matched_rows.iloc[0].get("sw_code"))

        sw_classes = sw_class_df.head(20).to_dict(orient="records") if not sw_class_df.empty else []

        sw_daily: list[dict] = []
        sw_daily_error: str | None = None

        if matched_sw_code:
            try:
                with TuShareHelper() as helper:
                    sw_daily_df = helper.sw_daily(
                        ts_code=matched_sw_code,
                        start_date=lookback_90,
                        end_date=today,
                        fields="ts_code,trade_date,close,pct_change,pe,pb,float_mv",
                    ).data
                sw_daily = sw_daily_df.head(10).to_dict(orient="records") if not sw_daily_df.empty else []
            except Exception as e:
                sw_daily_error = str(e)

        return {
            "stock_code": ts_code,
            "industry": industry,
            "sector": sector,
            "sw_classes": sw_classes,
            "sw_code": matched_sw_code,
            "sw_daily": sw_daily,
            "sw_daily_error": sw_daily_error,
        }

    return _CACHE.get_or_compute(("industry_fact", ts_code), _compute)


def render(data: dict[str, Any]) -> str:
    """将行业事实数据渲染为Markdown格式的文本。"""
    stock_code = data.get("stock_code", "")
    industry = data.get("industry", "")
    sector = data.get("sector", "")
    sw_classes = data.get("sw_classes", [])
    sw_code = data.get("sw_code")
    sw_daily = data.get("sw_daily", [])
    sw_daily_error = data.get("sw_daily_error")

    lines = [f"## {stock_code} 行业事实数据\n"]

    if industry or sector:
        lines += [
            "### 行业归属",
            f"- **所属行业（stock_basic）**: {industry}",
            f"- **板块**: {sector}",
            "",
        ]

    if sw_classes:
        lines += [
            "### 申万一级行业列表（前20个）",
            "| 行业代码 | 行业名称 |",
            "|---------|---------|",
        ]
        for row in sw_classes:
            idx_code = safe_str(row.get("sw_code"))
            idx_name = safe_str(row.get("industry_name", ""))
            lines.append(f"| {idx_code} | {idx_name} |")
        lines.append("")

    if sw_daily_error:
        lines.append("_申万行业指数行情获取失败_\n")
    elif sw_daily and sw_code:
        lines += [
            f"### 申万行业指数行情（{sw_code}，近期）",
            "| 交易日 | 收盘价 | 涨跌幅(%) | PE(TTM) | PB |",
            "|--------|--------|---------|--------|---|",
        ]
        for row in sw_daily:
            lines.append(
                f"| {safe_str(row.get('trade_date'))} "
                f"| {safe_float(row.get('industry_close')):.2f} "
                f"| {safe_float(row.get('industry_change_pct')):.2f} "
                f"| {safe_float(row.get('industry_pe_ttm')):.2f} "
                f"| {safe_float(row.get('industry_pb')):.2f} |"
            )
        lines.append("")

    return "\n".join(lines)


