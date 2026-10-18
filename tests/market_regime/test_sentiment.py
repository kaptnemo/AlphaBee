"""涨停情绪温度计（P0 设计 §8.1）单测：sentiment_heat 规则 + risk_preference 权重重平衡。"""

from __future__ import annotations

from alphabee.market_regime.score_engine import RISK_DELTA_RULES, MarketScoreEngine, load_rules


def _heat_result(up: int | None, down: int | None) -> dict:
    rules = load_rules()
    engine = MarketScoreEngine(rules=rules).engine
    facts: dict[str, float] = {}
    if up is not None:
        facts["limit_up_count"] = float(up)
    if down is not None:
        facts["limit_down_count"] = float(down)
    return engine.run(["sentiment_heat"], fact_values=facts)["sentiment_heat"]


def test_sentiment_heat_rule_registered():
    rules = load_rules()
    assert "sentiment_heat" in rules
    assert "sentiment_heat" in RISK_DELTA_RULES


def test_limit_up_200_down_0_positive():
    assert _heat_result(200, 0)["sentiment_heat"] == 3.0  # 涨停 200 家 → +3（过热）


def test_limit_up_10_down_30_negative():
    assert _heat_result(10, 30)["sentiment_heat"] == -5.0  # 涨停 10 家（−2）+ 跌停 30 家（−3）→ 冰点


def test_sentiment_heat_missing_is_missing_fact():
    assert _heat_result(None, None)["level"] == "missing_fact"


def test_risk_delta_missing_sentiment_heat_treated_as_zero():
    """缺失 sentiment_heat 子项 ⇒ 按 0 中性参与 _risk_delta（既有口径）。"""
    engine = MarketScoreEngine()
    results = {
        "risk_preference_delta": {"risk_preference_delta": None, "level": "missing_fact"},
        "turnover_delta": {"turnover_delta": 1.0, "level": "ok"},
        "margin_delta": {"margin_delta": 1.0, "level": "ok"},
        "etf_delta": {"etf_delta": 1.0, "level": "ok"},
    }
    # 0.40*1 + 0.25*1 + 0.15*1 + 0.20*0 = 0.80
    assert engine._risk_delta(results) == 0.80


def test_risk_delta_clamped_to_plus_minus_5():
    engine = MarketScoreEngine()
    assert engine._risk_delta({"risk_preference_delta": {"risk_preference_delta": 100.0, "level": "ok"}}) == 5.0
    assert engine._risk_delta({"risk_preference_delta": {"risk_preference_delta": -100.0, "level": "ok"}}) == -5.0


def test_rebalanced_weights_sum_to_one():
    rules = load_rules()
    assert rules["turnover_delta"].weight == 0.40
    assert rules["margin_delta"].weight == 0.25
    assert rules["etf_delta"].weight == 0.15
    assert rules["sentiment_heat"].weight == 0.20
    # 三旧规则权重和 = 0.8（钉住新权重表，防静默漂移）
    assert rules["turnover_delta"].weight + rules["margin_delta"].weight + rules["etf_delta"].weight == 0.80
    # 四子项权重和 = 1.0
    total = sum(rules[name].weight for name in RISK_DELTA_RULES)
    assert total == 1.0


def test_risk_preference_delta_aggregate_uses_new_formula():
    rules = load_rules()
    agg = rules["risk_preference_delta"]
    assert set(agg.required_derived_facts) == {"turnover_delta", "margin_delta", "etf_delta", "sentiment_heat"}
    assert "0.40" in agg.formula and "0.25" in agg.formula and "0.15" in agg.formula and "0.20" in agg.formula
