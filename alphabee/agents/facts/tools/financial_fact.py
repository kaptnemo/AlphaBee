"""FinancialFact tool — 多期财务报表与核心财务指标。"""

import datetime
from typing import Any

from alphabee.collectors.tushare.helper import TuShareHelper
from alphabee.tools.cache import SyncTTLCache
from alphabee.agents.facts.tools._utils import normalize_ts_code, safe_float

_CACHE = SyncTTLCache(ttl_seconds=600.0)


def _dedup(df, col="period"):
    return df.drop_duplicates(subset=[col]).reset_index(drop=True)


def get_financial_fact(symbol: str, periods: int = 8) -> dict[str, Any]:
    """获取A股公司多期财务报表数据，包括利润表、资产负债表、现金流量表和核心财务比率。

    适用场景：
    - 分析公司营收、净利润的历史趋势
    - 评估毛利率、净利率、ROE、ROA等盈利能力
    - 查看资产负债率、流动比率等偿债能力指标
    - 分析经营现金流、自由现金流质量
    - 查看营收/净利润同比增速

    Args:
        symbol:  股票代码，支持多种格式，如 "600519"、"600519.SH"
        periods: 返回报告期数量（默认8期，约2年季报；最多20期）

    Returns:
        包含多期财务数据的字典，所有字段使用 AlphaBee 标准命名。
    """
    periods = max(1, min(periods, 20))
    ts_code = normalize_ts_code(symbol)

    def _compute() -> dict[str, Any]:
        start = (
            datetime.date.today() - datetime.timedelta(days=periods * 110 + 180)
        ).strftime("%Y%m%d")

        with TuShareHelper() as helper:
            income_df = helper.income(
                ts_code=ts_code,
                start_date=start,
                fields="ts_code,end_date,total_revenue,operate_profit,n_income,ebitda,basic_eps",
            ).data
            balance_df = helper.balancesheet(
                ts_code=ts_code,
                start_date=start,
                fields="ts_code,end_date,total_assets,total_liab,"
                       "total_hldr_eqy_exc_min_int,money_cap,"
                       "total_cur_assets,total_cur_liab,accounts_receiv",
            ).data
            cashflow_df = helper.cashflow(
                ts_code=ts_code,
                start_date=start,
                fields="ts_code,end_date,n_cashflow_act,n_cashflow_inv_act,"
                       "n_cash_flows_fnc_act,c_pay_acq_const_fiolta",
            ).data
            fina_df = helper.fina_indicator(
                ts_code=ts_code,
                start_date=start,
                fields="ts_code,end_date,roe,roa,grossprofit_margin,netprofit_margin,"
                       "current_ratio,quick_ratio,debt_to_assets,"
                       "or_yoy,netprofit_yoy,basic_eps_yoy,fcff",
            ).data

        income_df = _dedup(income_df)
        balance_df = _dedup(balance_df)
        cashflow_df = _dedup(cashflow_df)
        fina_df = _dedup(fina_df)

        ref_dates = income_df.head(periods)["period"].tolist()

        return {
            "stock_code": ts_code,
            "ref_dates": ref_dates,
            "income": income_df.to_dict(orient="records"),
            "balance": balance_df.to_dict(orient="records"),
            "cashflow": cashflow_df.to_dict(orient="records"),
            "fina": fina_df.to_dict(orient="records"),
        }

    return _CACHE.get_or_compute(("financial_fact", ts_code, periods), _compute)


