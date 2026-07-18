import asyncio

from alphabee.agents.schemas import ConflictAnalysisResult, VerificationResultItem
from alphabee.core import Artifact, IssueSeverity, Run, RunStatus
from alphabee.orchestrator.contracts import (
    AnomalyReportArtifact,
    ConflictAnalysisArtifact,
    DerivedFactsArtifact,
    FactCollectionArtifact,
    ReportArtifact,
    ReportGenerationPayload,
    SignalAnalysisArtifact,
    ThesisArtifact,
    VerificationArtifact,
    find_artifact_model,
)
from alphabee.orchestrator.nodes import analyze as analyze_node
from alphabee.orchestrator.nodes import conflicts as conflicts_node
from alphabee.orchestrator.nodes import verification as verification_node
from alphabee.orchestrator.services.payload_builders import (
    build_report_generation_payload,
)
from alphabee.task_records.recorder import TaskRecorder


class _FakeFinancialFacts:
    stock_code = "600519.SH"
    snapshots = [object(), object()]


class _FakeAnomalyReport:
    def to_fact_values(self):
        return {
            "anomaly_triggered_count": 1.0,
            "anomaly_pattern_count": 1.0,
            "anomaly_max_zscore": 2.8,
            "anomaly_high_count": 1.0,
            "anomaly_pattern_inflated_revenue": 1.0,
        }

    def to_dict(self):
        return {
            "symbol": "600519.SH",
            "period": "2024Q4",
            "anomaly_count": 1,
            "pattern_count": 1,
            "anomalies": [
                {"rule_id": "receivable_gap", "level": "high", "z_score": 2.8}
            ],
            "pattern_matches": [
                {
                    "pattern_id": "inflated_revenue",
                    "pattern_name": "虚增收入嫌疑",
                    "severity": "high",
                    "triggering_rules": ["receivable_gap"],
                }
            ],
        }


class _FakeAnomalyEngine:
    def run(self, financial_facts, extra_values=None):
        return _FakeAnomalyReport()


class _FakeDerivedFactsEngine:
    def run(self, rule_names, fact_values):
        return {
            "roe_quality": {
                "roe_quality": 0.82,
                "level": "medium",
                "interpretation": "ROE 质量尚可",
            }
        }


def _base_run():
    return Run(
        id="run-1",
        goal="分析贵州茅台",
        status=RunStatus.RUNNING,
        context={"symbol": "600519.SH", "query": "分析贵州茅台"},
    )


def test_run_analysis_engines_emits_typed_artifacts(monkeypatch):
    import alphabee.agents.anomaly.engine as anomaly_engine_module

    class FakeSignalEngine:
        def run(self, rule_names, fact_values):
            return {
                "cashflow_warning": {
                    "level": "high",
                    "interpretation": "现金流承压",
                    "thesis_impact": {"financial_quality": "negative"},
                }
            }

    monkeypatch.setattr(analyze_node, "load_rules", lambda: None)
    monkeypatch.setattr(analyze_node, "RULES", {"roe_quality": object()})
    monkeypatch.setattr(analyze_node, "DerivedFactsEngine", lambda: _FakeDerivedFactsEngine())
    monkeypatch.setattr(analyze_node, "load_signal_rules", lambda: None)
    monkeypatch.setattr(analyze_node, "SIGNAL_RULES", {"cashflow_warning": object()})
    monkeypatch.setattr(analyze_node, "SignalEngine", lambda: FakeSignalEngine())
    monkeypatch.setattr(analyze_node, "record_signal_data_gaps", lambda *args, **kwargs: None)
    monkeypatch.setattr(analyze_node, "get_company_profile", lambda symbol: {})
    monkeypatch.setattr(anomaly_engine_module, "AnomalyEngine", lambda: _FakeAnomalyEngine())

    result = asyncio.run(
        analyze_node.run_analysis_engines(
            {
                "run": _base_run(),
                "steps": [],
                "artifacts": [],
                "issues": [],
                "fact_values": {"revenue": 1.0},
                "financial_facts": _FakeFinancialFacts(),
            },
            {},
        )
    )

    assert isinstance(result["derived_facts"], DerivedFactsArtifact)
    assert isinstance(result["signal_analysis"], SignalAnalysisArtifact)
    assert isinstance(result["anomaly_report"], AnomalyReportArtifact)
    assert find_artifact_model(result["artifacts"], "derived_facts", DerivedFactsArtifact)
    assert find_artifact_model(result["artifacts"], "signal_analysis", SignalAnalysisArtifact)
    assert find_artifact_model(result["artifacts"], "anomaly_report", AnomalyReportArtifact)


