# AlphaBee 中期投资决策模型 Roadmap（预期差 · 认知状态机 · 三层仓位）

> **定位（与既有 Roadmap 的三角 + 决策层分工）**
>
> 本仓库此前把「公司语境」拆成三层，本文档在其上新增一层**决策层**：
>
> ```text
> 语境注入栈（可计算数值 + 定性叙事）
> ├─ MARKET_REGIME_ROADMAP       市场状态（M，市场级，独立 graph，已实现 Phase 0-2）
> ├─ industry-context-injection  申万行业基线（industry_*）
> ├─ COMPANY_TRACK_ROADMAP       公司级覆盖（业务线解构 + 对标组 peer_*）
> └─ DOMAIN_CONTEXT_ROADMAP      定性叙事层（primitives / playbooks / ContextRouter）
>
> 决策层（本文档，新增，消费上面全部产物）
> └─ MIDTERM_INVESTMENT_ROADMAP  3–12 个月中期投资决策模型：
>     S0–S5 认知状态机 + M/F/E/T/V/C/R 七变量 + 预期差 + 三层仓位 + 贝叶斯证据加仓
> ```
>
> 依赖方向：本文档**只消费**上面几层的产物（`MARKET_REGIME` artifact、`COMPANY_TRACK` /
> `INDUSTRY_CONTEXT` artifact、`DRIVER_PROFILE` artifact、`peer_*` / `industry_*` fact_values、
> `business_model` archetype），不重复建设它们已覆盖的「数值 / 结构化标签 / 定性叙事」部分。

> **实现状态（2026-08 与代码对齐）**
> - 本文档为**新增决策层设计**，`alphabee/midterm/` 尚未落地；代码中无 `S0-S5` 状态、
>   `ExpectationGap`、`PositionDecision`、`Consensus`（分析师一致预期）、`Crowding` 产物。
> - 但多数**上游燃料已就位**：`market_regime`（M 变量 + 仓位档位）、`company_track`
>   （F 变量的赛道/对标组骨架）、`derived_facts`（F/V 的部分规则）、`signal`（R 变量的
>   风险信号）、`thesis`（8 维度）、`insight`（core_view/central_tension/main_driver）。
> - 最近似的「持续跟踪」能力是 `alphabee/workflow/framework_monitor.py`（snapshot + stage +
>   alerts + metric_changes），但它是**独立 workflow**，不与 orchestrator 集成，且无 S0-S5
>   认知状态、无预期差、无仓位——本文档的 S0-S5 状态机正是要把它升级为「带预期差与仓位的
>   认知状态机」并纳入主决策链路。

---

## 1. 背景判断：我们换的是一套决策模型，不是换几个指标

原有体系近似：

```text
价格走势（站上 120 日线） → 仓位决策
```

新体系应为：

```text
市场环境 → 产业/公司变化 → 预期差 → 新证据 → 市场确认 → 仓位调整 → 预期兑现/证伪
```

两者本质区别在于：**「价格突破均线 → 买」是价格→仓位的两段式；新体系是
「环境 → 预期差 → 证据 → 仓位 → 证伪」的闭环决策模型**。这是本文档要落地的东西，
不是对既有信号规则的参数调优。

### 1.1 我们到底在赚什么钱（三层收益）

3–12 个月 A 股中期投资赚的是三件事的叠加，而不是「突破均线后继续上涨」：

```text
股价收益 ≈ 盈利增长（ΔFundamental > 0）
         + 盈利预期上修（EarningsRevision > 0，核心层）
         + 估值扩张（PE Expansion，20X → 25X → 30X）
```

真正要寻找的标的画像：

```text
基本面改善 + 预期上修 + 尚未充分定价
```

而不是「站上 120 日线」。迈瑞医疗正是本文档的贯穿示例：**低估值本身不是买点；
「低估值 + 基本面边际改善」让它进入 S1，而真正值得明显提高仓位，要等「盈利预期
停止下修并转向上修」把它推进 S2/S3。**

### 1.2 为什么 A 股尤其适合这样分析

A 股中期行情常见传导链，且最佳买点通常在前段而非共识段：

```text
政策/产业变化 → 高频数据改善 → 产业链感受变化 → 股价提前反应 → 卖方预测上修
→ 机构增配 → 行业形成趋势 → 市场共识 → 散户/趋势资金进入 → 估值扩张
→ 交易拥挤 → 预期透支

最佳区域：产业改善 → 市场半信半疑 → 盈利预测开始上修（赔率+胜率兼顾）
牺牲区：所有人都知道了 → 股价已涨很多 → 技术全面多头 → 媒体大量讨论（赔率恶化）
```

---

## 2. 产品定位与差异化：AlphaBee = 投资操作系统，不是自动写研报

### 2.1 核心判断：不要做成「股票研究 Agent」

如果 AlphaBee 的终点只是「输入股票 → 搜索 → 分析 → 生成 20 页报告」，它很容易被
ChatGPT / Claude / Gemini 的 Deep Research 直接替代——基础模型会天然越来越强。AlphaBee
的价值不应建立在「比 ChatGPT 单次分析写得更长、更专业」上。

重新定义产品：从「AI 投资研究 Agent」→ **AI Investment Decision Support System**（持续
维护投资认知、监控证据变化并辅助仓位决策的个人投资操作系统）。

两者不是「AlphaBee vs ChatGPT」，而是分工：

```text
ChatGPT = Analyst     （临时深度分析、解释、推理、形成 Thesis、讨论反例）
AlphaBee = Investment Operating System
           （数据持续更新、状态持久化、证据追踪、Thesis 版本管理、
             状态转换、自动触发、组合管理、历史复盘、决策纪律）
```

一句话定位：

```text
ChatGPT 帮你「想清楚一次」，AlphaBee 帮你「长期保持想清楚」。
```

### 2.2 五个 ChatGPT 单次对话天然不擅长的能力

| # | 能力 | Prompt 模式的缺陷 | AlphaBee 的解法 |
|---|---|---|---|
| 1 | **持续性（Time Series of Belief）** | 两次问答间没有 StateTransition，认知不连续 | 持久化 `Belief_t`，而非只存研报 |
| 2 | **自动发现变化（EventDriven）** | UserDriven：你问才分析 | 信息变化 → 投资状态变化 → 提醒（不是新闻推送） |
| 3 | **约束纪律（反 ThesisDrift）** | 每次对话都能重新讲一个合理故事 | Thesis 版本管理 + 不可漂移的买入理由 + Invalidation |
| 4 | **组合级决策（PortfolioDecision）** | 只做 StockAnalysis | 统一算 ExpectedReturn/Risk/Correlation/Exposure → Weight_i |
| 5 | **规模化覆盖（50–100 家）** | 人工撑不住 10 只以上 | 只把「发生认知变化的标的」推给你，而非推新闻 |

