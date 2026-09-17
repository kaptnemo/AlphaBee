import asyncio

import pytest

from alphabee.core import Artifact, Decision, DeviationClass, Issue, IssueSeverity, Run, RunStatus
from alphabee.orchestrator import gates
from alphabee.orchestrator.gates import review_report, route_after_report_review
from alphabee.orchestrator.recovery import BUDGET_EXHAUSTED_ISSUE_CATEGORY, RERUN_BUDGET_CHECK_CATEGORY


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
            "disclosed_issue_ids": [],
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


def test_review_report_requires_explicit_issue_id_disclosure():
    report_artifact = Artifact(
        id="artifact-report",
        type="report",
        producer_step="generate_report",
        value={
            "title": "600519.SH 财报质量体检报告 — 2024Q4",
            "sections": {
                "executive_summary": "提示了注意风险，但没指向具体问题。",
                "investment_viewpoint": "观点",
                "scenario_analysis": "情景",
                "key_metrics": "表格",
                "signal_analysis": "信号",
                "anomaly_detection": "异常",
                "conflict_analysis": "存在冲突，注意风险。",
                "dimension_analysis": "维度",
                "review_findings": "审查",
                "falsification_conditions": "证伪",
                "risks": "本分析不构成投资建议，注意风险。",
                "disclaimer": "免责声明",
            },
            "summary": "总体判断",
            "risk_count": {"high": 1},
            "overall_confidence": "medium",
            "disclosed_issue_ids": [],
        },
    )
    state = {
        "run": _base_run(),
        "steps": [],
        "artifacts": [report_artifact],
        "observations": [],
        "issues": [
            Issue(
                id="issue-high-1",
                severity=IssueSeverity.HIGH,
                category="verified_conflict",
                message="已验证冲突未在报告中逐条披露。",
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
    assert "issue-high-1:verified_conflict" in result["report_rewrite_reason"]


# ── T25：report gate 回环裁决的永久回归保护（F2-2 / F2-3） ──────────────────


def _blocking_report_artifact() -> Artifact:
    """一份**必然过不了 gate** 的报告（缺章节 → deterministic assessment 产 blocking_issues）。"""
    return Artifact(
        id="artifact-report",
        type="report",
        producer_step="generate_report",
        value={
            "title": "t",
            "sections": {"executive_summary": "总结", "risks": ""},
            "summary": "总体判断",
            "risk_count": {"high": 1},
            "overall_confidence": "high",
            "disclosed_issue_ids": [],
        },
    )


def _blocking_state(*, round_no: int, max_rounds: int) -> dict:
    return {
        "run": _base_run(),
        "steps": [],
        "artifacts": [_blocking_report_artifact()],
        "observations": [],
        "issues": [],
        "decisions": [],
        "final_artifact_id": "artifact-report",
        "report_review_round": round_no,
        "max_report_review_rounds": max_rounds,
        "llm_review": False,
    }


def _budget_issues(result: dict) -> list[Issue]:
    return [issue for issue in result["issues"] if issue.category == BUDGET_EXHAUSTED_ISSUE_CATEGORY]


def test_gate_loops_back_with_budget_and_emits_no_budget_deviation():
    """F2-3 侧 A：预算未耗尽 ⇒ **路由确实回到 generate_report**，且不落 D5 预算偏离。

    口径：``report_review_round=0``（= 本 run 尚未回环过）时的这一轮，旧语义允许回环。
    """
    state = _blocking_state(round_no=0, max_rounds=2)

    result = asyncio.run(review_report(state, {}))

    assert result["report_rewrite_needed"] is True
    assert result["report_review_round"] == 1
    assert route_after_report_review({**state, **result}) == "generate_report"
    assert _budget_issues(result) == []
    assert result["run"].status is RunStatus.RUNNING


@pytest.mark.parametrize(("round_no", "label"), [(1, "边界 k=max-1"), (2, "越界 k=max")])
def test_gate_stops_and_emits_machine_readable_budget_deviation_when_exhausted(round_no: int, label: str):
    """F2-3 侧 B + F2-2：预算耗尽 ⇒ **不回环**，且落一条**机读** D5 预算偏离。

    ★★ 触发口径（t55 更正，**勿改回 `round==max` 单一参数**）：判据是
    ``written = k + 1``（gate 自增并写回后的总轮次）与上限比较 —— ``written < limit`` 才回环。
    于是 max=2 时：
    ```
    k=0 → written=1 < 2  ⇒ 回环（见 test_gate_loops_back_with_budget_and_emits_no_budget_deviation）
    k=1 → written=2 = 2  ⇒ **边界：首格不回环 + D5 + PARTIAL**（t41 的 F2-4 正是这一格暴露的）
    k=2 → written=3 > 2  ⇒ 也收口，但**非边界**（本参数化里的越界补充）
    ```
    ⇒ **边界是 `k = max-1`**，故本用例**两格都验**；早期版本只测 `k=max` 并声称"以 round=max
    为准"，那会**漏掉边界**（正是 F2-4 得以存在的缝隙）。
    """
    state = _blocking_state(round_no=round_no, max_rounds=2)

    result = asyncio.run(review_report(state, {}))

    assert result["report_rewrite_needed"] is True
    assert route_after_report_review({**state, **result}) == "finalize_message"

    budget_issues = _budget_issues(result)
    assert len(budget_issues) == 1, f"预算耗尽必须且只落一条 D5 预算偏离（{label}）"
    issue = budget_issues[0]
    assert issue.deviation_class is DeviationClass.D5_CONTROL
    assert issue.severity is IssueSeverity.HIGH
    assert issue.recovery_action == "escalated" and issue.recovery_cost == 5
    assert issue.detected_at_step == "review_report"
    assert issue.related_artifact == "artifact-report"
    assert result["run"].status is RunStatus.PARTIAL


def test_gate_and_route_agree_on_same_run_regression():
    """F2-3 同源回归：gate 落库与 route 最终路由**逐格一致**（t40 缺陷的永久护栏）。

    三个不变量（``max_rounds >= 1``，即真实流水线取值）：
    1. route ≡ 旧隐式判据（写回后的 ``round < max``）；
    2. 「拒绝回环」⟺「落且仅落一条 D5」（不出现"不收口也无记录"或"回环却报预算耗尽"）；
    3. RunStatus：回环 ⇒ RUNNING；收口 ⇒ PARTIAL。
    """
    for round_no in (0, 1, 2, 3):
        for max_rounds in (1, 2, 3):
            state = _blocking_state(round_no=round_no, max_rounds=max_rounds)
            result = asyncio.run(review_report(state, {}))
            merged = {**state, **result}  # 模拟 LangGraph 把 partial update 归并回 state
            loop = route_after_report_review(merged) == "generate_report"

            assert result["report_rewrite_needed"] is True
            assert loop == (result["report_review_round"] < max_rounds), "route 与旧判据不等价"
            assert bool(_budget_issues(result)) == (not loop), "D5 与拒绝回环必须同真同假"
            assert result["run"].status is (RunStatus.RUNNING if loop else RunStatus.PARTIAL)


def test_gate_never_emits_the_rerun_budget_probe_sentinel():
    """★ 哨兵不落库：``rerun_budget_check`` 只是路由触发值，**不得**出现在任何产出 Issue 里。

    ``_report_rerun_decision`` 为让 ``choose_recovery`` 落到"可重试"分支而构造一条探针 Issue；
    它只在裁决器内部短暂存在。若日后有人把它落进 state/账本，F0 的分类守卫与 §11/F5 的
    per-class 画像都会被污染 —— 本用例钉住这一点。
    """
    for round_no, max_rounds, expected_route in (
        (0, 2, "generate_report"),
        (2, 2, "finalize_message"),
        (0, 1, "finalize_message"),  # round=0 写回 1 ⇒ 1 < 1 为假，旧语义即收口
        (1, 1, "finalize_message"),
    ):
        state = _blocking_state(round_no=round_no, max_rounds=max_rounds)

        result = asyncio.run(review_report(state, {}))

        categories = [issue.category for issue in result["issues"]]
        assert RERUN_BUDGET_CHECK_CATEGORY not in categories, f"哨兵落库了：{categories}"
        assert route_after_report_review({**state, **result}) == expected_route


def test_gate_switch_off_is_pure_rollback_to_pre_f2(monkeypatch):
    """★ t55 契约（captain 裁定方案 (B)「纯回滚」）：``deviation.recovery.enabled=False``
    必须**逐格等于 pre-F2 行为**。

    基线谓词为 ``rewrite_needed and used < limit``（``used`` = 自增并写回后的总轮次）。
    两格都验（max=2）：
    ① 回环侧 ``k=0``（``used=1 < 2``）⇒ **回环能力保留**，run 仍 ``RUNNING``，不落 D5；
    ② 边界侧 ``k=max-1=1``（``used=2 >= 2``）⇒ 不回环 + ``RunStatus.PARTIAL``（与 pre-F2 一致），
       且**不新增** D5 ``budget_exhausted``（D5 生产者本身是 F2 新增物；关掉 F2 开关却新增记录，
       就不构成回滚）。

    （对照 t41 的 F2-5 缺陷：那时关闭开关会"所有轮次一律 TIER_5"，连 ① 的回环也一并禁掉；
    对照被否决的备选 (A)：回滚仍落 D5 —— 那会让回滚路径不再等于 pre-F2，已在
    ``_baseline_rerun_decision`` docstring 留档。）
    """
    monkeypatch.setattr(gates, "recovery_switches", lambda: False)

    looping = _blocking_state(round_no=0, max_rounds=2)
    looped = asyncio.run(review_report(looping, {}))
    assert route_after_report_review({**looping, **looped}) == "generate_report", "回环能力必须保留"
    assert looped["run"].status is RunStatus.RUNNING
    assert _budget_issues(looped) == []

    boundary = _blocking_state(round_no=1, max_rounds=2)
    stopped = asyncio.run(review_report(boundary, {}))
    assert route_after_report_review({**boundary, **stopped}) == "finalize_message"
    assert stopped["run"].status is RunStatus.PARTIAL
    assert _budget_issues(stopped) == [], "纯回滚不得新增 D5 记录（pre-F2 没有该记录）"
