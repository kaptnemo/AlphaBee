"""产能周期与核心盈利口径的三条新衍生事实规则。

- ``depreciation_to_revenue``        折旧摊销 / 营业收入（%）
- ``deducted_net_margin``            扣非归母净利润 / 营业收入（%）
- ``construction_in_progress_ratio`` 在建工程 / 营业收入（%）

三条规则的作用：让"转固压力"与"核心净利率"随最新报告期自动刷新，而不是让报告
回退到半年报文本/画像缓存里的旧数字。
"""

from __future__ import annotations

import pytest

from alphabee.agents.derived_facts.engine import Engine
from alphabee.agents.derived_facts.registry import RULES, load_rules

NEW_RULES = ("depreciation_to_revenue", "deducted_net_margin", "construction_in_progress_ratio")


@pytest.fixture(autouse=True)
def ensure_rules_loaded():
    load_rules()


class TestRuleRegistration:
    @pytest.mark.parametrize("name", NEW_RULES)
    def test_rule_is_registered(self, name):
        assert name in RULES

    @pytest.mark.parametrize("name", NEW_RULES)
    def test_rule_requires_canonical_fields_only(self, name):
        rule = RULES[name]
        assert rule.required_facts
        assert all(field.isidentifier() for field in rule.required_facts)
        assert rule.formula

    @pytest.mark.parametrize("name", NEW_RULES)
    def test_rule_has_thresholds_and_interpretation(self, name):
        rule = RULES[name]
        assert rule.thresholds
        assert set(rule.thresholds) == set(rule.interpretation)


class TestDepreciationToRevenue:
    def test_wuxi_style_ratio(self):
        # 药明康德 2026H1：折旧摊销 17.6 亿 / 营业收入 288.97 亿
        result = RULES["depreciation_to_revenue"].compute({"depreciation_amortization": 1.76e9, "revenue": 2.8897e10})
        assert result["depreciation_to_revenue"] == pytest.approx(6.09, abs=0.01)
        assert result["level"] == "moderate"

    def test_light_and_heavy_levels(self):
        rule = RULES["depreciation_to_revenue"]
        assert rule.compute({"depreciation_amortization": 1.0e8, "revenue": 1.0e10})["level"] == "light"
        assert rule.compute({"depreciation_amortization": 2.0e9, "revenue": 1.0e10})["level"] == "heavy"


class TestDeductedNetMargin:
    def test_nine_month_margin(self):
        # 2026 前三季度：扣非 164.77 亿 / 营收 455.37 亿
        result = RULES["deducted_net_margin"].compute({"net_profit_dedt": 1.6477e10, "revenue": 4.5537e10})
        assert result["deducted_net_margin"] == pytest.approx(36.18, abs=0.01)
        assert result["level"] == "strong"

    def test_zero_revenue_is_not_applicable(self):
        result = RULES["deducted_net_margin"].compute({"net_profit_dedt": 1.0, "revenue": 0.0})
        assert result["level"] == "not_applicable"


class TestConstructionInProgressRatio:
    def test_nine_month_ratio(self):
        # 2026-09-30：在建工程 95.33 亿 / 前三季度营收 455.37 亿
        result = RULES["construction_in_progress_ratio"].compute(
            {"construction_in_progress": 9.5328e9, "revenue": 4.5537e10}
        )
        assert result["construction_in_progress_ratio"] == pytest.approx(20.93, abs=0.01)
        assert result["level"] == "moderate"


class TestMissingFactsDegrade:
    @pytest.mark.parametrize("name", NEW_RULES)
    def test_missing_fact_does_not_raise(self, name):
        rule = RULES[name]
        result = rule.compute({"revenue": 1.0e10})
        assert result[name] is None
        assert result["level"] in ("invalid", "missing_fact")

    def test_engine_blocks_downstream_without_raising(self):
        results = Engine().run(list(NEW_RULES), {"revenue": 4.5537e10})
        for name in NEW_RULES:
            assert results[name][name] is None
