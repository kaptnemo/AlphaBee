import json

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from alphabee.collectors.tushare.helper import TuShareHelper
from alphabee.config import settings


class IncomeStatement(BaseModel):
    """利润表核心数据"""
    period: str = Field(description="报告期（YYYYMMDD），如 '20241231' 表示2024年年报")
    revenue: float = Field(description="营业总收入（元）")
    operating_profit: float = Field(description="营业利润（元）")
    net_profit: float = Field(description="归母净利润（元）")
    ebitda: float = Field(description="息税折旧摊销前利润EBITDA（元），衡量核心盈利能力")
    basic_eps: float = Field(description="基本每股收益（元/股）")


class BalanceSheet(BaseModel):
    """资产负债表核心数据"""
    period: str = Field(description="报告期（YYYYMMDD）")
    total_assets: float = Field(description="资产总计（元）")
    total_liabilities: float = Field(description="负债合计（元）")
    total_equity: float = Field(description="归母股东权益（元，不含少数股东权益）")
    cash: float = Field(description="货币资金（元），衡量短期流动性")
    current_assets: float = Field(description="流动资产合计（元）")
    current_liabilities: float = Field(description="流动负债合计（元）")


class CashFlow(BaseModel):
    """现金流量表核心数据"""
    period: str = Field(description="报告期（YYYYMMDD）")
    operating_cf: float = Field(description="经营活动现金流净额（元），反映主营业务造血能力")
    investing_cf: float = Field(description="投资活动现金流净额（元），负数通常代表扩张投入")
    financing_cf: float = Field(description="筹资活动现金流净额（元）")
    free_cf: float = Field(description="自由现金流FCFF（元）= 经营现金流 − 资本性支出，衡量可分配现金")


class FinancialRatios(BaseModel):
    """关键财务比率"""
    period: str = Field(description="报告期（YYYYMMDD）")
    roe: float = Field(description="净资产收益率ROE（%），衡量股东回报能力，越高越好")
    roa: float = Field(description="总资产净利率ROA（%），衡量资产运营效率")
    gross_margin: float = Field(description="毛利率（%），反映产品定价能力与竞争优势")
    net_margin: float = Field(description="净利润率（%），反映最终盈利能力")
    current_ratio: float = Field(description="流动比率（倍），衡量短期偿债能力，通常>1为健康")
    quick_ratio: float = Field(description="速动比率（倍），排除存货后的短期偿债能力，通常>0.8为健康")
    debt_to_assets: float = Field(description="资产负债率（%），衡量财务杠杆，过高存在偿债风险")


class GrowthMetrics(BaseModel):
    """成长性指标"""
    period: str = Field(description="报告期（YYYYMMDD）")
    revenue_growth_yoy: float = Field(description="营业收入同比增长率（%），正数为增长，负数为下滑")
    profit_growth_yoy: float = Field(description="净利润同比增长率（%）")
    eps_growth_yoy: float = Field(description="基本每股收益同比增长率（%）")


class Summary(BaseModel):
    """基本面分析摘要（由大模型生成）"""
    overview: str = Field(description="公司财务状况的总体评述（2-3句话）")
    strengths: list[str] = Field(description="主要财务优势，如高ROE、强现金流、低负债等")
    risks: list[str] = Field(description="主要财务风险，如利润下滑、高负债、现金流不足等")
    outlook: str = Field(description="基于财务数据的展望与投资参考观点（1-2句话）")


class Fundamentals(BaseModel):
    """A股公司基本面数据汇总"""
    symbol: str = Field(description="股票代码（Tushare格式，如 '600519.SH'）")
    name: str = Field(description="股票名称，如'贵州茅台'")
    income_statement: IncomeStatement = Field(description="利润表核心数据")
    balance_sheet: BalanceSheet = Field(description="资产负债表核心数据")
    cash_flow: CashFlow = Field(description="现金流量表核心数据")
    financial_ratios: FinancialRatios = Field(description="关键财务比率（ROE/ROA/毛利率/负债率等）")
    growth_metrics: GrowthMetrics = Field(description="成长性指标（营收/利润/EPS同比增速）")
    summary: Summary = Field(description="大模型生成的基本面分析摘要")


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
    import math
    try:
        v = float(value)
        return default if math.isnan(v) else v
    except (TypeError, ValueError):
        return default


