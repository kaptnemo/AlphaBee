"""Typed contracts for the AlphaBee midterm（中期）decision layer.

本模块落地 ROADMAP 7.5 / 7.6 的 typed contracts，并补充两类决策层数据结构：
七因子快照（:class:`FactorSnapshot`）与赔率/期望值（:class:`ExpectedValue`）。

层级与数据流向（单向，见 alphabee-schema-steward / alphabee-pipeline-contract-steward）::

    外部数据源字段 → adapter/mapping → canonical 字段
        → FactorSnapshot（七因子快照）
        → VariableScores（0-100 / 方向分）
        → CompanyStateArtifact（S0–S5 认知状态 + 证据日志 + 贝叶斯后验）
        → PositionDecision（三层仓位）→ CompanyStateDiff（"发生了什么变化"）

约束：

- 承载数据值的字段名一律使用 canonical 字段名（``alphabee/schemas/*.yaml``），
  禁止出现 Tushare / AkShare / 东方财富 等外部字段名（外部名只允许出现在
  adapter / mapping / fetcher 层）。
- 缺失值显式为 ``None``，绝不静默回退为 0 或中性。
- 本模块只定义数据契约，不承载业务逻辑（分类 / 评分 / 仓位计算由
  ``classifier.py`` / ``score_engine`` / ``position.py`` 等引擎实现）。

下游承载关系（S1–S4 状态机）：

- ``FactorSnapshot`` 是「第三十四节 Snapshot」的 typed 化：每次重要事件后保存一帧，
  真正有信息量的产物是 ``Snapshot_t − Snapshot_{t-1}``（:class:`CompanyStateDiff`）。
- ``CompanyStateArtifact`` 是 S0–S5 认知状态的持久化契约，内嵌
  ``expectation_gap`` / ``variable_scores`` / ``evidence_log`` / ``position``，
  由 ``bayes.py`` 用证据日志做 log-odds 更新，由 ``classifier.py`` 做合法迁移判定。
- ``ExpectedValue``（EV = Σ P·R、RiskAdjustedEV）是最终决策核心：状态机不替代 EV，
  而是帮助判断 bull/base/bear 三情景概率如何随证据变化
  （Evidence → BeliefUpdate → ScenarioProbability → ExpectedReturn → Position）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────────────────────
# 认知状态（S0–S5）
# ─────────────────────────────────────────────────────────────────────────────
class CognitiveState(StrEnum):
    """S0–S5 认知状态（文档 S1–S4；S1–S4 为核心持有周期，S0/S5 为出入口）。"""

    S0_RESEARCH = "S0"  # 研究候选（尚未形成可交易预期差）
    S1_GAP = "S1"  # 预期差形成（试探仓）
    S2_CONFIRM = "S2"  # 证据确认（加仓）
    S3_CONSENSUS = "S3"  # 共识扩散（核心持有）
    S4_PRICED = "S4"  # 充分定价（减仓）
    S5_EXIT = "S5"  # 退出（thesis_broken / alpha_exhausted）


# ─────────────────────────────────────────────────────────────────────────────
# 软状态 / 状态迁移（文档 §2b / §4）
# ─────────────────────────────────────────────────────────────────────────────
class Consistency(StrEnum):
    """因子一致性标注（文档 §4.4）：共振 / 背离 / 独立。

    - ``resonant``：多因子同向（如 F↑ + E↑ + T↑ 三共振，S3 健康）；
    - ``divergent``：业绩好但股价不涨（F 强、E/T 弱），本身即 Conflict；
    - ``independent``：各因子独立，无同向也无背离。
    """

    RESONANT = "resonant"  # 同向共振
    DIVERGENT = "divergent"  # 背离
    INDEPENDENT = "independent"  # 独立


class FieldChange(StrEnum):
    """L1 字段级变化类型（design §3 / §7）。

    严格区分「出现/消失」与「增减」（alphabee-schema-steward）：``None→值`` 是
    ``appeared``、``值→None`` 是 ``disappeared``，二者绝不得与 ``up`` / ``down``
    混淆（出现/消失是数据可用性变化，up/down 是口径内数值变化）。
    """

    APPEARED = "appeared"  # None → 有值（基线登记 / 数据源补齐）
    DISAPPEARED = "disappeared"  # 有值 → None（降级 / 覆盖消失）
    UP = "up"  # 数值上升（同名同口径）
    DOWN = "down"  # 数值下降（同名同口径）
    UNCHANGED = "unchanged"  # 数值不变


class StateBelief(BaseModel):
    """软状态（Soft State）——把 State 从点估计降级为概率分布（文档 §2b）。

    应对过渡态 / 矛盾态 / 多 Thesis / 层级错位等「不标准」状态：``distribution`` 承载
    概率质量分布（和约 1），``argmax_state`` 仅作为动作类型锚点（取值域
    :class:`CognitiveState`），``entropy`` 量化状态模糊度（越高越「不标准」），
    ``drift`` 记录相比上一帧的概率质量流向（无上一帧时显式 ``None``）。
    """

    distribution: dict[str, float]  # {"S1":0.1,"S2":0.7,...} 概率质量，和为 1
    argmax_state: str  # 名义状态（CognitiveState 取值域），动作类型锚点
    entropy: float  # 分布熵 = 状态模糊度（越高越「不标准」）
    drift: dict[str, float] | None = None  # 相比上一帧概率质量流向（无上一帧=None）


class StateTransition(BaseModel):
    """状态迁移判定（文档 §4.5）：classifier 只描述「生命周期在哪 + 是否合法迁移」。

    ``legal`` 表示迁移是否合法（S1→S2→S3→S4→S5 及反向降级合法；跳级如 S1→S3 非法）；
    ``kind`` 区分迁移类型（``reopen`` = 第二增长曲线重开预期差，§31–§32）；
    ``consistency`` 汇总触发迁移因子的整体一致性（共振 / 背离 / 独立）。
    """

    from_state: str = ""  # 迁移前状态（CognitiveState 取值域）
    to_state: str = ""  # 迁移后状态（CognitiveState 取值域）
    legal: bool = False  # 是否合法迁移（跳级=False，§41）
    trigger_factors: list[str] = Field(default_factory=list)  # 触发迁移的因子（如 ["E","T"]）
    kind: str = "normal"  # 迁移类型：normal / downgrade / reopen / illegal 等
    consistency: Consistency = Consistency.INDEPENDENT  # resonant / divergent / independent


class FactorScoreDelta(BaseModel):
    """单个因子的方向分变化 + 一致性标注（文档 §4.4）。

    ``delta`` 为该因子方向分的边际变化（正 = 改善 / 走强 / 赔率提高）；
    ``consistency`` 标注该因子相对其他因子是共振（resonant）/ 背离（divergent）/
    独立（independent）。背离是 S3→S4 与 EmergencyRiskStop 的前置信号。

    注意：本模型承载的是 L2 评分层的「方向分 Δ + 一致性」，属 classifier 的
    ``ClassifierResult.factor_deltas`` 契约。L1 数据层的「因子级字段变化聚合」由
    diff 引擎的 :class:`FactorDelta`（见下方 Snapshot Diff 子模型区）承载，二者
    口径不同（评分层 vs 数据层），故分开命名。
    """

    factor: str = ""  # 因子标识：F/E/T/V/C/R/M
    delta: float | None = None  # 方向分变化 Δscore（缺失显式 None）
    consistency: Consistency = Consistency.INDEPENDENT  # resonant / divergent / independent


# ─────────────────────────────────────────────────────────────────────────────
# 七因子快照（FactorSnapshot 的因子维度）
# ─────────────────────────────────────────────────────────────────────────────
class FundamentalFactor(BaseModel):
    """F 因子快照——基本面及其变化（canonical: financial / operation / company）。

    字段名沿用 ``schemas/financial.yaml`` 的 canonical 名，单位与 canonical 一致。
    ``None`` 表示该字段缺失或未采集（不静默回退为 0）。
    """

    revenue_yoy: float | None = None  # PERCENT 营收同比
    net_profit_yoy: float | None = None  # PERCENT 净利润同比
    eps_growth_yoy: float | None = None  # PERCENT EPS 同比
    roe: float | None = None  # PERCENT 净资产收益率
    gross_margin: float | None = None  # PERCENT 毛利率
    net_margin: float | None = None  # PERCENT 净利率
    operating_cashflow: float | None = None  # CNY 经营现金流净额
    free_cashflow: float | None = None  # CNY 自由现金流
    debt_to_assets: float | None = None  # PERCENT 资产负债率
    current_ratio: float | None = None  # RATIO 流动比率
    goodwill: float | None = None  # CNY 商誉（财务风险来源）
    segment_fastest_yoy: float | None = None  # PERCENT 最快细分业务同比增速（改造 E，company_track.segments）
    # 最慢细分同比增速：改造 E 预留字段，当前不参与评分；P2 改造 D（EV 收益 materiality 化）
    # 将作为「最慢业务拖累项」消费，避免结构性亮点只反映最快而不反映最慢的失真。
    segment_slowest_yoy: float | None = None  # PERCENT 最慢细分业务同比增速（预留：P2 materiality 拖累项）
    direction: str = "stable"  # improving / stable / deteriorating
    score: float | None = None  # 0-100 方向分（score_engine 输出）


class ExpectationFactor(BaseModel):
    """E 因子快照——市场预期及 Revision（canonical: expectation + consensus）。

    承载已有 ``expectation.yaml``（业绩预告/快报）与本批新增 ``consensus.yaml``
    （分析师一致预期，design §3.4 十三字段）两个 canonical 域。
    """

    # 已有 expectation 域（公告类预期）
    profit_forecast_min_change: float | None = None  # PERCENT 预告净利润同比下限
    profit_forecast_max_change: float | None = None  # PERCENT 预告净利润同比上限
    express_revenue_yoy: float | None = None  # PERCENT 快报营收同比
    express_net_profit_yoy: float | None = None  # PERCENT 快报净利同比
    # 新增 consensus 域（分析师一致预期，design §3.4）
    eps_fy1: float | None = None  # CNY_PER_SHARE FY1 一致 EPS
    eps_fy2: float | None = None  # CNY_PER_SHARE FY2 一致 EPS
    eps_fy3: float | None = None  # CNY_PER_SHARE FY3 一致 EPS
    target_price: float | None = None  # CNY 一致目标价
    rating_mean: float | None = None  # RATING emRatingValue 均值，持有=1/增持=2/买入=3（越大越看多）
    coverage_count: int | None = None  # COUNT 覆盖机构数
    eps_fy1_revision_1m: float | None = None  # PERCENT FY1 EPS 近1月上修幅度
    eps_fy1_revision_3m: float | None = None  # PERCENT FY1 EPS 近3月上修幅度
    eps_fy2_revision_1m: float | None = None  # PERCENT FY2 EPS 近1月上修幅度
    revision_breadth: float | None = None  # RATIO 上调机构占比 0-1
    revision_acceleration: float | None = None  # PERCENT revision 二阶差分
    rating_upgrade_1m: int | None = None  # COUNT 近1月上调机构数
    rating_downgrade_1m: int | None = None  # COUNT 近1月下调机构数
    direction: str = "neutral"  # improving / neutral / deteriorating
    score: float | None = None  # 方向分（revision_score，E 为核心因子）


class TrendFactor(BaseModel):
    """T 因子快照——价格/相对强度趋势（canonical: market / industry）。

    RS 字段为本批新增（design §4.3）。canonical 名为 ``rs_stock_market`` /
    ``rs_stock_industry`` / ``rs_industry_market``；快照按 20/60 日两个 horizon
    分别承载（文档 Snapshot 的 ``trend.rs_20d/rs_60d``）。``industry_breadth_20/60``
    为 canonical 原名（industry 域）。均线字段复用已有 ``market.yaml``。
    """

    rs_stock_market_20d: float | None = None  # PERCENT 个股-全市场 20日超额收益
    rs_stock_market_60d: float | None = None  # PERCENT 个股-全市场 60日超额收益
    rs_stock_industry_20d: float | None = None  # PERCENT 个股-行业 20日超额收益
    rs_stock_industry_60d: float | None = None  # PERCENT 个股-行业 60日超额收益
    rs_industry_market_20d: float | None = None  # PERCENT 行业-全市场 20日超额收益
    rs_industry_market_60d: float | None = None  # PERCENT 行业-全市场 60日超额收益
    industry_breadth_20: float | None = None  # PERCENT 行业成分股 P>MA20 占比
    industry_breadth_60: float | None = None  # PERCENT 行业成分股 P>MA60 占比
    price_change_pct: float | None = None  # PERCENT 当日涨跌幅
    ma20: float | None = None  # CNY 20日均线
    ma60: float | None = None  # CNY 60日均线
    ma120: float | None = None  # CNY 120日均线
    direction: str = "neutral"  # improving / neutral / deteriorating
    score: float | None = None  # 方向分（relative_strength_score）


class ValuationFactor(BaseModel):
    """V 因子快照——估值与赔率（canonical: market / industry）。

    估值分位为本批新增（design §5.3）：``pe_ttm_5y_percentile`` / ``pb_5y_percentile``
    （RATIO 0-1）。``percentile`` 为文档 Snapshot 的 ``valuation.percentile`` 综合分位
    （由上述两个 canonical 分位合成，低分位 = 赔率高；显式提示 Cheap≠Buy）。
    """

    pe_ttm: float | None = None  # RATIO 市盈率 TTM
    pb_ratio: float | None = None  # RATIO 市净率
    pe_ttm_5y_avg: float | None = None  # RATIO 近5年 PE-TTM 均值
    pe_ttm_5y_percentile: float | None = None  # RATIO 0-1 当前 PE 在 5 年序列分位（新增）
    pb_5y_percentile: float | None = None  # RATIO 0-1 当前 PB 在 5 年序列分位（新增）
    industry_pe_ttm: float | None = None  # RATIO 行业 PE-TTM
    industry_pb: float | None = None  # RATIO 行业 PB
    peer_median_pe_ttm: float | None = None  # RATIO 对标组 PE 中位数
    peer_median_pb: float | None = None  # RATIO 对标组 PB 中位数
    percentile: float | None = None  # RATIO 0-1 综合估值分位（Snapshot.valuation.percentile）
    direction: str = "neutral"  # cheap / fair / expensive
    score: float | None = None  # 方向分（valuation_percentile_score）


class CrowdingFactor(BaseModel):
    """C 因子快照——拥挤程度（canonical: crowding，本批新增 design §6.4）。

    覆盖「谁已建仓 · 交易多热 · 多少人盯着 · 筹码集中度 · 估值多极端 · 供给多压」。
    人气榜未上榜 / 股东户数缺失等场景显式产出 ``None``（绝不回退为 0）。
    ``turnover_rate`` 同时存在于 ``market.yaml``（日频绝对量），此处承载其拥挤度视角。
    """

    turnover_rate: float | None = None  # PERCENT 换手率
    turnover_rate_percentile: float | None = None  # RATIO 历史换手率分位
    amount_pct_of_market: float | None = None  # PERCENT 成交额占全市场比
    holder_count_change: float | None = None  # PERCENT 股东户数增幅（季度）
    per_capita_holding_change: float | None = None  # PERCENT 人均持股增幅（季度）
    institutional_holding_ratio: float | None = None  # PERCENT 前十大集中度（季度）
    margin_balance_yoy: float | None = None  # PERCENT 两融余额同比
    analyst_coverage_rank: int | None = None  # RANK 研报覆盖热度排名（周）
    hot_rank: int | None = None  # RANK 人气榜排名（日，无上榜=缺失）
    news_heat: float | None = None  # SCORE 新闻热度 0-100
    leader_concentration: float | None = None  # PERCENT 龙头成交集中度
    direction: str = "neutral"  # overheated / normal / cold
    score: float | None = None  # 方向分（crowding_score）


class AuditSnapshot(BaseModel):
    """R 因子——财务审计意见快照（canonical: risk.audit_*，来源 Tushare fina_audit）。

    design §7.3 原「违规处罚（penalty）」接口不存在（t3 实测），I7 改以 fina_audit
    审计意见承载，canonical 字段见 ``schemas/risk.yaml``（audit_opinion/audit_agency/
    audit_fees/audit_sign）。非标准无保留意见为审计异常风险信号。
    """

    audit_opinion: str | None = None  # TEXT 审计意见（如 标准无保留意见）
    audit_agency: str | None = None  # TEXT 审计机构（会计师事务所）
    audit_fees: float | None = None  # CNY 审计费用（元）
    audit_sign: str | None = None  # TEXT 签字会计师


class IndustryRiskSnapshot(BaseModel):
    """R 因子——产业风险快照（占位，design §7.3 缺口）。

    无行业级风险字段（景气拐点 / 政策监管 / 供需恶化）。占位契约，后续或复用
    ``industry.yaml`` + 定性 LLM 判断。
    """

    risk_level: str = "unknown"  # low / medium / high
    cycle_position: str = "unknown"  # 景气周期位置（early/mid/late/down）
    policy_risk: str = ""  # 定性：政策监管风险描述
    supply_demand: str = ""  # 定性：供需格局描述


class RiskFactor(BaseModel):
    """R 因子快照——个股/产业风险（canonical: risk + financial）。

    已有 ``risk.yaml`` 字段（质押/回购/新闻）+ ``financial.yaml`` 杠杆/商誉，
    加上财务审计意见（见 :class:`AuditSnapshot`）与产业风险（见
    :class:`IndustryRiskSnapshot`）两个契约。
    """

    pledge_ratio: float | None = None  # PERCENT 股权质押比例
    repurchase_progress: str = ""  # TEXT 回购进展状态
    news_title: str = ""  # TEXT 最新相关新闻标题
    debt_to_assets: float | None = None  # PERCENT 资产负债率（杠杆）
    goodwill: float | None = None  # CNY 商誉
    audit: AuditSnapshot | None = None  # 财务审计意见（fina_audit，替代 penalty 占位）
    industry_risk: IndustryRiskSnapshot | None = None  # 产业风险占位（design §7.3）
    direction: str = "neutral"  # risk_declining / neutral / risk_rising
    score: float | None = None  # 三层风险合成方向分


class MarketFactor(BaseModel):
    """M 因子快照——市场环境（canonical: market_regime）。

    复用 market_regime 子系统（ROADMAP 7.5：M 复用 MarketScore）。本快照承载
    regime 摘要 + 关键 canonical 摘要字段；权威全量快照为 ``RegimeSnapshot``
    （``alphabee.market_regime.models``），经 artifact 约定消费，不在此重复定义。
    """

    market_score: float | None = None  # MarketScore.total_score 0-100
    regime: str = ""  # 市场阶段名（如 震荡/熊市/牛市）
    position_low: float | None = None  # RATIO 建议仓位下限（PositionAdvice）
    position_high: float | None = None  # RATIO 建议仓位上限
    hs300_pe_ttm: float | None = None  # RATIO 沪深300 PE-TTM
    hs300_pb: float | None = None  # RATIO 沪深300 PB
    hs300_close: float | None = None  # POINT 沪深300 收盘点位
    breadth_above_ma60_pct: float | None = None  # PERCENT 市场宽度（field_gap，可能缺失）
    market_turnover: float | None = None  # CNY 全市场成交额
    margin_balance: float | None = None  # CNY 两融余额


class FactorSnapshot(BaseModel):
    """七因子快照（文档 S1–S4 第三十四节「Snapshot」的 typed 化）。

    整个中期决策层最核心的可落盘数据。每次重要事件后保存一帧，真正有信息量的
    增量产物是 ``Snapshot_t − Snapshot_{t-1}``（见 :class:`CompanyStateDiff`）。
    七个维度字段名均使用 canonical 名；缺失维度显式 ``None`` 并登记
    ``missing_facts``（绝不静默回退）。
    """

    schema_version: str = "1"
    symbol: str = ""
    as_of_date: str = ""  # YYYY-MM-DD
    state: str = "S0"  # CognitiveState（当前认知状态）
    confidence: float = 0.0  # P(H|Evidence) 后验 0-1
    fundamental: FundamentalFactor = Field(default_factory=FundamentalFactor)
    expectation: ExpectationFactor = Field(default_factory=ExpectationFactor)
    trend: TrendFactor = Field(default_factory=TrendFactor)
    valuation: ValuationFactor = Field(default_factory=ValuationFactor)
    crowding: CrowdingFactor = Field(default_factory=CrowdingFactor)
    risk: RiskFactor = Field(default_factory=RiskFactor)
    market: MarketFactor = Field(default_factory=MarketFactor)
    missing_facts: list[str] = Field(default_factory=list)  # 缺失 canonical 字段名
    degraded: bool = False  # 上游缺失触发降级（显式 issue）
    degraded_reason: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# 赔率 / 期望值（文档第四十二节）
# ─────────────────────────────────────────────────────────────────────────────
class ScenarioOutcome(BaseModel):
    """单个情景（bull / base / bear）的概率与收益，构成 EV 的求和项。

    ``expected_return`` 为该情景下的预期收益（PERCENT，正=盈利/负=亏损），
    ``probability`` 为 0-1。三情景概率之和应约为 1（由 bayes/状态机驱动）。
    """

    scenario: str = ""  # bull / base / bear
    probability: float = 0.0  # P，0-1
    expected_return: float | None = None  # R，PERCENT 该情景预期收益
    earnings_contribution: float | None = None  # PERCENT 盈利变化贡献
    valuation_contribution: float | None = None  # PERCENT 估值变化贡献
    max_drawdown: float | None = None  # PERCENT 该情景最大回撤（负值）


class ExpectedValue(BaseModel):
    """赔率 / 期望值决策结构（文档第四十二节）。

    ``ev = Σ P·R``（bull/base/bear 三情景加权收益），``risk_adjusted_ev`` 为
    除以风险度量后的风险调整期望值（RATIO）。状态机的意义不是替代 EV，而是帮助
    判断三情景概率如何随证据变化：Evidence → BeliefUpdate → ScenarioProbability
    → ExpectedReturn → Position。
    """

    scenarios: list[ScenarioOutcome] = Field(default_factory=list)
    ev: float | None = None  # PERCENT EV = Σ P·R
    risk: float | None = None  # PERCENT 风险度量（下行波动/回撤）
    risk_adjusted_ev: float | None = None  # RATIO EV / risk
    probability_source: str = ""  # bayes_posterior / state_prior / manual
    as_of_date: str = ""  # YYYY-MM-DD
    note: str = ""  # 定性赔率说明


# ─────────────────────────────────────────────────────────────────────────────
# ROADMAP 7.5 决策层 typed contracts
# ─────────────────────────────────────────────────────────────────────────────
class VariableScores(BaseModel):
    """七变量得分（M 复用 MarketScore，其余 0-100 或 [-1,1] 方向分）。"""

    m: dict[str, Any] = Field(default_factory=dict)  # 复用 RegimeSnapshot / MarketScore 摘要
    f_fundamental_trend: float | None = None  # 边际变化方向（dF/dt）
    e_revision: float | None = None  # 上修(+) / 下修(-)
    t_relative_strength: float | None = None  # 相对强度方向分
    v_valuation_percentile: float | None = None  # 估值分位（低=赔率高）
    c_crowding: float | None = None  # 拥挤度方向分
    r_risk: float | None = None  # 三层风险合成方向分


# ─────────────────────────────────────────────────────────────────────────────
# 证据抽取（Evidence Extraction）typed contracts（设计 MIDTERM_EVIDENCE_EXTRACTION.md）
#
# 两阶段抽取（§4）：Stage A 客观事实（:class:`FactEvent`，可跨 thesis 复用/缓存）
# → Stage B 相对 thesis 的方向判定（:class:`EvidenceJudgment`，离散等级）
# → 组装 :class:`EvidenceEvent`（供 ``bayes.update_confidence`` 消费）。
#
# ``confidence_delta`` 只允许离散等级（§6，禁止 LLM 自由出连续值）：
# weak=0.1 / medium=0.3 / strong=0.5，单条上限 0.7（见下方常量）。
# ─────────────────────────────────────────────────────────────────────────────


class Strength(StrEnum):
    """证据强度离散等级（设计 §6，禁 LLM 连续值）。

    Stage B 只允许输出这三个离散等级，任何连续数值（如 0.42）在构造时即被
    Pydantic 拒绝，从契约层杜绝「LLM 自由出连续值」。
    """

    WEAK = "weak"
    MEDIUM = "medium"
    STRONG = "strong"


class EffectOnThesis(StrEnum):
    """证据相对 thesis 的方向（Stage B 产出，§4.3）。"""

    CONFIRMING = "confirming"
    REFUTING = "refuting"
    NEUTRAL = "neutral"


# 离散证据强度等级 → confidence_delta 数值（设计 §6；E1 规则标定与 E3 Stage B
# 共享同一映射，只读，禁止在调用方各自重新定义导致口径分裂）。
STRENGTH_LEVELS: tuple[str, ...] = ("weak", "medium", "strong")
STRENGTH_DELTA: dict[str, float] = {"weak": 0.1, "medium": 0.3, "strong": 0.5}
MAX_CONFIDENCE_DELTA: float = 0.7  # 单条证据强度上限（§6；连续值一律拒绝）


class FactEvent(BaseModel):
    """Stage A 客观事实事件（无方向判定，可跨 thesis 复用，入库缓存，§4.1）。

    只回答「发生了什么」，不回答「好不好」。``numbers`` 只承载原文可溯源的
    canonical 数值（原文没有的字段显式置 ``None``，绝不补全）；``quotes`` 为
    ``description`` 的原文支撑句（防幻觉第一道闸：无引用不出数）。
    """

    id: str  # 事件签名哈希：date+kind+主体+数值（去重键，§7）
    date: str  # 事件发生日 YYYY-MM-DD（非抽取日）
    kind: str  # 受控词表：fundamental / expectation / trend / crowding / thesis / price
    description: str  # 客观事实陈述（不含方向判断）
    numbers: dict[str, float | None] = Field(default_factory=dict)  # canonical 数值（如 {"revenue_yoy": 5.2}）
    quotes: list[str] = Field(default_factory=list)  # 原文引用（description 的支撑句）
    source_refs: list[str] = Field(default_factory=list)  # 来源 URL / 引用
    source_type: str = ""  # financial_report / forecast / research_report / announcement / news


class EvidenceJudgment(BaseModel):
    """Stage B 输出：相对 thesis 的方向与强度（→ 组装 :class:`EvidenceEvent`，§4.3）。

    方向（``effect_on_thesis``）与强度（``strength``）分离：强度只允许离散等级
    weak / medium / strong，禁止 LLM 自由出连续值（§6）。``reasoning`` 必须引用
    事实的原文（防幻觉），可审计。
    """

    fact_id: str  # 对应 FactEvent.id
    effect_on_thesis: EffectOnThesis  # confirming / refuting / neutral
    strength: Strength  # weak / medium / strong（离散等级，§6；StrEnum 禁连续值）
    reasoning: str  # 相对 H 的推理（可审计，写入 note/description）


class EvidenceEvent(BaseModel):
    """一条证据事件，供 ``bayes.py`` 做 log-odds 更新 P(H|Evidence)。"""

    id: str
    date: str  # YYYY-MM-DD
    kind: str  # fundamental / expectation / trend / crowding / thesis / price
    description: str
    effect_on_thesis: str  # confirming / refuting / neutral
    confidence_delta: float  # ΔP(H|E)，供 bayes log-odds 更新
    source_refs: list[str] = Field(default_factory=list)


class ExitCondition(BaseModel):
    """退出条件（thesis / fundamental / expectation / valuation_crowding / opportunity_cost）。"""

    kind: str  # thesis_broken / fundamental_stop / expectation_stop
    # / valuation_crowding_exit / opportunity_cost
    condition: str  # 触发条件描述
    met: bool = False


class ExpectationGap(BaseModel):
    """预期差（OurForecast vs MarketImpliedExpectation）。"""

    symbol: str = ""
    as_of_date: str = ""
    our_forecast: dict[str, Any] = Field(default_factory=dict)  # 我们的盈利/驱动预测
    implied_expectation: dict[str, Any] = Field(default_factory=dict)  # 从估值/价格反推的市场隐含预期
    gap: float | None = None  # 预期差（正=我们更乐观）
    gap_direction: str = "neutral"  # positive / negative / neutral
    evidence: list[str] = Field(default_factory=list)
    stale_after: str | None = None


class PositionDecision(BaseModel):
    """三层仓位决策（exposure × stock_weight）。"""

    portfolio_exposure: float | None = None  # 来自 market_regime PositionAdvice
    stock_weight: float | None = None  # f(F,E,T,V,C,R)
    actual_weight: float | None = None  # exposure × weight
    position_band: str = ""  # 试探/加仓/核心/减仓/清仓（映射 S 阶段）
    rationale: list[str] = Field(default_factory=list)
    restricted: bool = False  # 单股上限/单次调仓限制是否生效


class CompanyStateArtifact(BaseModel):
    """S0–S5 认知状态持久化契约（决策层核心 artifact）。"""

    schema_version: str = "1"
    symbol: str = ""
    state: StateBelief | None = None  # 软状态（§2b；CognitiveState 为其 argmax_state 取值域）
    thesis: str = ""  # 核心假设 H
    thesis_confidence: float = 0.0  # P(H|Evidence) 后验
    prior_confidence: float = 0.0  # 先验 P(H)
    expectation_gap: ExpectationGap = Field(default_factory=ExpectationGap)
    variable_scores: VariableScores = Field(default_factory=VariableScores)
    factor_snapshot: FactorSnapshot | None = None  # 最近一帧七因子快照（本批新增）
    expected_value: ExpectedValue | None = None  # 赔率/期望值（本批新增）
    evidence_log: list[EvidenceEvent] = Field(default_factory=list)
    next_evidence_to_watch: list[str] = Field(default_factory=list)
    exit_conditions: list[ExitCondition] = Field(default_factory=list)
    position: PositionDecision | None = None
    as_of_date: str = ""
    stale_after: str | None = None
    degraded: bool = False  # 上游缺失触发降级（显式 issue）
    degraded_reason: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# ROADMAP 7.6 状态 / 组合 / 日志契约
# ─────────────────────────────────────────────────────────────────────────────
class ThesisVersion(BaseModel):
    """Thesis 版本（append-only，反漂移）。原始买入理由与证伪条件不可被行情重写。"""

    version: int
    as_of_date: str
    thesis: str
    buy_rationale: list[str] = Field(default_factory=list)  # 当时的买入理由
    invalidation: list[str] = Field(default_factory=list)  # 当时的证伪条件


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot Diff 子模型（design MIDTERM_STATE_DIFF_DESIGN.md §3；D1 typed contracts）
#
# 五层正交分层：L1 数据（FieldDelta / FactorDelta）→ L2 评分（scores，见主模型）
# → L3 状态（StateShift）+ 置信（ConfidenceDelta）→ L4 赔率（EVDiff）→ L5 仓位
# （PositionDiff）。归因链（ChangeAttribution）自下而上承载 evidence → factor →
# decision。全部为纯数据契约，不承载计算逻辑（计算由 ``diff.py`` 实现）。
# 主模型 :class:`CompanyStateDiff` 定义在子模型之后（见下方）。
# ─────────────────────────────────────────────────────────────────────────────


class ArtifactRef(BaseModel):
    """快照引用（不内嵌，append-only 反漂移，design §1.5）。"""

    id: str  # CompanyStateArtifact 持久化 id
    date: str  # YYYY-MM-DD
    symbol: str = ""


class FieldDelta(BaseModel):
    """L1 字段级变化（最小粒度，design §3）。

    只在同名同口径字段间计算（``revision_1m → revision_1m``）。``change`` 严格区分
    ``appeared``（None→值）/ ``disappeared``（值→None）与 ``up`` / ``down`` /
    ``unchanged``——出现/消失不得与增减混淆（alphabee-schema-steward）。
    """

    field: str  # canonical 字段名
    prev: float | None
    curr: float | None
    delta: float | None  # 绝对差 curr - prev（单位随 canonical）
    rel_delta: float | None  # 相对差 curr/prev - 1（跨量纲可比）
    change: FieldChange  # appeared | disappeared | up | down | unchanged


class FactorDelta(BaseModel):
    """L1 因子级变化（聚合，design §3）。

    仅聚合发生变化（``change != unchanged``）的字段；``direction`` 为
    improving / neutral / deteriorating（由关键字段 ``rel_delta`` 方向聚合）；
    ``consistency`` 为与其它因子的共振 / 背离 / 独立（resonant / divergent /
    independent）。

    注意：与 :class:`FactorScoreDelta`（L2 方向分 Δ，classifier 产物）口径不同，
    本模型承载的是 L1 数据层的字段变化聚合，供 diff 引擎消费。
    """

    factor: str  # F / E / T / V / C / R / M
    direction: str  # improving | neutral | deteriorating
    fields: list[FieldDelta] = Field(default_factory=list)  # 仅含发生变化的字段
    consistency: str = ""  # resonant | divergent | independent（与其它因子）


class StateShift(BaseModel):
    """L3 软状态漂移（design §3 / §2b）。

    State 变化 = 分布漂移，不是 argmax 跳变：``tv_distance`` 承载信念位移总量，
    ``mass_delta`` 承载逐状态质量流动，``entropy_delta`` 区分「整体平移」与
    「越来越拿不准」。``argmax_from/argmax_to`` 只是漂移的一个投影。
    """

    argmax_from: str | None  # 首帧为 None
    argmax_to: str
    mass_delta: dict[str, float] = Field(default_factory=dict)  # {S_k: P_curr - P_prev}
    tv_distance: float = 0.0  # 0.5·Σ|ΔP|，信念位移总量 0-1
    entropy_from: float | None = None
    entropy_to: float = 0.0
    entropy_delta: float | None = None  # 变确定（负）/ 变模糊（正）
    drift: dict[str, float] | None = None  # 质量流向（复用 classifier._drift 口径）
    legal: bool = False  # 迁移合法性（classifier 判定）
    kind: str = ""  # upgrade | downgrade | same | reopen


class ConfidenceDelta(BaseModel):
    """置信度变化（L3'，与状态漂移正交，design §3）。"""

    prior: float | None
    posterior: float | None
    delta: float | None  # posterior − prior
    log_odds_delta: float | None  # logit(posterior) − logit(prior)
    evidence_ids: list[str] = Field(default_factory=list)  # 驱动变化的 EvidenceEvent id


