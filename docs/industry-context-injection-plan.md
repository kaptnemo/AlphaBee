# 行业/产业语境注入方案

> **实现状态（2026-08 与代码对齐）**：本文档的“问题诊断”与当前代码一致——上游（derived_facts / signal / explore_conflicts / verify_hypotheses）仍**不消费任何行业数值**；`market_share_change` 规则因 `industry_revenue_yoy` 永远不在 `fact_values` 中而持续阻塞。已具备的基础比初版认知更多：**`industry_fact` 工具已存在**（`alphabee/agents/facts/tools/industry_fact.py`，返回申万一级行业 / SW 指数代码 / 行业 PE/PB），`build_company_context()` 已把 industry / sub_industry / lifecycle / market_cap 与行业 PE/PB 摘要注入 `synthesize_insights` / `run_thesis` / `review_thesis`（`contracts.py` 的 `ThesisIndustryContext`）；`ThesisEngine` / `ThesisReviewer` 有少量行业常量。真正缺的只有三样：① 行业 **financial/growth 基准**（ROE/负债率/营收增速中位数）——目前无人计算；② 把基准注入 `fact_values` 供规则引用；③ 行业阈值/基准相对阈值机制。**改造（IndustryContextArtifact、resolve_industry_context 节点、industry_thresholds、报告层行业字段）均未开始**（`alphabee/industry/` 目前为空包；`ArtifactType` 无 `INDUSTRY_CONTEXT`；`ReportGenerationPayload` 无 `industry` 字段）。

> **评审修订记录（2026-08）**：本次评审按代码核实修订了现状认知，并落地以下优化——① 实施路径重排为「垂直切片先行、研究工作流后置」；② 新增「基准相对阈值」免费机制（两引擎阈值表达式本就能引用 `fact_values` 任意字段，见 `registry.py` 的 `threshold_context`）；③ 行业匹配键改为 `classification_standard + 行业代码`，消除中文名硬编码漂移；④ 补降级契约（复用 0.4 的 degraded 模式）；⑤ 与 `DOMAIN_CONTEXT_ROADMAP.md` 划界（本文档收敛为**数值基准层**）；⑥ 补测试计划。

> **Phase 0 实施状态（2026-08）：✅ 已落地**。垂直切片完成：`alphabee/industry/benchmarks.py`（中位数推导）、`alphabee/industry/data.py`（Tushare 成分股取数，best-effort）、`alphabee/orchestrator/nodes/resolve_industry_context.py`（识别 + 注入 + 降级契约，已插入 `collect_raw_facts → run_analysis_engines` 之间）、`ArtifactType.INDUSTRY_CONTEXT` + `IndustryContextArtifact`、引擎阈值回退链（`registry.py`，level 表达式支持列表）、`debt_ratio / roe_level` 相对基准 + `market_share_change` 复活（peg_ratio 保持绝对，行业判断留给 signal 层）。测试：`tests/industry/test_benchmarks.py`、`tests/orchestrator/test_resolve_industry_context.py`、`tests/agents/derived_facts/test_industry_thresholds.py`。Phase 1-6 未开始。

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

> **补充说明（评审核实）**：行业**估值**基准（PE/PB）已有部分来源——`get_industry_fact()` 返回 `industry_pe_ttm / industry_pb` 并已进入 `ThesisIndustryContext`；缺的是**财务/成长类基准**（ROE、负债率、营收增速中位数）及其进入 `fact_values` 的通道。所以落地重心应从「从零建行业数据获取」改为「补齐财务/成长基准 + 打通注入 + 建立阈值机制」。

### 已存在行业语境的位置

| 节点 | 数据来源 | 字段 |
|------|---------|------|
| `synthesize_insights` | `build_company_context()` | industry, sub_industry, lifecycle_stage, market_cap_category |
| `run_thesis` (ThesisEngine) | `build_company_context()` → `_apply_company_context()` | 高杠杆行业/金融行业/R&D重行业 特殊处理 |
| `run_thesis` (ThesisEnhancer) | `CompanyContext` 传给 LLM | 行业语境融入增强 |
| `review_thesis` (ThesisReviewer) | `build_company_context()` → `_HIGH_LEVERAGE_INDUSTRIES` 等常量 | 行业感知的审核规则 |
| `industry_fact` 工具 | `get_industry_fact()`（申万分类 + 行业指数 + 行业 PE/PB） | industry, sw_code, industry_pe_ttm, industry_pb |

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

