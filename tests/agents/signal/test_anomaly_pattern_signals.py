from alphabee.agents.signal.engine import SignalEngine
from alphabee.orchestrator.services.payload_builders import default_anomaly_fact_values


def test_pattern_signal_uses_default_anomaly_facts_without_missing_fact():
    results = SignalEngine().run(
        ["anomaly_pattern_efficiency_gain"],
        default_anomaly_fact_values(),
    )

    assert results["anomaly_pattern_efficiency_gain"]["level"] == "none"


def test_pattern_signal_maps_second_order_anomaly_to_thesis_dimension():
    fact_values = default_anomaly_fact_values()
    fact_values["anomaly_pattern_inflated_revenue"] = 1.0

    results = SignalEngine().run(
        ["anomaly_pattern_inflated_revenue"],
        fact_values,
    )

    assert results["anomaly_pattern_inflated_revenue"]["level"] == "high"
    assert results["anomaly_pattern_inflated_revenue"]["thesis_impact"] == {
        "earnings_quality": "negative",
    }
    assert "收入未被真实回款支撑" in results["anomaly_pattern_inflated_revenue"]["interpretation"]