class EVDiff(BaseModel):
    """L4 赔率变化（design §3）。"""

    ev_from: float | None
    ev_to: float | None
    ev_delta: float | None
    risk_adjusted_ev_delta: float | None
    scenario_probability_delta: dict[str, float] = Field(default_factory=dict)  # {bull/base/bear: ΔP}
    scenario_return_delta: dict[str, float | None] = Field(default_factory=dict)  # {bull/base/bear: ΔR}
    probability_source_change: str = ""  # 如 state_prior → bayes_posterior


class PositionDiff(BaseModel):
    """L5 仓位变化（design §3）。"""

    stock_weight_delta: float | None = None
    actual_weight_delta: float | None = None
    exposure_delta: float | None = None  # M 的市场暴露变化
    band_from: str = ""
    band_to: str = ""
    band_weight_divergence: bool = False  # ⚠️ 标签与实际仓位背离（实跑已踩坑）
    drivers: list[str] = Field(default_factory=list)  # 归因：state / confidence / ev / market / portfolio


class ChangeAttribution(BaseModel):
    """归因（为什么变，design §3 / §5）：evidence → factor → decision。"""

    evidence_ids: list[str] = Field(default_factory=list)  # 触发变化的证据
    factor_deltas: list[str] = Field(default_factory=list)  # 受影响的因子（如 ["E","F"]）
    decision_effects: list[str] = Field(default_factory=list)  # 决策层影响（如 ["state:S2→S3","confidence:+0.2"]）
    note: str = ""  # 一句话因果解释