核心资产不是报告，而是：

```text
Company Model / Fundamental Drivers / Hypotheses / Evidence Graph /
Expectation History / Thesis History / Investment State History /
Position History / Decision History
```

尤其重要的是 **`Snapshot_t − Snapshot_{t−1}`（发生了什么变化）**——这远比每次重新生成
一篇报告有价值。

### 2.3 最终闭环（替代 Question → Report）

```text
Discover → Research → Form Thesis → Monitor → Update Belief → Allocate → Review
```

### 2.4 三个核心产品模块（砍掉 70% 后保留）

1. **Opportunity Radar**（机会雷达）：回答「现在最值得我看的机会是什么」——从几十家公司
   发现新预期差 / 基本面拐点 / EPS Revision / 估值错配 / 异常价格信号。
2. **Thesis Monitor**（论点监控）：回答「我已持有的股票，逻辑有没有变化」——Thesis /
   Evidence / Confidence / Invalidation / State Change。
3. **Portfolio Allocator**（组合配置）：回答「有限的钱应该放在哪里」——总仓位 / 单股仓位 /
   行业暴露 / 风险集中 / 应增应减。

### 2.5 人机分工（Human-in-the-loop，现阶段不做全自动）

```text
Machine：Data + Monitoring + Consistency + Search + Quant
LLM：    Reasoning + Hypothesis + Conflict Analysis
Human：  Final Judgment + Risk Preference + Capital Allocation

AlphaBee 发现 → 分析 → 建议 → 你审阅关键证据 → 你决定
```

### 2.6 决策日志（Decision Journal）与「知道自己不知道」

- **反 ThesisDrift**：保存「买入理由 + 证伪条件」，三个月后系统能说「你当初定义的第 2、3
  项证伪条件已经发生」，而不是任由人把逻辑漂移成新的合理故事。
- **Unknowns → ResearchTask**：维护关键未知（如「恺英《烈焰觉醒》Q3 流水 / 投放 ROI」），
  并主动转成研究任务——Deep Research Agent 的价值是「寻找决定结论的不确定项」，不是
  「把已知信息重新总结」。

> 以上定位直接决定本文档 Phase 优先级：**数据采集、Evidence/Hypothesis/Thesis、Expectation
> Revision、Investment State、Snapshot Diff、Event Trigger、Portfolio Position、Decision
> Journal 排在前；报告生成（展示层）排在后**。对应架构见 §6，落地见 Phase 2–4。

---

## 3. 顶层原则（五句话，作为本模块的顶层约束）

```text
预期差开仓 → 证据加仓 → 共识持有 → 透支减仓 → 证伪退出
```

- **预期差开仓**：`YourExpectation > ConsensusExpectation` 时才能首次买入（S1），且只能试探仓。
- **证据加仓**：加仓的唯一依据是「新证据提高了 P(Thesis)」，而不是「盈利了」或「更便宜了」。
- **共识持有**：S3 阶段尽量少交易，最大风险是被短期波动洗出去。
- **透支减仓**：股价上涨速度 > 盈利上修速度、估值进入高分位、拥挤度上升 → 减仓。
- **证伪退出**：Thesis Broken（我错了）或 Alpha Exhausted（我对了但大家都知道了）→ 清仓。

> 纪律保留：**价格不是老板，但是重要证人**。基本面看起来没问题、但 `StockRS↓` +
> `IndustryRS↓` + 成交量异常 + 价格持续创新低，不能简单归因「市场错了」，必须
> 触发 `PriceAnomaly → GenerateQuestion → Research → UpdateThesis` 的再研究闭环。

---

## 4. 核心概念与术语表

### 4.1 五个认知状态（S0–S5，替代「时间阶段」）

核心主张：**时间 ≠ 信息**。一个标的可能两周完成预期重估，也可能横盘半年。

| 状态 | 含义 | 核心问题 | 仓位原则 |
|---|---|---|---|
| **S0 研究候选** | 发现异常/见底/变化，但 Thesis 未建立 | 它是不是值得研究的对象？ | **0** |
| **S1 预期差形成** | 我的判断与市场共识有正向差异 | 我与共识哪里不同？ | **试探仓**（首次允许买入） |
| **S2 证据确认** | 新证据不断提高 P(Thesis\|Evidence) | 哪些证据证明我是对的？ | **逐步加仓**（证据加仓） |
| **S3 共识扩散** | 基本面改善 → 预期上修 → 机构买入 → 趋势增强 | 如何不被波动洗出去？ | **核心持有**（少交易） |
| **S4 充分定价** | 股价涨幅 > 盈利上修，收益大量来自 PE 扩张 | 还有多少赔率？ | **逐步减仓** |
| **S5 退出** | Thesis Broken（我错了）/ Alpha Exhausted（对了但大家知道了） | 什么证伪了？ | **清仓** |

**状态机不是只能向前**：允许 `S3 → S2`（基本面暂时弱化）、`S1/S2/S3 → S5`（重大证伪）。

### 4.2 七组变量（M + F + E + T + V + C + R）

| 变量 | 全称 | 回答的问题 | 在 AlphaBee 中的落点 |
|---|---|---|---|
| **M** | Market Regime | 账户该冒多大系统性风险 | `market_regime/`（已有） |
| **F** | Fundamental Trend | 经营在改善还是恶化（看边际 dF/dt） | `company_track` + `derived_facts` + DriverProfile |
| **E** | Expectation Revision | 市场对未来的预期在变好还是变差（**核心变量**） | **新增：一致预期域 + RevisionEngine** |
| **T** | Trend / Relative Strength | 价格有没有开始确认（相对强度 + 行业 Breadth） | **新增：RelativeStrengthEngine** |
| **V** | Valuation | 判断对了还有多少赔率（Cheap≠Buy, Expensive≠Sell） | `derived_facts` 估值规则 + 历史分位 |
| **C** | Crowding | 交易是否已经拥挤（A 股尤其需要） | **新增：CrowdingEngine** |
| **R** | Risk | 三层风险：Price / Fundamental / Thesis | `signal` + `thesis` reviewer 重构 |

