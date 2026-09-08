"""Crowding 采集器 — 关注度：研报覆盖热度排名（analyst_coverage_rank）。

复用东财研报数据（``collectors/consensus`` 的聚合引擎，其内部走
``tools.eastmoney.get_eastmoney_report_list`` → ``EastmoneyHelper.process_data``），
统计个股覆盖机构/研报数并在样本内排名。
"""

from __future__ import annotations

from alphabee.collectors.crowding.engine import normalize_code, rank_by_coverage


def fetch_coverage_count(
    code: str,
    *,
    months: int = 12,
    page_size: int = 100,
    timeout: int = 20,
    max_pages: int = 20,
) -> int | None:
    """个股研报覆盖机构数（复用 consensus 引擎的 coverage_count）。"""
    from alphabee.collectors.consensus.eastmoney import fetch_report_records  # noqa: PLC0415
    from alphabee.collectors.consensus.engine import aggregate_consensus  # noqa: PLC0415

    sec_code = normalize_code(code)
    if not sec_code:
        return None
    try:
        records = fetch_report_records(
            sec_code,
            months=months,
            page_size=page_size,
            timeout=timeout,
            max_pages=max_pages,
        )
        return aggregate_consensus(records).values.get("coverage_count")
    except Exception:
        return None


def fetch_analyst_coverage_rank(
    code: str,
    sample_codes: list[str] | None = None,
    *,
    months: int = 12,
    timeout: int = 20,
) -> int | None:
    """在 ``code`` + ``sample_codes`` 样本内按覆盖机构数排名，返回 ``code`` 的排名。

    排名 1 = 覆盖最热；``code`` 无覆盖 → None。样本为空时仅返回自身覆盖排名（恒为 1）。
    """
    codes = [code] + [c for c in (sample_codes or []) if c]
    counts: dict[str, int | None] = {}
    for c in codes:
        counts[c] = fetch_coverage_count(c, months=months, timeout=timeout)
    return rank_by_coverage(counts, code)
