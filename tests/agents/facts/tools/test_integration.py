"""Integration tests for FactCollector Agent tools.

These tests exercise the FULL internal pipeline without mocking:
  TuShareHelper → TuShare_Adapter → SyncTTLCache → get_*_fact → render

Requirements:
  - Valid TUSHARE_TOKEN environment variable (or in .env)
  - alphabee/static/all_stocks.csv (for competition_fact local data)

Key verification points:
  - Adapted（canonical）field names in returned data
  - render() can consume get_*_fact() output without errors
  - Empty/invalid inputs are handled gracefully
  - Error states produce meaningful output
"""

from __future__ import annotations

import os
import pytest

from alphabee.agents.facts.tools._utils import normalize_ts_code, to_pure_code


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _check_tushare_available() -> bool:
    """Return True if Tushare token is configured."""
    token = os.getenv("TUSHARE_TOKEN", "")
    return bool(token and token.strip() and not token.startswith("${"))


@pytest.fixture(autouse=True)
def _clear_caches():
    """Clear all SyncTTLCache instances before each test for isolation."""
    modules_to_clear = [
        "alphabee.agents.facts.tools.company_profile",
        "alphabee.agents.facts.tools.financial_fact",
        "alphabee.agents.facts.tools.market_fact",
        "alphabee.agents.facts.tools.operation_fact",
        "alphabee.agents.facts.tools.industry_fact",
        "alphabee.agents.facts.tools.competition_fact",
        "alphabee.agents.facts.tools.expectation_fact",
        "alphabee.agents.facts.tools.risk_fact",
    ]
    import importlib
    for mod_path in modules_to_clear:
        try:
            mod = importlib.import_module(mod_path)
            cache = getattr(mod, "_CACHE", None)
            if cache is not None:
                cache._cache.clear()
                cache._inflight.clear()
        except Exception:
            pass


tushare_required = pytest.mark.skipif(
    not _check_tushare_available(),
    reason="TUSHARE_TOKEN not configured",
)


# ──────────────────────────────────────────────────────────────────────
# Well-known test symbols
# ──────────────────────────────────────────────────────────────────────
# 600519: 贵州茅台 — 上海主板，白酒行业
# 000001: 平安银行 — 深圳主板
# 300760: 迈瑞医疗 — 创业板

_KWEICHOW_MOUTAI = "600519.SH"
_PINGAN_BANK = "000001.SZ"
_MINDRAY = "300760.SZ"


# ══════════════════════════════════════════════════════════════════════
# _utils (no external dependencies, always runs)
# ══════════════════════════════════════════════════════════════════════


class TestUtilsIntegration:
    """Integration-level validation of utility functions."""

    def test_normalize_ts_code_all_formats(self):
        """All common formats normalize to standard Tushare code."""
        assert normalize_ts_code("600519") == "600519.SH"
        assert normalize_ts_code("sh600519") == "600519.SH"
        assert normalize_ts_code("000001.SZ") == "000001.SZ"
        assert normalize_ts_code("sz000001") == "000001.SZ"
        assert normalize_ts_code("300760") == "300760.SZ"
        assert normalize_ts_code("430047") == "430047.BJ"

    def test_to_pure_code_all_exchanges(self):
        assert to_pure_code("600519.SH") == "600519"
        assert to_pure_code("000001.SZ") == "000001"
        assert to_pure_code("430047.BJ") == "430047"


# ══════════════════════════════════════════════════════════════════════
# company_profile
# ══════════════════════════════════════════════════════════════════════


