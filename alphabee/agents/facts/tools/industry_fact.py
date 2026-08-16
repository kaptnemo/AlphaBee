"""IndustryFact tool — 行业分类、行业指数行情与申万行业估值。"""

from typing import Any

from alphabee.agents.facts.tools._utils import normalize_ts_code, safe_float, safe_str
from alphabee.collectors.tushare.helper import TuShareHelper
from alphabee.industry.classification import _SW_LEVELS, match_sw_industry
from alphabee.providers.industry import get_industry_daily
from alphabee.tools.cache import SyncTTLCache

_CACHE = SyncTTLCache(ttl_seconds=600.0)


def get_industry_fact(symbol: str) -> dict[str, Any]:
    """获取A股公司所属行业的分类信息与行业整体行情，包括申万行业指数表现和近期估值水平。

    适用场景：
    - 确认公司所属行业分类（申万一级/二级/三级行业）
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
        with TuShareHelper() as helper:
            basic_df = helper.stock_basic(
                ts_code=ts_code,
                fields="ts_code,name,industry,sector",
            ).data
            frames: dict[str, Any] = {}
            for level in _SW_LEVELS:
                try:
                    df = helper.index_classify(level=level, src="SW2021").data
                    if df is not None and not df.empty:
                        frames[level] = df
                except Exception:
                    continue  # 单层分类失败不影响其他层

        industry = ""
        sector = ""
        if not basic_df.empty:
            r = basic_df.iloc[0]
            industry = safe_str(r.get("industry"))
            sector = safe_str(r.get("sector"))

        # Find matching SW index（L2/L3 优先——子行业成分股更同质；L1 兜底）
        sw_code, sw_level = match_sw_industry(industry, frames)
        l1_frame = frames.get("L1")
        sw_classes = l1_frame.head(20).to_dict(orient="records") if l1_frame is not None else []

        # Delegate to provider for industry daily data with fallback
        if sw_code:
            result = get_industry_daily(sw_code=sw_code, industry=industry)
            sw_daily = result.daily
            sw_daily_error = result.error
        else:
            sw_daily = []
            sw_daily_error = "SW指数代码匹配失败"

        return {
            "stock_code": ts_code,
            "industry": industry,
            "sector": sector,
            "sw_classes": sw_classes,
            "sw_code": sw_code,
            "sw_level": sw_level,  # L1 / L2 / L3（消费方据此定 classification_standard）
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
