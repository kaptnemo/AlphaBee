"""Canonical Pydantic models for FactCollector outputs.

这是 Fact 层的 schema 中心：
- FinancialSnapshot  单期财务快照（利润表 + 资产负债表 + 现金流量表 + 财务比率）
- FinancialFacts     多期财务事实集合，持有快照列表并暴露跨期 computed_field
- MoneyFlow         单日资金流向快照
- MarketFacts        行情事实集合（报价 + 估值 + 资金流向 + 均线）

所有字段名均为 AlphaBee canonical 命名，与 derived_facts 规则的 required_facts 直接对应。
collectors 负责填充这些模型，derived_facts 引擎通过 .to_fact_values() 消费。
"""

from __future__ import annotations

import math

from pydantic import BaseModel, Field, computed_field

# ═══════════════════════════════════════════════════════════════════════════
# Financial
# ═══════════════════════════════════════════════════════════════════════════


class FinancialSnapshot(BaseModel):
    """单期财务快照 — 合并利润表、资产负债表、现金流量表和财务比率，统一使用 canonical 字段名。

    period 格式为 YYYYMMDD（报告期截止日，如 "20231231"）。
    数值字段缺失时为 None，而非 0，以区分"未上报"和"真实为零"。
    """

    period: str  # 报告期截止日，如 "20231231"

    # ── 利润表 ─────────────────────────────────────────────────────────────
    revenue: float | None = None  # 营业总收入
    operating_profit: float | None = None  # 营业利润
    net_profit: float | None = None  # 归母净利润
    ebitda: float | None = None
    interest_expense: float | None = None
    basic_eps: float | None = None  # 基本每股收益
    income_tax_expense: float | None = None  # 所得税费用
    total_profit: float | None = None  # 利润总额

    # ── 资产负债表 ─────────────────────────────────────────────────────────
    total_assets: float | None = None
    total_liabilities: float | None = None
    shareholders_equity: float | None = None  # 归母净资产
    cash: float | None = None  # 货币资金
    current_assets: float | None = None
    current_liabilities: float | None = None
    accounts_receivable: float | None = None
    inventory: float | None = None
    goodwill: float | None = None

    # ── 现金流量表 ─────────────────────────────────────────────────────────
    operating_cashflow: float | None = None
    investing_cashflow: float | None = None
    financing_cashflow: float | None = None
    capex: float | None = None  # 资本支出（取绝对值）
    dividends_paid: float | None = None
    salary_paid: float | None = None  # 支付给职工以及为职工支付的现金

    # ── 财务比率 ───────────────────────────────────────────────────────────
    roe: float | None = None  # 净资产收益率（%）
    roa: float | None = None
    gross_margin: float | None = None  # 毛利率（%）
    net_margin: float | None = None  # 净利率（%）
    current_ratio: float | None = None
    quick_ratio: float | None = None
    debt_to_assets: float | None = None  # 资产负债率（%）
    revenue_yoy: float | None = None  # 营收同比增速（%）
    net_profit_yoy: float | None = None  # 净利润同比增速（%）
    eps_growth_yoy: float | None = None  # EPS 同比增速（%）
    free_cashflow: float | None = None  # 自由现金流（tushare fcff）
    interest_bearing_debt: float | None = None  # 有息负债
    depreciation_amortization: float | None = None  # 折旧与摊销
    fixed_assets_total: float | None = None  # 固定资产合计
    sales_expense_ratio: float | None = None  # 销售费用率（%）
    admin_expense_ratio: float | None = None  # 管理费用率（%）
    finance_expense_ratio: float | None = None  # 财务费用率（%）
    rd_expense: float | None = None  # 研发费用
    # ── 跨期衍生（桥接 AnomalyEngine）─────────────────────────────────────
    accounts_receivable_yoy: float | None = None  # 应收账款同比增速（%）
    inventory_yoy: float | None = None  # 存货同比增速（%）


