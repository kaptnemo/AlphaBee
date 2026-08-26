"""业务线数据采集（COMPANY_TRACK_ROADMAP Phase A，A3）。

数据源优先级（best-effort，任何一步失败不中断）：
1. **东方财富主营构成**（``ak.stock_zygc_em``）：占比/毛利率/成本/利润齐全，多报告期；
2. **Tushare fina_mainbz** 兜底：分项收入/成本/利润，无占比/增速（由 normalize 推导）。

外部列名只存在于本模块（经 adapter 后一律 canonical）；符号转换内联实现，
避免拉起 ``alphabee.agents.facts`` 包的 tushare import 副作用（与行业包同策略）。
"""

from __future__ import annotations

import datetime
from typing import Any

from alphabee.company_track.contracts import SegmentCollection
from alphabee.company_track.normalize import (
    latest_report_period,
    normalize_segments,
)


def _akshare_symbol(ts_code: str) -> str:
    """Tushare 格式（603986.SH）→ akshare 格式（SH603986）。"""
    code, _, suffix = ts_code.strip().partition(".")
    if suffix:
        return f"{suffix.upper()}{code}"
    upper = ts_code.strip().upper()
    if upper.startswith(("SH", "SZ", "BJ")):
        return upper
    if upper.startswith(("6", "9")):
        return f"SH{upper}"
    return f"SZ{upper}"


def _fetch_em_rows(symbol: str) -> tuple[list[dict[str, Any]], str | None]:
    """东方财富主营构成（post-adapter canonical 行）。"""
    try:
        from alphabee.collectors.akshare.helper import AkShareHelper

        with AkShareHelper() as helper:
            result = helper.stock_zygc_em(symbol=_akshare_symbol(symbol))
            df = result.to_dataframe()
        if df is None or df.empty:
            return [], "东方财富主营构成为空"
        # 报告日期是 date 对象 → 归一为 YYYYMMDD 字符串
        rows = df.to_dict(orient="records")
        for row in rows:
            raw = row.get("report_date")
            if hasattr(raw, "strftime"):
                row["report_date"] = raw.strftime("%Y%m%d")
        return rows, None
    except Exception as exc:
        return [], f"东方财富主营构成获取失败: {exc}"


def _dedupe_tushare_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """fina_mainbz 去重：同 (报告期, 分项) 保留 update_flag 最新修订（2 > 1）。"""
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row.get("period") or ""), str(row.get("biz_segment_name") or ""))
        if not all(key):
            continue
        current = best.get(key)
        flag = str(row.get("update_flag") or "1")
        if current is None or flag > str(current.get("update_flag") or "1"):
            best[key] = row
    return list(best.values())


def _fetch_tushare_rows(symbol: str) -> tuple[list[dict[str, Any]], str | None]:
    """Tushare fina_mainbz（post-adapter canonical 行；无占比/增速，含修订行去重）。"""
    try:
        from alphabee.collectors.tushare.helper import TuShareHelper

        start = (datetime.date.today() - datetime.timedelta(days=1095)).strftime("%Y%m%d")
        with TuShareHelper() as helper:
            df = helper.fina_mainbz(
                ts_code=symbol,
                start_date=start,
                fields="ts_code,end_date,bz_item,bz_sales,bz_profit,bz_cost,curr_type,update_flag",
            ).data
        if df is None or df.empty:
            return [], "fina_mainbz 为空"
        rows = _dedupe_tushare_rows(df.to_dict(orient="records"))
        for row in rows:
            # adapter 已把 end_date → period；统一为 report_date
            row["report_date"] = str(row.pop("period", "") or "")
        return rows, None
    except Exception as exc:
        return [], f"fina_mainbz 获取失败: {exc}"


def fetch_business_segments(
    symbol: str,
    *,
    min_share: float = 0.0,
    drop_other: bool = False,
) -> SegmentCollection:
    """获取公司业务线分项数据（EM 优先，fina_mainbz 兜底，已归一化）。

    Args:
        symbol: 股票代码（Tushare 格式，如 603986.SH）。
        min_share: 过滤分项占比低于该值（%）的噪音行（仅当占比可得时生效）。
        drop_other: 过滤名称含"其他/其它"的分项。

    Returns:
        ``SegmentCollection``：``segments`` 为多报告期归一化分项记录（含跨期 yoy 推导）；
        ``source`` 为实际来源（em / tushare / none）；双源都失败时 segments 为空、
        ``error`` 记录原因（显式留痕，不抛异常）。
    """
    rows, em_error = _fetch_em_rows(symbol)
    source = "em"
    error: str | None = em_error

    if not rows:
        rows, tushare_error = _fetch_tushare_rows(symbol)
        source = "tushare"
        error = tushare_error

    segments = normalize_segments(rows, source, min_share=min_share, drop_other=drop_other)
    return SegmentCollection(
        symbol=symbol,
        segments=segments,
        source=source if segments else "none",
        latest_period=latest_report_period(segments) or "",
        error=error if not segments else None,
    )
