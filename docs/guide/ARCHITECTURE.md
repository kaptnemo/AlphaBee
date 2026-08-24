# 架构与流水线详解

本文档详解 AlphaBee 的分层分析流水线、各引擎的规则设计、多期趋势分析与报告结构。规则统计总览：

| 层级 | 引擎 | 规则数 | 维度 |
|------|------|--------|------|
| 派生指标 | DerivedFacts | 21 | 盈利/成长/偿债/效率/估值/现金流/风险 |
| 信号检测 | SignalEngine | 20 | 基础风险信号 + 异常模式信号 |
| 勾稽关系 | AnomalyEngine | 10 | z-score 检测 |
| 投资论点 | ThesisEngine | 8 | 财务质量/盈利质量/信用风险/成长质量/资本效率/竞争壁垒/估值合理/经营稳定 |

---

## 架构概览

```
main.py (CLI)
  └─ Orchestrator (StateGraph)
       ├─ collect_raw_facts          ← 事实采集 + 结构化建模
       │   └─ FactCollectorAgent  (LLM, 8 工具)
       │
       ├─ run_analysis_engines       ← 确定性引擎（并行）
       │   ├─ DerivedFacts  (确定性, 21 条 YAML)
       │   ├─ SignalEngine  (确定性, 20 条 YAML)
       │   └─ AnomalyEngine (确定性, 10 条 YAML)
       │
       ├─ explore_conflicts          ← 跨维度矛盾发现 (LLM)
       ├─ verify_hypotheses          ← 假设证据验证 (LLM + web/Tushare/研报/本地财报工具)
       ├─ synthesize_insights        ← 上游证据综合与中心洞察提炼
       ├─ run_thesis                 ← 8 维度加权综合评分
       ├─ review_thesis              ← 证据充分性 / 信号一致性 / 语境适配
       ├─ generate_report            ← 单次 LLM, 结构化 → Markdown
       ├─ review_report              ← Harness-as-library 质量门控
       └─ finalize_message
```

---

## 1. 事实采集 (FactCollector)

`FactCollectorAgent` 通过 8 个领域工具采集客观数据，输出 Pydantic 结构化模型：

| 工具 | 采集内容 |
|------|---------|
| `get_company_profile` | 公司基本信息、股东结构 |
| `get_financial_fact` | 多期利润表/资产负债表/现金流量表/财务比率（最多 20 期） |
| `get_operation_fact` | 主营业务构成（产品/地区拆分） |
| `get_industry_fact` | 申万行业分类、行业指数PE/PB |
| `get_competition_fact` | 同行竞争对手关键指标对比 |
| `get_market_fact` | 行情、PE/PB、资金流向、均线 |
| `get_expectation_fact` | 业绩预告、业绩快报 |
| `get_risk_fact` | 新闻舆情、股权质押、回购 |

输出模型 `FinancialFacts`（41 字段 × 多期）和 `MarketFacts` 通过 `.to_fact_values()` 展平为 `dict[str, float]`，供下游引擎消费。

## 2. 衍生指标 (DerivedFacts)

21 条 YAML 规则，按利润表→资产负债表→现金流量表的依赖关系做拓扑排序：

```
盈利能力：  roe_level, gross_margin_trend
成长质量：  revenue_growth, profit_leverage, market_share_change
偿债能力：  debt_ratio, interest_coverage, current_ratio
运营效率：  inventory_pressure, asset_turnover, capex_intensity
估值匹配：  peg_ratio, pb_roe_match, valuation_compression
现金流：    cashflow_quality, receivable_pressure, receivable_growth_gap,
            accounts_receivable_growth, accounts_receivable_yoy, dividend_coverage
风险：      goodwill_risk
```

公式使用安全 AST 求值——仅算术和比较运算符：

```yaml
# 例: cashflow_quality.yaml
formula: "operating_cashflow / net_profit"
thresholds:
  excellent: "value >= 1.0"
  normal:    "0.8 <= value < 1.0"
  warning:   "value < 0.8"
```

## 3. 信号检测 (SignalEngine)

20 条 YAML 规则，分成两组：**11 条基础风险信号** + **9 条异常模式信号**。基础风险信号基于衍生指标 + 原始事实做严重度分级触发：

