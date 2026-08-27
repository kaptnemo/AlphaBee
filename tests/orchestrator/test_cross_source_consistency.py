"""cross_source_consistency 只统计“未结算/未消解”的不一致。"""

from alphabee.core import Artifact, Issue, IssueSeverity, Run, RunStatus
from alphabee.orchestrator.gates import (
    SETTLED_CONFLICT,
    UNRESOLVED_INCONSISTENCY,
    compute_report_metrics,
)


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


def _state_with_issue_categories(categories: list[str]) -> dict:
    report = Artifact(
        id="artifact-report",
        type="report",
        producer_step="generate_report",
        value=_complete_report(),
    )
    issues = [
        Issue(
            id=f"issue-{i}",
            severity=IssueSeverity.MEDIUM,
            category=category,
            message=f"issue {category}",
            related_step="review_thesis",
        )
        for i, category in enumerate(categories)
    ]
    return {
        "run": _base_run(),
        "steps": [],
        "artifacts": [report],
        "observations": [],
        "issues": issues,
        "decisions": [],
        "final_artifact_id": "artifact-report",
    }


def test_unresolved_and_settled_sets_are_disjoint():
    # 已结算冲突绝不能被当成“未结算不一致”统计，否则验证流程正常工作也会触发重写。
    assert "verified_conflict" in SETTLED_CONFLICT
    assert "thesis_conflict" in SETTLED_CONFLICT
    assert "verified_conflict" not in UNRESOLVED_INCONSISTENCY
    assert "thesis_conflict" not in UNRESOLVED_INCONSISTENCY
    assert "cross_source_conflict" in UNRESOLVED_INCONSISTENCY
    assert SETTLED_CONFLICT.isdisjoint(UNRESOLVED_INCONSISTENCY)


def test_cross_source_consistency_true_for_settled_conflicts_only():
    # 只有 verified_conflict + thesis_conflict（已结算），不算跨来源不一致。
    state = _state_with_issue_categories(["verified_conflict", "thesis_conflict"])
    metrics = compute_report_metrics(state)
    assert metrics.cross_source_consistency is True


def test_cross_source_consistency_false_for_cross_source_conflict():
    state = _state_with_issue_categories(["cross_source_conflict"])
    assert compute_report_metrics(state).cross_source_consistency is False


def test_cross_source_consistency_false_for_time_mismatch():
    state = _state_with_issue_categories(["time_mismatch"])
    assert compute_report_metrics(state).cross_source_consistency is False


def test_cross_source_consistency_false_for_numeric_inconsistency():
    state = _state_with_issue_categories(["numeric_inconsistency"])
    assert compute_report_metrics(state).cross_source_consistency is False


def test_cross_source_consistency_false_when_unresolved_mixed_with_settled():
    # 已结算 + 未结算并存时，只要存在未结算不一致就应判 False。
    state = _state_with_issue_categories(["verified_conflict", "cross_source_conflict"])
    assert compute_report_metrics(state).cross_source_consistency is False


def _state_with_settled_conflict(disclosed_ids: list[str]) -> dict:
    report = Artifact(
        id="artifact-report",
        type="report",
        producer_step="generate_report",
        value={**_complete_report(), "disclosed_issue_ids": disclosed_ids},
    )
    # 用 low 严重度：它不会进入 _issue_disclosure_status 的 required_ids，
    # 因此下面的断言专门验证 SETTLED_CONFLICT 的独立披露兜底（而非"所有 high 都要披露"）。
    issues = [
        Issue(
            id="issue-verified",
            severity=IssueSeverity.LOW,
            category="verified_conflict",
            message="已结算冲突。",
            related_step="verify_hypotheses",
        )
    ]
    return {
        "run": _base_run(),
        "steps": [],
        "artifacts": [report],
        "observations": [],
        "issues": issues,
        "decisions": [],
        "final_artifact_id": "artifact-report",
    }


def test_settled_conflict_not_disclosed_makes_issue_handling_false():
    # SETTLED_CONFLICT 是 load-bearing 的：已结算冲突不再影响 cross_source_consistency，
    # 但若未进 disclosed_issue_ids，issue_handling 必须为 False。
    metrics = compute_report_metrics(_state_with_settled_conflict(disclosed_ids=[]))
    assert metrics.cross_source_consistency is True
    assert metrics.issue_handling is False


def test_settled_conflict_disclosed_keeps_issue_handling_true():
    metrics = compute_report_metrics(_state_with_settled_conflict(disclosed_ids=["issue-verified"]))
    assert metrics.cross_source_consistency is True
    assert metrics.issue_handling is True
