import datetime
import json
import math

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from alphabee.collectors.tushare.helper import TuShareHelper
from alphabee.config import settings
from alphabee.tools.cache import AsyncTTLCache


# ---------------------------------------------------------------------------
# Pydantic models — all keyed by period (YYYYMMDD)
# ---------------------------------------------------------------------------

class IncomeStatement(BaseModel):
    """利润表核心数据（单期）"""
    period: str = Field(description="报告期（YYYYMMDD），如 '20241231' 表示2024年年报")
    revenue: float = Field(description="营业总收入（元）")
    operating_profit: float = Field(description="营业利润（元）")
    net_profit: float = Field(description="归母净利润（元）")
    ebitda: float = Field(description="息税折旧摊销前利润EBITDA（元），衡量核心盈利能力")
    basic_eps: float = Field(description="基本每股收益（元/股）")


class BalanceSheet(BaseModel):
    """资产负债表核心数据（单期）"""
    period: str = Field(description="报告期（YYYYMMDD）")
    total_assets: float = Field(description="资产总计（元）")
    total_liabilities: float = Field(description="负债合计（元）")
    total_equity: float = Field(description="归母股东权益（元，不含少数股东权益）")
    cash: float = Field(description="货币资金（元），衡量短期流动性")
    current_assets: float = Field(description="流动资产合计（元）")
    current_liabilities: float = Field(description="流动负债合计（元）")


class CashFlow(BaseModel):
    """现金流量表核心数据（单期）"""
    period: str = Field(description="报告期（YYYYMMDD）")
    operating_cf: float = Field(description="经营活动现金流净额（元），反映主营业务造血能力")
    investing_cf: float = Field(description="投资活动现金流净额（元），负数通常代表扩张投入")
    financing_cf: float = Field(description="筹资活动现金流净额（元）")
    free_cf: float = Field(description="自由现金流FCFF（元）= 经营现金流 − 资本性支出，衡量可分配现金")


class FinancialRatios(BaseModel):
    """关键财务比率（单期）"""
    period: str = Field(description="报告期（YYYYMMDD）")
    roe: float = Field(description="净资产收益率ROE（%），衡量股东回报能力，越高越好")
    roa: float = Field(description="总资产净利率ROA（%），衡量资产运营效率")
    gross_margin: float = Field(description="毛利率（%），反映产品定价能力与竞争优势")
    net_margin: float = Field(description="净利润率（%），反映最终盈利能力")
    current_ratio: float = Field(description="流动比率（倍），衡量短期偿债能力，通常>1为健康")
    quick_ratio: float = Field(description="速动比率（倍），排除存货后的短期偿债能力，通常>0.8为健康")
    debt_to_assets: float = Field(description="资产负债率（%），衡量财务杠杆，过高存在偿债风险")


class GrowthMetrics(BaseModel):
    """成长性指标（单期，相对于同比同期）"""
    period: str = Field(description="报告期（YYYYMMDD）")
    revenue_growth_yoy: float = Field(description="营业收入同比增长率（%），正数为增长，负数为下滑")
    profit_growth_yoy: float = Field(description="净利润同比增长率（%）")
    eps_growth_yoy: float = Field(description="基本每股收益同比增长率（%）")


class Summary(BaseModel):
    """基本面分析摘要（由大模型生成，综合多期数据趋势）"""
    overview: str = Field(description="公司财务状况的总体评述，含趋势判断（2-3句话）")
    strengths: list[str] = Field(description="主要财务优势，如高ROE、强现金流、低负债、持续增长等")
    risks: list[str] = Field(description="主要财务风险，如利润下滑、高负债、现金流恶化等")
    outlook: str = Field(description="基于多期财务趋势的展望与投资参考观点（1-2句话）")


