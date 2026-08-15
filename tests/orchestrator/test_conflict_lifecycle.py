"""Conflict lifecycle tests (ROADMAP 0.5).

生命周期契约：
- explore_conflicts 只产出 provisional（候选）冲突，即使 severity 很高也**不升格为 issue**；
- verify_hypotheses 是结算层：verified / partial 的高严重度冲突升格为
  ``verified_conflict`` issue（供 gate 要求披露），rejected 假设沉淀为 decision，
  provisional / unknown 不产生 issue；
- 结算后状态显式回写进 ``conflicts_result`` artifact（verified/partial/rejected/unknown）；
- review_thesis 不再重复制造 verified_conflict issue / rejected decision，
  只对“正向维度 vs 已验证冲突”制造 thesis_conflict。
"""

import asyncio
import json as _json

from alphabee.agents.schemas import (
    ConflictAnalysisResult,
    ConflictItem,
    HypothesisItem,
    VerificationResultItem,
)
from alphabee.agents.thesis.models import CompanyContext, ThesisReview
from alphabee.core import Artifact, ArtifactType, IssueSeverity, Run, RunStatus
from alphabee.orchestrator import agent as agent_module
from alphabee.orchestrator.contracts import find_artifact_model
from alphabee.orchestrator.nodes import conflicts as conflicts_node
from alphabee.orchestrator.nodes import verification as verification_node


def _base_run():
    return Run(
        id="run-1",
        goal="分析贵州茅台",
        status=RunStatus.RUNNING,
        context={"symbol": "600519.SH", "query": "分析贵州茅台"},
    )


