# AlphaBee 七因子数据获取设计文档（F / E / T / V / C / R / M）

> 关联文档：
> - `docs/midterm/MIDTERM_S1_S4.md` — 因子框架与 S1–S4 状态机定义
> - `docs/midterm/MIDTERM_INVESTMENT_ROADMAP.md` — 落地路线图（7.2/7.3/7.4 为本文档的数据基座规划）
> - `alphabee/schemas/INDEX.yaml` — canonical 字段索引入口
>
> 本文件是「**七因子数据从哪来、叫什么、怎么变成信号**」的权威实现参考。

---

## 0. 设计原则（不可违反）

1. **下游只认 AlphaBee 内部字段**：外部字段名只允许出现在 `adapters/` 与 collector/fetcher 层，
   不得泄漏到 `derived_facts/`、`signals/`、`thesis/`、报告层（`alphabee-schema-steward`）。
2. **链路单向**：
   ```text
   外部数据源字段 → adapter/mapping → canonical 字段 → derived facts → signals → thesis → state
   ```
3. **降级契约**：一致预期/拥挤度/相对强度等**缺失时显式产出 `*_missing` issue**，不静默回退为 0 或中性
   （区别于 `INDEX.yaml` 里长期挂 gap 的字段，见 ROADMAP 7.2 注释）。
4. **接口契约以实测为准**：每个新接口先冒烟打印 `columns` + `head`，再写 mapping；不凭接口名猜参数
   （`alphabee/data-source-contract`）。

---

## 1. 七因子 → 数据域总览

| 因子 | 含义 | 承载域（canonical） | 主数据源 | 状态 |
|------|------|--------------------|---------|------|
| **F** | 基本面及其变化 | `financial` + `operation` + `company` | Tushare 财报四接口 | ✅ 已较全 |
| **E** | 市场预期及 Revision | `expectation`（预告/快报）+ **新增 `consensus`** | 东财研报列表 + Tushare `report_rc` | ⚠️ 缺一致预期 |
| **T** | 价格/相对强度趋势 | `market`（价量/均线）+ **新增 RS 字段** | Tushare `daily`/`index_daily` | ⚠️ 缺相对强度 |
| **V** | 估值与赔率 | `market`（估值）+ `industry`（行业/对标） | Tushare `daily_basic`/`index_dailybasic` | ✅ 较全，缺分位 |
| **C** | 拥挤程度 | `market`（换手/资金流）+ **新增 `crowding`** | 巨潮股东户数 + 东财人气榜/研报 + Tushare 持仓/两融/龙虎榜 | ❌ 基本空白 |
| **R** | 个股/产业风险 | `risk` + `financial`（杠杆/商誉） | Tushare 质押/回购/薪酬 + AkShare 新闻 | ✅ 个股较全，缺产业 |
| **M** | 市场环境 | `market_regime` | AkShare 宏观/宽度 + Tushare 指数/利率/两融 | ✅ 已较全 |

---

## 2. F — Fundamental（基本面及其变化）

### 2.1 因子定位
S2 的 `RevenueInflection / MarginInflection / Margin↑ / IndustryData↑`，S3 的 `F↑` 共振。

### 2.2 数据源与工具

| 层 | 接口/字段 | 频率 | 说明 |
|----|----------|------|------|
| 利润表 | Tushare `income` → `total_revenue/operate_profit/n_income/ebitda/basic_eps/interest_expense` | 季/年 | `get_financial_fact` |
| 资产负债表 | Tushare `balancesheet` → `total_assets/total_liab/total_hldr_eqy_exc_min_int/money_cap/total_cur_assets/total_cur_liab/accounts_receiv/inventories/goodwill` | 季/年 | 同上 |
| 现金流量表 | Tushare `cashflow` → `n_cashflow_act/n_cashflow_inv_act/n_cash_flows_fnc_act/c_pay_acq_const_fiolta/c_pay_dist_dpcp_int_pvd` | 季/年 | 同上 |
| 财务比率 | Tushare `fina_indicator` → `roe/roa/grossprofit_margin/netprofit_margin/current_ratio/quick_ratio/debt_to_assets/or_yoy/netprofit_yoy/basic_eps_yoy/fcff/...` | 季/年 | 同上 |
| 主营构成 | Tushare `fina_mainbz` / AkShare `stock_zygc_em` → `biz_segment_*` | 半年/年 | `get_operation_fact` |
| 同行对比 | 本地 `get_industry_peers` + Tushare `daily_basic`/`fina_indicator` → peer ROE/毛利率/估值 | 季 | `get_competition_fact` |

