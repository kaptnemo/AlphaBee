"""E3 消费端测试：build_company_context 商业模式分类 + reviewer archetype 视角。"""

from types import SimpleNamespace

from alphabee.agents.facts.models import FinancialFacts, FinancialSnapshot
from alphabee.agents.thesis.models import CompanyContext
from alphabee.orchestrator.services import company_context


def _financial_facts(gross_margin=None, rd_expense=None, revenue=None):
    snapshot = FinancialSnapshot(
        period="20251231",
        gross_margin=gross_margin,
        rd_expense=rd_expense,
        revenue=revenue,
        revenue_yoy=10.0,
    )
    return FinancialFacts(stock_code="X", snapshots=[snapshot])


def _mock_sources(monkeypatch, industry="", sw_code="", profile_basic=None):
    monkeypatch.setattr(
        company_context, "get_industry_fact", lambda symbol: {"industry": industry, "sw_code": sw_code, "sw_daily": []}
    )
    monkeypatch.setattr(
        company_context,
        "get_company_profile",
        lambda symbol: {"basic": profile_basic or {}, "company": {}},
    )


def test_business_model_classified_from_financial_facts(monkeypatch):
    _mock_sources(monkeypatch)
    # ODM 特征：毛利率 12%，研发费率 4%（研发 4 亿 / 营收 100 亿）
    ctx = company_context.build_company_context(
        symbol="601138.SH",
        fact_text="",
        financial_facts=_financial_facts(gross_margin=12.0, rd_expense=4e8, revenue=1e10),
    )
    assert ctx.business_model == "odm"


def test_business_model_component_from_financial_facts(monkeypatch):
    _mock_sources(monkeypatch)
    # 核心零部件：毛利率 55%，研发费率 18%
    ctx = company_context.build_company_context(
        symbol="002415.SZ",
        fact_text="",
        financial_facts=_financial_facts(gross_margin=55.0, rd_expense=1.8e9, revenue=1e10),
    )
    assert ctx.business_model == "component"


def test_business_model_other_when_data_missing(monkeypatch):
    _mock_sources(monkeypatch)
    ctx = company_context.build_company_context(symbol="000001.SZ", fact_text="", financial_facts=None)
    assert ctx.business_model == ""  # 无财务数据 → 不分类（other 留给消费端判断）


def test_company_context_serializes_business_model():
    ctx = CompanyContext(symbol="X", business_model="odm")
    assert ctx.to_dict()["business_model"] == "odm"


# ── reviewer archetype 视角 ────────────────────────────────────────────────


def test_reviewer_odm_lens(monkeypatch):
    from alphabee.agents.thesis.reviewer import ThesisReviewer

    reviewer = ThesisReviewer()
    dim = SimpleNamespace(
        name="盈利质量",
        evidence=[SimpleNamespace(signal_name="gross_margin"), SimpleNamespace(signal_name="net_margin")],
        confidence=0.8,
        judgment="negative",
        score=-0.4,
    )
    ctx = CompanyContext(symbol="601138.SH", industry="通信设备", business_model="odm")

    verdict = reviewer._layer1_check("earnings_quality", dim, {}, ctx)
    assert any("ODM 代工商业模式毛利率低属常态" in issue for issue in verdict.issues)
