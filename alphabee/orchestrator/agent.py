"""Orchestrator — top-level entry point for AlphaBee.

Simplified pipeline:
1. collect_raw_facts      — FactCollector + structured model extraction (concurrent)
2. run_analysis_engines   — DerivedFacts + SignalEngine + AnomalyEngine
3. explore_conflicts      — ConflictExplorer: identify contradictions and gaps
4. verify_hypotheses       — Verify each hypothesis against evidence
5. synthesize_insights     — InsightAgent: synthesize central viewpoint from all upstream
6. run_thesis             — ThesisEngine + optional LLM enhancement
7. review_thesis          — ThesisReviewer (deterministic + optional LLM audit)
8. generate_report        — Single LLM call: structured data → Markdown report
9. review_report          — Harness-as-library quality gate with optional rewrite
10. finalize               — Merge all results into JSON AIMessage
"""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.store.memory import InMemoryStore

from alphabee.agents.schemas import ConflictAnalysisResult
from alphabee.core import (
    Artifact,
    Decision,
    Issue,
    IssueSeverity,
    Step,
    StepStatus,
)
from alphabee.orchestrator.collectors import (
    collect_raw_facts,
)
from alphabee.orchestrator.contracts import (
    SignalAnalysisArtifact,
    ThesisArtifact,
    VerificationArtifact,
    find_artifact_model,
)
from alphabee.orchestrator.gates import review_report, route_after_report_review
from alphabee.orchestrator.nodes.analyze import run_analysis_engines
from alphabee.orchestrator.nodes.conflicts import explore_conflicts
from alphabee.orchestrator.nodes.insights import synthesize_insights
from alphabee.orchestrator.nodes.thesis import run_thesis
from alphabee.orchestrator.nodes.verification import verify_hypotheses
from alphabee.orchestrator.reporter import generate_report
from alphabee.orchestrator.services.company_context import build_company_context
from alphabee.orchestrator.state import OrchestratorState
from alphabee.utils.pipeline import make_id


def _make_id(prefix: str) -> str:
    return make_id(prefix)


# ── review_thesis node ──────────────────────────────────────────────────────


