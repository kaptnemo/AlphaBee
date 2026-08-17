"""真实赛道标签推导（COMPANY_TRACK_ROADMAP Phase B）。

B1 规则层：真实赛道 = 收入占比 top1 且增速非负的业务线；占比与增速冲突时取
「占比 × 增速」加权得分最高者（避免高增速低占比噪音）。分项数据来自 EM 时占比
为 PERCENT 直接可用；tushare 兜底无占比 → 用类别内收入相对占比近似并告警。

B2 可选 LLM 复核（``agent.track``）：给规则输出 + 业务线明细，产出更丰富的
``track_label`` + ``override_basis``；LLM 失败/关闭回退纯规则。

B4 漂移检测：跨年报期业务主线变化可观测（直击申万分类更新滞后硬伤）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from alphabee.company_track.contracts import SegmentCollection, SegmentSnapshot

# 分类类型优选顺序：产品分解最能代表业务线；其次行业；再退其他
_CATEGORY_PREFERENCE = ("按产品分类", "按行业分类")
_OTHER_MARKERS = ("其他", "其它")


@dataclass
class TrackLabelResult:
    """规则层赛道推导结果。"""

    dominant_segment: str | None  # 占比（或收入）最大业务线
    fastest_segment: str | None  # 增速最快业务线（占比 ≥ min_share）
    track_label: str  # 规则标签（= 加权胜出分项名）
    override_basis: str  # 依据文本（引用占比/增速数值）
    candidates: list[dict] = field(default_factory=list)  # [{name, share, yoy, score}] 前 5
    warnings: list[str] = field(default_factory=list)
    category: str = ""  # 实际使用的分类类型


def _is_other(name: str) -> bool:
    return any(marker in name for marker in _OTHER_MARKERS)


def _pick_category(segments: list[SegmentSnapshot]) -> str:
    present = sorted({seg.category for seg in segments if seg.category})
    if not present:
        return ""
    for preferred in _CATEGORY_PREFERENCE:
        if preferred in present:
            return preferred
    return present[0]


def _candidate_segments(segments: list[SegmentSnapshot], category: str) -> list[SegmentSnapshot]:
    if not category:  # tushare 兜底：无分类类型标记 → 全量作为候选池
        return list(segments)
    return [seg for seg in segments if seg.category == category]


def _effective_share(seg: SegmentSnapshot, candidates: list[SegmentSnapshot]) -> float:
    """分项占比：数据源直接给（EM）→ 类别内收入相对占比（tushare 兜底近似）。"""
    if seg.revenue_share is not None:
        return seg.revenue_share
    total = sum(s.revenue or 0.0 for s in candidates)
    if total > 0 and seg.revenue is not None:
        return seg.revenue / total * 100.0
    return 0.0


def _weighted_score(seg: SegmentSnapshot, share: float) -> float:
    """「占比 × 增速」加权得分：非负增速放大，负增速衰减（-50% → ×0.5）。"""
    multiplier = 1.0
    if seg.revenue_yoy is not None:
        multiplier = 1.0 + seg.revenue_yoy / 100.0
        if multiplier < 0.05:  # 极端负增速不至于归零/变负
            multiplier = 0.05
    return share * multiplier


def derive_track_label(
    segments: list[SegmentSnapshot],
    *,
    min_share: float = 5.0,
) -> TrackLabelResult:
    """规则层：从最新报告期分项推导真实赛道（B1）。

    Args:
        segments: 最新报告期的分项记录（含占比/增速；增速可为 None）。
        min_share: 增速最快业务线的占比下限（避免高增速低占比噪音）。

    Returns:
        ``TrackLabelResult``：dominant（占比最大）/ fastest（增速最快且占比达标）/
        track_label（加权胜出分项）/ override_basis（数值依据）/ candidates（评分前 5）。
    """
    category = _pick_category(segments)
    candidates = [seg for seg in _candidate_segments(segments, category) if not _is_other(seg.segment_name)]
    if not candidates:
        return TrackLabelResult(None, None, "", "", [], ["无有效业务线分项"], category)

    share_missing = all(seg.revenue_share is None for seg in candidates)
    warnings: list[str] = []
    if share_missing and not category:
        warnings.append("占比缺失（tushare 兜底口径，产品/地区混列），按类别内收入近似排序，仅供参考")

    ranked = sorted(candidates, key=lambda seg: _effective_share(seg, candidates), reverse=True)
    dominant = ranked[0]

    fast_pool = [
        seg for seg in candidates if _effective_share(seg, candidates) >= min_share and seg.revenue_yoy is not None
    ]
    fastest = max(fast_pool, key=lambda seg: seg.revenue_yoy, default=None)

    scored = sorted(
        candidates,
        key=lambda seg: _weighted_score(seg, _effective_share(seg, candidates)),
        reverse=True,
    )
    weighted_winner = scored[0]

    label_segment = dominant
    if (
        dominant is not None
        and dominant.revenue_yoy is not None
        and dominant.revenue_yoy < 0
        and weighted_winner is not dominant
    ):
        warnings.append(
            f"占比最大业务线 {dominant.segment_name} 增速为负（{dominant.revenue_yoy:.1f}%），"
            f"按「占比×增速」加权取 {weighted_winner.segment_name}"
        )
        label_segment = weighted_winner

    basis_parts: list[str] = []
    for seg in scored[:5]:
        share = _effective_share(seg, candidates)
        yoy = f"同比 {seg.revenue_yoy:+.1f}%" if seg.revenue_yoy is not None else "增速未知"
        basis_parts.append(f"{seg.segment_name} {share:.1f}%（{yoy}）")
    override_basis = "；".join(basis_parts)

    return TrackLabelResult(
        dominant_segment=dominant.segment_name if dominant else None,
        fastest_segment=fastest.segment_name if fastest else None,
        track_label=label_segment.segment_name if label_segment else "",
        override_basis=override_basis,
        candidates=[
            {
                "name": seg.segment_name,
                "share": round(_effective_share(seg, candidates), 2),
                "yoy": seg.revenue_yoy,
                "score": round(_weighted_score(seg, _effective_share(seg, candidates)), 2),
            }
            for seg in scored[:5]
        ],
        warnings=warnings,
        category=category,
    )


def synthesize_track_label(
    collection: SegmentCollection,
    rule: TrackLabelResult,
    *,
    use_llm: bool = False,
) -> tuple[str, str, str]:
    """B2：可选 LLM 复核 → ``(track_label, override_basis, method)``。

    LLM 关闭或失败 → 回退规则结果（``method="rule"``），永不阻断。
    """
    if not use_llm:
        return rule.track_label, rule.override_basis, "rule"
    try:
        from alphabee.utils.llm import create_chat_model
        from alphabee.utils.pipeline import parse_json

        latest = collection.latest_segments()
        segment_lines = "\n".join(
            f"- {seg.segment_name}（{seg.category or '未分类'}）: "
            f"占比 {seg.revenue_share if seg.revenue_share is not None else '—'}%, "
            f"同比 {seg.revenue_yoy if seg.revenue_yoy is not None else '—'}%, "
            f"毛利率 {seg.gross_margin if seg.gross_margin is not None else '—'}%"
            for seg in latest
        )
        prompt = (
            "你是行业研究员。基于公司业务线收入构成，给出「真实赛道」标签——穿透申万行业分类，"
            "指向公司真正的增长引擎（例如工业富联 → 'AI 算力基础设施 ODM'）。"
            f"规则层建议: {rule.track_label or '（无）'}。\n"
            '只输出 JSON: {"track_label": "不超过 20 字", '
            '"override_basis": "引用具体占比/增速数据的一句话依据"}。\n'
            f"业务线数据（最新报告期 {collection.latest_period}）:\n{segment_lines}"
        )
        model = create_chat_model("agent.track")
        raw = model.invoke(prompt).content
        parsed = parse_json(raw)
        if not isinstance(parsed, dict):
            return rule.track_label, rule.override_basis, "rule"
        label = str(parsed.get("track_label") or "").strip()
        basis = str(parsed.get("override_basis") or "").strip()
        if label:
            return label, basis or rule.override_basis, "llm"
        return rule.track_label, rule.override_basis, "rule"
    except Exception:
        return rule.track_label, rule.override_basis, "rule"


def detect_track_drift(segments: list[SegmentSnapshot]) -> list[str]:
    """B4：跨年报期业务主线漂移检测（直击分类更新滞后硬伤）。

    仅比较年报期（报告期以 ``1231`` 结尾）。**全期统一优选分类类型**（产品优先），
    缺失该分类的报告期跳过——避免早期只有"按地区"行时把地区当主线、或跨分类误比较。
    主线（占比/收入最大分项）跨期变化 → note。
    """
    annual = [seg for seg in segments if seg.report_date.endswith("1231") and not _is_other(seg.segment_name)]
    annual_periods = sorted({seg.report_date for seg in annual})
    drift_category = _pick_category(annual)

    notes: list[str] = []
    prev_dominant: str | None = None
    prev_period: str | None = None
    for period in annual_periods:
        pool = _candidate_segments([seg for seg in annual if seg.report_date == period], drift_category)
        if not pool:
            continue
        dominant = max(
            pool,
            key=lambda seg: seg.revenue_share if seg.revenue_share is not None else (seg.revenue or 0.0),
        )
        if prev_dominant is not None and dominant.segment_name != prev_dominant:
            notes.append(f"业务主线漂移: {prev_dominant} → {dominant.segment_name}（{prev_period} → {period}）")
        prev_dominant = dominant.segment_name
        prev_period = period
    return notes
