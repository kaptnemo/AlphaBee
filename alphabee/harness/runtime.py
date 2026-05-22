from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from typing import Any, TypedDict
from uuid import uuid4

from deepagents import create_deep_agent
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore
from pydantic import BaseModel, Field

from alphabee.config import settings
from alphabee.core import (
    Artifact,
    Decision,
    EvaluationAssessment,
    EvaluationReport,
    EvaluateMetrics,
    Issue,
    IssueSeverity,
    Observation,
    Run,
    RunStatus,
    Step,
    StepStatus,
)
from alphabee.harness.prompts import (
    CRITIC_NODE_PROMPT,
    EVALUATOR_NODE_PROMPT,
    PLANNER_NODE_PROMPT,
    REPORTER_NODE_PROMPT,
)


class HarnessState(TypedDict):
    run: Run
    steps: list[Step]
    artifacts: list[Artifact]
    observations: list[Observation]
    decisions: list[Decision]
    issues: list[Issue]
    final_artifact_id: str | None
    evaluation_artifact_id: str | None


class ThinkingNodeOutput(BaseModel):
    decisions: list[Decision] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)


class HarnessExecutionResult(BaseModel):
    run: Run
    steps: list[Step]
    artifacts: list[Artifact]
    observations: list[Observation]
    decisions: list[Decision]
    issues: list[Issue]
    final_artifact_id: str | None = None
    evaluation_artifact_id: str | None = None


class HarnessStateDiff(BaseModel):
    run_status_before: str | None = None
    run_status_after: str | None = None
    added_step_ids: list[str] = Field(default_factory=list)
    added_artifact_ids: list[str] = Field(default_factory=list)
    added_decision_ids: list[str] = Field(default_factory=list)
    added_issue_ids: list[str] = Field(default_factory=list)
    removed_step_ids: list[str] = Field(default_factory=list)
    removed_artifact_ids: list[str] = Field(default_factory=list)
    removed_decision_ids: list[str] = Field(default_factory=list)
    removed_issue_ids: list[str] = Field(default_factory=list)


def _make_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _model() -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.llm.model,
        api_key=settings.llm.api_key,
        base_url=settings.llm.base_url,
    )


@lru_cache(maxsize=16)
def _planner_agent(prompt: str) -> CompiledStateGraph:
    return create_deep_agent(
        model=_model(),
        system_prompt=prompt,
        tools=[],
        name="HarnessPlanner",
    )


@lru_cache(maxsize=16)
def _reporter_agent(prompt: str) -> CompiledStateGraph:
    return create_deep_agent(
        model=_model(),
        system_prompt=prompt,
        tools=[],
        name="HarnessReporter",
    )


@lru_cache(maxsize=16)
def _critic_agent(prompt: str) -> CompiledStateGraph:
    return create_deep_agent(
        model=_model(),
        system_prompt=prompt,
        tools=[],
        name="HarnessCritic",
    )


@lru_cache(maxsize=16)
def _evaluator_agent(prompt: str) -> CompiledStateGraph:
    return create_deep_agent(
        model=_model(),
        system_prompt=prompt,
        tools=[],
        name="HarnessEvaluator",
    )


