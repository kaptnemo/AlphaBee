"""Harness-as-library quality gates for the active orchestrator."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from alphabee.agents.schemas import ReportOutput
from alphabee.core import (
    Artifact,
    ArtifactType,
    Decision,
    EvaluateMetrics,
    EvaluationAssessment,
    EvaluationReport,
    Issue,
    IssueScope,
    IssueSeverity,
    IssueStatus,
    RunStatus,
    Step,
    StepStatus,
)
from alphabee.harness.prompts import EVALUATOR_NODE_PROMPT
from alphabee.orchestrator.state import OrchestratorState
from alphabee.utils import create_chat_model, extract_text, json_instruction, make_id, parse_json


def _make_id(prefix: str) -> str:
    return make_id(prefix)


def _find_latest_artifact(artifacts: list[Artifact], artifact_type: str) -> Artifact | None:
    for artifact in reversed(artifacts):
        if artifact.type == artifact_type:
            return artifact
    return None


def _find_latest_report_artifact(state: OrchestratorState) -> Artifact | None:
    artifacts = state.get("artifacts", [])
    final_artifact_id = state.get("final_artifact_id")
    if final_artifact_id:
        for artifact in reversed(artifacts):
            if artifact.id == final_artifact_id:
                return artifact
    return _find_latest_artifact(artifacts, ArtifactType.REPORT)


def _normalize_text_for_search(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _truncate(value: str, limit: int = 200) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "..."


def _base_issues(state: OrchestratorState) -> list[Issue]:
    return [issue for issue in state.get("issues", []) if issue.category != "report_rewrite_needed"]


def _load_report_output(state: OrchestratorState) -> ReportOutput | None:
    report_artifact = _find_latest_report_artifact(state)
    if report_artifact is None or not isinstance(report_artifact.value, dict):
        return None
    try:
        return ReportOutput.model_validate(report_artifact.value)
    except Exception:
        return None


def _issue_disclosure_status(
    state: OrchestratorState,
) -> tuple[list[Issue], set[str], set[str], list[Issue]]:
    source_issues = _base_issues(state)
    report_output = _load_report_output(state)
    disclosed_ids = set(report_output.disclosed_issue_ids) if report_output else set()
    high_priority_issues = [
        issue for issue in source_issues if issue.severity in {IssueSeverity.HIGH, IssueSeverity.CRITICAL}
    ]
    required_ids = {issue.id for issue in high_priority_issues}
    undisclosed = [issue for issue in high_priority_issues if issue.id not in disclosed_ids]
    return source_issues, disclosed_ids, required_ids, undisclosed


def build_evidence_map(state: OrchestratorState) -> list[dict[str, Any]]:
    artifacts = state.get("artifacts", [])
    observations = state.get("observations", [])
    decisions = state.get("decisions", [])
    art_by_id = {artifact.id: artifact for artifact in artifacts}
    obs_by_id = {observation.id: observation for observation in observations}
    art_ids = set(art_by_id)
    obs_ids = set(obs_by_id)

    # report gate 关注的不只是“报告有没有写完整”，
    # 还关注中间结论能否回溯到 artifacts / observations。
    # evidence_map 把 decision -> evidence 的链路压成轻量结构，供 deterministic / LLM gate 共用。
    evidence_map: list[dict[str, Any]] = []
    for decision in decisions:
        refs = decision.resolved_evidence(art_ids, obs_ids)
        if not refs:
            continue

        evidence: list[dict[str, Any]] = []
        for ref in refs:
            if ref.ref_type == "artifact" and ref.ref_id in art_by_id:
                artifact = art_by_id[ref.ref_id]
                entry: dict[str, Any] = {
                    "artifact_id": ref.ref_id,
                    "type": artifact.type,
                }
                if isinstance(artifact.value, dict):
                    raw = artifact.value.get("raw_response", "")
                    if isinstance(raw, str) and raw:
                        entry["snippet"] = _truncate(raw)
                evidence.append(entry)
            elif ref.ref_type == "observation" and ref.ref_id in obs_by_id:
                observation = obs_by_id[ref.ref_id]
                evidence.append(
                    {
                        "observation_id": ref.ref_id,
                        "source": observation.source,
                        "payload_preview": _truncate(json.dumps(observation.payload, ensure_ascii=False)),
                    }
                )
            elif ref.ref_type == "decision":
                evidence.append({"decision_ref_id": ref.ref_id})

        evidence_map.append(
            {
                "decision_id": decision.id,
                "claim": _truncate(decision.rationale, 300),
                "confidence": decision.confidence,
                "evidence": evidence,
            }
        )

    return evidence_map


def compute_report_metrics(state: OrchestratorState) -> EvaluateMetrics:
    report_artifact = _find_latest_report_artifact(state)
    report_value = report_artifact.value if report_artifact is not None else None
    report_output = _load_report_output(state)
    report_payload = (
        report_output.model_dump(mode="json")
        if report_output
        else (report_value if isinstance(report_value, dict) else {})
    )
    sections = report_payload.get("sections", {}) if isinstance(report_payload, dict) else {}

    # 这些 section 对应 AlphaBee 最终交付物的业务契约：
    # 缺任一关键章节，都意味着用户拿到的不是完整“财报质量体检”。
    expected_sections = {
        "executive_summary",
        "key_metrics",
        "signal_analysis",
        "anomaly_detection",
        "conflict_analysis",
        "investment_thesis",
        "review_findings",
        "risks",
        "disclaimer",
    }
    present_sections = {
        key for key in expected_sections if isinstance(sections, dict) and sections.get(key) not in (None, "", [], {})
    }
    coverage_hits = len(present_sections)
    if report_payload.get("summary") not in (None, "", [], {}):
        coverage_hits += 1
    if report_payload.get("overall_confidence") not in (None, "", [], {}):
        coverage_hits += 1
    artifact_coverage = coverage_hits / (len(expected_sections) + 2)

    decisions = state.get("decisions", [])
    evidence_coverage = (
        sum(1 for decision in decisions if decision.based_on or decision.evidence_refs) / len(decisions)
        if decisions
        else 0.0
    )

    # gate 直接读取前面节点沉淀的 issues，
    # 用来判断报告是否把“已知不确定性”如实暴露，而不是只看文案是否流畅。
    source_issues, _, _, undisclosed = _issue_disclosure_status(state)
    issue_categories = {issue.category for issue in source_issues}
    numeric_consistency = not any(
        category in issue_categories for category in {"numeric_inconsistency", "conflict", "cross_source_conflict"}
    )
    cross_source_consistency = not any(
        category in issue_categories
        for category in {"cross_source_conflict", "conflict", "time_mismatch", "thesis_conflict"}
    )

    issue_handling = not undisclosed

    freshness_values = {observation.freshness.value for observation in state.get("observations", [])}
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
        *(artifact.id for artifact in state.get("artifacts", [])),
        *(observation.id for observation in state.get("observations", [])),
        *(issue.id for issue in source_issues),
        *(decision.id for decision in decisions),
    }
    grounded_references = 0
    total_references = 0
    for decision in decisions:
        refs = list(decision.based_on) + [ref.ref_id for ref in decision.evidence_refs]
        total_references += len(refs)
        grounded_references += sum(1 for item in refs if item in valid_ids)
    grounding_score = grounded_references / total_references if total_references else 0.0

    schema_validity = report_output is not None

    overall_confidence = report_payload.get("overall_confidence", "unknown")
    if undisclosed:
        overconfidence_presence = "high" if overall_confidence == "high" else "medium"
    elif (
        any(issue.severity in {IssueSeverity.HIGH, IssueSeverity.CRITICAL} for issue in source_issues)
        and overall_confidence == "high"
    ):
        overconfidence_presence = "medium"
    else:
        overconfidence_presence = "low"

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
        cross_analysis_depth="good" if "conflict_analysis" in present_sections else "shallow",
        fact_inference_distinction="good" if evidence_coverage >= 0.3 else "weak",
        risk_warning_sufficiency="good" if "risks" in present_sections else "weak",
        overconfidence_presence=overconfidence_presence,
        user_usefulness="high" if schema_validity and artifact_coverage >= 0.8 else "medium",
    )


def _deterministic_assessment(state: OrchestratorState, metrics: EvaluateMetrics) -> EvaluationAssessment:
    report_artifact = _find_latest_report_artifact(state)
    report_value = report_artifact.value if report_artifact is not None else {}
    report_payload = report_value if isinstance(report_value, dict) else {}
    _, _, _, undisclosed = _issue_disclosure_status(state)

    blocking_issues: list[str] = []
    weaknesses: list[str] = []
    strengths: list[str] = []

    # deterministic gate 的定位是“最低交付标准守门员”：
    # 哪怕 LLM 审查关闭或失败，它也要能稳定挡住缺章节、缺风险披露、强冲突未处理等问题。
    high_issues = [
        issue for issue in _base_issues(state) if issue.severity in {IssueSeverity.HIGH, IssueSeverity.CRITICAL}
    ]
    for issue in high_issues[:4]:
        blocking_issues.append(issue.message)

    if not metrics.schema_validity:
        blocking_issues.append("报告输出缺少必需字段，未达到可交付结构。")
    if metrics.artifact_coverage < 0.8:
        blocking_issues.append("报告关键章节覆盖不足，无法完整表达主流程分析结果。")
    if not metrics.issue_handling:
        missing = "；".join(f"{issue.id}:{issue.category}" for issue in undisclosed[:4])
        blocking_issues.append(f"报告没有充分显式披露高优先级问题，至少遗漏：{missing}。")
    if not metrics.cross_source_consistency:
        blocking_issues.append("当前结果存在跨来源或跨维度冲突，报告未形成稳定结论。")

    if metrics.schema_validity:
        strengths.append("报告结构完整，基本符合 AlphaBee 最终输出 schema。")
    if metrics.artifact_coverage >= 0.8:
        strengths.append("报告较完整覆盖了指标、异常、冲突、论点与风险。")
    if metrics.issue_handling:
        strengths.append("报告对已有问题有显式披露，没有把不确定性完全隐藏。")

    if metrics.evidence_coverage < 0.3:
        weaknesses.append("中间决策的证据引用仍偏少，报告 grounding 能力有限。")
    if metrics.grounding_score < 0.5:
        weaknesses.append("部分结论的可追溯证据链仍然偏弱。")
    if report_payload.get("overall_confidence") == "high" and high_issues:
        weaknesses.append("存在高优先级问题时仍给出高置信度，容易显得过度自信。")
    if undisclosed:
        weaknesses.append("高优先级 issue 未全部进入 disclosed_issue_ids，报告的风险披露映射不完整。")

    passed = not blocking_issues
    recommendation = (
        "可以继续交付当前报告。" if passed else "请根据阻断问题重写报告，优先修复风险披露、冲突呈现和结构覆盖。"
    )
    summary = "报告已达到基本交付标准。" if passed else "报告尚未达到交付标准，需要一次面向问题的重写。"

    return EvaluationAssessment(
        summary=summary,
        strengths=strengths,
        weaknesses=weaknesses,
        blocking_issues=blocking_issues,
        passed=passed,
        recommendation=recommendation,
        improvement_actions=[
            "补全缺失章节并保持与 thesis / anomaly / conflict 结果一致。",
            "显式呈现高优先级问题，不要把 unresolved gap 隐藏在弱措辞里。",
        ]
        if not passed
        else [],
    )


async def _llm_assessment(
    state: OrchestratorState,
    metrics: EvaluateMetrics,
) -> EvaluationAssessment:
    report_artifact = _find_latest_report_artifact(state)
    report_value = report_artifact.value if report_artifact is not None else None
    evidence_map = build_evidence_map(state)
    prompt = (
        "请作为主流程的 report quality gate，评估当前报告是否可交付。\n\n"
        + json_instruction(
            EvaluationAssessment,
            example='{"summary":"","strengths":[],"weaknesses":[],"blocking_issues":[],"passed":false,"recommendation":"","improvement_actions":[]}',
        )
        + "\n\n"
        + "定量指标：\n"
        + metrics.model_dump_json(indent=2)
        + "\n\n当前报告：\n"
        + json.dumps(report_value, ensure_ascii=False, indent=2)
        + "\n\n问题列表：\n"
        + json.dumps(
            [
                {
                    "severity": issue.severity.value,
                    "category": issue.category,
                    "message": issue.message,
                }
                for issue in _base_issues(state)
            ],
            ensure_ascii=False,
            indent=2,
        )
        + "\n\n证据映射：\n"
        + json.dumps(evidence_map, ensure_ascii=False, indent=2)
    )
    model = create_chat_model("harness.evaluator")
    response = model.invoke(
        [
            SystemMessage(content=EVALUATOR_NODE_PROMPT),
            HumanMessage(content=prompt),
        ]
    )
    payload = parse_json(extract_text(response.content).strip())
    return EvaluationAssessment.model_validate(payload)


async def review_report(
    state: OrchestratorState,
    config: RunnableConfig,
) -> OrchestratorState:
    """Evaluate the generated report and request one rewrite when needed."""
    del config
    steps = list(state.get("steps", []))
    issues = list(state.get("issues", []))
    decisions = list(state.get("decisions", []))
    artifacts = list(state.get("artifacts", []))
    run = state.get("run")

    step = Step(
        id="review_report",
        kind="review_report",
        inputs={
            "artifact_count": len(artifacts),
            "report_review_round": state.get("report_review_round", 0) + 1,
        },
        status=StepStatus.RUNNING,
    )
    review_round = state.get("report_review_round", 0) + 1

    report_artifact = _find_latest_report_artifact(state)
    if report_artifact is None:
        completed_step = step.model_copy(update={"status": StepStatus.SKIPPED, "outputs": []})
        return {
            **state,
            "steps": [*steps, completed_step],
        }

    # 先做结构化打分，再决定是否引入 LLM gate。
    # 这样即使 LLM 不可用，最关键的交付约束仍然是可重复、可解释的。
    metrics = compute_report_metrics(state)
    use_llm = state.get("llm_review", False)
    try:
        assessment = await _llm_assessment(state, metrics) if use_llm else _deterministic_assessment(state, metrics)
    except Exception as exc:
        fallback = _deterministic_assessment(state, metrics)
        fallback.weaknesses.append(f"LLM report gate failed, fell back to deterministic review: {exc}")
        assessment = fallback

    evaluation_report = EvaluationReport(
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
        type=ArtifactType.EVALUATION_REPORT,
        producer_step=step.id,
        value=evaluation_report.model_dump(mode="json"),
    )
    artifacts.append(evaluation_artifact)

    decisions.append(
        Decision(
            id=_make_id("decision"),
            maker="report_quality_gate",
            rationale=assessment.recommendation,
            confidence=0.9 if assessment.passed else 0.7,
            based_on=[
                report_artifact.id,
                evaluation_artifact.id,
            ],
        )
    )

    # 只有“确实没过 gate 且存在明确阻断项”时才触发重写。
    # 这避免因为轻微措辞问题反复重写，保持编排层对重试次数的可控性。
    rewrite_needed = not assessment.passed and bool(assessment.blocking_issues)
    rewrite_reason = "；".join(assessment.blocking_issues[:3]) if rewrite_needed else None
    retries_remaining = rewrite_needed and review_round < state.get("max_report_review_rounds", 2)
    if not rewrite_needed:
        issues = [
            issue.model_copy(
                update={
                    "status": IssueStatus.RESOLVED if issue.category == "report_rewrite_needed" else issue.status,
                    "resolution_evidence": report_artifact.id
                    if issue.category == "report_rewrite_needed"
                    else issue.resolution_evidence,
                }
            )
            if issue.category == "report_rewrite_needed" and issue.status == IssueStatus.OPEN
            else issue
            for issue in issues
        ]
    for message in assessment.blocking_issues:
        issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.HIGH,
                category="report_rewrite_needed",
                message=message,
                related_step=step.id,
                related_artifact=report_artifact.id,
                scope=IssueScope.EVALUATION,
                owner_node="review_report",
            )
        )

    completed_step = step.model_copy(
        update={
            "status": StepStatus.SUCCEEDED,
            "outputs": [evaluation_artifact.id],
        }
    )

    next_run = run
    if next_run is not None:
        if assessment.passed:
            next_run = next_run.model_copy(update={"status": RunStatus.SUCCEEDED, "ended_at": datetime.now()})
        elif not retries_remaining:
            next_run = next_run.model_copy(update={"status": RunStatus.PARTIAL, "ended_at": datetime.now()})

    return {
        **state,
        "run": next_run,
        "steps": [*steps, completed_step],
        "artifacts": artifacts,
        "decisions": decisions,
        "issues": issues,
        "evaluation_artifact_id": evaluation_artifact.id,
        "report_review_round": review_round,
        "report_rewrite_needed": rewrite_needed,
        "report_rewrite_reason": rewrite_reason,
    }


def route_after_report_review(state: OrchestratorState) -> str:
    # report review 是图里唯一允许回环的节点：
    # 若 gate 认为当前报告还能通过一次定向修补改善，就回到 generate_report；
    # 否则直接结束，避免无限重写。
    if state.get("report_rewrite_needed") and state.get("report_review_round", 0) < state.get(
        "max_report_review_rounds", 2
    ):
        return "generate_report"
    return "finalize_message"
