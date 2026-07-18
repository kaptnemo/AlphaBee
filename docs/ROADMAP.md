# AlphaBee Analysis Agent Roadmap

## 背景判断

AlphaBee 当前已经具备较完整的“事实采集 → 衍生指标 → 风险信号 → 异常检测 → 冲突探索 → 论点汇总 → 报告生成”流水线，但核心短板是：系统能发现大量指标和风险，却还没有形成真正像公司财务分析师一样的主观点。

当前输出容易表现为：

- 指标很多，但主次不清
- 风险信号很多，但没有中心矛盾
- 结论是维度打分，而不是可争辩的投资论点
- 报告像数据堆砌，而不是观点驱动的研究备忘录

后续建设目标应从“财务指标检测系统”升级为“有洞见的公司财务分析 Agent”。

---

## 当前关键问题

### 1. ThesisEngine 只是聚合器，不是观点引擎

当前 `ThesisEngine` 主要做：

```text
signal level × thesis_impact direction → 按维度平均 → judgment
```

这会得到 `financial_quality=negative`、`earnings_quality=neutral` 这类结构化判断，但不会自然形成：

```text
公司当前核心矛盾是：市场仍按高成长定价，但财务数据已经显示增长质量下降；
应收扩张快于收入、现金流没有同步兑现，当前估值需要更强利润兑现能力支撑。
```

### 2. anomaly/conflict 没有充分进入 thesis 判断

已完成第一步：`AnomalyEngine` 现在会在 `SignalEngine` 之前运行，并把 anomaly facts 注入 signal 计算，使 anomaly signals 可以进入 thesis。

下一步仍需让 `ThesisEngine` 显式消费：

- `anomaly_report`
- `conflict_analysis`
- `verification_results`
- `company_context`

这样勾稽异常和已验证冲突才会真正改变核心论点，而不是只出现在报告附录。

### 3. Report Generator 被限制为格式化器

当前报告 prompt 明确要求“不是分析师，只做格式化和文字润色”。这能避免幻觉，但也意味着如果上游没有强观点，最终报告一定会变成结构化信息堆砌。

### 4. Reviewer 维度覆盖落后

Thesis dimensions 已扩展到：

```text
financial_quality
earnings_quality
growth_quality
credit_risk
valuation_fit
competitive_moat
capital_efficiency
operational_stability
```

但 reviewer prompt 仍明显偏向旧的 `financial_quality / earnings_quality / credit_risk` 三维度，需要同步升级审查逻辑。

### 5. 缺少重要性排序

当前系统更擅长覆盖，而不是取舍。真正的分析师需要回答：

- 最关键变量是什么？
- 哪个事实最能改变判断？
- 哪些异常是主矛盾？
- 哪些指标只是背景噪音？

---

## Roadmap

## Phase 0：修正当前链路的结构性问题

### 0.1 anomaly 进入 signal/thesis

状态：已完成第一版。

已实现：

```text
DerivedFacts
→ AnomalyEngine
→ 注入 anomaly fact_values
→ SignalEngine
→ ThesisEngine
```

已明确 anomaly signal dependencies：

- `anomaly_cluster_risk`
- `cross_validation_break`

后续可继续增强 anomaly signals，让二阶异常模式直接映射到 thesis 维度。

### 0.2 ThesisEngine 显式消费 anomaly/conflict

建议扩展接口：

```python
ThesisEngine.run(
    symbol,
    period,
    signal_results,
    anomaly_report=None,
    conflict_analysis=None,
    verification_results=None,
    company_context=None,
)
```

目标：

- 已验证 high/critical conflict 可以下调相关维度
- anomaly pattern 可以直接生成 thesis evidence
- rejected hypotheses 可以作为反向证据进入 thesis
- unknown hypotheses 可以进入 missing evidence

### 0.3 修复 canonical field / signal rule 不一致

重点检查：

- signal rules 是否只依赖 canonical fields
- `operating_cash_flow` vs `operating_cashflow` 这类字段不一致
- anomaly facts 是否有统一 schema 记录
- blocked/missing_fact 是否被误判为 none

---

## Phase 1：新增 InsightAgent，负责形成观点

这是从“数字堆砌”变成“观点驱动”的关键阶段。

建议新增：

```text
alphabee/agents/insights/
  models.py
  engine.py
  prompts.py
  agent.py
```

在 orchestrator 中插入：

```text
verify_hypotheses
→ synthesize_insights
→ run_thesis / review_thesis
→ generate_report
```

### InsightAgent 输出结构

```json
{
  "core_view": "一句话核心观点",
  "central_tension": "最关键矛盾",
  "main_driver": "决定结论的核心变量",
  "supporting_evidence": [],
  "counter_evidence": [],
  "materiality_rank": [],
  "business_model_context": "",
  "base_case": "",
  "bull_case": "",
  "bear_case": "",
  "what_would_change_my_mind": []
}
```

