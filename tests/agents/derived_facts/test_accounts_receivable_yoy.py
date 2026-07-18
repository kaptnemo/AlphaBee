"""
测试 agent 能否通过 derived_facts rules 正确计算 accounts_receivable_yoy。

覆盖范围：
- accounts_receivable_yoy 规则本身的加载与计算
- 链式依赖：accounts_receivable_yoy → accounts_receivable_growth / receivable_growth_gap
- agent 工具函数 evaluate_derived_facts 的完整调用路径
- 缺失字段时的优雅降级与 blocked_by 传播
- 边界条件：零除、负增长、零增长
"""

import pytest

from alphabee.agents.derived_facts.engine import Engine
from alphabee.agents.derived_facts.registry import RULES, load_rules
from alphabee.agents.derived_facts.tools import evaluate_derived_facts

# ── 前置：确保规则注册表已加载 ─────────────────────────────────────────────


@pytest.fixture(autouse=True)
def ensure_rules_loaded():
    load_rules()


# ═══════════════════════════════════════════════════════════════════════════
# 1. 规则加载
# ═══════════════════════════════════════════════════════════════════════════


class TestRuleLoading:
    def test_rule_is_registered(self):
        """accounts_receivable_yoy 应出现在全局规则注册表中。"""
        assert "accounts_receivable_yoy" in RULES

    def test_rule_has_required_facts(self):
        rule = RULES["accounts_receivable_yoy"]
        assert "accounts_receivable" in rule.required_facts
        assert "accounts_receivable_prev" in rule.required_facts

    def test_rule_has_no_required_derived_facts(self):
        """accounts_receivable_yoy 直接依赖 canonical 字段，无衍生依赖。"""
        rule = RULES["accounts_receivable_yoy"]
        assert rule.required_derived_facts == []

    def test_rule_formula_is_set(self):
        rule = RULES["accounts_receivable_yoy"]
        assert "accounts_receivable" in rule.formula
        assert "accounts_receivable_prev" in rule.formula

    def test_rule_has_thresholds(self):
        rule = RULES["accounts_receivable_yoy"]
        assert set(rule.thresholds) == {"fast", "moderate", "slow"}


# ═══════════════════════════════════════════════════════════════════════════
# 2. 规则计算正确性
# ═══════════════════════════════════════════════════════════════════════════


class TestRuleComputation:
    @pytest.fixture
    def rule(self):
        return RULES["accounts_receivable_yoy"]

    def test_positive_growth(self, rule):
        """(1200 - 1000) / 1000 * 100 = 20.0"""
        result = rule.compute({"accounts_receivable": 1200, "accounts_receivable_prev": 1000})
        assert result["accounts_receivable_yoy"] == pytest.approx(20.0)

    def test_zero_growth(self, rule):
        result = rule.compute({"accounts_receivable": 1000, "accounts_receivable_prev": 1000})
        assert result["accounts_receivable_yoy"] == pytest.approx(0.0)

    def test_negative_growth(self, rule):
        """应收账款下降 25%。"""
        result = rule.compute({"accounts_receivable": 750, "accounts_receivable_prev": 1000})
        assert result["accounts_receivable_yoy"] == pytest.approx(-25.0)

    def test_zero_division(self, rule):
        """上期应收账款为 0 时不应抛出异常，level 应为 invalid。"""
        result = rule.compute({"accounts_receivable": 1200, "accounts_receivable_prev": 0})
        assert result["accounts_receivable_yoy"] is None
        assert result["level"] == "invalid"

    def test_missing_current(self, rule):
        result = rule.compute({"accounts_receivable_prev": 1000})
        assert result["accounts_receivable_yoy"] is None
        assert result["level"] in ("invalid", "missing_fact")

    def test_missing_prev(self, rule):
        result = rule.compute({"accounts_receivable": 1200})
        assert result["accounts_receivable_yoy"] is None
        assert result["level"] in ("invalid", "missing_fact")