| 信号 | 触发逻辑 | 覆盖维度 |
|------|---------|---------|
| `revenue_quality_risk` | 应收/营收增速差值 | 财务质量 + 盈利质量 |
| `cashflow_quality_risk` | 经营现金流/净利润 < 阈值 | 财务质量 + 盈利质量 |
| `debt_risk` | 负债率 > 阈值且流动比率低 | 财务质量 + 信用风险 |
| `profitability_quality_risk` | ROE + 毛利率趋势 | 财务质量 + 盈利质量 |
| `growth_quality_risk` | 应收/营收差距 + 利润杠杆 | 财务质量 + 成长质量 |
| `expansion_risk` | 商誉 + 负债率 + 资本支出 | 财务质量 + 信用风险 |
| `valuation_risk` | PEG + 估值压缩 + PB-ROE 匹配 | 财务质量 + 估值合理 |
| `anomaly_cluster_risk` | 2+ 个异常模式触发 | 财务质量 |
| `cross_validation_break` | 最强异常 \|z\| > 2.5σ | 财务质量 + 盈利质量 |
| `moat_erosion_risk` | 毛利率趋势 + ROE + 营收增速 | 竞争壁垒 + 盈利质量 |
| `capital_efficiency_risk` | ROE + 现金流质量 + 资本支出强度 + 分红覆盖 | 资本效率 + 财务质量 |

每条信号携带 `thesis_impact`（影响方向+维度）和 `critic_questions`，直接驱动下游论点引擎。

异常模式信号当前包括：`anomaly_pattern_inflated_revenue`、`anomaly_pattern_profit_without_cash`、`anomaly_pattern_high_cash_high_debt`、`anomaly_pattern_inventory_shenanigans`、`anomaly_pattern_depreciation_manipulation`、`anomaly_pattern_expense_capitalization`、`anomaly_pattern_tax_profit_mismatch`、`anomaly_pattern_cost_pressure`、`anomaly_pattern_efficiency_gain`。

## 4. 勾稽关系异常检测 (AnomalyEngine)

基于《手把手教你读财报》框架，两步检测：

**一阶：10 条勾稽关系 z-score 检查**

每条规则取近 4 期历史基线（μ±σ），检测本期是否显著偏离：

| 勾稽关系 | 检测内容 |
|---------|---------|
| 应收/营收背离 | 应收账款增速是否远超营收增速 |
| 存货/营收背离 | 存货增速是否远超营收增速 |
| 现金流/利润背离 | 经营现金流是否远低于净利润 |
| 毛利/费用背离 | 毛利率与费用率是否同向变化 |
| 折旧/固定资产背离 | 折旧率是否异常下降 |
| 税费/利润背离 | 有效税率是否异常低于法定税率 |
| 薪酬/员工背离 | 人均薪酬是否异常波动 |
| 利息/有息负债背离 | 隐含借款利率是否异常 |
| 大存大贷 | 货币资金和有息负债是否同时偏高 |
| 经营/投资现金流错配 | 自由现金流缺口是否持续扩大 |

AnomalyEngine 本身当前负责 **10 条一阶勾稽关系检查**；二阶异常模式已沉淀为 SignalEngine 中的异常模式信号，因此会在信号层和后续 thesis/review 环节继续被消费。

每条触发规则都附带 **财报附注排查路径**（如"核对应收账款附注账龄结构表"），在报告和后续验证环节中直接呈现。

## 5. 冲突探索 (ExploreConflicts)

LLM 驱动的跨维度矛盾发现引擎，基于上游分析结果检测 **5 大冲突模式**：

| 模式 | 检测内容 |
|------|---------|
| 盈利 vs 现金流背离 | 净利润改善但经营现金流/应收/存货恶化 |
| 估值 vs 基本面背离 | PE/PB 上升但盈利质量/ROE/增长下滑 |
| 行业 vs 公司背离 | 行业信号正面但公司数据走弱 |
| 表间不一致 | 利润表、资产负债表、现金流量表无法交叉验证 |
| 信号方向冲突 | 同维度内信号方向矛盾且强度相近 |

对每个检测到的冲突，生成 **3-5 个候选解释假设**，每个假设附带可验证的预测和验证清单，传递给下游假设验证节点。

## 6. 假设验证 (VerifyHypotheses)

对上一步生成的假设进行证据驱动验证，配备丰富工具：