class FinancialFacts(BaseModel):
    """多期财务事实集合。

    snapshots 按时间倒序排列：snapshots[0] = 最新期，snapshots[1] = 上一期，
    snapshots[4] ≈ 去年同期（季报口径）。

    跨期衍生字段（avg_shareholders_equity / ebit / inventory_yoy）通过
    @computed_field 自动计算，无需手工提取。

    to_fact_values() 输出可直接传入 DerivedFacts 引擎的 fact_values 参数。
    """

    stock_code: str
    snapshots: list[FinancialSnapshot] = Field(default_factory=list)

    # ── 跨期 computed_field ────────────────────────────────────────────────

    @computed_field  # type: ignore[misc]
    @property
    def avg_shareholders_equity(self) -> float | None:
        """归母净资产近两期均值，用于 ROE 分母（杜邦分析口径）。"""
        if len(self.snapshots) < 2:
            return None
        a = self.snapshots[0].shareholders_equity
        b = self.snapshots[1].shareholders_equity
        return (a + b) / 2.0 if a is not None and b is not None else None

    @computed_field  # type: ignore[misc]
    @property
    def ebit(self) -> float | None:
        """EBIT ≈ 营业利润 + 利息费用（简化近似，适用于利息保障倍数计算）。"""
        if not self.snapshots:
            return None
        op = self.snapshots[0].operating_profit
        ie = self.snapshots[0].interest_expense
        return op + ie if op is not None and ie is not None else None

    @computed_field  # type: ignore[misc]
    @property
    def inventory_yoy(self) -> float | None:
        """存货同比增速（%）。优先取去年同期（offset=4），数据不足时退化为环比（offset=1）。"""
        snaps = self.snapshots
        if len(snaps) < 2:
            return None
        cur = snaps[0].inventory
        comp = snaps[4].inventory if len(snaps) >= 5 else snaps[1].inventory
        if cur is not None and comp is not None and comp != 0:
            return (cur - comp) / comp * 100.0
        return None

    # ── 核心输出接口 ───────────────────────────────────────────────────────

    def to_fact_values(self) -> dict[str, float]:
        """返回 DerivedFacts 引擎所需的平面 canonical 字段 dict。

        覆盖规则：
        - 当期字段：来自 snapshots[0] 的所有非 None 数值字段
        - gross_margin_current：gross_margin 的别名（规则使用该名称）
        - _prev 后缀字段：来自 snapshots[1] 的指定字段
        - 跨期计算字段：avg_shareholders_equity / ebit / inventory_yoy
        """
        result: dict[str, float] = {}

        def _put(key: str, val: float | None) -> None:
            if val is not None and not (isinstance(val, float) and math.isnan(val)):
                result[key] = val

        # 当期快照：所有标量字段直接展开
        if self.snapshots:
            cur = self.snapshots[0]
            for field_name, val in cur.model_dump(exclude={"period"}).items():
                _put(field_name, val)
            # gross_margin_current 是 gross_margin_trend 规则使用的别名
            _put("gross_margin_current", cur.gross_margin)

        # 上一期快照：仅导出规则中显式声明 _prev 的字段
        if len(self.snapshots) >= 2:
            prev = self.snapshots[1]
            _put("accounts_receivable_prev", prev.accounts_receivable)
            _put("inventory_prev", prev.inventory)
            _put("gross_margin_prev", prev.gross_margin)
            _put("shareholders_equity_prev", prev.shareholders_equity)
            # 新增：多期对比与 anomaly detection 关键字段
            _put("revenue_prev", prev.revenue)
            _put("net_profit_prev", prev.net_profit)
            _put("operating_profit_prev", prev.operating_profit)
            _put("operating_cashflow_prev", prev.operating_cashflow)
            _put("roe_prev", prev.roe)
            _put("current_ratio_prev", prev.current_ratio)
            _put("debt_to_assets_prev", prev.debt_to_assets)
            _put("interest_bearing_debt_prev", prev.interest_bearing_debt)
            _put("salary_paid_prev", prev.salary_paid)
            _put("total_profit_prev", prev.total_profit)
            _put("income_tax_expense_prev", prev.income_tax_expense)

        # 跨期 computed_field
        _put("avg_shareholders_equity", self.avg_shareholders_equity)
        _put("ebit", self.ebit)
        _put("inventory_yoy", self.inventory_yoy)

        return result


