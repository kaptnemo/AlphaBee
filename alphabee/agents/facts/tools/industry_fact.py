"""IndustryFact tool — 行业分类、行业指数行情与申万行业估值。"""

import datetime
import math
from typing import Any

from alphabee.agents.facts.tools._utils import normalize_ts_code, safe_float, safe_str
from alphabee.collectors.tushare.helper import TuShareHelper
from alphabee.industry.classification import _SW_LEVELS, extract_sw_member, match_sw_industry
from alphabee.providers.industry import get_industry_daily
from alphabee.tools.cache import SyncTTLCache

_CACHE: SyncTTLCache[dict[str, Any]] = SyncTTLCache(ttl_seconds=600.0)

# 行业宽度（breadth）计算的最大成分股数：超过该上限的行业（如申万一级）逐股拉日线
# 成本过高，显式返回 None（缺失）而非近似值，避免静默回退。
_MAX_BREADTH_CONSTITUENTS = 100
# 行业宽度计算的最少有效成分股数（有足够历史算 MA 的个股数），低于该阈值返回 None。
_MIN_BREADTH_CONSTITUENTS = 5


def _opt_float(value: Any) -> float | None:
    """转 float；NaN / 无法解析返回 None（区别于 safe_float 的 0 值兜底）。"""
    try:
        v = float(value)
        return None if math.isnan(v) else v
    except (TypeError, ValueError):
        return None


def _sorted_close(df: Any, col: str) -> list[float | None]:
    """按 trade_date 升序取出收盘序列（可空 float 列表）。"""
    if df is None or getattr(df, "empty", True) or col not in df.columns:
        return []
    d = df.sort_values("trade_date") if "trade_date" in df.columns else df
    return [_opt_float(v) for v in d[col]]


def _pct_return(closes: list[float | None], window: int) -> float | None:
    """窗口期收益（%）= (最新收盘 / N 日前收盘 - 1) × 100；历史不足返回 None。"""
    if len(closes) < window + 1:
        return None
    start, end = closes[-window - 1], closes[-1]
    if start is None or end is None or start <= 0:
        return None
    return round((end / start - 1.0) * 100.0, 4)


def _excess_return(
    series: list[float | None],
    bench: list[float | None],
) -> tuple[float | None, float | None]:
    """series 相对 bench 的 20/60 日超额收益（PERCENT）；任一侧历史不足为 None。"""
    out: list[float | None] = []
    for window in (20, 60):
        r = _pct_return(series, window)
        b = _pct_return(bench, window)
        out.append(round(r - b, 4) if r is not None and b is not None else None)
    return out[0], out[1]


def _fetch_constituents(helper: TuShareHelper, sw_code: str | None, sw_level: str | None) -> list[str] | None:
    """按申万层级取当前有效成分股 ts_code 列表（index_member_all，is_new=Y）。

    成分表接口无 adapter mapping，故直接读外部列名 ``ts_code`` / ``out_date``
    （与 ``_get_sw_member`` 同口径，外部字段只出现在 fetcher 层）。
    """
    if not sw_code or sw_level not in ("L1", "L2", "L3"):
        return None
    level_col = {"L1": "l1_code", "L2": "l2_code", "L3": "l3_code"}[sw_level]
    try:
        df = helper.index_member_all(is_new="Y", **{level_col: sw_code}).data
    except Exception:
        return None
    if df is None or getattr(df, "empty", True):
        return None
    codes: list[str] = []
    for _, row in df.iterrows():
        out = row.get("out_date")
        inactive = (
            out is not None
            and not (isinstance(out, float) and out != out)
            and str(out).strip() not in ("", "nan", "None")
        )
        if inactive:
            continue
        code = row.get("ts_code")
        if code:
            codes.append(str(code))
    return codes or None


def _compute_breadths(
    helper: TuShareHelper,
    constituents: list[str],
    start_date: str,
    end_date: str,
) -> tuple[float | None, float | None]:
    """行业成分股中收盘价高于 20/60 日均线的占比（PERCENT，两窗口一次遍历）。

    成分股过多（>_MAX_BREADTH_CONSTITUENTS）或有效样本不足（<_MIN_BREADTH_CONSTITUENTS）
    时对应窗口返回 None（缺失），绝不静默回退为 0。
    """
    if not constituents or len(constituents) > _MAX_BREADTH_CONSTITUENTS:
        return None, None
    above = {20: 0, 60: 0}
    valid = {20: 0, 60: 0}
    for code in constituents:
        try:
            df = helper.daily(
                ts_code=code,
                start_date=start_date,
                end_date=end_date,
                fields="ts_code,trade_date,close",
            ).data
        except Exception:
            continue
        closes = _sorted_close(df, "close_price")
        latest = closes[-1] if closes else None
        if latest is None or latest <= 0:
            continue
        for window in (20, 60):
            if len(closes) < window + 1:
                continue
            window_closes = [v for v in closes[-window:] if v is not None]
            if len(window_closes) < window or any(v <= 0 for v in window_closes):
                continue
            ma = sum(window_closes) / len(window_closes)
            valid[window] += 1
            if latest > ma:
                above[window] += 1
    out: list[float | None] = []
    for window in (20, 60):
        if valid[window] < _MIN_BREADTH_CONSTITUENTS:
            out.append(None)
        else:
            out.append(round(above[window] / valid[window] * 100.0, 2))
    return out[0], out[1]


