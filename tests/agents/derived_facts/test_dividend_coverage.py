import pytest

from alphabee.agents.derived_facts.engine import Engine
from alphabee.agents.derived_facts.registry import RULES, load_rules
from alphabee.agents.derived_facts.tools import evaluate_derived_facts


@pytest.fixture(autouse=True)
def ensure_rules_loaded():
    load_rules()


def test_dividend_coverage_zero_division_is_not_applicable():
    rule = RULES["dividend_coverage"]

    result = rule.compute(
        {"operating_cashflow": 1200.0, "dividends_paid": 0.0},
        interpretation=True,
    )

    assert result["dividend_coverage"] is None
    assert result["level"] == "not_applicable"
    assert result["error"] == "no_dividend_paid"
    assert "不适用" in result["interpretation"]


def test_engine_keeps_not_applicable_out_of_failed_chain():
    engine = Engine()

    results = engine.run(
        ["dividend_coverage"],
        {"operating_cashflow": 1200.0, "dividends_paid": 0.0},
    )

    assert results["dividend_coverage"]["level"] == "not_applicable"


def test_tool_formats_not_applicable_dividend_coverage():
    output = evaluate_derived_facts(
        ["dividend_coverage"],
        {"operating_cashflow": 1200.0, "dividends_paid": 0.0},
    )

    assert "dividend_coverage" in output
    assert "不适用" in output
    assert "no_dividend_paid" in output or "本期未发生现金分红" in output
