"""resolve_driver_profile node — DOMAIN_CONTEXT P0 第 5 步在线注入。

职责（只路由 + 注入，不重复取数，研究内核在 domain_context）：
1. 读 ``INDUSTRY_CONTEXT`` + ``COMPANY_TRACK`` + ``fact_values`` → 构造 ``RouterInput``
   （身份信号 + 结构信号：sw_code / 分部结构 / 财务结构）；
2. ``ContextRouter.route`` → ``build_driver_profile`` → 落 ``DRIVER_PROFILE`` artifact；
3. 降级契约（显式留痕，不静默）：
   - 身份信号全空 → ``generic_fundamental`` 兜底 + ``degraded=True`` + MEDIUM issue；
   - 普通未命中（有信号但无专用框架）→ ``generic_fundamental`` 兜底 + ``fallback=True``（非异常，不产 issue）；
   - 画像构建本身失败（primitive/playbook 加载异常等）→ 落降级画像 + MEDIUM issue，
     **绝不抛穿** LangGraph（否则整条 run 因一个可选的分析框架而中断）。

默认不阻塞：``DRIVER_PROFILE`` 恒产出（哪怕 fallback），下游 ``synthesize_insights`` 总是能
消费到一个 driver_profile 上下文。
"""

from __future__ import annotations

from datetime import UTC, datetime

from langchain_core.runnables import RunnableConfig

from alphabee.company_track.contracts import CompanyTrackArtifact, SegmentSnapshot
from alphabee.core import Artifact, ArtifactType, Issue, IssueSeverity, Step, StepStatus
from alphabee.domain_context import GENERIC_FALLBACK_ID, DriverProfile, RouterInput, build_driver_profile
from alphabee.orchestrator.collectors import _finalize_step, _make_id
from alphabee.orchestrator.contracts import IndustryContextArtifact, find_artifact_model
from alphabee.orchestrator.state import OrchestratorState

# ── 结构信号口径 ────────────────────────────────────────────────────────
# ``RouterInput.financial_structure`` 的键只允许是 canonical 字段名（schemas/*.yaml），
# 由 fact_values（规范化数值事实）直接取值；取不到就不写（"没拿到口径" ≠ "口径不达标"）。
# 注意：设计稿举例的 rnd_ratio / inventory_ratio 在 canonical schema 中并不存在，
# 故不入表——避免发明字段名（schema-steward 纪律：业务代码只用 canonical 字段）。
_FINANCIAL_STRUCTURE_FIELDS = (
    "gross_margin",
    "net_margin",
    "roe",
    "roa",
    "debt_to_assets",
    "current_ratio",
    "inventory_yoy",
    "revenue_yoy",
    "net_profit_yoy",
)

# 毛利率行业基准：优先真对标组（peer_avg_gross_margin），回退申万行业基准
# （industry_avg_gross_margin）。两者都是 canonical 字段。
_PEER_GROSS_MARGIN_FIELDS = ("peer_avg_gross_margin", "industry_avg_gross_margin")
# RouterInput 内部键名（context_router 的结构性兜底驱动按此键读取，见编码设计 §7.1）。
_PEER_GROSS_MARGIN_KEY = "peer_gross_margin"

# 分部摘要条数上限：仅供路由/报告做"结构画像"，不需要全量分项。
_SEGMENT_SUMMARY_LIMIT = 5


def _latest_segments(track: CompanyTrackArtifact) -> list[SegmentSnapshot]:
    """取**最新报告期**的分项快照（跨期混用会让占比/增速失真）。"""
    if not track.segments:
        return []
    latest = max(seg.report_date for seg in track.segments)
    return [seg for seg in track.segments if seg.report_date == latest]


def _effective_share(seg: SegmentSnapshot, rows: list[SegmentSnapshot]) -> float | None:
    """分项占比（%）：数据源直接给就用；否则按同报告期收入相对占比近似。

    与 ``company_track.label._effective_share`` 同口径（tushare 分项无占比时由收入推导）。
    """
    if seg.revenue_share is not None:
        return float(seg.revenue_share)
    total = sum(row.revenue or 0.0 for row in rows)
    if total > 0 and seg.revenue is not None:
        return float(seg.revenue) / total * 100.0
    return None


def _segment_stat(rows: list[SegmentSnapshot], name: str | None) -> tuple[float | None, float | None]:
    """按分项名取 (占比%, 同比%)——名字缺失或查不到即 (None, None)。"""
    if not name:
        return None, None
    for seg in rows:
        if seg.segment_name == name:
            yoy = float(seg.revenue_yoy) if seg.revenue_yoy is not None else None
            return _effective_share(seg, rows), yoy
    return None, None


def _segment_summary(rows: list[SegmentSnapshot]) -> list[str]:
    """分部结构摘要：``["云计算/服务器 42%(+58%)", ...]``（按占比降序，截断）。"""
    ranked = sorted(
        rows,
        key=lambda seg: (_effective_share(seg, rows) or 0.0, seg.revenue or 0.0),
        reverse=True,
    )
    summary: list[str] = []
    for seg in ranked[:_SEGMENT_SUMMARY_LIMIT]:
        text = seg.segment_name
        share = _effective_share(seg, rows)
        if share is not None:
            text += f" {share:.0f}%"
        if seg.revenue_yoy is not None:
            text += f"({seg.revenue_yoy:+.0f}%)"
        summary.append(text)
    return summary


