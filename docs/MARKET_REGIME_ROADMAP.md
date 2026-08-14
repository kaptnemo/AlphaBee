# AlphaBee Market Regime Roadmap（实现版）

## 背景判断

AlphaBee 现有流水线是**单标的、股票级**的：

```text
collect_raw_facts → run_analysis_engines → explore_conflicts
→ verify_hypotheses → synthesize_insights → run_thesis
→ review_thesis → generate_report → review_report → finalize_message
```

它回答的是"这只股票值不值得持有"，但还没有回答用户在**下注之前**的那个问题：

> 现在整个市场适合重仓还是轻仓？未来 3–6 个月的市场胜率如何？

本文档把《蜂巢仓位雷达》功能设计落地为一条**可独立运行、可逐步上线、可被股票级流水线消费**的实现路径。核心原则：

```text
不预测涨跌，只识别市场所处的状态。
```

### 与现有架构的关系

| 维度 | 现有股票级流水线 | 本引擎（市场级） |
|---|---|---|
| 分析对象 | 单标的（symbol） | 市场/指数（沪深300/500/1000/创业板 + 宏观资金面） |
| 运行频率 | 用户触发 | 周级为主（weekly），日级采集沉淀 |
| 数据源 | tushare 个股/财务接口 | tushare 指数估值/宏观 + akshare 市场宽度 |
| 产出 | 个股研究报告 | 市场状态评分 + 仓位建议 + 风险提示 |
| 核心模型 | OrchestratorState / Artifact / Decision / Issue | 复用同一套 core 模型 |

**架构立场：不做成一个塞进现有 LangGraph 股票流水线里的普通节点，而是一个独立的市场级 graph。** 理由：

1. 它的数据对象（指数、宏观、资金面）与股票流水线完全不同，塞进去只会让两条生命周期的状态互相污染。
2. 它天然是"周级 + 定时"运行，而股票流水线是"用户触发"，生命周期不匹配。
3. 它需要被**多个下游**消费：既可以是独立 CLI 报告，也可以作为股票分析的"风险暴露上下文"注入。

但两类 graph 共用 `alphabee/core/`（Run/Step/Artifact/Observation/Decision/Issue）和 `alphabee/orchestrator/contracts.py` 的 typed artifact 约定，保证后续可以互相消费。

---

## 模块落地目录

```text
alphabee/market_regime/                  ← 新增，独立于 agents/（市场级，非个股级）
  __init__.py
  state.py                              # MarketRegimeState（镜像 OrchestratorState）
  graph.py                              # market_regime_agent = 编译后的 LangGraph
  models.py                             # MarketScore / RegimePhase / PositionAdvice 等
  data.py                               # 市场数据采集与清洗（指数估值/宽度/流动性）
  score_engine.py                       # 确定性评分引擎（镜像 derived_facts Engine）
  regime_classifier.py                  # 六阶段状态机 + 相似历史搜索
  position.py                           # 仓位映射 + 周级调整限制
  explainer.py                          # Market Analyst Agent（LLM 解释与风险提示）
  persistence.py                        # market_indicator_daily / market_score_history 落盘
  rules/
    valuation.yaml                      # ERP / PE分位 / PB分位 + 权重
    trend.yaml                          # 均线结构 / 市场宽度 / 动量
    liquidity.yaml                      # 利率周期 / M1-M2 剪刀差 / 社融周期
    risk_preference.yaml                # 成交额组合 / 融资余额 / ETF 资金流
    position.yaml                       # 评分 → 仓位区间 + 单周 ±10% 限制
  prompts/
    explainer.py                        # Market Analyst Agent prompt
  tests/
    test_score_engine.py
    test_regime_classifier.py
    test_position.py
```

数据落盘：

```text
data/market_regime/
  market_indicator_daily.csv     # 日期级指标快照（对应 market_indicator_daily 表）
  market_score_history.csv       # 周级评分历史（对应 market_score_history 表）
  regime_history.csv             # 阶段状态历史（状态机迁移记录）
```

---

## Phase 0：数据基座（可独立交付）

目标：把设计文档"数据需求"一节变成可复用、可落盘的采集层。这一阶段不产出任何评分，只保证数据能稳定拉取并规范化。

### 0.1 新增 canonical 字段

在 `alphabee/schemas/` 增加**市场域**（现有 7 域为 financial/market/company/industry/expectation/risk/operation，其中 `market` 是"个股行情估值"，需新增 `market_regime` 域，避免概念混叠）。