### 2.3 canonical 字段（已有）
`financial.yaml` 30 字段 + `operation.yaml` 5 字段，见 `alphabee/schemas/financial.yaml` / `operation.yaml`。

### 2.4 派生因子 / 信号（已有）
派生（`alphabee/agents/derived_facts/rules/`，21 条）：
`revenue_growth`、`profit_leverage`、`gross_margin_trend`、`roe_level`、`cashflow_quality`、
`asset_turnover`、`capex_intensity`、`current_ratio`、`debt_ratio`、`interest_coverage`、
`inventory_pressure`、`receivable_pressure`、`accounts_receivable_yoy/growth`、`receivable_growth_gap`、
`goodwill_risk`、`dividend_coverage`、`market_share_change`、`peg_ratio`、`valuation_compression`、`pb_roe_match`。

信号（`alphabee/agents/signal/rules/`）：
`growth_quality_risk`、`profitability_quality_risk`、`revenue_quality_risk`、`cashflow_quality_risk`、
`debt_risk`、`capital_efficiency_risk`、`moat_erosion_risk`、`expansion_risk` + 9 条财务造假 anomaly 模式。

### 2.5 缺口
- 计算字段（非直接来源，`INDEX.yaml` field_gaps）：`ebit`、`avg_shareholders_equity`、`inventory_yoy`、`accounts_receivable_prev`、`gross_margin_prev`——由 `financial_fact.py` 的 `extract_financial_facts` 跨期计算填充。
- 基本面「变化」的二阶信息（拐点、加速度）目前靠派生因子的一阶同比，无显式拐点检测。

---

## 3. E — Expectation（市场预期及 Revision）

### 3.1 因子定位
文档第十二节将 `RevisionMomentum` 定为**核心因子**：`Revision_1M / Revision_3M / RevisionBreadth / RevisionAcceleration`。
S2 的 `EPSRevision↑`、S3 的 `TargetPrice↑ + Revision↑`、S4 的 `Revision↑↑→↑→Flat`。

### 3.2 现状：只有「公告类预期」，缺「分析师一致预期」

**已有（`expectation.yaml` 14 字段）**：
| 层 | 接口 | canonical 字段 |
|----|------|---------------|
| 业绩预告 | Tushare `forecast` | `forecast_type/profit_forecast_min_change/profit_forecast_max_change/forecast_net_profit_min/max/last_year_net_profit/forecast_summary/change_reason` |
| 业绩快报 | Tushare `express` | `express_revenue/express_net_profit/express_revenue_yoy/express_net_profit_yoy/express_diluted_eps/express_diluted_roe` |

工具：`alphabee/agents/facts/tools/expectation_fact.py::get_expectation_fact`。

### 3.3 新增：`consensus.yaml`（分析师一致预期域）

**主数据源：东方财富研报列表接口**（实测可用，无需 token）：

```text
GET https://reportapi.eastmoney.com/report/list
参数：cb, pageNo, pageSize, code(6位), industryCode='*', beginTime, endTime, qType=0
返回（JSONP 包裹）：data[] 每条含
  predictThisYearEps   → FY1 每股收益预测（实测覆盖率 25/26）
  predictNextYearEps   → FY2 每股收益预测（25/26）
  predictNextTwoYearEps→ FY3 每股收益预测（24/26）
  predictThisYearPe / predictNextYearPe / predictNextTwoYearPe
  indvAimPriceT        → 目标价（实测覆盖率仅 10/26 ≈ 38%，需降级）
  emRatingName         → 评级（买入/增持/中性/减持，实测 26/26）
  orgSName / orgName   → 机构
  researcher           → 研究员
  publishDate          → 发布日期
  ratingChange         → ⚠️ 恒为 3，是东财分类码，**不能**当上/下调方向
  infoCode / encodeUrl → 详情/PDF 标识
```