# ═══════════════════════════════════════════════════════════════════════════
# Market
# ═══════════════════════════════════════════════════════════════════════════


class MoneyFlow(BaseModel):
    """单日资金流向快照（所有金额单位：万元）。"""

    trade_date: str = ""
    super_large_order_flow: float | None = None  # 超大单净流入
    large_order_flow: float | None = None  # 大单净流入
    medium_order_flow: float | None = None  # 中单净流入
    retail_flow: float | None = None  # 小单（散户）净流入
    main_force_inflow: float | None = None  # 主力净流入 = 超大单 + 大单


class MarketFacts(BaseModel):
    """行情事实集合 — 最新报价、估值指标、资金流向和均线数据。

    to_fact_values() 输出可直接传入 DerivedFacts 引擎的 fact_values 参数。
    """

    stock_code: str
    company_name: str = ""

    # ── 最新报价 ───────────────────────────────────────────────────────────
    trade_date: str = ""
    close_price: float | None = None
    open_price: float | None = None
    high_price: float | None = None
    low_price: float | None = None
    prev_close_price: float | None = None
    price_change: float | None = None
    price_change_pct: float | None = None  # 涨跌幅（%）
    volume: float | None = None  # 成交量（手）
    turnover_amount: float | None = None  # 成交额（千元）

    # ── 估值 ───────────────────────────────────────────────────────────────
    pe_ttm: float | None = None
    pb_ratio: float | None = None
    market_cap: float | None = None  # 总市值（万元）
    circulating_market_cap: float | None = None  # 流通市值（万元）
    turnover_rate: float | None = None  # 换手率（%）

    # ── 5年PE均值（历史序列计算）──────────────────────────────────────────
    pe_ttm_5y_avg: float | None = None

    # ── 资金流向 ───────────────────────────────────────────────────────────
    moneyflow: MoneyFlow | None = None

    # ── 均线（元）────────────────────────────────────────────────────────
    ma5: float | None = None
    ma10: float | None = None
    ma20: float | None = None
    ma60: float | None = None
    ma120: float | None = None

    # ── 核心输出接口 ───────────────────────────────────────────────────────

    def to_fact_values(self) -> dict[str, float]:
        """返回 DerivedFacts 引擎所需的平面 canonical 字段 dict。

        当前规则使用的市场字段：pe_ttm, pb_ratio, pe_ttm_5y_avg。
        其余行情字段（均线、资金流向）在此一并导出，供未来规则直接使用。
        """
        result: dict[str, float] = {}

        def _put(key: str, val: float | None) -> None:
            if val is not None and not (isinstance(val, float) and math.isnan(val)):
                result[key] = val

        # 估值字段（当前规则直接依赖）
        _put("pe_ttm", self.pe_ttm)
        _put("pb_ratio", self.pb_ratio)
        _put("pe_ttm_5y_avg", self.pe_ttm_5y_avg)

        # 报价字段（为未来规则预留）
        _put("close_price", self.close_price)
        _put("price_change_pct", self.price_change_pct)
        _put("turnover_rate", self.turnover_rate)

        # 均线
        for label in ("ma5", "ma10", "ma20", "ma60", "ma120"):
            _put(label, getattr(self, label))

        # 资金流向
        if self.moneyflow is not None:
            mf = self.moneyflow
            _put("main_force_inflow", mf.main_force_inflow)
            _put("super_large_order_flow", mf.super_large_order_flow)
            _put("large_order_flow", mf.large_order_flow)
            _put("medium_order_flow", mf.medium_order_flow)
            _put("retail_flow", mf.retail_flow)

        return result
