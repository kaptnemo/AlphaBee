# 行业/产业语境注入方案

> **实现状态（2026-08 与代码对齐）**：本文档的“问题诊断”与当前代码一致——上游（derived_facts / signal / explore_conflicts / verify_hypotheses）仍无行业感知；`market_share_change` 规则因 `industry_revenue_yoy` 永远不在 `fact_values` 中而持续阻塞。已具备的基础：`build_company_context()` 已把 industry / sub_industry / lifecycle / market_cap 注入 `synthesize_insights` / `run_thesis` / `review_thesis`；`ThesisEngine` / `ThesisReviewer` 有少量行业常量调整。**Phase 1–6 的改造（IndustryContextArtifact、resolve_industry_context 节点、industry_thresholds、报告层行业字段）均未开始**（`alphabee/industry/` 目前为空包；`ArtifactType` 无 `INDUSTRY_CONTEXT`；`ReportGenerationPayload` 无 `industry` 字段）。

## 问题诊断

当前管道中存在明显的"语境断层"：**上游节点（derived_facts、signal、explore_conflicts、verify_hypotheses）完全无行业感知**，而下游节点（synthesize_insights、run_thesis、review_thesis）已有行业语境。这导致上层分析结论泛泛，无法区分"银行的高负债率是正常的"与"制造业的高负债率是危险的"。

### 现状总览

```
collect_raw_facts → run_analysis_engines → explore_conflicts → verify_hypotheses
  [无行业语境]          [无行业语境]          [无行业语境]          [无行业语境]

→ synthesize_insights → run_thesis → review_thesis → generate_report → review_report
  [已有行业语境]         [已有行业语境]    [已有行业语境]     [间接有语境]       [无行业语境]
```

### 核心缺口

1. **`fact_values` 不包含任何行业字段**：`OrchestratorState.fact_values: dict[str, float]` 只有公司级指标。`market_share_change` 规则需要 `industry_revenue_yoy`，该字段永远为空，规则永远被阻塞。
2. **21 个 derived_fact 规则的阈值是绝对的**：`debt_ratio > 0.65 → aggressive` 对所有行业一视同仁。银行杠杆率 90%+ 是常态，制造业 65% 就算激进。
3. **20 个 signal 规则的触发条件是绝对的**：`peg_ratio > 3 → high risk` 不区分行业。科技股 PEG=2 算便宜，食品股 PEG=2 算贵。
4. **explore_conflicts 和 verify_hypotheses 的 prompt 无行业参照系**：冲突探索只能做"数学上的背离"，无法判断"行业层面的合理性"。

### 已存在行业语境的位置

| 节点 | 数据来源 | 字段 |
|------|---------|------|
| `synthesize_insights` | `build_company_context()` | industry, sub_industry, lifecycle_stage, market_cap_category |
| `run_thesis` (ThesisEngine) | `build_company_context()` → `_apply_company_context()` | 高杠杆行业/金融行业/R&D重行业 特殊处理 |
| `run_thesis` (ThesisEnhancer) | `CompanyContext` 传给 LLM | 行业语境融入增强 |
| `review_thesis` (ThesisReviewer) | `build_company_context()` → `_HIGH_LEVERAGE_INDUSTRIES` 等常量 | 行业感知的审核规则 |

---

## 方案设计

整体思路从"每次个股分析时临时抽取行业上下文"升级为：

```
独立行业/产业知识工作流
→ 产出可版本化、可复用、可审计的行业知识资产
→ 个股分析阶段按股票所属行业加载最新可用上下文
→ 作为 `ArtifactType.INDUSTRY_CONTEXT` 注入 active orchestrator
```

核心原则：

1. **行业/产业分析单独成 workflow**：行业商业模式、产业链、景气度、估值中枢、竞争格局、关键变量不应在每次个股分析时临场生成。
2. **个股分析只负责解析和注入**：在线 pipeline 根据股票所属行业加载最新非过期 `IndustryContextArtifact`，不承担完整行业研究职责。
3. **行业上下文是一等 artifact**：完整结构进入 `artifacts`，下游通过 `find_artifact_model(...)` 消费；不要新增 `OrchestratorState.industry_context` 这类专用字段。
4. **数值型基准按 canonical facts 进入 `fact_values`**：只有 derived facts / signals 需要计算的少量行业均值字段进入 flat values。
5. **外部数据源字段不得泄漏到下游**：Tushare、AkShare、东方财富等字段必须先经过 adapter / mapping，统一成 AlphaBee canonical fields。

### 一、独立行业/产业知识工作流

新增 `IndustryResearchAgent` / `IndustryContextWorkflow`，作为离线或准离线任务运行：

