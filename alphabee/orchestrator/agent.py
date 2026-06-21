"""Orchestrator — top-level entry point for AlphaBee.

Simplified pipeline:
1. collect_raw_facts      — FactCollector + structured model extraction (concurrent)
2. run_analysis_engines   — DerivedFacts + SignalEngine + AnomalyEngine
3. run_thesis             — ThesisEngine + optional LLM enhancement
4. review_thesis          — ThesisReviewer (deterministic + optional LLM audit)
5. generate_report        — Single LLM call: structured data → Markdown report
6. finalize               — Merge all results into JSON AIMessage
"""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.store.memory import InMemoryStore

from alphabee.core import (
    Artifact,
    Decision,
    Issue,
    IssueSeverity,
    Step,
    StepStatus,
)
from alphabee.orchestrator.collectors import (
    _build_company_context,
    collect_raw_facts,
    run_analysis_engines,
    run_thesis,
)
from alphabee.orchestrator.reporter import generate_report
from alphabee.orchestrator.state import OrchestratorState
from alphabee.utils.pipeline import make_id


def _make_id(prefix: str) -> str:
    return make_id(prefix)


# ── review_thesis node ──────────────────────────────────────────────────────


async def review_thesis(
    state: OrchestratorState, config: RunnableConfig,
) -> OrchestratorState:
    """Audit the thesis_analysis artifact for analytical quality.

    Runs the ThesisReviewer (Layer 1 deterministic + optional Layer 2 LLM)
    and produces Decisions and Issues for each dimension verdict.
    """
    from alphabee.agents.thesis.models import InvestmentThesis
    from alphabee.agents.thesis.registry import load_dimension_defs
    from alphabee.agents.thesis.reviewer import ThesisReviewer

    load_dimension_defs()

    artifacts = state.get("artifacts", [])
    issues = list(state.get("issues", []))
    decisions = list(state.get("decisions", []))
    steps = list(state.get("steps", []))
    run = state.get("run")

    step = Step(
        id="review_thesis",
        kind="review_thesis",
        inputs={"artifact_count": len(artifacts)},
        status=StepStatus.RUNNING,
    )

    # ── Find thesis_analysis artifact ──
    thesis_dict = _find_artifact_value(artifacts, "thesis_analysis")
    if thesis_dict is None:
        completed_step = step.model_copy(
            update={
                "status": StepStatus.SKIPPED,
                "outputs": [],
            }
        )
        return {
            **state,
            "steps": [*steps, completed_step],
        }

    # ── Reconstruct InvestmentThesis from artifact dict ──
    thesis = _reconstruct_thesis(thesis_dict)

    # ── Get signal_results for detail-level review ──
    signal_val = _find_artifact_value(artifacts, "signal_analysis")
    signal_results = signal_val.get("results", {}) if signal_val else {}

    # ── Get company context ──
    fact_val = _find_artifact_value(artifacts, "fact_collection")
    fact_text = fact_val.get("raw_response", "") if fact_val else ""
    company_ctx = _build_company_context(
        symbol=thesis.symbol if thesis else "",
        fact_text=fact_text,
        financial_facts=state.get("financial_facts"),
        market_facts=state.get("market_facts"),
    )

    # ── Run reviewer ──
    reviewer = ThesisReviewer()
    use_llm = state.get("llm_review", False)
    review = reviewer.review(
        thesis=thesis,
        signal_results=signal_results,
        company_context=company_ctx,
        use_llm=use_llm,
    )

    # ── Produce Decisions per dimension ──
    for dim_id, verdict in review.dimension_verdicts.items():
        decisions.append(Decision(
            id=_make_id("decision"),
            maker="thesis_reviewer",
            rationale=(
                f"{verdict.dimension_name}: {verdict.status}. "
                f"证据数={verdict.evidence_count}, "
                f"建议={verdict.suggested_action}. "
                + "; ".join(verdict.issues) if verdict.issues else ""
            ),
            confidence={
                "confirmed": 0.9,
                "qualified": 0.7,
                "insufficient": 0.3,
                "contested": 0.2,
            }.get(verdict.status, 0.5),
        ))

    # ── Produce Issues ──
    for msg in review.blocking_issues:
        issues.append(Issue(
            id=_make_id("issue"),
            severity=IssueSeverity.HIGH,
            category="thesis_gap",
            message=msg,
            related_step=step.id,
        ))
    for msg in review.warning_issues:
        issues.append(Issue(
            id=_make_id("issue"),
            severity=IssueSeverity.MEDIUM,
            category="thesis_warning",
            message=msg,
            related_step=step.id,
        ))

    # ── Produce thesis_review Artifact ──
    review_artifact = Artifact(
        id=_make_id("artifact"),
        type="thesis_review",
        producer_step=step.id,
        value=review.to_dict(),
    )

    completed_step = step.model_copy(
        update={
            "status": StepStatus.SUCCEEDED,
            "outputs": [review_artifact.id],
        }
    )

    return {
        **state,
        "steps": [*steps, completed_step],
        "artifacts": [*artifacts, review_artifact],
        "decisions": decisions,
        "issues": issues,
    }


# ── finalize ────────────────────────────────────────────────────────────────


def finalize_message(state: OrchestratorState) -> OrchestratorState:
    """Merge all artifacts into a final JSON AIMessage for streaming output."""
    artifacts = state.get("artifacts", [])
    final_artifact_id = state.get("final_artifact_id")

    final_artifact = next(
        (a for a in artifacts if a.id == final_artifact_id), None
    )
    if final_artifact is None:
        final_artifact = next(
            (a for a in reversed(artifacts) if a.type == "report"), None
        )

    payload = {
        "run": state["run"].model_dump(mode="json") if state.get("run") else None,
        "final_report": (
            final_artifact.value if final_artifact is not None else None
        ),
        "artifacts": [a.model_dump(mode="json") for a in artifacts],
        "decisions": [
            d.model_dump(mode="json") for d in state.get("decisions", [])
        ],
        "issues": [
            i.model_dump(mode="json") for i in state.get("issues", [])
        ],
    }

    return {
        **state,
        "messages": [
            AIMessage(
                content=json.dumps(payload, ensure_ascii=False, indent=2)
            )
        ],
    }


# ── helpers ─────────────────────────────────────────────────────────────────


def _find_artifact_value(artifacts: list, artifact_type: str) -> dict | None:
    for a in reversed(artifacts):
        if a.type == artifact_type and isinstance(a.value, dict):
            return a.value
    return None


def _reconstruct_thesis(thesis_dict: dict):
    """Reconstruct an InvestmentThesis from the dict stored in the artifact."""
    from alphabee.agents.thesis.models import InvestmentThesis
    return InvestmentThesis.from_dict(thesis_dict)


# ── graph assembly ──────────────────────────────────────────────────────────


_graph = StateGraph(OrchestratorState)

_graph.add_node("collect_raw_facts", collect_raw_facts)
_graph.add_node("run_analysis_engines", run_analysis_engines)
_graph.add_node("run_thesis", run_thesis)
_graph.add_node("review_thesis", review_thesis)
_graph.add_node("generate_report", generate_report)
_graph.add_node("finalize_message", finalize_message)

_graph.add_edge(START, "collect_raw_facts")
_graph.add_edge("collect_raw_facts", "run_analysis_engines")
_graph.add_edge("run_analysis_engines", "run_thesis")
_graph.add_edge("run_thesis", "review_thesis")
_graph.add_edge("review_thesis", "generate_report")
_graph.add_edge("generate_report", "finalize_message")
_graph.add_edge("finalize_message", END)

alphabee_agent = _graph.compile(store=InMemoryStore())
