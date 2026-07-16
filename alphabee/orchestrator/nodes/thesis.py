"""Thesis-generation node."""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig

from alphabee.agents.thesis.engine import ThesisEngine
from alphabee.agents.thesis.enhancer import ThesisEnhancer
from alphabee.core import Artifact, Issue, IssueSeverity, Step, StepStatus
from alphabee.orchestrator.collectors import _build_conflict_data, _finalize_step, _find_artifact, _make_id
from alphabee.orchestrator.services.company_context import build_company_context
from alphabee.orchestrator.state import OrchestratorState


async def run_thesis(
    state: OrchestratorState, config: RunnableConfig,
) -> OrchestratorState:
    """Run ThesisEngine on signal results, with optional LLM enhancement."""
    del config
    run = state.get("run")
    symbol = run.context.get("symbol") if run else None
    query = run.context.get("query", "") if run else ""
    financial_facts = state.get("financial_facts")
    market_facts = state.get("market_facts")
    enhance = state.get("enhance", False)

    step = Step(
        id="run_thesis",
        kind="run_thesis",
        inputs={"symbol": symbol},
        status=StepStatus.RUNNING,
    )

    new_artifacts: list[Artifact] = []
    new_issues: list[Issue] = []

    signal_av = _find_artifact(state.get("artifacts", []), "signal_analysis")
    signal_results: dict = signal_av.get("results", {}) if signal_av else {}

    fc_av = _find_artifact(state.get("artifacts", []), "fact_collection")
    fact_text: str = fc_av.get("raw_response", "") if fc_av else ""

    anomaly_av = _find_artifact(state.get("artifacts", []), "anomaly_report")
    anomaly_data: dict = {}
    if anomaly_av:
        anomaly_data = {
            "anomaly_count": anomaly_av.get("anomaly_count", 0),
            "pattern_count": anomaly_av.get("pattern_count", 0),
            "anomalies": [
                item for item in anomaly_av.get("anomalies", [])
                if item.get("level") != "none"
            ],
            "pattern_matches": anomaly_av.get("pattern_matches", []),
        }

    if not signal_results:
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="missing_data",
                message="No signal results available — skipping ThesisEngine.",
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
        period = "latest"
        if financial_facts is not None and financial_facts.snapshots:
            snap_period = financial_facts.snapshots[0].period
            if snap_period:
                period = snap_period

        thesis_engine = ThesisEngine()
        thesis = thesis_engine.run(
            symbol=symbol or "unknown",
            period=period,
            signal_results=signal_results,
        )

        company_ctx = build_company_context(
            symbol=symbol,
            fact_text=fact_text,
            financial_facts=financial_facts,
            market_facts=market_facts,
        )

        enhanced = None
        if enhance:
            try:
                enhancer = ThesisEnhancer()
                enhanced = enhancer.enhance(
                    thesis=thesis,
                    signal_results=signal_results,
                    company_context=company_ctx,
                    user_intent=query,
                    fact_summary=fact_text[:2000] if fact_text else "",
                )
            except Exception:
                pass

        new_artifacts.append(
            Artifact(
                id=_make_id("artifact"),
                type="thesis_analysis",
                producer_step=step.id,
                value={
                    "thesis": thesis.to_dict(),
                    "enhanced": enhanced.to_dict() if enhanced else None,
                    "industry_context": {
                        "industry": company_ctx.industry,
                        "sub_industry": company_ctx.sub_industry,
                        "market_cap_category": company_ctx.market_cap_category,
                        "lifecycle_stage": company_ctx.lifecycle_stage,
                        "business_model_summary": (
                            company_ctx.business_model_summary[:300]
                            if company_ctx.business_model_summary
                            else ""
                        ),
                    },
                    "anomaly_data": anomaly_data,
                    "conflict_data": _build_conflict_data(state),
                },
            )
        )
    except Exception as exc:
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.HIGH,
                category="subagent_failure",
                message=f"ThesisEngine failed: {exc}",
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

