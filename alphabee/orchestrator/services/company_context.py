"""Company-context construction helpers for thesis and review nodes."""

from __future__ import annotations

from alphabee.agents.facts.models import FinancialFacts, MarketFacts
from alphabee.agents.thesis.models import CompanyContext
from alphabee.agents.facts.tools.company_profile import get_company_profile
from alphabee.agents.facts.tools.industry_fact import get_industry_fact


def _keyword_extract_industry(text: str) -> str:
    """Fallback: extract industry from free text using keyword matching."""
    industry_keywords: list[tuple[str, str]] = [
        ("白酒", "白酒"), ("银行", "银行"), ("证券", "证券"),
        ("保险", "保险"), ("房地产", "房地产"),
        ("半导体", "半导体"), ("芯片", "半导体"),
        ("新能源汽车", "新能源汽车"), ("光伏", "光伏"),
        ("医药", "医药"), ("消费电子", "消费电子"),
        ("钢铁", "钢铁"), ("煤炭", "煤炭"), ("电力", "电力"),
        ("化工", "化工"), ("机械", "机械"), ("军工", "军工"),
        ("农林", "农林牧渔"), ("食品", "食品饮料"), ("家电", "家电"),
        ("纺织", "纺织服装"), ("建材", "建材"), ("建筑", "建筑装饰"),
        ("传媒", "传媒"), ("计算机", "计算机"), ("通信", "通信"),
        ("环保", "环保"), ("公用", "公用事业"), ("交通", "交通运输"),
    ]
    for kw, industry in industry_keywords:
        if kw in text:
            return industry
    return ""


def _detect_market_cap(
    fact_text: str,
    market_facts: MarketFacts | None = None,
) -> str:
    """Detect market cap category from structured data or text hints."""
    text = fact_text.lower()
    if "大盘" in text or "蓝筹" in text or "白马" in text:
        return "large"
    if "中小盘" in text or "中盘" in text:
        return "mid"
    if "小盘" in text or "创业板" in text or "微盘" in text:
        return "small"
    if market_facts is not None and market_facts.market_cap is not None:
        mv = market_facts.market_cap / 1e8
        if mv >= 500:
            return "large"
        if mv >= 100:
            return "mid"
        return "small"
    return ""


def _detect_lifecycle(
    fact_text: str,
    financial_facts: FinancialFacts | None = None,
) -> str:
    """Detect lifecycle stage from text hints."""
    text = fact_text.lower()
    if "成熟" in text or "稳定" in text:
        return "mature"
    if "成长" in text or "高增长" in text:
        return "growth"
    if financial_facts is not None and financial_facts.snapshots:
        yoy = financial_facts.snapshots[0].revenue_yoy or 0
        if yoy >= 20:
            return "growth"
        if yoy >= 5:
            return "mature"
    return ""


def build_company_context(
    symbol: str | None,
    fact_text: str,
    *,
    financial_facts: FinancialFacts | None = None,
    market_facts: MarketFacts | None = None,
) -> CompanyContext:
    """Build a ``CompanyContext`` from structured data sources."""
    ctx = CompanyContext(symbol=symbol or "")
    if not symbol:
        return ctx

    ctx.name = symbol
    profile: dict = {}

    try:
        profile = get_company_profile(symbol)
        basic = profile.get("basic", {})
        if basic:
            tushare_industry = basic.get("industry", {})
            if isinstance(tushare_industry, dict):
                val = tushare_industry.get(0, "")
                if val:
                    ctx.industry = str(val)
    except Exception:
        pass

    try:
        ind_fact = get_industry_fact(symbol)
        if not ctx.industry:
            ctx.industry = ind_fact.get("industry", "")
        ctx.sub_industry = ind_fact.get("sw_code", "") or ""
        sw_daily = ind_fact.get("sw_daily", [])
        if sw_daily and isinstance(sw_daily[0], dict):
            item = sw_daily[0]
            ctx.business_model_summary = (
                f"行业PE(TTM): {item.get('industry_pe_ttm', 'N/A')}, "
                f"行业PB: {item.get('industry_pb', 'N/A')}"
            )
    except Exception:
        pass

    if not ctx.industry:
        ctx.industry = _keyword_extract_industry(fact_text.lower())

    ctx.market_cap_category = _detect_market_cap(fact_text, market_facts)
    ctx.lifecycle_stage = _detect_lifecycle(fact_text, financial_facts)

    try:
        company = profile.get("company", {})
        if not ctx.business_model_summary and company:
            main_biz = company.get("main_business", {})
            if isinstance(main_biz, dict):
                biz_val = main_biz.get(0, "")
                if biz_val:
                    ctx.business_model_summary = str(biz_val)[:300]
    except Exception:
        pass

    return ctx