> 已有代码：`alphabee/collectors/eastmoney/helper.py::EastmoneyHelper.process_data` 已解析上述字段，
> 但 `financial_report/links.py::get_research_report_links` 只暴露了 title/date/org/researcher/rating/url，
> **EPS/目标价字段被丢弃**，需补齐。

**兜底/交叉验证源：Tushare `report_rc`**（券商盈利预测，2010 起、每晚更新，需积分）。

### 3.4 consensus canonical 字段设计

```yaml
# alphabee/schemas/consensus.yaml（新增域）
eps_fy1:               { category: consensus, type: float, unit: CNY_PER_SHARE, frequency: [weekly] }
eps_fy2:               { category: consensus, type: float, unit: CNY_PER_SHARE, frequency: [weekly] }
eps_fy3:               { category: consensus, type: float, unit: CNY_PER_SHARE, frequency: [weekly] }
target_price:          { category: consensus, type: float, unit: CNY, frequency: [weekly] }
rating_mean:           { category: consensus, type: float, unit: RATING, frequency: [weekly] }  # 买入1/增持2/中性3/减持4/卖出5
coverage_count:        { category: consensus, type: int, unit: COUNT, frequency: [weekly] }
eps_fy1_revision_1m:   { category: consensus, type: float, unit: PERCENT, frequency: [weekly] }
eps_fy1_revision_3m:   { category: consensus, type: float, unit: PERCENT, frequency: [weekly] }
eps_fy2_revision_1m:   { category: consensus, type: float, unit: PERCENT, frequency: [weekly] }
revision_breadth:      { category: consensus, type: float, unit: RATIO, frequency: [weekly] }   # 上调机构占比 0-1
revision_acceleration: { category: consensus, type: float, unit: PERCENT, frequency: [weekly] } # revision 二阶差分
rating_upgrade_1m:     { category: consensus, type: int, unit: COUNT, frequency: [weekly] }
rating_downgrade_1m:   { category: consensus, type: int, unit: COUNT, frequency: [weekly] }
```

**聚合规则**（`alphabee/collectors/consensus/`）：
1. 拉个股近 N 个月全量研报列表（翻页）；
2. 按报告期聚合同日 `eps_fy1` 中位数 = 一致预期 EPS；
3. `eps_fy1_revision_1m = (今日一致EPS / 30日前一致EPS - 1)`，`3m` 同理；
4. `revision_acceleration = revision_1m_t - revision_1m_{t-1}`；
5. `rating_mean` = 评级编码均值；`revision_breadth` = 按 `(机构,股票)` 分组对相邻两篇研报评级做差分（不能读 `ratingChange`）；
6. `target_price` = 有值子集中位数；覆盖率 < 某阈值时显式 `consensus_missing: target_price`。

### 3.5 缺口
- 目标价覆盖率低（~38%），需降级；
- 中小盘/冷门股 `coverage_count` 低（分析师覆盖少），需 `coverage_count` 阈值判断。

---

## 4. T — Trend（价格/相对强度趋势）

### 4.1 因子定位
文档 Snapshot 的 `trend.rs_20d/rs_60d`，S1 第八节的「个股跌、行业不跌 → RS 恶化」，S3 的 `RS↑`。

### 4.2 现状：有价量与均线，缺相对强度（RS）

**已有（`market.yaml`）**：`close_price/open/high/low/volume/turnover_amount/price_change_pct`（Tushare `daily`），
`ma5/ma10/ma20/ma60/ma120`（`get_market_fact` 由 `close_price` 序列计算）。

**已有行业/市场基准**：`industry_close`（Tushare `index_daily`/`sw_daily`）、`hs300_close`（market_regime）。

### 4.3 新增：相对强度字段（并入 `market.yaml` / `industry.yaml`）

