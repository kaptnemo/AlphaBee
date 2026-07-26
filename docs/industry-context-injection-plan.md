# 行业/产业语境注入方案

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

整体思路是**在管道中段注入行业/产业语境，向上游和下游双向传播**：新增 `extract_industry_context` 节点插入在 `collect_raw_facts` 之后、`run_analysis_engines` 之前，使行业数据成为一等公民。

### 一、新增节点：`extract_industry_context`

```
collect_raw_facts → [extract_industry_context] → run_analysis_engines → ...
```

**位置**：在 `collect_raw_facts` 之后、`run_analysis_engines` 之前。

**职责**：
1. 调用 `build_company_context()` 获取行业标签（industry、sub_industry、lifecycle_stage、market_cap_category）
2. 调用 `get_industry_fact(symbol)` 获取行业估值数据（PE/PB 均值、成分股数量等）
3. 产出 `IndustryContextArtifact`（新 `ArtifactType`）写入 `artifacts` 列表

**`IndustryContextArtifact` 结构**：

```python
class IndustryContextArtifact(BaseModel):
    industry: str                # 申万一级行业
    sub_industry: str            # 申万二级行业
    lifecycle_stage: str         # growth / mature / decline / cyclical
    market_cap_category: str     # large / mid / small
    industry_pe_ttm: float       # 行业平均 PE(TTM)
    industry_pb: float           # 行业平均 PB
    industry_avg_roe: float      # 行业平均 ROE
    industry_avg_debt_ratio: float
    industry_avg_gross_margin: float
    industry_revenue_yoy: float  # 行业营收增速
    peer_count: int              # 可比公司数量
    business_model_summary: str
```

### 二、重构 `run_analysis_engines`

#### 2.1 衍生指标引擎行业感知

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

#### 2.2 信号引擎行业感知

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

### 三、冲突探索与假设验证行业感知

- **`explore_conflicts`**：payload 新增 `industry` 字段（行业名、PE/PB 均值、负债率均值等），prompt 增加"评估指标偏离时必须参考行业上下文"
- **`verify_hypotheses`**：`shared_context` 新增 `industry` 字段，验证 prompt 增加"涉及估值应对比行业均值"

### 四、报告层行业感知

- **`ReportGenerationPayload`** 新增顶层 `industry: ReportIndustryPayload | None`
- **`review_report`** 增加行业感知检查：报告是否包含行业对比、估值判断是否参考行业均值

### 五、`OrchestratorState` 扩展

在 `fact_values` 中新增行业均值字段：

```python
"industry_revenue_yoy": 0.12,
"industry_avg_roe": 0.15,
"industry_avg_debt_ratio": 0.45,
```

---

## 实施路径

### Phase 1：基础设施

1. 新增 `IndustryContextArtifact` 合约 → `alphabee/orchestrator/contracts.py`
2. 新增 `ArtifactType.INDUSTRY_CONTEXT` → `alphabee/core/schemas.py`
3. 新增 `extract_industry_context` 节点 → `alphabee/orchestrator/nodes/`
4. 更新 graph topology，插入在 `collect_raw_facts` → `run_analysis_engines` 之间
5. 扩展 `OrchestratorState`

### Phase 2：引擎行业感知

6. `DerivedFactsEngine.run()` 支持 `industry_thresholds`
7. `SignalEngine.run()` 支持 `industry_trigger_rules`
8. 为 5-8 个最受益的规则添加行业感知阈值（优先：debt_ratio、roe_level、peg_ratio、interest_coverage、asset_turnover）

### Phase 3：冲突与验证行业感知

9. `explore_conflicts` prompt 增强
10. `verify_hypotheses` prompt 增强

### Phase 4：报告层行业感知

11. `ReportGenerationPayload` 新增 `industry` 字段
12. 报告 prompt 模板更新
13. `review_report` 新增行业感知检查项

### Phase 5：数据源完善

14. 填充 `fact_values` 中的行业均值字段
15. 利用 `stock_board_industry_summary_ths()` + Tushare 申万指数获取行业 PE/PB/ROE 中位数

---

## 关键设计决策

### 为什么不直接在 `collect_raw_facts` 中注入行业数据？

`collect_raw_facts` 的职责是"从用户输入提取事实"，行业数据应由独立的专业节点负责。分离后：
- `collect_raw_facts` 保持纯粹，不因行业数据获取失败而阻塞管道
- `extract_industry_context` 失败时管道可降级（industry=None → 所有引擎回退到默认阈值）

### 为什么用 YAML 配置行业阈值而不是硬编码？

- 与现有架构一致：派生指标规则和信号规则已全部 YAML 化
- 分析师可直接修改 YAML 调整行业标准，无需改 Python 代码

### 行业均值从哪里来？

优先级链：申万行业指数（Tushare）→ 同花顺板块摘要（akshare summary_ths）→ 东方财富快照（akshare name_em，仅作回退）

### 行业阈值初始值从哪来？

保守策略：只对已确认有显著行业差异的行业（银行、房地产、非银金融、公用事业）设置特殊阈值，其余行业用通用阈值。

---

## 预期收益

| 维度 | 现状 | 改造后 |
|------|------|--------|
| 银行负债率 92% | signal: debt_risk=high | signal: debt_risk=none（行业正常水平） |
| 科技股 PEG=2.5 | signal: valuation_risk=medium | signal: valuation_risk=low（科技行业正常） |
| 制造业 ROE=6% | derived: roe_level=weak | derived: roe_level=good（行业对比） |
| conflict "高负债 vs 高利润" | 标记为冲突 | 标记为非冲突（金融行业特征） |
| 报告质量 | 通用分析，缺乏行业定位 | 有行业定位 + 行业基准对比 |
