"""真实赛道标签推导（COMPANY_TRACK_ROADMAP Phase B）。

B1 规则层：真实赛道 = 收入占比 top1 且增速非负的业务线；占比与增速冲突时取
「占比 × 增速」加权得分最高者（避免高增速低占比噪音）。分项数据来自 EM 时占比
为 PERCENT 直接可用；tushare 兜底无占比 → 用类别内收入相对占比近似并告警。

口径稳定化（相对原始设计的关键修正）：
- 标签基准期用 **最新年报期**（:func:`select_label_base`），而非最新总体期——
  半年报常退化为「销售商品/许可收入」收入性质拆分，年报才是治疗领域/产品组合；
- 识别**收入性质拆分**与 **EM 匿名占位名**，产出显式告警；
- 漂移检测区分「业务主线漂移 / 分类口径切换 / 纯改名」，避免把改名当漂移。

B2 可选 LLM 复核（``agent.track``）：给规则输出 + 业务线明细，产出更丰富的
``track_label`` + ``override_basis``；LLM 失败/关闭回退纯规则。

B4 漂移检测：跨年报期业务主线变化可观测（直击申万分类更新滞后硬伤）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher

from alphabee.company_track.contracts import SegmentCollection, SegmentSnapshot
from alphabee.company_track.naming import (
    is_anonymized_segment,
    is_other_placeholder,
    is_revenue_type_segment,
)

# 分类类型优选顺序：产品分解最能代表业务线；其次行业；再退其他
_CATEGORY_PREFERENCE = ("按产品分类", "按行业分类")


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
    return is_other_placeholder(name)


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


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _is_rename(a: set[str], b: set[str]) -> bool:
    """两个单元素集合是否只是改名（相似度高），而非实质重分类。"""
    if len(a) != 1 or len(b) != 1:
        return False
    x, y = next(iter(a)), next(iter(b))
    return SequenceMatcher(None, x, y).ratio() >= 0.5


def derive_track_label(
    segments: list[SegmentSnapshot],
    *,
    min_share: float = 5.0,
) -> TrackLabelResult:
    """规则层：从**基准报告期**分项推导真实赛道（B1）。

    Args:
        segments: 基准报告期的分项记录（含占比/增速；增速可为 None）。
            调用方应先用 :func:`select_label_base` 选最新年报期，避免半年报
            收入性质拆分污染标签。
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

    # 收入性质拆分告警：候选全是「销售商品/许可收入/提供服务…」→ 非产品组合
    if all(is_revenue_type_segment(seg.segment_name) for seg in candidates):
        warnings.append(
            f"「{category}」分项均为收入性质拆分（销售商品/许可收入/提供服务…），"
            "非产品组合，赛道标签参考性弱，建议回退最近年报期产品口径"
        )

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

    # EM 匿名占位名告警：标签若落在「主业1/产品2…」上，真实业务线不可还原
    if label_segment and is_anonymized_segment(label_segment.segment_name):
        warnings.append(f"赛道分项「{label_segment.segment_name}」为匿名占位名，真实业务线未解析，建议人工核对")

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


def select_label_base(segments: list[SegmentSnapshot]) -> tuple[list[SegmentSnapshot], str]:
    """选择赛道标签的基准报告期（稳定产品口径）。

    优先级：
    1. 最新**年报期**（``1231``）且其「按产品分类」为产品组合口径（非收入性质拆分）；
    2. 最近一个「按产品分类」为产品组合口径的年报期（跨期退一步，仍可比）；
    3. 最新年报期（即使口径退化）；
    4. 最新期兜底（无年报数据）。

    返回 ``(base_segments, base_period)``：base_segments 为该基准期的全部分项。
    """
    periods = sorted({s.report_date for s in segments})
    if not periods:
        return [], ""
    annual = [p for p in periods if p.endswith("1231")]

    def _product_is_mix(period: str) -> bool:
        segs = [
            s
            for s in segments
            if s.report_date == period and s.category == "按产品分类" and not _is_other(s.segment_name)
        ]
        if not segs:
            return False
        return not all(is_revenue_type_segment(s.segment_name) for s in segs)

    # 1) 最新年报且产品口径为产品组合
    for period in reversed(annual):
        if _product_is_mix(period):
            return [s for s in segments if s.report_date == period], period
    # 2) 最新年报（即使口径退化）
    if annual:
        period = annual[-1]
        return [s for s in segments if s.report_date == period], period
    # 3) 最新期兜底
    period = periods[-1]
    return [s for s in segments if s.report_date == period], period


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

        base, base_period = select_label_base(collection.segments)
        segment_lines = "\n".join(
            f"- {seg.segment_name}（{seg.category or '未分类'}）: "
            f"占比 {seg.revenue_share if seg.revenue_share is not None else '—'}%, "
            f"同比 {seg.revenue_yoy if seg.revenue_yoy is not None else '—'}%, "
            f"毛利率 {seg.gross_margin if seg.gross_margin is not None else '—'}%"
            for seg in base
        )
        prompt = (
            "你是行业研究员。基于公司业务线收入构成，给出「真实赛道」标签——穿透申万行业分类，"
            "指向公司真正的增长引擎（例如工业富联 → 'AI 算力基础设施 ODM'）。"
            f"规则层建议: {rule.track_label or '（无）'}。\n"
            '只输出 JSON: {"track_label": "不超过 20 字", '
            '"override_basis": "引用具体占比/增速数据的一句话依据"}。\n'
            f"业务线数据（基准报告期 {base_period}）:\n{segment_lines}"
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

    区分三类相邻期变化（比原版只比「主线换人」更细）：
    - **业务主线漂移**：分项集合有交集但主线（占比/收入最大分项）换人；
    - **分类口径切换**：分项集合完全不相交（Jaccard=0）且非纯改名 → 实质重分类，破坏可比；
    - **纯改名**：分项集合完全不相交但两侧均为单元素且名称高度相似 → 吸收，不报漂移。
    """
    annual = [seg for seg in segments if seg.report_date.endswith("1231") and not _is_other(seg.segment_name)]
    annual_periods = sorted({seg.report_date for seg in annual})
    drift_category = _pick_category(annual)

    notes: list[str] = []
    prev: tuple[str, list[str], str] | None = None  # (period, info_names, dominant)
    for period in annual_periods:
        pool = [seg for seg in annual if seg.report_date == period and seg.category == drift_category]
        info = [seg.segment_name for seg in pool if not _is_other(seg.segment_name)]
        if not info:
            continue
        dominant = max(
            pool,
            key=lambda seg: seg.revenue_share if seg.revenue_share is not None else (seg.revenue or 0.0),
        ).segment_name
        if prev is not None:
            prev_period, prev_info, prev_dominant = prev
            if _jaccard(set(prev_info), set(info)) == 0.0:
                if len(prev_info) == 1 and len(info) == 1 and _is_rename(set(prev_info), set(info)):
                    pass  # 纯改名，非漂移
                elif len(prev_info) == 1 and len(info) == 1:
                    # 单元素互不相交且非改名 → 业务主线换人（保守沿用原语义）
                    notes.append(f"业务主线漂移: {prev_dominant} → {dominant}（{prev_period} → {period}）")
                else:
                    notes.append(f"分类口径切换: {'/'.join(prev_info)} → {'/'.join(info)}（{prev_period} → {period}）")
            elif dominant != prev_dominant:
                notes.append(f"业务主线漂移: {prev_dominant} → {dominant}（{prev_period} → {period}）")
        prev = (period, info, dominant)
    return notes