1. **行业/产业分析单独成 workflow（后置增强，非前置依赖）**：行业商业模式、产业链、景气度、估值中枢、竞争格局、关键变量不应在每次个股分析时临场生成。但完整研究工作流**不是第一步**——垂直切片（见「实施路径」Phase 0）先行见效，工作流作为增量演进。
2. **个股分析只负责解析和注入**：在线 pipeline 根据股票所属行业加载最新非过期 `IndustryContextArtifact`，不承担完整行业研究职责。
3. **行业上下文是一等 artifact**：完整结构进入 `artifacts`，下游通过 `find_artifact_model(...)` 消费；不要新增 `OrchestratorState.industry_context` 这类专用字段。
4. **数值型基准按 canonical facts 进入 `fact_values`**：只有 derived facts / signals 需要计算的少量行业均值字段进入 flat values。
5. **外部数据源字段不得泄漏到下游**：Tushare、AkShare、东方财富等字段必须先经过 adapter / mapping，统一成 AlphaBee canonical fields。
6. **基准相对阈值优先，逐行业绝对覆盖其次**：两个引擎的阈值/触发表达式本就能引用 `fact_values` 任意字段（`registry.py` 的 `threshold_context = {**fact_values, "value": ...}`，SignalEngine 同理），因此「相对行业均值」的规则（如 `value > industry_avg_debt_ratio * 1.05`）**零引擎改动**即可生效，且覆盖所有行业；`industry_thresholds`（逐行业绝对带）只留给结构性差异行业（银行/房地产/公用事业），减少 YAML 维护量。
7. **范围划界**：本文档只负责**数值基准层**（估值/财务/成长中位数、阈值机制、注入通道）；定性解释层（商业模式、产业链叙事、关键驱动与风险的 playbook 化）归 `DOMAIN_CONTEXT_ROADMAP.md`，两者按「数值 vs 定性」分工，避免静态行业知识库与动态 primitives/playbooks 两套体系互斥。

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

**存储后端选型（v1 从简）**：每行业一个 JSON 快照文件（`data/industry_profiles/{classification_standard}/{industry_code}.json`，原子写：先写临时文件再 rename），天然可版本化、可 diff、可人工 review；`stale_after` 默认按基准类别给建议值（估值基准 7-30 天、财务基准 90 天、定性描述月度），后续需要检索/血缘再迁 SQLite 或复用 `data_fetch` 存储，不在 v1 过度设计。

**行业匹配键（B1 优化）**：阈值与基准的匹配**一律用 `classification_standard + 行业代码`**（如 `sw_l1 + 801780`），显示名（"银行"）只在展示层解析。理由：`build_company_context` 的关键词抽取、`reviewer.py` 的 `_HIGH_LEVERAGE_INDUSTRIES`、`engine.py` 的 `_FINANCIAL_INDUSTRIES` 三处硬编码中文名已存在漂移风险，新机制不能再引入第四处。落地时把行业名规范收敛为**单一字典**（`industry_code ↔ 显示名`），三处硬编码逐步迁移引用该字典。

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

#### 2.1 多行业匹配与降级契约

**匹配优先级（C7）**：公司可能映射多个行业分类，按 `classification_standard` 优先级解析：`sw_l1 > sw_l2 > ths > custom`；精确键（standard+code）未命中时，跨标准按**行业名规范字典**回退查找；仍找不到 → `industry_context=None` 降级。

**降级契约（B2，复用 ROADMAP 0.4 的 degraded 模式，不重新发明）**：

- `IndustryContextArtifact` 增加 `degraded: bool` 与 `stale: bool` 字段（默认 False），随 artifact 落库；
- 缺失时：不发 artifact，发 `industry_context_missing` issue（MEDIUM）——**静默回退不可观测，必须显式留痕**；
- 过期时：正常产出 artifact 但 `stale=True`，发 `industry_stale` issue（MEDIUM），下游置信度按档位下调（如维度置信度 ×0.9），报告 prompt 增加「行业上下文可能过期」分支；
- `review_report` gate 对 `industry_stale` 要求报告显式披露（与 `verified_conflict` 同机制，进 `disclosed_issue_ids` 检查）。

