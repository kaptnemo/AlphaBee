"""Phase 1 tests — deterministic scoring engine (no LLM, no network)."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from alphabee.market_regime.models import MarketIndicatorSnapshot
from alphabee.market_regime.score_engine import (
    MarketScoreEngine,
    build_decision_issue,
    compute_features,
    load_rules,
)


def _trading_dates(n: int, end: str = "2026-08-07") -> list[str]:
    anchor = date.fromisoformat(end)
    out: list[str] = []
    day = anchor
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day -= timedelta(days=1)
    return list(reversed(out))


def _build_history(n: int = 2600, end: str = "2026-08-07") -> pd.DataFrame:
    rows = []
    for i, day in enumerate(_trading_dates(n, end)):
        pe = 14.0 - 0.001 * i
        rows.append(
            {
                "date": day,
                "hs300_pe_ttm": pe,
                "hs300_pb": 1.5 - 0.0005 * i,
                "hs300_ep_ttm": 100.0 / pe,
                "cn_10y_yield": 2.5 - 0.0001 * i,
                "hs300_close": 3000 + 0.8 * i,
                "market_turnover": 10000 + i,
                "margin_balance": 12000 + i,
                "social_financing_increment": 20000 + i,
            }
        )
    return pd.DataFrame(rows)


def _full_values() -> dict[str, float]:
    return {
        "hs300_pe_ttm": 12.0,
        "hs300_pb": 1.3,
        "hs300_ep_ttm": 8.33,
        "cn_10y_yield": 1.8,
        "m1_m2_gap": 2.0,
        "hs300_close": 4500,
        "hs300_ma20": 4400,
        "hs300_ma60": 4300,
        "hs300_ma250": 4000,
        "breadth_above_ma60_pct": 65.0,
        "market_turnover": 12000,
        "margin_balance": 15000,
        "etf_net_inflow": 50.0,
        "social_financing_increment": 25000.0,
    }


class TestRuleLoading:
    def test_rules_loaded(self):
        rules = load_rules()
        for name in (
            "erp_score",
            "pe_percentile_score",
            "pb_percentile_score",
            "valuation_score",
            "ma_structure_score",
            "breadth_score",
            "momentum_score",
            "trend_score",
            "rate_cycle_score",
            "m1_m2_score",
            "socfin_score",
            "liquidity_score",
            "market_score",
            "turnover_delta",
            "margin_delta",
            "etf_delta",
            "risk_preference_delta",
        ):
            assert name in rules, name

    def test_weights_match_roadmap(self):
        rules = load_rules()
        assert rules["valuation_score"].weight == 0.30
        assert rules["trend_score"].weight == 0.40
        assert rules["liquidity_score"].weight == 0.30
        assert rules["market_score"].weight == 1.0

    def test_dependency_dag_is_declared(self):
        rules = load_rules()
        assert set(rules["valuation_score"].required_derived_facts) == {
            "erp_score",
            "pe_percentile_score",
            "pb_percentile_score",
        }
        assert set(rules["trend_score"].required_derived_facts) == {
            "ma_structure_score",
            "breadth_score",
            "momentum_score",
        }
        assert set(rules["liquidity_score"].required_derived_facts) == {
            "rate_cycle_score",
            "m1_m2_score",
            "socfin_score",
        }
        assert set(rules["market_score"].required_derived_facts) == {
            "valuation_score",
            "trend_score",
            "liquidity_score",
        }


class TestEngineDeterministic:
    @pytest.fixture
    def engine(self):
        return MarketScoreEngine()

    def test_same_input_same_output(self, engine):
        values = _full_values()
        history = _build_history()
        r1 = engine.score(values, history=history, asof_date="2026-08-07")
        r2 = engine.score(values, history=history, asof_date="2026-08-07")
        assert r1.scores.total_score == r2.scores.total_score
        assert r1.scores.valuation_score == r2.scores.valuation_score
        assert r1.scores.trend_score == r2.scores.trend_score
        assert r1.scores.liquidity_score == r2.scores.liquidity_score

    def test_scores_in_range(self, engine):
        result = engine.score(_full_values(), history=_build_history(), asof_date="2026-08-07")
        assert 0 <= result.scores.total_score <= 100
        for s in (
            result.scores.valuation_score,
            result.scores.trend_score,
            result.scores.liquidity_score,
        ):
            assert 0 <= s <= 100

    def test_accepts_snapshot_object(self, engine):
        snap = MarketIndicatorSnapshot(date="2026-08-07", values=_full_values())
        result = engine.score(snap, history=_build_history(), asof_date="2026-08-07")
        assert result.date == "2026-08-07"
        assert result.scores.total_score is not None


class TestWeightComposition:
    def test_market_score_is_weighted_sum(self):
        """market_score = valuation×0.30 + trend×0.40 + liquidity×0.30."""
        engine = MarketScoreEngine()
        result = engine.score(_full_values(), history=_build_history(), asof_date="2026-08-07")
        expected = (
            result.scores.valuation_score * 0.30
            + result.scores.trend_score * 0.40
            + result.scores.liquidity_score * 0.30
        )
        assert result.scores.total_score == pytest.approx(expected + result.scores.risk_preference_delta, abs=0.01)

    def test_rule_levels_present(self):
        engine = MarketScoreEngine()
        result = engine.score(_full_values(), history=_build_history(), asof_date="2026-08-07")
        for name in ("valuation_score", "trend_score", "liquidity_score", "market_score"):
            rule_result = result.rule_results[name]
            assert rule_result["level"] != "missing_fact"
            assert "interpretation" in rule_result or rule_result["level"] in ("invalid", "blocked")


class TestMissingDataDegradation:
    def test_missing_indicator_is_not_zero(self):
        """缺 hs300_pe_ttm → pe_percentile_score 应为 missing_fact，而非 0 分拖低估值。"""
        engine = MarketScoreEngine()
        values = _full_values()
        del values["hs300_pe_ttm"]
        result = engine.score(values, history=_build_history(), asof_date="2026-08-07")
        assert result.rule_results["pe_percentile_score"]["level"] == "missing_fact"
        # 估值引擎从可用子指标重归一化，而不是把缺失项当 0
        if result.scores.valuation_score is not None:
            assert result.scores.valuation_score > 0

    def test_engine_renormalizes_on_missing_engine(self):
        """估值引擎整组缺失 → market_score 在趋势/流动性上重归一化。"""
        engine = MarketScoreEngine()
        values = _full_values()
        for key in ("hs300_pe_ttm", "hs300_pb", "hs300_ep_ttm", "cn_10y_yield"):
            values.pop(key, None)
        result = engine.score(values, history=_build_history(), asof_date="2026-08-07")
        assert result.scores.valuation_score is None
        assert result.scores.total_score is not None
        expected = (result.scores.trend_score * 0.40 + result.scores.liquidity_score * 0.30) / 0.70
        assert result.scores.total_score == pytest.approx(expected + result.scores.risk_preference_delta, abs=0.02)

    def test_no_history_degrades_to_missing_fact(self):
        engine = MarketScoreEngine()
        result = engine.score(_full_values(), history=None, asof_date="2026-08-07")
        # 无历史 → 分位/动量规则降级，但趋势引擎（纯当日字段）仍可算
        assert "erp_score" in result.missing_facts or "pe_percentile_score" in result.missing_facts
        assert result.rule_results["ma_structure_score"]["level"] not in ("missing_fact", "invalid", "blocked")


class TestPercentileNoLookahead:
    def test_future_data_does_not_change_percentile(self):
        """在 asof_date 之后放入极端便宜的 PE，不应改变当日分位（无前视偏差）。"""
        engine = MarketScoreEngine()
        history = _build_history()
        values = _full_values()

        before = engine.score(values, history=history, asof_date="2026-08-07").scores.valuation_score

        # 在 asof_date 之后加入 10 天极端便宜（PE=1）的估值
        future_rows = [
            {"date": "2026-08-10", "hs300_pe_ttm": 1.0, "hs300_pb": 0.1, "hs300_ep_ttm": 100.0, "cn_10y_yield": 1.0},
        ]
        polluted = pd.concat([history, pd.DataFrame(future_rows)], ignore_index=True)
        after = engine.score(values, history=polluted, asof_date="2026-08-07").scores.valuation_score
        assert after == before

    def test_pe_percentile_matches_manual_rank(self):
        history = _build_history()
        features = compute_features(_full_values(), history, "2026-08-07")
        # pe_percentile 应等于 history 中 <= 当前 PE 的比例
        current = 12.0
        cutoff = pd.Timestamp("2026-08-07") - pd.DateOffset(years=10)
        window = history[history["date"] <= "2026-08-07"]
        window = window[window["date"] >= cutoff.date().isoformat()]["hs300_pe_ttm"]
        expected = float((window <= current).mean())
        assert features["pe_percentile"] == pytest.approx(expected, abs=0.01)


class TestRiskPreferenceAdjustment:
    def test_delta_bounded(self):
        engine = MarketScoreEngine()
        values = _full_values()
        # 极端过热：成交额/融资余额远超 20 日均值
        values["market_turnover"] = values["market_turnover"] * 1.5
        values["margin_balance"] = values["margin_balance"] * 1.5
        result = engine.score(values, history=_build_history(), asof_date="2026-08-07")
        assert -5.0 <= result.scores.risk_preference_delta <= 5.0

    def test_status_mapping(self):
        engine = MarketScoreEngine()
        result = engine.score(_full_values(), history=_build_history(), asof_date="2026-08-07")
        assert result.risk_preference_status in ("positive", "neutral", "negative")


class TestSnapshotPayload:
    def test_snapshot_fields_populated(self):
        engine = MarketScoreEngine()
        result = engine.score(_full_values(), history=_build_history(), asof_date="2026-08-07", prev_week_score=0.5)
        snap = result.snapshot
        assert snap.date == "2026-08-07"
        assert snap.regime in ("强牛阶段", "趋势健康", "震荡阶段", "风险增加", "熊市阶段")
        assert snap.position_low is not None
        assert snap.position_high is not None
        assert isinstance(snap.main_drivers, list)
        assert isinstance(snap.risks, list)

    def test_weekly_limit_recorded(self):
        engine = MarketScoreEngine()
        # 熊市分位（总分很低）但上周仓位 0.85 → 单周最多降到 0.75
        values = _full_values()
        for key in (
            "hs300_close",
            "hs300_ma20",
            "hs300_ma60",
            "hs300_ma250",
            "breadth_above_ma60_pct",
            "market_turnover",
            "margin_balance",
        ):
            values.pop(key, None)
        values["m1_m2_gap"] = -5.0
        result = engine.score(values, history=None, asof_date="2026-08-07", prev_week_score=0.85)
        if result.position is not None and result.position.weekly_change is not None:
            assert result.position.position_low >= 0.75 - 1e-6 or result.position.restricted


class TestDecisionIssue:
    def test_decision_maker_and_evidence(self):
        engine = MarketScoreEngine()
        result = engine.score(_full_values(), history=_build_history(), asof_date="2026-08-07")
        decision, issues = build_decision_issue(result)
        assert decision.maker == "market_score_engine"
        assert decision.confidence == 1.0
        assert any(ref.ref_id == "hs300_pe_ttm" for ref in decision.evidence_refs)

    def test_low_score_produces_issue(self):
        engine = MarketScoreEngine()
        values = _full_values()
        # 压低到明显低分：清空趋势与流动性，仅保留高估
        for key in (
            "hs300_close",
            "hs300_ma20",
            "hs300_ma60",
            "hs300_ma250",
            "breadth_above_ma60_pct",
            "market_turnover",
            "margin_balance",
            "m1_m2_gap",
        ):
            values.pop(key, None)
        result = engine.score(values, history=None, asof_date="2026-08-07")
        decision, issues = build_decision_issue(result)
        if result.scores.total_score is not None and result.scores.total_score < 50:
            assert any(issue.category == "market_regime" for issue in issues)

    def test_missing_data_produces_issue(self):
        engine = MarketScoreEngine()
        result = engine.score(_full_values(), history=None, asof_date="2026-08-07")
        _, issues = build_decision_issue(result)
        assert any(issue.category == "missing_data" for issue in issues)