def _state_to_prompt_payload(state: HarnessState) -> str:
    payload = {
        "run": state["run"].model_dump(mode="json"),
        "steps": [step.model_dump(mode="json") for step in state["steps"]],
        "artifacts": [artifact.model_dump(mode="json") for artifact in state["artifacts"]],
        "observations": [
            observation.model_dump(mode="json") for observation in state["observations"]
        ],
        "decisions": [decision.model_dump(mode="json") for decision in state["decisions"]],
        "issues": [issue.model_dump(mode="json") for issue in state["issues"]],
        "final_artifact_id": state["final_artifact_id"],
        "evaluation_artifact_id": state["evaluation_artifact_id"],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


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


def _extract_final_text(result: dict[str, Any]) -> str:
    messages = result.get("messages", [])
    if messages:
        return _extract_text(getattr(messages[-1], "content", messages[-1])).strip()

    structured = result.get("structured_response")
    if structured is None:
        return ""
    if hasattr(structured, "model_dump_json"):
        return structured.model_dump_json()
    return json.dumps(structured, ensure_ascii=False)


def _parse_json_text(raw: str) -> Any:
    text = raw.strip()
    if not text:
        raise ValueError("Model returned empty text instead of JSON.")

    candidates: list[str] = []
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2:
            fenced = "\n".join(lines[1:-1]).strip()
            if fenced.startswith("json"):
                fenced = fenced[4:].strip()
            candidates.append(fenced)
    candidates.append(text)

    start_positions = [index for index in (text.find("{"), text.find("[")) if index != -1]
    if start_positions:
        start = min(start_positions)
        opener = text[start]
        closer = "}" if opener == "{" else "]"
        end = text.rfind(closer)
        if end > start:
            candidates.append(text[start : end + 1])

    seen = set()
    for candidate in candidates:
        normalized = candidate.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        try:
            return json.loads(normalized)
        except json.JSONDecodeError:
            continue

    excerpt = text[:400].replace("\n", "\\n")
    raise ValueError(f"Failed to parse model output as JSON: {excerpt}")


def _coerce_thinking_output(payload: Any) -> ThinkingNodeOutput:
    if isinstance(payload, ThinkingNodeOutput):
        return payload
    if isinstance(payload, dict):
        return ThinkingNodeOutput.model_validate(payload)
    if isinstance(payload, list):
        grouped: dict[str, list[dict[str, Any]]] = {
            "decisions": [],
            "issues": [],
            "artifacts": [],
        }
        unknown_items: list[Any] = []
        for item in payload:
            if not isinstance(item, dict):
                unknown_items.append(item)
                continue

            item_type = str(item.get("type", "")).lower()
            if item_type == "decision" or {"maker", "rationale", "confidence"} <= item.keys():
                grouped["decisions"].append(item)
                continue
            if item_type == "issue" or {"severity", "category", "message"} <= item.keys():
                grouped["issues"].append(item)
                continue
            if item_type == "artifact" or {"producer_step"} <= item.keys():
                grouped["artifacts"].append(item)
                continue
            unknown_items.append(item)

        if unknown_items:
            raise ValueError(f"Unsupported thinking output items: {unknown_items[:3]}")
        return ThinkingNodeOutput.model_validate(grouped)

    raise ValueError(f"Unsupported thinking output payload type: {type(payload).__name__}")


def _coerce_evaluation_assessment(payload: Any) -> EvaluationAssessment:
    if isinstance(payload, EvaluationAssessment):
        return payload
    if isinstance(payload, list):
        if len(payload) != 1:
            raise ValueError("Evaluation output must be a single JSON object.")
        payload = payload[0]
    return EvaluationAssessment.model_validate(payload)


def _json_instruction(schema: type[BaseModel], *, example: str) -> str:
    return (
        "输出要求：\n"
        "1. 只返回 JSON，不要 Markdown，不要代码块，不要额外解释。\n"
        "2. 顶层必须严格符合下面给出的结构。\n"
        f"3. 输出示例：{example}\n"
        f"4. JSON Schema:\n{json.dumps(schema.model_json_schema(), ensure_ascii=False, indent=2)}"
    )


async def _invoke_json_agent(
    agent: CompiledStateGraph,
    *,
    prompt: str,
) -> Any:
    result = await agent.ainvoke({"messages": [{"role": "user", "content": prompt}]})
    return _parse_json_text(_extract_final_text(result))


def _upsert_step(steps: list[Step], step: Step) -> list[Step]:
    updated = [existing for existing in steps if existing.id != step.id]
    updated.append(step)
    return updated


def _normalize_output(
    output: ThinkingNodeOutput,
    *,
    step_id: str,
    default_artifact_type: str,
) -> ThinkingNodeOutput:
    artifacts = []
    for artifact in output.artifacts:
        artifacts.append(
            artifact.model_copy(
                update={
                    "id": artifact.id or _make_id(step_id),
                    "producer_step": artifact.producer_step or step_id,
                    "type": artifact.type or default_artifact_type,
                }
            )
        )

    decisions = []
    for decision in output.decisions:
        decisions.append(
            decision.model_copy(
                update={
                    "id": decision.id or _make_id(step_id),
                    "maker": decision.maker or step_id,
                }
            )
        )

    issues = []
    for issue in output.issues:
        issues.append(
            issue.model_copy(
                update={
                    "id": issue.id or _make_id(step_id),
                    "related_step": issue.related_step or step_id,
                }
            )
        )

    return ThinkingNodeOutput(
        decisions=decisions,
        issues=issues,
        artifacts=artifacts,
    )


def _snapshot_payload(state: HarnessState) -> dict[str, Any]:
    return {
        "run": state["run"].model_dump(mode="json"),
        "steps": [step.model_dump(mode="json") for step in state["steps"]],
        "artifacts": [artifact.model_dump(mode="json") for artifact in state["artifacts"]],
        "observations": [
            observation.model_dump(mode="json") for observation in state["observations"]
        ],
        "decisions": [decision.model_dump(mode="json") for decision in state["decisions"]],
        "issues": [issue.model_dump(mode="json") for issue in state["issues"]],
        "final_artifact_id": state["final_artifact_id"],
        "evaluation_artifact_id": state["evaluation_artifact_id"],
    }


def _store_snapshot(store: BaseStore, run_id: str, key: str, state: HarnessState) -> None:
    store.put(
        ("harness", "runs", run_id, "snapshots"),
        key,
        _snapshot_payload(state),
    )


async def _run_thinking_node(
    state: HarnessState,
    *,
    step_id: str,
    kind: str,
    default_artifact_type: str,
    prompt_prefix: str,
    agent: CompiledStateGraph,
    store: BaseStore,
) -> HarnessState:
    running_step = Step(
        id=step_id,
        kind=kind,
        inputs={
            "run_id": state["run"].id,
            "artifact_ids": [artifact.id for artifact in state["artifacts"]],
            "observation_ids": [observation.id for observation in state["observations"]],
            "decision_ids": [decision.id for decision in state["decisions"]],
            "issue_ids": [issue.id for issue in state["issues"]],
        },
        status=StepStatus.RUNNING,
    )
    steps = _upsert_step(state["steps"], running_step)

    prompt = (
        f"{prompt_prefix}\n\n"
        f"{_json_instruction(ThinkingNodeOutput, example='{\"decisions\": [], \"issues\": [], \"artifacts\": []}')}\n\n"
        f"当前 step_id: {step_id}\n"
        f"默认 artifact.type: {default_artifact_type}\n"
        "请直接返回结构化对象，不要输出额外说明。\n\n"
        f"{_state_to_prompt_payload({**state, 'steps': steps})}"
    )
    normalized = _normalize_output(
        _coerce_thinking_output(await _invoke_json_agent(agent, prompt=prompt)),
        step_id=step_id,
        default_artifact_type=default_artifact_type,
    )

    artifacts = [*state["artifacts"], *normalized.artifacts]
    decisions = [*state["decisions"], *normalized.decisions]
    issues = [*state["issues"], *normalized.issues]
    completed_step = running_step.model_copy(
        update={
            "status": StepStatus.SUCCEEDED,
            "outputs": [artifact.id for artifact in normalized.artifacts],
        }
    )
    next_state: HarnessState = {
        "run": state["run"],
        "steps": _upsert_step(steps, completed_step),
        "artifacts": artifacts,
        "observations": state["observations"],
        "decisions": decisions,
        "issues": issues,
        "final_artifact_id": (
            normalized.artifacts[-1].id if normalized.artifacts else state["final_artifact_id"]
        ),
        "evaluation_artifact_id": state["evaluation_artifact_id"],
    }
    _store_snapshot(store, state["run"].id, step_id, next_state)
    return next_state


def _planner_node_factory(store: BaseStore):
    async def planner_node(state: HarnessState) -> HarnessState:
        return await _run_thinking_node(
            state,
            step_id="planner",
            kind="plan",
            default_artifact_type="plan",
            prompt_prefix="请为本次 run 生成执行计划与关键关注点。",
            agent=_planner_agent(PLANNER_NODE_PROMPT),
            store=store,
        )

    return planner_node


def _reporter_node_factory(store: BaseStore, prompt: str):
    async def reporter_node(state: HarnessState) -> HarnessState:
        return await _run_thinking_node(
            state,
            step_id="reporter",
            kind="report",
            default_artifact_type="report",
            prompt_prefix="请基于当前状态整合已有产物，生成阶段性报告。",
            agent=_reporter_agent(prompt),
            store=store,
        )

    return reporter_node


def _critic_node_factory(store: BaseStore, prompt: str):
    async def critic_node(state: HarnessState) -> HarnessState:
        return await _run_thinking_node(
            state,
            step_id="critic",
            kind="critic",
            default_artifact_type="critique",
            prompt_prefix="请审查当前 run 的结论、证据和风险缺口。",
            agent=_critic_agent(prompt),
            store=store,
        )

    return critic_node


def _find_latest_artifact(state: HarnessState, artifact_type: str) -> Artifact | None:
    for artifact in reversed(state["artifacts"]):
        if artifact.type == artifact_type:
            return artifact
    return None


def _normalize_text_for_search(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _compute_evaluation_metrics(state: HarnessState) -> EvaluateMetrics:
    report_artifact = _find_latest_artifact(state, "report")
    report_value = report_artifact.value if report_artifact is not None else None
    report_payload = report_value if isinstance(report_value, dict) else {}
    expected_sections = {"summary", "opportunities", "risks", "divergences", "confidence"}
    present_sections = {key for key in expected_sections if report_payload.get(key) not in (None, "", [], {})}
    artifact_coverage = len(present_sections) / len(expected_sections)

    decisions = state["decisions"]
    evidence_coverage = (
        sum(1 for decision in decisions if decision.based_on) / len(decisions)
        if decisions
        else 0.0
    )

    issue_categories = {issue.category for issue in state["issues"]}
    numeric_consistency = not any(
        category in issue_categories
        for category in {"numeric_inconsistency", "conflict", "cross_source_conflict"}
    )
    cross_source_consistency = not any(
        category in issue_categories
        for category in {"cross_source_conflict", "conflict", "time_mismatch"}
    )

    report_text = _normalize_text_for_search(report_value).lower()
    issue_handling = (
        not state["issues"]
        or any(token in report_text for token in ("risk", "issue", "gap", "缺口", "风险", "背离"))
    )

    freshness_values = {
        observation.freshness.value for observation in state["observations"]
    }
    if not freshness_values:
        freshness_score = 0.5
    elif freshness_values <= {"realtime", "recent"}:
        freshness_score = 1.0
    elif "stale" in freshness_values:
        freshness_score = 0.25
    elif "historical" in freshness_values:
        freshness_score = 0.6
    else:
        freshness_score = 0.5

    valid_ids = {
        *(artifact.id for artifact in state["artifacts"]),
        *(observation.id for observation in state["observations"]),
        *(issue.id for issue in state["issues"]),
        *(decision.id for decision in state["decisions"]),
    }
    grounded_references = 0
    total_references = 0
    for decision in decisions:
        total_references += len(decision.based_on)
        grounded_references += sum(1 for item in decision.based_on if item in valid_ids)
    grounding_score = grounded_references / total_references if total_references else 0.0

    schema_validity = isinstance(report_payload, dict) and "summary" in report_payload
    return EvaluateMetrics(
        schema_validity=schema_validity,
        artifact_coverage=artifact_coverage,
        evidence_coverage=evidence_coverage,
        numeric_consistency=numeric_consistency,
        issue_handling=issue_handling,
        cross_source_consistency=cross_source_consistency,
        freshness_score=freshness_score,
        grounding_score=grounding_score,
        conclusion_clarity="good" if schema_validity and artifact_coverage >= 0.8 else "needs_improvement",
        cross_analysis_depth="good" if "divergences" in present_sections else "shallow",
        fact_inference_distinction="good" if evidence_coverage >= 0.5 else "weak",
        risk_warning_sufficiency="good" if "risks" in present_sections else "weak",
        overconfidence_presence="low" if state["issues"] else "medium",
        user_usefulness="high" if schema_validity and artifact_coverage >= 0.8 else "medium",
    )


async def _run_evaluator_node(
    state: HarnessState,
    *,
    prompt: str,
    store: BaseStore,
) -> HarnessState:
    running_step = Step(
        id="evaluator",
        kind="evaluate",
        inputs={
            "run_id": state["run"].id,
            "report_artifact_id": state["final_artifact_id"],
            "artifact_ids": [artifact.id for artifact in state["artifacts"]],
            "decision_ids": [decision.id for decision in state["decisions"]],
            "issue_ids": [issue.id for issue in state["issues"]],
        },
        status=StepStatus.RUNNING,
    )
    steps = _upsert_step(state["steps"], running_step)
    metrics = _compute_evaluation_metrics({**state, "steps": steps})

    evaluator_prompt = (
        "请基于以下定量指标和当前 run 状态，生成最终评估。\n\n"
        f"{_json_instruction(EvaluationAssessment, example='{\"summary\": \"\", \"strengths\": [], \"weaknesses\": [], \"blocking_issues\": [], \"passed\": false, \"recommendation\": \"\", \"improvement_actions\": []}')}\n\n"
        f"定量指标：\n{metrics.model_dump_json(indent=2)}\n\n"
        f"{_state_to_prompt_payload({**state, 'steps': steps})}"
    )
    assessment = _coerce_evaluation_assessment(
        await _invoke_json_agent(_evaluator_agent(prompt), prompt=evaluator_prompt)
    )

    report = EvaluationReport(
        metrics=metrics,
        summary=assessment.summary,
        strengths=assessment.strengths,
        weaknesses=assessment.weaknesses,
        blocking_issues=assessment.blocking_issues,
        passed=assessment.passed,
        recommendation=assessment.recommendation,
        improvement_actions=assessment.improvement_actions,
    )
    evaluation_artifact = Artifact(
        id=_make_id("evaluation"),
        type="evaluation_report",
        producer_step=running_step.id,
        value=report.model_dump(mode="json"),
    )
    evaluation_decision = Decision(
        id=_make_id("decision"),
        maker="evaluator",
        rationale=assessment.recommendation,
        confidence=0.9 if assessment.passed else 0.7,
        based_on=[
            *( [state["final_artifact_id"]] if state["final_artifact_id"] else []),
            *(issue.id for issue in state["issues"]),
        ],
    )

    evaluation_issues = list(state["issues"])
    for message in assessment.blocking_issues:
        evaluation_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.HIGH if not assessment.passed else IssueSeverity.MEDIUM,
                category="evaluation_failure",
                message=message,
                related_step=running_step.id,
                related_artifact=evaluation_artifact.id,
            )
        )

    completed_step = running_step.model_copy(
        update={
            "status": StepStatus.SUCCEEDED,
            "outputs": [evaluation_artifact.id],
        }
    )
    final_run = state["run"].model_copy(
        update={
            "status": RunStatus.SUCCEEDED if assessment.passed else RunStatus.PARTIAL,
            "ended_at": datetime.now(),
        }
    )
    next_state: HarnessState = {
        "run": final_run,
        "steps": _upsert_step(steps, completed_step),
        "artifacts": [*state["artifacts"], evaluation_artifact],
        "observations": state["observations"],
        "decisions": [*state["decisions"], evaluation_decision],
        "issues": evaluation_issues,
        "final_artifact_id": state["final_artifact_id"],
        "evaluation_artifact_id": evaluation_artifact.id,
    }
    _store_snapshot(store, state["run"].id, "evaluator", next_state)
    _store_snapshot(store, state["run"].id, "final", next_state)
    return next_state