def test_conflict_and_verification_nodes_emit_typed_contracts(monkeypatch):
    class FakeAgent:
        async def ainvoke(self, payload, config=None):
            return {
                "messages": [
                    type(
                        "Msg",
                        (),
                        {
                            "content": """{
  "conflicts": [
    {
      "id": "c1",
      "theme": "盈利增长但现金流恶化",
      "description": "利润增长没有被现金流验证。",
      "related_dimensions": ["earnings_quality", "financial_quality"],
      "severity": "high",
      "confidence": 0.9,
      "hypotheses": [
        {
          "id": "h1",
          "conflict_id": "c1",
          "explanation": "收入质量不足",
          "predictions": ["经营现金流/净利润持续低于1"],
          "required_evidence": ["financial_facts"],
          "score": 0.8
        }
      ]
    }
  ]
}"""
                        },
                    )()
                ]
            }

    monkeypatch.setattr(
        conflicts_node,
        "generate_explore_conflicts_prompt",
        lambda state, query, symbol: "prompt",
    )
    monkeypatch.setattr(
        __import__("alphabee.agents.explore_conflicts.agent", fromlist=["explore_conflicts_agent_factory"]),
        "explore_conflicts_agent_factory",
        lambda: FakeAgent(),
    )

    conflict_state = asyncio.run(
        conflicts_node.explore_conflicts(
            {
                "run": _base_run(),
                "steps": [],
                "artifacts": [],
                "issues": [],
            },
            {},
        )
    )

    assert isinstance(conflict_state["conflicts_result"], ConflictAnalysisResult)
    assert find_artifact_model(
        conflict_state["artifacts"], "conflict_analysis", ConflictAnalysisArtifact
    )

    async def fake_verify_single_conflict(conflict, shared_context, step_id, config):
        return (
            [
                VerificationResultItem(
                    id="v1",
                    hypothesis_id="h1",
                    status="verified",
                    support_score=0.9,
                    contradiction_score=0.1,
                    confidence=0.8,
                    gaps=[],
                    summary="现金流未能验证利润增长。",
                )
            ],
            [],
        )

    monkeypatch.setattr(verification_node, "_verify_single_conflict", fake_verify_single_conflict)
    monkeypatch.setattr(verification_node, "build_verify_context", lambda state, symbol: {})

    verification_state = asyncio.run(
        verification_node.verify_hypotheses(conflict_state, {})
    )

    assert isinstance(verification_state["verification_results"], VerificationArtifact)
    assert (
        verification_state["verification_results"].results[0].status == "verified"
    )
    assert find_artifact_model(
        verification_state["artifacts"],
        "verification_results",
        VerificationArtifact,
    )


def test_report_generation_payload_is_typed_and_tracks_required_disclosures():
    thesis_artifact = ThesisArtifact(
        thesis={
            "symbol": "600519.SH",
            "period": "2024Q4",
            "overall_judgment": "neutral",
            "dimensions": {},
        }
    )
    state = {
        "artifacts": [
            Artifact(
                id="a1",
                type="fact_collection",
                producer_step="collect_raw_facts",
                value=FactCollectionArtifact(
                    agent="FactCollector",
                    query="分析贵州茅台",
                    symbol="600519.SH",
                    raw_response="公司经营稳健。",
                ).model_dump(mode="json"),
            ),
            Artifact(
                id="a2",
                type="derived_facts",
                producer_step="run_analysis_engines",
                value=DerivedFactsArtifact(
                    results={
                        "roe_quality": {
                            "roe_quality": 0.82,
                            "level": "medium",
                            "interpretation": "ROE 质量尚可",
                        }
                    },
                    rule_count=1,
                ).model_dump(mode="json"),
            ),
            Artifact(
                id="a3",
                type="signal_analysis",
                producer_step="run_analysis_engines",
                value=SignalAnalysisArtifact(
                    results={
                        "cashflow_warning": {
                            "level": "high",
                            "interpretation": "现金流承压",
                            "thesis_impact": {"financial_quality": "negative"},
                        }
                    },
                    rule_count=1,
                ).model_dump(mode="json"),
            ),
            Artifact(
                id="a4",
                type="anomaly_report",
                producer_step="run_analysis_engines",
                value=AnomalyReportArtifact(
                    symbol="600519.SH",
                    period="2024Q4",
                    anomaly_count=1,
                    pattern_count=1,
                    anomalies=[{"rule_id": "receivable_gap", "level": "high"}],
                    pattern_matches=[{"pattern_id": "inflated_revenue"}],
                ).model_dump(mode="json"),
            ),
            Artifact(
                id="a5",
                type="thesis_analysis",
                producer_step="run_thesis",
                value=thesis_artifact.model_dump(mode="json"),
            ),
        ],
        "issues": [
            type(
                "IssueLike",
                (),
                {
                    "id": "issue-1",
                    "severity": IssueSeverity.HIGH,
                    "category": "verified_conflict",
                    "message": "已验证冲突需要披露。",
                },
            )()
        ],
        "conflicts_result": ConflictAnalysisResult.model_validate(
            {
                "conflicts": [
                    {
                        "id": "c1",
                        "theme": "盈利增长但现金流恶化",
                        "description": "利润增长没有被现金流验证。",
                        "related_dimensions": ["earnings_quality"],
                        "severity": "high",
                        "confidence": 0.9,
                        "hypotheses": [
                            {
                                "id": "h1",
                                "conflict_id": "c1",
                                "explanation": "收入质量不足",
                                "predictions": [],
                                "required_evidence": [],
                                "score": 0.8,
                                "status": "verified",
                            }
                        ],
                    }
                ]
            }
        ),
        "verification_results": VerificationArtifact(
            results=[
                VerificationResultItem(
                    id="v1",
                    hypothesis_id="h1",
                    status="verified",
                    support_score=0.9,
                    contradiction_score=0.1,
                    confidence=0.8,
                    gaps=[],
                    summary="现金流未能验证利润增长。",
                )
            ],
            verified_count=1,
            rejected_count=0,
            unknown_count=0,
        ),
    }

    payload = build_report_generation_payload(state)

    assert isinstance(payload, ReportGenerationPayload)
    assert payload.company.symbol == "600519.SH"
    assert payload.required_issue_disclosures[0].id == "issue-1"
    assert payload.conflict_analysis is not None
    assert payload.conflict_analysis.conflicts[0].related_dimensions == [
        "earnings_quality"
    ]