`alphabee/schemas/market_regime.yaml` 建议字段（命名对齐 tushare/akshare 源字段，走 adapter 映射）：

```yaml
fields:
  # ── 指数估值 ──
  hs300_pe_ttm:
    source: index_dailybasic.pe
    unit: x
  hs300_pb: { source: index_dailybasic.pb }
  hs300_ep_ttm:              # 盈利收益率 = 1/PE，供 ERP 计算
  cs500_pe_ttm: {}
  cs1000_pe_ttm: {}
  cyb_pe_ttm: {}
  # ── 利率与流动性 ──
  cn_10y_yield: { source: yield_cnbd, note: 中债10年期国债到期收益率 }
  shibor_3m: { source: shibor }
  us_10y_yield: { source: us_tycr }
  m1_yoy: { source: cn_m, unit: "%" }
  m2_yoy: { source: cn_m }
  m1_m2_gap: {}              # M1增速 - M2增速（衍生）
  social_financing_yoy: { source: cn_sf }
  # ── 市场宽度与趋势 ──
  breadth_above_ma60_pct: {} # 站上60日均线股票比例
  nh_nl_diff: {}             # 创新高-创新低家数差
  up_stock_ratio: {}         # 上涨家数占比
  index_ma20: {}
  index_ma60: {}
  index_ma250: {}
  market_turnover: {}        # 两市成交额
  margin_balance: {}         # 融资余额
  etf_net_inflow: {}         # ETF 资金净流入
```

> 注意：`cn_10y_yield` 等 tushare 接口（`yield_cnbd` / `shibor` / `cn_m` / `cn_sf` / `us_tycr`）的字段名以实际返回为准，落地时通过 `alphabee/adapters/tushare/market_regime_mapping.yaml` 收敛，业务层永远只读 canonical 名。是否具备权限积分在 Phase 0 就要确认，缺的用 akshare 兜底。

### 0.2 采集器

新增 `alphabee/collectors/market_regime/`：

- `index_valuation.py`：`index_dailybasic` 拉沪深300/500/1000/创业板 PE/PB（指数代码可配）。
- `breadth.py`：akshare 全市场日线计算 `breadth_above_ma60_pct` / `nh_nl_diff` / `up_stock_ratio`（A股5000+标的，注意接口限频，可缓存到本地按日复用）。
- `liquidity.py`：中债收益率 / SHIBOR / 美债 / M1 / M2 / 社融（低频，每月更新，周级采集时读缓存）。
- `risk_preference.py`：两市成交额 / 融资余额 / ETF 净流入。

数据层约定：每个采集函数返回规范化的 `dict[canonical_field, value]`，统一经 adapter 映射，`alphabee/collectors/` 现有的 `helper.py` 失败上报（`capture_failure`）机制直接复用。

### 0.3 落盘与历史回填

`persistence.py` 负责：

- 按日写 `market_indicator_daily.csv`（date + 全部指标 + 来源/新鲜度）。
- 首次上线回填近 10 年（ERP 分位和 PE/PB 分位都需要历史序列）。分位计算窗口统一为"过去 10 年，且只含滚动窗口内数据"，避免前视偏差。

### 0.4 验收

```bash
poetry run pytest tests/market_regime -m "not integration"
# 单测：字段映射、指数代码解析、CSV 落盘格式、分位计算正确性
poetry run pytest -m integration tests/market_regime
# 集成：真实拉取一日数据，断言关键指标非空且单位正确
```

---

## Phase 1：确定性评分引擎（核心，先不做 LLM）✅ 已实现

目标：用现有 `derived_facts` 的 **YAML 规则 + 拓扑排序 + 安全求值** 模式，把三个引擎（估值/趋势/流动性）+ 风险偏好调整全部做成确定性、可审计、可单测的规则集。

