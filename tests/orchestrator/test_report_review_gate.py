import asyncio

from alphabee.core import Artifact, Decision, Issue, IssueSeverity, Run, RunStatus
from alphabee.orchestrator.gates import review_report, route_after_report_review


def _base_run():
    return Run(
        id="run-1",
        goal="分析贵州茅台",
        status=RunStatus.RUNNING,
        context={"symbol": "600519.SH", "query": "分析贵州茅台"},
    )


def _complete_report():
    return {
        "title": "600519.SH 财报质量体检报告 — 2024Q4",
        "sections": {
            "executive_summary": "总结",
            "key_metrics": "表格",
            "signal_analysis": "信号",
            "anomaly_detection": "异常",
            "conflict_analysis": "冲突",
            "investment_thesis": "论点",
            "review_findings": "审查",
            "risks": "风险",
            "disclaimer": "免责声明",
        },
        "summary": "总体判断",
        "risk_count": {"high": 0, "medium": 1, "low": 1, "blocked": 0},
        "overall_confidence": "medium",
    }


def test_review_report_passes_complete_report():
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
                id="decision-1",
                maker="tester",
                rationale="风险已在报告中披露。",
                confidence=0.8,
                based_on=["artifact-report"],
            )
        ],
        "final_artifact_id": "artifact-report",
        "report_review_round": 0,
        "max_report_review_rounds": 2,
        "llm_review": False,
    }

    result = asyncio.run(review_report(state, {}))

    assert result["report_rewrite_needed"] is False
    assert result["report_review_round"] == 1
    assert result["run"].status == RunStatus.SUCCEEDED
    assert result["evaluation_artifact_id"] is not None
    assert result["artifacts"][-1].type == "evaluation_report"


def test_review_report_requests_rewrite_and_routes_back():
    report_artifact = Artifact(
        id="artifact-report",
        type="report",
        producer_step="generate_report",
        value={
            "title": "600519.SH 财报质量体检报告 — 2024Q4",
            "sections": {
                "executive_summary": "总结",
                "risks": "",
            },
            "summary": "总体判断",
            "risk_count": {"high": 1},
            "overall_confidence": "high",
        },
    )
    state = {
        "run": _base_run(),
        "steps": [],
        "artifacts": [report_artifact],
        "observations": [],
        "issues": [
            Issue(
                id="issue-1",
                severity=IssueSeverity.HIGH,
                category="thesis_conflict",
                message="正向论点与已验证冲突矛盾。",
                related_step="review_thesis",
            )
        ],
        "decisions": [],
        "final_artifact_id": "artifact-report",
        "report_review_round": 0,
        "max_report_review_rounds": 2,
        "llm_review": False,
    }

    result = asyncio.run(review_report(state, {}))

    assert result["report_rewrite_needed"] is True
    assert "正向论点与已验证冲突矛盾" in result["report_rewrite_reason"]
    assert route_after_report_review(result) == "generate_report"
    assert result["run"].status == RunStatus.RUNNING