class Fundamentals(BaseModel):
    """A股公司多期基本面数据汇总"""
    symbol: str = Field(description="股票代码（Tushare格式，如 '600519.SH'）")
    name: str = Field(description="股票名称，如'贵州茅台'")
    periods: list[str] = Field(description="包含的报告期列表，按时间倒序排列（最新在前）")
    income_statements: list[IncomeStatement] = Field(description="多期利润表，按时间倒序")
    balance_sheets: list[BalanceSheet] = Field(description="多期资产负债表，按时间倒序")
    cash_flows: list[CashFlow] = Field(description="多期现金流量表，按时间倒序")
    financial_ratios: list[FinancialRatios] = Field(description="多期关键财务比率，按时间倒序")
    growth_metrics: list[GrowthMetrics] = Field(description="多期成长性指标（同比增速），按时间倒序")
    summary: Summary = Field(description="大模型生成的多期趋势综合分析摘要")


_FUNDAMENTALS_CACHE = AsyncTTLCache(ttl_seconds=300.0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_ts_code(symbol: str) -> str:
    s = symbol.strip().lower()
    if s.startswith("sh"):
        return s[2:].upper() + ".SH"
    if s.startswith("sz"):
        return s[2:].upper() + ".SZ"
    if s.startswith("bj"):
        return s[2:].upper() + ".BJ"
    upper = symbol.strip().upper()
    if upper.endswith((".SH", ".SZ", ".BJ")):
        return upper
    if upper.startswith(("6", "9")):
        return upper + ".SH"
    if upper.startswith(("0", "3")):
        return upper + ".SZ"
    if upper.startswith(("4", "8")):
        return upper + ".BJ"
    raise ValueError(f"Cannot determine exchange for symbol: {symbol}")


def _safe_float(value, default: float = 0.0) -> float:
    try:
        v = float(value)
        return default if math.isnan(v) else v
    except (TypeError, ValueError):
        return default


def _dedup_by_date(df, date_col: str = "end_date"):
    """Drop duplicate rows for the same reporting period, keeping the first
    (which is typically the consolidated / official statement, report_type=1)."""
    return df.drop_duplicates(subset=[date_col]).reset_index(drop=True)


def _lookup_row(df, date: str, date_col: str = "end_date"):
    """Return the first row matching ``date``, or None if not found."""
    rows = df[df[date_col] == date]
    return rows.iloc[0] if not rows.empty else None


# ---------------------------------------------------------------------------
# LLM summary generation (multi-period)
# ---------------------------------------------------------------------------

async def _generate_summary(
    name: str,
    ts_code: str,
    income_list: list[IncomeStatement],
    balance_list: list[BalanceSheet],
    cf_list: list[CashFlow],
    ratio_list: list[FinancialRatios],
    growth_list: list[GrowthMetrics],
) -> Summary:
    # Build a period-keyed dict for easy lookup
    ratio_map = {r.period: r for r in ratio_list}
    growth_map = {g.period: g for g in growth_list}
    cf_map = {c.period: c for c in cf_list}

    # Compose a multi-period table for the prompt
    rows = []
    for inc in income_list:
        p = inc.period
        r = ratio_map.get(p)
        g = growth_map.get(p)
        c = cf_map.get(p)
        row: dict = {
            "报告期": p,
            "营收(亿元)": round(inc.revenue / 1e8, 2),
            "净利润(亿元)": round(inc.net_profit / 1e8, 2),
            "基本EPS(元)": inc.basic_eps,
        }
        if r:
            row.update({
                "ROE(%)": round(r.roe, 2),
                "毛利率(%)": round(r.gross_margin, 2),
                "净利润率(%)": round(r.net_margin, 2),
                "资产负债率(%)": round(r.debt_to_assets, 2),
            })
        if g:
            row.update({
                "营收同比增速(%)": round(g.revenue_growth_yoy, 2),
                "净利润同比增速(%)": round(g.profit_growth_yoy, 2),
            })
        if c:
            row.update({
                "经营现金流(亿元)": round(c.operating_cf / 1e8, 2),
                "自由现金流(亿元)": round(c.free_cf / 1e8, 2),
            })
        rows.append(row)

    prompt = (
        "你是一位专业的A股证券分析师。请根据以下多期财务数据，对该公司进行简明扼要的基本面趋势分析。\n\n"
        f"公司：{name}（{ts_code}）\n\n"
        f"多期财务数据（最新在前，共{len(rows)}期）：\n"
        f"{json.dumps(rows, ensure_ascii=False, indent=2)}\n\n"
        "请关注各指标的趋势变化（改善/恶化/稳定），并严格按以下JSON格式返回分析结果"
        "（不要包含任何JSON以外的内容）：\n"
        "{\n"
        '  "overview": "2-3句话，含趋势判断的财务总体概述",\n'
        '  "strengths": ["优势1", "优势2", "优势3"],\n'
        '  "risks": ["风险1", "风险2"],\n'
        '  "outlook": "1-2句话，基于趋势的展望"\n'
        "}"
    )

    client = AsyncOpenAI(
        api_key=settings.llm.api_key,
        base_url=settings.llm.base_url,
    )
    response = await client.chat.completions.create(
        model=settings.llm.model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )

    raw = (response.choices[0].message.content or "").strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0].strip()

    parsed = json.loads(raw)
    return Summary(
        overview=parsed.get("overview", ""),
        strengths=parsed.get("strengths", []),
        risks=parsed.get("risks", []),
        outlook=parsed.get("outlook", ""),
    )