> 落地说明（相对本文档的差异）：
> - 每个主题 YAML（valuation/trend/liquidity/risk_preference）内用 `rules:` 键同时声明**指标层**与**聚合层**规则，`market_score.yaml` 单独放市场总分规则（本文档模块清单中未列出，属合理补充）。
> - `score_engine.py` 直接复用 `derived_facts` 的 `Engine`（拓扑排序）+ `DerivedFactRule`（safe-AST 公式 + thresholds → level/interpretation），`MarketRegimeRule` 覆写 `compute` 把"未知变量"映射为 `missing_fact`。
> - 缺失子指标时聚合**重归一化**权重（缺失项跳过，不作为 0 拖累）；全部缺失则对应引擎/总分为 `None` 并记录 `missing_facts`。
> - 产出：`MarketScore` / `RegimeSnapshot` / `PositionAdvice`（models.py），ArtifactType 新增 `MARKET_REGIME` / `MARKET_REGIME_HISTORY`，`contracts.py` re-export 类型 + `coerce_market_regime`，`build_decision_issue` 生成 Decision/Issue。

### 1.1 规则引擎复用

不新造轮子。`alphabee/market_regime/score_engine.py` 直接复用 `alphabee/agents/derived_facts/engine.py` 的 `Engine` 语义（拓扑排序 + safe AST formula + thresholds → level/interpretation），规则声明依赖：

```yaml
# rules/valuation.yaml
name: valuation_score
weight: 0.30
required_derived_facts: [erp_score, pe_percentile_score, pb_percentile_score]
formula: erp_score * 0.5 + pe_percentile_score * 0.3 + pb_percentile_score * 0.2
thresholds:
  high_attractiveness: "value >= 70"
  neutral: "50 <= value < 70"
  low: "value < 50"
```

规则分两层：

- **指标层**（`erp_score`、`pe_percentile_score`、`ma_structure_score`、`breadth_score`、`rate_cycle_score`、`m1_m2_score`、`socfin_score` 等）：吃 canonical 数据，输出 0–100 分。
- **聚合层**（`valuation_score`、`trend_score`、`liquidity_score`、`market_score`）：按权重合成。

### 1.2 三引擎评分口径（对齐设计文档）

| 引擎 | 权重 | 子指标 | 分值映射要点 |
|---|---|---|---|
| Valuation | 30% | ERP×50% + PE分位×30% + PB分位×20% | ERP 用历史分位映射：≥90% 历史分位记 90–100，逐档递减 |
| Trend | 40% | 均线结构 + 市场宽度 + 动量（低权） | 宽度权重 > 均线，动量仅作微调 |
| Liquidity | 30% | 利率周期 + M1-M2 + 社融拐点 | 利率下降趋势 90 / 稳定 60 / 上升 30；社融看拐点不看绝对值 |

`market_score = valuation×0.30 + trend×0.40 + liquidity×0.30`，范围 0–100。

风险偏好引擎（成交额×价格组合、融资余额、ETF 流）作为**调整项**：评分确定后按规则 `+5 ~ -5` 微调，并单独输出 `risk_preference_status`，不进主权重，防止情绪因子污染结构性判断。

### 1.3 仓位映射

`rules/position.yaml`：

```yaml
bands:
  - { min: 85, max: 100, regime: 强牛阶段,   position_lo: 0.80, position_hi: 0.90 }
  - { min: 70, max: 85,  regime: 趋势健康,   position_lo: 0.60, position_hi: 0.80 }
  - { min: 50, max: 70,  regime: 震荡阶段,   position_lo: 0.40, position_hi: 0.60 }
  - { min: 30, max: 50,  regime: 风险增加,   position_lo: 0.20, position_hi: 0.40 }
  - { min: 0,  max: 30,  regime: 熊市阶段,   position_lo: 0.00, position_hi: 0.20 }
weekly_delta_limit: 0.10   # 单周最大 ±10%，防止追涨杀跌
```

`position.py` 维护 `prev_week_score`：本次建议区间 = `min(band_hi, prev + 0.10)` / `max(band_lo, prev - 0.10)`，并把"被限制的差异"记入 `rationale`。

### 1.4 产出与契约

- 新增 `ArtifactType`（`alphabee/core/schemas.py`）：
  - `MARKET_REGIME = "market_regime"`（评分+状态+仓位建议）
  - `MARKET_REGIME_HISTORY = "market_regime_history"`（周序列，供回测与前端趋势）
- `alphabee/orchestrator/contracts.py` 新增 typed payload：

```python
class MarketScore(BaseModel):
    valuation_score: float
    trend_score: float
    liquidity_score: float
    risk_preference_delta: float
    total_score: float

class RegimeSnapshot(BaseModel):
    date: str
    scores: MarketScore
    regime: str                    # 强牛/趋势健康/震荡/风险增加/熊市
    position_low: float
    position_high: float
    weekly_change: float | None
    main_drivers: list[str]        # 本周评分变化的驱动因子
    risks: list[str]               # 风险提示
    explanation: str = ""          # Phase 3 由 explainer 填充
```