```text
collect_industry_facts
→ normalize_industry_schema
→ derive_industry_benchmarks
→ synthesize_industry_context
→ review_industry_context
→ persist_industry_profile
```

#### 1.1 节点职责

| 节点 | 职责 | 主要产物 |
|------|------|----------|
| `collect_industry_facts` | 采集行业指数、板块行情、财务分布、产业链、政策/新闻、代表公司 | 标准化行业 facts |
| `normalize_industry_schema` | 外部字段映射到 AlphaBee canonical fields，统一单位、频率、口径和来源 metadata | canonical industry facts |
| `derive_industry_benchmarks` | 计算估值、盈利、杠杆、成长、现金流等行业基准 | quantitative benchmarks |
| `synthesize_industry_context` | 总结商业模式、产业链位置、周期阶段、关键驱动和风险 | qualitative context |
| `review_industry_context` | 审核数据新鲜度、口径一致性、结论是否有证据支撑 | review result |
| `persist_industry_profile` | 按行业、日期、版本持久化可复用知识资产 | versioned profile |

#### 1.2 更新机制

行业知识不是每次个股分析实时重算，而是按以下方式不定时更新：

- **定期更新**：月度/季度刷新估值中枢、财务分布、行业景气度。
- **事件触发更新**：重大政策、行业指数大幅波动、代表公司财报密集披露、产业链价格剧烈变化时触发。
- **人工触发更新**：分析师发现行业上下文过期或需要专题研究时手动运行。
- **过期降级**：若 `stale_after` 已过期，个股 pipeline 仍可读取但必须标记为 stale，并降低下游置信度或在报告中提示。

#### 1.3 持久化与版本

行业知识资产需要带版本和血缘，避免报告引用不可追踪的上下文：

```python
class IndustryContextArtifact(BaseModel):
    schema_version: str
    industry: str
    sub_industry: str | None = None
    classification_standard: str  # sw_l1 / sw_l2 / ths / custom
    as_of_date: str
    generated_at: str
    stale_after: str | None = None
    source_refs: list[str] = []
    confidence: float | None = None

    lifecycle_stage: str | None = None
    business_model_summary: str | None = None
    industry_chain: dict[str, list[str]] = {}
    key_drivers: list[str] = []
    risk_factors: list[str] = []

    valuation_benchmarks: dict[str, float | None] = {}
    financial_benchmarks: dict[str, float | None] = {}
    growth_benchmarks: dict[str, float | None] = {}
    peer_universe: list[str] = []
    peer_count: int | None = None

    review_status: str | None = None
    review_notes: list[str] = []
```

其中 `valuation_benchmarks` / `financial_benchmarks` / `growth_benchmarks` 内的 key 必须使用 AlphaBee canonical field，例如：

```python
valuation_benchmarks = {
    "industry_pe_ttm_median": 18.5,
    "industry_pb_lf_median": 2.1,
}
financial_benchmarks = {
    "industry_avg_roe": 0.15,
    "industry_avg_debt_ratio": 0.45,
    "industry_avg_gross_margin": 0.32,
}
growth_benchmarks = {
    "industry_revenue_yoy": 0.12,
}
```

外部源字段如 `stock_board_industry_summary_ths` 返回列名、Tushare 指数字段、东方财富字段，只能出现在 adapter / mapping 层，不能出现在 derived facts、signals、thesis、reporter 中。

### 二、在线个股分析注入层

在 active orchestrator 中新增轻量节点 `resolve_industry_context`，插入在 `collect_raw_facts` 之后、`run_analysis_engines` 之前：

```text
collect_raw_facts
→ resolve_industry_context
→ run_analysis_engines
→ explore_conflicts
→ verify_hypotheses
→ synthesize_insights
→ run_thesis
→ review_thesis
→ generate_report
→ review_report
→ finalize_message
```

`resolve_industry_context` 不重新做完整行业研究，只做三件事：

1. 调用 `build_company_context()` 或公司画像事实解析股票所属行业、子行业、生命周期、规模分层。
2. 从行业知识存储中读取最新非过期 `IndustryContextArtifact`；若没有非过期版本，读取最新版本并标记 stale，或返回 `None` 降级。
3. 把完整 `IndustryContextArtifact` 写入 `artifacts`，并把少量数值型 canonical benchmark 注入 `fact_values`。

示例：

```python
fact_values.update({
    "industry_revenue_yoy": 0.12,
    "industry_avg_roe": 0.15,
    "industry_avg_debt_ratio": 0.45,
    "industry_pe_ttm_median": 18.5,
    "industry_pb_lf_median": 2.1,
})
```

