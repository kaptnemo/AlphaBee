"""Phase 1 tests — position band mapping + weekly delta limit."""

from __future__ import annotations

import pytest

from alphabee.market_regime.position import (
    PositionRules,
    advise_position,
    find_band,
    load_position_rules,
)


@pytest.fixture
def rules() -> PositionRules:
    return load_position_rules()


class TestBandMapping:
    def test_band_boundaries(self, rules):
        assert find_band(0, rules).regime == "熊市阶段"
        assert find_band(29.9, rules).regime == "熊市阶段"
        assert find_band(30, rules).regime == "风险增加"
        assert find_band(49.9, rules).regime == "风险增加"
        assert find_band(50, rules).regime == "震荡阶段"
        assert find_band(69.9, rules).regime == "震荡阶段"
        assert find_band(70, rules).regime == "趋势健康"
        assert find_band(84.9, rules).regime == "趋势健康"
        assert find_band(85, rules).regime == "强牛阶段"
        assert find_band(100, rules).regime == "强牛阶段"

    def test_regime_names(self, rules):
        regimes = {b.regime for b in rules.bands}
        assert regimes == {"强牛阶段", "趋势健康", "震荡阶段", "风险增加", "熊市阶段"}


class TestAdviseWithoutPrev:
    def test_no_prev_uses_raw_band(self, rules):
        advice = advise_position(75, prev_week_score=None, rules=rules)
        assert advice.regime == "趋势健康"
        assert advice.band_low == 0.60
        assert advice.band_high == 0.80
        assert advice.position_low == 0.60
        assert advice.position_high == 0.80
        assert advice.restricted is False
        assert advice.weekly_change is None

    def test_low_score_bear_band(self, rules):
        advice = advise_position(20, rules=rules)
        assert advice.regime == "熊市阶段"
        assert advice.position_low == 0.00
        assert advice.position_high == 0.20


class TestWeeklyDeltaLimit:
    def test_limit_binds_high_side(self, rules):
        """上周仓位 0.85，本周仍强牛（0.80-0.90）→ 上限压缩到 0.90（原值正好）无限制；"""
        # 强牛 band 0.80-0.90，prev 0.60 → 上限 max(0.60+0.10)=0.70 < 0.90 受限
        advice = advise_position(90, prev_week_score=0.60, rules=rules)
        assert advice.regime == "强牛阶段"
        assert advice.position_low == pytest.approx(0.70)  # max(0.80, 0.60-0.10)
        assert advice.position_high == pytest.approx(0.70)  # min(0.90, 0.60+0.10)
        assert advice.restricted is True
        assert any("限制" in line for line in advice.rationale)

    def test_limit_binds_low_side(self, rules):
        """上周 0.10，本周熊市（0.00-0.20）→ 区间下限抬升到 0.00（原值），上限 0.20。"""
        advice = advise_position(10, prev_week_score=0.10, rules=rules)
        assert advice.regime == "熊市阶段"
        assert advice.position_low == pytest.approx(0.00)
        assert advice.position_high == pytest.approx(0.20)

    def test_limit_binds_when_band_fully_on_one_side(self, rules):
        """上周 0.85，本周熊市（0.00-0.20）→ 单周最多降到 0.75。"""
        advice = advise_position(15, prev_week_score=0.85, rules=rules)
        assert advice.regime == "熊市阶段"
        assert advice.position_low == pytest.approx(0.75)
        assert advice.position_high == pytest.approx(0.75)
        assert advice.restricted is True

    def test_no_binding_when_prev_within_band(self, rules):
        advice = advise_position(75, prev_week_score=0.70, rules=rules)
        assert advice.restricted is False
        assert advice.position_low == pytest.approx(0.60)
        assert advice.position_high == pytest.approx(0.80)

    def test_weekly_change_recorded(self, rules):
        advice = advise_position(75, prev_week_score=0.50, rules=rules)
        assert advice.weekly_change == pytest.approx(74.5)  # score - prev_week_score(分数)
