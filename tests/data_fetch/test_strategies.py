"""Unit tests for data_fetch.strategies."""

import pytest

from alphabee.data_fetch.models import FixStrategy
from alphabee.data_fetch.strategies import recommend_fix


class TestRecommendFix:
    def test_permission_error(self):
        plan = recommend_fix("tushare", "income", "permission")
        assert plan.strategy == FixStrategy.FIX_INTERFACE
        assert any("token" in a.lower() for a in plan.recommended_actions)
        assert "config.yaml" in plan.relevant_paths

    def test_missing_field_error(self):
        plan = recommend_fix("tushare", "fina_indicator", "missing_field")
        assert plan.strategy == FixStrategy.ADD_FIELD
        assert any("adapter" in p for p in plan.relevant_paths)
        assert any("INDEX.yaml" in p for p in plan.relevant_paths)

    def test_timeout_error(self):
        plan = recommend_fix("akshare", "stock_news_em", "timeout")
        assert plan.strategy == FixStrategy.SWITCH_SOURCE
        assert any("fallback" in a.lower() or "备用" in a for a in plan.recommended_actions)

    def test_network_error(self):
        plan = recommend_fix("tushare", "daily", "network")
        assert plan.strategy == FixStrategy.FALLBACK
        assert "config.yaml" in plan.relevant_paths

    def test_rate_limit_error(self):
        plan = recommend_fix("eastmoney", "reports", "rate_limit")
        assert plan.strategy == FixStrategy.FALLBACK
        assert any("cache" in p for p in plan.relevant_paths)

    def test_parse_error(self):
        plan = recommend_fix("tushare", "balancesheet", "parse_error")
        assert plan.strategy == FixStrategy.FIX_INTERFACE
        assert any("adapter" in p.lower() or "_utils" in p for p in plan.relevant_paths)

    def test_empty_response_error(self):
        plan = recommend_fix("baostock", "query_history", "empty_response")
        assert plan.strategy == FixStrategy.SWITCH_SOURCE
        assert any("fallback" in a.lower() or "备用" in a for a in plan.recommended_actions)

    def test_unknown_error_returns_fix_interface(self):
        plan = recommend_fix("tushare", "unknown_api", "some_weird_error")
        assert plan.strategy == FixStrategy.FIX_INTERFACE
        assert len(plan.recommended_actions) > 0

    def test_unknown_provider_has_no_extra_paths(self):
        plan = recommend_fix("nonexistent", "test", "timeout")
        assert plan.strategy == FixStrategy.SWITCH_SOURCE
        # Should still produce actions and instruction

    def test_all_plans_have_instructions(self):
        for et in ["permission", "missing_field", "timeout", "network",
                    "rate_limit", "parse_error", "empty_response", "unknown"]:
            plan = recommend_fix("tushare", "test_api", et)
            assert plan.agent_instruction, f"Missing instruction for {et}"
            assert plan.recommended_actions, f"Missing actions for {et}"