### InsightAgent 核心职责

- 从 signals / anomaly / conflicts / verification 中提炼中心矛盾
- 识别最重要的 1-3 个判断变量
- 区分主证据、反证和缺失证据
- 输出可证伪的观点，而不是指标摘要

---

## Phase 2：建立 Business Model Context 层

当前 company context 只有行业、生命周期、市值分类，无法支撑高质量财务解释。

建议新增：

```text
BusinessModelClassifier
```

输出：

```json
{
  "revenue_model": "to_b_credit_sales | to_c_cash_sales | project_based | subscription | commodity_cycle",
  "asset_intensity": "light | medium | heavy",
  "working_capital_pattern": "receivable_heavy | inventory_heavy | advance_payment | cash_conversion_fast",
  "cycle_sensitivity": "low | medium | high",
  "key_financial_pressure_points": [
    "accounts_receivable",
    "inventory",
    "capex",
    "gross_margin"
  ]
}
```

同样的财务信号在不同行业和商业模式下含义不同：

- 白酒的应收增长可能高度异常
- 军工的应收增长可能来自结算周期
- 软件公司的应收可能来自项目验收节奏
- 医药流通企业天然账期较重
- 光伏制造要结合库存、价格周期和资本开支

---

## Phase 3：从风险信号升级到论证图谱

建议引入 claim-evidence graph：

```json
{
  "claims": [
    {
      "claim": "公司增长质量下降",
      "stance": "bearish",
      "confidence": 0.72,
      "evidence_for": [],
      "evidence_against": [],
      "missing_evidence": [],
      "depends_on": []
    }
  ]
}
```

目标是让报告从：

```text
列指标、列风险、列维度
```

升级为：

```text
提出观点 → 给出证据 → 给出反证 → 指出还缺什么 → 说明什么会改变判断
```

示例：

```text
Claim: 增长质量下降

Evidence for:
- 应收增速高于收入增速
- 经营现金流/净利润下降
- 利润增速未显著超过收入增速

Evidence against:
- 毛利率仍稳定
- 行业账期可能普遍拉长

Missing:
- 应收账龄
- 前五大客户变化
- 同行应收周转天数
```

---

## Phase 4：接入市场预期 / 估值隐含假设

当前估值更多是静态指标：PE、PB、PEG、历史估值位置。

真正有洞见的分析需要回答：

```text
市场价格隐含了什么预期？
财务质量能不能支撑这个预期？
如果不能，风险在哪里？
```

建议新增：

```text
ExpectationFitAgent
```

输入：

- `pe_ttm`
- `pb`
- `roe`
- `net_profit_yoy`
- `revenue_yoy`
- 行业估值
- 历史估值
- 可选分析师预期

输出：

```json
{
  "implied_expectation": "市场当前定价隐含未来仍需维持高利润增长",
  "fundamental_support": "weak | medium | strong",
  "expectation_gap": "估值要求的增长质量高于当前财务数据能证明的水平",
  "de_rating_risk": "high"
}
```

---

## Phase 5：报告升级为投资研究备忘录

当前报告偏“体检报告”。建议升级为“观点优先”的研究备忘录。

目标结构：

```text
1. 核心观点
2. 最关键矛盾
3. 支撑证据
4. 反向证据
5. 商业模式语境
6. 估值 / 预期匹配
7. 情景分析：Bull / Base / Bear
8. 需要继续验证的 3 个问题
9. 结论置信度
```

原则：

```text
数字服务观点，而不是观点附着在数字后面。
```

---

## 推荐优先级

| 优先级 | 事项 | 价值 |
|---|---|---|
| P0 | anomaly 进入 signal/thesis | 修正当前链路断点 |
| P0 | ThesisEngine 显式消费 conflict/anomaly | 让高价值发现影响结论 |
| P1 | 新增 InsightAgent | 从数字堆砌变成观点生成 |
| P1 | 报告结构改成核心观点优先 | 立刻改善用户感知 |
| P2 | BusinessModelContext | 提升行业/公司语境判断 |
| P2 | Claim-Evidence Graph | 让观点可追踪、可审查 |
| P3 | ExpectationFitAgent | 打通财务质量与投资价值 |
| P3 | 同行基准 / 行业分位 | 降低固定阈值误判 |

---

## 下一步建议

短期最值得做的 3 件事：

1. 扩展 `ThesisEngine`，让 anomaly/conflict/verification 成为 thesis evidence。
2. 新增 `InsightAgent`，输出 `core_view / central_tension / evidence_for / evidence_against`。
3. 重构 `REPORT_GENERATOR_PROMPT`，让报告以 InsightAgent 的观点骨架为主线。