# ═══════════════════════════════════════════════════════════════════════════
# 3. 阈值档位判断
# ═══════════════════════════════════════════════════════════════════════════


class TestThresholds:
    @pytest.fixture
    def rule(self):
        return RULES["accounts_receivable_yoy"]

    @pytest.mark.parametrize(
        "current,prev,expected_level",
        [
            (1310, 1000, "fast"),  # +31% → fast（严格 > 30）
            (1300, 1000, "moderate"),  # +30% 恰好在 moderate 上边界（10 <= 30 <= 30）
            (1299, 1000, "moderate"),  # +29.9% → moderate
            (1100, 1000, "moderate"),  # +10% 下边界
            (1099, 1000, "slow"),  # +9.9% → slow
            (900, 1000, "slow"),  # 负增长 → slow
        ],
    )
    def test_threshold_level(self, rule, current, prev, expected_level):
        result = rule.compute({"accounts_receivable": current, "accounts_receivable_prev": prev})
        assert result["level"] == expected_level


# ═══════════════════════════════════════════════════════════════════════════
# 4. Engine 链式依赖
# ═══════════════════════════════════════════════════════════════════════════


class TestEngineChain:
    @pytest.fixture
    def engine(self):
        return Engine()

    def test_chain_computes_all_three(self, engine):
        """accounts_receivable_yoy → accounts_receivable_growth → receivable_growth_gap 全链路。"""
        facts = {
            "accounts_receivable": 1600,
            "accounts_receivable_prev": 1000,  # +60%
            "revenue_yoy": 15.0,
        }
        results = engine.run(
            ["accounts_receivable_yoy", "accounts_receivable_growth", "receivable_growth_gap"],
            facts,
        )
        assert results["accounts_receivable_yoy"]["accounts_receivable_yoy"] == pytest.approx(60.0)
        assert results["accounts_receivable_growth"]["accounts_receivable_growth"] == pytest.approx(60.0)
        assert results["receivable_growth_gap"]["receivable_growth_gap"] == pytest.approx(45.0)
        assert results["receivable_growth_gap"]["level"] == "high_risk"

    def test_chain_injects_yoy_for_downstream(self, engine):
        """即使不显式请求 accounts_receivable_yoy，growth 规则也能拿到它的值。"""
        facts = {
            "accounts_receivable": 1200,
            "accounts_receivable_prev": 1000,  # +20%
            "revenue_yoy": 10.0,
        }
        results = engine.run(["accounts_receivable_growth", "receivable_growth_gap"], facts)
        assert results["accounts_receivable_growth"]["accounts_receivable_growth"] == pytest.approx(20.0)
        assert results["receivable_growth_gap"]["receivable_growth_gap"] == pytest.approx(10.0)
        assert results["receivable_growth_gap"]["level"] == "low_risk"

    def test_missing_prev_propagates_blocked(self, engine):
        """缺少 accounts_receivable_prev 时，下游规则应标记为 blocked。"""
        facts = {"accounts_receivable": 1200, "revenue_yoy": 10.0}
        results = engine.run(
            ["accounts_receivable_yoy", "accounts_receivable_growth", "receivable_growth_gap"],
            facts,
        )
        assert results["accounts_receivable_yoy"]["level"] == "invalid"
        assert results["accounts_receivable_growth"]["level"] == "blocked"
        assert "accounts_receivable_yoy" in results["accounts_receivable_growth"]["blocked_by"]
        assert results["receivable_growth_gap"]["level"] == "blocked"

    def test_topo_order_is_correct(self, engine):
        """引擎返回的结果顺序应保证依赖先于被依赖方。"""
        facts = {
            "accounts_receivable": 1200,
            "accounts_receivable_prev": 1000,
            "revenue_yoy": 10.0,
        }
        results = engine.run(
            ["receivable_growth_gap", "accounts_receivable_growth"],
            facts,
        )
        # 即使请求顺序颠倒，yoy 也应先于 growth 和 gap 出现
        keys = list(results.keys())
        yoy_idx = keys.index("accounts_receivable_yoy")
        growth_idx = keys.index("accounts_receivable_growth")
        gap_idx = keys.index("receivable_growth_gap")
        assert yoy_idx < growth_idx
        assert yoy_idx < gap_idx


