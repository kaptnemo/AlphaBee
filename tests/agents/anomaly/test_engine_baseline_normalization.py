from alphabee.agents.anomaly.engine import AnomalyEngine
from alphabee.agents.anomaly.registry import CROSS_RULES, ensure_loaded
from alphabee.agents.facts.models import FinancialFacts, FinancialSnapshot


def _facts(*snapshots: FinancialSnapshot) -> FinancialFacts:
    return FinancialFacts(stock_code="600519.SH", snapshots=list(snapshots))


def test_extract_field_series_single_quarterizes_cumulative_flow_fields():
    engine = AnomalyEngine()
    facts = _facts(
        FinancialSnapshot(period="20240930", revenue=160.0),
        FinancialSnapshot(period="20240630", revenue=100.0),
        FinancialSnapshot(period="20240331", revenue=10.0),
        FinancialSnapshot(period="20231231", revenue=180.0),
        FinancialSnapshot(period="20230930", revenue=130.0),
        FinancialSnapshot(period="20230630", revenue=60.0),
        FinancialSnapshot(period="20230331", revenue=10.0),
    )

    series = engine._extract_field_series("revenue", facts.snapshots, {})

    assert series == [
        ("20240930", 60.0),
        ("20240630", 90.0),
        ("20240331", 10.0),
        ("20231231", 50.0),
        ("20230930", 70.0),
        ("20230630", 50.0),
        ("20230331", 10.0),
    ]


def test_extract_rule_values_prefers_same_report_period_history():
    ensure_loaded()
    engine = AnomalyEngine()
    facts = _facts(
        FinancialSnapshot(period="20240930", operating_cashflow=130.0, net_profit=50.0),
        FinancialSnapshot(period="20240630", operating_cashflow=70.0, net_profit=45.0),
        FinancialSnapshot(period="20240331", operating_cashflow=20.0, net_profit=10.0),
        FinancialSnapshot(period="20231231", operating_cashflow=150.0, net_profit=60.0),
        FinancialSnapshot(period="20230930", operating_cashflow=90.0, net_profit=40.0),
        FinancialSnapshot(period="20230630", operating_cashflow=40.0, net_profit=36.0),
        FinancialSnapshot(period="20230331", operating_cashflow=10.0, net_profit=8.0),
    )
    rule = CROSS_RULES["cashflow_profit_ratio"]

    current_value, history, baseline_mode, history_periods = engine._extract_rule_values(
        rule=rule,
        snapshots=facts.snapshots,
        extra={},
    )

    assert current_value == 12.0
    assert history[0] == 12.5
    assert baseline_mode == "mixed_periods"
    assert history_periods[0] == "20230930"


def test_extract_field_series_keeps_stock_fields_as_point_in_time_values():
    engine = AnomalyEngine()
    facts = _facts(
        FinancialSnapshot(period="20240930", cash=120.0, interest_bearing_debt=80.0),
        FinancialSnapshot(period="20240630", cash=110.0, interest_bearing_debt=75.0),
        FinancialSnapshot(period="20240331", cash=100.0, interest_bearing_debt=70.0),
    )

    cash_series = engine._extract_field_series("cash", facts.snapshots, {})

    assert cash_series == [
        ("20240930", 120.0),
        ("20240630", 110.0),
        ("20240331", 100.0),
    ]


def test_extract_field_series_keeps_annual_only_cumulative_values_comparable():
    engine = AnomalyEngine()
    facts = _facts(
        FinancialSnapshot(period="20241231", revenue=220.0),
        FinancialSnapshot(period="20231231", revenue=180.0),
        FinancialSnapshot(period="20221231", revenue=150.0),
    )

    series = engine._extract_field_series("revenue", facts.snapshots, {})

    assert series == [
        ("20241231", 220.0),
        ("20231231", 180.0),
        ("20221231", 150.0),
    ]


def test_metric_anomaly_tags_mixed_baseline_history():
    ensure_loaded()
    engine = AnomalyEngine()
    facts = _facts(
        FinancialSnapshot(period="20240930", operating_cashflow=130.0, net_profit=50.0),
        FinancialSnapshot(period="20240630", operating_cashflow=70.0, net_profit=45.0),
        FinancialSnapshot(period="20240331", operating_cashflow=20.0, net_profit=10.0),
        FinancialSnapshot(period="20231231", operating_cashflow=150.0, net_profit=60.0),
        FinancialSnapshot(period="20230930", operating_cashflow=90.0, net_profit=40.0),
        FinancialSnapshot(period="20230630", operating_cashflow=40.0, net_profit=36.0),
        FinancialSnapshot(period="20230331", operating_cashflow=10.0, net_profit=8.0),
    )

    anomaly = engine._evaluate_rule(
        CROSS_RULES["cashflow_profit_ratio"],
        facts.snapshots,
        {},
    )

    assert anomaly is not None
    assert anomaly.baseline_mode == "mixed_periods"
    assert anomaly.history_periods[0] == "20230930"
