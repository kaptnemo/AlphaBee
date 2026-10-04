from types import SimpleNamespace

import pytest

from alphabee.agents.facts.models import MarketFacts
from alphabee.orchestrator.services import company_context


def test_build_company_context_prefers_structured_industry_over_keywords(monkeypatch):
    monkeypatch.setattr(
        company_context,
        "get_industry_fact",
        lambda symbol: {
            "industry": "电力设备",
            "sw_code": "801730",
            "sw_daily": [],
        },
    )
    monkeypatch.setattr(
        company_context,
        "get_company_profile",
        lambda symbol: {
            "basic": {"industry": {0: "白酒"}},
            "company": {},
        },
    )

    ctx = company_context.build_company_context(
        symbol="300750.SZ",
        fact_text="这是一家银行和白酒概念公司。",
    )

    assert ctx.industry == "电力设备"
    assert ctx.sub_industry == "801730"


def test_build_company_context_prefers_structured_market_cap_over_text_hints(monkeypatch):
    monkeypatch.setattr(
        company_context,
        "get_industry_fact",
        lambda symbol: {"industry": "", "sw_code": "", "sw_daily": []},
    )
    monkeypatch.setattr(
        company_context,
        "get_company_profile",
        lambda symbol: {"basic": {}, "company": {}},
    )

    ctx = company_context.build_company_context(
        symbol="600519.SH",
        fact_text="市场常把它视作小盘成长股。",
        # MarketFacts.market_cap 单位为万元（models.py 注释）：800e4 万元 = 800 亿元
        market_facts=SimpleNamespace(market_cap=800e4),
    )

    assert ctx.market_cap_category == "large"


def test_build_company_context_prefers_structured_lifecycle_over_text_hints(monkeypatch):
    monkeypatch.setattr(
        company_context,
        "get_industry_fact",
        lambda symbol: {"industry": "", "sw_code": "", "sw_daily": []},
    )
    monkeypatch.setattr(
        company_context,
        "get_company_profile",
        lambda symbol: {"basic": {}, "company": {}},
    )

    ctx = company_context.build_company_context(
        symbol="300750.SZ",
        fact_text="公司已经进入成熟稳定期。",
        financial_facts=SimpleNamespace(snapshots=[SimpleNamespace(revenue_yoy=32.0)]),
    )

    assert ctx.lifecycle_stage == "growth"


# 市值分类单位回归钉（P0-1，/1e8 → /1e4，万元 → 亿元）：
# 真实市值基准为 Tushare 实测（单位：亿元）：002916=2541、300037=700、603979=368。
# 判别力自证：若把 /1e4 改回 /1e8，以下三条断言必红（变异实验见提交记录）。
_MARKET_CAP_PEGS = [
    ("002916.SZ", 2541, "large"),
    ("300037.SZ", 700, "large"),
    ("603979.SH", 368, "mid"),
]


@pytest.mark.parametrize("symbol,mv_yi,expected", _MARKET_CAP_PEGS)
def test_market_cap_category_regression_pegs(monkeypatch, symbol, mv_yi, expected):
    """三标的市值分档回归钉：不依赖外部数据，直接构造 MarketFacts 输入。"""
    monkeypatch.setattr(
        company_context,
        "get_industry_fact",
        lambda symbol: {"industry": "", "sw_code": "", "sw_daily": []},
    )
    monkeypatch.setattr(
        company_context,
        "get_company_profile",
        lambda symbol: {"basic": {}, "company": {}},
    )

    # MarketFacts.market_cap 单位是万元（models.py:254 注释）：亿元 * 1e4 → 万元
    market_facts = MarketFacts(stock_code=symbol, market_cap=mv_yi * 1e4)

    ctx = company_context.build_company_context(
        symbol=symbol,
        fact_text="",
        market_facts=market_facts,
    )

    assert ctx.market_cap_category == expected