def _evaluator_node_factory(store: BaseStore, prompt: str):
    async def evaluator_node(state: HarnessState) -> HarnessState:
        return await _run_evaluator_node(
            state,
            prompt=prompt,
            store=store,
        )

    return evaluator_node


def create_initial_harness_state(
    *,
    goal: str,
    context: dict[str, Any] | None = None,
    steps: list[Step] | None = None,
    observations: list[Observation] | None = None,
    artifacts: list[Artifact] | None = None,
    decisions: list[Decision] | None = None,
    issues: list[Issue] | None = None,
    run_id: str | None = None,
) -> HarnessState:
    run = Run(
        id=run_id or _make_id("run"),
        goal=goal,
        status=RunStatus.RUNNING,
        context=context or {},
        started_at=datetime.now(),
    )
    return {
        "run": run,
        "steps": steps or [],
        "artifacts": artifacts or [],
        "observations": observations or [],
        "decisions": decisions or [],
        "issues": issues or [],
        "final_artifact_id": None,
        "evaluation_artifact_id": None,
    }


def build_harness_graph(
    *,
    checkpointer: InMemorySaver | None = None,
    store: BaseStore | None = None,
    reporter_prompt: str = REPORTER_NODE_PROMPT,
    critic_prompt: str = CRITIC_NODE_PROMPT,
    evaluator_prompt: str = EVALUATOR_NODE_PROMPT,
) -> tuple[CompiledStateGraph, BaseStore]:
    graph = StateGraph(HarnessState)
    runtime_store = store or InMemoryStore()
    graph.add_node("planner", _planner_node_factory(runtime_store))
    graph.add_node("reporter", _reporter_node_factory(runtime_store, reporter_prompt))
    graph.add_node("critic", _critic_node_factory(runtime_store, critic_prompt))
    graph.add_node("evaluator", _evaluator_node_factory(runtime_store, evaluator_prompt))
    graph.add_edge(START, "planner")
    graph.add_edge("planner", "reporter")
    graph.add_edge("reporter", "critic")
    graph.add_edge("critic", "evaluator")
    graph.add_edge("evaluator", END)
    return graph.compile(checkpointer=checkpointer or InMemorySaver(), store=runtime_store), runtime_store