class TestCompanyProfileIntegration:

    @tushare_required
    def test_fetches_moutai_data(self):
        """贵州茅台 — well-known blue chip with complete data."""
        from alphabee.agents.facts.tools.company_profile import (
            get_company_profile, render,
        )

        data = get_company_profile(_KWEICHOW_MOUTAI)

        assert "basic" in data
        assert "company" in data
        assert "holders" in data

        # Verify adapted field names in holders (critical post-fix check)
        holders = data["holders"]
        if holders:
            row = holders[0]
            assert "top10_holder_name" in row
            assert "top10_holder_amount" in row
            assert "period" in row
            assert "holder_name" not in row, "Raw Tushare 'holder_name' leaked"

        # Render must produce valid output
        output = render(data)
        assert len(output) > 100
        assert "600519" in output

    @tushare_required
    def test_fetches_sz_stock(self):
        """平安银行 — different exchange, different industry."""
        from alphabee.agents.facts.tools.company_profile import get_company_profile

        data = get_company_profile(_PINGAN_BANK)
        assert "basic" in data
        assert "holders" in data

    @tushare_required
    def test_render_contains_expected_sections(self):
        """render must show all key sections from real data."""
        from alphabee.agents.facts.tools.company_profile import (
            get_company_profile, render,
        )

        data = get_company_profile(_KWEICHOW_MOUTAI)
        output = render(data)

        assert "公司档案" in output
        assert "基本信息" in output
        assert "公司详情" in output


# ══════════════════════════════════════════════════════════════════════
# financial_fact
# ══════════════════════════════════════════════════════════════════════


class TestFinancialFactIntegration:

    @tushare_required
    def test_fetches_multi_period_and_adapter_works(self):
        """Full pipeline: API → adapter → dedup → canonical fields."""
        from alphabee.agents.facts.tools.financial_fact import get_financial_fact

        data = get_financial_fact(_KWEICHOW_MOUTAI, periods=4)

        assert data["stock_code"] == _KWEICHOW_MOUTAI
        assert len(data["ref_dates"]) >= 1

        # ── Verify adapter renamed all raw fields ──
        income = data["income"]
        if income:
            r = income[0]
            assert "revenue" in r
            assert "operating_profit" in r
            assert "net_profit" in r
            assert "total_revenue" not in r, "Raw 'total_revenue' leaked"
            assert "n_income" not in r, "Raw 'n_income' leaked"

        balance = data["balance"]
        if balance:
            r = balance[0]
            assert "shareholders_equity" in r
            assert "total_hldr_eqy_exc_min_int" not in r, "Raw field leaked"
            assert "accounts_receivable" in r
            assert "accounts_receiv" not in r, "Raw 'accounts_receiv' leaked"

        cashflow = data["cashflow"]
        if cashflow:
            r = cashflow[0]
            assert "operating_cashflow" in r
            assert "n_cashflow_act" not in r, "Raw field leaked"

        fina = data["fina"]
        if fina:
            r = fina[0]
            assert "gross_margin" in r
            assert "grossprofit_margin" not in r, "Raw 'grossprofit_margin' leaked"

    @tushare_required
    def test_extract_financial_facts_feeds_engines(self):
        """extract_financial_facts output is used by DerivedFacts/Signal."""
        from alphabee.agents.facts.tools.financial_fact import (
            get_financial_fact, extract_financial_facts,
        )

        data = get_financial_fact(_KWEICHOW_MOUTAI, periods=4)
        facts = extract_financial_facts(data)

        assert "revenue" in facts
        assert "net_profit" in facts
        assert "roe" in facts
        assert all(isinstance(v, (int, float)) for v in facts.values())

    @tushare_required
    def test_render_produces_all_tables(self):
        """render generates all expected Markdown tables."""
        from alphabee.agents.facts.tools.financial_fact import (
            get_financial_fact, render,
        )

        data = get_financial_fact(_KWEICHOW_MOUTAI, periods=4)
        output = render(data)

        assert "财务事实数据" in output
        for section in ("利润表", "资产负债表", "现金流量表", "核心财务比率", "同比成长性"):
            assert section in output, f"Missing section: {section}"

    @tushare_required
    def test_single_period_boundary(self):
        from alphabee.agents.facts.tools.financial_fact import get_financial_fact

        data = get_financial_fact(_KWEICHOW_MOUTAI, periods=1)
        assert len(data["ref_dates"]) == 1


# ══════════════════════════════════════════════════════════════════════
# market_fact
# ══════════════════════════════════════════════════════════════════════