def _financial_structure(fact_values: dict[str, float]) -> dict[str, float]:
    """从 canonical ``fact_values`` 抽出财务结构（全部 canonical 键，缺失不写）。"""
    structure: dict[str, float] = {}
    for field in _FINANCIAL_STRUCTURE_FIELDS:
        value = fact_values.get(field)
        if isinstance(value, int | float):
            structure[field] = float(value)
    for field in _PEER_GROSS_MARGIN_FIELDS:
        value = fact_values.get(field)
        if isinstance(value, int | float):
            structure[_PEER_GROSS_MARGIN_KEY] = float(value)
            break
    return structure


def _degraded_profile(symbol: str, generated_at: str, reason: str) -> DriverProfile:
    """构建失败的降级画像：仍是合法 DriverProfile，下游可照常消费（读不到内容即跳过）。"""
    return DriverProfile(
        symbol=symbol,
        generated_at=generated_at,
        playbook=GENERIC_FALLBACK_ID,
        fallback=True,
        degraded=True,
        degraded_reason=reason,
    )


async def resolve_driver_profile(
    state: OrchestratorState,
    config: RunnableConfig,
) -> OrchestratorState:
    """解析公司驱动画像（DriverProfile）并落 artifact。"""
    del config
    run = state.get("run")
    symbol = run.context.get("symbol") if run else None

    step = Step(
        id="resolve_driver_profile",
        kind="resolve_driver_profile",
        inputs={"symbol": symbol},
        status=StepStatus.RUNNING,
    )

    if not symbol:
        completed = step.model_copy(update={"status": StepStatus.SKIPPED, "outputs": []})
        return {"steps": [completed]}

    artifacts = state.get("artifacts", [])
    new_issues: list[Issue] = []

    # ── 1. 读上游身份/结构信号（缺失即空，交由 router 兜底）──────────────
    # 业务含义：路由只消费「已落地产物」（INDUSTRY_CONTEXT 的申万行业/代码、COMPANY_TRACK 的
    # 真实赛道 + 分部结构、fact_values 的规范化财务事实），绝不重新取数——行业识别、业务线
    # 解构与本公司的财务口径都是前面节点的职责，本节点只做"把已有信号翻译成分析框架"，
    # 保持单一职责与可回放。
    ind = find_artifact_model(artifacts, ArtifactType.INDUSTRY_CONTEXT, IndustryContextArtifact)
    track = find_artifact_model(artifacts, ArtifactType.COMPANY_TRACK, CompanyTrackArtifact)
    fact_values: dict[str, float] = dict(state.get("fact_values") or {})

    rows = _latest_segments(track) if track else []
    dominant_share, _ = _segment_stat(rows, track.dominant_segment if track else None)
    _, fastest_yoy = _segment_stat(rows, track.fastest_segment if track else None)

    router_input = RouterInput(
        symbol=symbol,
        track_label=(track.track_label if track else ""),
        industry=(ind.industry if ind else ""),
        sub_industry=(ind.sub_industry if ind else ""),
        business_model=(track.business_model if track else ""),
        sw_code=((ind.sw_code or "") if ind else ""),
        dominant_segment=((track.dominant_segment or "") if track else ""),
        dominant_share=dominant_share,
        fastest_segment=((track.fastest_segment or "") if track else ""),
        fastest_yoy=fastest_yoy,
        segment_summary=_segment_summary(rows),
        financial_structure=_financial_structure(fact_values),
    )

    # ── 2. 路由 + 组装（恒产出，哪怕 fallback）─────────────────────────
    # 业务含义：DRIVER_PROFILE 一定产出，即使公司命中不了专用框架（回退 generic_fundamental）。
    # 这样下游 synthesize_insights 的 context 里"永远有 driver_profile 这一块"，报告主线
    # 不会因为某家公司没有专用框架而整段缺失——最坏也退回通用财务维度并显式说明。
    generated_at = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        profile = build_driver_profile(symbol, router_input, generated_at=generated_at)
    except Exception as exc:  # noqa: BLE001 —— 画像属可选增强，失败必须降级而非中断 run
        # G-10：build_driver_profile 内部要读 playbook/primitive 目录（磁盘 IO + 校验），
        # 任何异常都不该让整条 LangGraph run 崩掉；落降级画像 + MEDIUM issue 显式留痕。
        profile = _degraded_profile(
            symbol,
            generated_at,
            reason=f"driver_profile_build_failed:{type(exc).__name__}",
        )
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="driver_profile_degraded",
                message=f"公司驱动画像构建失败（{type(exc).__name__}: {exc}），回退 {profile.playbook}",
                related_step=step.id,
            )
        )

    new_artifacts = [
        Artifact(
            id=_make_id("artifact"),
            type=ArtifactType.DRIVER_PROFILE,
            producer_step=step.id,
            value=profile.model_dump(mode="json"),
        )
    ]

    # ── 3. 降级留痕：只有「身份信号全空」才算降级，普通未命中不算 ────────
    if profile.degraded and not new_issues:
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="driver_profile_degraded",
                message=(f"公司驱动画像降级（{profile.degraded_reason or '输入缺失'}），回退 {profile.playbook}"),
                related_step=step.id,
            )
        )

    completed = _finalize_step(step, new_issues, new_artifacts)
    return {
        "steps": [completed],
        "artifacts": new_artifacts,
        "issues": new_issues,
    }