> 若只能选一个变量，选 **E**。因为股票交易的是「未来相对于市场原预期发生了什么变化」。

### 4.3 三层仓位（明确区分三个概念）

```text
ActualPosition（个股占总资产比例）
  = PortfolioExposure（总权益仓位，由 M 决定）
  × StockWeight（权益仓内个股权重，由 f(F,E,T,V,C,R) 决定）
```

例如：市场中性 → 总权益 60%；迈瑞 S1 → 权益仓内权重 15%；实际占总资产 = 60% × 15% = 9%。
30 万账户 → 2.7 万。

### 4.4 动态贝叶斯仓位

```text
P(H)        初始 Thesis 概率（如 55%）
E1/E2/E3    新证据（海外+15.7%、国内Q2转正、EPS 上修）→ P(H|E)↑ → Position↑
E4          反证（海外增速降到 5%）→ P(H|E)↓ → Position↓
```

替代「+5% 加仓、-5% 止损」的机械规则。

---

## 5. 现状盘点（七变量 → 已有模块 → 缺口）

> 状态标记：✅ 已实现　🟡 部分实现　⬜ 未实现

| 变量 | 已有（代码位置） | 状态 | 缺口（本文档要补的） |
|---|---|---|---|
| **M** | `alphabee/market_regime/`（score_engine / regime_classifier / position） | 🟡 | Phase 3-4 未做：`explainer`、CLI、**注入个股流水线的 `load_market_regime_context` 节点**（PortfolioExposure 未与个股权重相乘） |
| **F** | `company_track/`（segments/track_label/peer_*）+ `derived_facts/`（revenue_growth / gross_margin_trend / market_share_change）+ `industry/` | 🟡 | 缺「边际变化 dF/dt」的统一刻画（二阶/拐点）；DriverProfile（DOMAIN_CONTEXT Phase 0）未落地，FundamentalModel 尚未 domain-specific 化 |
| **E** | `schemas/expectation.yaml` 只有业绩预告/快报（forecast/express）；ROADMAP Phase 4 `ExpectationFitAgent` 未实现 | ⬜ | **最大缺口**：无分析师一致预期 EPS FY1/FY2、无 Revision 1M/3M、无 `ImpliedExpectation` 反推、无 `ExpectationGap` |
| **T** | `schemas/market.yaml`（均线/行情）；`market_regime` 有市场级 breadth（`breadth_above_ma60_pct` 为 gap） | ⬜ | 无 `RS_stock-market` / `RS_stock-industry` / `RS_industry-market`；无行业级 Breadth20/60 |
| **V** | `derived_facts`（peg_ratio / pb_roe_match / valuation_compression）+ thesis `valuation_fit` 维度 | 🟡 | 缺统一「历史估值分位」序列化；「估值决定赔率不决定方向」的定位需显式进 PositionScore |
| **C** | 无 | ⬜ | **全缺口**：换手率分位、成交额占比、两融、ETF 申购、分析师覆盖、新闻热度、龙头集中度 |
| **R** | `signal/` 19 规则（风险信号）+ thesis reviewer + conflict 生命周期 | 🟡 | 需重构为 **PriceRisk / FundamentalRisk / ThesisRisk** 三层，优先级 Thesis > Fundamental > PriceNoise |
| **S0-S5 状态机** | `workflow/framework_monitor.py`（snapshot + stage + alerts） | 🟡 | 无 S0-S5 taxonomy、无预期差、无仓位、未接入 orchestrator、无跨会话状态持久化 |
| **三层仓位** | `market_regime/position.py`（只有 PortfolioExposure 档位） | ⬜ | 无 `StockWeight = f(F,E,T,V,C,R)`、无乘法、无单股上限/调仓限制 |
| **贝叶斯更新** | `core/schemas.py` `Decision.confidence`（单次，无状态） | ⬜ | 无跨会话 `P(H|Evidence)` 证据日志与更新 |

**结论**：E、C、T 三个引擎 + S0-S5 状态机 + 三层仓位是纯增量；M、F、V、R 是「已有燃料 + 需要新消费逻辑」。本文档 Phases 严格只依赖已完成部分（`market_regime` Phase 0-2、`company_track` A-F、`derived_facts`/`signal`/`thesis`/`insight` 已接入主图），不依赖未完成阶段。

---

## 6. 目标架构：`alphabee/midterm/` 决策层

### 6.1 架构立场：独立于个股流水线的「状态化决策层」

沿用 `MARKET_REGIME_ROADMAP.md` 的架构立场——**不塞进现有 LangGraph 个股流水线做普通节点，
而是独立的、状态化的决策层**。理由：

1. 它的生命周期是**周级/事件驱动 + 跨会话状态**（S0-S5 迁移、贝叶斯后验），而个股流水线是「用户触发的一次性分析」，生命周期不匹配。
2. 它要**同时消费两类上游**：市场级（`MARKET_REGIME` artifact）与个股级（`COMPANY_TRACK` / `DRIVER_PROFILE` / consensus / fact_values），塞进个股 graph 会让状态互相污染。
3. 它产出两类下游：独立的中期决策报告，以及作为**上下文注入**个股流水线（让报告带上「当前 S 阶段 + 建议仓位」）。

但复用 `alphabee/core/`（Run/Step/Artifact/Observation/Decision/Issue）与 `orchestrator/contracts.py`
的 typed artifact 约定，保证可被互相消费。

### 6.2 模块落地目录