# ═══════════════════════════════════════════════════════════════════════════
# 5. Agent 工具函数 evaluate_derived_facts
# ═══════════════════════════════════════════════════════════════════════════


class TestEvaluateDerivedFacts:
    def test_tool_returns_yoy_value(self):
        """agent 工具调用应在输出中包含计算结果。"""
        output = evaluate_derived_facts(
            ["accounts_receivable_yoy"],
            {"accounts_receivable": 1200, "accounts_receivable_prev": 1000},
        )
        assert "accounts_receivable_yoy" in output
        assert "20" in output  # 20% growth

    def test_tool_returns_level(self):
        output = evaluate_derived_facts(
            ["accounts_receivable_yoy"],
            {"accounts_receivable": 1200, "accounts_receivable_prev": 1000},
        )
        assert "moderate" in output

    def test_tool_chain_shows_transitive_dep(self):
        """请求 accounts_receivable_growth 时，输出末尾应附加 accounts_receivable_yoy 依赖详情。"""
        output = evaluate_derived_facts(
            ["accounts_receivable_growth"],
            {"accounts_receivable": 1600, "accounts_receivable_prev": 1000},
        )
        assert "accounts_receivable_growth" in output
        assert "accounts_receivable_yoy" in output  # 传递依赖附录

    def test_tool_missing_prev_reports_error(self):
        """缺少 accounts_receivable_prev 时工具应返回错误信息，不应抛出异常。"""
        output = evaluate_derived_facts(
            ["accounts_receivable_yoy"],
            {"accounts_receivable": 1200},
        )
        assert "accounts_receivable_yoy" in output
        assert any(marker in output for marker in ["❌", "⚠️", "invalid", "blocked"])

    def test_tool_unknown_rule_warns(self):
        output = evaluate_derived_facts(
            ["nonexistent_rule"],
            {"accounts_receivable": 1200},
        )
        assert "nonexistent_rule" in output
        assert "⚠️" in output

    def test_tool_full_revenue_quality_chain(self):
        """revenue_quality_risk 所需的全部衍生事实应一次调用全部算出。"""
        output = evaluate_derived_facts(
            ["accounts_receivable_growth", "revenue_growth", "receivable_growth_gap"],
            {
                "accounts_receivable": 1600,
                "accounts_receivable_prev": 1000,
                "revenue_yoy": 15.0,
            },
        )
        assert "high_risk" in output
        assert "45" in output  # gap = 60 - 15 = 45pp


# ═══════════════════════════════════════════════════════════════════════════
# 6. Agent 配置验证
# ═══════════════════════════════════════════════════════════════════════════


class TestAgentConfiguration:
    def test_agent_tool_is_evaluate_derived_facts(self):
        """derived_fact_agent 的工具列表中应包含 evaluate_derived_facts。"""
        import inspect

        from alphabee.agents.derived_facts import agent as agent_module

        src = inspect.getsource(agent_module)
        assert "evaluate_derived_facts" in src

    def test_evaluate_derived_facts_is_callable(self):
        from alphabee.agents.derived_facts.tools import evaluate_derived_facts as tool_fn

        assert callable(tool_fn)

    def test_evaluate_derived_facts_signature(self):
        """工具函数签名应接受 rule_names 和 fact_values 参数。"""
        import inspect

        from alphabee.agents.derived_facts.tools import evaluate_derived_facts as tool_fn

        params = inspect.signature(tool_fn).parameters
        assert "rule_names" in params
        assert "fact_values" in params
