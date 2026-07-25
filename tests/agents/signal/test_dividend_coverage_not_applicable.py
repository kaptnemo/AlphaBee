from alphabee.agents.signal.engine import SignalEngine
from alphabee.agents.signal.registry import load_signal_rules


class _FakeDerivedFactsEngine:
    def __init__(self, results):
        self._results = results

    def run(self, rule_names, fact_values):
        return {name: self._results[name] for name in rule_names}


def test_capital_efficiency_risk_not_blocked_when_no_dividend_paid():
    load_signal_rules()
    engine = SignalEngine()
    engine._df_engine = _FakeDerivedFactsEngine(
        {
            "roe_level": {"roe_level": 0.12, "level": "none"},
            "cashflow_quality": {"cashflow_quality": 0.5, "level": "none"},
            "capex_intensity": {"capex_intensity": 0.25, "level": "medium"},
            "dividend_coverage": {
                "dividend_coverage": None,
                "level": "not_applicable",
                "error": "no_dividend_paid",
            },
        }
    )

    result = engine.run(
        ["capital_efficiency_risk"],
        {
            "net_profit": 100.0,
            "operating_cash_flow": 120.0,
            "dividends_paid": 0.0,
        },
    )["capital_efficiency_risk"]

    assert result["level"] == "none"


def test_capital_efficiency_risk_still_uses_dividend_coverage_when_dividend_exists():
    load_signal_rules()
    engine = SignalEngine()
    engine._df_engine = _FakeDerivedFactsEngine(
        {
            "roe_level": {"roe_level": 0.12, "level": "none"},
            "cashflow_quality": {"cashflow_quality": 0.5, "level": "none"},
            "capex_intensity": {"capex_intensity": 0.25, "level": "medium"},
            "dividend_coverage": {
                "dividend_coverage": 0.8,
                "level": "risky",
            },
        }
    )

    result = engine.run(
        ["capital_efficiency_risk"],
        {
            "net_profit": 100.0,
            "operating_cash_flow": 120.0,
            "dividends_paid": 10.0,
        },
    )["capital_efficiency_risk"]

    assert result["level"] == "low"