```yaml
rs_stock_market:     { category: market, type: float, unit: PERCENT, frequency: [daily] }  # 个股相对全市场 20/60 日超额收益
rs_stock_industry:   { category: market, type: float, unit: PERCENT, frequency: [daily] }  # 个股相对行业超额收益
rs_industry_market:  { category: industry, type: float, unit: PERCENT, frequency: [daily] } # 行业相对全市场超额收益
industry_breadth_20: { category: industry, type: float, unit: PERCENT, frequency: [daily] } # 行业成分股 P>MA20 占比
industry_breadth_60: { category: industry, type: float, unit: PERCENT, frequency: [daily] }
```

**计算**：`rs_stock_market_20d = (个股20日收益) - (沪深300 20日收益)`，`rs_stock_industry_20d = 个股收益 - 行业指数收益`。
数据全部来自**已有**接口（`daily`、`index_daily`、`hs300_close`），无需新数据源，只新增派生计算。

### 4.4 缺口
- 无趋势/动量 signal 规则（`signal/rules/` 全是财务/估值/异常），需新增 `trend_break_risk` / `rs_deterioration` 信号；
- 「20 周线」无现成口径，可用 `ma60` 近似或自建周线聚合。

---

## 5. V — Valuation（估值与赔率）

### 5.1 因子定位
文档 Snapshot 的 `valuation.percentile`，S4 的「Valuation 扩张」，S1 的「估值已充分反映坏消息」。

### 5.2 现状：估值绝对量 + 5 年均值，缺历史分位

**已有**：`pe_ttm/pb_ratio/market_cap/circulating_market_cap`（Tushare `daily_basic`）、`pe_ttm_5y_avg`
（`get_market_fact` 拉 5 年 `daily_basic(pe_ttm,pb)` 算均值）、`industry_pe_ttm/industry_pb`
（Tushare `index_dailybasic`）、`peer_median_pe_ttm/peer_median_pb`（`get_competition_fact`）。

**派生/信号（已有）**：`peg_ratio`（= pe_ttm/net_profit_yoy）、`valuation_compression`（= pe_ttm/pe_ttm_5y_avg）、
`pb_roe_match` → 信号 `valuation_risk`。

### 5.3 新增：个股历史估值分位

```yaml
pe_ttm_5y_percentile: { category: market, type: float, unit: RATIO, frequency: [daily] }  # 0-1 历史分位
pb_5y_percentile:     { category: market, type: float, unit: RATIO, frequency: [daily] }
```

**计算**：`get_market_fact` 已拉 5 年 `daily_basic(pe_ttm,pb)` 历史（`daily_basic_history_df`），
只需在 adapter 层把「均值」升级为「当前值在历史序列中的分位」。无新数据源。

### 5.4 缺口
- 赔率/Expected Value（`EV = Σ P·R`、`RiskAdjustedEV`，文档第四十二节）是决策层概念，需在
  `alphabee/midterm/models.py`（ROADMAP 7.5 已规划）落地，非数据获取问题；
- 成长股 vs 价值股溢价、行业估值相对全市场分位，可复用 market_regime 的 `all_market_pe_10y_percentile` 组合。

---

## 6. C — Crowding（拥挤程度）

### 6.1 因子定位
S4 的「Crowding↑」、S3 的「机构 Position↑」。拥挤度回答：**谁已建仓（持仓）· 交易多热（流量）·
多少人盯着（关注）· 筹码集中度（结构）· 估值多极端（估值）· 供给多压（供给）**。

### 6.2 现状：仅弱代理
`turnover_rate`（`daily_basic`）、`main_force_inflow/super_large_order_flow/large_order_flow/retail_flow`
（Tushare `moneyflow`）、`top10_holder_ratio`（Tushare `top10_holders`）、
全市场 `margin_balance`（market_regime）。**无拥挤度评分/信号。**

### 6.3 数据源矩阵（含实测状态）