完整行业上下文不应作为 `OrchestratorState` 顶层字段保存，而是作为 artifact 保存：

```python
Artifact(
    type=ArtifactType.INDUSTRY_CONTEXT,
    value=industry_context.model_dump(),
    producer_step="resolve_industry_context",
)
```

### 三、重构 `run_analysis_engines`

#### 3.1 衍生指标引擎行业感知

为 YAML 规则新增可选的 `industry_thresholds` 字段：

```yaml
# debt_ratio.yaml（改造后）
thresholds:
  conservative: "value < 0.40"
  moderate: "0.40 <= value <= 0.65"
  aggressive: "value > 0.65"
industry_thresholds:
  银行:
    conservative: "value < 0.88"
    moderate: "0.88 <= value <= 0.93"
    aggressive: "value > 0.93"
  房地产:
    conservative: "value < 0.60"
    moderate: "0.60 <= value <= 0.80"
    aggressive: "value > 0.80"
```

引擎改造：`DerivedFactsEngine.run()` 新增 `industry_context` 参数，行业匹配时用行业阈值覆盖默认阈值。

行业上下文来源：

```python
industry_context = find_artifact_model(
    artifacts,
    ArtifactType.INDUSTRY_CONTEXT,
    IndustryContextArtifact,
)
```

#### 3.2 信号引擎行业感知

信号规则新增 `industry_trigger_rules`：

```yaml
# debt_risk.yaml（改造后）
industry_trigger_rules:
  银行:
    high:
      condition: debt_ratio > 0.95 and current_ratio < 0.5
    medium:
      condition: debt_ratio > 0.93
```

引擎改造：`SignalEngine.run()` 新增 `industry_context` 参数，行业匹配时优先使用行业触发规则。

### 四、冲突探索与假设验证行业感知

- **`explore_conflicts`**：payload 新增 `industry` 字段（行业名、PE/PB 均值、负债率均值等），prompt 增加"评估指标偏离时必须参考行业上下文"
- **`verify_hypotheses`**：`shared_context` 新增 `industry` 字段，验证 prompt 增加"涉及估值应对比行业均值"

### 五、报告层行业感知

- **`ReportGenerationPayload`** 新增顶层 `industry: ReportIndustryPayload | None`
- **`review_report`** 增加行业感知检查：报告是否包含行业对比、估值判断是否参考行业均值

### 六、`OrchestratorState` 与 artifact 契约

不新增 `OrchestratorState.industry_context`。完整行业上下文通过 `artifacts` 传递，避免破坏当前 orchestrator artifact contract。

只在 `fact_values` 中新增计算所需的 canonical 数值字段：

```python
"industry_revenue_yoy": 0.12,
"industry_avg_roe": 0.15,
"industry_avg_debt_ratio": 0.45,
"industry_pe_ttm_median": 18.5,
"industry_pb_lf_median": 2.1,
```

这些字段需要进入 canonical schema / industry mapping，并由 adapter 层负责单位和口径转换。

---

## 实施路径

### Phase 1：行业知识工作流基础设施

1. 新增 `IndustryContextArtifact` 合约 → `alphabee/orchestrator/contracts.py` 或行业知识专用 contracts 模块
2. 新增 `ArtifactType.INDUSTRY_CONTEXT` → `alphabee/core/schemas.py`
3. 新增 `IndustryResearchAgent` / `IndustryContextWorkflow`
4. 新增行业知识持久化接口：按 `classification_standard + industry + sub_industry + as_of_date + schema_version` 存取
5. 新增 stale / confidence / source_refs / review_status 等元数据

### Phase 2：字段治理与数据源适配

6. 在 canonical schema 中补充行业 benchmark 字段（如 `industry_revenue_yoy`、`industry_avg_roe`、`industry_avg_debt_ratio`、`industry_pe_ttm_median`、`industry_pb_lf_median`）
7. 新增或完善 Tushare / AkShare / 东方财富行业字段 mapping
8. 确保外部字段只存在于 adapter / mapping 层，下游统一使用 canonical field
9. 利用申万行业指数、同花顺板块摘要、东方财富快照等来源生成标准化 industry facts

### Phase 3：在线注入层

10. 新增 `resolve_industry_context` 节点 → `alphabee/orchestrator/nodes/`
11. 更新 graph topology，插入在 `collect_raw_facts` → `run_analysis_engines` 之间
12. 将完整 `IndustryContextArtifact` 写入 `artifacts`
13. 将少量数值型 industry benchmark 注入 `fact_values`
14. 缺失或过期时显式降级：`industry_context=None` 或 `stale=True`，下游回退默认阈值并保留提示

