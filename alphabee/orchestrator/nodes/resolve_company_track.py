"""resolve_company_track node — COMPANY_TRACK Phase D3 在线注入。

职责（只注入，不做完整赛道研究，完整研究走离线 build_company_track / Phase C）：
1. 从对标组存储（``data/peer_groups/{symbol}.json``）读取该标的的对标组代码；
2. 无对标组 → 发 ``company_track_missing``（MEDIUM）issue，**不发 artifact**、
   不注入 peer_*（回退申万基线——静默回退不可观测，必须显式留痕）；
3. 有对标组 → ``derive_peer_benchmarks`` 计算 peer_* 基准：写 ``COMPANY_TRACK``
   artifact（含 peer_benchmarks / 血缘 / 降级标记），数值注入 ``fact_values``
   （None 不注入——缺失即回退 industry_* → 绝对阈值，见 registry.py 回退链）。

注入的 ``peer_*`` canonical 字段：peer_avg_roe / peer_avg_debt_ratio /
peer_avg_gross_margin / peer_revenue_yoy / peer_median_pe_ttm / peer_median_pb。
"""

from __future__ import annotations

from datetime import UTC, datetime

from langchain_core.runnables import RunnableConfig

from alphabee.core import Artifact, ArtifactType, Issue, IssueSeverity, Step, StepStatus
from alphabee.orchestrator.collectors import _finalize_step, _make_id
from alphabee.orchestrator.state import OrchestratorState


async def resolve_company_track(
    state: OrchestratorState,
    config: RunnableConfig,
) -> OrchestratorState:
    """解析公司赛道对标组并注入 peer_* 基准（无对标组 → 显式降级留痕）。"""
    del config
    run = state.get("run")
    symbol = run.context.get("symbol") if run else None

    step = Step(
        id="resolve_company_track",
        kind="resolve_company_track",
        inputs={"symbol": symbol},
        status=StepStatus.RUNNING,
    )

    if not symbol:
        completed = step.model_copy(update={"status": StepStatus.SKIPPED, "outputs": []})
        return {"steps": [completed]}

    # ── 1. 读对标组存储 ─────────────────────────────────────────────
    from alphabee.company_track.peer_group_store import PeerGroupStore

    peer_group = PeerGroupStore().load(symbol)
    if peer_group is None or peer_group.is_empty():
        # 无对标组 → 显式降级：回退申万基线（industry_* / 绝对阈值）
        new_issues = [
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="company_track_missing",
                message=f"标的 {symbol} 无对标组配置（data/peer_groups/{symbol}.json），回退申万行业基线",
                related_step=step.id,
            )
        ]
        completed = step.model_copy(update={"status": StepStatus.SKIPPED, "outputs": []})
        return {"steps": [completed], "issues": new_issues}

    # ── 2. 对标组基准计算（best-effort）─────────────────────────────
    from alphabee.company_track.peer import derive_peer_benchmarks

    peer_values, meta = derive_peer_benchmarks(peer_group.codes, industry=peer_group.name)
    new_issues: list[Issue] = []
    degraded = bool(meta.get("error")) or not peer_values

    if degraded:
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="peer_group_benchmarks_missing",
                message=f"对标组基准计算失败（degraded）: {meta.get('error') or '无可用基准'}",
                related_step=step.id,
            )
        )

    artifact_value = {
        "schema_version": "1",
        "symbol": symbol,
        "as_of_date": meta.get("as_of_date", ""),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "peer_group": peer_group.codes,
        "peer_group_source": peer_group.source,
        "peer_group_name": peer_group.name,
        "peer_benchmarks": peer_values,
        "peer_count": meta.get("peer_count", 0),
        "fetched_codes": meta.get("fetched_codes", []),
        "source_refs": meta.get("source_refs", []),
        "degraded": degraded,
        "degraded_reason": meta.get("error") or "",
    }
    new_artifacts = [
        Artifact(
            id=_make_id("artifact"),
            type=ArtifactType.COMPANY_TRACK,
            producer_step=step.id,
            value=artifact_value,
        )
    ]

    completed = _finalize_step(step, new_issues, new_artifacts)
    return {
        "steps": [completed],
        "artifacts": new_artifacts,
        "issues": new_issues,
        # None 不注入：缺失即回退 industry_* → 绝对阈值（registry.py 回退链）
        "fact_values": peer_values,
    }
