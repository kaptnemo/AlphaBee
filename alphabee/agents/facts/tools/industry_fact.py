"""IndustryFact tool — 行业分类、行业指数行情与申万行业估值。"""

from typing import Any

from alphabee.agents.facts.tools._utils import normalize_ts_code, safe_float, safe_str
from alphabee.collectors.tushare.helper import TuShareHelper
from alphabee.industry.classification import _SW_LEVELS, extract_sw_member, match_sw_industry
from alphabee.providers.industry import get_industry_daily
from alphabee.tools.cache import SyncTTLCache

_CACHE = SyncTTLCache(ttl_seconds=600.0)


def _get_sw_member(ts_code: str) -> Any:
    """按个股查申万归属（index_member_all，is_new=Y）；失败返回 None 由调用方降级。

    接口契约（Tushare 官方核实）：``index_member_all(ts_code=...)`` 直接返回该股
    L1/L2/L3 代码与名称（列 ``l1_code/l1_name/.../l3_code/l3_name``），
    无 ``src`` 参数，权限需 2000 积分。
    """
    try:
        with TuShareHelper() as helper:  # type: ignore[no-untyped-call]
            return helper.index_member_all(ts_code=ts_code, is_new="Y").data
    except Exception:
        return None


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
                fields="ts_code,name,industry",
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
        if not basic_df.empty:
            r = basic_df.iloc[0]
            industry = safe_str(r.get("industry"))

        # 申万归属：优先 index_member_all 成分表精确查（权威，返回完整 L1/L2/L3 层级路径），
        # 失败再按行业名 L1/L2/L3 精确+前缀匹配兜底。
        # industry 与 sw_code 同源——成分表解析成功时用申万行业名覆盖 stock_basic 名。
        sw_path: dict[str, str] = {}
        sw_code, sw_level = None, None
        member = None
        try:
            member = extract_sw_member(_get_sw_member(ts_code))
        except Exception:
            pass
        if member:
            sw_path = {
                key: member.get(key, "") for key in ("l1_code", "l1_name", "l2_code", "l2_name", "l3_code", "l3_name")
            }
            sw_code = member.get("sw_code") or None
            sw_level = member.get("sw_level") or None
            if member.get("industry_name"):
                industry = member["industry_name"]
        if not sw_code:
            sw_code, sw_level = match_sw_industry(industry, frames)
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
            "sw_path": sw_path,
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
    sw_path = data.get("sw_path", {}) or {}
    sw_code = data.get("sw_code")
    sw_daily = data.get("sw_daily", [])
    sw_daily_error = data.get("sw_daily_error")

    lines = [f"## {stock_code} 行业事实数据\n"]

    if industry:
        source = "申万" if data.get("sw_level") else "stock_basic"
        lines += [
            "### 行业归属",
            f"- **所属行业（{source}）**: {industry}",
            "",
        ]

    if sw_path:
        lines.append("### 申万行业层级路径")
        for lvl in ("L1", "L2", "L3"):
            code = sw_path.get(f"{lvl.lower()}_code", "")
            name = sw_path.get(f"{lvl.lower()}_name", "")
            if code or name:
                lines.append(f"- **{lvl}**: {code} {name}".rstrip())
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


if __name__ == "__main__":
    # Example usage
    symbol = "600577.SH"  # 精达股份
    fact_data = get_industry_fact(symbol)
    markdown_output = render(fact_data)
    print(markdown_output)