```text
alphabee/midterm/                         ← 新增，决策层（消费 market_regime + company 流水线）
  __init__.py
  state.py                                # CompanyStateArtifact / EvidenceEvent / ExitCondition
  models.py                               # VariableScores / PositionDecision / ExpectationGap
  classifier.py                           # S0–S5 状态机（规则先行 + 迁移约束 + 允许回退）
  expectation_gap.py                      # OurForecast vs MarketImpliedExpectation → ExpectationGap
  position.py                             # 三层仓位：exposure × stock_weight + 上限 + 调仓限制
  bayes.py                                # 证据日志 → log-odds 更新 P(H|Evidence)
  explainer.py                            # Midterm Analyst Agent（LLM 解释，Phase 4）
  persistence.py                          # data/midterm/{symbol}.json 落盘（状态 + 证据日志 + 仓位）
  diff.py                                 # Snapshot_t − Snapshot_{t-1}：SNAPSHOT_DIFF（"发生了什么变化"）
  trigger.py                              # EventDriven：信息变化 → 状态变化 → 提醒（只推认知变化）
  journal.py                              # Decision Journal：买入理由 + 证伪 + 事后复盘
  portfolio.py                            # Portfolio Allocator：跨持仓 Weight_i（组合级配置）
  radar.py                                # Opportunity Radar：从覆盖池发现新预期差/拐点/错配
  rules/
    revision.yaml                         # E：EPS FY1/FY2 上修/下修/停止 → revision_score
    relative_strength.yaml                # T：RS_stock_market / RS_stock_industry / RS_industry_market + breadth
    crowding.yaml                         # C：换手率分位/成交额占比/两融/分析师覆盖/新闻热度
    valuation_percentile.yaml             # V：历史 PE/PB 分位（复用 market_regime 的分位口径）
    position_score.yaml                   # StockWeight = f(F,E,T,V,C,R) + 门槛 + 上限
    transition_matrix.yaml                # S0–S5 合法迁移（含回退），非法迁移标 suspicious
  prompts/
    explainer.py                          # Midterm Analyst Agent prompt（忠实裁决 + 降级）
  tests/
    test_classifier.py
    test_expectation_gap.py
    test_position.py
    test_bayes.py
```

数据落盘：

```text
data/midterm/
  {symbol}.json             # CompanyStateArtifact + evidence_log + position 历史（版本化，人工可编辑）
  position_history.csv      # 周级仓位建议历史（供回测）
  state_history.csv         # S0–S5 迁移记录（date / from / to / confidence / transition_valid）
```

### 6.3 与个股流水线的关系（注入而非替代）

个股流水线新增 `load_market_regime_context`（MARKET_REGIME Phase 4.3，已有计划）与
`resolve_midterm_state`（本文档 Phase 4）两个节点，默认**不阻塞个股分析**：上游缺失时
skip，个股流水线照常运行。

### 6.4 三个产品模块的落点

| 模块 | 对应组件 | 回答的问题 |
|---|---|---|
| Opportunity Radar | `radar.py` + `trigger.py`（EventDriven） | 现在最值得看的机会是什么 |
| Thesis Monitor | `state.py` + `diff.py` + `classifier.py` + `bayes.py` | 已持有标的的逻辑有没有变化 |
| Portfolio Allocator | `portfolio.py` + `position.py` | 有限的钱应该放在哪里 |

---

## 7. 数据模型（新增 typed contracts）

> 仓库约定：所有新产物走 `ArtifactType` + `find_artifact_model(...)`，**不给 `OrchestratorState`
> 增加顶层字段**；`coerce_*` helper 与 `contracts.py` re-export 对齐既有模式。

### 7.1 新增 `ArtifactType`（`alphabee/core/schemas.py`）

```python
class ArtifactType(enum.StrEnum):
    ...
    # 中期决策层新增：
    CONSENSUS = "consensus"                # 分析师一致预期（EPS FY1/FY2 + revision）
    EXPECTATION_GAP = "expectation_gap"    # 预期差（OurForecast vs ImpliedExpectation）
    COMPANY_STATE = "company_state"        # S0–S5 认知状态 + 证据日志 + 贝叶斯后验
    POSITION_DECISION = "position_decision"  # 三层仓位决策
    CROWDING = "crowding"                  # 拥挤度评分（也可并入 signal，先独立便于回测）
    SNAPSHOT_DIFF = "snapshot_diff"                # Snapshot_t − Snapshot_{t-1}（发生了什么变化）
    PORTFOLIO_ALLOCATION = "portfolio_allocation"  # 组合级配置（跨持仓 Weight_i）
    DECISION_JOURNAL = "decision_journal"          # 决策日志（买入理由 + 证伪 + 复盘）
    RESEARCH_TASK = "research_task"                # 关键未知 → 研究任务
    THESIS_HISTORY = "thesis_history"              # Thesis 版本历史（append-only，反漂移）
```

角色分组（`_ARTIFACT_TYPE_TO_ROLE_GROUP`）：`CONSENSUS`/`EXPECTATION_GAP`/`CROWDING`/
`SNAPSHOT_DIFF`/`PORTFOLIO_ALLOCATION`/`DECISION_JOURNAL`/`RESEARCH_TASK`/`THESIS_HISTORY` → DATA；
`COMPANY_STATE`/`POSITION_DECISION` → DATA（决策层产物，供报告消费，非 narrative）。

### 7.2 一致预期域（新增 `schemas/consensus.yaml`）

当前 `expectation.yaml` 只覆盖业绩预告/快报。新增**一致预期域**（分析师共识），字段命名对齐
东方财富/研报抽取源，走 adapter 映射，业务层只读 canonical 名：

```yaml
# schemas/consensus.yaml —— 分析师一致预期（新增域）
fields:
  eps_fy1:            { unit: CNY_PER_SHARE, frequency: [weekly] }   # 未来1年一致 EPS
  eps_fy2:            { unit: CNY_PER_SHARE, frequency: [weekly] }   # 未来2年一致 EPS
  revenue_fy1:        { unit: CNY, frequency: [weekly] }
  net_profit_fy1:     { unit: CNY, frequency: [weekly] }
  target_price:       { unit: CNY, frequency: [weekly] }             # 一致目标价
  rating_mean:        { unit: RATING, frequency: [weekly] }          # 买入1/增持2/中性3/减持4
  coverage_count:     { unit: COUNT, frequency: [weekly] }           # 覆盖机构数
  eps_fy1_revision_1m: { unit: PERCENT }                              # 近1月 FY1 EPS 上修幅度
  eps_fy1_revision_3m: { unit: PERCENT }                              # 近3月 FY1 EPS 上修幅度
  eps_fy2_revision_1m: { unit: PERCENT }
  rating_upgrade_1m:  { unit: COUNT }                                 # 近1月上调机构数
  rating_downgrade_1m:{ unit: COUNT }
```

> 数据源风险（落地前必须确认）：分析师一致预期在中国市场免费源不稳定（东财「盈利预测」
> 接口、同花顺需 token）。Phase 0 明确「**研报/评级抽取为主、接口为辅助、缺失显式
> `consensus_missing` issue**」，避免像 `breadth_above_ma60_pct` 一样长期挂 gap。可复用
> `alphabee/company_track/peer_extract.py` 的研报文本抽取模式。

