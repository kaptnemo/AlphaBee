"""Insight synthesis node — runs InsightAgent between verification and thesis."""

from __future__ import annotations

import json as _json

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from alphabee.core import Artifact, ArtifactType, Issue, IssueSeverity, Step, StepStatus
from alphabee.orchestrator.collectors import _extract_final_text, _finalize_step, _make_id
from alphabee.orchestrator.contracts import InsightArtifact
from alphabee.orchestrator.services.payload_builders import build_insight_context
from alphabee.orchestrator.state import OrchestratorState
from alphabee.utils.pipeline import parse_json


async def synthesize_insights(
    state: OrchestratorState,
    config: RunnableConfig,
) -> OrchestratorState:
    """Run the InsightAgent to synthesize upstream findings into a central viewpoint.

    This node consumes signals, anomalies, conflicts, and verification results
    and produces an ``insight_analysis`` artifact that downstream thesis and
    report nodes can use as their narrative backbone.

    Insertion point: verify_hypotheses → synthesize_insights → run_thesis
    """
    from alphabee.agents.insights.agent import insight_agent_factory
    from alphabee.agents.insights.models import InsightOutput
    from alphabee.agents.insights.prompts import INSIGHT_AGENT_USER_TEMPLATE

    run = state.get("run")
    symbol = run.context.get("symbol") if run else None

    step = Step(
        id="synthesize_insights",
        kind="synthesize_insights",
        inputs={"symbol": symbol},
        status=StepStatus.RUNNING,
    )

    new_artifacts: list[Artifact] = []
    new_issues: list[Issue] = []

    # ── Build context ────────────────────────────────────────────────
    try:
        context = build_insight_context(state, symbol)
    except Exception as exc:
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="context_build_failure",
                message=f"Failed to build insight context: {exc}",
                related_step=step.id,
            )
        )
        completed_step = _finalize_step(step, new_issues, new_artifacts)
        return {
            **state,
            "steps": state.get("steps", []) + [completed_step],
            "issues": state.get("issues", []) + new_issues,
        }

    # ── Run InsightAgent ─────────────────────────────────────────────
    try:
        context_json = _json.dumps(context, ensure_ascii=False, indent=2)
        user_msg = INSIGHT_AGENT_USER_TEMPLATE.substitute(context_json=context_json)
        agent = insight_agent_factory()
        raw_result = await agent.ainvoke(
            {"messages": [HumanMessage(content=user_msg)]},
            config=config,
        )
        raw_text = _extract_final_text(raw_result)
    except Exception as exc:
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.HIGH,
                category="subagent_failure",
                message=f"InsightAgent failed: {exc}",
                related_step=step.id,
            )
        )
        completed_step = _finalize_step(step, new_issues, new_artifacts)
        return {
            **state,
            "steps": state.get("steps", []) + [completed_step],
            "issues": state.get("issues", []) + new_issues,
        }

    # ── Parse output ─────────────────────────────────────────────────
    if not raw_text:
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="empty_response",
                message="InsightAgent returned empty response.",
                related_step=step.id,
            )
        )
        completed_step = _finalize_step(step, new_issues, new_artifacts)
        return {
            **state,
            "steps": state.get("steps", []) + [completed_step],
            "issues": state.get("issues", []) + new_issues,
        }

    try:
        parsed = parse_json(raw_text)
        insight_output = InsightOutput.model_validate(parsed)

        artifact_payload = InsightArtifact(
            core_view=insight_output.core_view,
            central_tension=insight_output.central_tension,
            main_driver=insight_output.main_driver,
            supporting_evidence=[e.model_dump(mode="json") for e in insight_output.supporting_evidence],
            counter_evidence=[e.model_dump(mode="json") for e in insight_output.counter_evidence],
            materiality_rank=[m.model_dump(mode="json") for m in insight_output.materiality_rank],
            business_model_context=insight_output.business_model_context,
            base_case=insight_output.base_case,
            bull_case=insight_output.bull_case,
            bear_case=insight_output.bear_case,
            what_would_change_my_mind=list(insight_output.what_would_change_my_mind),
            confidence=insight_output.confidence,
        )

        new_artifacts.append(
            Artifact(
                id=_make_id("artifact"),
                type=ArtifactType.INSIGHT_ANALYSIS,
                producer_step=step.id,
                value=artifact_payload.model_dump(mode="json"),
            )
        )
    except Exception as exc:
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="parse_error",
                message=f"InsightOutput parse failed: {exc}",
                related_step=step.id,
            )
        )

    completed_step = _finalize_step(step, new_issues, new_artifacts)
    return {
        **state,
        "steps": state.get("steps", []) + [completed_step],
        "artifacts": state.get("artifacts", []) + new_artifacts,
        "issues": state.get("issues", []) + new_issues,
    }
