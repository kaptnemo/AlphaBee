# Insight 降级设计（ROADMAP 0.4 收尾）

> 目标条目：ROADMAP 0.4「修复 Insight schema 脆弱性」的剩余缺口——**parse fail 时保留降级 insight，而不是整层丢弃**。
> 关联文档：`docs/ROADMAP.md`（0.4 小节、推荐优先级、下一步建议）。
> 状态：✅ **已实施（2026-08）**。实现落点：`alphabee/agents/insights/rescue.py`（Tier 1-3）、`alphabee/orchestrator/nodes/insights.py`（四级决策树）、`alphabee/orchestrator/contracts.py`（降级字段）、`alphabee/orchestrator/prompts.py`（降级分支）；测试 `tests/orchestrator/test_insight_degradation.py`（14 用例）。本文档保留为设计与验收依据。

---

## 1. 背景与问题

### 1.1 现状

`synthesize_insights`（`alphabee/orchestrator/nodes/insights.py`）是观点层唯一入口，产出 `INSIGHT_ANALYSIS` artifact 后被 thesis 与 report 消费：

```text
build_insight_context(state, symbol)          # 结构化上下文（dict）
→ insight_agent_factory()                     # create_deep_agent, tools=[]
→ INSIGHT_AGENT_USER_TEMPLATE.substitute()    # 纯 JSON 输出要求
→ parse_json(raw_text)
→ InsightOutput.model_validate(parsed)        # 严格校验 ← 唯一入口
→ InsightArtifact → artifact
```

### 1.2 失败模式（已逐行核实代码）

| 失败点 | 当前行为 | 后果 |
|---|---|---|
| `build_insight_context` 抛异常 | 记 `context_build_failure`（MEDIUM），**无 artifact** | 观点层整体缺失 |
| agent 调用抛异常 | 记 `subagent_failure`（HIGH），**无 artifact** | 观点层整体缺失 |
| 空响应 | 记 `empty_response`（MEDIUM），**无 artifact** | 观点层整体缺失 |
| **parse fail**（最常见的漂移场景） | 记 `parse_error`（MEDIUM），**无 artifact** | 观点层整体缺失 ← 本次修复重点 |

### 1.3 影响的真实代价

parse fail 是**静默劣化**：报告照常输出、看起来"没问题"，但：

- `nodes/thesis.py:105` 读不到 insight → `_apply_insight`（`agents/thesis/engine.py`）不注入 central_tension / counter_evidence / confidence 调节；
- 报告 prompt（`orchestrator/prompts.py`）走 `insight == null` 分支，三个核心章节退化为"未生成独立投资观点 / 未生成情景分析 / 无可证伪条件"；
- 用户拿到的是"维度打分 + 模板解释"，即 ROADMAP 背景里说的"数字堆砌"。

### 1.4 失败根因（为什么 parse 会失败）

`InsightOutput`（`agents/insights/models.py`）里 `core_view / central_tension / main_driver` 是**必填 str**；`EvidenceItem.weight`、`MaterialityRank.importance`、`CrossSignalPattern.severity_modifier` 是**严格 Literal**。LLM 常见漂移：

- 漏字段（如没写 `main_driver`）；
- 类型漂移（`supporting_evidence` 输出成字符串数组而不是对象数组）；
- 枚举漂移（`weight: "significant"`、`importance: "major"`）；
- 顶层包了一层（如 `{"insight": {...}}`）。

---

## 2. 设计目标与非目标

### 2.1 目标

1. **观点层永不整层丢失**：除"上游数据本身为空"这一极端情况外，任何失败都产出一个可消费的 `INSIGHT_ANALYSIS` artifact。
2. **降级必须显式**：artifact 带 `degraded / fallback_tier / degradation_reason`，下游（thesis 置信度调节、报告 prompt、可观测性）能感知并区别对待。
3. **降级必须诚实**：确定性兜底只**转述**结构化上下文，不**虚构**任何事实、数字、情景。
4. **改动局部化**：只改 `models.py / nodes/insights.py / contracts.py / payload_builders.py / prompts.py`，下游 thesis 引擎**零改动**（它读 dict 的 `.get()` 默认值，天然容忍空字段）。

### 2.2 非目标

- 不做"重试一次 LLM"（成本不可控，且漂移大概率复现）；
- 不做 claim-evidence graph 级别的观点审计（那是 Phase 3）；
- 不承诺降级输出与 LLM 观点质量相当——降级是**保底**，不是替代。

---

## 3. 总体架构：四级降级阶梯

