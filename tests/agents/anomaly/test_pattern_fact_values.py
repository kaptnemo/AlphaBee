from alphabee.agents.anomaly.models import AnomalyReport, PatternMatch
from alphabee.agents.anomaly.registry import ANOMALY_PATTERNS, ensure_loaded, load_rules
from alphabee.orchestrator.services.payload_builders import default_anomaly_fact_values


def test_default_anomaly_fact_values_include_pattern_flags_even_if_rules_loaded_first():
    load_rules()

    fact_values = default_anomaly_fact_values()

    assert fact_values["anomaly_pattern_cost_pressure"] == 0.0
    assert fact_values["anomaly_pattern_inflated_revenue"] == 0.0


def test_anomaly_report_to_fact_values_marks_pattern_match_flags():
    ensure_loaded()
    report = AnomalyReport(
        symbol="600519.SH",
        period="2024Q4",
        pattern_matches=[
            PatternMatch(pattern=ANOMALY_PATTERNS["cost_pressure"]),
        ],
    )

    fact_values = report.to_fact_values()

    assert fact_values["anomaly_pattern_cost_pressure"] == 1.0
    assert fact_values["anomaly_pattern_inflated_revenue"] == 0.0
