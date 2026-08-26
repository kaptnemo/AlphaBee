"""resolve_company_track node — COMPANY_TRACK Phase F 在线注入（完整赛道）。

职责（只解析 + 注入，完整研究走离线 build_company_track）：
1. 组装完整 ``CompanyTrackArtifact``（业务线分项 + 真实赛道标签 + 商业模式 + 漂移）；
2. 读对标组存储 → 计算并注入 ``peer_*`` 基准（fact_values），写 COMPANY_TRACK artifact；
3. 降级分级（显式留痕，不静默）：

| 场景 | 产物 | issue |
|---|---|---|
| 无业务线数据（无 track） | 无 artifact | ``company_track_missing``（MEDIUM） |
| 有 track 但无对标组 | 全量 artifact（无 peer_*） | ``peer_group_missing``（LOW） |
| 对标组计算失败 | artifact（degraded）+ 无 peer_* | ``peer_group_benchmarks_missing``（MEDIUM） |
| track 过期 | artifact（stale=True） | ``company_track_stale``（MEDIUM，进报告披露检查） |

注入的 ``peer_*`` canonical 字段：peer_avg_roe / peer_avg_debt_ratio / peer_avg_gross_margin /
peer_revenue_yoy / peer_median_pe_ttm / peer_median_pb。
"""

from __future__ import annotations

from datetime import date
from typing import cast

from langchain_core.runnables import RunnableConfig

from alphabee.core import Artifact, ArtifactType, Issue, IssueSeverity, Step, StepStatus
from alphabee.orchestrator.collectors import _finalize_step, _make_id
from alphabee.orchestrator.state import OrchestratorState


def _is_stale(stale_after: str | None) -> bool:
    if not stale_after:
        return False
    try:
        return date.today() > date.fromisoformat(stale_after)
    except ValueError:
        return False


async def resolve_company_track(
    state: OrchestratorState,
    config: RunnableConfig,
) -> OrchestratorState:
    """解析公司赛道并注入 peer_* 基准（无 track → 显式降级留痕）。"""
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

    artifacts = state.get("artifacts", [])
    new_issues: list[Issue] = []

    # ── 0. 申万基线（B3 并存：公司赛道为修正字段）────────────────
    from alphabee.orchestrator.contracts import IndustryContextArtifact, find_artifact_model

    ind = find_artifact_model(artifacts, ArtifactType.INDUSTRY_CONTEXT, IndustryContextArtifact)
    sw_industry = ind.industry or "" if ind is not None else ""
    sw_code = ind.sw_code or "" if ind is not None else ""

    # ── 1. 完整赛道（segments + track_label + business_model，best-effort）──
    from alphabee.company_track import build_company_track

    track = build_company_track(symbol, use_llm=True, sw_industry=sw_industry, sw_code=sw_code)
    if track.degraded or not track.segments:
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="company_track_missing",
                message=f"标的 {symbol} 无业务线数据（{track.degraded_reason or '未知原因'}），回退申万行业基线",
                related_step=step.id,
            )
        )
        completed = step.model_copy(update={"status": StepStatus.SKIPPED, "outputs": []})
        return {"steps": [completed], "issues": new_issues}

    # ── 2. 对标组基准（有对标组 → peer_* 注入）───────────────────
    from alphabee.company_track.peer_group_store import PeerGroupStore

    peer_group = PeerGroupStore().load(symbol)
    peer_values: dict[str, float] = {}
    if peer_group is not None and not peer_group.is_empty():
        from alphabee.company_track import derive_peer_benchmarks

        peer_values, meta = derive_peer_benchmarks(peer_group.codes, industry=peer_group.name)
        track.peer_group = peer_group.codes
        track.peer_group_source = peer_group.source
        track.peer_benchmarks = cast(dict[str, float | None], peer_values)
        if meta.get("error") or not peer_values:
            track.degraded = True
            track.degraded_reason = meta.get("error") or "对标组基准不可得"
            new_issues.append(
                Issue(
                    id=_make_id("issue"),
                    severity=IssueSeverity.MEDIUM,
                    category="peer_group_benchmarks_missing",
                    message=f"对标组基准计算失败: {meta.get('error') or '无可用基准'}",
                    related_step=step.id,
                )
            )
    else:
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.LOW,
                category="peer_group_missing",
                message=f"标的 {symbol} 无对标组配置（data/peer_groups/{symbol}.json），赛道判断仅用申万基线",
                related_step=step.id,
            )
        )

    # ── 3. 过期标记（进报告披露检查）─────────────────────────────
    if _is_stale(track.stale_after):
        track.stale = True
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="company_track_stale",
                message=f"公司赛道数据截至 {track.as_of_date}（已过期），报告需显式提示",
                related_step=step.id,
            )
        )

    new_artifacts = [
        Artifact(
            id=_make_id("artifact"),
            type=ArtifactType.COMPANY_TRACK,
            producer_step=step.id,
            value=track.model_dump(mode="json"),
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
