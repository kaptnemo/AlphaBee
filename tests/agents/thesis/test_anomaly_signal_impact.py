from alphabee.agents.thesis.engine import ThesisEngine


def test_anomaly_signal_contributes_to_thesis_dimensions():
    thesis = ThesisEngine().run(
        symbol="600519.SH",
        period="2024Q4",
        signal_results={
            "cross_validation_break": {
                "level": "high",
                "interpretation": "勾稽关系严重断裂。",
                "thesis_impact": {
                    "financial_quality": "negative",
                    "earnings_quality": "negative",
                    "operational_stability": "negative",
                },
            }
        },
    )

    assert thesis.dimensions["financial_quality"].judgment == "strong_negative"
    assert thesis.dimensions["earnings_quality"].judgment == "strong_negative"
    assert thesis.dimensions["operational_stability"].judgment == "strong_negative"
    assert thesis.primary_risks
