"""P0-④：Decision 补 based_on 证据引用。"""

import asyncio

from alphabee.agents.schemas import (
    ConflictAnalysisResult,
    ConflictItem,
    HypothesisItem,
    VerificationResultItem,
)
from alphabee.agents.thesis.models import CompanyContext, DimensionVerdict, ThesisReview
from alphabee.core import Artifact, ArtifactType, Decision, IssueScope, Run, RunStatus
from alphabee.orchestrator import agent as agent_module
from alphabee.orchestrator.collectors import _find_artifact_id
from alphabee.orchestrator.gates import (
    build_evidence_map,
    compute_report_metrics,
    review_report,
)
from alphabee.orchestrator.nodes import verification as verification_node


def _base_run():
    return Run(
        id="run-1",
        goal="分析贵州茅台",
        status=RunStatus.RUNNING,
        context={"symbol": "600519.SH", "query": "分析贵州茅台"},
    )


def _conflict_result() -> ConflictAnalysisResult:
    return ConflictAnalysisResult(
        conflicts=[
            ConflictItem(
                id="c1",
                theme="盈利增长但现金流恶化",
                description="利润增长没有被现金流验证。",
                related_dimensions=["earnings_quality"],
                severity="high",
                confidence=0.9,
                hypotheses=[
                    HypothesisItem(
                        id="h1",
                        conflict_id="c1",
                        explanation="收入确认前置，回款滞后",
                        predictions=["经营现金流/净利润持续低于1"],
                        required_evidence=["financial_facts"],
                        score=0.8,
                    )
                ],
            )
        ]
    )


def _state_with_conflicts() -> dict:
    return {
        "run": _base_run(),
        "steps": [],
        "artifacts": [
            Artifact(
                id="artifact-conflicts",
                type=ArtifactType.CONFLICTS_RESULT,
                producer_step="explore_conflicts",
                value=_conflict_result().model_dump(mode="json"),
            )
        ],
        "issues": [],
        "decisions": [],
    }


def _patch_verify_rejected(monkeypatch):
    async def fake_verify_single_conflict(conflict, shared_context, step_id, config):
        return (
            [
                VerificationResultItem(
                    id="v1",
                    hypothesis_id="h1",
                    status="rejected",
                    support_score=0.2,
                    contradiction_score=0.8,
                    confidence=0.8,
                    gaps=[],
                    summary="反证充分，假设被推翻。",
                )
            ],
            [],
        )

    monkeypatch.setattr(verification_node, "_verify_single_conflict", fake_verify_single_conflict)
    monkeypatch.setattr(verification_node, "build_verify_context", lambda state, symbol: {})


def _complete_report():
    return {
        "title": "600519.SH 财报质量体检报告 — 2024Q4",
        "sections": {
            "executive_summary": "总结",
            "investment_viewpoint": "观点",
            "scenario_analysis": "情景",
            "key_metrics": "表格",
            "signal_analysis": "信号",
            "anomaly_detection": "异常",
            "conflict_analysis": "冲突",
            "dimension_analysis": "维度",
            "review_findings": "审查",
            "falsification_conditions": "证伪",
            "risks": "风险",
            "disclaimer": "免责声明",
        },
        "summary": "总体判断",
        "risk_count": {"high": 0, "medium": 1, "low": 1, "blocked": 0},
        "overall_confidence": "medium",
        "disclosed_issue_ids": [],
    }


def test_find_artifact_id_returns_latest_matching_id():
    artifacts = [
        Artifact(id="a1", type="fact_collection", producer_step="s1", value={}),
        Artifact(id="a2", type="signal_analysis", producer_step="s2", value={}),
    ]
    assert _find_artifact_id(artifacts, "fact_collection") == "a1"
    assert _find_artifact_id(artifacts, "signal_analysis") == "a2"
    assert _find_artifact_id(artifacts, "missing_type") is None


def test_verification_rejected_decision_has_based_on(monkeypatch):
    _patch_verify_rejected(monkeypatch)

    result = asyncio.run(verification_node.verify_hypotheses(_state_with_conflicts(), {}))

    decisions = result.get("decisions", [])
    assert len(decisions) == 1
    # rejected 决策必须能回溯到其消费的 conflicts_result artifact。
    assert decisions[0].based_on == ["artifact-conflicts"]