### 7.3 拥挤度域（新增 `schemas/crowding.yaml` 或并入 `market.yaml`）

```yaml
# schemas/crowding.yaml —— 拥挤度（新增域）
fields:
  turnover_rate:            { unit: PERCENT }   # 换手率
  turnover_rate_percentile: { unit: PERCENT }   # 历史换手率分位
  amount_pct_of_market:     { unit: PERCENT }   # 成交额占全市场比
  margin_balance_yoy:       { unit: PERCENT }   # 两融余额同比
  etf_inflow:               { unit: CNY }       # ETF 净流入
  analyst_coverage_rank:    { unit: RANK }      # 分析师覆盖热度
  news_heat:                { unit: SCORE }     # 新闻热度（0-100）
  leader_concentration:     { unit: PERCENT }   # 龙头成交集中度（前N名成交占比）
```

### 7.4 相对强度字段（并入 `schemas/market.yaml` / `industry.yaml`）

```yaml
rs_stock_market:     { unit: PERCENT }   # 个股相对全市场超额收益（20/60 日）
rs_stock_industry:   { unit: PERCENT }   # 个股相对行业超额收益
rs_industry_market:  { unit: PERCENT }   # 行业相对全市场超额收益
industry_breadth_20: { unit: PERCENT }   # 行业成分股 P>MA20 占比
industry_breadth_60: { unit: PERCENT }   # 行业成分股 P>MA60 占比
```

### 7.5 决策层 typed contracts（`alphabee/midterm/models.py`）

```python
from enum import StrEnum
from pydantic import BaseModel, Field

class CognitiveState(StrEnum):
    S0_RESEARCH = "S0"       # 研究候选
    S1_GAP = "S1"            # 预期差形成（试探仓）
    S2_CONFIRM = "S2"        # 证据确认（加仓）
    S3_CONSENSUS = "S3"      # 共识扩散（核心持有）
    S4_PRICED = "S4"         # 充分定价（减仓）
    S5_EXIT = "S5"           # 退出（thesis_broken / alpha_exhausted）

class VariableScores(BaseModel):
    """七变量得分（M 复用 MarketScore，其余 0-100 或 [-1,1] 方向分）。"""
    m: dict = Field(default_factory=dict)              # 复用 RegimeSnapshot / MarketScore
    f_fundamental_trend: float | None = None           # 边际变化方向（dF/dt）
    e_revision: float | None = None                    # 上修(+)/下修(-)
    t_relative_strength: float | None = None
    v_valuation_percentile: float | None = None        # 估值分位（低=赔率高）
    c_crowding: float | None = None
    r_risk: float | None = None                        # 三层风险合成

class EvidenceEvent(BaseModel):
    id: str
    date: str
    kind: str                       # fundamental / expectation / trend / crowding / thesis / price
    description: str
    effect_on_thesis: str           # confirming / refuting / neutral
    confidence_delta: float         # ΔP(H|E)，供 bayes.py log-odds 更新
    source_refs: list[str] = Field(default_factory=list)

class ExitCondition(BaseModel):
    kind: str                       # thesis_broken / fundamental_stop / expectation_stop
                                    # / valuation_crowding_exit / opportunity_cost
    condition: str                  # 触发条件描述
    met: bool = False

class ExpectationGap(BaseModel):
    symbol: str = ""
    as_of_date: str = ""
    our_forecast: dict = Field(default_factory=dict)      # 我们的盈利/驱动预测
    implied_expectation: dict = Field(default_factory=dict) # 从估值/价格反推的市场隐含预期
    gap: float | None = None                               # 预期差（正=我们更乐观）
    gap_direction: str = "neutral"                         # positive / negative / neutral
    evidence: list[str] = Field(default_factory=list)
    stale_after: str | None = None

class PositionDecision(BaseModel):
    portfolio_exposure: float | None = None   # 来自 market_regime PositionAdvice
    stock_weight: float | None = None         # f(F,E,T,V,C,R)
    actual_weight: float | None = None        # exposure × weight
    position_band: str = ""                   # 试探/加仓/核心/减仓/清仓（映射 S 阶段）
    rationale: list[str] = Field(default_factory=list)
    restricted: bool = False                  # 单股上限/单次调仓限制是否生效

class CompanyStateArtifact(BaseModel):
    schema_version: str = "1"
    symbol: str = ""
    state: str = "S0"                         # CognitiveState
    thesis: str = ""                          # 核心假设 H
    thesis_confidence: float = 0.0            # P(H|Evidence) 后验
    prior_confidence: float = 0.0             # 先验 P(H)
    expectation_gap: ExpectationGap = Field(default_factory=ExpectationGap)
    variable_scores: VariableScores = Field(default_factory=VariableScores)
    evidence_log: list[EvidenceEvent] = Field(default_factory=list)
    next_evidence_to_watch: list[str] = Field(default_factory=list)
    exit_conditions: list[ExitCondition] = Field(default_factory=list)
    position: PositionDecision | None = None
    as_of_date: str = ""
    stale_after: str | None = None
    degraded: bool = False
    degraded_reason: str = ""
```

> `coerce_company_state` / `coerce_position_decision` / `coerce_expectation_gap` 在
> `orchestrator/contracts.py` 增加，与 `coerce_market_regime` 对齐。

### 7.6 状态/组合/日志契约（补充：反漂移 · 差分 · 组合 · 日志）