```text
Tier 0  严格解析成功（现有路径）
   │ 失败
Tier 1  宽松救援（rescue）：结构修补后重新校验（无 LLM）
   │ 失败
Tier 2  确定性兜底（fallback）：从 build_insight_context 的 dict 直接合成（无 LLM）
   │ 上下文为空/构建失败
Tier 3  最小骨架：仅 symbol + 空字段 + 降级标记
```

**核心洞察**：`synthesize_insights` 在调用 agent **之前**已经拿到了 `build_insight_context` 返回的完整结构化 dict（signals / derived / anomaly / conflicts 含已验证状态 / snapshot / market / company）。Tier 2 直接复用这份 dict，零额外数据访问成本。

| 层 | 产物 | confidence | degraded | fallback_tier | 触发条件 |
|---|---|---|---|---|---|
| 0 | 完整 insight | 按 LLM 输出 | false | 0 | `model_validate` 成功 |
| 1 | 修补后的 insight | 按 LLM 输出（缺失字段不影响） | true | 1 | 救援后校验成功 |
| 2 | 结构化合成 insight | `"low"` 强制 | true | 2 | 救援失败或原始输出不可解析 |
| 3 | 空骨架 | `"low"` 强制 | true | 3 | 上下文构建失败 / 上下文无任何数据 |

> `confidence = "low"` 是刻意的：下游 `_apply_insight` 会把维度置信度 ×0.85，报告 prompt 的 low-confidence 分支会下调 overall_confidence 一档——**降级自动传导为更保守的输出**。

---

## 4. 数据契约变更

### 4.1 `InsightArtifact`（`orchestrator/contracts.py`，约 109 行）

新增三个字段（带默认值，向后兼容）：

```python
class InsightArtifact(BaseModel):
    # ...现有字段不变...
    degraded: bool = False                    # 是否降级产出
    fallback_tier: int = 0                    # 0=完整 1=宽松救援 2=确定性兜底 3=最小骨架
    degradation_reason: str = ""              # 人类可读原因，如 "parse_failed: <原始错误>"
```

### 4.2 `ReportInsightPayload`（`orchestrator/contracts.py`，约 221 行）

新增：

```python
degraded: bool = False
```

`payload_builders.py` 的 insight 段透传 `degraded=insight_val.degraded`。

### 4.3 新增模块：`alphabee/agents/insights/rescue.py`

```python
def lenient_parse(raw_text: str) -> tuple[InsightOutput, str] | None:
    """Tier 1：宽松解析。返回 (修复后的 InsightOutput, 修复原因) 或 None。"""

def build_fallback_insight(context: dict, symbol: str | None) -> InsightOutput:
    """Tier 2：确定性合成。只转述 context 中的结构化事实，不虚构。"""

def build_minimal_insight(symbol: str | None, reason: str) -> InsightOutput:
    """Tier 3：最小骨架。"""
```

---

## 5. Tier 1：宽松救援（rescue）

### 5.1 流程

```python
parsed = parse_json(raw_text)            # 兼容现有 parse_json
if not isinstance(parsed, dict):
    return None                          # 交 Tier 2
normalized = _normalize_payload(parsed)  # 一次性结构修补
try:
    output = InsightOutput.model_validate(normalized)
    return output, f"lenient_rescue: {修复点摘要}"
except Exception:
    return None                          # 交 Tier 2
```

只做**一次性规范化**，不做多轮 error-driven 修补（v1 保持简单、可单测）。

### 5.2 修补规则（`_normalize_payload`）

| # | 输入异常 | 修补 |
|---|---|---|
| N1 | 顶层包一层（`{"insight": {...}}` 等） | 递归解包，取最内层 dict |
| N2 | 必填文本字段缺失（core_view / central_tension / main_driver） | 置 `""` |
| N3 | 文本字段类型错误（如 dict/数字） | `str()` 强转，失败置 `""` |
| N4 | `supporting_evidence / counter_evidence` 是字符串数组 | 每条 → `EvidenceItem(statement=该字符串, source="insight:raw", weight="moderate")` |
| N5 | evidence 对象缺字段 | `statement` 缺 → `""`；`source` 缺 → `"insight:raw"`；weight 非法 → `"moderate"` |
| N6 | `materiality_rank` 对象缺字段 | `variable` 缺 → `""`；`reasoning` 缺 → `""`；`importance` 非法 → `"medium"` |
| N7 | `cross_signal_patterns` 缺字段 | `pattern_name` 缺 → `""`；`severity_modifier` 非法 → `"unchanged"` |
| N8 | `what_would_change_my_mind` 是 dict 数组 | 按 `condition / evidence / statement / falsification` 键序取字符串，取不到跳过 |
| N9 | 顶层多出未知字段 | 忽略（Pydantic 默认行为，无需处理） |