class TestMarketFactIntegration:

    @tushare_required
    def test_fetches_latest_market_and_adapter_works(self):
        from alphabee.agents.facts.tools.market_fact import get_market_fact

        data = get_market_fact(_KWEICHOW_MOUTAI)

        assert data["stock_code"] == _KWEICHOW_MOUTAI

        d = data.get("latest_daily")
        if d is not None:
            assert "close_price" in d
            assert "close" not in d, "Raw 'close' leaked"
            assert "price_change_pct" in d
            assert "pct_chg" not in d, "Raw 'pct_chg' leaked"

        ma = data.get("ma", {})
        assert isinstance(ma, dict)

    @tushare_required
    def test_extract_market_facts_for_engines(self):
        from alphabee.agents.facts.tools.market_fact import (
            get_market_fact, extract_market_facts,
        )

        data = get_market_fact(_KWEICHOW_MOUTAI)
        facts = extract_market_facts(data)

        db = data.get("latest_daily_basic")
        if db is not None:
            assert "pe_ttm" in facts
            assert isinstance(facts["pe_ttm"], (int, float))

    @tushare_required
    def test_render_has_sections(self):
        from alphabee.agents.facts.tools.market_fact import (
            get_market_fact, render,
        )

        data = get_market_fact(_KWEICHOW_MOUTAI)
        output = render(data)

        assert "行情事实数据" in output
        if data.get("latest_daily") is not None:
            assert "最新报价" in output


# ══════════════════════════════════════════════════════════════════════
# operation_fact (was broken by duplicate definitions — now fixed)
# ══════════════════════════════════════════════════════════════════════


class TestOperationFactIntegration:

    @tushare_required
    def test_adapter_renames_correctly(self):
        """After fix: adapted names present, raw names absent."""
        from alphabee.agents.facts.tools.operation_fact import get_operation_fact

        data = get_operation_fact(_KWEICHOW_MOUTAI)

        assert data["stock_code"] == _KWEICHOW_MOUTAI

        items = data.get("latest_items", [])
        if items:
            row = items[0]
            assert "biz_segment_name" in row
            assert "biz_segment_revenue" in row
            assert "biz_segment_cost" in row
            assert "biz_segment_profit" in row
            assert "bz_item" not in row, "Raw 'bz_item' leaked — adapter bypassed"
            assert "bz_sales" not in row, "Raw 'bz_sales' leaked"

    @tushare_required
    def test_render_uses_adapted_names(self):
        """render must find adapted field names in data."""
        from alphabee.agents.facts.tools.operation_fact import (
            get_operation_fact, render,
        )

        data = get_operation_fact(_KWEICHOW_MOUTAI)
        output = render(data)
        assert "主营业务构成" in output


# ══════════════════════════════════════════════════════════════════════
# industry_fact (was broken by duplicate definitions — now fixed)
# ══════════════════════════════════════════════════════════════════════


class TestIndustryFactIntegration:

    @tushare_required
    def test_sw_classification_found(self):
        """贵州茅台 should match 白酒 SW industry."""
        from alphabee.agents.facts.tools.industry_fact import get_industry_fact

        data = get_industry_fact(_KWEICHOW_MOUTAI)

        assert data["stock_code"] == _KWEICHOW_MOUTAI
        assert data["industry"] == "白酒"

        if data["sw_code"]:
            assert len(data["sw_code"]) >= 6

    @tushare_required
    def test_sw_daily_uses_adapted_names(self):
        """After fix: SW daily data uses adapted field names."""
        from alphabee.agents.facts.tools.industry_fact import get_industry_fact

        data = get_industry_fact(_KWEICHOW_MOUTAI)

        sw_daily = data.get("sw_daily", [])
        if sw_daily:
            row = sw_daily[0]
            assert "industry_close" in row
            assert "close" not in row, "Raw 'close' leaked"
            assert "industry_change_pct" in row
            assert "pct_change" not in row, "Raw 'pct_change' leaked"

    @tushare_required
    def test_render_renders(self):
        from alphabee.agents.facts.tools.industry_fact import (
            get_industry_fact, render,
        )

        data = get_industry_fact(_KWEICHOW_MOUTAI)
        output = render(data)
        assert "行业事实数据" in output


# ══════════════════════════════════════════════════════════════════════
# competition_fact (was broken by variable shadowing — now fixed)
# ══════════════════════════════════════════════════════════════════════