- **`web_search`**：定性信息检索
- **`query_tushare`**：结构化财务/市场数据
- **`query_financial_report`**：本地已解析的公司财报检索（半年报/年报的一手披露内容）
- **东方财富研报工具**（8 个）：研报列表、研报详情、行业研报、PDF 下载等

每个假设归类为 `verified` / `partial` / `rejected` / `unknown` 四种裁决，输出包含支持证据、反驳证据、置信度评分和证据缺口列表。遵循严格的"唯证据论"原则——不做推测。

> **本地财报读取纪律**：`query_financial_report` 是读取 `reports/` 下财报的唯一途径，验证代理被明确约束不得用 `ls/glob/grep/read_file` 自行浏览或拼接报告原始 md 文件路径。

## 7. 洞察综合 (SynthesizeInsights)

`synthesize_insights` 节点会把信号、异常、冲突和验证结果压缩为中心洞察，作为 thesis 和 report 的高密度输入，减少下游直接消费原始中间产物的耦合。

## 8. 论点生成与审查 (ThesisEngine + Reviewer)

- **ThesisEngine**：8 维度加权综合评分（财务质量、盈利质量、信用风险、成长质量、资本配置效率、竞争壁垒、估值合理性、经营稳定性），每个维度有 5 级分层模板（strong_positive ~ strong_negative）
- **CriticEngine**：从信号层/维度层/系统层汇总质疑追问清单，按严重度去重排序
- **ThesisReviewer**：两层审查。Layer 1（确定性）检查零置信度/单证据/信号冲突/行业校准。Layer 2（可选 LLM）做定性评估
- **ThesisEnhancer**（可选 `--enhance`）：LLM 做跨信号模式识别、行业语境化、用户意图适配

## 9. 本地财报检索 (query_financial_report)

`alphabee/financial_report/` 子模块将公司财报/公告的 markdown 原文清洗并拆分为章节目录；`alphabee/tools/financial_report.py` 在其上提供 `query_financial_report` 工具，用受限 deep agent（`ls`/`grep`/`glob`/`read`）在报告目录内检索，回答关于公司自身披露内容的问题。

- **数据准备**：`report_parser.py` 清洗单文件 markdown（去除重复页眉页脚）并按章节拆分到 `reports/<公司名>：<年份><报告期>/`；工具按「公司名 + 年份 + 报告类型」在 `reports/` 下定位报告目录。
- **`query_financial_report(request)`**：输入 `FinancialReportRequest`（`company_name`/`company_code`、`year`、`report_type`、`query`）。先定位报告目录——无本地报告时返回 `None`（近零成本），有报告时驱动报告代理针对性检索，返回文本答案。
- **`resolve_company_name_by_code(code)`**：股票代码（`300750` / `300750.SZ`）→ 公司名称反查，供报告目录定位使用。
- **接入位置**：`verify_hypotheses` 节点已注册该工具，用公司自身披露的经营/财务/风险/展望事实核验假设；prompt 强制通过工具读取，禁止模型自行浏览 `reports/` 原始文件。
- **成本提示**：工具内部运行一次 LLM 检索代理（数秒~数十秒），查询越具体成本越低；无本地报告的标的不产生任何成本。

---

## 多期趋势分析

AnomalyEngine 的基线计算是系统多期能力的核心。每条勾稽关系规则取近 4 期历史（不含当期）计算均值和标准差：

```
baseline = avg(metric[t-4], metric[t-3], metric[t-2], metric[t-1])
z_score  = (current - baseline) / std
```

这使得系统能够区分"绝对值差但符合公司历史模式"和"本期突然偏离"——前者不是异常，后者才是。

`FinancialSnapshot` 模型支持最多 20 期快照，`to_fact_values()` 输出 11 个 `_prev` 后缀字段供环比分析。

---

## 报告结构

生成的报告包含以下章节：

| 章节 | 内容 |
|------|------|
| 核心发现 | 2-3 句总体判断 |
| 核心指标 | 5-8 个最重要的衍生指标及解读 |
| 风险信号 | 基础风险信号 + 异常模式信号（high→medium→low） |
| 勾稽关系异常检测 | 触发异常 + z-score + 排查路径 + 匹配模式 |
| 冲突与假设验证 | 检测到的跨维度矛盾 + 假设 + 证据验证结果 |
| 投资论点 | 8 维度判断/评分/置信度/证据链 |
| 审查发现 | blocking issues + warning issues |
| 主要风险 | thesis.primary_risks + review 阻断项 |