### 三、重构 `run_analysis_engines`

#### 3.0 基准相对阈值（免费机制，优先落地，A3）

两个引擎的表达式求值**本来就能引用 `fact_values` 任意字段**：

- 衍生引擎：`registry.py` `threshold_context = {**fact_values, "value": derived_value}`
- 信号引擎：`extended_values = fact_values + derived` 后逐条求值

因此只要把行业基准注入 `fact_values`，绝大多数「行业相对判断」**零引擎改动**：

```yaml
# debt_ratio.yaml（相对基准，覆盖所有行业，无需逐行业维护）
thresholds:
  conservative: "value < industry_avg_debt_ratio * 0.8"
  moderate: "value <= industry_avg_debt_ratio * 1.2"
  aggressive: "value > industry_avg_debt_ratio * 1.2"
```

```yaml
# valuation_risk.yaml 信号侧（同理）
trigger_rules:
  medium:
    condition: peg_ratio > 1.5 and peg_ratio > industry_peg_ratio_median * 1.2
```

**缺失字段回退**：行业基准字段不在 `fact_values` 时，表达式求值抛异常 → 被跳过（衍生引擎 `except: continue`）→ 落到后续绝对阈值，天然构成「行业缺失 → 回退默认」链，语义清晰、无需引擎改动。**唯一注意**：相对表达式必须排在绝对表达式**之前**，否则绝对阈值总是先命中。

#### 3.1 衍生指标引擎行业感知（结构性覆盖，增量）

为 YAML 规则新增可选的 `industry_thresholds` 字段——**只用于结构性差异行业**（银行/房地产/公用事业等绝对带不同者），key 用 `classification_standard + 行业代码`（见 1.3 的 B1 优化），不是中文名：

```yaml
# debt_ratio.yaml（改造后）
thresholds:
  conservative: "value < 0.40"
  moderate: "0.40 <= value <= 0.65"
  aggressive: "value > 0.65"
industry_thresholds:
  sw_l1:801780:   # 银行（申万一级，代码以分类标准为准）
    conservative: "value < 0.88"
    moderate: "0.88 <= value <= 0.93"
    aggressive: "value > 0.93"
  sw_l1:801951:   # 房地产
    conservative: "value < 0.60"
    moderate: "0.60 <= value <= 0.80"
    aggressive: "value > 0.80"
```

引擎改造：`DerivedFactsEngine.run()` 新增可选 `industry_context` 参数，按 `classification_standard + 行业代码` 匹配，命中时用行业阈值覆盖默认阈值；`industry_context=None` 时完全回退默认阈值（**向后兼容，现有调用方 `nodes/analyze.py` 与测试无需改动即可继续工作**）。

行业上下文来源：

```python
industry_context = find_artifact_model(
    artifacts,
    ArtifactType.INDUSTRY_CONTEXT,
    IndustryContextArtifact,
)
```

#### 3.2 信号引擎行业感知

优先走 3.0 的**基准相对条件**（零引擎改动）；`industry_trigger_rules` 只留给结构性差异行业，key 同样用 `classification_standard + 行业代码`：

```yaml
# debt_risk.yaml（改造后）
industry_trigger_rules:
  sw_l1:801780:   # 银行
    high:
      condition: debt_ratio > 0.95 and current_ratio < 0.5
    medium:
      condition: debt_ratio > 0.93
```

引擎改造：`SignalEngine.run()` 新增可选 `industry_context` 参数（默认 None 向后兼容），行业匹配时优先使用行业触发规则；未命中回退默认 `trigger_rules`。

### 四、冲突探索与假设验证行业感知

- **`explore_conflicts`**：payload 新增 `industry` 字段（行业名、PE/PB 均值、负债率均值等），prompt 增加"评估指标偏离时必须参考行业上下文"
- **`verify_hypotheses`**：`shared_context` 新增 `industry` 字段，验证 prompt 增加"涉及估值应对比行业均值"

### 五、报告层行业感知

- **`ReportGenerationPayload`** 新增顶层 `industry: ReportIndustryPayload | None`，字段明确为：
  ```python
  class ReportIndustryPayload(BaseModel):
      industry: str = ""
      sub_industry: str = ""
      classification_standard: str = ""   # sw_l1 / sw_l2 / ths / custom
      as_of_date: str = ""
      stale: bool = False                 # 行业上下文是否过期
      benchmarks: dict[str, float | None] = {}   # 少量展示用基准子集（PE/PB 中位数、ROE、负债率）
  ```