> 语义约束：Tier 1 只**修结构**，不补内容。缺失的必填字段以 `""` 保留，由下游空值容忍逻辑处理（`_apply_insight` 的 `if central_tension:`、报告 prompt 的占位文案）。

### 5.3 现有 coercer 的配合

`_coerce_confidence / _coerce_importance`（models.py）继续生效；本层只补它们没覆盖的枚举（weight、severity_modifier）与结构问题。

---

## 6. Tier 2：确定性兜底（fallback）

### 6.1 输入

`context`：即 `build_insight_context(state, symbol)` 的返回值（节点里现成）：

```text
company           {industry, sub_industry, market_cap_category, lifecycle_stage}
latest_snapshot   {period, revenue_yoy, net_profit_yoy, gross_margin, roe, ...}
market_valuation  {pe_ttm, pb_ratio, market_cap}
key_signals       [{signal_id, level, interpretation, thesis_impact}]  # 已按 severity 降序、已滤 neutral
key_derived_facts {name: {value, level, interpretation}}
anomaly           {anomaly_count, pattern_count, top_anomalies, pattern_matches}
conflicts         [{theme, severity, description, related_dimensions, hypotheses:[{explanation, status, summary, gaps, predictions}]}]
verified_count / rejected_count
```

> 关键前提：管线顺序 `explore_conflicts → verify_hypotheses → synthesize_insights`，所以 `conflicts[].hypotheses[].status` 已是**结算后**的 `verified / partial / rejected / unknown`（0.5 落地后回写进 artifact）。

### 6.2 字段映射表（全部确定性规则）

| 输出字段 | 规则 |
|---|---|
| `core_view` | 模板合成（见 6.3），只引用计数与主题，不新增判断 |
| `central_tension` | 取第一个含 verified/partial 假设的冲突 `theme`；没有 → 取第一个 `anomaly.top_anomalies[].metric`；没有 → `""` |
| `main_driver` | 取 `key_derived_facts` 中 level 最高的字段名；没有 → 取 top signal 的 `signal_id`；没有 → `""` |
| `supporting_evidence` | top 3 个 level∈{high,medium} 的信号 → `EvidenceItem(statement=interpretation, source=f"signal:{signal_id}", weight=level→strong/moderate)` |
| `counter_evidence` | verified/partial 假设的 `summary`（≤3 条）→ `EvidenceItem(statement=summary, source=f"conflict:{theme}", weight="moderate")`；不足再补 anomaly 的 interpretation |
| `materiality_rank` | 按 `key_derived_facts` level 取前 3-5 → `MaterialityRank(variable=name, importance=high→critical/medium→high, reasoning=interpretation)` |
| `cross_signal_patterns` | 从 `anomaly.pattern_matches` 转述（pattern_name + severity） |
| `business_model_context` | `f"行业: {industry}；细分: {sub_industry}；生命周期: {lifecycle_stage}；市值分类: {market_cap_category}。"`（字段缺则跳过） |
| `base_case` | 转述式摘要（见 6.3） |
| `bull_case / bear_case` | **恒为 `""`**——不虚构情景 |
| `what_would_change_my_mind` | verified/partial 假设的 `predictions`（本身是可证伪陈述）取前 2；不足用 unknown 假设的 `gaps` 补，前缀"待验证: "；上限 4 条 |
| `confidence` | 恒 `"low"` |

### 6.3 core_view / base_case 模板（示例）

```text
# 有风险信号或已验证冲突时
core_view = f"当前分析识别到 {high_count} 个高风险信号、{verified_count} 个已验证冲突，"
            f"核心矛盾聚焦于「{central_tension}」；基于现有结构化证据，投资判断需谨慎。"

# 无风险信号且无已验证冲突时
core_view = "当前分析未检出高风险信号或已验证冲突，基本面未见显著恶化信号。"

# base_case
base_case = f"基于当前数据：核心信号为「{top_signal_interpretation}」（{top_signal_level}）；"
            f"最新快照 {snapshot_period}：营收同比 {revenue_yoy}、净利润同比 {net_profit_yoy}。"
```

### 6.4 诚实性硬规则