def _conflict_result(severity: str = "high") -> ConflictAnalysisResult:
    """Build a ConflictAnalysisResult with one high/critical conflict and one hypothesis."""
    return ConflictAnalysisResult(
        conflicts=[
            ConflictItem(
                id="c1",
                theme="盈利增长但现金流恶化",
                description="利润增长没有被现金流验证。",
                related_dimensions=["earnings_quality"],
                severity=severity,  # type: ignore[arg-type]
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


def _state_with_conflicts(conflicts_result: ConflictAnalysisResult) -> dict:
    return {
        "run": _base_run(),
        "steps": [],
        "artifacts": [
            Artifact(
                id="artifact-conflicts",
                type=ArtifactType.CONFLICTS_RESULT,
                producer_step="explore_conflicts",
                value=conflicts_result.model_dump(mode="json"),
            )
        ],
        "issues": [],
        "decisions": [],
    }


def _patch_verify(monkeypatch, status: str):
    async def fake_verify_single_conflict(conflict, shared_context, step_id, config):
        return (
            [
                VerificationResultItem(
                    id="v1",
                    hypothesis_id="h1",
                    status=status,  # type: ignore[arg-type]
                    support_score=0.9 if status != "rejected" else 0.2,
                    contradiction_score=0.1 if status != "rejected" else 0.8,
                    confidence=0.8,
                    gaps=["缺少同行数据"] if status != "rejected" else [],
                    summary=("现金流未能验证利润增长。" if status != "rejected" else "反证充分，假设被推翻。"),
                )
            ],
            [],
        )

    monkeypatch.setattr(verification_node, "_verify_single_conflict", fake_verify_single_conflict)
    monkeypatch.setattr(verification_node, "build_verify_context", lambda state, symbol: {})


# ── 探索层：provisional 不升格 ────────────────────────────────────────────


def test_explore_conflicts_does_not_escalate_provisional_conflicts(monkeypatch):
    class FakeAgent:
        async def ainvoke(self, payload, config=None):
            raw = _conflict_result(severity="critical").model_dump(mode="json")
            return {"messages": [type("Msg", (), {"content": _json.dumps(raw, ensure_ascii=False)})()]}

    monkeypatch.setattr(conflicts_node, "generate_explore_conflicts_prompt", lambda state, query, symbol: "prompt")
    monkeypatch.setattr(
        __import__("alphabee.agents.explore_conflicts.agent", fromlist=["explore_conflicts_agent_factory"]),
        "explore_conflicts_agent_factory",
        lambda: FakeAgent(),
    )

    result = asyncio.run(
        conflicts_node.explore_conflicts(
            {"run": _base_run(), "steps": [], "artifacts": [], "issues": []},
            {},
        )
    )

    # 探索阶段即使 critical 冲突也不升格为 issue（不再有 category="conflict" 的 issue）
    assert result["issues"] == []
    conflicts = find_artifact_model(result["artifacts"], ArtifactType.CONFLICTS_RESULT, ConflictAnalysisResult)
    assert conflicts is not None
    assert conflicts.conflicts[0].severity == "critical"
    assert conflicts.conflicts[0].hypotheses[0].status == "pending"


# ── 结算层：verified 升格 / unknown 不升格 / rejected 沉淀 decision ──────


def test_verification_promotes_settled_high_severity_conflicts_to_issues(monkeypatch):
    _patch_verify(monkeypatch, status="verified")

    result = asyncio.run(
        verification_node.verify_hypotheses(_state_with_conflicts(_conflict_result(severity="high")), {})
    )

    verified_issues = [i for i in result["issues"] if i.category == "verified_conflict"]
    assert len(verified_issues) == 1
    assert verified_issues[0].severity == IssueSeverity.HIGH
    assert "盈利增长但现金流恶化" in verified_issues[0].message
    # 不再有探索层直接升格的 category="conflict" issue
    assert not [i for i in result["issues"] if i.category == "conflict"]


def test_verification_keeps_unknown_hypotheses_out_of_issues(monkeypatch):
    _patch_verify(monkeypatch, status="unknown")

    result = asyncio.run(
        verification_node.verify_hypotheses(_state_with_conflicts(_conflict_result(severity="high")), {})
    )

    # unknown = 证据未闭环，仍保持 provisional，不升格为 issue
    assert not [i for i in result["issues"] if i.category == "verified_conflict"]


def test_verification_records_rejected_hypotheses_as_decisions(monkeypatch):
    _patch_verify(monkeypatch, status="rejected")

    result = asyncio.run(
        verification_node.verify_hypotheses(_state_with_conflicts(_conflict_result(severity="high")), {})
    )

    assert not [i for i in result["issues"] if i.category == "verified_conflict"]
    decisions = result.get("decisions", [])
    assert len(decisions) == 1
    assert decisions[0].maker == "conflict_verifier"
    assert "已排除" in decisions[0].rationale


def test_verification_writes_back_settled_status_into_conflicts_artifact(monkeypatch):
    _patch_verify(monkeypatch, status="verified")

    result = asyncio.run(
        verification_node.verify_hypotheses(_state_with_conflicts(_conflict_result(severity="high")), {})
    )

    settled = find_artifact_model(result["artifacts"], ArtifactType.CONFLICTS_RESULT, ConflictAnalysisResult)
    assert settled is not None
    assert settled.conflicts[0].hypotheses[0].status == "verified"


# ── 回归：hypothesis.status 必须接受 unknown（ROADMAP 0.5 结算态）────────────
# 曾因 HypothesisItem.status 的 Literal 漏掉 "unknown"，验证节点把 unknown
# 回写进 conflicts_result 后，generate_report 重新校验时抛 ValidationError，
# 整条 query 崩溃。这里锁住两条边界：schema 解析 + 节点回写后的 artifact 重校验。


def test_hypothesis_status_schema_accepts_unknown():
    raw = _conflict_result(severity="high").model_dump(mode="json")
    raw["conflicts"][0]["hypotheses"][0]["status"] = "unknown"

    parsed = ConflictAnalysisResult.model_validate(raw)

    assert parsed.conflicts[0].hypotheses[0].status == "unknown"


def test_verification_unknown_writeback_keeps_conflicts_artifact_valid(monkeypatch):
    _patch_verify(monkeypatch, status="unknown")

    result = asyncio.run(
        verification_node.verify_hypotheses(_state_with_conflicts(_conflict_result(severity="high")), {})
    )

    # unknown 验证结果回写后，conflicts_result artifact 必须仍能通过
    # ConflictAnalysisResult 重新校验（下游 generate_report 正是这样读取的）。
    settled = find_artifact_model(result["artifacts"], ArtifactType.CONFLICTS_RESULT, ConflictAnalysisResult)
    assert settled is not None
    assert settled.conflicts[0].hypotheses[0].status == "unknown"


# ── 审查层：review_thesis 只负责论点矛盾 ──────────────────────────────────


def test_review_thesis_emits_thesis_conflict_but_not_generic_verified_conflict(monkeypatch):
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

    conflicts_result = _conflict_result(severity="high")
    conflicts_result.conflicts[0].hypotheses[0].status = "verified"

    artifacts = [
        Artifact(
            id="a1",
            type=ArtifactType.FACT_COLLECTION,
            producer_step="collect_raw_facts",
            value={"agent": "FactCollector", "query": "q", "symbol": "600519.SH", "raw_response": ""},
        ),
        Artifact(
            id="a2",
            type=ArtifactType.THESIS_ANALYSIS,
            producer_step="run_thesis",
            value={"thesis": thesis_dict},
        ),
        Artifact(
            id="a3",
            type=ArtifactType.CONFLICTS_RESULT,
            producer_step="explore_conflicts",
            value=conflicts_result.model_dump(mode="json"),
        ),
    ]

    class FakeReviewer:
        def review(self, thesis, signal_results, company_context, use_llm=False):
            return ThesisReview(
                symbol=thesis.symbol,
                period=thesis.period,
                dimension_verdicts={},
                overall_status="qualified_pass",
                blocking_issues=[],
                warning_issues=[],
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

    categories = {issue.category for issue in result.get("issues", [])}
    # 已验证冲突 vs 正向维度 → thesis_conflict
    assert "thesis_conflict" in categories
    # 结算层的 verified_conflict 由 verify_hypotheses 沉淀，review_thesis 不再重复制造
    assert "verified_conflict" not in categories
