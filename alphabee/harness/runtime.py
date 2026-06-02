from __future__ import annotations

import json
import structlog
from datetime import datetime
from functools import lru_cache
from typing import Any, TypedDict
from uuid import uuid4

from langchain_core.messages import SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore
from pydantic import BaseModel, Field

from alphabee.core import (
    Artifact,
    Decision,
    EvaluationAssessment,
    EvaluationReport,
    EvaluateMetrics,
    Issue,
    IssueSeverity,
    IssueScope,
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
from alphabee.harness.state_compressor import CompressorConfig, HarnessStateCompressor, NodeKind
from alphabee.utils import create_chat_model
from alphabee.harness.utils import json_instruction


class HarnessState(TypedDict):
    run: Run
    steps: list[Step]
    artifacts: list[Artifact]
    observations: list[Observation]
    decisions: list[Decision]
    issues: list[Issue]
    final_artifact_id: str | None
    evaluation_artifact_id: str | None
    reporter_round: int
    critic_round: int
    max_reporter_rounds: int
    latest_step_output: dict[str, Any] | None
    rewrite_reason: str | None


class DataCollectionNodeOutput(BaseModel):
    artifacts: list[Artifact] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)


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


DEFAULT_MAX_REPORTER_ROUNDS = 3
REWRITE_TRIGGER_CATEGORIES = {
    "report_rewrite_needed",
    "missing_data",
    "verification_needed",
    "conflict",
    "cross_source_conflict",
    "time_mismatch",
    "numeric_inconsistency",
    "missing_cross_evidence",
    "weak_grounding",
    "unsupported_cross_claim",
    "incomplete_report",
}
REWRITE_TRIGGER_KEYWORDS = (
    "rewrite",
    "revise",
    "rework",
    "重写",
    "改写",
    "补充",
    "修订",
    "证据不足",
    "缺口",
    "冲突",
    "错配",
)

# Maps the node step_id to the IssueScope that should be stamped on issues it
# produces.  Used by _normalize_output so every issue carries its origin scope.
_STEP_SCOPE_MAP: dict[str, IssueScope] = {
    "planner": IssueScope.PLANNING,
    "reporter": IssueScope.REPORT,
    "critic": IssueScope.REVIEW,
    "evaluator": IssueScope.EVALUATION,
}


def _make_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _model(component: str) -> ChatOpenAI:
    return create_chat_model(component)


@lru_cache(maxsize=16)
def _planner_model(prompt: str) -> ChatOpenAI:
    return create_chat_model("harness.planner")


@lru_cache(maxsize=16)
def _reporter_model(prompt: str) -> ChatOpenAI:
    return create_chat_model("harness.reporter")


@lru_cache(maxsize=16)
def _critic_model(prompt: str) -> ChatOpenAI:
    return create_chat_model("harness.critic")


@lru_cache(maxsize=16)
def _evaluator_model(prompt: str) -> ChatOpenAI:
    return create_chat_model("harness.evaluator")


@lru_cache(maxsize=1)
def _compressor_model() -> ChatOpenAI:
    return create_chat_model("harness.compressor")


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
        "reporter_round": state["reporter_round"],
        "critic_round": state["critic_round"],
        "max_reporter_rounds": state["max_reporter_rounds"],
        "latest_step_output": state["latest_step_output"],
        "rewrite_reason": state["rewrite_reason"],
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


_VALID_REF_TYPES = {"artifact", "observation", "decision"}

# LLMs sometimes emit ref_type values outside the allowed Literal set (e.g. "issue",
# "step", "observation_id").  Map known variants; everything else falls back to "artifact".
_REF_TYPE_ALIASES: dict[str, str] = {
    "issue": "artifact",
    "step": "artifact",
    "artifact_id": "artifact",
    "observation_id": "observation",
    "decision_id": "decision",
}


def _sanitize_decisions(decisions: list[Any]) -> tuple[list[Any], list[str]]:
    """Coerce invalid evidence_refs.ref_type values and return (cleaned_decisions, warnings)."""
    cleaned: list[Any] = []
    warnings: list[str] = []
    for dec in decisions:
        if not isinstance(dec, dict):
            cleaned.append(dec)
            continue
        refs = dec.get("evidence_refs")
        if not refs:
            cleaned.append(dec)
            continue
        clean_refs: list[Any] = []
        for ref in refs:
            if isinstance(ref, dict):
                rt = ref.get("ref_type", "")
                if rt not in _VALID_REF_TYPES:
                    coerced = _REF_TYPE_ALIASES.get(str(rt).lower(), "artifact")
                    warnings.append(
                        f"evidence_refs.ref_type '{rt}' is invalid; coerced to '{coerced}'"
                    )
                    ref = {**ref, "ref_type": coerced}
            clean_refs.append(ref)
        cleaned.append({**dec, "evidence_refs": clean_refs})
    return cleaned, warnings


