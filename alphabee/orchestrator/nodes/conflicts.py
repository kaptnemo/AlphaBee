"""Conflict-exploration node."""

from __future__ import annotations

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from alphabee.core import Artifact, ArtifactType, Issue, IssueSeverity, Step, StepStatus
from alphabee.orchestrator.collectors import _extract_final_text, _finalize_step, _make_id
from alphabee.orchestrator.contracts import ConflictAnalysisArtifact
from alphabee.orchestrator.services.payload_builders import generate_explore_conflicts_prompt
from alphabee.orchestrator.state import OrchestratorState
from alphabee.utils.pipeline import parse_json


async def explore_conflicts(
    state: OrchestratorState,
    config: RunnableConfig,
) -> OrchestratorState:
    """Run the ConflictExplorer agent to identify gaps and conflicts."""
    from alphabee.agents.explore_conflicts.agent import explore_conflicts_agent_factory
    from alphabee.agents.schemas import ConflictAnalysisResult

    run = state.get("run")
    symbol = run.context.get("symbol") if run else None
    query = run.context.get("query", "") if run else ""

    step = Step(
        id="explore_conflicts",
        kind="explore_conflicts",
        inputs={"symbol": symbol, "query": query},
        status=StepStatus.RUNNING,
    )

    new_artifacts: list[Artifact] = []
    new_issues: list[Issue] = []
    conflicts_result: ConflictAnalysisResult | None = None
    raw_text = ""

    try:
        # 冲突探索节点的职责不是再做一遍 thesis，
        # 而是专门寻找“表面看起来不错，但内部逻辑互相打架”的地方。
        # 输入只给结构化摘要，目的是让 agent 聚焦于背离/矛盾/缺口，而不是自由发散。
        content = generate_explore_conflicts_prompt(state, query, symbol)
        agent = explore_conflicts_agent_factory()
        raw_result = await agent.ainvoke(
            {"messages": [HumanMessage(content=content)]},
            config=config,
        )
        raw_text = _extract_final_text(raw_result)
    except Exception as exc:
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.HIGH,
                category="subagent_failure",
                message=f"ExploreConflicts agent failed: {exc}",
                related_step=step.id,
            )
        )
        completed_step = _finalize_step(step, new_issues, new_artifacts)
        return {
            "steps": [completed_step],
            "issues": new_issues,
        }

    parse_error: str | None = None
    if raw_text:
        try:
            parsed_dict = parse_json(raw_text)
            conflicts_result = ConflictAnalysisResult.model_validate(parsed_dict)
        except Exception as exc:
            parse_error = str(exc)
            new_issues.append(
                Issue(
                    id=_make_id("issue"),
                    severity=IssueSeverity.MEDIUM,
                    category="parse_error",
                    message=f"ConflictAnalysisResult parse failed: {exc} — raw_text saved in artifact",
                    related_step=step.id,
                )
            )

    artifact_payload = ConflictAnalysisArtifact(
        symbol=symbol,
        raw_text=raw_text[:4000] if raw_text else "",
    )
    if conflicts_result is not None:
        # artifact 除了保存结构化结果，也保留基础计数，
        # 方便后续 thesis / report / evaluation 快速判断冲突密度。
        artifact_payload.conflicts = list(conflicts_result.conflicts)
        artifact_payload.conflict_count = len(conflicts_result.conflicts)
        hypothesis_count = sum(len(item.hypotheses) for item in conflicts_result.conflicts)
        artifact_payload.hypothesis_count = hypothesis_count
    else:
        artifact_payload.parse_error = parse_error or "unknown"

    new_artifacts.append(
        Artifact(
            id=_make_id("artifact"),
            type=ArtifactType.CONFLICT_ANALYSIS,
            producer_step=step.id,
            value=artifact_payload.model_dump(mode="json"),
        )
    )

    # 冲突生命周期分层（ROADMAP 0.5）：
    # 探索阶段的输出全部是 provisional（候选冲突 / 待验证假设），
    # 即使 severity 很高，也**不在这里直接升格为 issue**——
    # 否则下游 thesis / report / quality gate 会把“待验证怀疑”当成“已成立问题”。
    # 真正的升格发生在 verify_hypotheses 的结算层：只有 verified / partial
    # 的高严重度冲突才会进入 issue（进而被 gate 要求披露），
    # rejected 假设沉淀为 decision，unknown 保留为证据缺口。
    if conflicts_result is not None:
        new_artifacts.append(
            Artifact(
                id=_make_id("artifact"),
                type=ArtifactType.CONFLICTS_RESULT,
                producer_step=step.id,
                value=conflicts_result.model_dump(mode="json"),
            )
        )
    completed_step = _finalize_step(step, new_issues, new_artifacts)
    return {
        "steps": [completed_step],
        "issues": new_issues,
        "artifacts": new_artifacts,
    }