def test_task_recorder_reads_typed_artifact_payloads():
    report = ReportArtifact(
        title="报告",
        sections={
            "executive_summary": "总结",
            "key_metrics": "",
            "signal_analysis": "",
            "anomaly_detection": "",
            "conflict_analysis": "",
            "investment_thesis": "",
            "review_findings": "",
            "risks": "",
            "disclaimer": "",
        },
        summary="总结",
        risk_count={"high": 1},
        overall_confidence="medium",
        disclosed_issue_ids=["issue-1"],
    )
    artifacts = [
        {
            "type": "fact_collection",
            "value": FactCollectionArtifact(
                agent="FactCollector",
                query="分析贵州茅台",
                symbol="600519.SH",
                raw_response="稳健经营",
            ).model_dump(mode="json"),
        },
        {
            "type": "derived_facts",
            "value": DerivedFactsArtifact(rule_count=3).model_dump(mode="json"),
        },
        {
            "type": "signal_analysis",
            "value": SignalAnalysisArtifact(
                rule_count=2,
                results={
                    "cashflow_warning": {
                        "level": "high",
                        "interpretation": "现金流承压",
                    }
                },
            ).model_dump(mode="json"),
        },
        {
            "type": "anomaly_report",
            "value": AnomalyReportArtifact(
                anomaly_count=1,
                pattern_count=1,
                anomalies=[{"rule_id": "receivable_gap", "level": "high"}],
                pattern_matches=[
                    {
                        "pattern_id": "inflated_revenue",
                        "triggering_rules": ["receivable_gap"],
                    }
                ],
            ).model_dump(mode="json"),
        },
        {
            "type": "thesis_analysis",
            "value": ThesisArtifact(
                thesis={
                    "overall_judgment": "neutral",
                    "dimensions": {
                        "earnings_quality": {
                            "name": "盈利质量",
                            "judgment": "negative",
                            "score": -0.4,
                            "confidence": 0.8,
                            "evidence": [{"signal_id": "cashflow_warning"}],
                        }
                    },
                },
                industry_context={"industry": "白酒", "lifecycle_stage": "mature"},
            ).model_dump(mode="json"),
        },
    ]
    payload = {
        "final_report": report.model_dump(mode="json"),
        "issues": [
            {
                "severity": "high",
                "category": "verified_conflict",
                "message": "已验证冲突需要披露。",
                "related_step": "review_thesis",
            }
        ],
    }

    record = TaskRecorder().capture(
        query="分析贵州茅台",
        symbol="600519.SH",
        flags={"enhance": False, "llm_review": False},
        payload=payload,
        artifacts=artifacts,
    )

    assert record.signal_count == 2
    assert record.anomaly_pattern_count == 1
    assert record.company_industry == "白酒"