class TestCompetitionFactIntegration:

    def test_local_csv_peer_list_works(self):
        """Basic peer info comes from local CSV — works without Tushare."""
        from alphabee.agents.facts.tools.competition_fact import get_competition_fact

        data = get_competition_fact(_KWEICHOW_MOUTAI, max_peers=3)

        assert data["stock_code"] == _KWEICHOW_MOUTAI
        assert data["company_name"] != ""
        assert data["industry"] == "白酒"
        assert data["total_peers"] > 0

    def test_render_with_local_data(self):
        """render works even without Tushare market data."""
        from alphabee.agents.facts.tools.competition_fact import (
            get_competition_fact, render,
        )

        data = get_competition_fact("600519.SH", max_peers=3)
        output = render(data)

        assert "竞争格局" in output
        assert "白酒" in output

    def test_nonexistent_stock_graceful(self):
        """Nonexistent stock returns empty data, not exception."""
        from alphabee.agents.facts.tools.competition_fact import get_competition_fact

        data = get_competition_fact("999999.SH", max_peers=3)
        assert data["stock_code"] == "999999.SH"
        assert data["industry"] == ""
        assert data["peers"] == []

    def test_empty_data_render_no_crash(self):
        from alphabee.agents.facts.tools.competition_fact import render

        data = {
            "stock_code": "999999.SH", "company_name": "",
            "industry": "", "total_peers": 0, "peers": [],
        }
        output = render(data)
        assert len(output) > 0

    def test_sz_stock_works(self):
        from alphabee.agents.facts.tools.competition_fact import get_competition_fact

        data = get_competition_fact(_PINGAN_BANK, max_peers=3)
        assert data["stock_code"] == _PINGAN_BANK
        assert data["company_name"] != ""

    def test_peer_records_use_adapted_names(self):
        """After fix: variable shadowing resolved, peers use canonical names."""
        from alphabee.agents.facts.tools.competition_fact import get_competition_fact

        data = get_competition_fact(_KWEICHOW_MOUTAI, max_peers=3)
        if data["peers"]:
            peer = data["peers"][0]
            assert "stock_code" in peer
            assert "company_name" in peer
            assert "market_cap" in peer  # adapted, not 'total_mv'


# ══════════════════════════════════════════════════════════════════════
# expectation_fact (was broken by duplicate definitions — now fixed)
# ══════════════════════════════════════════════════════════════════════


class TestExpectationFactIntegration:

    @tushare_required
    def test_adapter_renames_correctly(self):
        """After fix: forecast/express use adapted field names."""
        from alphabee.agents.facts.tools.expectation_fact import get_expectation_fact

        data = get_expectation_fact(_KWEICHOW_MOUTAI)

        assert data["stock_code"] == _KWEICHOW_MOUTAI

        forecast = data.get("forecast", [])
        if forecast:
            row = forecast[0]
            assert "forecast_type" in row
            assert "type" not in row, "Raw 'type' leaked"
            assert "period" in row
            assert "end_date" not in row, "Raw 'end_date' leaked"
            assert "forecast_net_profit_min" in row

        express = data.get("express", [])
        if express:
            row = express[0]
            assert "express_revenue" in row
            assert "revenue" not in row, "Raw 'revenue' leaked in express"
            assert "express_net_profit" in row

    @tushare_required
    def test_render_all_paths_work(self):
        from alphabee.agents.facts.tools.expectation_fact import (
            get_expectation_fact, render,
        )

        data = get_expectation_fact(_KWEICHOW_MOUTAI)
        output = render(data)
        assert "业绩预期事实数据" in output


# ══════════════════════════════════════════════════════════════════════
# risk_fact (was broken by duplicate definitions — now fixed)
# ══════════════════════════════════════════════════════════════════════