- **`review_report`** 增加行业感知检查（3 条可执行检查）：
  1. `industry` 存在时，报告必须包含至少一处行业对比（估值或财务指标 vs 行业基准）；
  2. `industry.stale=True` 时，报告必须显式写出「行业上下文可能过期」（进 `disclosed_issue_ids` 检查，同 `industry_stale` issue 机制）；
  3. `industry=None` 时，报告不得声称做过行业对比（避免虚构基准）。
- **`finalize_message`（C5，遵循 0.6 输出分层）**：行业版本/血缘元数据只进 `artifacts` 与调试视图，**不写入用户可见的 final_report**；`task_records/recorder.py` 读取摘要与版本信息用于观测。

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

**口径风险（B3，`market_share_change` 前置条件）**：`revenue_yoy - industry_revenue_yoy` 要求两侧**周期与口径对齐**——公司 yoy 来自财报期（季/年报），行业 yoy 若来自申万指数成分加权或不同频率，直接相减会产生错配。normalize 层必须显式对齐：行业营收增速按**同一报告期口径**计算（成分股定期报告汇总），并在 `source_refs` 记录口径说明；无法对齐时该字段置空并让规则回到 blocked（而不是产出错误数值）。

**`peer_universe / peer_count` 消费者（B4）**：v1 中 `peer_universe` 仅供报告「行业对比」章节引用（展示基准计算覆盖的家数，增强可信度）；**不做**跨个股分位数引擎（避免范围蔓延）。若 v2 需要分位判断，再单独设计，届时 `peer_universe` 才有第二消费者。

---

## 实施路径

> **排序原则（A1 评审优化）**：垂直切片先行、研究工作流后置。Phase 0 单独一次交付即可解决文档开头列举的即时痛点（`market_share_change` 阻塞、银行 92% 误报、科技股 PEG 误判）；Phase 1–6 为增量演进，其中 Phase 1 的完整研究工作流可推迟到 Phase 0 验证机制后再启动。

### Phase 0：垂直切片（✅ 已实施，2026-08）

0.1 ✅ canonical schema 补 3 个行业财务基准字段（`industry_avg_roe` / `industry_avg_debt_ratio` / `industry_avg_gross_margin`；`industry_revenue_yoy` 原已存在）
0.2 ✅ 行业基准推导（`alphabee/industry/benchmarks.py`，中位数）+ 成分股取数（`alphabee/industry/data.py`，Tushare index_member + fina_indicator，best-effort）
0.3 ✅ `resolve_industry_context` 节点（含降级契约），已插入 `collect_raw_facts → run_analysis_engines` 之间；基准注入 `fact_values`，完整上下文写 `INDUSTRY_CONTEXT` artifact
0.4 ✅ 引擎阈值回退链（`registry.py` level 表达式支持列表）；`debt_ratio` / `roe_level` 改相对基准（缺失回退绝对阈值）；`market_share_change` 接 `industry_revenue_yoy` 复活；`peg_ratio` 保持绝对（PEG 已按公司成长归一化，行业判断留给 signal 层）
0.5 ⏭️ 结构性行业阈值（`industry_thresholds`）未做——相对基准机制已覆盖银行场景，按 Phase 4 增量推进
0.6 ✅ 测试：`tests/industry/test_benchmarks.py`、`tests/orchestrator/test_resolve_industry_context.py`、`tests/agents/derived_facts/test_industry_thresholds.py`

### Phase 1：行业知识工作流基础设施（后置）

1. 新增 `IndustryContextArtifact` 合约 → `alphabee/orchestrator/contracts.py` 或行业知识专用 contracts 模块
2. 新增 `ArtifactType.INDUSTRY_CONTEXT` → `alphabee/core/schemas.py`
3. 新增 `IndustryResearchAgent` / `IndustryContextWorkflow`（离线/准离线，采集 → 归一化 → 基准 → 定性 → 审核 → 持久化）
4. 新增行业知识持久化接口：按 `classification_standard + industry + as_of_date + schema_version` 存取（v1 每行业一个 JSON 快照文件，原子写）
5. 新增 stale / confidence / source_refs / review_status 等元数据，`stale_after` 按基准类别给默认值