def render(data: dict[str, Any]) -> str:
    """将财务事实数据渲染为Markdown格式的文本。"""
    stock_code = data.get("stock_code", "")
    ref_dates = data.get("ref_dates", [])

    income_map = {r["period"]: r for r in data.get("income", [])}
    balance_map = {r["period"]: r for r in data.get("balance", [])}
    cashflow_map = {r["period"]: r for r in data.get("cashflow", [])}
    fina_map = {r["period"]: r for r in data.get("fina", [])}

    lines = [f"## {stock_code} 财务事实数据（最近 {len(ref_dates)} 期）\n"]

    # ── 利润表 ────────────────────────────────────────────────────────
    lines += [
        "### 利润表（单位：亿元）",
        "| 报告期 | 营业总收入 | 营业利润 | 归母净利润 | EBITDA | 基本EPS(元) |",
        "|--------|-----------|---------|-----------|--------|------------|",
    ]
    for d in ref_dates:
        r = income_map.get(d)
        if r is not None:
            lines.append(
                f"| {d} "
                f"| {safe_float(r['revenue'])/1e8:.2f} "
                f"| {safe_float(r['operating_profit'])/1e8:.2f} "
                f"| {safe_float(r['net_profit'])/1e8:.2f} "
                f"| {safe_float(r['ebitda'])/1e8:.2f} "
                f"| {safe_float(r['basic_eps']):.4f} |"
            )
    lines.append("")

    # ── 资产负债表 ─────────────────────────────────────────────────────
    lines += [
        "### 资产负债表（单位：亿元）",
        "| 报告期 | 总资产 | 总负债 | 归母净资产 | 货币资金 | 流动资产 | 流动负债 |",
        "|--------|--------|--------|-----------|---------|---------|---------|",
    ]
    for d in ref_dates:
        r = balance_map.get(d)
        if r is not None:
            lines.append(
                f"| {d} "
                f"| {safe_float(r['total_assets'])/1e8:.2f} "
                f"| {safe_float(r['total_liabilities'])/1e8:.2f} "
                f"| {safe_float(r['shareholders_equity'])/1e8:.2f} "
                f"| {safe_float(r['cash'])/1e8:.2f} "
                f"| {safe_float(r['current_assets'])/1e8:.2f} "
                f"| {safe_float(r['current_liabilities'])/1e8:.2f} |"
            )
    lines.append("")

    # ── 现金流量表 ─────────────────────────────────────────────────────
    lines += [
        "### 现金流量表（单位：亿元）",
        "| 报告期 | 经营活动净现金流 | 投资活动净现金流 | 筹资活动净现金流 | 自由现金流 |",
        "|--------|----------------|----------------|----------------|----------|",
    ]
    for d in ref_dates:
        r = cashflow_map.get(d)
        fin = fina_map.get(d)
        if r is not None:
            op_cf = safe_float(r["operating_cashflow"])
            capex = safe_float(r["capex"])
            fcff = safe_float(fin["free_cashflow"]) if fin is not None else 0.0
            free_cf = fcff if fcff != 0.0 else (op_cf - capex)
            lines.append(
                f"| {d} "
                f"| {op_cf/1e8:.2f} "
                f"| {safe_float(r['investing_cashflow'])/1e8:.2f} "
                f"| {safe_float(r['financing_cashflow'])/1e8:.2f} "
                f"| {free_cf/1e8:.2f} |"
            )
    lines.append("")

    # ── 财务比率 ───────────────────────────────────────────────────────
    lines += [
        "### 核心财务比率",
        "| 报告期 | ROE(%) | ROA(%) | 毛利率(%) | 净利率(%) | 流动比率 | 速动比率 | 资产负债率(%) |",
        "|--------|--------|--------|---------|---------|--------|--------|-------------|",
    ]
    for d in ref_dates:
        r = fina_map.get(d)
        if r is not None:
            lines.append(
                f"| {d} "
                f"| {safe_float(r['roe']):.2f} "
                f"| {safe_float(r['roa']):.2f} "
                f"| {safe_float(r['gross_margin']):.2f} "
                f"| {safe_float(r['net_margin']):.2f} "
                f"| {safe_float(r['current_ratio']):.2f} "
                f"| {safe_float(r['quick_ratio']):.2f} "
                f"| {safe_float(r['debt_to_assets']):.2f} |"
            )
    lines.append("")

    # ── 成长指标 ───────────────────────────────────────────────────────
    lines += [
        "### 同比成长性",
        "| 报告期 | 营收同比增速(%) | 净利润同比增速(%) | EPS同比增速(%) |",
        "|--------|--------------|----------------|--------------|",
    ]
    for d in ref_dates:
        r = fina_map.get(d)
        if r is not None:
            lines.append(
                f"| {d} "
                f"| {safe_float(r['revenue_yoy']):.2f} "
                f"| {safe_float(r['net_profit_yoy']):.2f} "
                f"| {safe_float(r['eps_growth_yoy']):.2f} |"
            )
    lines.append("")

    return "\n".join(lines)