def _ids(items: list[dict[str, Any]] | list[BaseModel], key: str = "id") -> set[str]:
    values = set()
    for item in items:
        if isinstance(item, BaseModel):
            values.add(str(getattr(item, key)))
        else:
            values.add(str(item[key]))
    return values


def diff_harness_states(before: dict[str, Any], after: dict[str, Any]) -> HarnessStateDiff:
    before_steps = _ids(before.get("steps", []))
    after_steps = _ids(after.get("steps", []))
    before_artifacts = _ids(before.get("artifacts", []))
    after_artifacts = _ids(after.get("artifacts", []))
    before_decisions = _ids(before.get("decisions", []))
    after_decisions = _ids(after.get("decisions", []))
    before_issues = _ids(before.get("issues", []))
    after_issues = _ids(after.get("issues", []))

    before_run = before.get("run")
    after_run = after.get("run")
    before_status = before_run["status"] if isinstance(before_run, dict) else getattr(before_run, "status", None)
    after_status = after_run["status"] if isinstance(after_run, dict) else getattr(after_run, "status", None)
    before_status_value = getattr(before_status, "value", before_status)
    after_status_value = getattr(after_status, "value", after_status)

    return HarnessStateDiff(
        run_status_before=str(before_status_value) if before_status_value is not None else None,
        run_status_after=str(after_status_value) if after_status_value is not None else None,
        added_step_ids=sorted(after_steps - before_steps),
        added_artifact_ids=sorted(after_artifacts - before_artifacts),
        added_decision_ids=sorted(after_decisions - before_decisions),
        added_issue_ids=sorted(after_issues - before_issues),
        removed_step_ids=sorted(before_steps - after_steps),
        removed_artifact_ids=sorted(before_artifacts - after_artifacts),
        removed_decision_ids=sorted(before_decisions - after_decisions),
        removed_issue_ids=sorted(before_issues - after_issues),
    )


