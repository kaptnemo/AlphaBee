"""resolve_driver_profile node — DOMAIN_CONTEXT P0 第 5 步在线注入。

职责（只路由 + 注入，不重复取数，研究内核在 domain_context）：
1. 读 ``INDUSTRY_CONTEXT`` + ``COMPANY_TRACK`` artifact → 构造 ``RouterInput``；
2. ``ContextRouter.route`` → ``build_driver_profile`` → 落 ``DRIVER_PROFILE`` artifact；
3. 降级契约（显式留痕，不静默）：
   - 身份信号全空 → ``generic_fundamental`` 兜底 + ``degraded=True`` + MEDIUM issue；
   - 普通未命中（有信号但无专用框架）→ ``generic_fundamental`` 兜底 + ``fallback=True``（非异常，不产 issue）。

默认不阻塞：``DRIVER_PROFILE`` 恒产出（哪怕 fallback），下游 ``synthesize_insights`` 总是能
消费到一个 driver_profile 上下文。
"""

from __future__ import annotations

from datetime import UTC, datetime

from langchain_core.runnables import RunnableConfig

from alphabee.company_track.contracts import CompanyTrackArtifact
from alphabee.core import Artifact, ArtifactType, Issue, IssueSeverity, Step, StepStatus
from alphabee.domain_context import RouterInput, build_driver_profile
from alphabee.orchestrator.collectors import _finalize_step, _make_id
from alphabee.orchestrator.contracts import IndustryContextArtifact, find_artifact_model
from alphabee.orchestrator.state import OrchestratorState


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

    # ── 1. 读上游身份信号（缺失即 None，交由 router 兜底）────────────────
    # 业务含义：路由只消费「已落地产物」（INDUSTRY_CONTEXT 的申万行业、COMPANY_TRACK 的
    # 真实赛道 + archetype），绝不重新取数——行业识别和业务线解构是前面两个节点的职责，
    # 本节点只做"把已有身份信号翻译成分析框架"，保持单一职责与可回放。
    ind = find_artifact_model(artifacts, ArtifactType.INDUSTRY_CONTEXT, IndustryContextArtifact)
    track = find_artifact_model(artifacts, ArtifactType.COMPANY_TRACK, CompanyTrackArtifact)

    router_input = RouterInput(
        symbol=symbol,
        track_label=(track.track_label if track else ""),
        industry=(ind.industry if ind else ""),
        sub_industry=(ind.sub_industry if ind else ""),
        business_model=(track.business_model if track else ""),
    )

    # ── 2. 路由 + 组装（恒产出，哪怕 fallback）─────────────────────────
    # 业务含义：DRIVER_PROFILE 一定产出，即使公司命中不了专用框架（回退 generic_fundamental）。
    # 这样下游 synthesize_insights 的 context 里"永远有 driver_profile 这一块"，报告主线
    # 不会因为某家公司没有专用框架而整段缺失——最坏也退回通用财务维度并显式说明。
    profile = build_driver_profile(
        symbol,
        router_input,
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
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
    if profile.degraded:
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