- `run_analysis` 节点同时产出 `Decision`（maker=`market_score_engine`，携带 evidence_refs 指向所用 canonical 指标）和 `Issue`（评分低于阈值或关键指标缺失时）。

### 1.5 验收

```bash
poetry run pytest tests/market_regime/test_score_engine.py
```

单测覆盖：权重合成正确性、分数边界档位、`weekly_delta_limit` 生效、数据缺失时规则降级为 `missing_fact` 而非 0 分、分位计算无前视偏差。

---

## Phase 2：市场状态机（Regime Machine）+ 相似历史搜索

设计文档结尾建议的状态机是"比评分更接近机构框架"的下一步，建议在评分引擎稳定后接入，而不是一步到位。

### 2.1 六阶段分类器（规则先行）

`regime_classifier.py` 把 `market_score` + 分引擎分映射到六阶段，同时引入**迁移约束**（Markov 风格），不允许任意跳转：

```text
吸筹期 → 趋势启动 → 趋势加速 → 高位分歧 → 风险释放 → 底部修复 →（回吸筹期）
```

实现方式：

- 规则层：由趋势分 + 宽度 + 风险偏好 delta 判定当前候选阶段。
- 约束层：`transition_matrix.yaml` 声明合法迁移（如 `高位分歧` 不能直接跳 `趋势启动`），不合法的迁移标记为 `suspicious` 并人工/LLM 复核。
- 输出 `regime_history.csv`，保存 `date / phase / confidence / transition_from / transition_valid`。

### 2.2 相似历史搜索

目标：给定当前特征向量（ERP 分位、趋势分、流动性分），回放历史，输出"相似环境下未来 6 个月上涨概率与最大回撤"。

实现要点：

- 特征归一化后用简单距离（欧氏距离，先不做 embedding，保持可解释）。
- 只在 `regime_history` 同阶段内搜索（先按阶段过滤，再按距离排序），避免跨阶段误配。
- 明确披露局限：相似历史 = 统计参考，不是预测承诺；样本窗口覆盖多个完整牛熊才有意义。
- `forward_returns.py` 独立模块做"特征日之后 N 个月（21 日制）的收益/回撤"计算，与评分管线解耦，可单独回测。

### 2.3 验收

```bash
poetry run pytest tests/market_regime/test_regime_classifier.py
```

单测：六阶段判定边界、非法迁移拦截、相似历史排序、前视偏差检查（回测只用当日及以前数据）。

---

## Phase 3：Market Analyst Agent（LLM 解释层）

目标：给评分加上"人话"——为什么涨、为什么降、风险在哪。这是"评分+仓位"变成"可读建议"的关键。

### 3.1 Agent 形态

复用 `deepagents.create_deep_agent`（与 `FactCollectorAgent` 同模式），注册在独立 graph 的 `explain_market` 节点：

```text
MarketScore 输入（score_engine 输出）
+ 本周 vs 上周分项 diff
+ 相似历史搜索摘要
+ 关键 canonical 指标（ERP分位/宽度/利率/M1-M2）
→
{ regime, explanation, main_drivers[], risks[], what_would_change_my_mind[] }
```

约束（沿用 `report_generator` 的"忠实裁决"原则）：

- 不引入 payload 之外的新数字。
- `main_drivers` 必须能回溯到具体分项变化（例如"十年国债收益率下行 → 流动性分 +N"）。
- `risks` 必须来自 risk_preference delta 或相似历史回撤分布，不自由发挥。
- schema 容错：枚举值 normalize（沿用 ROADMAP.md 里 `InsightOutput` schema 脆弱性的教训），parse 失败时降级为模板解释，不整层丢弃。

### 3.2 产出

- `MarketRegimeReport`（Markdown，市场级报告，结构对齐设计文档"详细解释页面"的业务内容，但**不含 UI 布局**，只输出内容 JSON/Markdown，由 apps/web 层渲染）。
- 所有结论沉淀为 `Decision` + `evidence_refs`，评分和解释分开存（`market_regime` artifact 只存结构化评分，`market_regime_report` 存 LLM 文本），避免 parse 失败污染评分层。

### 3.3 验收

```bash
poetry run pytest tests/market_regime/test_explainer.py
```

