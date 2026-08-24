"""业务线数据归一化（COMPANY_TRACK_ROADMAP Phase A，A4）。

职责：
1. 源行 → ``SegmentSnapshot``（canonical，报告期统一为 ``report_date`` YYYYMMDD）；
2. 占比/增速补齐：EM 直接给占比；Tushare 无占比/增速 → 由收入推导（``is_calculated`` 标记）；
3. **跨期 yoy 推导**（设计修正）：两数据源均无"分项增速"列，按
   「最新报告期 vs 去年同报告期」同名分项收入对比计算（``report_date[:-4]`` 换年前缀）；
4. 噪音过滤（可选）：``min_share`` 低占比剔除、``drop_other`` 剔除"其他/其它"分项；
5. 报告期对齐：``latest_report_period`` / ``assess_period_consistency`` 供口径检查。

纯函数层，不触网；外部列名只存在于 data 层（经 adapter 后 canonical）。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from alphabee.company_track.contracts import SegmentSnapshot
from alphabee.company_track.naming import is_other_placeholder


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # 排除 NaN


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(value).strip()
    except (TypeError, ValueError):
        return ""


def normalize_segments(
    rows: list[dict[str, Any]],
    source: str,
    *,
    min_share: float = 0.0,
    drop_other: bool = False,
) -> list[SegmentSnapshot]:
    """把源行归一化为分项记录（含占比推导与跨期 yoy 推导）。

    Args:
        rows: post-adapter canonical 行（EM：report_date/biz_segment_*；
            tushare：period/biz_segment_*）。
        source: ``em`` 或 ``tushare``。
        min_share: 过滤占比低于该值（%）的分项（仅占比可得时生效）。
        drop_other: 过滤名称含"其他/其它"的分项。

    Returns:
        归一化分项列表（跨全部报告期；已按 report_date 升序稳定排列）。
    """
    if source not in ("em", "tushare"):
        return []
    segments: list[SegmentSnapshot] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        segment = _row_to_snapshot(row, source)
        if segment is not None:
            segments.append(segment)
    if not segments:
        return []

    # 占比：仅 EM 源直接给出（tushare 兜底不推导——fina_mainbz 产品/地区混列且无
    # 分类类型标记，求和推导会产生口径错配（实测兆易创新"集成电路产品"被算成 ~1%），
    # 宁缺毋错：占比缺失 → Phase B 标签推导改用 revenue 兜底）

    # 跨期 yoy 推导（两源通用）
    derive_segment_yoy(segments)

    # 噪音过滤
    if min_share > 0 or drop_other:
        segments = [
            seg
            for seg in segments
            if not (
                (drop_other and _is_other(seg.segment_name))
                or (seg.revenue_share is not None and seg.revenue_share < min_share)
            )
        ]

    return segments


def _row_to_snapshot(row: dict[str, Any], source: str) -> SegmentSnapshot | None:
    report_date = _safe_str(row.get("report_date") or row.get("period"))
    segment_name = _safe_str(row.get("biz_segment_name"))
    if not report_date or not segment_name:
        return None
    revenue = _safe_float(row.get("biz_segment_revenue"))
    if revenue is None:
        return None  # 无收入的分项无意义（毛利率/占比均无从谈起）
    share = _safe_float(row.get("biz_segment_revenue_share"))
    gross_margin = _safe_float(row.get("biz_gross_margin"))
    if source == "em":
        # 单位转换（实测修正）：东方财富 API 的 收入比例 / 毛利率 是 0-1 比例
        # （各类别内部合计 = 1.0，茅台酒 0.8569 ≈ 85.7%），canonical 为 PERCENT → ×100
        share = share * 100.0 if share is not None else None
        gross_margin = gross_margin * 100.0 if gross_margin is not None else None
    yoy = _safe_float(row.get("biz_segment_revenue_yoy"))
    return SegmentSnapshot(
        report_date=report_date,
        segment_name=segment_name,
        category=_safe_str(row.get("biz_segment_category")),
        revenue=revenue,
        revenue_share=share,
        revenue_yoy=yoy,
        gross_margin=gross_margin,
        cost=_safe_float(row.get("biz_segment_cost")),
        profit=_safe_float(row.get("biz_segment_profit")),
        is_calculated=False,  # 由 derive_segment_yoy 在推导时置 True
        source=source,
    )


def _prior_report_date(report_date: str) -> str | None:
    """去年同报告期（20251231 → 20241231；20250630 → 20240630）。"""
    if len(report_date) != 8:
        return None
    year = int(report_date[:4])
    if year <= 1000:
        return None
    return f"{year - 1}{report_date[4:]}"


def derive_segment_yoy(segments: list[SegmentSnapshot]) -> None:
    """跨期同口径推导分项同比增速（%）。

    对每个分项：优先用数据源直接给的 yoy（EM 无此列，tushare 也无——统一推导）；
    推导口径 = 当期收入 / 去年同期同报告期同名分项收入 − 1，×100；
    仅当去年同期同名分项存在且收入为正时可计算，否则保持 None。
    """
    by_key: dict[tuple[str, str], float | None] = {(seg.report_date, seg.segment_name): seg.revenue for seg in segments}
    for seg in segments:
        if seg.revenue_yoy is not None:
            continue
        prior = _prior_report_date(seg.report_date)
        if prior is None:
            continue
        prior_revenue = by_key.get((prior, seg.segment_name))
        if prior_revenue is None or prior_revenue <= 0 or seg.revenue is None:
            continue
        seg.revenue_yoy = round((seg.revenue / prior_revenue - 1.0) * 100.0, 4)
        seg.is_calculated = True


def _is_other(name: str) -> bool:
    # 精确判定：只匹配「其他/其他(补充)/其他业务…」占位项，
    # 不误杀「汽车、汽车相关产品及其他产品」这类复合名（见 naming.py）。
    return is_other_placeholder(name)


def latest_report_period(segments: list[SegmentSnapshot]) -> str | None:
    """最新报告期（YYYYMMDD 字典序即时间序）。"""
    periods = {seg.report_date for seg in segments}
    return max(periods) if periods else None


def segments_for_period(segments: list[SegmentSnapshot], report_date: str) -> list[SegmentSnapshot]:
    """取指定报告期的分项列表。"""
    return [seg for seg in segments if seg.report_date == report_date]


def assess_period_consistency(segments: list[SegmentSnapshot]) -> tuple[str, dict[str, int]]:
    """报告期口径一致性（A4：latest_period 唯一性检查）。

    Returns:
        (status, period_counts)：``aligned``（单报告期）/ ``multi_period``（多报告期，
        属正常——跨期 yoy 推导的前提）；counts 为各报告期分项数。
    """
    counts: dict[str, int] = Counter(seg.report_date for seg in segments)
    if not counts:
        return "empty", {}
    return ("aligned" if len(counts) == 1 else "multi_period", dict(counts))
