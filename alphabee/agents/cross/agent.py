from __future__ import annotations

import asyncio
import json
from datetime import datetime
from functools import lru_cache
from typing import Any, TypedDict
from uuid import uuid4

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.store.memory import InMemoryStore

from alphabee.agents.cross.prompts import (
    CROSS_HARNESS_CRITIC_PROMPT,
    CROSS_HARNESS_EVALUATOR_PROMPT,
    CROSS_HARNESS_REPORTER_PROMPT,
)
from alphabee.agents.fundamental.agent import fundamental_agent
from alphabee.agents.market.agent import market_agent
from alphabee.agents.risk.agent import risk_agent
from alphabee.core import Artifact, Decision, Issue, IssueSeverity, Observation, Run, RunStatus, Step, StepStatus
from alphabee.harness import HarnessRuntime


MAX_SUPPLEMENT_ROUNDS = 1

# Issue categories that signal a data gap worth supplementing
_SUPPLEMENT_TRIGGER_CATEGORIES = {
    "subagent_failure",
    "missing_data",
    "missing_cross_evidence",
    "data_gap",
}

# (lowercase keyword in issue message) → subagent name
_KEYWORD_TO_AGENT: list[tuple[str, str]] = [
    ("fundamentalagent", "FundamentalAgent"),
    ("fundamental", "FundamentalAgent"),
    ("基本面", "FundamentalAgent"),
    ("财务", "FundamentalAgent"),
    ("marketagent", "MarketAgent"),
    ("market", "MarketAgent"),
    ("行情", "MarketAgent"),
    ("估值", "MarketAgent"),
    ("资金", "MarketAgent"),
    ("riskagent", "RiskAgent"),
    ("risk", "RiskAgent"),
    ("风险", "RiskAgent"),
    ("舆情", "RiskAgent"),
]


class CrossAnalysisState(TypedDict, total=False):
    messages: list[AnyMessage]
    run: Run
    steps: list[Step]
    artifacts: list[Artifact]
    observations: list[Observation]
    decisions: list[Decision]
    issues: list[Issue]
    final_artifact_id: str | None
    evaluation_artifact_id: str | None
    supplement_round: int
    max_supplement_rounds: int


def _make_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in {"text", "thinking"}:
                parts.append(block.get("text", ""))
        return "\n".join(part for part in parts if part)
    return str(content)


def _latest_query(messages: list[AnyMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            text = _extract_text(message.content).strip()
            if text:
                return text
    return _extract_text(messages[-1].content).strip() if messages else ""


def _extract_final_text(result: dict[str, Any]) -> str:
    if result.get("structured_response") is not None:
        structured = result["structured_response"]
        if hasattr(structured, "model_dump_json"):
            return structured.model_dump_json()
        return json.dumps(structured, ensure_ascii=False)
    messages = result.get("messages", [])
    if not messages:
        return ""
    return _extract_text(messages[-1].content).strip()


def _maybe_parse_json(raw: str) -> Any:
    text = raw.strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0].strip()
    try:
        return json.loads(text)
    except Exception:
        return None


@lru_cache(maxsize=1)
def _cross_harness_runtime() -> HarnessRuntime:
    return HarnessRuntime(
        checkpointer=InMemorySaver(),
        store=InMemoryStore(),
        reporter_prompt=CROSS_HARNESS_REPORTER_PROMPT,
        critic_prompt=CROSS_HARNESS_CRITIC_PROMPT,
        evaluator_prompt=CROSS_HARNESS_EVALUATOR_PROMPT,
    )


async def _invoke_subagent(name: str, runnable, query: str) -> tuple[str, str | None, Exception | None]:
    try:
        result = await runnable.ainvoke({"messages": [HumanMessage(content=query)]})
        return name, _extract_final_text(result), None
    except Exception as exc:  # propagate through structured issue instead of hard-failing the whole cross analysis
        return name, None, exc


async def collect_subagent_artifacts(state: CrossAnalysisState) -> CrossAnalysisState:
    query = _latest_query(state.get("messages", []))
    run = Run(
        id=_make_id("cross-run"),
        goal=query or "cross analysis",
        status=RunStatus.RUNNING,
        context={"query": query, "agent": "CrossAnalysisAgent"},
        started_at=datetime.now(),
    )

    step = Step(
        id="collect_subagents",
        kind="collect_subagents",
        inputs={"query": query, "subagents": ["FundamentalAgent", "MarketAgent", "RiskAgent"]},
        status=StepStatus.RUNNING,
    )

    results = await asyncio.gather(
        _invoke_subagent("FundamentalAgent", fundamental_agent, query),
        _invoke_subagent("MarketAgent", market_agent, query),
        _invoke_subagent("RiskAgent", risk_agent, query),
    )

    artifacts: list[Artifact] = []
    issues: list[Issue] = []

    artifact_type_map = {
        "FundamentalAgent": "fundamental_analysis",
        "MarketAgent": "market_analysis",
        "RiskAgent": "risk_analysis",
    }

    for agent_name, text, error in results:
        if error is not None:
            issues.append(
                Issue(
                    id=_make_id("issue"),
                    severity=IssueSeverity.HIGH,
                    category="subagent_failure",
                    message=f"{agent_name} failed: {error}",
                    related_step=step.id,
                )
            )
            continue

        parsed = _maybe_parse_json(text or "")
        artifacts.append(
            Artifact(
                id=_make_id("artifact"),
                type=artifact_type_map[agent_name],
                producer_step=step.id,
                value={
                    "agent": agent_name,
                    "query": query,
                    "raw_response": text or "",
                    "parsed_response": parsed,
                },
            )
        )

    if artifacts and issues:
        step_status = StepStatus.PARTIAL
    elif artifacts:
        step_status = StepStatus.SUCCEEDED
    else:
        step_status = StepStatus.FAILED

    completed_step = step.model_copy(
        update={
            "status": step_status,
            "outputs": [artifact.id for artifact in artifacts],
        }
    )

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
        "max_supplement_rounds": MAX_SUPPLEMENT_ROUNDS,
    }


