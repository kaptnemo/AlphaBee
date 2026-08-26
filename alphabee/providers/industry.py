"""Industry data provider — unified interface with source fallback chain.

Priority order for each data domain:
  industry daily (行情+估值): sw_daily → akshare 申万指数 → index_daily + akshare 东财
"""

from __future__ import annotations

import datetime
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class IndustryDailyResult:
    daily: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    source: str = ""  # "sw_daily" | "akshare_sw_daily" | "index_daily+akshare" | "none"


def get_industry_daily(
    sw_code: str,
    industry: str,
    lookback_days: int = 90,
) -> IndustryDailyResult:
    """获取申万行业指数的日行情数据，含 PE/PB。

    按优先级尝试：
    1. Tushare ``sw_daily`` — 完整数据（close / pct_change / PE / PB），需 5000 积分
    2. AkShare 申万指数 — ``index_hist_sw``（行情）+ ``sw_index_*_info``（PE/PB 快照），
       免费、无积分门槛，任意层级指数代码都支持
    3. Tushare ``index_daily`` + AkShare 快照 — 趋势 + 估值分别获取（东财板块口径）

    Args:
        sw_code: 申万行业指数代码，如 ``801010.SI``。
        industry: 行业名称（如 ``白酒``），用于东财兜底的名称匹配。
        lookback_days: 回溯天数。
    """
    today = datetime.date.today().strftime("%Y%m%d")
    start = (datetime.date.today() - datetime.timedelta(days=lookback_days)).strftime("%Y%m%d")

    # 1. Tushare sw_daily
    result = _try_sw_daily(sw_code, start, today)
    if result is not None:
        return result

    # 2. AkShare 申万指数（免费，替代 sw_daily 的 5000 积分门槛）
    result = _try_akshare_sw_daily(sw_code, start, today)
    if result is not None:
        return result

    # 3. Tushare index_daily + AkShare 东财板块 PE/PB
    result = _try_index_daily_plus_akshare(sw_code, industry, start, today)
    if result is not None:
        return result

    return IndustryDailyResult(source="none", error="All sources exhausted")


# ── primary: sw_daily ──────────────────────────────────────────────────


def _try_sw_daily(sw_code: str, start: str, end: str) -> IndustryDailyResult | None:
    try:
        from alphabee.collectors.tushare.helper import TuShareHelper

        with TuShareHelper() as helper:
            df = helper.sw_daily(
                ts_code=sw_code,
                start_date=start,
                end_date=end,
                fields="ts_code,trade_date,close,pct_change,pe,pb,float_mv",
            ).data

        if df.empty:
            return None

        rows = _to_rows(df, extra=True)
        return IndustryDailyResult(daily=rows, source="sw_daily")

    except Exception:
        return None


# ── fallback 2: akshare 申万指数（免费，替代 sw_daily 的 5000 积分门槛）────────


def _iso_date(ymd: str) -> str:
    """``YYYYMMDD`` → ``YYYY-MM-DD``（index_hist_sw 的日期列格式）。"""
    s = str(ymd)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s


def _try_akshare_sw_daily(sw_code: str, start: str, end: str) -> IndustryDailyResult | None:
    """AkShare 申万指数日线 + 申万行业估值快照。

    - 行情：``index_hist_sw``（申万宏源研究官网），L1/L2/L3 指数代码都支持；
      无涨跌幅列，由收盘价逐日推导。
    - 估值：``sw_index_first/second/third_info``（乐咕乐股）按 sw_code 匹配
      TTM 市盈率 / 市净率快照（快照级而非逐日，与东财兜底口径一致）。
    """
    swcode6 = str(sw_code).split(".")[0]
    if not swcode6:
        return None

    try:
        from alphabee.collectors.akshare.helper import AkShareHelper

        with AkShareHelper() as helper:
            hist = helper.index_hist_sw(symbol=swcode6, period="day").to_dataframe()
            pe_val, pb_val = _get_sw_index_pe_pb(sw_code)
    except Exception:
        return None

    if hist.empty:
        return None

    start_iso, end_iso = _iso_date(start), _iso_date(end)
    rows: list[dict[str, Any]] = []
    prev_close: float | None = None
    for _, row in hist.iterrows():
        trade_date = str(row.get("日期"))
        close = _safe_float(row.get("收盘"))
        change_pct = 0.0
        if prev_close is not None and prev_close:
            change_pct = round((close - prev_close) / prev_close * 100, 2)
        prev_close = close
        if trade_date < start_iso or trade_date > end_iso:
            continue
        item: dict[str, Any] = {
            "trade_date": trade_date,
            "industry_close": close,
            "industry_change_pct": change_pct,
        }
        if pe_val is not None:
            item["industry_pe_ttm"] = pe_val
        if pb_val is not None:
            item["industry_pb"] = pb_val
        rows.append(item)

    if not rows:
        return None
    return IndustryDailyResult(daily=rows, source="akshare_sw_daily")


