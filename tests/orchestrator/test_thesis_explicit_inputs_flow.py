import asyncio

from alphabee.agents.thesis.models import CompanyContext, InvestmentThesis
from alphabee.core import Artifact, Run, RunStatus
from alphabee.orchestrator.nodes import thesis as thesis_node


class _FakeFinancialFacts:
    snapshots = [type("Snapshot", (), {"period": "2024Q4"})()]


def test_run_thesis_passes_anomaly_conflict_verification_and_context(monkeypatch):
    captured: dict = {}

    class FakeThesisEngine:
        def run(self, **kwargs):
            captured.update(kwargs)
            return InvestmentThesis(
                symbol=kwargs["symbol"],
                period=kwargs["period"],
                dimensions={},
            )

    monkeypatch.setattr(thesis_node, "ThesisEngine", lambda: FakeThesisEngine())
    monkeypatch.setattr(
        thesis_node,
        "build_company_context",
        lambda **kwargs: CompanyContext(
            symbol=kwargs.get("symbol") or "",
            industry="军工",
            lifecycle_stage="growth",
        ),
    )

    state = {
        "run": Run(
            id="run-1",
            goal="分析公司",
            status=RunStatus.RUNNING,
            context={"symbol": "600519.SH", "query": "分析公司"},
        ),
        "steps": [],
        "artifacts": [
            Artifact(
                id="a1",
                type="signal_analysis",
                producer_step="run_analysis_engines",
                value={
                    "results": {
                        "cross_validation_break": {
                            "level": "high",
                            "interpretation": "勾稽关系异常",
                            "thesis_impact": {"financial_quality": "negative"},
                        }
                    }
                },
            ),
            Artifact(
                id="a2",
                type="fact_collection",
                producer_step="collect_raw_facts",
                value={"raw_response": "军工企业，项目制验收。"},
            ),
            Artifact(
                id="a3",
                type="anomaly_report",
                producer_step="run_analysis_engines",
                value={
                    "pattern_matches": [
                        {
                            "pattern_id": "inflated_revenue",
                            "pattern_name": "虚增收入嫌疑",
                            "severity": "high",
                            "risk_dimension": "earnings_quality",
                            "explanation": "收入未被真实回款支撑。",
                        }
                    ]
                },
            ),
        ],
        "issues": [],
        "financial_facts": _FakeFinancialFacts(),
        "market_facts": None,
        "conflicts_result": {
            "conflicts": [
                {
                    "id": "c1",
                    "theme": "盈利增长但现金流恶化",
                    "description": "利润增长未被现金流验证。",
                    "related_dimensions": ["earnings_quality", "financial_quality"],
                    "severity": "high",
                    "hypotheses": [{"id": "h1", "explanation": "收入质量不足"}],
                }
            ]
        },
        "verification_results": [
            {"hypothesis_id": "h1", "status": "verified", "summary": "现金流未验证利润增长。"}
        ],
    }

    asyncio.run(thesis_node.run_thesis(state, {}))

    assert captured["anomaly_report"]["pattern_matches"]
    assert captured["conflict_analysis"]["conflicts"][0]["theme"] == "盈利增长但现金流恶化"
    assert captured["verification_results"][0]["status"] == "verified"
    assert captured["company_context"].industry == "军工"