```python
class ThesisVersion(BaseModel):
    """Thesis 版本（append-only，反漂移）。原始买入理由与证伪条件不可被行情重写。"""
    version: int
    as_of_date: str
    thesis: str
    buy_rationale: list[str] = Field(default_factory=list)   # 当时的买入理由
    invalidation: list[str] = Field(default_factory=list)    # 当时的证伪条件

class SnapshotDiff(BaseModel):
    """Snapshot_t − Snapshot_{t-1}：报告的核心是「发生了什么变化」。"""
    prev_date: str = ""
    curr_date: str = ""
    state_from: str = ""
    state_to: str = ""
    variable_deltas: dict[str, float | None] = Field(default_factory=dict)  # 七变量差分
    evidence_changed: list[str] = Field(default_factory=list)               # 新增/变化证据
    thesis_delta: str = ""                                                  # 认知变化摘要

class ResearchTask(BaseModel):
    """关键未知 → 研究任务（知道自己不知道）。"""
    id: str
    unknown: str                     # 如「《烈焰觉醒》Q3 流水 / 投放 ROI」
    importance: str = "medium"       # high / medium / low
    decides: list[str] = Field(default_factory=list)   # 影响哪个假设/结论
    status: str = "open"             # open / done / blocked

class HoldingWeight(BaseModel):
    symbol: str = ""
    state: str = ""
    weight: float | None = None      # 权益仓内权重
    odds: str = ""                   # 赔率定性
    confidence: float = 0.0
    crowding: str = ""

class PortfolioAllocation(BaseModel):
    """组合级配置（Portfolio Allocator）：跨持仓 Weight_i。"""
    holdings: list[HoldingWeight] = Field(default_factory=list)
    total_exposure: float | None = None
    sector_exposure: dict[str, float] = Field(default_factory=dict)
    risk_concentration: str = ""
    suggested_changes: list[str] = Field(default_factory=list)   # 应增/应减/应换

class DecisionJournalEntry(BaseModel):
    """决策日志：买入理由 + 证伪 + 事后复盘。"""
    id: str
    date: str
    action: str                     # buy / add / hold / reduce / sell / replace
    symbol: str = ""
    rationale: list[str] = Field(default_factory=list)
    thesis_at_time: str = ""
    confidence_at_time: float = 0.0
    evidence_refs: list[str] = Field(default_factory=list)
    review_notes: list[str] = Field(default_factory=list)   # 事后复盘
```

---

## 8. 分阶段实施

> 状态标记（本文档为新路线图，除注明「已实现」外均 ⬜ 未开始；「已实现」指依赖的上游燃料）。

### Phase 0：数据基座（Consensus + Crowding + Relative Strength）⬜ 未开始

目标：把 E/T/C 三个引擎的**燃料**变成可复用、可落盘、可降级的采集层。不产出任何评分。

- [ ] 0.1 新增 `schemas/consensus.yaml`（7.2）、`schemas/crowding.yaml`（7.3），`market.yaml`/
      `industry.yaml` 补相对强度与行业 breadth 字段（7.4）。
- [ ] 0.2 新增 `alphabee/collectors/consensus/`：东方财富盈利预测/研报评级抽取 + 接口兜底；
      复用 `company_track/peer_extract.py` 的研报文本抽取模式。**缺失显式 `consensus_missing`
      issue，不静默回退**。
- [ ] 0.3 新增 `alphabee/collectors/crowding/`：换手率分位（复用 market_regime 分位口径）、
      成交额占比、两融、分析师覆盖、新闻热度、龙头集中度。
- [ ] 0.4 新增 `alphabee/collectors/relative_strength/`：RS_stock_market / RS_stock_industry /
      RS_industry_market（用日线相对收益），行业 Breadth20/60（成分股 P>MA20/60 占比）。
- [ ] 0.5 adapter mapping：`adapters/tushare/consensus_mapping.yaml` 等，业务层只读 canonical 名。
- [ ] 0.6 落盘与历史回填：`data/midterm/` 下按日写 consensus/crowding/rs 快照；分位窗口统一
      「过去 10 年滚动」，无前视偏差。

**验收**：`poetry run pytest tests/midterm -m "not integration"`（字段映射、分位无前视、
缺失降级为 issue 而非 0）；集成测拉取一日真实数据断言关键字段非空。

### Phase 1：确定性评分引擎（E/T/C/V 四引擎，复用 derived_facts 模式）⬜ 未开始

目标：把 E/T/C/V 做成**确定性、可审计、可单测**的规则集，复用
`alphabee/agents/derived_facts/engine.py` 的 YAML 规则 + 拓扑排序 + 安全 AST 求值。

- [ ] 1.1 `rules/revision.yaml`（E）：`eps_fy1_revision_1m/3m` + `rating_upgrade/downgrade_1m`
      → `revision_score`（上修/下修/停止三态 + 方向分）。**E 是核心，优先打磨**。
- [ ] 1.2 `rules/relative_strength.yaml`（T）：RS_stock_market / RS_stock_industry /
      RS_industry_market + industry_breadth → `relative_strength_score`（相对收益 > 绝对涨跌）。
- [ ] 1.3 `rules/crowding.yaml`（C）：换手率分位 + 成交额占比 + 两融 + 分析师覆盖 + 新闻热度
      → `crowding_score`（过热/正常/冷清三态）。
- [ ] 1.4 `rules/valuation_percentile.yaml`（V）：历史 PE/PB 分位 → `valuation_percentile_score`
      （低分位=赔率高；显式输出「Cheap≠Buy」的风险提示）。
- [ ] 1.5 `score_engine.py`：复用 derived_facts `Engine`（拓扑排序 + `MarketRegimeRule` 式
      缺失重归一化），产出 `VariableScores`。缺失子指标重归一化，全缺失则 `None` + `missing_facts`。

**验收**：`poetry run pytest tests/midterm/test_*_engine.py`；单测覆盖权重合成、方向分符号、
缺失重归一化、无前视偏差。

### Phase 2：认知状态机（S0–S5）+ 预期差（ExpectationGap）+ 贝叶斯更新 ⬜ 未开始

目标：把「时间阶段」换成「认知状态」，把「盈利加仓」换成「证据加仓」。

- [ ] 2.1 `alphabee/midterm/classifier.py`：规则版 S0–S5 状态机（`transition_matrix.yaml`
      声明合法迁移，**含回退** `S3→S2`、`S1/S2/S3→S5`），非法迁移标 `suspicious` 人工/LLM 复核。
- [ ] 2.2 `alphabee/midterm/expectation_gap.py`：
      - `OurForecast`：消费 DriverProfile（F 变量）+ 基本面趋势，产出我们的盈利/驱动预测。
      - `MarketImpliedExpectation`：从当前估值分位 + 一致预期反推「市场实际在交易什么预期」。
      - `ExpectationGap = OurForecast − ImpliedExpectation`，正值才允许进入 S1。
- [ ] 2.3 `alphabee/midterm/bayes.py`：证据日志 → log-odds 更新 `P(H|Evidence)`；每条
      `EvidenceEvent` 携带 `effect_on_thesis` + `confidence_delta`，落 `Decision`（maker=`bayes_updater`，
      evidence_refs 指向证据来源）。
- [ ] 2.4 `persistence.py`：`data/midterm/{symbol}.json`（版本化、latest-wins、人工可编辑），
      `state_history.csv` 记录 S0–S5 迁移。
- [ ] 2.5 降级契约：上游缺失时 `CompanyStateArtifact.degraded=True` + 显式 issue，不静默回退。