def _get_sw_index_pe_pb(sw_code: str) -> tuple[float | None, float | None]:
    """按申万代码从 ``sw_index_first/second/third_info`` 匹配 TTM 市盈率 / 市净率快照。

    乐咕乐股为免费网页源，偶发加载失败（``NoneType.find_all``），故逐表轻量重试。
    返回 ``(pe_ttm, pb)``；任一缺失为 None（不置 0）。
    """
    try:
        from alphabee.collectors.akshare.helper import AkShareHelper

        with AkShareHelper() as helper:
            for name in (
                "sw_index_first_info",
                "sw_index_second_info",
                "sw_index_third_info",
            ):
                df = None
                for _ in range(3):
                    try:
                        df = getattr(helper, name)().to_dataframe()
                        break
                    except Exception:
                        time.sleep(1.0)
                if df is None or df.empty or "行业代码" not in df.columns:
                    continue
                matched = df[df["行业代码"].astype(str) == str(sw_code)]
                if matched.empty:
                    continue
                row = matched.iloc[0]
                return _opt_float(row.get("TTM(滚动)市盈率")), _opt_float(row.get("市净率"))
    except Exception:
        pass
    return None, None


# ── fallback: index_daily + akshare ────────────────────────────────────


def _try_index_daily_plus_akshare(sw_code: str, industry: str, start: str, end: str) -> IndustryDailyResult | None:
    """index_daily gives close + pct_chg; akshare snapshot fills PE/PB."""

    # Step A: Tushare index_daily
    try:
        from alphabee.collectors.tushare.helper import TuShareHelper

        with TuShareHelper() as helper:
            df = helper.index_daily(
                ts_code=sw_code,
                start_date=start,
                end_date=end,
                fields="ts_code,trade_date,close,pct_chg",
            ).data

        if df.empty:
            return None

        rows = _to_rows(df, extra=False)

    except Exception:
        return None

    # Step B: AkShare PE/PB snapshot
    pe_val, pb_val = _get_akshare_pe_pb(industry)

    if pe_val is not None or pb_val is not None:
        for row in rows:
            if pe_val is not None:
                row["industry_pe_ttm"] = pe_val
            if pb_val is not None:
                row["industry_pb"] = pb_val

    return IndustryDailyResult(daily=rows, source="index_daily+akshare")


def _get_akshare_pe_pb(industry: str) -> tuple[float | None, float | None]:
    """从 AkShare 行业板块快照获取 PE/PB（东方财富，同花顺无等效接口）。"""
    if not industry:
        return None, None

    try:
        from alphabee.collectors.akshare.helper import AkShareHelper

        with AkShareHelper() as helper:
            result = helper.stock_board_industry_name_em()
            df = result.to_dataframe()

        if df.empty:
            return None, None

        # 字段治理（Phase 2）：adapter 重命名后只剩 canonical 列名，
        # 此处不回退外部列名（"板块名称"/"市盈率-动态" 只存在于 adapter mapping）。
        if "industry_name" not in df.columns:
            return None, None

        # 匹配：精确优先，前缀兜底（避免 industry[:2] contains 误中"半导体设备"等相近板块）
        names = df["industry_name"].astype(str)
        exact = df[names == industry]
        if exact.empty:
            pref = df[names.str.startswith(industry, na=False)]
            if pref.empty:
                return None, None
            row = pref.iloc[0]
        else:
            row = exact.iloc[0]
        pe = _safe_float(row.get("industry_pe_ttm"))
        pb = _safe_float(row.get("industry_pb"))
        return pe, pb

    except Exception:
        return None, None


# ── helpers ────────────────────────────────────────────────────────────


def _to_rows(df: Any, extra: bool) -> list[dict[str, Any]]:
    """Convert DataFrame to canonical row dicts.

    After TuShare adapter renaming, columns are already canonical names.
    """
    rows: list[dict[str, Any]] = []
    for _, row in df.head(10).iterrows():
        item: dict[str, Any] = {
            "trade_date": _safe_str(row, "trade_date"),
            "industry_close": _safe_float(row, "industry_close"),
            "industry_change_pct": _safe_float(row, "industry_change_pct"),
        }
        if extra:
            item["industry_pe_ttm"] = _safe_float(row, "industry_pe_ttm")
            item["industry_pb"] = _safe_float(row, "industry_pb")
        rows.append(item)
    return rows


def _safe_float(row_or_val: Any, col: str | None = None) -> float:
    import math

    val = row_or_val
    try:
        if col is not None:
            val = row_or_val.get(col, row_or_val)
        f = float(val)
        return f if not math.isnan(f) else 0.0
    except (ValueError, TypeError, AttributeError):
        return 0.0


def _safe_str(row_or_val: Any, col: str | None = None) -> str:
    try:
        if col is not None and hasattr(row_or_val, "get"):
            val = row_or_val.get(col, row_or_val)
        else:
            val = row_or_val
        if val is None or (isinstance(val, float) and val != val):
            return ""
        return str(val)
    except (ValueError, TypeError):
        return ""


def _opt_float(val: Any) -> float | None:
    """转 float；NaN / 无法解析返回 None（区别于 ``_safe_float`` 的 0 值）。"""
    import math

    try:
        f = float(val)
        return f if not math.isnan(f) else None
    except (TypeError, ValueError):
        return None