async def _generate_summary(
    name: str,
    ts_code: str,
    income: IncomeStatement,
    balance: BalanceSheet,
    cash: CashFlow,
    ratios: FinancialRatios,
    growth: GrowthMetrics,
) -> Summary:
    data_snapshot = {
        "股票": f"{name}（{ts_code}）",
        "报告期": income.period,
        "营业总收入(亿元)": round(income.revenue / 1e8, 2),
        "营业利润(亿元)": round(income.operating_profit / 1e8, 2),
        "净利润(亿元)": round(income.net_profit / 1e8, 2),
        "EBITDA(亿元)": round(income.ebitda / 1e8, 2),
        "基本EPS(元)": income.basic_eps,
        "资产总计(亿元)": round(balance.total_assets / 1e8, 2),
        "负债合计(亿元)": round(balance.total_liabilities / 1e8, 2),
        "股东权益(亿元)": round(balance.total_equity / 1e8, 2),
        "货币资金(亿元)": round(balance.cash / 1e8, 2),
        "经营现金流(亿元)": round(cash.operating_cf / 1e8, 2),
        "自由现金流(亿元)": round(cash.free_cf / 1e8, 2),
        "ROE(%)": ratios.roe,
        "ROA(%)": ratios.roa,
        "毛利率(%)": ratios.gross_margin,
        "净利润率(%)": ratios.net_margin,
        "流动比率": ratios.current_ratio,
        "速动比率": ratios.quick_ratio,
        "资产负债率(%)": ratios.debt_to_assets,
        "营收同比增长(%)": growth.revenue_growth_yoy,
        "净利润同比增长(%)": growth.profit_growth_yoy,
        "EPS同比增长(%)": growth.eps_growth_yoy,
    }

    prompt = (
        "你是一位专业的A股证券分析师。请根据以下财务数据，对该公司进行简明扼要的基本面分析。\n\n"
        f"财务数据：\n{json.dumps(data_snapshot, ensure_ascii=False, indent=2)}\n\n"
        "请严格按照以下JSON格式返回分析结果（不要包含任何JSON以外的内容）：\n"
        "{\n"
        '  "overview": "2-3句话的财务总体概述",\n'
        '  "strengths": ["优势1", "优势2", "优势3"],\n'
        '  "risks": ["风险1", "风险2"],\n'
        '  "outlook": "1-2句话的财务展望"\n'
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


async def get_fundamentals(symbol: str) -> Fundamentals:
    """获取指定A股公司的基本面财务数据，并生成AI分析摘要。

    当用户需要分析某只股票的财务状况时调用，包括：营业收入、净利润、
    现金流、ROE/ROA、毛利率、偿债能力、成长性（同比增速）等基本面指标，
    以及综合财务健康评估。不适用于查询实时价格或行情数据。

    Args:
        symbol: 股票代码，支持多种格式，例如：
                "600519"（纯代码）、"600519.SH"（带交易所）、"sh600519"（带前缀）

    Returns:
        Fundamentals，包含：
        - income_statement：利润表（营收、净利润、EPS等）
        - balance_sheet：资产负债表（总资产、负债、货币资金等）
        - cash_flow：现金流量表（经营/投资/筹资/自由现金流）
        - financial_ratios：关键比率（ROE、ROA、毛利率、负债率等）
        - growth_metrics：成长性（营收/利润/EPS同比增速）
        - summary：AI生成的优势、风险与投资展望
    """
    ts_code = _normalize_ts_code(symbol)

    with TuShareHelper() as helper:
        income_df = helper.income(
            ts_code=ts_code,
            fields="ts_code,end_date,total_revenue,operate_profit,n_income,ebitda,basic_eps",
        ).data
        balance_df = helper.balancesheet(
            ts_code=ts_code,
            fields="ts_code,end_date,total_assets,total_liab,"
                   "total_hldr_eqy_exc_min_int,money_cap,"
                   "total_cur_assets,total_cur_liab",
        ).data
        cashflow_df = helper.cashflow(
            ts_code=ts_code,
            fields="ts_code,end_date,n_cashflow_act,n_cashflow_inv_act,"
                   "n_cash_flows_fnc_act,c_pay_acq_const_fiolta",
        ).data
        fina_df = helper.fina_indicator(
            ts_code=ts_code,
            fields="ts_code,end_date,roe,roa,grossprofit_margin,netprofit_margin,"
                   "current_ratio,quick_ratio,debt_to_assets,"
                   "or_yoy,netprofit_yoy,basic_eps_yoy,fcff",
        ).data
        stock_basic_df = helper.stock_basic(
            ts_code=ts_code, fields="ts_code,name"
        ).data

    name = stock_basic_df.iloc[0]["name"] if not stock_basic_df.empty else ""

    inc = income_df.iloc[0]
    bal = balance_df.iloc[0]
    cf = cashflow_df.iloc[0]
    fin = fina_df.iloc[0]

    income = IncomeStatement(
        period=str(inc["end_date"]),
        revenue=_safe_float(inc["total_revenue"]),
        operating_profit=_safe_float(inc["operate_profit"]),
        net_profit=_safe_float(inc["n_income"]),
        ebitda=_safe_float(inc["ebitda"]),
        basic_eps=_safe_float(inc["basic_eps"]),
    )
    balance = BalanceSheet(
        period=str(bal["end_date"]),
        total_assets=_safe_float(bal["total_assets"]),
        total_liabilities=_safe_float(bal["total_liab"]),
        total_equity=_safe_float(bal["total_hldr_eqy_exc_min_int"]),
        cash=_safe_float(bal["money_cap"]),
        current_assets=_safe_float(bal["total_cur_assets"]),
        current_liabilities=_safe_float(bal["total_cur_liab"]),
    )

    operating_cf = _safe_float(cf["n_cashflow_act"])
    capex = _safe_float(cf["c_pay_acq_const_fiolta"])
    fcff = _safe_float(fin["fcff"])
    free_cf = fcff if fcff != 0.0 else (operating_cf - capex)

    cash_flow = CashFlow(
        period=str(cf["end_date"]),
        operating_cf=operating_cf,
        investing_cf=_safe_float(cf["n_cashflow_inv_act"]),
        financing_cf=_safe_float(cf["n_cash_flows_fnc_act"]),
        free_cf=free_cf,
    )
    ratios = FinancialRatios(
        period=str(fin["end_date"]),
        roe=_safe_float(fin["roe"]),
        roa=_safe_float(fin["roa"]),
        gross_margin=_safe_float(fin["grossprofit_margin"]),
        net_margin=_safe_float(fin["netprofit_margin"]),
        current_ratio=_safe_float(fin["current_ratio"]),
        quick_ratio=_safe_float(fin["quick_ratio"]),
        debt_to_assets=_safe_float(fin["debt_to_assets"]),
    )
    growth = GrowthMetrics(
        period=str(fin["end_date"]),
        revenue_growth_yoy=_safe_float(fin["or_yoy"]),
        profit_growth_yoy=_safe_float(fin["netprofit_yoy"]),
        eps_growth_yoy=_safe_float(fin["basic_eps_yoy"]),
    )

    summary = await _generate_summary(
        name=name,
        ts_code=ts_code,
        income=income,
        balance=balance,
        cash=cash_flow,
        ratios=ratios,
        growth=growth,
    )

    return Fundamentals(
        symbol=ts_code,
        name=name,
        income_statement=income,
        balance_sheet=balance,
        cash_flow=cash_flow,
        financial_ratios=ratios,
        growth_metrics=growth,
        summary=summary,
    )