**验收**：`poetry run pytest tests/midterm/test_classifier.py test_expectation_gap.py test_bayes.py`；
覆盖状态迁移合法性/回退、预期差符号、log-odds 更新单调性、状态持久化 roundtrip。

- [ ] 2.6 `thesis_history` 版本化 + 反漂移：原始买入理由与证伪条件 append-only，禁止随行情
      重写（ThesisDrift 防护）；报告只读最近版本但保留全历史。
- [ ] 2.7 `diff.py`：每次更新产出 `SNAPSHOT_DIFF`（state/variable/evidence 差分），报告主线
      是「发生了什么变化」而非「重新写一遍」。
- [ ] 2.8 `research_tasks`：把 `next_evidence_to_watch` 升级为 `RESEARCH_TASK`（关键未知），
      驱动 Deep Research Agent 去查「决定结论的不确定项」。

### Phase 3：三层仓位（PositionDecision）⬜ 未开始

目标：落地 `ActualPosition = PortfolioExposure × StockWeight`，并加单股上限与调仓限制。

- [ ] 3.1 `position.py` 读 `market_regime` 的 `PositionAdvice`（PortfolioExposure，已有 Phase 1.3）。
- [ ] 3.2 `rules/position_score.yaml`：`StockWeight = f(F,E,T,V,C,R)`——加权 + 门槛（E 未转正时
      上限压低）+ 状态门槛（S0=0、S1=5–15%、S2=10–20%、S3=15–25%、S4 递减、S5=0）。
- [ ] 3.3 乘法 + 单股集中度上限 + 单次调仓限制（对齐 market_regime 的 `weekly_delta_limit`
      思想，防止证据突变一次性满仓/清仓）。
- [ ] 3.4 `PositionDecisionArtifact` + `Decision` 落库（maker=`midterm_position_engine`，
      evidence_refs 指向变量得分来源）。

**验收**：`poetry run pytest tests/midterm/test_position.py`；覆盖三层乘法、状态门槛、单股上限、
调仓限制生效、E 未转正时上限压低。

- [ ] 3.5 `portfolio.py`：组合级 Portfolio Allocator——统一算 ExpectedReturn / Risk /
      Correlation / SectorExposure / MarketExposure → 跨持仓 Weight_i，替代逐股独立分析。
- [ ] 3.6 `PORTFOLIO_ALLOCATION` artifact + 建议变更（增/减/换），落 `Decision`
      （maker=`midterm_portfolio_engine`）。

### Phase 4：编排、CLI 与报告集成 ⬜ 未开始

- [ ] 4.1 `load_market_regime_context` 节点（MARKET_REGIME Phase 4.3）：读最近 `market_regime`
      artifact，作为风险暴露上下文注入，默认不阻塞。
- [ ] 4.2 `resolve_midterm_state` 节点：读最近 `CompanyStateArtifact` → 用新证据更新 → 回写 →
      注入报告。默认不阻塞个股分析。
- [ ] 4.3 `explainer.py`：Midterm Analyst Agent（`create_deep_agent` 模式），产出「当前 S 阶段 +
      预期差 + 七变量 + 建议仓位 + 下一步要盯的证据 + 证伪条件」，忠实裁决、parse 失败降级。
- [ ] 4.4 报告层：`ReportGenerationPayload` 新增 `midterm` 章节（认知状态 + 预期差 + 仓位建议
      + 证伪条件）；确定性报告 + LLM prompt 双分支。
- [ ] 4.5 CLI：`main.py` 增加子命令
      `python main.py midterm <symbol>`（评估一次）/ `--watch`（周级定时更新）/ `--history`（状态历史）。

**验收**：端到端跑通 `midterm` 命令；缺失上游时个股流水线不阻塞；报告含 S 阶段 + 仓位 + 证伪条件。

- [ ] 4.6 `trigger.py`：EventDriven——监听财报/预告/研报/EPS Revision/股价异常 → 判定
      「Information Change → Investment State Change」→ 只推认知变化，不推新闻（Opportunity Radar）。
- [ ] 4.7 `journal.py` + `DECISION_JOURNAL`：买入理由 + 证伪 + 事后复盘（Decision Journal）。
- [ ] 4.8 三个产品模块入口：Opportunity Radar / Thesis Monitor / Portfolio Allocator 的 CLI/UI。

### Phase 5：验证与增强（不阻塞上线）⬜ 未开始

- [ ] 5.1 PositionScore 有效性回测：对 3/6 个月未来收益的 IC、高分区 vs 低分区回撤对比。
- [ ] 5.2 S0–S5 迁移回测：用 `state_history.csv` 验证「S1 开仓 → S2 加仓 → S4 减仓」是否跑赢
      「均线开仓」基线。
- [ ] 5.3 ExpectationGap 的 Alpha 验证：高正向 gap 组合是否跑赢；gap 收窄（Alpha Exhausted）
      是否有效提示减仓。
- [ ] 5.4 状态机升级：规则分类器稳定后，再考虑 HMM/GMM 拟合，与规则版一致性对比。
- [ ] 5.5 数据源容错：consensus/crowding 接口不稳定时 fallback + `gap_recorder` 记录。

---

## 9. 决策顺序落地（十问 → graph/prompt 顺序）

把「先打开 K 线」改成十问顺序，作为 `midterm` 分析与报告 prompt 的组织骨架：

```text
Q1 市场环境允许多少总仓位？            → M → PortfolioExposure
Q2 公司的核心驱动变量是什么？          → F → DriverProfile（DOMAIN_CONTEXT）
Q3 市场当前共识是什么？                → E → consensus
Q4 我的判断与市场有什么不同？          → ExpectationGap
Q5 有什么证据证明我是对的？            → EvidenceEvent（证据日志）
Q6 盈利预期在上修还是下修？            → E → revision_score
Q7 价格/行业有没有开始确认？           → T → relative_strength + breadth
Q8 当前估值还有多少赔率？              → V → valuation_percentile
Q9 交易是不是已经拥挤？                → C → crowding_score
Q10 什么证据出现意味着我错了？         → R → exit_conditions / falsification
最后才是：买多少                        → PositionDecision（三层仓位）
```

报告固定围绕 4 个问题组织（对齐 ROADMAP Phase 5 备忘录原则）：