class CompanyStateDiff(BaseModel):
    """五层分层差分主模型（design §3；D1 升级替换薄壳 ``SnapshotDiff``）。

    承载同一标的两个时间点的 ``CompanyStateArtifact`` 变化：五层正交分层
    （L1 ``factors`` / L2 ``scores`` / L3 ``state_shift`` + ``confidence`` /
    L4 ``ev`` / L5 ``position``）+ 证据归因（``new_evidence`` / ``attribution`` /
    ``thesis_delta``）+ 退出检查（``exit_conditions_met``）+ 元信息（降级翻转、
    missing 差集）。首帧（``is_first=True``）各层按「基线登记」处理。

    旧 ``SnapshotDiff`` 字段语义迁移：``state_from/state_to`` →
    :class:`StateShift` 的 ``argmax_from/argmax_to``；``variable_deltas`` →
    ``scores`` + ``factors[].fields`` 分层承载；``evidence_changed`` →
    ``new_evidence`` + ``attribution[].evidence_ids``。
    """

    symbol: str
    prev: ArtifactRef | None  # 首帧为 None
    curr: ArtifactRef
    anchor: ArtifactRef | None = None  # 建仓锚点（可选，§44）
    is_first: bool = False
    elapsed_days: int

    # 五层变化（首帧时各层按「基线登记」处理）
    factors: list[FactorDelta] = Field(default_factory=list)  # L1
    scores: dict[str, float | None] = Field(default_factory=dict)  # L2：canonical 方向分名 → Δ
    state_shift: StateShift | None = None  # L3
    confidence: ConfidenceDelta | None = None  # L3'
    ev: EVDiff | None = None  # L4
    position: PositionDiff | None = None  # L5

    # 证据与归因
    new_evidence: list[EvidenceEvent] = Field(default_factory=list)
    attribution: list[ChangeAttribution] = Field(default_factory=list)
    thesis_delta: str = ""  # 可读总结（由 attribution 投影 / LLM 润色）

    # 退出检查（§37 ExitEngine）
    exit_conditions_met: list[str] = Field(default_factory=list)  # 新满足的退出条件 kind

    # 元信息
    degraded_flip: str = ""  # 降级状态翻转（False→True / True→False）说明
    missing_appeared: list[str] = Field(default_factory=list)  # 新出现（数据源补齐）的字段
    missing_disappeared: list[str] = Field(default_factory=list)  # 新缺失（降级/覆盖消失）的字段


