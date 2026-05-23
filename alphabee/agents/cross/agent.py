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
        "messages": state.get("messages", []),
        "run": result.run,
        "steps": result.steps,
        "artifacts": result.artifacts,
        "observations": result.observations,
        "decisions": result.decisions,
        "issues": result.issues,
        "final_artifact_id": result.final_artifact_id,
        "evaluation_artifact_id": result.evaluation_artifact_id,
    }


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
graph.add_node("finalize_cross_message", finalize_cross_message)
graph.add_edge(START, "collect_subagents")
graph.add_edge("collect_subagents", "run_cross_harness")
graph.add_edge("run_cross_harness", "finalize_cross_message")
graph.add_edge("finalize_cross_message", END)

cross_agent = graph.compile(store=InMemoryStore())
