from alphabee.core import Run, RunStatus
from alphabee.orchestrator.nodes import analyze as analyze_node


class _FakeFinancialFacts:
    stock_code = "600519.SH"
    snapshots = [object(), object()]


class _FakeAnomalyReport:
    def to_fact_values(self):
        return {
            "anomaly_triggered_count": 2.0,
            "anomaly_pattern_count": 1.0,
            "anomaly_max_zscore": 3.4,
            "anomaly_high_count": 1.0,
        }

    def to_dict(self):
        return {
            "symbol": "600519.SH",
            "period": "2024Q4",
            "anomaly_count": 2,
            "pattern_count": 1,
            "anomalies": [],
            "pattern_matches": [],
        }


class _FakeAnomalyEngine:
    def run(self, financial_facts, extra_values=None):
        return _FakeAnomalyReport()


class _FakeDerivedFactsEngine:
    def run(self, rule_names, fact_values):
        return {}


def test_run_analysis_engines_injects_anomaly_facts_before_signal(monkeypatch):
    import alphabee.agents.anomaly.engine as anomaly_engine_module

    captured_signal_facts = {}

    class FakeSignalEngine:
        def run(self, rule_names, fact_values):
            captured_signal_facts.update(fact_values)
            return {
                "cross_validation_break": {
                    "level": "high",
                    "interpretation": "z-score high",
                    "thesis_impact": {"financial_quality": "negative"},
                }
            }

    monkeypatch.setattr(analyze_node, "load_rules", lambda: None)
    monkeypatch.setattr(analyze_node, "RULES", {})
    monkeypatch.setattr(analyze_node, "DerivedFactsEngine", lambda: _FakeDerivedFactsEngine())
    monkeypatch.setattr(analyze_node, "load_signal_rules", lambda: None)
    monkeypatch.setattr(analyze_node, "SIGNAL_RULES", {"cross_validation_break": object()})
    monkeypatch.setattr(analyze_node, "SignalEngine", lambda: FakeSignalEngine())
    monkeypatch.setattr(analyze_node, "record_signal_data_gaps", lambda *args, **kwargs: None)
    monkeypatch.setattr(analyze_node, "get_company_profile", lambda symbol: {})
    monkeypatch.setattr(anomaly_engine_module, "AnomalyEngine", lambda: _FakeAnomalyEngine())

    state = {
        "run": Run(
            id="run-1",
            goal="分析贵州茅台",
            status=RunStatus.RUNNING,
            context={"symbol": "600519.SH", "query": "分析贵州茅台"},
        ),
        "steps": [],
        "artifacts": [],
        "issues": [],
        "fact_values": {"revenue": 1.0},
        "financial_facts": _FakeFinancialFacts(),
    }

    import asyncio

    result = asyncio.run(analyze_node.run_analysis_engines(state, {}))

    assert captured_signal_facts["anomaly_max_zscore"] == 3.4
    assert captured_signal_facts["anomaly_pattern_count"] == 1.0
    assert result["fact_values"]["anomaly_triggered_count"] == 2.0
    assert result["signal_analysis"]["cross_validation_break"]["level"] == "high"