async def review_thesis(
    state: OrchestratorState,
    config: RunnableConfig,
) -> OrchestratorState:
    """Audit the thesis_analysis artifact for analytical quality.

    Runs the ThesisReviewer (Layer 1 deterministic + optional Layer 2 LLM)
    and produces Decisions and Issues for each dimension verdict.
    """
    from alphabee.agents.thesis.registry import load_dimension_defs
    from alphabee.agents.thesis.reviewer import ThesisReviewer

    load_dimension_defs()

    artifacts = state.get("artifacts", [])
    issues = list(state.get("issues", []))
    decisions = list(state.get("decisions", []))
    steps = list(state.get("steps", []))

    step = Step(
        id="review_thesis",
        kind="review_thesis",
        inputs={"artifact_count": len(artifacts)},
        status=StepStatus.RUNNING,
    )

    # ── Find thesis_analysis artifact ──
    # review_thesis 不负责生成新观点，而是审计已有 thesis 是否站得住脚。
    # 这里从 artifact 里拿到 thesis_analysis，确保审查面对的是编排层真正产出的正式结论。
    thesis_payload = find_artifact_model(artifacts, "thesis_analysis", ThesisArtifact)
    if thesis_payload is None:
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
    thesis = _reconstruct_thesis(thesis_payload.thesis)

    # ── Get signal_results for detail-level review ──
    # 维度审查需要回看 signal 粒度的细节：
    # thesis 可能把多个信号压缩成一个维度判断，review 则要检查压缩后是否丢失关键反证。
    signal_val = find_artifact_model(artifacts, "signal_analysis", SignalAnalysisArtifact)
    signal_results = signal_val.results if signal_val else {}

    # ── Get company context ──
    fact_val = _find_artifact_value(artifacts, "fact_collection")
    fact_text = fact_val.get("raw_response", "") if fact_val else ""
    # 审查阶段同样引入公司语境，避免机械地用统一阈值判断所有公司。
    # 例如高成长行业的估值与成熟行业的估值，其审查口径不能完全一致。
    company_ctx = build_company_context(
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
    # 维度 verdict 会沉淀为 Decision，便于最终报告和质量 gate 回溯：
    # 每个维度到底是 confirmed、qualified 还是 contested，都有单独决策对象承载。
    for dim_id, verdict in review.dimension_verdicts.items():
        decisions.append(
            Decision(
                id=_make_id("decision"),
                maker="thesis_reviewer",
                rationale=(
                    f"{verdict.dimension_name}: {verdict.status}. "
                    f"证据数={verdict.evidence_count}, "
                    f"建议={verdict.suggested_action}. " + "; ".join(verdict.issues)
                    if verdict.issues
                    else ""
                ),
                confidence={
                    "confirmed": 0.9,
                    "qualified": 0.7,
                    "insufficient": 0.3,
                    "contested": 0.2,
                }.get(verdict.status, 0.5),
            )
        )

    # ── Produce Issues ──
    # 审查问题按严重度拆成 blocking / warning，
    # 这样最终报告既能突出真正阻断结论的缺陷，也不会丢掉次级风险提示。
    for msg in review.blocking_issues:
        issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.HIGH,
                category="thesis_gap",
                message=msg,
                related_step=step.id,
            )
        )
    for msg in review.warning_issues:
        issues.append(
            Issue(
                id=_make_id("issue"),
                severity=IssueSeverity.MEDIUM,
                category="thesis_warning",
                message=msg,
                related_step=step.id,
            )
        )

    # ── Inject verified conflicts as additional review evidence ──
    conflicts_raw = find_artifact_model(artifacts, "conflicts_result", ConflictAnalysisResult)
    if conflicts_raw:
        verification_results = (
            find_artifact_model(artifacts, "verification_results", VerificationArtifact) or VerificationArtifact()
        )
        verify_by_hid: dict[str, dict] = {
            vr.hypothesis_id: vr.model_dump(mode="json") for vr in verification_results.results
        }

        # 已验证冲突是 thesis review 最重要的反证来源之一：
        # 它意味着“某个疑点不再只是怀疑，而是已经被额外证据部分或全部支持”。
        for conflict in conflicts_raw.conflicts:
            theme = conflict.theme
            severity = conflict.severity
            related_dimensions = list(conflict.related_dimensions)
            conflict_severity = IssueSeverity.HIGH if severity in ("high", "critical") else IssueSeverity.MEDIUM

            for hyp in conflict.hypotheses:
                vstatus = hyp.status
                if vstatus not in ("verified", "partial"):
                    continue

                hid = hyp.id
                vr = verify_by_hid.get(hid, {})
                explanation = hyp.explanation
                gap_hint = f" 缺口: {', '.join(vr.get('gaps', [])[:3])}" if vr.get("gaps") else ""

                issues.append(
                    Issue(
                        id=_make_id("issue"),
                        severity=conflict_severity,
                        category="verified_conflict",
                        message=(f"[冲突已验证] {theme}: {explanation}. 结论: {vr.get('summary', '')}" + gap_hint),
                        related_step=step.id,
                    )
                )

                # 业务含义：如果 thesis 某维度仍然给出正向判断，
                # 但 verified conflict 已经指出该方向存在反证，就要显式制造 thesis_conflict。
                # 这样最终 confidence 会被压低，报告也必须把矛盾写出来。
                for dim_id in related_dimensions:
                    dim = thesis.dimensions.get(dim_id)
                    if dim is None:
                        continue
                    dim_name = dim.name if hasattr(dim, "name") else dim_id
                    judgment = dim.judgment if hasattr(dim, "judgment") else ""
                    if judgment in ("strong_positive", "positive"):
                        issues.append(
                            Issue(
                                id=_make_id("issue"),
                                severity=IssueSeverity.HIGH,
                                category="thesis_conflict",
                                message=(
                                    f"[论点矛盾] 维度'{dim_name}'判断为{judgment}，"
                                    f"但已验证冲突'{theme}'暗示相反方向. "
                                    f"假设: {explanation}"
                                ),
                                related_step=step.id,
                            )
                        )

        # 对被推翻的假设也保留 decision，
        # 这是为了告诉下游“哪些怀疑已经排除”，避免报告把所有疑点都写成悬而未决。
        for conflict in conflicts_raw.conflicts:
            for hyp in conflict.hypotheses:
                if hyp.status != "rejected":
                    continue
                hid = hyp.id
                vr = verify_by_hid.get(hid, {})
                decisions.append(
                    Decision(
                        id=_make_id("decision"),
                        maker="conflict_verifier",
                        rationale=(
                            f"假设已排除: {conflict.theme} — {hyp.explanation}. 推翻理由: {vr.get('summary', '')}"
                        ),
                        confidence=vr.get("contradiction_score", 0.7),
                    )
                )

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

    final_artifact = next((a for a in artifacts if a.id == final_artifact_id), None)
    if final_artifact is None:
        final_artifact = next((a for a in reversed(artifacts) if a.type == "report"), None)

    # finalize_message 的职责是把整条分析链压成一个统一 JSON 响应：
    # 终端流式展示可以直接读取 final_report，而调试/审计端仍能拿到 artifacts / decisions / issues。
    payload = {
        "run": state["run"].model_dump(mode="json") if state.get("run") else None,
        "final_report": (final_artifact.value if final_artifact is not None else None),
        "artifacts": [a.model_dump(mode="json") for a in artifacts],
        "decisions": [d.model_dump(mode="json") for d in state.get("decisions", [])],
        "issues": [i.model_dump(mode="json") for i in state.get("issues", [])],
    }

    return {
        **state,
        "messages": [AIMessage(content=json.dumps(payload, ensure_ascii=False, indent=2))],
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
_graph.add_node("explore_conflicts", explore_conflicts)
_graph.add_node("verify_hypotheses", verify_hypotheses)
_graph.add_node("synthesize_insights", synthesize_insights)
_graph.add_node("run_thesis", run_thesis)
_graph.add_node("review_thesis", review_thesis)
_graph.add_node("generate_report", generate_report)
_graph.add_node("review_report", review_report)
_graph.add_node("finalize_message", finalize_message)

_graph.add_edge(START, "collect_raw_facts")
_graph.add_edge("collect_raw_facts", "run_analysis_engines")
_graph.add_edge("run_analysis_engines", "explore_conflicts")
_graph.add_edge("explore_conflicts", "verify_hypotheses")
_graph.add_edge("verify_hypotheses", "synthesize_insights")
_graph.add_edge("synthesize_insights", "run_thesis")
_graph.add_edge("run_thesis", "review_thesis")
_graph.add_edge("review_thesis", "generate_report")
_graph.add_edge("generate_report", "review_report")
# 整个主流程是单向串联，只有 report quality gate 允许一次受控回环。
# 这样既能保留“先收集事实 → 再计算 → 再形成论点 → 再出报告”的业务顺序，
# 又能在最终交付前对报告结构做一次纠偏。
_graph.add_conditional_edges(
    "review_report",
    route_after_report_review,
    {
        "generate_report": "generate_report",
        "finalize_message": "finalize_message",
    },
)
_graph.add_edge("finalize_message", END)

alphabee_agent = _graph.compile(store=InMemoryStore())