| 维度 | 指标 | 数据源 | 频率 | 实测/积分 |
|------|------|--------|------|-----------|
| C1 持仓 | 公募基金持仓 | Tushare `fund_portfolio`；AkShare `stock_report_fund_hold` | 季 | 需积分 |
| C1 持仓 | 北向（沪深股通）持股 | Tushare `hk_hold`；AkShare `stock_hsgt_*` | 日 | 需积分 |
| C1 持仓 | 前十大流通股东 | Tushare `top10_floatholders`；`top10_holders`（已有） | 季 | 部分已有 |
| C1 持仓 | 个股两融余额 | Tushare `margin_detail`；AkShare `stock_margin_detail_sse(date)` | 日 | 需积分 |
| C2 交易 | 换手率（绝对/分位） | `turnover_rate`（已有）+ 自算分位 | 日 | 绝对✅ |
| C2 交易 | 成交额占全市场比 | 个股 `turnover_amount` ÷ `market_turnover`（已有） | 日 | 可组合 |
| C2 交易 | 龙虎榜（机构/游资席位） | Tushare `top_list`/`top_inst`；AkShare `stock_lhb_detail_em(start,end)` | 日 | 需积分 |
| C2 交易 | 游资明细 | Tushare `hm_detail`（2022.8 起） | 日 | 需积分 |
| C3 关注 | 研报覆盖热度 | 东财研报列表（`EastmoneyHelper` 已有） | 日 | ✅ 已有地基 |
| C3 关注 | 机构调研频次 | Tushare `stk_surv` | 事件 | 需积分 |
| C3 关注 | 东财人气榜 | AkShare `stock_hot_rank_em()` | 日 | ✅ **实测可用**（top100） |
| C3 关注 | 同花顺热榜 | Tushare `ths_hot` | 日 | 需积分 |
| C3 关注 | 券商月度金股 | Tushare `broker_recommend` | 月 | 需积分 |
| C4 筹码 | 股东户数/人均持股 | AkShare `stock_hold_num_cninfo(date=)`；Tushare `stk_holdernumber` | 季 | ✅ **实测可用**（5110 只） |
| C4 筹码 | 筹码分布/获利盘/平均成本 | Tushare `cyq_chips`/`cyq_perf`；AkShare `stock_cyq_em(symbol,adjust)` | 日 | ⚠️ 沙箱 ProxyError，生产需确认代理 |
| C5 估值 | 个股 PE/PB 历史分位 | `daily_basic` 历史自算（见 V 因子） | 日 | 缺 |
| C6 供给 | 限售解禁 | Tushare `share_float` | 事件 | 需积分 |
| C6 供给 | 股东增减持 | Tushare `stk_holdertrade` | 事件 | 需积分 |
| C6 供给 | 股权质押 | `pledge_ratio`（已有 risk_fact） | 季 | ✅ |

> 接口签名（实测确认）：`stock_hold_num_cninfo(date='20240930')`（**参数是 date 不是 symbol**，
> 返回全市场某日快照，含 `本期股东人数/上期股东人数/股东人数增幅/本期人均持股数量/人均持股数量增幅`）；
> `stock_hot_rank_em()`（无参，返回 top100）；`stock_cyq_em(symbol, adjust)`。

### 6.4 新增：`crowding.yaml` 字段设计（对齐 ROADMAP 7.3 并扩展）

```yaml
# alphabee/schemas/crowding.yaml（新增域）
turnover_rate:               { category: crowding, type: float, unit: PERCENT, frequency: [daily] }
turnover_rate_percentile:    { category: crowding, type: float, unit: RATIO, frequency: [daily] }   # 历史换手率分位
amount_pct_of_market:        { category: crowding, type: float, unit: PERCENT, frequency: [daily] } # 成交额占全市场比
holder_count_change:         { category: crowding, type: float, unit: PERCENT, frequency: [quarterly] } # 股东户数增幅
per_capita_holding_change:   { category: crowding, type: float, unit: PERCENT, frequency: [quarterly] } # 人均持股增幅
institutional_holding_ratio: { category: crowding, type: float, unit: PERCENT, frequency: [quarterly] } # 前十大集中度
margin_balance_yoy:          { category: crowding, type: float, unit: PERCENT, frequency: [daily] }     # 两融余额同比
analyst_coverage_rank:       { category: crowding, type: int, unit: RANK, frequency: [weekly] }        # 研报覆盖热度排名
hot_rank:                    { category: crowding, type: int, unit: RANK, frequency: [daily] }         # 人气榜排名（无上榜=缺失）
news_heat:                   { category: crowding, type: float, unit: SCORE, frequency: [daily] }      # 新闻热度 0-100
leader_concentration:        { category: crowding, type: float, unit: PERCENT, frequency: [daily] }    # 龙头成交集中度
```

