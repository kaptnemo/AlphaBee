"""Company-context construction helpers for thesis and review nodes."""

from __future__ import annotations

from typing import Any

from alphabee.agents.facts.models import FinancialFacts, MarketFacts
from alphabee.agents.facts.tools.company_profile import get_company_profile
from alphabee.agents.facts.tools.industry_fact import get_industry_fact
from alphabee.agents.thesis.models import CompanyContext
from alphabee.industry.names import keyword_extract_industry


def _detect_market_cap(
    fact_text: str,
    market_facts: MarketFacts | None = None,
) -> str:
    """Detect market cap category from structured data or text hints."""
    if market_facts is not None and market_facts.market_cap is not None:
        mv = market_facts.market_cap / 1e8
        if mv >= 500:
            return "large"
        if mv >= 100:
            return "mid"
        return "small"
    text = fact_text.lower()
    if "大盘" in text or "蓝筹" in text or "白马" in text:
        return "large"
    if "中小盘" in text or "中盘" in text:
        return "mid"
    if "小盘" in text or "创业板" in text or "微盘" in text:
        return "small"
    return ""


def _detect_lifecycle(
    fact_text: str,
    financial_facts: FinancialFacts | None = None,
) -> str:
    """Detect lifecycle stage from text hints."""
    if financial_facts is not None and financial_facts.snapshots:
        yoy = financial_facts.snapshots[0].revenue_yoy or 0
        if yoy >= 20:
            return "growth"
        if yoy >= 5:
            return "mature"
    text = fact_text.lower()
    if "成熟" in text or "稳定" in text:
        return "mature"
    if "成长" in text or "高增长" in text:
        return "growth"
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

    # CompanyContext 的作用不是生成结论，而是给 thesis / review 提供“解释坐标系”：
    # 同样的增速、估值、现金流表现，在大盘蓝筹与成长股上含义可能完全不同。
    ctx.name = symbol
    profile: dict[str, Any] = {}

    try:
        ind_fact = get_industry_fact(symbol)
        ctx.industry = ind_fact.get("industry", "") or ctx.industry
        ctx.sub_industry = ind_fact.get("sw_code", "") or ""
        sw_daily = ind_fact.get("sw_daily", [])
        if sw_daily and isinstance(sw_daily[0], dict):
            item = sw_daily[0]
            # 这里不直接下行业结论，只把行业估值语境压缩成短摘要，
            # 让下游论点知道公司目前处在怎样的行业定价区间。
            ctx.business_model_summary = (
                f"行业PE(TTM): {item.get('industry_pe_ttm', 'N/A')}, 行业PB: {item.get('industry_pb', 'N/A')}"
            )
    except Exception:
        pass

    try:
        profile = get_company_profile(symbol)
        basic = profile.get("basic", {})
        if basic and not ctx.industry:
            tushare_industry = basic.get("industry", {})
            if isinstance(tushare_industry, dict):
                val = tushare_industry.get(0, "")
                if val:
                    ctx.industry = str(val)
    except Exception:
        pass

    if not ctx.industry:
        ctx.industry = keyword_extract_industry(fact_text.lower())

    # 当结构化信息不足时，允许使用 fact_text 做弱推断，
    # 但这里只提炼行业/市值/生命周期标签，不直接生成买卖判断。
    ctx.market_cap_category = _detect_market_cap(fact_text, market_facts)
    ctx.lifecycle_stage = _detect_lifecycle(fact_text, financial_facts)

    # 商业模式定位（COMPANY_TRACK Phase E3）：基于财务指标规则启发分类，
    # 指标不足时不猜测（other），由 thesis/review 按 archetype 切换审查口径。
    if financial_facts is not None and financial_facts.snapshots:
        snapshot = financial_facts.snapshots[0]
        from alphabee.company_track.business_model import classify_business_model

        raw_margin = getattr(snapshot, "gross_margin", None)
        gross_margin = raw_margin / 100.0 if raw_margin is not None else None  # % → RATIO
        rd_expense = getattr(snapshot, "rd_expense", None)
        revenue = getattr(snapshot, "revenue", None)
        rd_ratio = rd_expense / revenue if rd_expense is not None and revenue else None
        ctx.business_model, _ = classify_business_model(gross_margin=gross_margin, rd_ratio=rd_ratio)

    try:
        company = profile.get("company", {})
        if not ctx.business_model_summary and company:
            main_biz = company.get("main_business", {})
            if isinstance(main_biz, dict):
                biz_val = main_biz.get(0, "")
                if biz_val:
                    # 若行业估值摘要拿不到，则退回主营描述，
                    # 至少让下游知道企业靠什么赚钱、属于哪类商业模式。
                    ctx.business_model_summary = str(biz_val)[:300]
    except Exception:
        pass

    return ctx
