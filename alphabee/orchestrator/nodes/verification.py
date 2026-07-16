"""Hypothesis-verification node."""

from __future__ import annotations

import asyncio
import json as _json

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from alphabee.core import Artifact, Issue, IssueSeverity, Step, StepStatus
from alphabee.orchestrator.collectors import _extract_final_text, _finalize_step, _make_id
from alphabee.orchestrator.services.payload_builders import build_verify_context
from alphabee.orchestrator.state import OrchestratorState
from alphabee.utils.pipeline import parse_json


async def _verify_single_conflict(
    conflict: "ConflictItem",
    shared_context: dict,
    step_id: str,
    config: RunnableConfig,
) -> "tuple[list[VerificationResultItem], list[Issue]]":
    from alphabee.agents.schemas import VerificationResultList, VerificationResultItem
    from alphabee.agents.verify_hypotheses.agent import verify_hypotheses_agent_factory
    from alphabee.agents.verify_hypotheses.prompts import VERIFY_HYPOTHESES_USER_TEMPLATE

    issues: list[Issue] = []
    if not conflict.hypotheses:
        return [], issues

    hypotheses_json = _json.dumps(
        [hypothesis.model_dump() for hypothesis in conflict.hypotheses],
        ensure_ascii=False,
        indent=2,
    )
    ctx = {
        **shared_context,
        "conflict_theme": conflict.theme,
        "conflict_severity": conflict.severity,
    }
    context_json = _json.dumps(ctx, ensure_ascii=False, indent=2)
    user_msg = VERIFY_HYPOTHESES_USER_TEMPLATE.format(
        hypotheses_json=hypotheses_json,
        context_json=context_json,
    )

    try:
        agent = verify_hypotheses_agent_factory()
        raw_result = await agent.ainvoke(
            {"messages": [HumanMessage(content=user_msg)]},
            config=config,
        )
        raw_text = _extract_final_text(raw_result)
    except Exception as exc:
        issues.append(Issue(
            id=_make_id("issue"),
            severity=IssueSeverity.HIGH,
            category="subagent_failure",
            message=f"VerifyHypotheses agent failed for conflict '{conflict.theme}': {exc}",
            related_step=step_id,
        ))
        return [], issues

    if not raw_text:
        return [], issues

    try:
        parsed = parse_json(raw_text)
        if isinstance(parsed, list):
            parsed = {"results": parsed}
        vlist = VerificationResultList.model_validate(parsed)
        return vlist.results, issues
    except Exception as exc:
        issues.append(Issue(
            id=_make_id("issue"),
            severity=IssueSeverity.MEDIUM,
            category="parse_error",
            message=f"VerificationResultList parse failed for conflict '{conflict.theme}': {exc}",
            related_step=step_id,
        ))
        return [], issues


async def verify_hypotheses(
    state: OrchestratorState, config: RunnableConfig,
) -> OrchestratorState:
    """Verify hypotheses from explore_conflicts in parallel."""
    from alphabee.agents.schemas import ConflictAnalysisResult, VerificationResultItem

    run = state.get("run")
    symbol = run.context.get("symbol") if run else None

    step = Step(
        id="verify_hypotheses",
        kind="verify_hypotheses",
        inputs={"symbol": symbol},
        status=StepStatus.RUNNING,
    )
    new_artifacts: list[Artifact] = []
    new_issues: list[Issue] = []

    conflicts_raw = state.get("conflicts_result")
    if not conflicts_raw:
        completed_step = step.model_copy(update={"status": StepStatus.SKIPPED, "outputs": []})
        return {**state, "steps": state.get("steps", []) + [completed_step]}

    try:
        conflicts_result = ConflictAnalysisResult.model_validate(conflicts_raw)
    except Exception as exc:
        new_issues.append(Issue(
            id=_make_id("issue"),
            severity=IssueSeverity.MEDIUM,
            category="parse_error",
            message=f"verify_hypotheses: conflicts_result parse failed: {exc}",
            related_step=step.id,
        ))
        completed_step = _finalize_step(step, new_issues, new_artifacts)
        return {
            **state,
            "steps": state.get("steps", []) + [completed_step],
            "issues": state.get("issues", []) + new_issues,
        }

    all_hypotheses = [hypothesis for conflict in conflicts_result.conflicts for hypothesis in conflict.hypotheses]
    if not all_hypotheses:
        completed_step = step.model_copy(update={"status": StepStatus.SKIPPED, "outputs": []})
        return {**state, "steps": state.get("steps", []) + [completed_step]}

    shared_context = build_verify_context(state, symbol)
    tasks = [
        _verify_single_conflict(conflict, shared_context, step.id, config)
        for conflict in conflicts_result.conflicts
        if conflict.hypotheses
    ]
    task_results: list[tuple[list[VerificationResultItem], list[Issue]]] = (
        await asyncio.gather(*tasks)
    )

    all_results: list[VerificationResultItem] = []
    for results, issues in task_results:
        all_results.extend(results)
        new_issues.extend(issues)

    result_by_hid = {result.hypothesis_id: result for result in all_results}
    for conflict in conflicts_result.conflicts:
        for hypothesis in conflict.hypotheses:
            if hypothesis.id in result_by_hid:
                hypothesis.status = result_by_hid[hypothesis.id].status

    verified_ids = {hid for hid, result in result_by_hid.items() if result.status in ("verified", "partial")}
    rejected_ids = {hid for hid, result in result_by_hid.items() if result.status == "rejected"}

    new_artifacts.append(Artifact(
        id=_make_id("artifact"),
        type="verification_results",
        producer_step=step.id,
        value={
            "symbol": symbol,
            "results": [result.model_dump() for result in all_results],
            "verified_count": len(verified_ids),
            "rejected_count": len(rejected_ids),
            "unknown_count": len(all_hypotheses) - len(verified_ids) - len(rejected_ids),
        },
    ))

    completed_step = _finalize_step(step, new_issues, new_artifacts)
    return {
        **state,
        "steps": state.get("steps", []) + [completed_step],
        "issues": state.get("issues", []) + new_issues,
        "artifacts": state.get("artifacts", []) + new_artifacts,
        "conflicts_result": conflicts_result.model_dump(),
        "verification_results": [result.model_dump() for result in all_results],
    }