class HarnessRuntime:
    def __init__(
        self,
        *,
        checkpointer: InMemorySaver | None = None,
        store: BaseStore | None = None,
        reporter_prompt: str = REPORTER_NODE_PROMPT,
        critic_prompt: str = CRITIC_NODE_PROMPT,
        evaluator_prompt: str = EVALUATOR_NODE_PROMPT,
    ) -> None:
        self.graph, self.store = build_harness_graph(
            checkpointer=checkpointer,
            store=store,
            reporter_prompt=reporter_prompt,
            critic_prompt=critic_prompt,
            evaluator_prompt=evaluator_prompt,
        )

    async def arun(
        self,
        *,
        goal: str,
        context: dict[str, Any] | None = None,
        steps: list[Step] | None = None,
        observations: list[Observation] | None = None,
        artifacts: list[Artifact] | None = None,
        decisions: list[Decision] | None = None,
        issues: list[Issue] | None = None,
        run_id: str | None = None,
        thread_id: str | None = None,
    ) -> HarnessExecutionResult:
        initial_state = create_initial_harness_state(
            goal=goal,
            context=context,
            steps=steps,
            observations=observations,
            artifacts=artifacts,
            decisions=decisions,
            issues=issues,
            run_id=run_id,
        )
        _store_snapshot(self.store, initial_state["run"].id, "initial", initial_state)
        config = {"configurable": {"thread_id": thread_id or initial_state["run"].id}}
        final_state = await self.graph.ainvoke(initial_state, config=config)
        return HarnessExecutionResult.model_validate(final_state)

    def run(
        self,
        *,
        goal: str,
        context: dict[str, Any] | None = None,
        steps: list[Step] | None = None,
        observations: list[Observation] | None = None,
        artifacts: list[Artifact] | None = None,
        decisions: list[Decision] | None = None,
        issues: list[Issue] | None = None,
        run_id: str | None = None,
        thread_id: str | None = None,
    ) -> HarnessExecutionResult:
        initial_state = create_initial_harness_state(
            goal=goal,
            context=context,
            steps=steps,
            observations=observations,
            artifacts=artifacts,
            decisions=decisions,
            issues=issues,
            run_id=run_id,
        )
        _store_snapshot(self.store, initial_state["run"].id, "initial", initial_state)
        config = {"configurable": {"thread_id": thread_id or initial_state["run"].id}}
        final_state = self.graph.invoke(initial_state, config=config)
        return HarnessExecutionResult.model_validate(final_state)

    def recover_state(self, thread_id: str) -> dict[str, Any]:
        snapshot = self.graph.get_state({"configurable": {"thread_id": thread_id}})
        return snapshot.values

    async def arecover_state(self, thread_id: str) -> dict[str, Any]:
        snapshot = await self.graph.aget_state({"configurable": {"thread_id": thread_id}})
        return snapshot.values

    def replay(self, thread_id: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        history = self.graph.get_state_history(
            {"configurable": {"thread_id": thread_id}},
            limit=limit,
        )
        return [snapshot.values for snapshot in history]

    async def areplay(self, thread_id: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        snapshots = []
        async for snapshot in self.graph.aget_state_history(
            {"configurable": {"thread_id": thread_id}},
            limit=limit,
        ):
            snapshots.append(snapshot.values)
        return snapshots

    def list_stored_snapshots(self, run_id: str) -> list[dict[str, Any]]:
        items = self.store.search(("harness", "runs", run_id, "snapshots"), limit=100)
        return [item.value for item in items]

    def diff_stored_snapshots(self, run_id: str, before_key: str, after_key: str) -> HarnessStateDiff:
        before = self.store.get(("harness", "runs", run_id, "snapshots"), before_key)
        after = self.store.get(("harness", "runs", run_id, "snapshots"), after_key)
        if before is None:
            raise ValueError(f"Snapshot not found: {before_key}")
        if after is None:
            raise ValueError(f"Snapshot not found: {after_key}")
        return diff_harness_states(before.value, after.value)