def _get_sw_member(ts_code: str) -> Any:
    """按个股查申万归属（index_member_all，is_new=Y）；失败返回 None 由调用方降级。

    接口契约（Tushare 官方核实）：``index_member_all(ts_code=...)`` 直接返回该股
    L1/L2/L3 代码与名称（列 ``l1_code/l1_name/.../l3_code/l3_name``），
    无 ``src`` 参数，权限需 2000 积分。
    """
    try:
        with TuShareHelper() as helper:
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

        # ── 相对强度与行业宽度（自算，零新数据源）─────────────────────────────
        today = datetime.date.today().strftime("%Y%m%d")
        lookback = (datetime.date.today() - datetime.timedelta(days=180)).strftime("%Y%m%d")
        rs_stock_industry_20d = rs_stock_industry_60d = None
        rs_industry_market_20d = rs_industry_market_60d = None
        industry_breadth_20 = industry_breadth_60 = None

        with TuShareHelper() as helper:
            # 个股收盘（复用已有 daily）与沪深300 收盘（复用已有 index_daily）
            stock_df = helper.daily(
                ts_code=ts_code, start_date=lookback, end_date=today, fields="ts_code,trade_date,close"
            ).data
            hs300_df = helper.index_daily(
                ts_code="000300.SH", start_date=lookback, end_date=today, fields="ts_code,trade_date,close"
            ).data
            stock_closes = _sorted_close(stock_df, "close_price")
            hs300_closes = _sorted_close(hs300_df, "industry_close")

            if sw_code:
                # 行业指数收盘（复用已有 index_daily）
                industry_df = helper.index_daily(
                    ts_code=sw_code, start_date=lookback, end_date=today, fields="ts_code,trade_date,close"
                ).data
                industry_closes = _sorted_close(industry_df, "industry_close")

                rs_stock_industry_20d, rs_stock_industry_60d = _excess_return(stock_closes, industry_closes)
                rs_industry_market_20d, rs_industry_market_60d = _excess_return(industry_closes, hs300_closes)

                # 行业宽度为自算增强字段，任何失败都降级为 None，不影响行业事实主体
                try:
                    constituents = _fetch_constituents(helper, sw_code, sw_level)
                    industry_breadth_20, industry_breadth_60 = _compute_breadths(
                        helper, constituents or [], lookback, today
                    )
                except Exception:
                    industry_breadth_20 = industry_breadth_60 = None

        return {
            "stock_code": ts_code,
            "industry": industry,
            "sw_path": sw_path,
            "sw_code": sw_code,
            "sw_level": sw_level,  # L1 / L2 / L3（消费方据此定 classification_standard）
            "sw_daily": sw_daily,
            "sw_daily_error": sw_daily_error,
            "rs_stock_industry_20d": rs_stock_industry_20d,
            "rs_stock_industry_60d": rs_stock_industry_60d,
            "rs_industry_market_20d": rs_industry_market_20d,
            "rs_industry_market_60d": rs_industry_market_60d,
            "industry_breadth_20": industry_breadth_20,
            "industry_breadth_60": industry_breadth_60,
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

    rs_values = {
        "个股相对行业 20日超额收益（%）": _opt_float(data.get("rs_stock_industry_20d")),
        "个股相对行业 60日超额收益（%）": _opt_float(data.get("rs_stock_industry_60d")),
        "行业相对沪深300 20日超额收益（%）": _opt_float(data.get("rs_industry_market_20d")),
        "行业相对沪深300 60日超额收益（%）": _opt_float(data.get("rs_industry_market_60d")),
        "行业宽度 20日（%）": _opt_float(data.get("industry_breadth_20")),
        "行业宽度 60日（%）": _opt_float(data.get("industry_breadth_60")),
    }
    if any(v is not None for v in rs_values.values()):
        lines += ["### 相对强度与行业宽度", "| 指标 | 数值 |", "|------|------|"]
        for label, v in rs_values.items():
            if v is not None:
                lines.append(f"| {label} | {v:+.2f} |" if "超额收益" in label else f"| {label} | {v:.2f} |")
        lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    # Example usage
    symbol = "600577.SH"  # 精达股份
    fact_data = get_industry_fact(symbol)
    markdown_output = render(fact_data)
    print(markdown_output)