async def run_cross_harness(state: CrossAnalysisState) -> CrossAnalysisState:
    runtime = _cross_harness_runtime()
    run = state["run"]
    result = await runtime.arun(
        goal=run.goal,
        context=run.context,
        steps=state.get("steps", []),
        observations=state.get("observations", []),
        artifacts=state.get("artifacts", []),
        decisions=state.get("decisions", []),
        issues=state.get("issues", []),
        run_id=run.id,
        thread_id=run.id,
    )
    return {
        **state,
        "run": result.run,
        "steps": result.steps,
        "artifacts": result.artifacts,
        "observations": result.observations,
        "decisions": result.decisions,
        "issues": result.issues,
        "final_artifact_id": result.final_artifact_id,
        "evaluation_artifact_id": result.evaluation_artifact_id,
    }


def _agents_to_supplement(issues: list[Issue]) -> set[str]:
    """Determine which subagents should be re-called based on gap issues."""
    agents: set[str] = set()
    for issue in issues:
        if issue.category not in _SUPPLEMENT_TRIGGER_CATEGORIES:
            continue
        if issue.severity not in {IssueSeverity.HIGH, IssueSeverity.CRITICAL}:
            continue
        msg_lower = issue.message.lower()
        for keyword, agent_name in _KEYWORD_TO_AGENT:
            if keyword in msg_lower:
                agents.add(agent_name)
    return agents


_SUBAGENT_RUNNABLES = {
    "FundamentalAgent": fundamental_agent,
    "MarketAgent": market_agent,
    "RiskAgent": risk_agent,
}

_ARTIFACT_TYPE_MAP = {
    "FundamentalAgent": "fundamental_analysis",
    "MarketAgent": "market_analysis",
    "RiskAgent": "risk_analysis",
}


