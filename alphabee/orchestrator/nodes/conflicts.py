"""Conflict-exploration node."""

from __future__ import annotations

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from alphabee.core import Artifact, Issue, IssueSeverity, Step, StepStatus
from alphabee.orchestrator.collectors import _extract_final_text, _finalize_step, _make_id
from alphabee.orchestrator.services.payload_builders import generate_explore_conflicts_prompt
from alphabee.orchestrator.state import OrchestratorState
from alphabee.utils.pipeline import parse_json


async def explore_conflicts(
    state: OrchestratorState, config: RunnableConfig,
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
        content = generate_explore_conflicts_prompt(state, query, symbol)
        agent = explore_conflicts_agent_factory()
        raw_result = await agent.ainvoke(
            {"messages": [HumanMessage(content=content)]},
            config=config,
        )
        raw_text = _extract_final_text(raw_result)
    except Exception as exc:
        new_issues.append(Issue(
            id=_make_id("issue"),
            severity=IssueSeverity.HIGH,
            category="subagent_failure",
            message=f"ExploreConflicts agent failed: {exc}",
            related_step=step.id,
        ))
        completed_step = _finalize_step(step, new_issues, new_artifacts)
        return {
            **state,
            "steps": state.get("steps", []) + [completed_step],
            "issues": state.get("issues", []) + new_issues,
        }

    parse_error: str | None = None
    if raw_text:
        try:
            parsed_dict = parse_json(raw_text)
            conflicts_result = ConflictAnalysisResult.model_validate(parsed_dict)
        except Exception as exc:
            parse_error = str(exc)
            new_issues.append(Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="parse_error",
                message=f"ConflictAnalysisResult parse failed: {exc} — raw_text saved in artifact",
                related_step=step.id,
            ))

    artifact_value: dict = {
        "symbol": symbol,
        "raw_text": raw_text[:4000] if raw_text else "",
    }
    if conflicts_result is not None:
        artifact_value["conflicts"] = conflicts_result.model_dump()
        artifact_value["conflict_count"] = len(conflicts_result.conflicts)
        hypothesis_count = sum(len(item.hypotheses) for item in conflicts_result.conflicts)
        artifact_value["hypothesis_count"] = hypothesis_count
    else:
        artifact_value["parse_error"] = parse_error or "unknown"

    new_artifacts.append(Artifact(
        id=_make_id("artifact"),
        type="conflict_analysis",
        producer_step=step.id,
        value=artifact_value,
    ))

    if conflicts_result:
        for conflict in conflicts_result.conflicts:
            if conflict.severity in ("high", "critical"):
                new_issues.append(Issue(
                    id=_make_id("issue"),
                    severity=IssueSeverity.HIGH if conflict.severity == "high" else IssueSeverity.CRITICAL,
                    category="conflict",
                    message=f"[冲突] {conflict.theme}: {conflict.description}",
                    related_step=step.id,
                ))

    completed_step = _finalize_step(step, new_issues, new_artifacts)
    return {
        **state,
        "steps": state.get("steps", []) + [completed_step],
        "issues": state.get("issues", []) + new_issues,
        "artifacts": state.get("artifacts", []) + new_artifacts,
        "conflicts_result": conflicts_result.model_dump() if conflicts_result else None,
    }