### Phase 2：字段治理与数据源适配（可与 Phase 0 并行）

6. 行业名规范字典落地（`industry_code ↔ 显示名`），`reviewer.py` / `engine.py` 三处硬编码常量迁移引用
7. 新增或完善 Tushare / AkShare / 东方财富行业字段 mapping
8. 确保外部字段只存在于 adapter / mapping 层，下游统一使用 canonical field
9. 申万行业指数、同花顺板块摘要、东方财富快照等多来源交叉校验，产出标准化 industry facts

### Phase 3：在线注入层完善

10. `resolve_industry_context` 接读行业知识存储（替代 Phase 0 的直接计算），支持 stale 版本读取
11. 更新 graph topology（已在 Phase 0 插入，此处仅切换数据来源）
12. 完整 `IndustryContextArtifact` 写入 `artifacts`（含 degraded / stale 标记）
13. 数值型 industry benchmark 注入 `fact_values`（与 Phase 0 相同，来源升级）
14. 缺失或过期降级显式化：`industry_context=None` / `stale=True` + `industry_context_missing` / `industry_stale` issue

### Phase 4：引擎行业感知（结构性覆盖，增量）

15. `DerivedFactsEngine.run()` 支持 `industry_thresholds`（默认 None 向后兼容）
16. `SignalEngine.run()` 支持 `industry_trigger_rules`
17. 为 5-8 个最受益的规则补充行业感知（优先：debt_ratio、roe_level、peg_ratio、interest_coverage、asset_turnover），相对基准为主、逐行业覆盖为辅

### Phase 5：冲突、验证与观点合成行业感知

18. `explore_conflicts` payload / prompt 增强（注入行业基准摘要）
19. `verify_hypotheses` shared context / prompt 增强
20. `synthesize_insights` 消费 `IndustryContextArtifact`，把行业主线纳入中心观点

### Phase 6：报告层与持久化边界

21. `ReportGenerationPayload` 新增 `industry` 字段（结构见第五节）
22. 报告 prompt 模板更新（行业对比章节 + stale 提示分支）
23. `review_report` 新增行业感知检查（3 条可执行检查，见第五节）
24. `finalize_message` 行业元数据只进 artifacts / 调试视图，不进主报告（0.6 分层）
25. `task_records/recorder.py` 同步读取最终行业上下文摘要与版本信息

---

## 关键设计决策

### 为什么行业/产业分析要单独成 workflow？

行业知识具有跨个股复用价值，不应绑定到单次个股分析运行。独立 workflow 可以沉淀行业商业模式、产业链、估值中枢、关键驱动和风险，并通过版本、数据日期、source_refs、review_status 保证可追踪。

> 注意（评审新增）：这是**后置增强**而非前置依赖——Phase 0 垂直切片（扩展 `industry_fact` + 注入 + 相对阈值）不依赖该 workflow，先跑通机制再沉淀知识资产，避免重基建压住即时收益。

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

### 行业知识过期怎么办？

`IndustryContextArtifact` 必须带 `as_of_date`、`generated_at` 和可选 `stale_after`。在线 pipeline 读取到过期版本时不应静默当作新数据使用，而应标记 stale，降低置信度，必要时在 report / review 中提示"行业上下文可能过期"。

### 为什么优先"基准相对阈值"而不是"逐行业阈值"？（评审新增 A3）

引擎求值机制已核实：衍生引擎 `threshold_context = {**fact_values, "value": ...}`、信号引擎 `extended_values = fact_values + derived`，两者都能在表达式里引用 `fact_values` 任意字段。因此相对行业均值的判断**零引擎改动**即可生效，且天然覆盖所有行业——逐行业 `industry_thresholds` 每加一个行业就要维护一份 YAML，只适合结构性差异行业（银行/房地产/公用事业）。两条机制并存，相对基准为主、绝对覆盖为辅。

### 行业阈值初始值从哪来？

保守策略：只对已确认有显著行业差异的行业（银行、房地产、非银金融、公用事业）设置特殊阈值，其余行业用通用阈值。**每个阈值必须带 `source_refs`**（数据来源/推导依据），优先由 peer 分位数数据驱动推导后人工 review，避免拍脑袋数值；Golden test 固化「银行 92% → none」等行为防回归。

