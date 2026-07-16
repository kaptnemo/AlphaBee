"""Deterministic analysis node for derived facts, anomalies, and signals."""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig

from alphabee.agents.derived_facts.engine import Engine as DerivedFactsEngine
from alphabee.agents.derived_facts.registry import RULES, load_rules
from alphabee.agents.facts.tools.company_profile import get_company_profile
from alphabee.agents.signal.engine import SignalEngine
from alphabee.agents.signal.registry import SIGNAL_RULES, load_signal_rules
from alphabee.core import Artifact, Issue, IssueSeverity, Step, StepStatus
from alphabee.orchestrator.collectors import _finalize_step, _make_id
from alphabee.orchestrator.services.gap_recorder import record_signal_data_gaps
from alphabee.orchestrator.services.payload_builders import default_anomaly_fact_values
from alphabee.orchestrator.state import OrchestratorState


async def run_analysis_engines(
    state: OrchestratorState, config: RunnableConfig,
) -> OrchestratorState:
    """Run deterministic analysis engines on the structured fact values."""
    del config
    run = state.get("run")
    symbol = run.context.get("symbol") if run else None
    fact_values: dict[str, float] = dict(state.get("fact_values") or {})
    financial_facts = state.get("financial_facts")

    step = Step(
        id="run_analysis_engines",
        kind="run_analysis_engines",
        inputs={"symbol": symbol, "fact_values_count": len(fact_values)},
        status=StepStatus.RUNNING,
    )

    new_artifacts: list[Artifact] = []
    new_issues: list[Issue] = []

    if not fact_values:
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.CRITICAL,
                category="missing_data",
                message="No fact_values available — skipping all analysis engines.",
                related_step=step.id,
            )
        )
        completed_step = _finalize_step(step, new_issues, new_artifacts)
        return {
            **state,
            "steps": state.get("steps", []) + [completed_step],
            "issues": state.get("issues", []) + new_issues,
        }

    derived_facts: dict[str, dict] = {}
    try:
        load_rules()
        df_engine = DerivedFactsEngine()
        all_rule_names = list(RULES.keys())
        derived_facts = df_engine.run(all_rule_names, fact_values)
        new_artifacts.append(
            Artifact(
                id=_make_id("artifact"),
                type="derived_facts",
                producer_step=step.id,
                value={"results": derived_facts, "rule_count": len(all_rule_names)},
            )
        )
    except Exception as exc:
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.HIGH,
                category="subagent_failure",
                message=f"DerivedFacts engine failed: {exc}",
                related_step=step.id,
            )
        )

    anomaly_report = None
    anomaly_fact_values: dict[str, float] = default_anomaly_fact_values()
    if financial_facts is not None and len(financial_facts.snapshots) >= 2:
        try:
            from alphabee.agents.anomaly.engine import AnomalyEngine

            extra_vals: dict[str, float] = {}
            try:
                profile = get_company_profile(symbol)
                company_data = profile.get("company", {}) if profile else {}
                employees_raw = company_data.get("employees", {})
                if isinstance(employees_raw, dict):
                    employees_val = employees_raw.get(0)
                    if employees_val is not None:
                        extra_vals["employees"] = float(employees_val)
            except Exception:
                pass

            anomaly_engine = AnomalyEngine()
            anomaly_report = anomaly_engine.run(
                financial_facts, extra_values=extra_vals or None,
            )
            anomaly_fact_values.update(anomaly_report.to_fact_values())
            new_artifacts.append(
                Artifact(
                    id=_make_id("artifact"),
                    type="anomaly_report",
                    producer_step=step.id,
                    value=anomaly_report.to_dict(),
                )
            )
        except Exception as exc:
            new_issues.append(
                Issue(
                    id=_make_id("issue"),
                    severity=IssueSeverity.MEDIUM,
                    category="subagent_failure",
                    message=f"AnomalyEngine failed: {exc}",
                    related_step=step.id,
                )
            )

    fact_values.update(anomaly_fact_values)

    signal_analysis: dict[str, dict] = {}
    try:
        load_signal_rules()
        signal_engine = SignalEngine()
        all_signal_names = list(SIGNAL_RULES.keys())
        signal_analysis = signal_engine.run(all_signal_names, fact_values)
        new_artifacts.append(
            Artifact(
                id=_make_id("artifact"),
                type="signal_analysis",
                producer_step=step.id,
                value={"results": signal_analysis, "rule_count": len(all_signal_names)},
            )
        )
        record_signal_data_gaps(signal_analysis, fact_values, symbol)
    except Exception as exc:
        new_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.HIGH,
                category="subagent_failure",
                message=f"SignalEngine failed: {exc}",
                related_step=step.id,
            )
        )

    completed_step = _finalize_step(step, new_issues, new_artifacts)
    return {
        **state,
        "steps": state.get("steps", []) + [completed_step],
        "artifacts": state.get("artifacts", []) + new_artifacts,
        "issues": state.get("issues", []) + new_issues,
        "fact_values": fact_values,
        "derived_facts": derived_facts,
        "signal_analysis": signal_analysis,
        "anomaly_report": anomaly_report.to_dict() if anomaly_report else None,
    }