### Phase 4：引擎行业感知

15. `DerivedFactsEngine.run()` 支持 `industry_thresholds`
16. `SignalEngine.run()` 支持 `industry_trigger_rules`
17. 为 5-8 个最受益的规则添加行业感知阈值（优先：debt_ratio、roe_level、peg_ratio、interest_coverage、asset_turnover）

### Phase 5：冲突、验证与观点合成行业感知

18. `explore_conflicts` payload / prompt 增强
19. `verify_hypotheses` shared context / prompt 增强
20. `synthesize_insights` 消费 `IndustryContextArtifact`，把行业主线纳入中心观点

### Phase 6：报告层与持久化边界

21. `ReportGenerationPayload` 新增 `industry` 字段
22. 报告 prompt 模板更新
23. `review_report` 新增行业感知检查项
24. `finalize_message` 输出必要的 industry context metadata
25. `task_records/recorder.py` 同步读取最终行业上下文摘要与版本信息

---

## 关键设计决策

### 为什么行业/产业分析要单独成 workflow？

行业知识具有跨个股复用价值，不应绑定到单次个股分析运行。独立 workflow 可以沉淀行业商业模式、产业链、估值中枢、关键驱动和风险，并通过版本、数据日期、source_refs、review_status 保证可追踪。

### 为什么在线节点叫 `resolve_industry_context`，而不是 `extract_industry_context`？

在线阶段的职责是"解析股票所属行业并加载已有行业上下文"，不是重新做完整行业研究。命名为 `resolve` 可以避免把离线研究和在线注入混在一起。

### 为什么不直接在 `collect_raw_facts` 中注入行业数据？

`collect_raw_facts` 的职责是"从用户输入提取事实"，行业数据应由独立的专业节点负责。分离后：
- `collect_raw_facts` 保持纯粹，不因行业数据获取失败而阻塞管道
- `resolve_industry_context` 失败时管道可降级（industry=None → 所有引擎回退到默认阈值）

### 为什么不新增 `OrchestratorState.industry_context`？

当前 active orchestrator 的约定是节点产物进入 `artifacts`，下游通过 typed contract 消费。`IndustryContextArtifact` 是新的中间产物，应作为 `ArtifactType.INDUSTRY_CONTEXT` 写入 artifacts，避免把每个新产物都扩展成 `OrchestratorState` 顶层字段。

### 为什么仍要把少量行业字段写入 `fact_values`？

`DerivedFactsEngine` 和 `SignalEngine` 需要数值型输入才能参与公式和规则判断。完整行业上下文用于解释和报告，少量 canonical benchmark 用于计算，两者职责不同。

### 为什么用 YAML 配置行业阈值而不是硬编码？

- 与现有架构一致：派生指标规则和信号规则已全部 YAML 化
- 分析师可直接修改 YAML 调整行业标准，无需改 Python 代码

### 行业均值从哪里来？

优先级链：申万行业指数（Tushare）→ 同花顺板块摘要（AkShare `stock_board_industry_summary_ths`）→ 东方财富快照（仅作回退）。

所有来源必须先通过 adapter / mapping 标准化为 AlphaBee canonical fields，并保留 source metadata、单位、日期和口径。

### 行业阈值初始值从哪来？

保守策略：只对已确认有显著行业差异的行业（银行、房地产、非银金融、公用事业）设置特殊阈值，其余行业用通用阈值。

### 行业知识过期怎么办？

`IndustryContextArtifact` 必须带 `as_of_date`、`generated_at` 和可选 `stale_after`。在线 pipeline 读取到过期版本时不应静默当作新数据使用，而应标记 stale，降低置信度，必要时在 report / review 中提示"行业上下文可能过期"。

---

## 预期收益

| 维度 | 现状 | 改造后 |
|------|------|--------|
| 银行负债率 92% | signal: debt_risk=high | signal: debt_risk=none（行业正常水平） |
| 科技股 PEG=2.5 | signal: valuation_risk=medium | signal: valuation_risk=low（科技行业正常） |
| 制造业 ROE=6% | derived: roe_level=weak | derived: roe_level=good（行业对比） |
| conflict "高负债 vs 高利润" | 标记为冲突 | 标记为非冲突（金融行业特征） |
| 报告质量 | 通用分析，缺乏行业定位 | 有行业定位 + 行业基准对比 |
| 知识复用 | 每次分析重复生成行业判断 | 行业知识沉淀为可版本化资产，多只股票共享 |
| 可追踪性 | 行业语境来源不稳定 | 带 `schema_version`、`as_of_date`、`source_refs`、`review_status` |
