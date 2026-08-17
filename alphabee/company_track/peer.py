"""对标组基准计算（COMPANY_TRACK_ROADMAP Phase D，D1/D2）。

``derive_peer_benchmarks`` 对**显式对标组代码列表**取数并推导 ``peer_*`` canonical
基准——复用行业包的 ``fetch_peer_financials_for_codes`` + ``normalize_industry_records``
+ ``derive_benchmarks`` 纯函数链（中位数语义与行业基准完全一致，唯一差异是成分来源
从"申万指数成分"换成"真对手代码列表"）。

回退语义（D5）：``peer_*`` 缺失 → 顺延 ``industry_*`` → 绝对阈值——规则消费链
由缺失字段回退机制（registry.py 表达式列表）天然实现，零引擎改动。
"""

from __future__ import annotations

from datetime import date
from typing import Any

_PEER_BENCHMARK_FIELDS = (
    "peer_avg_roe",
    "peer_avg_debt_ratio",
    "peer_avg_gross_margin",
    "peer_revenue_yoy",
    "peer_median_pe_ttm",
    "peer_median_pb",
)


def peer_benchmark_fields() -> tuple[str, ...]:
    """可注入 fact_values 的 ``peer_*`` canonical 键目录（防拼写漂移）。"""
    return _PEER_BENCHMARK_FIELDS


def derive_peer_benchmarks(
    peer_codes: list[str],
    *,
    industry: str = "",
    as_of_date: str | None = None,
    limit: int = 20,
) -> tuple[dict[str, float], dict[str, Any]]:
    """对标组基准推导 → ``(peer_values, meta)``。

    Args:
        peer_codes: 对标组代码列表（Tushare 格式）。
        industry: 展示用行业名（血缘）。
        as_of_date: 数据日期（默认今天）。
        limit: 对标组成分抽样上限。

    Returns:
        peer_values: 非空 ``peer_*`` 基准（None 不注入——缺失即回退 industry_* / 绝对）；
        meta: ``{"peer_count", "fetched_codes", "error", "source_refs"}`` 血缘与诊断。
    """
    from alphabee.industry.benchmarks import derive_benchmarks
    from alphabee.industry.data import fetch_peer_financials_for_codes
    from alphabee.industry.normalize import normalize_industry_records

    meta: dict[str, Any] = {
        "peer_count": 0,
        "fetched_codes": [],
        "error": None,
        "source_refs": [f"peer_group:manual({len(peer_codes)} codes)"],
    }

    rows, fetched, error = fetch_peer_financials_for_codes(peer_codes, limit=limit)
    meta["fetched_codes"] = fetched
    meta["error"] = error
    if error or not rows:
        return {}, meta

    records = normalize_industry_records(rows, source="tushare")
    if not records:
        meta["error"] = "对标组记录归一化后为空"
        return {}, meta

    benchmarks = derive_benchmarks(
        records,
        industry=industry,
        as_of_date=as_of_date or date.today().isoformat(),
        source_refs=meta["source_refs"],
    )
    meta["peer_count"] = len(records)

    values: dict[str, float] = {}
    for field, attr in (
        ("peer_avg_roe", "avg_roe"),
        ("peer_avg_debt_ratio", "avg_debt_ratio"),
        ("peer_avg_gross_margin", "avg_gross_margin"),
        ("peer_revenue_yoy", "revenue_yoy"),
        ("peer_median_pe_ttm", "pe_ttm"),
        ("peer_median_pb", "pb"),
    ):
        value = getattr(benchmarks, attr)
        if value is not None:
            values[field] = value
    return values, meta