def _coerce_thinking_output(payload: Any) -> ThinkingNodeOutput:
    if isinstance(payload, ThinkingNodeOutput):
        return payload
    if isinstance(payload, dict):
        if "decisions" in payload and isinstance(payload["decisions"], list):
            clean_decisions, warnings = _sanitize_decisions(payload["decisions"])
            if warnings:
                structlog.get_logger().warning(
                    "evidence_refs.ref_type coerced", warnings=warnings
                )
            payload = {**payload, "decisions": clean_decisions}
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
        clean_decisions, warnings = _sanitize_decisions(grouped["decisions"])
        if warnings:
            structlog.get_logger().warning(
                "evidence_refs.ref_type coerced", warnings=warnings
            )
        grouped["decisions"] = clean_decisions
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



async def _invoke_json_agent(
    model: ChatOpenAI,
    *,
    system_prompt: str,
    prompt: str,
) -> Any:
    messages = [SystemMessage(content=system_prompt), {"role": "user", "content": prompt}]
    response = await model.ainvoke(messages)
    raw_text = _extract_text(response.content).strip()
    return _parse_json_text(raw_text)


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
                    "owner_node": issue.owner_node or step_id,
                    "scope": issue.scope if issue.scope != IssueScope.REPORT else (
                        _STEP_SCOPE_MAP.get(step_id, IssueScope.REPORT)
                    ),
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
        "reporter_round": state["reporter_round"],
        "critic_round": state["critic_round"],
        "max_reporter_rounds": state["max_reporter_rounds"],
        "latest_step_output": state["latest_step_output"],
        "rewrite_reason": state["rewrite_reason"],
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
    model: ChatOpenAI,
    system_prompt: str,
    store: BaseStore,
    compressor: HarnessStateCompressor | None = None,
) -> HarnessState:
    previous_step = next((step for step in state["steps"] if step.id == step_id), None)
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
        retries=(previous_step.retries + 1) if previous_step is not None else 0,
    )
    steps = _upsert_step(state["steps"], running_step)
    state_with_running_step: HarnessState = {**state, "steps": steps}

    if compressor is not None:
        state_payload = await compressor.compress(
            state_with_running_step,
            model=_compressor_model(),
        )
    else:
        state_payload = _state_to_prompt_payload(state_with_running_step)

    prompt = (
        f"{prompt_prefix}\n\n"
        f"{json_instruction(ThinkingNodeOutput, example='{\"decisions\": [], \"issues\": [], \"artifacts\": []}')}\n\n"
        f"当前 step_id: {step_id}\n"
        f"默认 artifact.type: {default_artifact_type}\n"
        "请直接返回结构化对象，不要输出额外说明。\n\n"
        f"{state_payload}"
    )
    normalized = _normalize_output(
        _coerce_thinking_output(await _invoke_json_agent(model, system_prompt=system_prompt, prompt=prompt)),
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
        "final_artifact_id": next(
            (
                artifact.id
                for artifact in reversed(normalized.artifacts)
                if artifact.type == "report"
            ),
            state["final_artifact_id"],
        ),
        "evaluation_artifact_id": state["evaluation_artifact_id"],
        "reporter_round": state["reporter_round"],
        "critic_round": state["critic_round"],
        "max_reporter_rounds": state["max_reporter_rounds"],
        "latest_step_output": {
            "step_id": step_id,
            "artifact_ids": [artifact.id for artifact in normalized.artifacts],
            "decision_ids": [decision.id for decision in normalized.decisions],
            "issue_ids": [issue.id for issue in normalized.issues],
        },
        "rewrite_reason": state["rewrite_reason"],
    }
    _store_snapshot(store, state["run"].id, step_id, next_state)
    return next_state


def _planner_node_factory(store: BaseStore, compressor: HarnessStateCompressor | None = None):
    async def planner_node(state: HarnessState) -> HarnessState:
        return await _run_thinking_node(
            state,
            step_id="planner",
            kind="plan",
            default_artifact_type="plan",
            prompt_prefix="请为本次 run 生成执行计划与关键关注点。",
            model=_planner_model(PLANNER_NODE_PROMPT),
            system_prompt=PLANNER_NODE_PROMPT,
            store=store,
            compressor=compressor,
        )

    return planner_node


