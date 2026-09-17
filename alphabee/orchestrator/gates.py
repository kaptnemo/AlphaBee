"""Harness-as-library quality gates for the active orchestrator."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from alphabee.agents.schemas import ReportOutput
from alphabee.core import (
    Artifact,
    ArtifactType,
    Decision,
    DeviationClass,
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
from alphabee.orchestrator.contracts import AssumptionRegistryArtifact, find_artifact_model
from alphabee.orchestrator.node_contracts import get_contract
from alphabee.orchestrator.recovery import (
    BUDGET_EXHAUSTED_ISSUE_CATEGORY,
    RERUN_BUDGET_CHECK_CATEGORY,
    RecoveryDecision,
    RecoveryTier,
    choose_recovery,
    recovery_switches,
)
from alphabee.orchestrator.services import detection
from alphabee.orchestrator.state import OrchestratorState
from alphabee.utils import create_structured_model, extract_text, json_instruction, make_id, parse_json

logger = logging.getLogger(__name__)


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


def build_invalidated_assumption_issues(state: OrchestratorState, step_id: str) -> list[Issue]:
    """报告 gate 的「依赖已证伪假设」检查（§6.3 / §13 F1）。

    **严格 no-op 语义（C2）**：开关关闭、登记簿 artifact 不存在、或登记簿存在但无 invalidated
    假设 → 返回空列表（不产 issue、不改 gate 判定与返回值）；只有「登记簿确实存在且含已证伪
    假设」才产出一条 D3 提示（非阻塞：不进 blocking_issues、不触发重写）。
    """
    if not detection.detection_switches():
        return []
    artifacts = list(state.get("artifacts", []))
    if not any(artifact.type == ArtifactType.ASSUMPTION_REGISTRY for artifact in artifacts):
        return []
    registry = find_artifact_model(artifacts, ArtifactType.ASSUMPTION_REGISTRY, AssumptionRegistryArtifact)
    if registry is None:
        return []
    invalidated = registry.invalidated
    if not invalidated:
        return []
    statements = "；".join(entry.statement or entry.id for entry in invalidated[:3])
    return [
        Issue(
            id=_make_id("issue"),
            severity=IssueSeverity.MEDIUM,
            category="assumption_based_claim",
            message=(
                f"报告结论可能依赖 {len(invalidated)} 条已被证伪的假设：{statements}"
                "（不得作为论证前提，除非显式引用反驳证据）"
            ),
            related_step=step_id,
            scope=IssueScope.REPORT,
            owner_node="review_report",
        )
    ]


def _load_report_output(state: OrchestratorState) -> ReportOutput | None:
    report_artifact = _find_latest_report_artifact(state)
    if report_artifact is None or not isinstance(report_artifact.value, dict):
        return None
    try:
        return ReportOutput.model_validate(report_artifact.value)
    except Exception:
        return None


# cross_source_consistency 口径修正。
# 冲突 issue 要区分两类，不能一股脑都算“跨来源不一致”：
#
# - UNRESOLVED_INCONSISTENCY：尚未结算/尚未消解的真实不一致。
#   cross_source_conflict / time_mismatch（数据源或时点口径打架）、
#   numeric_inconsistency（数字自洽性被打破）、conflict（未分类冲突）。
#   只要命中任一类别，报告就尚未形成稳定结论，cross_source_consistency 应为 False。
#
# - SETTLED_CONFLICT：已被验证流程结算或已被识别的论点矛盾。
#   verified_conflict（verify_hypotheses 已证实的高严重度冲突）与
#   thesis_conflict（review_thesis 识别的正向论点 vs 已验证冲突）。
#   它们是“做了实质冲突验证后如实披露”的结果，不是“跨来源打架”；
#   若把它们算进 cross_source_consistency，任何认真做了验证的报告都会必然 False，
#   再叠加 _deterministic_assessment 触发无意义重写。
#   这类冲突的披露义务由 issue_handling（disclosed_issue_ids）承接，见 _issue_disclosure_status。
UNRESOLVED_INCONSISTENCY: frozenset[str] = frozenset(
    {"cross_source_conflict", "time_mismatch", "numeric_inconsistency", "conflict"}
)
SETTLED_CONFLICT: frozenset[str] = frozenset({"verified_conflict", "thesis_conflict"})


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
        "investment_viewpoint",
        "scenario_analysis",
        "key_metrics",
        "signal_analysis",
        "anomaly_detection",
        "conflict_analysis",
        "dimension_analysis",
        "review_findings",
        "falsification_conditions",
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
    source_issues, disclosed_ids, _, undisclosed = _issue_disclosure_status(state)
    issue_categories = {issue.category for issue in source_issues}
    numeric_consistency = not any(
        category in issue_categories for category in {"numeric_inconsistency", "conflict", "cross_source_conflict"}
    )
    # cross_source_consistency 只统计“未结算/未消解”的不一致（UNRESOLVED_INCONSISTENCY）。
    # verified_conflict / thesis_conflict 是“已结算冲突”（SETTLED_CONFLICT），
    # 它们代表验证/审查流程正常工作，而非“跨来源打架”；若继续把它们算进这里，
    # 任何做了实质验证的报告都会必然 false 并触发无意义重写。
    cross_source_consistency = not any(category in issue_categories for category in UNRESOLVED_INCONSISTENCY)

    # SETTLED_CONFLICT 的披露义务由 issue_handling 承接：它们不再算“跨来源不一致”，
    # 但作为已被验证/识别的高严重度冲突，仍必须出现在报告的 disclosed_issue_ids 中。
    # 这里显式把“已结算冲突未披露”单独兜底（而非只依赖“所有 high/critical 都要披露”），
    # 让披露契约不因未来 severity 口径调整而悄悄失效。
    settled_undisclosed = [
        issue for issue in source_issues if issue.category in SETTLED_CONFLICT and issue.id not in disclosed_ids
    ]
    issue_handling = not undisclosed and not settled_undisclosed

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
    # 只有未结算/未消解的不一致（UNRESOLVED_INCONSISTENCY）才会触发阻断；
    # 已结算冲突（SETTLED_CONFLICT）走 issue_handling 的披露检查，不在这里阻断。
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
        + json_instruction(EvaluationAssessment)
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
    model = create_structured_model("harness.evaluator")
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
    issues = list(state.get("issues", []))
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
        return {"steps": [completed_step]}

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
    new_artifacts = [evaluation_artifact]

    new_decisions = [
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
    ]

    # 只有“确实没过 gate 且存在明确阻断项”时才触发重写。
    # 这避免因为轻微措辞问题反复重写，保持编排层对重试次数的可控性。
    rewrite_needed = not assessment.passed and bool(assessment.blocking_issues)
    rewrite_reason = "；".join(assessment.blocking_issues[:3]) if rewrite_needed else None
    # F2-3：本轮的“是否回环”由 choose_recovery 统一裁决（唯一判据，与 route_after_report_review 共用）。
    # t51（F2-4）口径：gate 消费的是**本次 gate 运行结束后的总轮次**（= 自增后写回 state 的
    # ``report_review_round``，也正是 route 读到的值、以及 pre-F2 gate 判据的左操作数），
    # 因此 gate 与 route 对"还能不能再跑一轮"**必然同判**（k=1 边界曾因两侧快照不同而分歧）。
    rerun_decision = _report_rerun_decision(
        state,
        used_rerun_rounds=max(review_round, int(state.get("report_review_round", 0))),
        rewrite_needed=rewrite_needed,
    )
    # 单一谓词：它同时决定 ① 是否落 D5 预算偏离 ② RunStatus 是否 PARTIAL。
    # t51（F2-4）：此前 RunStatus 只看 ``action``，而 D5 只看 ``issue_category`` ⇒ k=1 边界出现
    # "route 不回环、run 却不标 PARTIAL"的基线回归；现在两者共用下面这一个布尔值。
    budget_exhausted = rewrite_needed and rerun_decision.issue_category == BUDGET_EXHAUSTED_ISSUE_CATEGORY
    # 回滚/fail-open 路径（基线谓词）**不产生** D5（零新增行为，见 _baseline_rerun_decision），
    # 但 pre-F2 在"要重写却没回环"时**本来就**会把 run 标为 PARTIAL ⇒ 这里用与基线同源的
    # 判据补上，保证 `deviation.recovery.enabled=false` 真的是纯回滚（回环能力保留 + 无新记录）。
    partial_run = budget_exhausted or (rewrite_needed and not rerun_decision.action.startswith("rerun"))
    updated_issues: list[Issue] = []

    # P0-④ 证据链前置校验（非阻塞）：若本轮到 gate 时所有 Decision 都缺
    # based_on / evidence_refs，说明中间结论无法回溯到 artifact/observation，
    # 产出 DATA-scope warning 提醒证据链未闭环。它只作提示，不进入 blocking_issues，
    # 不会因“引用缺失”反复触发重写（引用缺失是过程质量问题，不是交付红线）。
    decisions = state.get("decisions", [])
    if decisions and not any(d.based_on or d.evidence_refs for d in decisions):
        updated_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="evidence_chain_incomplete",
                message="所有中间 Decision 均缺少 based_on/evidence_refs 证据引用，证据链未闭环。",
                related_step=step.id,
                scope=IssueScope.DATA,
                owner_node="review_report",
            )
        )

    # 假设生命周期检查（§6.3 / C2）：登记簿缺失或开关关闭时严格 no-op（返回空列表）。
    # 只作 D3 提示，**不进 blocking_issues**，因此不会改变 gate 判定与返回值。
    updated_issues.extend(build_invalidated_assumption_issues(state, step.id))

    # F2-2 的**生产者**：回环预算耗尽（D5 控制偏离）必须有机器可判的落库物。
    # - 触发条件：本轮确实要重写（rewrite_needed）但裁决器已拒绝回环（预算耗尽 → escalate）；
    # - category 来自 RecoveryDecision.issue_category（机读），**不是**从 reason 文案里抠词；
    # - recovery_cost 直接取 decision.cost（§14.3-A：可直接写 Issue.recovery_cost）；
    # - 只在本轮落一条，且此时图必然收口（不回环）⇒ 不会与下一轮的 _base_issues 互相影响。
    if budget_exhausted:
        updated_issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.HIGH,
                category=BUDGET_EXHAUSTED_ISSUE_CATEGORY,
                message=(
                    f"报告重写预算耗尽（report_review_round={review_round} >= "
                    f"max_report_review_rounds={state.get('max_report_review_rounds', 2)}），"
                    f"按恢复阶梯升级（Tier {int(rerun_decision.tier)}）而不再回环：{rerun_decision.reason}"
                ),
                related_step=step.id,
                related_artifact=report_artifact.id,
                scope=IssueScope.REVIEW,
                owner_node="review_report",
                deviation_class=DeviationClass.D5_CONTROL,
                detected_at_step="review_report",
                recovery_action=rerun_decision.action,
                recovery_cost=rerun_decision.cost,
            )
        )

    if not rewrite_needed:
        updated_issues.extend(
            issue.model_copy(
                update={
                    "status": IssueStatus.RESOLVED,
                    "resolution_evidence": report_artifact.id,
                }
            )
            for issue in issues
            if issue.category == "report_rewrite_needed" and issue.status == IssueStatus.OPEN
        )
    for message in assessment.blocking_issues:
        updated_issues.append(
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
        elif partial_run:
            # t51（F2-4/F2-5）：还要重写但本轮无回环 ⇒ 交付"部分完成"。
            # pre-F2 的等价写法是 ``not (rewrite_needed and written < limit)``；本分支与它同判，
            # 且与 route 的最终路由结果同源（route 不回环 ⟺ 这里置 PARTIAL）。
            next_run = next_run.model_copy(update={"status": RunStatus.PARTIAL, "ended_at": datetime.now()})

    return {
        **({"run": next_run} if next_run is not None else {}),
        "steps": [completed_step],
        "artifacts": new_artifacts,
        "decisions": new_decisions,
        "issues": updated_issues,
        "evaluation_artifact_id": evaluation_artifact.id,
        "report_review_round": review_round,
        "report_rewrite_needed": rewrite_needed,
        "report_rewrite_reason": rewrite_reason,
    }


def _report_rerun_decision(
    state: OrchestratorState, *, used_rerun_rounds: int, rewrite_needed: bool
) -> RecoveryDecision:
    """report gate 的"是否回环"裁决（F2-3 / §14.3-A）：交给 :func:`choose_recovery` 统一裁决。

    :param used_rerun_rounds: **本次裁决结束后的总轮次**（= 自增后写回 ``report_review_round`` 的值；
        gate 传 ``review_round``，route 传它读到的 ``state["report_review_round"]``）——两处**同值同源**，
        故 gate 与 route 必然同判（t51/F2-4：此前 gate 传自增前值，k=1 边界与 route 分歧）。
    :param rewrite_needed: 本轮是否确实需要重写（route 传 state 里的 ``report_rewrite_needed``，
        gate 传自己算出的 ``rewrite_needed``）。仅用于开关关闭时的**基线谓词**。

    **为什么放在这里**：route 函数（条件边）只能返回节点名、**不能写 state**，因此"唯一裁决点"
    落在这个**能写 state**、又持有同一快照的一层：:func:`review_report` 与
    :func:`route_after_report_review` 共用本函数，不存在第二个判据。

    **行为等价（与 pre-F2 内联判据逐字对齐）**：

    .. code-block:: python

       rewrite_needed and used < state["max_report_review_rounds"]

    预算未耗尽 → **Tier 4（回环）**；耗尽 → **Tier 5（escalate，不回环 + 落 D5 预算偏离）**。
    上限取 ``state["max_report_review_rounds"]``（缺失时由 :func:`choose_recovery` 回落到契约
    ``max_retries``）。

    **回滚口径（F2-5）**：关 ``deviation.recovery.enabled``（§14.8 PR5）或裁决/配置异常时，
    直接按**同一条基线谓词**返回 Tier4/Tier5 —— 即"关闭开关 == 回到 pre-F2 行为"，
    **不是**"一律不回环"。这样才能真正回滚：pre-F2 在预算尚存时是允许回环的。
    """
    try:
        limit = int(state.get("max_report_review_rounds", 2))
        used = max(int(used_rerun_rounds), 0)
        # 回滚 / fail-open 路径：与基线谓词同判（F2-5）。放在最前面，
        # 保证"配置不可用"时引擎仍按 pre-F2 语义工作。
        if not recovery_switches():
            return _baseline_rerun_decision(used, limit, rewrite_needed, "recovery 开关关闭 → 基线谓词")
        contract = get_contract("generate_report")
        if contract is None:
            return _baseline_rerun_decision(used, limit, rewrite_needed, "generate_report 契约缺失 → 基线谓词")
        # 只读视图：仅覆盖计数器键（不改 state；§14.0 只读约束）。
        counter_view = {"report_review_round": used, "max_report_review_rounds": limit}
        # 触发偏离：回环裁决问的是"还能不能再跑一轮"（纯预算问题）。ladder 的第 1 条分支是
        # "无 issue → T0"，故必须带一条**不命中可修补/可降级/骨架类目**的偏离，才能落到
        # "可重试"分支。哨兵常量见 recovery.RERUN_BUDGET_CHECK_CATEGORY（不落库、不登记分类表）。
        budget_probe = Issue(
            id="report-rerun-budget-probe",
            severity=IssueSeverity.HIGH,
            category=RERUN_BUDGET_CHECK_CATEGORY,
            message="report gate 请求一次定向重写：本次回环预算检查",
            related_step="review_report",
        )
        return choose_recovery("generate_report", [budget_probe], contract=contract, state=counter_view)
    except Exception as exc:  # noqa: BLE001 - 裁决异常绝不打断 run（fail-open 到**基线**判据）
        logger.warning("report rerun arbitration failed (fail-open to baseline): %s", exc)
        return _baseline_rerun_decision(
            max(int(used_rerun_rounds), 0),
            int(state.get("max_report_review_rounds", 2)),
            rewrite_needed,
            "裁决异常 → 基线谓词",
        )


def _baseline_rerun_decision(used: int, limit: int, rewrite_needed: bool, reason: str) -> RecoveryDecision:
    """pre-F2 基线谓词的 RecoveryDecision 投影（回滚与 fail-open 共用，F2-5）。

    基线谓词：``rewrite_needed and used < limit`` ⇒ 回环（Tier 4）；否则 escalate（Tier 5）。

    **显式契约（t55 定稿，captain 裁定方案 (B)「纯回滚」）**：
    ``deviation.recovery.enabled=false`` 必须**逐格等于 pre-F2 行为** ——
    ① 回环能力**保留**（预算尚存时照样 Tier 4）；② ``RunStatus`` 与 pre-F2 一致
    （"要重写却没回环" ⇒ ``PARTIAL``）；③ **不新增** D5 ``budget_exhausted`` 记录
    （本路径的 escalate 刻意**不带** ``issue_category``）。

    **裁定理由**：D5 的**生产者本身是 F2 新增物**（F2-2）；关掉 F2 的开关却新增记录，
    恰恰不构成"回滚"。§14.8 PR5「残留 issue 无害」应读作"回滚后账本里**既有**的残留记录
    无需清理"，而非"回滚必须新增记录"。

    **被否决的备选（(A) 回滚仍落 D5）及其代价，留档避免反向重排**：好处是可避免
    "run 标 PARTIAL 而账本零记录"的观感矛盾；否决原因是它使回滚路径**不再等于** pre-F2，
    且该矛盾在 pre-F2 本来就存在（基线语义如此），不属于回滚引入的问题。
    若将来改判为 (A)，必须**四处同改**：本 docstring、§14.8 PR5 行、
    ``DeviationRecoverySettings`` docstring、以及两条开关关闭 pinning 用例（见
    ``test_gate_baseline_path_adds_no_deviation_record_when_recovery_switch_off``）。
    """
    if rewrite_needed and used < limit:
        return RecoveryDecision(RecoveryTier.TIER_4_RERUN, f"rerun_round={used + 1}", 4, f"基线谓词：{reason}")
    if rewrite_needed:
        return RecoveryDecision(
            RecoveryTier.TIER_5_ESCALATE,
            "escalated",
            5,
            f"基线谓词：回环预算耗尽（{used} >= {limit}）｜{reason}",
        )
    return RecoveryDecision(RecoveryTier.TIER_5_ESCALATE, "escalated", 5, f"基线谓词：无需重写｜{reason}")


def route_after_report_review(state: OrchestratorState) -> str:
    # report review 是图里唯一允许回环的节点：
    # 若 gate 认为当前报告还能通过一次定向修补改善，就回到 generate_report；
    # 否则直接结束，避免无限重写。
    #
    # F2-3：该判据是"基于契约阶梯 + 轮次预算的恢复裁决"，不再内联手写 —— 由
    # _report_rerun_decision → choose_recovery 统一裁决（行为等价，见该函数 docstring）。
    # t51（F2-4）：route 传的是自己读到的 ``report_review_round``（gate 写回后的总轮次），
    # 与 gate 侧传入的同值 ⇒ 两侧对"还能不能再跑一轮"必然同判。
    rewrite_needed = bool(state.get("report_rewrite_needed"))
    if rewrite_needed and _report_rerun_decision(
        state,
        used_rerun_rounds=int(state.get("report_review_round", 0)),
        rewrite_needed=rewrite_needed,
    ).action.startswith("rerun"):
        return "generate_report"
    return "finalize_message"