# ---------------------------------------------------------------------------
# Main tool function
# ---------------------------------------------------------------------------

async def get_fundamentals(symbol: str, periods: int = 4) -> Fundamentals:
    """获取指定A股公司的多期基本面财务数据，并生成AI趋势分析摘要。

    当用户需要分析某只股票的财务状况、历史趋势时调用，包括：多期营业收入、
    净利润、现金流、ROE/ROA、毛利率、偿债能力、同比增速等，以及综合财务
    健康评估与趋势判断。不适用于查询实时价格或行情数据。

    Args:
        symbol:  股票代码，支持多种格式，例如：
                 "600519"、"600519.SH"、"sh600519"
        periods: 返回的报告期数量（默认4期，即约1年的季报数据；
                 设为8可获取约2年数据，最多不超过20期）

    Returns:
        Fundamentals，包含：
        - periods：报告期列表（最新在前）
        - income_statements：多期利润表（营收、净利润、EPS等）
        - balance_sheets：多期资产负债表（总资产、负债、货币资金等）
        - cash_flows：多期现金流量表（经营/投资/筹资/自由现金流）
        - financial_ratios：多期关键比率（ROE、ROA、毛利率、负债率等）
        - growth_metrics：多期成长性（营收/利润/EPS同比增速）
        - summary：AI生成的多期趋势综合分析（优势、风险、展望）
    """
    periods = max(1, min(periods, 20))
    ts_code = _normalize_ts_code(symbol)

    async def _compute() -> Fundamentals:
        # Query far enough back to cover requested periods (each ~91 days)
        start_date = (
            datetime.date.today() - datetime.timedelta(days=periods * 110 + 180)
        ).strftime("%Y%m%d")

        with TuShareHelper() as helper:
            income_df = helper.income(
                ts_code=ts_code,
                start_date=start_date,
                fields="ts_code,end_date,total_revenue,operate_profit,n_income,ebitda,basic_eps",
            ).data
            balance_df = helper.balancesheet(
                ts_code=ts_code,
                start_date=start_date,
                fields="ts_code,end_date,total_assets,total_liab,"
                       "total_hldr_eqy_exc_min_int,money_cap,"
                       "total_cur_assets,total_cur_liab",
            ).data
            cashflow_df = helper.cashflow(
                ts_code=ts_code,
                start_date=start_date,
                fields="ts_code,end_date,n_cashflow_act,n_cashflow_inv_act,"
                       "n_cash_flows_fnc_act,c_pay_acq_const_fiolta",
            ).data
            fina_df = helper.fina_indicator(
                ts_code=ts_code,
                start_date=start_date,
                fields="ts_code,end_date,roe,roa,grossprofit_margin,netprofit_margin,"
                       "current_ratio,quick_ratio,debt_to_assets,"
                       "or_yoy,netprofit_yoy,basic_eps_yoy,fcff",
            ).data
            stock_basic_df = helper.stock_basic(
                ts_code=ts_code, fields="ts_code,name"
            ).data

        name = stock_basic_df.iloc[0]["name"] if not stock_basic_df.empty else ""

        income_df = _dedup_by_date(income_df)
        balance_df = _dedup_by_date(balance_df)
        cashflow_df = _dedup_by_date(cashflow_df)
        fina_df = _dedup_by_date(fina_df)

        ref_dates: list[str] = income_df.head(periods)["end_date"].tolist()

        income_list: list[IncomeStatement] = []
        balance_list: list[BalanceSheet] = []
        cf_list: list[CashFlow] = []
        ratio_list: list[FinancialRatios] = []
        growth_list: list[GrowthMetrics] = []

        for date in ref_dates:
            inc = _lookup_row(income_df, date)
            bal = _lookup_row(balance_df, date)
            cf = _lookup_row(cashflow_df, date)
            fin = _lookup_row(fina_df, date)

            if inc is not None:
                income_list.append(IncomeStatement(
                    period=str(inc["end_date"]),
                    revenue=_safe_float(inc["total_revenue"]),
                    operating_profit=_safe_float(inc["operate_profit"]),
                    net_profit=_safe_float(inc["n_income"]),
                    ebitda=_safe_float(inc["ebitda"]),
                    basic_eps=_safe_float(inc["basic_eps"]),
                ))

            if bal is not None:
                balance_list.append(BalanceSheet(
                    period=str(bal["end_date"]),
                    total_assets=_safe_float(bal["total_assets"]),
                    total_liabilities=_safe_float(bal["total_liab"]),
                    total_equity=_safe_float(bal["total_hldr_eqy_exc_min_int"]),
                    cash=_safe_float(bal["money_cap"]),
                    current_assets=_safe_float(bal["total_cur_assets"]),
                    current_liabilities=_safe_float(bal["total_cur_liab"]),
                ))

            if cf is not None:
                operating_cf = _safe_float(cf["n_cashflow_act"])
                capex = _safe_float(cf["c_pay_acq_const_fiolta"])
                fcff = _safe_float(fin["fcff"]) if fin is not None else 0.0
                free_cf = fcff if fcff != 0.0 else (operating_cf - capex)
                cf_list.append(CashFlow(
                    period=str(cf["end_date"]),
                    operating_cf=operating_cf,
                    investing_cf=_safe_float(cf["n_cashflow_inv_act"]),
                    financing_cf=_safe_float(cf["n_cash_flows_fnc_act"]),
                    free_cf=free_cf,
                ))

            if fin is not None:
                ratio_list.append(FinancialRatios(
                    period=str(fin["end_date"]),
                    roe=_safe_float(fin["roe"]),
                    roa=_safe_float(fin["roa"]),
                    gross_margin=_safe_float(fin["grossprofit_margin"]),
                    net_margin=_safe_float(fin["netprofit_margin"]),
                    current_ratio=_safe_float(fin["current_ratio"]),
                    quick_ratio=_safe_float(fin["quick_ratio"]),
                    debt_to_assets=_safe_float(fin["debt_to_assets"]),
                ))
                growth_list.append(GrowthMetrics(
                    period=str(fin["end_date"]),
                    revenue_growth_yoy=_safe_float(fin["or_yoy"]),
                    profit_growth_yoy=_safe_float(fin["netprofit_yoy"]),
                    eps_growth_yoy=_safe_float(fin["basic_eps_yoy"]),
                ))

        summary = await _generate_summary(
            name=name,
            ts_code=ts_code,
            income_list=income_list,
            balance_list=balance_list,
            cf_list=cf_list,
            ratio_list=ratio_list,
            growth_list=growth_list,
        )

        return Fundamentals(
            symbol=ts_code,
            name=name,
            periods=ref_dates,
            income_statements=income_list,
            balance_sheets=balance_list,
            cash_flows=cf_list,
            financial_ratios=ratio_list,
            growth_metrics=growth_list,
            summary=summary,
        )

    return await _FUNDAMENTALS_CACHE.get_or_compute(
        ("fundamentals", ts_code, periods),
        _compute,
    )