def test_review_thesis_decisions_have_based_on(monkeypatch):
    import alphabee.agents.thesis.reviewer as reviewer_module

    thesis_dict = {
        "symbol": "600519.SH",
        "period": "2024Q4",
        "overall_judgment": "positive",
        "dimensions": {
            "earnings_quality": {
                "id": "earnings_quality",
                "name": "盈利质量",
                "judgment": "positive",
                "score": 0.4,
                "confidence": 0.8,
                "evidence": [],
            }
        },
    }

    artifacts = [
        Artifact(
            id="a-fact",
            type=ArtifactType.FACT_COLLECTION,
            producer_step="collect_raw_facts",
            value={"agent": "FactCollector", "query": "q", "symbol": "600519.SH", "raw_response": ""},
        ),
        Artifact(
            id="a-thesis",
            type=ArtifactType.THESIS_ANALYSIS,
            producer_step="run_thesis",
            value={"thesis": thesis_dict},
        ),
        Artifact(
            id="a-signal",
            type=ArtifactType.SIGNAL_ANALYSIS,
            producer_step="run_analysis_engines",
            value={"results": {}, "rule_count": 0},
        ),
    ]

    class FakeReviewer:
        def review(self, thesis, signal_results, company_context, use_llm=False):
            return ThesisReview(
                symbol=thesis.symbol,
                period=thesis.period,
                dimension_verdicts={
                    "earnings_quality": DimensionVerdict(
                        dimension_id="earnings_quality",
                        dimension_name="盈利质量",
                        status="confirmed",
                        evidence_count=2,
                        suggested_action="accept",
                    )
                },
                overall_status="passed",
            )

    monkeypatch.setattr(reviewer_module, "ThesisReviewer", lambda: FakeReviewer())
    monkeypatch.setattr(
        agent_module,
        "build_company_context",
        lambda **kwargs: CompanyContext(symbol="600519.SH"),
    )

    result = asyncio.run(
        agent_module.review_thesis(
            {
                "run": _base_run(),
                "steps": [],
                "artifacts": artifacts,
                "issues": [],
                "decisions": [],
                "financial_facts": None,
                "market_facts": None,
                "llm_review": False,
            },
            {},
        )
    )

    decisions = result.get("decisions", [])
    assert decisions
    for decision in decisions:
        # 每个维度审查决策都追溯到 thesis + signal 两个上游 artifact。
        assert set(decision.based_on) == {"a-thesis", "a-signal"}


def test_compute_report_metrics_grounds_decisions_with_based_on():
    report_artifact = Artifact(
        id="artifact-report",
        type="report",
        producer_step="generate_report",
        value=_complete_report(),
    )
    state = {
        "run": _base_run(),
        "steps": [],
        "artifacts": [report_artifact],
        "observations": [],
        "issues": [],
        "decisions": [
            Decision(
                id="d1",
                maker="tester",
                rationale="结论基于报告。",
                confidence=0.8,
                based_on=["artifact-report"],
            )
        ],
        "final_artifact_id": "artifact-report",
    }

    metrics = compute_report_metrics(state)

    assert metrics.evidence_coverage == 1.0
    assert metrics.grounding_score > 0


def test_build_evidence_map_nonempty_when_decisions_have_based_on():
    report_artifact = Artifact(
        id="artifact-report",
        type="report",
        producer_step="generate_report",
        value=_complete_report(),
    )
    state = {
        "artifacts": [report_artifact],
        "observations": [],
        "decisions": [
            Decision(
                id="d1",
                maker="tester",
                rationale="结论基于报告。",
                confidence=0.8,
                based_on=["artifact-report"],
            )
        ],
    }

    evidence_map = build_evidence_map(state)

    assert len(evidence_map) == 1
    assert evidence_map[0]["decision_id"] == "d1"


def test_review_report_emits_evidence_chain_warning_when_decisions_unreferenced():
    report_artifact = Artifact(
        id="artifact-report",
        type="report",
        producer_step="generate_report",
        value=_complete_report(),
    )
    state = {
        "run": _base_run(),
        "steps": [],
        "artifacts": [report_artifact],
        "observations": [],
        "issues": [],
        "decisions": [
            Decision(
                id="d1",
                maker="tester",
                rationale="无证据引用。",
                confidence=0.5,
            )
        ],
        "final_artifact_id": "artifact-report",
        "report_review_round": 0,
        "max_report_review_rounds": 2,
        "llm_review": False,
    }

    result = asyncio.run(review_report(state, {}))

    warnings = [issue for issue in result["issues"] if issue.category == "evidence_chain_incomplete"]
    assert len(warnings) == 1
    assert warnings[0].scope == IssueScope.DATA