断言：explanation 不包含 payload 外数字、main_drivers 能映射到分项 diff、parse 失败走降级路径不抛异常。

---

## Phase 4：编排、CLI 与集成

### 4.1 独立 graph

`alphabee/market_regime/graph.py` 编译 `market_regime_agent`：

```text
collect_market_data → score_market → classify_regime → explain_market → finalize
```

State 用 `MarketRegimeState`（复用 core 的 Artifact/Decision/Issue，但字段是市场级）。LangGraph 的 `subgraphs=True` 流式、`make_id`、Step 生命周期全部照搬现有约定。

### 4.2 CLI 入口

在 `main.py` 增加子命令（沿用 argparse）：

```bash
poetry run python main.py market-regime                # 计算本周评分并输出报告
poetry run python main.py market-regime --history      # 打印周序列
poetry run python main.py market-regime --backtest     # 相似历史搜索
poetry run python main.py market-regime --watch        # 常驻定时（周级）更新并告警
```

### 4.3 注入股票级流水线（可选，P1/P2）

市场级状态以**上下文 artifact** 注入个股流水线：在 `collect_raw_facts` 之后新增 `load_market_regime_context` 节点，读取最近一期 `market_regime` artifact，作为 risk exposure 上下文喂给 `review_thesis` / `generate_report` 的 prompt，使个股结论带上"当前是轻仓期/重仓期"的宏观背景。**默认不阻塞个股分析**：市场数据缺失时该节点 skip，个股流水线照常运行。

### 4.4 存储与配置

- `config.yaml` 增加 `market_regime:` 段（指数清单、回填窗口、周级调度 cron 表达式、数据源优先级 tushare→akshare）。
- `persistence.py` 支持从 CSV 导出/导入，方便迁移到 sqlite 或后续数据库。

### 4.5 验收

```bash
poetry run python main.py market-regime                # 端到端跑通
poetry run python main.py market-regime --backtest     # 相似历史可输出
poetry run pytest tests/market_regime/ tests/orchestrator/
```

---

## Phase 5：验证与增强（不阻塞上线）

- **评分有效性回测**：`market_score` 对未来 3/6 个月指数收益的秩相关（IC），以及"高分区未来回撤显著低于低分区"的验证；结果沉淀到 `data/market_regime/validation/`。
- **状态机升级**：规则分类器稳定后，再考虑隐马尔可夫（HMM）或 GMM 拟合，用 `regime_history.csv` 作为训练序列，输出与规则版做一致性对比。
- **相似历史规模化**：数据充足后把欧氏距离升级为带阶段约束的最近邻搜索，加入 `max_drawdown` 分布披露。
- **多数据源容错**：tushare 积分不足时自动 fallback akshare；缺失指标记 `missing_fact` 并进入 gap_recorder，沿用 `record_signal_data_gaps` 模式。

---

## 推荐优先级

| 优先级 | 事项 | 价值 | 依赖 |
|---|---|---|---|
| P0 | Phase 0 数据基座 + 回填 | 一切评分的燃料；先验证 tushare 权限积分 | 无 |
| P0 | Phase 1 确定性评分引擎 | 核心可交付物，纯确定性可单测 | Phase 0 |
| P1 | Phase 2 状态机 + 相似历史 | 从"评分"升级到"阶段"，更接近机构框架 | Phase 1 |
| P1 | Phase 3 LLM 解释层 | 评分变"可读建议" | Phase 1 |
| P2 | Phase 4 CLI + 周级调度 | 可交付给用户日常使用 | Phase 1 |
| P2 | 注入个股流水线 | 打通"宏观仓位 → 个股分析" | Phase 4 |
| P3 | Phase 5 回测与模型升级 | 用数据证明评分有效，再考虑换模型 | Phase 1+ |

---

## 成功标准

- `python main.py market-regime` 一条命令输出：评分、阶段、建议仓位区间、周变化、驱动因子、风险提示。
- 评分引擎 100% 确定性，同一输入永远同一输出，全部规则可审计、可单测。
- `weekly_delta_limit` 生效，评分突变被记录而不是被掩盖。
- 市场级 artifact 能通过 `find_artifact_model` 被个股流水线消费，且缺失时静默 skip。
- 相似历史搜索明示统计局限，不输出"预测承诺"。
- 六阶段状态机不允许非法迁移，迁移记录可回放。