def _reporter_node_factory(store: BaseStore, prompt: str, compressor: HarnessStateCompressor | None = None):
    async def reporter_node(state: HarnessState) -> HarnessState:
        reporter_round = state["reporter_round"] + 1
        state_for_run: HarnessState = {
            **state,
            "reporter_round": reporter_round,
        }
        round_prompt = (
            f"请基于当前状态整合已有产物，生成阶段性报告。\n"
            f"当前 reporter 轮次: {reporter_round}/{state['max_reporter_rounds']}。"
        )
        if state["rewrite_reason"]:
            round_prompt += (
                "\n这是基于 critic 反馈的重写。"
                "你必须优先修复以下问题，再产出新的 report：\n"
                f"- {state['rewrite_reason']}"
            )

        next_state = await _run_thinking_node(
            state_for_run,
            step_id="reporter",
            kind="report",
            default_artifact_type="report",
            prompt_prefix=round_prompt,
            model=_reporter_model(prompt),
            system_prompt=prompt,
            store=store,
            compressor=compressor,
        )
        final_state: HarnessState = {
            **next_state,
            "rewrite_reason": None,
        }
        _store_snapshot(store, state["run"].id, "reporter", final_state)
        return final_state

    return reporter_node


def _resolve_critic_rewrite_request(state: HarnessState) -> tuple[bool, str | None]:
    latest_step_output = state["latest_step_output"] or {}
    if latest_step_output.get("step_id") != "critic":
        return False, None

    issue_ids = set(latest_step_output.get("issue_ids", []))
    decision_ids = set(latest_step_output.get("decision_ids", []))
    critic_issues = [issue for issue in state["issues"] if issue.id in issue_ids]
    critic_decisions = [decision for decision in state["decisions"] if decision.id in decision_ids]

    trigger_issues = [
        issue
        for issue in critic_issues
        if issue.severity in {IssueSeverity.HIGH, IssueSeverity.CRITICAL}
        or issue.category in REWRITE_TRIGGER_CATEGORIES
    ]
    trigger_decisions = [
        decision
        for decision in critic_decisions
        if decision.confidence >= 0.5
        and any(keyword in decision.rationale.lower() for keyword in REWRITE_TRIGGER_KEYWORDS)
    ]
    needs_rewrite = bool(trigger_issues or trigger_decisions)
    if not needs_rewrite:
        return False, None

    reason_parts = [issue.message for issue in trigger_issues[:3]]
    if not reason_parts:
        reason_parts = [decision.rationale for decision in trigger_decisions[:2]]
    if not reason_parts:
        reason_parts = ["critic identified report issues that require a rewrite."]
    return True, "；".join(reason_parts)


def _critic_node_factory(store: BaseStore, prompt: str, compressor: HarnessStateCompressor | None = None):
    async def critic_node(state: HarnessState) -> HarnessState:
        critic_round = state["critic_round"] + 1
        state_for_run: HarnessState = {
            **state,
            "critic_round": critic_round,
        }
        next_state = await _run_thinking_node(
            state_for_run,
            step_id="critic",
            kind="critic",
            default_artifact_type="critique",
            prompt_prefix=(
                "请审查当前 run 的结论、证据和风险缺口。\n"
                f"当前 critic 轮次: {critic_round}。\n"
                "如果 report 需要 reporter 重写，优先通过 high/critical Issue 表达，"
                "并尽量使用以下 category 之一："
                "report_rewrite_needed / missing_cross_evidence / weak_grounding / "
                "cross_source_conflict / time_mismatch / incomplete_report。"
            ),
            model=_critic_model(prompt),
            system_prompt=prompt,
            store=store,
            compressor=compressor,
        )
        needs_rewrite, rewrite_reason = _resolve_critic_rewrite_request(next_state)
        final_state: HarnessState = {
            **next_state,
            "rewrite_reason": rewrite_reason if needs_rewrite else None,
        }
        _store_snapshot(store, state["run"].id, "critic", final_state)
        return final_state

    return critic_node