class ResearchTask(BaseModel):
    """关键未知 → 研究任务（知道自己不知道）。"""

    id: str
    unknown: str  # 如「《烈焰觉醒》Q3 流水 / 投放 ROI」
    importance: str = "medium"  # high / medium / low
    decides: list[str] = Field(default_factory=list)  # 影响哪个假设/结论
    status: str = "open"  # open / done / blocked


class HoldingWeight(BaseModel):
    """单个持仓的组合权重。"""

    symbol: str = ""
    state: str = ""
    weight: float | None = None  # 权益仓内权重
    odds: str = ""  # 赔率定性
    confidence: float = 0.0
    crowding: str = ""


class PortfolioAllocation(BaseModel):
    """组合级配置（Portfolio Allocator）：跨持仓 Weight_i。"""

    holdings: list[HoldingWeight] = Field(default_factory=list)
    total_exposure: float | None = None
    sector_exposure: dict[str, float] = Field(default_factory=dict)
    risk_concentration: str = ""
    suggested_changes: list[str] = Field(default_factory=list)  # 应增/应减/应换


class DecisionJournalEntry(BaseModel):
    """决策日志：买入理由 + 证伪 + 事后复盘。"""

    id: str
    date: str
    action: str  # buy / add / hold / reduce / sell / replace
    symbol: str = ""
    rationale: list[str] = Field(default_factory=list)
    thesis_at_time: str = ""
    confidence_at_time: float = 0.0
    evidence_refs: list[str] = Field(default_factory=list)
    review_notes: list[str] = Field(default_factory=list)  # 事后复盘
