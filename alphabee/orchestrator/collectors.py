"""Data collection node — FactCollector LLM agent + structured model extraction.

Also provides shared utility helpers used by both collectors and analyzers:
- _make_id, _extract_final_text, _find_artifact, _build_conflict_data, _finalize_step
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from langchain_core.messages import AnyMessage, HumanMessage
from langchain_core.runnables import RunnableConfig

from alphabee.agents.facts.agent import fact_collector_agent_factory
from alphabee.agents.facts.models import FinancialFacts, MarketFacts
from alphabee.agents.facts.tools.financial_fact import get_financial_facts_model
from alphabee.agents.facts.tools.market_fact import get_market_facts_model
from alphabee.core import (
    Artifact,
    Issue,
    IssueSeverity,
    Run,
    RunStatus,
    Step,
    StepStatus,
)
from alphabee.orchestrator.state import OrchestratorState
from alphabee.tools.common import extract_symbols_from_query
from alphabee.utils.pipeline import extract_text, make_id


# ── shared helpers ────────────────────────────────────────────────────


def _make_id(prefix: str) -> str:
    return make_id(prefix)


def _extract_final_text(result: dict[str, Any]) -> str:
    messages = result.get("messages", [])
    if not messages:
        return ""
    return extract_text(messages[-1].content).strip()


def _find_artifact(artifacts: list[Artifact], artifact_type: str) -> dict | None:
    """Return the most recent artifact value matching *artifact_type*, or None."""
    for a in reversed(artifacts):
        if a.type == artifact_type and isinstance(a.value, dict):
            return a.value
    return None


def _build_conflict_data(state: OrchestratorState) -> dict:
    """Summarise conflict+verification results for downstream nodes."""
    conflicts_raw = state.get("conflicts_result")
    verification_results = state.get("verification_results") or []

    if not conflicts_raw:
        return {}

    conflicts = conflicts_raw.get("conflicts", [])
    all_hypotheses = [h for c in conflicts for h in c.get("hypotheses", [])]

    verified = [h for h in all_hypotheses if h.get("status") in ("verified", "partial")]
    rejected = [h for h in all_hypotheses if h.get("status") == "rejected"]

    return {
        "conflict_count": len(conflicts),
        "hypothesis_count": len(all_hypotheses),
        "verified_count": len(verified),
        "rejected_count": len(rejected),
        "verified_hypotheses": [
            {"id": h.get("id"), "description": h.get("description"), "status": h.get("status")}
            for h in verified
        ],
        "conflicts_summary": [
            {
                "theme": c.get("theme", ""),
                "severity": c.get("severity", ""),
                "description": c.get("description", "")[:200],
            }
            for c in conflicts
        ],
        "verification_results": verification_results,
    }


def _finalize_step(
    step: Step, issues: list[Issue], artifacts: list[Artifact]
) -> Step:
    """Return a copy of *step* with status and outputs set."""
    if issues and not artifacts:
        status = StepStatus.FAILED
    elif issues:
        status = StepStatus.PARTIAL
    else:
        status = StepStatus.SUCCEEDED
    return step.model_copy(
        update={"status": status, "outputs": [a.id for a in artifacts]}
    )


# ── data collection helpers ───────────────────────────────────────────


def _latest_query(messages: list[AnyMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            text = extract_text(message.content).strip()
            if text:
                return text
    return extract_text(messages[-1].content).strip() if messages else ""


def _first_symbol(query: str) -> str | None:
    """Extract the first stock symbol from a query string."""
    symbols = extract_symbols_from_query(query)
    if symbols:
        return list(symbols.values())[0]
    return None


# ── node: collect_raw_facts ───────────────────────────────────────────


async def collect_raw_facts(
    state: OrchestratorState, config: RunnableConfig,
) -> OrchestratorState:
    """Concurrently run FactCollector LLM agent and structured model extraction.

    Produces:
    - ``fact_collection`` artifact with the agent's narrative text and metadata
    - ``fact_values`` dict in state (canonical numeric facts for downstream engines)
    - ``financial_facts`` / ``market_facts`` Pydantic objects in state
    """
    query = _latest_query(state.get("messages", []))
    symbol = _first_symbol(query)

    run = Run(
        id=_make_id("orch-run"),
        goal=query or "investment analysis",
        status=RunStatus.RUNNING,
        context={"query": query, "symbol": symbol},
        started_at=datetime.now(),
    )

    step = Step(
        id="collect_raw_facts",
        kind="collect_raw_facts",
        inputs={"query": query, "symbol": symbol},
        status=StepStatus.RUNNING,
    )

    artifacts: list[Artifact] = []
    issues: list[Issue] = []

    fact_text: str = ""
    financial_facts: FinancialFacts | None = None
    market_facts: MarketFacts | None = None

    async def _run_fact_agent() -> str:
        try:
            agent = fact_collector_agent_factory()
            result = await agent.ainvoke(
                {"messages": [HumanMessage(content=query)]},
                config=config,
            )
            return _extract_final_text(result)
        except Exception as exc:
            issues.append(
                Issue(
                    id=_make_id("issue"),
                    severity=IssueSeverity.HIGH,
                    category="subagent_failure",
                    message=f"FactCollector agent failed: {exc}",
                    related_step=step.id,
                )
            )
            return ""

    async def _run_structured_models() -> None:
        nonlocal financial_facts, market_facts
        if not symbol:
            return
        try:
            financial_facts = await asyncio.to_thread(
                get_financial_facts_model, symbol,
            )
        except Exception as exc:
            issues.append(
                Issue(
                    id=_make_id("issue"),
                    severity=IssueSeverity.HIGH,
                    category="missing_data",
                    message=f"Financial facts unavailable for {symbol}: {exc}",
                    related_step=step.id,
                )
            )
        try:
            market_facts = await asyncio.to_thread(
                get_market_facts_model, symbol,
            )
        except Exception as exc:
            issues.append(
                Issue(
                    id=_make_id("issue"),
                    severity=IssueSeverity.MEDIUM,
                    category="missing_data",
                    message=f"Market facts unavailable for {symbol}: {exc}",
                    related_step=step.id,
                )
            )

    fact_text, _ = await asyncio.gather(
        _run_fact_agent(),
        _run_structured_models(),
    )

    artifacts.append(
        Artifact(
            id=_make_id("artifact"),
            type="fact_collection",
            producer_step=step.id,
            value={
                "agent": "FactCollector",
                "query": query,
                "symbol": symbol,
                "raw_response": fact_text,
            },
        )
    )

    fact_values: dict[str, float] = {}
    if financial_facts is not None:
        fact_values.update(financial_facts.to_fact_values())
    if market_facts is not None:
        fact_values.update(market_facts.to_fact_values())

    if not fact_values and symbol:
        issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.CRITICAL,
                category="missing_data",
                message=(
                    "No structured data available for derived facts or signal "
                    "computation. Ensure the stock symbol is recognized."
                ),
                related_step=step.id,
            )
        )

    completed_step = _finalize_step(step, issues, artifacts)
    return {
        "messages": state.get("messages", []),
        "run": run,
        "steps": [completed_step],
        "artifacts": artifacts,
        "observations": [],
        "decisions": [],
        "issues": issues,
        "final_artifact_id": None,
        "evaluation_artifact_id": None,
        "supplement_round": 0,
        "max_supplement_rounds": 1,
        "fact_values": fact_values,
        "financial_facts": financial_facts,
        "market_facts": market_facts,
    }
