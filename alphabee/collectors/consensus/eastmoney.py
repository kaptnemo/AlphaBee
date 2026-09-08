"""Eastmoney consensus fetcher — pull research-report pages and aggregate.

直接消费 :func:`alphabee.tools.eastmoney.get_eastmoney_report_list`（全量 58 字段），
**不要**依赖 :func:`alphabee.financial_report.links.get_research_report_links`
（它只服务 PDF 下载链接，会裁剪 EPS/目标价字段）。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from alphabee.collectors.consensus.engine import ConsensusOutput, aggregate_consensus
from alphabee.tools.eastmoney import get_eastmoney_report_list


def _normalize_code(code: str) -> str:
    """``300750.SZ`` / ``sz300750`` → ``300750``（6 位，去交易所前后缀）。"""
    code = (code or "").strip().lower()
    if code.startswith(("sz", "sh", "bj")):
        code = code[2:]
    if "." in code:
        code = code.split(".")[0]
    return code


def fetch_report_records(
    code: str,
    *,
    months: int = 12,
    page_size: int = 100,
    timeout: int = 20,
    max_pages: int = 20,
) -> list[dict[str, Any]]:
    """拉取个股近 ``months`` 个月全量研报列表（自动翻页），返回原始记录列表。"""
    sec_code = _normalize_code(code)
    if not sec_code:
        raise ValueError("code 不能为空。")

    end = date.today()
    start = end - timedelta(days=round(months * 30.44))

    records: list[dict[str, Any]] = []
    page_num = 1
    while page_num <= max_pages:
        result = get_eastmoney_report_list(
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            page_num=page_num,
            page_size=page_size,
            code=sec_code,
            timeout=timeout,
        )
        reports = result.get("reports") or []
        records.extend(reports)
        if not reports or not result.get("has_next"):
            break
        page_num += 1
    return records


def build_consensus(
    code: str,
    *,
    months: int = 12,
    page_size: int = 100,
    timeout: int = 20,
    max_pages: int = 20,
    as_of_date: str | None = None,
    target_price_coverage_threshold: float = 0.3,
) -> ConsensusOutput:
    """拉取个股研报并聚合成 consensus canonical 字段（:class:`ConsensusOutput`）。"""
    records = fetch_report_records(
        code,
        months=months,
        page_size=page_size,
        timeout=timeout,
        max_pages=max_pages,
    )
    return aggregate_consensus(
        records,
        as_of_date=as_of_date,
        target_price_coverage_threshold=target_price_coverage_threshold,
    )