1. 一句话观点：当前最值得相信/最该怀疑的是什么？
2. 核心矛盾：哪两个事实或预期在打架？（= ExpectationGap + central_tension）
3. 裁决依据：支持观点的 2-3 条关键证据 + 最强反证。
4. 证伪条件：未来看到什么数据，这个判断需要改变？（= exit_conditions）

---

## 10. 与现有 Roadmap 的关系与依赖（只依赖已完成部分）

| 本文档 Phase | 依赖的已有 Roadmap 阶段 | 关系 |
|---|---|---|
| Phase 0（E/T/C 数据） | MARKET_REGIME Phase 0-1（分位口径）、COMPANY_TRACK Phase A/C（研报抽取模式） | 复用机制，零引擎改动 |
| Phase 1（E/T/C/V 引擎） | derived_facts `Engine`（拓扑排序 + 安全 AST） | 直接复用 |
| Phase 2（状态机+预期差） | DOMAIN_CONTEXT Phase 0（DriverProfile，**未落地**）、ROADMAP Phase 4（ExpectationFitAgent，**未落地**） | 预期差依赖 DriverProfile/ImpliedExpectation，需先补 E 数据 |
| Phase 3（三层仓位） | MARKET_REGIME Phase 1.3（PositionAdvice，**已实现**） | 消费 PortfolioExposure |
| Phase 4（注入+CLI） | MARKET_REGIME Phase 4.3（load_market_regime_context，**未落地**） | 合并实施，避免重复改 prompt |

> 关键依赖澄清：Phase 2 的 `OurForecast` 依赖 DOMAIN_CONTEXT 的 DriverProfile，而
> DriverProfile 尚未落地。因此**推荐顺序**：先做 Phase 0-1（E/T/C 数据 + 引擎，纯增量、
> 不依赖未完成项），再用「简化 OurForecast（consensus 外推 + 基本面趋势）」先行落地 Phase 2，
> DriverProfile 成熟后再替换为 domain-specific 版本——保证决策层**不阻塞于上游叙事层**。

---

## 11. 推荐优先级与依赖

| 优先级 | 事项 | 价值 | 依赖 | 状态 |
|---|---|---|---|---|
| P0 | Phase 0 E/T/C 数据基座 | 一切评分的燃料；先验证一致预期源可行性 | 无 | ⬜ |
| P0 | Phase 1 E 引擎（revision） | **核心变量**，可独立验证 Alpha | Phase 0 | ⬜ |
| P1 | Phase 1 T/C/V 引擎 | 补齐七变量 | Phase 0 | ⬜ |
| P1 | Phase 2 状态机 + 预期差 + 贝叶斯 | 从「指标」升级为「决策模型」 | Phase 1 + 简化 OurForecast | ⬜ |
| P1 | Phase 3 三层仓位 | 打通 M → 个股 → 实际仓位 | Phase 2 + market_regime 已实现 | ⬜ |
| P1 | Phase 2 Snapshot Diff + Thesis 反漂移 | 认知连续性（核心资产）+ 纪律约束 | Phase 2 | ⬜ |
| P1 | Phase 3 Portfolio Allocator | 组合级决策，替代逐股独立分析 | Phase 3 | ⬜ |
| P2 | Phase 4 EventDriven 触发 + Decision Journal | 从 UserDriven 到 EventDriven；决策可复盘 | Phase 4 | ⬜ |
| P2 | Phase 4 CLI + 注入个股流水线 | 交付用户日常使用 | Phase 3 | ⬜ |
| P3 | Phase 5 回测与模型升级 | 用数据证明有效 | Phase 3+ | ⬜ |

---

## 12. 成功标准（可量化，配合仓库测试文化）

1. **预期差有效性**：高正向 `ExpectationGap` 组合未来 3/6 个月超额收益显著为正；gap 收窄
   时有效提示减仓。
2. **状态机迁移正确性**：S0–S5 迁移不产生非法跳转，回退（S3→S2、S1→S5）可回放；
   `state_history.csv` 覆盖完整迁移记录。
3. **证据加仓可审计**：每次加仓/减仓都能回溯到具体 `EvidenceEvent`（evidence_refs），
   gate 的 `evidence_coverage` 提升。
4. **三层仓位区分清晰**：`PortfolioExposure / StockWeight / ActualWeight` 三个概念在 artifact
   与报告中显式分离，不再混用。
5. **降级契约**：一致预期/拥挤度/相对强度缺失时显式 `*_missing` issue，不静默回退为 0 或中性。
6. **黄金样本命中率**：迈瑞医疗（低估值 + 基本面边际改善 → S1；盈利预期转向上修 → S2/S3）
   作为 golden 样本，报告摘要含「S 阶段 + 预期差 + 仓位建议 + 证伪条件」，回归不下降。
7. **认知连续性（Time Series of Belief）**：每次更新必产出 `Snapshot_t − Snapshot_{t-1}`，
   报告核心是「发生了什么变化」；同一标的两次运行之间 Belief 连续、可回放。
8. **反漂移（ThesisDrift 防护）**：原始买入理由与证伪条件 append-only；证伪条件命中时系统
   显式提示「你当初定义的第 N 项证伪已发生」，禁止静默重写 Thesis。
9. **EventDriven 有效性**：信息变化 → 状态变化的触发可回放；「只推认知变化、不推新闻」的
   提醒精确率（reminder precision）可追踪。
10. **三个模块闭环**：Opportunity Radar / Thesis Monitor / Portfolio Allocator 三个入口可用，
    覆盖 50–100 家标的不退化；Decision Journal 记录每次 buy/add/reduce/sell/replace 的
    理由与事后复盘。

---

## 13. 顶层公式（最终抽象）

```text
Position(i, t)
  = MarketExposure(t)
  × f( ThesisConfidence,      # P(H|Evidence)，贝叶斯后验
       ExpectationGap,        # OurForecast − ImpliedExpectation
       FundamentalMomentum,   # dF/dt 边际变化
       RevisionMomentum,      # E 上修/下修
       RelativeStrength,      # T 相对强度 + Breadth
       Valuation,             # V 历史分位（赔率）
       Crowding,              # C 拥挤度
       Risk )                 # R 三层风险（Thesis > Fundamental > PriceNoise）
```

第一部分（`MarketExposure`）回答「现在应该冒多少系统性风险」；第二部分（`f(...)`）回答
「这个股票值得占多少风险预算」。这既是本文档的设计主线，也是未来
**AlphaBee 3–12 个月中期投资模块的顶层原则**。
