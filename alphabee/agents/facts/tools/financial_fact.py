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
                fields="ts_code,end_date,total_revenue,operate_profit,"
                       "n_income,ebitda,basic_eps,interest_expense,"
                       "income_tax,total_profit",
            ).data
            balance_df = helper.balancesheet(
                ts_code=ts_code,
                start_date=start,
                fields="ts_code,end_date,total_assets,total_liab,"
                       "total_hldr_eqy_exc_min_int,money_cap,"
                       "total_cur_assets,total_cur_liab,accounts_receiv,"
                       "inventories,goodwill",
            ).data
            cashflow_df = helper.cashflow(
                ts_code=ts_code,
                start_date=start,
                fields="ts_code,end_date,n_cashflow_act,n_cashflow_inv_act,"
                       "n_cash_flows_fnc_act,c_pay_acq_const_fiolta,"
                       "c_pay_dist_dpcp_int_pvd,c_paid_to_for_empl",
            ).data
            fina_df = helper.fina_indicator(
                ts_code=ts_code,
                start_date=start,
                fields="ts_code,end_date,roe,roa,grossprofit_margin,netprofit_margin,"
                       "current_ratio,quick_ratio,debt_to_assets,"
                       "or_yoy,netprofit_yoy,basic_eps_yoy,fcff,"
                       "interestdebt,daa,fixed_assets,"
                       "saleexp_to_gr,adminexp_of_gr,finaexp_of_gr,rd_exp",
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


# ═══════════════════════════════════════════════════════════════════════════
# Canonical 字段提取层
# 从 get_financial_fact() 返回的多期嵌套结构中提取单期平面值，
# 供 DerivedFacts Engine / Signal Engine 使用。
# ═══════════════════════════════════════════════════════════════════════════

# canonical 字段 → (子表名, 列名, 期偏移, 转换函数)
# offset=0 → 最新一期, offset=1 → 上一期, offset=4 → 去年同期（季报）
_FIELD_EXTRACTORS: dict[str, tuple[str, str, int]] = {
    # ── 利润表（income）──────────────────────────────────────
    "revenue":           ("income",   "revenue",           0),
    "net_profit":        ("income",   "net_profit",        0),
    "operating_profit":  ("income",   "operating_profit",  0),
    "ebitda":            ("income",   "ebitda",            0),
    "interest_expense":  ("income",   "interest_expense",  0),
    "income_tax_expense": ("income",  "income_tax_expense", 0),
    "total_profit":      ("income",   "total_profit",      0),
    # ── 资产负债表（balance）─────────────────────────────────
    "total_assets":        ("balance", "total_assets",        0),
    "total_liabilities":   ("balance", "total_liabilities",   0),
    "shareholders_equity": ("balance", "shareholders_equity", 0),
    "current_assets":      ("balance", "current_assets",      0),
    "current_liabilities": ("balance", "current_liabilities", 0),
    "accounts_receivable":      ("balance", "accounts_receivable", 0),
    "accounts_receivable_prev": ("balance", "accounts_receivable", 1),
    "goodwill":            ("balance", "goodwill",            0),
    "inventory":           ("balance", "inventory",           0),
    "inventory_prev":      ("balance", "inventory",           1),
    # ── 现金流量表（cashflow）────────────────────────────────
    "operating_cashflow": ("cashflow", "operating_cashflow", 0),
    "capex":              ("cashflow", "capex",              0),
    "dividends_paid":     ("cashflow", "dividends_paid",     0),
    "salary_paid":        ("cashflow", "salary_paid",        0),
    # ── 财务比率（fina_indicator）────────────────────────────
    "gross_margin_current": ("fina", "gross_margin", 0),
    "gross_margin_prev":    ("fina", "gross_margin", 1),
    "roe":           ("fina", "roe",            0),
    "revenue_yoy":   ("fina", "revenue_yoy",    0),
    "net_profit_yoy": ("fina", "net_profit_yoy", 0),
    "interest_bearing_debt":      ("fina", "interest_bearing_debt",      0),
    "depreciation_amortization":  ("fina", "depreciation_amortization",  0),
    "fixed_assets_total":         ("fina", "fixed_assets_total",         0),
    "sales_expense_ratio":        ("fina", "sales_expense_ratio",        0),
    "admin_expense_ratio":        ("fina", "admin_expense_ratio",        0),
    "finance_expense_ratio":      ("fina", "finance_expense_ratio",      0),
    "rd_expense":                 ("fina", "rd_expense",                 0),
}


def _extract_from_table(
    data: dict,
    table: str,
    field: str,
    offset: int = 0,
) -> float | None:
    """从指定子表的指定期偏移提取单个字段值。"""
    records = data.get(table, [])
    if len(records) <= offset:
        return None
    return safe_float(records[offset].get(field))


def extract_financial_facts(
    data: dict,
    fields: list[str] | None = None,
) -> dict[str, float]:
    """从 get_financial_fact() 结果中批量提取 canonical 字段值。

    覆盖 _FIELD_EXTRACTORS 中声明的大部分字段，以及手动计算的复合字段
    （avg_shareholders_equity、ebit、inventory_yoy）。

    Args:
        data: get_financial_fact() 的返回结果。
        fields: 需要提取的字段列表；为 None 时提取全部可用字段。

    Returns:
        ``{canonical_field_name: float_value}``，缺失字段不出现在结果中。
    """
    target = fields if fields is not None else list(_FIELD_EXTRACTORS)

    result: dict[str, float] = {}

    for field in target:
        # ── 复合计算字段 ──────────────────────────────────
        if field == "avg_shareholders_equity":
            val = _extract_avg_shareholders_equity(data)
            if val is not None:
                result[field] = val
            continue
        if field == "ebit":
            val = _extract_ebit(data)
            if val is not None:
                result[field] = val
            continue
        if field == "inventory_yoy":
            val = _extract_inventory_yoy(data)
            if val is not None:
                result[field] = val
            continue

        # ── 映射表提取 ────────────────────────────────────
        spec = _FIELD_EXTRACTORS.get(field)
        if spec is None:
            continue
        table, col, offset = spec
        val = _extract_from_table(data, table, col, offset)
        if val is not None:
            result[field] = val

    return result


# ── 复合计算字段（跨行 / 跨表）────────────────────────────────


def _extract_avg_shareholders_equity(data: dict) -> float | None:
    """归母净资产近两期均值，用于 ROE 计算。"""
    cur = _extract_from_table(data, "balance", "shareholders_equity", 0)
    prev = _extract_from_table(data, "balance", "shareholders_equity", 1)
    if cur is not None and prev is not None:
        return (cur + prev) / 2.0
    return None


def _extract_ebit(data: dict) -> float | None:
    """EBIT ≈ 营业利润 + 利息费用（简化近似）。"""
    op = _extract_from_table(data, "income", "operating_profit", 0)
    ie = _extract_from_table(data, "income", "interest_expense", 0)
    if op is not None and ie is not None:
        return op + ie
    return None


def _extract_inventory_yoy(data: dict) -> float | None:
    """存货同比增速（%），取最近一期与上一期对比。

    优先使用同比口径（offset=4，即与去年同期比）；若数据不足则回退
    到环比（offset=1）。
    """
    records = data.get("balance", [])
    if len(records) < 2:
        return None
    cur = safe_float(records[0].get("inventory"))
    # 优先取去年同期（季报 offset=4），否则取上一期
    if len(records) >= 5:
        prev = safe_float(records[4].get("inventory"))
    else:
        prev = safe_float(records[1].get("inventory"))
    if cur is not None and prev is not None and prev != 0:
        return (cur - prev) / prev * 100.0
    return None


def get_financial_facts_model(symbol: str, periods: int = 24) -> "FinancialFacts":
    """获取 A 股多期财务数据并返回 FinancialFacts Pydantic 模型。

    封装 get_financial_fact() 的返回值，将多期字典数据映射到类型化的
    FinancialFacts / FinancialSnapshot 模型，供 DerivedFacts 引擎直接消费。

    Args:
        symbol:  股票代码，支持多种格式，如 "600519"、"600519.SH"
        periods: 返回报告期数量（默认24期；最多24期）

    Returns:
        FinancialFacts 模型，含 snapshots 列表和跨期 computed_field。
    """
    from alphabee.agents.facts.models import FinancialFacts, FinancialSnapshot

    data = get_financial_fact(symbol, periods)
    ts_code = data["stock_code"]
    ref_dates = data.get("ref_dates", [])

    income_map = {r["period"]: r for r in data.get("income", [])}
    balance_map = {r["period"]: r for r in data.get("balance", [])}
    cashflow_map = {r["period"]: r for r in data.get("cashflow", [])}
    fina_map = {r["period"]: r for r in data.get("fina", [])}

    snapshots: list[FinancialSnapshot] = []
    for period in ref_dates:
        inc = income_map.get(period, {})
        bal = balance_map.get(period, {})
        cf = cashflow_map.get(period, {})
        fi = fina_map.get(period, {})
        snapshots.append(FinancialSnapshot(
            period=period,
            # 利润表
            revenue=safe_float(inc.get("revenue")),
            operating_profit=safe_float(inc.get("operating_profit")),
            net_profit=safe_float(inc.get("net_profit")),
            ebitda=safe_float(inc.get("ebitda")),
            interest_expense=safe_float(inc.get("interest_expense")),
            basic_eps=safe_float(inc.get("basic_eps")),
            income_tax_expense=safe_float(inc.get("income_tax_expense")),
            total_profit=safe_float(inc.get("total_profit")),
            # 资产负债表
            total_assets=safe_float(bal.get("total_assets")),
            total_liabilities=safe_float(bal.get("total_liabilities")),
            shareholders_equity=safe_float(bal.get("shareholders_equity")),
            cash=safe_float(bal.get("cash")),
            current_assets=safe_float(bal.get("current_assets")),
            current_liabilities=safe_float(bal.get("current_liabilities")),
            accounts_receivable=safe_float(bal.get("accounts_receivable")),
            inventory=safe_float(bal.get("inventory")),
            goodwill=safe_float(bal.get("goodwill")),
            # 现金流量表
            operating_cashflow=safe_float(cf.get("operating_cashflow")),
            investing_cashflow=safe_float(cf.get("investing_cashflow")),
            financing_cashflow=safe_float(cf.get("financing_cashflow")),
            capex=safe_float(cf.get("capex")),
            dividends_paid=safe_float(cf.get("dividends_paid")),
            salary_paid=safe_float(cf.get("salary_paid")),
            # 财务比率
            roe=safe_float(fi.get("roe")),
            roa=safe_float(fi.get("roa")),
            gross_margin=safe_float(fi.get("gross_margin")),
            net_margin=safe_float(fi.get("net_margin")),
            current_ratio=safe_float(fi.get("current_ratio")),
            quick_ratio=safe_float(fi.get("quick_ratio")),
            debt_to_assets=safe_float(fi.get("debt_to_assets")),
            revenue_yoy=safe_float(fi.get("revenue_yoy")),
            net_profit_yoy=safe_float(fi.get("net_profit_yoy")),
            eps_growth_yoy=safe_float(fi.get("eps_growth_yoy")),
            free_cashflow=safe_float(fi.get("free_cashflow")),
            interest_bearing_debt=safe_float(fi.get("interest_bearing_debt")),
            depreciation_amortization=safe_float(fi.get("depreciation_amortization")),
            fixed_assets_total=safe_float(fi.get("fixed_assets_total")),
            sales_expense_ratio=safe_float(fi.get("sales_expense_ratio")),
            admin_expense_ratio=safe_float(fi.get("admin_expense_ratio")),
            finance_expense_ratio=safe_float(fi.get("finance_expense_ratio")),
            rd_expense=safe_float(fi.get("rd_expense")),
        ))

    # ── 后处理：逐期计算跨期 yoy 字段 ──
    for i, snap in enumerate(snapshots):
        # 同比（优先取去年同期 offset=4，回退到环比 offset=1）
        yoy_idx = i + 4 if i + 4 < len(snapshots) else i + 1
        if yoy_idx < len(snapshots):
            prev_snap = snapshots[yoy_idx]
            if snap.accounts_receivable is not None and prev_snap.accounts_receivable not in (None, 0):
                snap.accounts_receivable_yoy = (
                    (snap.accounts_receivable - prev_snap.accounts_receivable)
                    / prev_snap.accounts_receivable * 100.0
                )
            if snap.inventory is not None and prev_snap.inventory not in (None, 0):
                snap.inventory_yoy = (
                    (snap.inventory - prev_snap.inventory)
                    / prev_snap.inventory * 100.0
                )

    return FinancialFacts(stock_code=ts_code, snapshots=snapshots)


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