async def supplement_missing_data(state: CrossAnalysisState) -> CrossAnalysisState:
    """After run_cross_harness, re-call subagents for any high-severity data gaps found by
    planner/critic, then route back for a second harness pass with the enriched artifacts."""
    supplement_round = state.get("supplement_round", 0)
    max_supplement_rounds = state.get("max_supplement_rounds", MAX_SUPPLEMENT_ROUNDS)

    if supplement_round >= max_supplement_rounds:
        return state

    agents_needed = _agents_to_supplement(state.get("issues", []))
    if not agents_needed:
        return {**state, "supplement_round": supplement_round + 1}

    query = _latest_query(state.get("messages", []))
    step_id = f"supplement_round_{supplement_round + 1}"

    results = await asyncio.gather(*[
        _invoke_subagent(name, _SUBAGENT_RUNNABLES[name], query)
        for name in agents_needed
    ])

    new_artifacts: list[Artifact] = []
    new_issues = list(state.get("issues", []))

    for agent_name, text, error in results:
        if error is not None:
            new_issues.append(Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="subagent_failure",
                message=f"{agent_name} supplement failed: {error}",
                related_step=step_id,
            ))
            continue
        parsed = _maybe_parse_json(text or "")
        new_artifacts.append(Artifact(
            id=_make_id("artifact"),
            type=_ARTIFACT_TYPE_MAP[agent_name],
            producer_step=step_id,
            value={
                "agent": agent_name,
                "query": query,
                "raw_response": text or "",
                "parsed_response": parsed,
                "supplement_round": supplement_round + 1,
            },
        ))

    if not new_artifacts:
        return {**state, "supplement_round": supplement_round + 1, "issues": new_issues}

    # Create a fresh run ID so the harness re-executes from scratch with enriched data.
    new_run = state["run"].model_copy(update={
        "id": _make_id("cross-run"),
        "status": RunStatus.RUNNING,
        "context": {**state["run"].context, "supplement_round": supplement_round + 1},
        "started_at": datetime.now(),
    })
    # Carry only collect + supplement steps (drop harness-internal planner/reporter/critic/evaluator
    # steps from previous round so the harness starts clean with fresh step IDs).
    carry_steps = [
        s for s in state.get("steps", [])
        if s.id == "collect_subagents" or s.id.startswith("supplement_round_")
    ]
    return {
        **state,
        "run": new_run,
        "steps": carry_steps,
        "artifacts": [*state.get("artifacts", []), *new_artifacts],
        "decisions": [],
        "observations": [],
        "issues": new_issues,
        "final_artifact_id": None,
        "evaluation_artifact_id": None,
        "supplement_round": supplement_round + 1,
    }


def _route_after_supplement(state: CrossAnalysisState) -> str:
    supplement_round = state.get("supplement_round", 0)
    # If new supplement artifacts were produced in this round, re-run harness.
    new_artifacts = [
        a for a in state.get("artifacts", [])
        if isinstance(a.value, dict) and a.value.get("supplement_round", 0) == supplement_round
    ]
    if new_artifacts:
        return "run_cross_harness"
    return "finalize_cross_message"


def finalize_cross_message(state: CrossAnalysisState) -> CrossAnalysisState:
    artifacts = state.get("artifacts", [])
    final_artifact_id = state.get("final_artifact_id")
    evaluation_artifact_id = state.get("evaluation_artifact_id")
    final_artifact = next((artifact for artifact in artifacts if artifact.id == final_artifact_id), None)
    evaluation_artifact = next((artifact for artifact in artifacts if artifact.id == evaluation_artifact_id), None)

    if final_artifact is None:
        final_artifact = next((artifact for artifact in reversed(artifacts) if artifact.type == "report"), None)
    if evaluation_artifact is None:
        evaluation_artifact = next((artifact for artifact in reversed(artifacts) if artifact.type == "evaluation_report"), None)

    payload = {
        "run": state["run"].model_dump(mode="json"),
        "final_report": final_artifact.value if final_artifact is not None else None,
        "evaluation_report": evaluation_artifact.value if evaluation_artifact is not None else None,
        "artifacts": [artifact.model_dump(mode="json") for artifact in artifacts],
        "decisions": [decision.model_dump(mode="json") for decision in state.get("decisions", [])],
        "issues": [issue.model_dump(mode="json") for issue in state.get("issues", [])],
    }

    return {
        **state,
        "messages": [AIMessage(content=json.dumps(payload, ensure_ascii=False, indent=2))],
    }


graph = StateGraph(CrossAnalysisState)
graph.add_node("collect_subagents", collect_subagent_artifacts)
graph.add_node("run_cross_harness", run_cross_harness)
graph.add_node("supplement_missing_data", supplement_missing_data)
graph.add_node("finalize_cross_message", finalize_cross_message)
graph.add_edge(START, "collect_subagents")
graph.add_edge("collect_subagents", "run_cross_harness")
graph.add_edge("run_cross_harness", "supplement_missing_data")
graph.add_conditional_edges(
    "supplement_missing_data",
    _route_after_supplement,
    {
        "run_cross_harness": "run_cross_harness",
        "finalize_cross_message": "finalize_cross_message",
    },
)
graph.add_edge("finalize_cross_message", END)

cross_agent = graph.compile(store=InMemoryStore())
