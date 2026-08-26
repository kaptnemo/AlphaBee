from alphabee.agents.anomaly.engine import AnomalyEngine
from alphabee.agents.anomaly.models import CrossRule
from alphabee.agents.anomaly.registry import CROSS_RULES, ensure_loaded
from alphabee.agents.facts.models import FinancialFacts, FinancialSnapshot


def _facts(*snapshots: FinancialSnapshot) -> FinancialFacts:
    return FinancialFacts(stock_code="002130.SZ", snapshots=list(snapshots))


def _annual_snapshot(period: str, income_tax_expense: float, total_profit: float) -> FinancialSnapshot:
    return FinancialSnapshot(
        period=period,
        income_tax_expense=income_tax_expense,
        total_profit=total_profit,
    )


def test_effective_tax_rate_uses_historical_etr_when_stable():
    ensure_loaded()
    engine = AnomalyEngine()
    # 历史 ETR 稳定在 0.09~0.12，本期 0.10 落在历史区间内，不应触发。
    facts = _facts(
        _annual_snapshot("20241231", 10.0, 100.0),  # 本期 ETR = 0.10
        _annual_snapshot("20231231", 12.0, 100.0),  # 0.12
        _annual_snapshot("20221231", 11.0, 100.0),  # 0.11
        _annual_snapshot("20211231", 10.0, 100.0),  # 0.10
        _annual_snapshot("20201231", 9.0, 100.0),  # 0.09
    )

    anomaly = engine._evaluate_rule(CROSS_RULES["effective_tax_rate"], facts.snapshots, {})

    assert anomaly is not None
    assert anomaly.level == "none"
    # 法定税率仅作解释参考，不再作为基线。
    assert anomaly.baseline_mode != "statutory"
    assert anomaly.reference_rate == 0.25


def test_effective_tax_rate_skips_when_history_too_short():
    ensure_loaded()
    engine = AnomalyEngine()
    # 只有 1 期历史，无法形成自身历史基线，应跳过而非用法定税率硬凑。
    facts = _facts(
        _annual_snapshot("20241231", 10.0, 100.0),
        _annual_snapshot("20231231", 12.0, 100.0),
    )

    anomaly = engine._evaluate_rule(CROSS_RULES["effective_tax_rate"], facts.snapshots, {})

    assert anomaly is None


def test_effective_tax_rate_statutory_as_baseline_keeps_legacy_behavior():
    ensure_loaded()
    engine = AnomalyEngine()
    rule = CrossRule(
        id="effective_tax_rate",
        name="税费/利润背离",
        description="",
        metric_a="income_tax_expense",
        metric_b="total_profit",
        rule_type="ratio",
        anomaly_direction="drop",
        threshold_sigma=2.0,
        use_statutory=True,
        statutory_rate=0.25,
        statutory_as_baseline=True,
    )
    facts = _facts(
        _annual_snapshot("20241231", 8.66, 100.0),  # 本期 ETR = 0.0866
        _annual_snapshot("20231231", 9.0, 100.0),
        _annual_snapshot("20221231", 10.0, 100.0),
        _annual_snapshot("20211231", 9.0, 100.0),
        _annual_snapshot("20201231", 10.0, 100.0),
    )

    anomaly = engine._evaluate_rule(rule, facts.snapshots, {})

    assert anomaly is not None
    # 显式开启制度基线时保持旧行为：z 用 0.25±0.0125。
    assert anomaly.baseline_mode == "statutory"
    assert anomaly.baseline_mean == 0.25
    assert anomaly.baseline_std == 0.0125
    assert anomaly.level == "high"
    assert anomaly.reference_rate == 0.25