- **H1**：输出中每个陈述必须能在 `context` 里找到来源（原样或截断），单测断言语句 ⊆ 上下文内容；
- **H2**：无输入 → 无输出。bull/bear 永不虚构；central_tension 无来源时留空；
- **H3**：`confidence = "low"` 恒成立（Tier 2/3），让下游置信度调节自动生效；
- **H4**：不引用 thesis 层数据（synthesize_insights 先于 run_thesis 执行，根本拿不到）。

---

## 7. Tier 3：最小骨架

```python
def build_minimal_insight(symbol, reason):
    return InsightOutput(
        core_view="", central_tension="", main_driver="",
        business_model_context="",
        confidence="low",
    )
```

触发场景：`build_insight_context` 抛异常、或 context 中 signals/derived/anomaly/conflicts 全部为空。此时仍产出 artifact（degraded=true, tier=3），保证下游统一走"有 insight 对象"的路径；报告 prompt 对 tier≥2 的降级分支输出占位文案。

> 决策说明：**总是产出 artifact**（而不是继续"失败即 None"），把 `insight == null` 收敛为理论态，报告 prompt 只按 `degraded` 分支处理，避免两套空态逻辑。

---

## 8. 节点改造（`synthesize_insights`）

```python
# 失败处理改为四级决策树：

# A. 上下文构建失败 → Tier 3
except Exception as exc:
    issue(context_build_failure, MEDIUM)           # 保留
    artifact = build_minimal_insight(symbol, f"context_build_failure: {exc}")
    → 产出 degraded artifact + insight_degraded issue

# B. agent 异常 / 空响应 → Tier 3
except Exception as exc:
    issue(subagent_failure, HIGH)                  # 保留
    artifact = build_minimal_insight(symbol, f"agent_failure: {exc}")
    → 产出 degraded artifact + insight_degraded issue
if not raw_text:
    issue(empty_response, MEDIUM)                  # 保留
    artifact = build_minimal_insight(symbol, "empty_response")
    → 产出 degraded artifact + insight_degraded issue

# C. 解析
try:
    output, reason = lenient_parse(raw_text)       # Tier 0 + Tier 1
    tier = 0 if not reason else 1
    if tier == 1: issue(insight_degraded, MEDIUM, reason)
except ...:
    output = None
if output is None:
    output = build_fallback_insight(context, symbol)   # Tier 2（context 已在作用域）
    if output 内容为空:                                # 上下文本身无数据
        output = build_minimal_insight(symbol, "empty_context")
        tier = 3
    else:
        tier = 2
    issue(insight_degraded, MEDIUM, f"deterministic_fallback tier={tier}")

# 统一产出 artifact（degraded / fallback_tier / degradation_reason 随 artifact 落库）
```

**Issue 策略**：只有降级才发 `insight_degraded`（MEDIUM）；成功不发。`parse_error` 只在 Tier 1/2/3 全部失败（理论上不存在，兜底保险）时保留。

---

## 9. 报告 prompt 适配（`orchestrator/prompts.py`）

改动 4 处，原则：**降级 → 允许结构化摘要、禁止虚构、放宽"必须有实质内容"**：

| 位置 | 现状 | 改为 |
|---|---|---|
| 第 49 行 总纲 | 按 `insight == null` 二分 | 三分：`insight 完整` → 观点主线；`insight.degraded=true` → 结构化摘要模式（转述 supporting/counter evidence、base_case，禁止虚构新事实）；`null` → 纯数据呈现 |
| 第 56 行 investment_viewpoint | null → 占位 | degraded → "观点层降级，以下为基于结构化证据的摘要" + 转述 evidence 列表 |
| 第 57 行 scenario_analysis | null → 占位 | degraded → 只有 base_case 非空才写；bull/bear 缺失时写"情景分析暂缺（观点层降级）" |
| 第 64 行 falsification_conditions | null → 占位 | degraded → 有 `what_would_change_my_mind` 则逐条转述，否则写"观点层降级，暂无明确证伪条件" |
| 第 124 行 硬约束 | "insight 不为 null 则三章节必须有实质内容" | 增加限定："**且 insight.degraded=false** 时三章节必须有实质内容；degraded 时允许简短、诚实的结构化表述" |

第 84-105 行的置信度闸门无需改：Tier 2/3 的 `confidence="low"` 自动命中低置信度下调分支。

**Thesis 引擎（`_apply_insight`）零改动**：它读 dict `.get()` 默认值；Tier 2 的 central_tension / counter_evidence / confidence 都是正常形状，Tier 3 的空字段自动 no-op。

---

## 10. 可观测性

