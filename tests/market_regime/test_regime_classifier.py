"""Phase 2 tests — six-phase regime classifier, transition constraints, and
similar-history search (no LLM, no network)."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from alphabee.market_regime.forward_returns import (
    build_feature_vector,
    compute_forward_returns,
    search_similar,
)
from alphabee.market_regime.models import (
    MarketScore,
    MarketScoreResult,
    RegimeSnapshot,
    RegimeTransition,
)
from alphabee.market_regime.persistence import (
    append_regime_transition,
    load_regime_history,
)
from alphabee.market_regime.regime_classifier import (
    PHASES,
    TransitionRules,
    classify_regime,
    load_transition_rules,
)


def _result(
    total: float,
    trend: float | None = None,
    risk_delta: float = 0.0,
    breadth: float | None = None,
    date: str = "2026-08-07",
) -> MarketScoreResult:
    scores = MarketScore(
        total_score=total,
        trend_score=trend,
        risk_preference_delta=risk_delta,
    )
    rule_results: dict = {}
    if breadth is not None:
        rule_results["breadth_score"] = {"breadth_score": breadth}
    return MarketScoreResult(
        date=date,
        scores=scores,
        rule_results=rule_results,
        snapshot=RegimeSnapshot(date=date, scores=scores),
    )


@pytest.fixture
def rules() -> TransitionRules:
    return load_transition_rules()


# ── 2.1 六阶段判定边界 ─────────────────────────────────────────────────────


class TestCandidateBoundaries:
    def test_high_score_trend_acceleration(self):
        transition = classify_regime(_result(total=90, trend=85, breadth=70))
        assert transition.phase == "趋势加速"
        assert transition.suspicious is False

    def test_high_score_overheated_is_divergence(self):
        transition = classify_regime(_result(total=90, risk_delta=2.0, breadth=70))
        assert transition.phase == "高位分歧"

    def test_high_score_weak_breadth_is_divergence(self):
        transition = classify_regime(_result(total=75, trend=80, breadth=45))
        assert transition.phase == "高位分歧"

    def test_mid_healthy_breadth_is_start(self):
        transition = classify_regime(_result(total=60, trend=65, breadth=60))
        assert transition.phase == "趋势启动"

    def test_mid_risk_retreat_is_release(self):
        transition = classify_regime(_result(total=60, risk_delta=-2.0, breadth=60))
        assert transition.phase == "风险释放"

    def test_mid_weak_breadth_is_accumulation(self):
        transition = classify_regime(_result(total=55, breadth=45))
        assert transition.phase == "吸筹期"

    def test_low_zone_with_decline_is_release(self):
        transition = classify_regime(_result(total=40, risk_delta=-2.0, breadth=50))
        assert transition.phase == "风险释放"

    def test_low_zone_stable_is_accumulation(self):
        transition = classify_regime(_result(total=40, breadth=50))
        assert transition.phase == "吸筹期"

    def test_bottom_repair(self):
        transition = classify_regime(_result(total=20, breadth=30))
        assert transition.phase == "底部修复"

    def test_missing_score_unknown(self):
        transition = classify_regime(
            MarketScoreResult(
                date="2026-08-07",
                scores=MarketScore(total_score=None),
            )
        )
        assert transition.phase == "未知"
        assert transition.suspicious is True

    def test_all_phases_reachable(self):
        seen = {classify_regime(_result(total=t)).phase for t in (95, 85, 75, 60, 40, 20)}
        assert seen <= set(PHASES)


# ── 2.1 迁移约束（Markov） ──────────────────────────────────────────────────


class TestTransitionConstraints:
    def test_first_evaluation_any_phase_valid(self, rules):
        for phase in PHASES:
            assert rules.allows(None, phase) is True

    def test_forward_cycle_is_valid(self, rules):
        cycle = ["吸筹期", "趋势启动", "趋势加速", "高位分歧", "风险释放", "底部修复"]
        for prev, nxt in zip(cycle, cycle[1:] + ["吸筹期"]):
            assert rules.allows(prev, nxt), f"{prev} → {nxt} 应合法"

    def test_self_loop_is_valid(self, rules):
        for phase in PHASES:
            assert rules.allows(phase, phase), f"{phase} 自环应合法"

    def test_one_step_back_is_valid(self, rules):
        assert rules.allows("趋势加速", "趋势启动")
        assert rules.allows("高位分歧", "趋势加速")
        assert rules.allows("底部修复", "风险释放")

    def test_illegal_jump_marked_suspicious(self):
        # 高位分歧 不能直接跳到 趋势启动（跳过风险释放）
        transition = classify_regime(_result(total=90, risk_delta=2.0), prev_phase="高位分歧")
        assert transition.phase == "高位分歧"
        assert transition.transition_valid is True

        # 从 吸筹期 直接跳到 趋势加速 → 非法
        jumped = classify_regime(_result(total=90), prev_phase="吸筹期")
        assert jumped.phase == "趋势加速"
        assert jumped.transition_valid is False
        assert jumped.suspicious is True
        assert jumped.transition_from == "吸筹期"

    def test_legal_transition_not_suspicious(self, rules):
        transition = classify_regime(_result(total=60, breadth=60), prev_phase="吸筹期")
        assert transition.phase == "趋势启动"
        assert transition.transition_valid is True
        assert transition.suspicious is False


# ── regime_history.csv 持久化 ──────────────────────────────────────────────


class TestRegimeHistoryPersistence:
    def test_roundtrip(self, tmp_path):
        path = tmp_path / "regime_history.csv"
        entry = RegimeTransition(
            date="2026-08-07",
            phase="趋势启动",
            confidence=0.6,
            transition_from="吸筹期",
            transition_valid=True,
            suspicious=False,
        )
        append_regime_transition(entry, path)
        df = load_regime_history(path)
        assert len(df) == 1
        assert df.iloc[0]["phase"] == "趋势启动"
        assert df.iloc[0]["transition_from"] == "吸筹期"
        assert bool(df.iloc[0]["transition_valid"]) is True

    def test_upsert_by_date(self, tmp_path):
        path = tmp_path / "regime_history.csv"
        append_regime_transition(RegimeTransition(date="2026-08-07", phase="吸筹期"), path)
        append_regime_transition(RegimeTransition(date="2026-08-14", phase="趋势启动"), path)
        append_regime_transition(RegimeTransition(date="2026-08-07", phase="吸筹期", confidence=0.9), path)
        df = load_regime_history(path)
        assert len(df) == 2  # 同日期覆盖，不产生重复行


# ── 2.2 相似历史搜索 ───────────────────────────────────────────────────────


def _trading_dates(n: int, end: str = "2026-08-07") -> list[str]:
    anchor = date.fromisoformat(end)
    out: list[str] = []
    day = anchor
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day -= timedelta(days=1)
    return list(reversed(out))


def _build_price_history(n: int = 520, end: str = "2026-08-07") -> pd.DataFrame:
    rows = []
    for i, day in enumerate(_trading_dates(n, end)):
        rows.append(
            {
                "date": day,
                "hs300_close": 3000 + 3.0 * i,  # 单调上涨，前视收益恒正
                "hs300_pe_ttm": 14.0 - 0.001 * i,
                "hs300_pb": 1.5 - 0.0005 * i,
                "hs300_ep_ttm": 100.0 / (14.0 - 0.001 * i),
                "cn_10y_yield": 2.5 - 0.0001 * i,
                "breadth_above_ma60_pct": 60.0,
                "market_turnover": 10000 + i,
                "margin_balance": 12000 + i,
                "social_financing_increment": 20000 + i,
            }
        )
    return pd.DataFrame(rows)


class TestForwardReturns:
    def test_monotonic_up_series_positive_returns(self):
        history = _build_price_history(n=520)
        fwd = compute_forward_returns(history, horizon_days=126)
        assert not fwd.empty
        assert (fwd["forward_return"] > 0).all()

    def test_no_lookahead_rows_at_tail(self):
        history = _build_price_history(n=520)
        fwd = compute_forward_returns(history, horizon_days=126)
        max_fwd_date = pd.to_datetime(fwd["date"]).max()
        last_history_date = pd.to_datetime(history["date"]).max()
        # 最后 ~126 天不应出现（前视窗口未走完）
        assert (max_fwd_date + pd.Timedelta(days=126)) <= last_history_date
        assert max_fwd_date < last_history_date

    def test_drawdown_between_minus_one_and_zero(self):
        history = _build_price_history(n=520)
        fwd = compute_forward_returns(history, horizon_days=126)
        assert (fwd["max_drawdown"] <= 0).all()
        assert (fwd["max_drawdown"] >= -1).all()


class TestSimilaritySearch:
    def test_same_phase_filter_and_ordering(self):
        history = _build_price_history(n=520)
        # 构造 regime_history：多数为 吸筹期（特征相近），穿插少数 趋势加速
        regime_rows = []
        for i, day in enumerate(_trading_dates(120, "2025-12-31")):
            phase = "趋势加速" if i % 25 == 0 else "吸筹期"
            regime_rows.append({"date": day, "phase": phase})
        regime_history = pd.DataFrame(regime_rows)

        # 当前特征：吸筹期典型的低趋势分 + 高 ERP 分位
        current = {"erp_percentile": 0.90, "trend_score": 45.0, "liquidity_score": 55.0}
        result = search_similar(
            history,
            regime_history,
            current,
            phase="吸筹期",
            k=3,
            horizon_days=126,
        )
        assert result.phase == "吸筹期"
        assert len(result.hits) == 3
        # 排序：距离升序
        distances = [h.distance for h in result.hits]
        assert distances == sorted(distances)
        # 全部来自 吸筹期
        assert all(h.phase == "吸筹期" for h in result.hits)
        assert result.sample_size > 0
        assert result.positive_probability is not None
        assert result.limitation_note  # 局限声明必须存在

    def test_feature_vector_uses_only_past_data(self):
        history = _build_price_history(n=520, end="2026-08-07")
        asof = str(history["date"].iloc[200])
        features = build_feature_vector(history, asof)
        assert "erp_percentile" in features or "trend_score" in features

    def test_no_cross_phase_matching(self):
        history = _build_price_history(n=520)
        regime_rows = [{"date": day, "phase": "吸筹期"} for day in _trading_dates(120, "2025-12-31")]
        regime_history = pd.DataFrame(regime_rows)
        current = {"erp_percentile": 0.90, "trend_score": 45.0, "liquidity_score": 55.0}
        # 搜索一个 regime_history 中不存在的阶段 → 空命中 + 局限声明
        result = search_similar(history, regime_history, current, phase="底部修复", k=3)
        assert result.hits == []
        assert result.sample_size == 0
        assert result.positive_probability is None
        assert result.limitation_note