**优先级建议（免费/已有地基优先）**：
1. `holder_count_change` / `per_capita_holding_change`（`stock_hold_num_cninfo`，免费）——筹码集中度；
2. `hot_rank`（`stock_hot_rank_em`，免费）+ `analyst_coverage_rank`（`EastmoneyHelper` 已有）——关注度；
3. `turnover_rate_percentile` / `amount_pct_of_market`（已有数据自算）——交易热度。

---

## 7. R — Risk（个股/产业风险）

### 7.1 因子定位
文档 S1 的「Thesis/Time Stop」、`EmergencyRiskStop`（造假/审计/监管/黑天鹅）。

### 7.2 现状：个股风险较全

| 层 | 接口 | canonical 字段 |
|----|------|---------------|
| 股权质押 | Tushare `pledge_stat` | `pledge_ratio/pledge_count/unreleased_pledge/released_pledge` |
| 回购 | Tushare `repurchase` | `repurchase_volume/amount/progress/high_limit/low_limit` |
| 高管薪酬 | Tushare `stk_rewards` | `executive_name/title/reward` |
| 新闻舆情 | AkShare `stock_news_em` | `news_title/news_publish_time` |
| 财务风险 | `financial.yaml` | `goodwill/debt_to_assets/interest_bearing_debt` |

工具：`get_risk_fact`。信号：`debt_risk`/`goodwill_risk`/`cashflow_quality_risk` + 9 条财务造假 anomaly 模式。

### 7.3 缺口
- **产业风险缺失**：无行业级风险字段（行业景气拐点、政策监管、供需恶化），建议新增
  `industry_risk` 域或复用 `industry.yaml` + 定性 LLM 判断；
- **违规/处罚未实现**：`get_risk_fact` docstring 声称「重大违规/处罚」，但代码只取
  news/pledge/repurchase/rewards，未接入 Tushare `penalty`（违规处罚）——文档与实现不一致，需补齐；
- 审计异常靠 anomaly 引擎启发式模式间接代理，非直接来源。

---

## 8. M — Market（市场环境）

### 8.1 现状：已较完整
`market_regime.yaml` 28 字段 + `alphabee/market_regime/` 引擎：

| 引擎 | 输入 | 来源 |
|------|------|------|
| 估值(30%) | `hs300_pe_ttm/pb`、`cs500/cs1000_*`、`all_market_pe_ttm/pb`、10 年分位、ERP | AkShare `stock_index_pe_lg/pb_lg/stock_a_ttm_lyr/stock_a_all_pb`；Tushare `index_dailybasic` |
| 趋势(40%) | `hs300_close/ma20/ma60/ma250`、`breadth_above_ma60_pct`、`up_stock_ratio` | AkShare `stock_zh_index_daily/stock_market_activity_legu` |
| 流动性(30%) | `cn_10y_yield`、`shibor_3m`、`m1_yoy/m2_yoy/m1_m2_gap`、`social_financing_increment` | AkShare `bond_china_yield/bond_zh_us_rate/macro_china_shibor_all/macro_china_money_supply/macro_china_shrzgm`；Tushare `cn_m/sf_month/shibor` |
| 风险偏好(±5) | `market_turnover`、`margin_balance`、`etf_net_inflow` | AkShare `stock_sse_deal_daily/stock_szse_summary/stock_margin_sse/stock_margin_szse`；Tushare `margin` |

聚合：`valuation×30% + trend×40% + liquidity×30%` → `market_score` → `regime_classifier` → `position`。

### 8.2 缺口（`INDEX.yaml` field_gaps，已登记）
`cyb_pe_ttm/cyb_pb/cyb_ep_ttm`（创业板估值）、`social_financing_yoy`（社融存量同比）、
`breadth_above_ma60_pct`、`nh_nl_diff`（需全市场日线/52 周高低，暂无低成本接口）、
`etf_net_inflow`（无稳定免费源）。