1. **Issue**：降级时发 `insight_degraded`（MEDIUM，message 含 tier + reason）。
2. **artifact 字段**：`degraded / fallback_tier / degradation_reason` 随 artifact 落库，最终 JSON 可审计。
3. **指标**（建议加到 `task_records` 或后续仪表盘）：按 run 统计 `tier≥1 的比例 / tier 分布`。若 tier≥2 占比持续升高，说明 prompt 或 schema 与 LLM 输出漂移加剧，应回到 prompt 校准。

---

## 11. 测试计划

新增 `tests/orchestrator/test_insight_degradation.py`（复用 `tests/orchestrator/test_conflict_lifecycle.py` 的 monkeypatch 模式）：

| # | 用例 | 断言 |
|---|---|---|
| 1 | 合法 JSON | artifact 完整、`degraded=false`、无 issue |
| 2 | 缺 `main_driver` + evidence 为字符串数组 + `weight:"significant"` | Tier 1：核心字段齐、`degraded=true`、`fallback_tier=1`、无虚构内容 |
| 3 | 顶层包 `{"insight": {...}}` | Tier 1 解包成功 |
| 4 | 非 dict 输出（垃圾文本） | Tier 2：`fallback_tier=2`、`confidence="low"`、core_view 非空且来自 context |
| 5 | context 含 2 个 high signal + 1 个 verified conflict | Tier 2 的 central_tension == 该冲突 theme；what_would_change_my_mind 含 predictions |
| 6 | context 全空 | Tier 3 最小骨架、degraded=true |
| 7 | 节点级：monkeypatch agent 返回不可解析文本 | 节点**产出 artifact**（而非空）+ `insight_degraded` issue；`build_report_generation_payload` 可消费（ReportInsightPayload.degraded=true） |
| 8 | `build_fallback_insight` 诚实性 | 所有 statement 字符串均能在 context 中找到来源（H1 硬规则） |
| 9 | 回归 | 现有 `tests/orchestrator/`、`tests/agents/insights/test_models.py` 全绿 |

---

## 12. 兼容性与风险

| 风险 | 缓解 |
|---|---|
| Tier 2 模板读起来机械/误导 | H1-H4 硬规则 + 单测断言语句有来源；模板措辞评审时过一遍真实样例 |
| 报告 prompt 降级分支被 LLM 利用来偷懒（永远写"观点层降级"） | 降级分支仅在 `degraded=true` 时生效；非降级 run 无此路径 |
| 旧 artifact（无 degraded 字段）被新代码读取 | `degraded: bool = False` 默认值向后兼容；旧报告 payload 同理 |
| Tier 1 修补引入幻觉（把结构错误误当内容修复） | Tier 1 只修结构不补内容（5.2 规则）；缺失文本一律 `""` |
| `confidence="low"` 导致全链路过度悲观 | 这是有意的保守策略；且只影响降级 run |

---

## 13. 实施步骤与工作量

| 步骤 | 内容 | 工作量 |
|---|---|---|
| 1 | `agents/insights/rescue.py`：`lenient_parse` + `_normalize_payload` | 0.5 天 |
| 2 | `agents/insights/rescue.py`：`build_fallback_insight` + `build_minimal_insight` | 0.5 天 |
| 3 | `contracts.py`：InsightArtifact / ReportInsightPayload 加字段 | 0.1 天 |
| 4 | `nodes/insights.py`：四级决策树改造 | 0.3 天 |
| 5 | `payload_builders.py` 透传 `degraded`；`prompts.py` 5 处降级分支 | 0.3 天 |
| 6 | 测试（9 用例）+ 回归 | 0.5 天 |
| 7 | ROADMAP.md 0.4 / 推荐优先级 / 下一步建议 状态更新 | 0.1 天 |

合计约 **2 个工作日**。优先级：步骤 1+2+4（Tier 1/2 主链路）先行，3+5 必须同批落地（否则降级标记无法消费），6 不通过不合并。

---

## 14. 验收标准

1. 任一失败模式（agent 异常 / 空响应 / parse fail / 上下文失败）下，`INSIGHT_ANALYSIS` artifact **必然存在**；
2. 降级 artifact 必带 `degraded=true` 与合理的 `fallback_tier`，且 `confidence` 恒为 `"low"`（Tier≥2）；
3. 报告对降级 run 输出诚实的结构化摘要，**不出现虚构情景或数字**；
4. `tests/orchestrator/test_insight_degradation.py` 9 用例全绿，现有测试无回归；
5. ROADMAP 0.4 状态更新为 ✅。