### 为什么行业匹配键用代码而不是中文名？（评审新增 B1）

`build_company_context` 关键词抽取、`reviewer.py` 的 `_HIGH_LEVERAGE_INDUSTRIES`、`engine.py` 的 `_FINANCIAL_INDUSTRIES` 已是三处中文名硬编码。新机制若再用「银行」「房地产」做 key，会出现第四处且与 artifact 的 `classification_standard` 字段不一致。统一用 `classification_standard + 行业代码` 作 key，显示名经规范字典解析，三处硬编码逐步收敛。

### 与 `DOMAIN_CONTEXT_ROADMAP.md` 的边界？（评审新增 A2）

该文档主张「不要把 domain context 做成静态行业知识库」（primitives/playbooks 动态激活），本方案的 `IndustryContextArtifact` 恰好是静态知识资产，两者方向存在张力。划界方案：**本方案只做数值基准层**（可版本化的估值/财务/成长中位数 + 阈值机制 + 注入通道），定性解释层（商业模式、产业链叙事、关键驱动与风险的分析框架）归 `DOMAIN_CONTEXT_ROADMAP.md` 的 primitives/playbooks。`IndustryContextArtifact` 的定性字段（`business_model_summary / industry_chain / key_drivers / risk_factors`）在 v1 保持空或轻量，避免越界与重复建设。

---

## 测试计划（评审新增 C1）

### 单元级

1. **阈值回退链**：相对表达式引用的行业字段缺失 → 表达式被跳过 → 命中绝对阈值；相对在前、绝对在后时行为正确（银行 92% 命中 `industry_thresholds`，制造业 55% 命中相对/默认阈值）。
2. **引擎向后兼容**：`DerivedFactsEngine.run()` / `SignalEngine.run()` 不传 `industry_context` 时行为与现在完全一致（现有 21 条衍生 + 20 条信号规则回归全绿）。
3. **`lenient` 无关**：本方案不触碰 insights，但 `industry_fact` 扩展后的字段归一化（单位/口径）要有独立单测。

### 节点级（`resolve_industry_context`）

4. 行业命中：公司映射到已知行业 → artifact 正常产出、基准注入 `fact_values`。
5. 行业缺失：映射失败 → `industry_context=None` + `industry_context_missing` issue，管道继续跑默认阈值。
6. 版本过期：`stale_after < now` → artifact `stale=True` + `industry_stale` issue，报告 prompt 收到 stale 标记。
7. 多标准回退：`sw_l1` 未命中 → 按规范字典回退 `ths` → 仍失败 → 降级。

### 端到端（预期收益表逐条转 golden test）

8. 银行负债率 92% + 流动比率正常 → `debt_risk=none`（改造前为 high）。
9. 科技股 PEG=2.5 且行业 PEG 中位数高 → `valuation_risk=low`（改造前为 medium）。
10. 制造业 ROE=6% vs 行业均值 5% → `roe_level=good`（改造前为 weak）。
11. `market_share_change`：注入 `industry_revenue_yoy` 后从 blocked 恢复为可计算。
12. 报告含行业对比 + `industry.stale=True` 时报告写出过期提示（gate 披露检查通过）。

---

## 预期收益

| 维度 | 现状 | 改造后 |
|------|------|--------|
| 银行负债率 92% | signal: debt_risk=high | signal: debt_risk=none（行业正常水平） |
| 科技股 PEG=2.5 | signal: valuation_risk=medium | signal: valuation_risk=low（科技行业正常） |
| 制造业 ROE=6% | derived: roe_level=weak | derived: roe_level=good（行业对比） |
| conflict "高负债 vs 高利润" | 标记为冲突 | 标记为非冲突（金融行业特征） |
| `market_share_change` | 永远 blocked | 可计算（注入 `industry_revenue_yoy`） |
| 报告质量 | 通用分析，缺乏行业定位 | 有行业定位 + 行业基准对比 |
| 知识复用 | 每次分析重复生成行业判断 | 行业知识沉淀为可版本化资产，多只股票共享 |
| 可追踪性 | 行业语境来源不稳定 | 带 `schema_version`、`as_of_date`、`source_refs`、`review_status` |