---

## 9. 落地分期建议

| 期 | 内容 | 依赖 |
|----|------|------|
| **P0（数据基座，最高优先）** | ① 建 `consensus.yaml` + 东财研报 adapter 补齐 EPS/目标价字段 + `collectors/consensus/` 聚合引擎；② 建 `crowding.yaml` + 接入 `stock_hold_num_cninfo`/`stock_hot_rank_em` + `EastmoneyHelper` 覆盖热度 | E/C 是状态机跑通的短板 |
| **P1** | ③ 相对强度 RS 字段（纯自算，零新数据源）；④ 个股估值分位 `pe_ttm_5y_percentile`（自算） | T/V |
| **P2** | ⑤ Tushare 持仓/两融/龙虎榜（`fund_portfolio/hk_hold/margin_detail/top_list`，需积分）；⑥ `penalty` 违规处罚接入（R 补齐） | C/R |
| **P3** | ⑦ 拥挤度/RS/趋势 signal 规则 + `midterm/models.py` 状态机 + 赔率 EV | 全部 |

---

## 10. 数据源接口契约速查（实测/官方核对）

| 接口 | 来源 | 关键参数 | 关键出参 | 状态 |
|------|------|---------|---------|------|
| `reportapi.eastmoney.com/report/list` | 东财 | `code`(6位), `beginTime/endTime`, `pageNo/pageSize` | `predictThisYearEps/NextYearEps/NextTwoYearEps/indvAimPriceT/emRatingName/orgSName/publishDate` | ✅ 实测（EPS~96%、目标价~38%） |
| `report_rc` | Tushare | 券商盈利预测 | 2010 起，需积分 | 兜底源 |
| `stock_hold_num_cninfo(date)` | AkShare(巨潮) | `date='YYYYMMDD'`（**非 symbol**） | 全市场快照：本期/上期股东人数、增幅、人均持股 | ✅ 实测 5110 只 |
| `stock_hot_rank_em()` | AkShare(东财) | 无参 | top100：排名/代码/名称/涨跌幅 | ✅ 实测 |
| `stock_cyq_em(symbol, adjust)` | AkShare(东财) | `symbol`(6位) | 筹码分布 | ⚠️ 沙箱 ProxyError，生产需确认代理 |
| `stock_lhb_detail_em(start,end)` | AkShare(东财) | 日期范围 | 龙虎榜 | 免费 |
| `stock_margin_detail_sse(date)` | AkShare | `date` | 上证个股两融 | 免费 |
| `fund_portfolio` / `hk_hold` / `margin_detail` / `top_list` / `top_inst` / `hm_detail` / `cyq_chips` / `cyq_perf` / `stk_surv` / `ths_hot` / `share_float` / `stk_holdertrade` / `stk_holdernumber` / `broker_recommend` / `ccass_hold` | Tushare | 见 `skills/tushare/references/数据接口.md` | 持仓/两融/龙虎榜/游资/筹码/调研/热榜/解禁/增减持 | 需不同积分，落地前逐接口冒烟 |

---

## 附：待办落地清单（checklist）

- [ ] `alphabee/schemas/consensus.yaml`（E）
- [ ] `alphabee/schemas/crowding.yaml`（C）
- [ ] `alphabee/adapters/eastmoney/research_report_mapping.yaml`（东财研报 → consensus canonical）
- [ ] `alphabee/adapters/akshare/crowding_mapping.yaml`（股东户数/人气榜 → crowding canonical）
- [ ] `alphabee/collectors/consensus/`（研报聚合 + revision 计算）
- [ ] `alphabee/collectors/crowding/`（股东户数/人气榜/覆盖热度采集）
- [ ] RS 相对强度字段 + 计算（T，并入 market/industry）
- [ ] 个股估值分位字段 + 计算（V，并入 market）
- [ ] `signal/rules/trend_break_risk.yaml`、`crowding_risk.yaml`（新增信号）
- [ ] `alphabee/midterm/models.py`（ROADMAP 7.5 typed contracts）
