"""公司赛道组装（COMPANY_TRACK_ROADMAP Phase B，B3/B4）。

``build_company_track`` 把 Phase A 的业务线数据 + Phase B 的标签推导聚合成
``CompanyTrackArtifact``：
- B3 override 机制：``track_label``（公司赛道，修正字段）与 ``sw_industry``（申万基线）
  并存；下游引用 track 必须注明「公司赛道标签（数据截至 X 报告期）」；
- B4 新鲜度：``as_of_date`` = 最新报告期；``stale_after`` = 报告期 + 90 天（年报期口径）；
  跨年报期主线漂移写入 ``review_notes``（业务漂移可观测）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from alphabee.company_track.contracts import CompanyTrackArtifact
from alphabee.company_track.label import (
    TrackLabelResult,
    derive_track_label,
    detect_track_drift,
    select_label_base,
    synthesize_track_label,
)
from alphabee.company_track.naming import is_other_placeholder, is_revenue_type_segment

_STALE_AFTER_DAYS = 90  # 报告期口径：年报披露后 ~3 个月视为需刷新


def _stale_after(report_date: str) -> str | None:
    try:
        parsed = datetime.strptime(report_date, "%Y%m%d").date()
    except ValueError:
        return None
    return (parsed + timedelta(days=_STALE_AFTER_DAYS)).isoformat()


def build_company_track(
    symbol: str,
    *,
    use_llm: bool = False,
    min_share: float = 0.0,
    drop_other: bool = True,
    sw_industry: str = "",
    sw_code: str = "",
    source_refs: list[str] | None = None,
) -> CompanyTrackArtifact:
    """构建公司赛道 artifact（取数 → 标签推导 → 可选 LLM 复核 → 漂移检测 → 组装）。

    Args:
        symbol: 股票代码（Tushare 格式，如 603986.SH）。
        use_llm: 是否启用 LLM 标签复核（``agent.track``；失败回退规则）。
        min_share / drop_other: 透传业务线取数的噪音过滤。
        sw_industry / sw_code: 申万基线（B3 并存展示；由调用方从已解析的行业上下文注入）。
        source_refs: 额外血缘（如研报来源）。

    Returns:
        ``CompanyTrackArtifact``；无业务线数据时 ``degraded=True`` + ``review_status="rejected"``
        并显式留痕（不抛异常）。
    """
    from alphabee.company_track.data import fetch_business_segments

    collection = fetch_business_segments(symbol, min_share=min_share, drop_other=drop_other)
    generated_at = datetime.now(UTC).isoformat(timespec="seconds")
    refs = list(source_refs or [])
    if collection.source:
        refs.append(f"segment_source:{collection.source}")

    if not collection.segments:
        return CompanyTrackArtifact(
            schema_version="1",
            symbol=symbol,
            sw_industry=sw_industry,
            sw_code=sw_code,
            as_of_date=collection.latest_period,
            generated_at=generated_at,
            source_refs=refs,
            review_status="rejected",
            review_notes=[f"无业务线数据: {collection.error or '未知原因'}"],
            degraded=True,
            degraded_reason=collection.error or "无业务线数据",
        )

    base_segments, base_period = select_label_base(collection.segments)
    rule: TrackLabelResult = derive_track_label(base_segments)
    track_label, basis, method = synthesize_track_label(collection, rule, use_llm=use_llm)
    drift_notes = detect_track_drift(collection.segments)

    notes = [f"公司赛道标签基于 {base_period} 报告期数据（来源 {collection.source}）"]
    if base_period and collection.latest_period and base_period != collection.latest_period:
        latest_product = [
            s
            for s in collection.segments
            if s.report_date == collection.latest_period
            and s.category == "按产品分类"
            and not is_other_placeholder(s.segment_name)
        ]
        if latest_product and all(is_revenue_type_segment(s.segment_name) for s in latest_product):
            notes.append(
                f"最新期 {collection.latest_period} 为收入性质拆分（半年报口径退化），"
                f"赛道标签回退最近年报期 {base_period}"
            )
        else:
            notes.append(
                f"标签基准取最近年报期 {base_period}（口径稳定优先），最新期 {collection.latest_period} 未用于标签"
            )
    notes.extend(rule.warnings)
    notes.extend(drift_notes)
    if method == "llm":
        notes.append("标签由 LLM 复核生成（agent.track），需人工复核证据")

    return CompanyTrackArtifact(
        schema_version="1",
        symbol=symbol,
        sw_industry=sw_industry,
        sw_code=sw_code,
        as_of_date=collection.latest_period,
        generated_at=generated_at,
        stale_after=_stale_after(collection.latest_period),
        source_refs=refs,
        segments=collection.segments,
        dominant_segment=rule.dominant_segment,
        fastest_segment=rule.fastest_segment,
        track_label=track_label,
        track_method=method,
        override_basis=basis,
        review_status="needs_review" if (rule.warnings or drift_notes) else "approved",
        review_notes=notes,
    )
