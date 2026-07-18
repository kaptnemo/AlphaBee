from types import SimpleNamespace

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
        market_facts=SimpleNamespace(market_cap=800e8),
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
        financial_facts=SimpleNamespace(
            snapshots=[SimpleNamespace(revenue_yoy=32.0)]
        ),
    )

    assert ctx.lifecycle_stage == "growth"