def _route_after_critic(state: HarnessState) -> str:
    needs_rewrite, _ = _resolve_critic_rewrite_request(state)
    if needs_rewrite and state["reporter_round"] < state["max_reporter_rounds"]:
        return "reporter"
    return "evaluator"


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
        f"{json_instruction(EvaluationAssessment, example='{\"summary\": \"\", \"strengths\": [], \"weaknesses\": [], \"blocking_issues\": [], \"passed\": false, \"recommendation\": \"\", \"improvement_actions\": []}')}\n\n"
        f"定量指标：\n{metrics.model_dump_json(indent=2)}\n\n"
        f"{_state_to_prompt_payload({**state, 'steps': steps})}"
    )
    assessment = _coerce_evaluation_assessment(
        await _invoke_json_agent(_evaluator_model(prompt), system_prompt=prompt, prompt=evaluator_prompt)
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
                scope=IssueScope.EVALUATION,
                owner_node="evaluator",
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
        "reporter_round": state["reporter_round"],
        "critic_round": state["critic_round"],
        "max_reporter_rounds": state["max_reporter_rounds"],
        "latest_step_output": {
            "step_id": "evaluator",
            "artifact_ids": [evaluation_artifact.id],
            "decision_ids": [evaluation_decision.id],
            "issue_ids": [issue.id for issue in evaluation_issues if issue.related_step == running_step.id],
        },
        "rewrite_reason": state["rewrite_reason"],
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
    runtime_context = dict(context or {})
    raw_max_reporter_rounds = runtime_context.get("max_reporter_rounds", DEFAULT_MAX_REPORTER_ROUNDS)
    max_reporter_rounds = (
        raw_max_reporter_rounds
        if isinstance(raw_max_reporter_rounds, int) and raw_max_reporter_rounds > 0
        else DEFAULT_MAX_REPORTER_ROUNDS
    )
    run = Run(
        id=run_id or _make_id("run"),
        goal=goal,
        status=RunStatus.RUNNING,
        context=runtime_context,
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
        "reporter_round": 0,
        "critic_round": 0,
        "max_reporter_rounds": max_reporter_rounds,
        "latest_step_output": None,
        "rewrite_reason": None,
    }


def build_harness_graph(
    *,
    checkpointer: InMemorySaver | None = None,
    store: BaseStore | None = None,
    reporter_prompt: str = REPORTER_NODE_PROMPT,
    critic_prompt: str = CRITIC_NODE_PROMPT,
    evaluator_prompt: str = EVALUATOR_NODE_PROMPT,
    compressor: HarnessStateCompressor | None = None,
) -> tuple[CompiledStateGraph, BaseStore]:
    graph = StateGraph(HarnessState)
    runtime_store = store or InMemoryStore()
    graph.add_node("planner", _planner_node_factory(runtime_store, compressor))
    graph.add_node("reporter", _reporter_node_factory(runtime_store, reporter_prompt, compressor))
    graph.add_node("critic", _critic_node_factory(runtime_store, critic_prompt, compressor))
    graph.add_node("evaluator", _evaluator_node_factory(runtime_store, evaluator_prompt))
    graph.add_edge(START, "planner")
    graph.add_edge("planner", "reporter")
    graph.add_edge("reporter", "critic")
    graph.add_conditional_edges(
        "critic",
        _route_after_critic,
        {
            "reporter": "reporter",
            "evaluator": "evaluator",
        },
    )
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


def _select_state_for_prompt(state: HarnessState) -> dict[str, Any]:
    latest_artifacts = state["artifacts"][-3:]

    high_issues = [
        issue for issue in state["issues"]
        if issue.severity in {IssueSeverity.HIGH, IssueSeverity.CRITICAL}
    ]

    referenced_ids = set()
    for decision in state["decisions"]:
        if decision.confidence >= 0.5:
            referenced_ids.update(decision.based_on)

    

class HarnessRuntime:
    def __init__(
        self,
        *,
        checkpointer: InMemorySaver | None = None,
        store: BaseStore | None = None,
        reporter_prompt: str = REPORTER_NODE_PROMPT,
        critic_prompt: str = CRITIC_NODE_PROMPT,
        evaluator_prompt: str = EVALUATOR_NODE_PROMPT,
        compressor: HarnessStateCompressor | None = None,
        use_state_compression: bool = False,
    ) -> None:
        # Build a default compressor if compression is requested but none provided.
        resolved_compressor: HarnessStateCompressor | None
        if use_state_compression:
            resolved_compressor = compressor if compressor is not None else HarnessStateCompressor()
        else:
            resolved_compressor = None

        self.graph, self.store = build_harness_graph(
            checkpointer=checkpointer,
            store=store,
            reporter_prompt=reporter_prompt,
            critic_prompt=critic_prompt,
            evaluator_prompt=evaluator_prompt,
            compressor=resolved_compressor,
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
        parent_config: RunnableConfig | None = None,
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
        config: RunnableConfig = {"configurable": {"thread_id": thread_id or initial_state["run"].id}}
        if parent_config and parent_config.get("callbacks"):
            config["callbacks"] = parent_config["callbacks"]
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