class TestRiskFactIntegration:

    def test_akshare_news_without_tushare(self):
        """AkShare news works without Tushare token."""
        from alphabee.agents.facts.tools.risk_fact import get_risk_fact

        data = get_risk_fact(_KWEICHOW_MOUTAI)

        assert data["stock_code"] == _KWEICHOW_MOUTAI

        news = data.get("news", [])
        news_error = data.get("news_error")

        if not news_error and news:
            row = news[0]
            assert "news_title" in row
            assert "news_publish_time" in row

    def test_render_with_real_data(self):
        from alphabee.agents.facts.tools.risk_fact import (
            get_risk_fact, render,
        )

        data = get_risk_fact(_KWEICHOW_MOUTAI)
        output = render(data)
        assert "风险事实数据" in output

    @tushare_required
    def test_tushare_pledge_adapted_names(self):
        """After fix: pledge data uses adapted field names."""
        from alphabee.agents.facts.tools.risk_fact import get_risk_fact

        data = get_risk_fact(_KWEICHOW_MOUTAI)

        pledge = data.get("pledge", [])
        pledge_error = data.get("pledge_error")

        if pledge and not pledge_error:
            row = pledge[0]
            assert "period" in row
            assert "end_date" not in row, "Raw 'end_date' leaked"
            assert "unreleased_pledge" in row
            assert "unrest_pledge" not in row, "Raw 'unrest_pledge' leaked"
            assert "released_pledge" in row

    @tushare_required
    def test_repurchase_adapted_names(self):
        """After fix: repurchase data uses adapted field names."""
        from alphabee.agents.facts.tools.risk_fact import get_risk_fact

        data = get_risk_fact(_KWEICHOW_MOUTAI)

        repurchase = data.get("repurchase", [])
        repurchase_error = data.get("repurchase_error")

        if repurchase and not repurchase_error:
            row = repurchase[0]
            assert "repurchase_progress" in row
            assert "proc" not in row, "Raw 'proc' leaked"
            assert "repurchase_volume" in row
            assert "vol" not in row, "Raw 'vol' leaked"


# ══════════════════════════════════════════════════════════════════════
# Structured model extraction (feeds DerivedFacts / Signal engines)
# ══════════════════════════════════════════════════════════════════════


class TestModelExtraction:

    @tushare_required
    def test_financial_facts_model(self):
        from alphabee.agents.facts.tools.financial_fact import get_financial_facts_model

        model = get_financial_facts_model(_KWEICHOW_MOUTAI, periods=4)

        assert model.stock_code == _KWEICHOW_MOUTAI
        assert len(model.snapshots) >= 1

        snap = model.snapshots[0]
        assert snap.period is not None
        assert len(snap.period) == 8  # YYYYMMDD

        # to_fact_values() feeds the DerivedFacts / Signal engines
        fact_values = model.to_fact_values()
        assert isinstance(fact_values, dict)
        assert len(fact_values) > 0

    @tushare_required
    def test_market_facts_model(self):
        from alphabee.agents.facts.tools.market_fact import get_market_facts_model

        model = get_market_facts_model(_KWEICHOW_MOUTAI)

        assert model.stock_code == _KWEICHOW_MOUTAI

        fact_values = model.to_fact_values()
        assert isinstance(fact_values, dict)


# ══════════════════════════════════════════════════════════════════════
# End-to-end: orchestrator's collect_facts node
# ══════════════════════════════════════════════════════════════════════


class TestCollectFactsPipeline:
    """Full pipeline: FactCollector LLM + models + DerivedFacts + Signal."""

    @tushare_required
    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_collect_facts_full_pipeline(self):
        """Run the complete collect_facts node with real data."""
        from alphabee.orchestrator.collectors import collect_facts
        from alphabee.orchestrator.state import OrchestratorState
        from alphabee.core import RunStatus
        from langchain_core.messages import HumanMessage

        state: OrchestratorState = {
            "messages": [
                HumanMessage(content="帮我分析一下贵州茅台(600519)的投资价值")
            ],
        }

        result = await collect_facts(state, {})

        assert "run" in result
        assert "steps" in result
        assert "artifacts" in result
        assert "issues" in result

        run_obj = result["run"]
        assert run_obj.status in {
            RunStatus.RUNNING, RunStatus.SUCCEEDED, RunStatus.PARTIAL,
        }

        steps = result["steps"]
        assert len(steps) >= 1

        artifacts = result["artifacts"]
        artifact_types = {a.type for a in artifacts}
        assert "fact_collection" in artifact_types

        # DerivedFacts and Signal engines should produce output
        if "derived_facts" in artifact_types:
            df_artifact = next(
                a for a in artifacts if a.type == "derived_facts"
            )
            assert df_artifact.value.get("rule_count", 0) > 0

        if "signal_analysis" in artifact_types:
            sig_artifact = next(
                a for a in artifacts if a.type == "signal_analysis"
            )
            assert sig_artifact.value.get("rule_count", 0) > 0
