# AlphaBee 偏离控制框架设计（Deviation Control Framework）

> **状态：📋 设计提案（未实施）**。本文档是把"偏离论"（Deviation Theory）落地为 AlphaBee 整体架构的纲领性设计：把 AlphaBee 的核心工程问题从"Planning + Tool Calling 管道"重新定义为"偏离控制系统"。
> 关联文档：`docs/roadmap/ROADMAP.md`（业务路线图）、`docs/roadmap/ENGINEERING_ROADMAP.md`（工程路线图）、`docs/design/INSIGHT_DEGRADATION_DESIGN.md`（降级阶梯先例）、`docs/design/data_collection_failed_auto_fix_design.md`（失败库先例）、`docs/roadmap/COMPANY_TRACK_ROADMAP.md` / `docs/midterm/`（长期跟踪层）。
> **姊妹篇**：`docs/design/RESEARCH_CONTINUUM_DESIGN.md`（Temporal Long-Horizon：五层架构映射、Persistent Research Object、State Reconciliation 对账协议、事件驱动调度）。宏观环（§9）的完整设计在该文档，本文档只保留偏离控制视角。
> 实现任何本框架的契约改动时，必须走 `alphabee-pipeline-contract-steward` / `alphabee-schema-steward` skill 流程（Artifact / Issue / OrchestratorState 的字段变更会影响所有下游消费者）。

---

## 1. 背景：为什么 AlphaBee 的困难是"偏离"而不是"单步"

偏离论的核心命题：

> Agent 失败主要不来自"某一步不会做"，而来自执行过程中不断产生偏离（deviation），且 Agent 能否及时发现并纠正这些偏离。

$$
\delta_t = D(s_t, S_t^*)
\quad\text{（当前状态距"仍能顺利完成任务的理想轨迹"的距离）}
$$

经典失败模式不是"第 17 步突然不会"，而是"第 4 步偏一点 → 第 8 步偏更多 → 第 15 步无法恢复"。任务难度由此拆成 8 个维度：

| 维度 | 符号 | 含义 |
|---|---|---|
| Horizon | H | 任务长度（步数），错误以 $p^H$ 复利 |
| Branching | B | 每步"看起来合理"的选择数，决定搜索空间 |
| Observability | O | 发生偏离后能被检测到的概率 |
| Recoverability | R | 检测到偏离后能回到可恢复轨迹的概率 |
| Amplification | A | 已有偏离被下游放大的系数 $\delta_{t+1}=\alpha_t\delta_t+\epsilon_t$ |
| State Complexity | S | 内部世界模型 $\hat W_t$ 与真实 $W_t$ 的漂移空间 |
| Uncertainty | U | 环境/数据的不确定性 |
| Deviation Cost | C | 偏离的代价（不可逆任务 C→∞） |

AlphaBee 是一个**长 horizon、中高 branching、中观测、部分可恢复、存在真实放大通道**的研究 Agent，天然是偏离控制问题：

- **H 高**：单次 run 16 个节点，且宏观环（公司跟踪）是无上限的季度级时间轴；
- **B 高**：`explore_conflicts` / `verify_hypotheses` 是开放式 DeepAgents 节点，调查方向近乎任意；
- **A 高（且未受控）**：`insight → thesis → report` 是一条加权传导链，早期错误会被放大而非衰减；
- **O 不均**：report 层有强 gate，中间节点基本只有"成功/抛异常"两个信号；
- **R 局部**：insight 有四级降级、report 有受控回环，但其他节点是"坏了就带伤前行"。

### 1.1 现状盘点：AlphaBee 已经有一批零散的偏离控制机制

| 已有机制 | 代码位置 | 对应维度 |
|---|---|---|
| Issue 生命周期（severity/status/scope/owner_node/resolution_evidence） | `alphabee/core/schemas.py` `Issue` | O（检测记录） |
| Report 质量门：9 项确定性指标 + 可选 LLM 评估 + 受控重写回环 | `alphabee/orchestrator/gates.py` | O + R |
| ThesisReviewer（L1 确定性 + L2 LLM 双审计） | `alphabee/agents/thesis/reviewer.py` | O |
| Insight 四级降级阶梯（严格解析 → 宽松救援 → 确定性兜底 → 最小骨架） | `alphabee/agents/insights/rescue.py` | R |
| 数据采集失败库（事件 → 指纹去重 → 问题单 → 修复任务 → 换源策略） | `alphabee/data_fetch/`（`database.py`/`fingerprint.py`/`recorder.py`/`scanner.py`/`fix_executor.py`/`strategies.py`） | O + R（数据层） |
| 信号数据缺口接入失败库 | `alphabee/orchestrator/services/gap_recorder.py` | O |
| 冲突生命周期分层（provisional / verified / partial / rejected / unknown 显式回写） | `alphabee/orchestrator/nodes/conflicts.py` + `verification.py` | O（防"怀疑冒充事实"） |
| 降级标记传导（`degraded`/`fallback_tier`/`degradation_reason`） | `alphabee/orchestrator/contracts.py` `InsightArtifact` | O + R |
| 软状态 + 信念漂移度量（`StateBelief.entropy`/`drift`、`StateShift.tv_distance`/`entropy_delta`） | `alphabee/midterm/models.py` | A（宏观防漂移） |
| Thesis 版本 append-only（买入理由不可被行情重写） | `alphabee/midterm/models.py` `ThesisVersion` | A（宏观防重写） |
| 五层差分 + 归因链（evidence → factor → decision） | `alphabee/midterm/models.py` `CompanyStateDiff`/`ChangeAttribution` | O（宏观） |
| 贝叶斯证据更新（log-odds） | `alphabee/midterm/bayes.py` | U |

**核心判断**：机制不少，但它们是**各自为政的孤岛**——没有统一的偏离分类法、没有跨节点的账本、没有把"检测 → 恢复 → 放大控制"串成一个协议。本框架的价值在于把这些孤岛统一成一个有明确语义和预算约束的控制系统。

---

## 2. 双层执行模型

AlphaBee 在两个时间尺度上执行，偏离控制必须分层定义：

```text
微观环（单次 run）：16 节点 LangGraph 流水线
  collect_raw_facts → resolve_industry_context → resolve_company_track
  → resolve_driver_profile → run_analysis_engines → explore_conflicts
  → verify_hypotheses → synthesize_insights → run_thesis → review_thesis
  → [midterm 可选] → generate_report → review_report → record_deviations → finalize_message
  （alphabee/orchestrator/agent.py 实装配线）

宏观环（跨 run 跟踪）：S0–S5 认知状态机 + 快照 + 差分 + 贝叶斯更新
  每次重要事件（财报/公告/行情异动）→ 新 Snapshot
  → CompanyStateDiff（五层差分 + 归因）
  → evidence_log 贝叶斯更新 → thesis 复核 → 状态迁移
  （alphabee/midterm/ 已建模，缺运行调度）
```

| | 微观环偏离 | 宏观环偏离 |
|---|---|---|
| 定义 | 单次执行中，artifact 集合距"完整、可消费、有证据的理想轨迹"的距离 | 跨时间，信念/结论距"thesis 前提仍然成立"的距离 |
| 代理指标 | 节点的 Issue 数、degraded_tier、gate 指标（`EvaluateMetrics`） | `StateShift.tv_distance`、`entropy_delta`、$\Delta P(H|E)$、`exit_conditions_met` |
| 危险方向 | 静默劣化（无 issue 但 artifact 空洞，下游一路带伤） | thesis 漂移（行情/叙事重写原始买入理由）、忘记证伪条件 |

---

## 3. 八维现状矩阵：每维做什么

> 每行给出现状（代码已核实）、缺口、本框架的设计动作。动作编号对应第 4–11 节。

### 3.1 H（Horizon）

- **现状**：流水线步数固定（16 节点），回环已有限额（`supplement_round/max_supplement_rounds`、`report_review_round/max_report_review_rounds`，见 `alphabee/orchestrator/state.py`）。真正无上限的 horizon 在 LLM 节点内部的 tool-calling 循环与宏观跟踪时长。
- **缺口**：`explore_conflicts` / `verify_hypotheses` 无内部步数/工具调用预算（ROADMAP 1.5 的"验证预算"未落地）；宏观环没有声明的跟踪 horizon。
- **动作**：§10 预算统一；§9 宏观环声明跟踪协议（几个快照、何时结束/退出）。

### 3.2 B（Branching）

- **现状**：确定性节点（derived_facts / signal / anomaly）branching≈1（规则驱动）；LLM 节点高，其中 `verify_hypotheses` 自主决定查什么（最大 branching 点）。
- **缺口**：无 triage（严重度 × 可验证性 × 对最终判断影响度决定预算分配）、无"最短排除路径"策略（ROADMAP 1.5 已设计未落地）。
- **动作**：§10 预算机制与 ROADMAP 1.5 合并实施；验证节点纳入节点契约（§6）。

### 3.3 O（Observability）—— AlphaBee 最强的维度

- **现状**：见 §1.1 盘点。检测器集中在 report/thesis 尾部。
- **缺口（关键）**：**检测时延**。目前唯一系统性 gate 在 `review_report`（倒数第 2 个节点）。若 `run_analysis_engines` 产出空 derived facts、或 `synthesize_insights` 产出降级 insight，要到 report gate 才发现——检测时延约 6–8 步，且届时恢复手段只剩"重写报告文案"，无法回上游修复。这就是偏离论的"第 15 步才发现第 4 步偏了"。
- **动作**：§6 每节点后置条件检测器（把检测时延从 O(流水线) 降到 O(1 节点)）；§5 偏离账本记录 `detected_at_step`。

### 3.4 R（Recoverability）

- **现状**：insight 四级降级、report 受控回环（唯一回环点，`route_after_report_review`）、确定性报告兜底、数据层换源修复。
- **缺口**：恢复手段是**节点私有**的，无统一协议；除 report 外无回环授权（facts 采集失败不会重试，直接带 gap 前行）；无"该 degrade 还是该 escalate"的统一决策。
- **动作**：§7 恢复阶梯协议（Tier 0–5）统一所有节点。

### 3.5 A（Amplification）—— AlphaBee 最弱、最危险的维度

- **现状**：**完全没有显式建模**，但代码里存在多条真实放大通道：
  1. `nodes/analyze.py` 把 AnomalyEngine 输出投影回 `fact_values` → 触发 signal 规则 → 进入 thesis（ROADMAP 0.1 设计如此，放大是特性也是风险）；
  2. `agents/thesis/engine.py` `_apply_insight`：insight 的 confidence 直接乘进维度置信度（`low` 时 ×0.85）——insight 错则 thesis 全维度错；
  3. 已验证冲突对维度的扣分（`_apply_conflict_analysis`）；
  4. insight 的 `core_view/central_tension` 主导报告主线（`orchestrator/prompts.py`）。
  这些都是 α>1 的边，且**没有一条边要求下游 review 审计加权方向是否正确**。
- **动作**：§8 放大标注（VERBATIM / WEIGHTED / OVERRIDE），对 WEIGHTED 边强制审计。

### 3.6 S（State Complexity）

- **现状**：单 run 状态 = artifacts/observations/decisions/issues 列表 + `fact_values`；上下文保护只有 `check_message_limit` 硬截断（无优先级淘汰）。宏观层已有 `FactorSnapshot`、`ThesisVersion` append-only（反漂移）。
- **缺口**：单 run 内无"假设登记簿"（assumption registry）——假设未被显式登记，就无法在后续节点检查"该假设是否已被证伪"；截断是盲截，可能先丢关键约束再丢废话；宏观层缺"每帧自动检查 falsification/exit conditions"的调度。
- **动作**：§6 节点契约引入 assumption 登记；§9 宏观环自动检查 `exit_conditions_met`。

### 3.7 U（Uncertainty）

- **现状**：confidence 字段分散（`Decision.confidence`、insight `confidence`、`thesis_confidence` 后验、verification `confidence`），freshness 分类（`ObservationFreshness`）。unknown 假设有状态但无"为什么没查"。
- **缺口**：无统一不确定性账本；ROADMAP 1.5 的"未探索区域记录"未落地——**不知道没查什么**是研究 Agent 最大的隐性偏离。
- **动作**：§5 账本纳入 unknown/未探索区域；§11 度量层统计。

### 3.8 C（Deviation Cost）

- **现状**：`IssueSeverity` 是静态标签，与"下游影响面 × 不可逆性"不挂钩。当前 AlphaBee 只读分析（不做交易），C 有界。
- **缺口**：一旦宏观环接入仓位（`alphabee/midterm/position.py` 已建模），C 骤增——**行动类输出必须过 gate，且 gate 语义从"质量检查"变为"放行确认"**。
- **动作**：§10 预算与升级策略按 C 分层；§9 宏观环对"状态迁移/仓位建议"设置独立确认门。

---

## 4. 偏离分类法：D1–D5

把现有散落的 `Issue.category` 归一化为 5 类偏离。**分类法只做归一化映射，不改动现有 issue 语义**（v1 加字段，v2 再逐步迁移 category 命名）。

| 类 | 名称 | 现有 category 示例 | 典型场景 |
|---|---|---|---|
| D1 | 数据偏离 | `missing_data`、`blocked`、`stale`、`numeric_inconsistency`、`cross_source_conflict`、`company_track_missing`、`company_track_stale`、`peer_group_missing`、`peer_group_benchmarks_missing`、`industry_context_missing`、`industry_benchmarks_missing` | 采集缺字段、数据陈旧、跨源数字打架 |
| D2 | 结构偏离 | `parse_error`、`schema` 缺失、降级（`degraded`）、`insight_degraded`、`driver_profile_degraded`、`context_build_failure`、`empty_response` | LLM JSON 漂移、字段枚举漂移、降级产出、上下文构建失败 |
| D3 | 论证偏离 | `thesis_gap`、`thesis_conflict`、`evidence_chain_incomplete`、`unverified_claim`、`thesis_warning`、`verified_conflict`、`conflict`、`verification_needed` | 无证据结论、论点与已验证冲突矛盾、证据链断裂 |
| D4 | 状态偏离 | （暂无，需新增）`assumption_invalidated`、`context_truncated`、`state_drift`、`market_regime` | 忘记约束、假设被证伪却未感知、环境状态迁移、重复调查 |
| D5 | 控制偏离 | `report_rewrite_needed`、`subagent_failure`、`midterm_decision_failed`、`midterm_decision_report_failed`、`failure`、预算超限（需新增 `budget_exhausted`） | 回环次数打满、验证预算耗尽、子代理/决策节点调用失败 |

> **F0 实测覆盖（代码为准）**：现网 `alphabee/` 里 22 个 `Issue(category=...)` 生产点已全部显式登记
> （清单见 §14.1-B 的 `CLASS_BY_CATEGORY`）；覆盖守卫测试会扫描源码，出现未登记 category 即失败
> （`tests/orchestrator/test_deviation_service.py::test_every_produced_issue_category_is_registered`）。
> 未登记（历史/外部来源）的 category 仍按 §14.1-B 保守回落 D2。

**扩展 Issue 契约（`alphabee/core/schemas.py`）**，全部带默认值向后兼容：

```python
class DeviationClass(enum.StrEnum):   # 新增
    D1_DATA = "d1_data"
    D2_STRUCTURE = "d2_structure"
    D3_ARGUMENT = "d3_argument"
    D4_STATE = "d4_state"
    D5_CONTROL = "d5_control"

# Issue 新增字段：
deviation_class: DeviationClass | None   # 归一化分类（旧 issue 为 None，按 category 惰性映射）
detected_at_step: str | None             # 哪个节点的后置检测器发现（≠ related_step 产生地）
recovery_action: str | None              # 采取的恢复动作（如 "rerun_round=1" / "degraded_tier=2" / "escalated"）
recovery_cost: int | None                # 恢复代价（重跑轮数 / 降级层数 / 0=未恢复）
amplified_by: list[str] | None           # 被哪些下游边放大（§8 标注）
```

> 契约变更必须走 `alphabee-pipeline-contract-steward`：`Issue` 被 `gates.py`、CLI、recorder 消费，新增字段只允许 append，不允许改既有字段语义。

---

## 5. 偏离账本（Deviation Ledger）

**目标**：让每次偏离有身份（指纹）、有来源（检测器）、有去向（恢复动作/放大路径）、有结局（recovered / unresolved）。这是 §11 度量层与跨 run 学习的唯一数据源。

### 5.1 单 run 内

- 复用 `OrchestratorState.issues`，不新开列表——账本视图由 §4 新增字段承载；
- 每个节点在**后置条件检测**（§6）触发时，用统一 helper 上报：
  ```python
  # alphabee/orchestrator/services/deviation.py（新增）
  def record_deviation(
      class_: DeviationClass,
      severity: IssueSeverity,
      message: str,
      *, related_artifact: str | None = None,
      detected_at_step: str,
      recovery_action: str | None = None,
  ) -> Issue: ...
  ```

### 5.2 跨 run 持久化

- 复用 `alphabee/data_fetch/database.py` 的 SQLite 模式（`DataFetchEvent` 已有事件表），新增一张 `deviation_events` 表；
- **指纹去重**复用 `alphabee/data_fetch/fingerprint.py` 的思路：`(deviation_class, category, 归一化 message, symbol, 节点)` 签名，避免同一偏离在每个 run 重复计数；
- 表结构（对齐失败库风格）：

```text
deviation_events
  id, run_id, symbol, step_id, deviation_class, category,
  severity, fingerprint, message, recovery_action, recovery_cost,
  resolved(bool), created_at
```

### 5.3 账本驱动两个闭环

1. **per-symbol 偏离画像**：哪家公司/行业最容易触发哪类偏离 → 反哺 `resolve_industry_context` / `resolve_company_track` 的降级与提示（例如"该行业账期天然重，应收类 D1 偏离按口径解释而非报错"）。
2. **未探索区域记录**（ROADMAP 1.5 第 7 条）：D4 类记录"未验证但重要的方向 + 为什么没查"，写入账本供人机接力。

---

## 6. 节点契约与后置条件检测器（检测时延 O(流水线) → O(1)）

**核心动作：每个节点声明一份契约，检测器在节点出口立即执行。** 这是 O 维度最大的杠杆。

### 6.1 契约模板

```python
# alphabee/orchestrator/contracts.py 或新增 node_contracts.py
class NodeContract(BaseModel):
    node_id: str
    preconditions: list[str]      # 上游 artifact 必须存在/非空
    postconditions: list[str]     # 本节点产物必须满足的可检查断言
    detectors: list[str]          # 出口检测器名（确定性优先，LLM 可选）
    recovery_ladder: list[int]    # 本节点允许的恢复阶梯（§7），如 [1,2,3]
    amplification_labels: dict[str, str]  # 本节点消费的边 → VERBATIM/WEIGHTED/OVERRIDE（§8）
    max_retries: int              # 回环预算（0=禁止回环，只能 degrade/escalate）
```

### 6.2 首批检测器（按性价比排序）

| 检测器 | 节点 | 断言 | 偏离类 |
|---|---|---|---|
| `derived_facts_nonempty` | `run_analysis_engines` | `DerivedFactsArtifact.results` 非空；若空则查 fact_values 是否同样空（区分"真无数据"与"引擎故障"） | D1 |
| `artifact_schema_valid` | 所有产 artifact 节点 | `find_artifact_model(...)` 可校验；已有 coerce_* helper 直接复用 | D2 |
| `insight_artifacts_present` | `synthesize_insights` | 已由四级降级保证（现状 ✅，纳入统一协议即可） | D2 |
| `evidence_refs_present` | `run_thesis` / `review_thesis` | 维度 verdict 对应 Decision 必须带 `based_on/evidence_refs`（ROADMAP P0 项，检测器化） | D3 |
| `downstream_inputs_present` | `generate_report` | payload 里 thesis/insight/anomaly/conflict 段非空；空则显式降级分支而非静默 | D1/D2 |
| `assumption_still_valid` | `synthesize_insights` / `run_thesis` | 对照 §6.3 假设登记簿，检查本 run 内是否已有 D4 记录证伪了某假设 | D4 |

### 6.3 假设登记簿（Assumption Registry）——新增最小结构

研究 Agent 的 D4 偏离根因是"假设没被显式登记，就无从检查它何时失效"。最小落地：不引入新全局结构，而是在 `fact_values` 所在节点之上加一个轻量 artifact：

```python
class AssumptionEntry(BaseModel):       # alphabee/orchestrator/contracts.py
    id: str
    statement: str                      # 如 "应收增长源于军工结算周期而非恶化"
    status: str = "active"              # active | invalidated | confirmed
    source_artifact: str = ""           # 从哪个 artifact 提出
    invalidated_by: str = ""            # 被哪个 evidence/issue 证伪
```

- `explore_conflicts` / `verify_hypotheses` 产出假设时登记（active）；
- 后续节点（尤其 `synthesize_insights` / `run_thesis`）消费前检查：`invalidated` 假设不得再作为论证前提，除非显式引用反驳证据；
- 报告 gate（`gates.py`）增加检查：报告结论若依赖 `invalidated` 假设 → D3 偏离（`assumption_based_claim`）。

> 这是 Phase 3 claim-evidence graph 的轻量前置：先管住"假设生命周期"，再谈"证据图谱"。

---

## 7. 恢复阶梯协议（Recovery Ladder Protocol）

**核心动作：把 insight 的四级降级推广为全链路统一协议。** 每个节点在契约里声明自己允许的阶梯范围，越界即 escalate。

| Tier | 名称 | 语义 | 现状对应 |
|---|---|---|---|
| 0 | 完整通过 | 正常产物 | 各节点主路径 |
| 1 | 局部修复 | 确定性结构修补，无 LLM、零成本 | insight `lenient_parse`（rescue.py） |
| 2 | 降级产出 | `degraded=true` + 确定性兜底 + confidence 强制保守 | insight `build_fallback_insight`、report `build_deterministic_report` |
| 3 | 骨架/跳过 | 显式声明缺失，产最小 artifact，让下游按 gap 决策 | insight `build_minimal_insight` |
| 4 | 受控回环 | 有限次重跑上游（**唯一允许的图回环**，有预算） | report rewrite（`route_after_report_review`） |
| 5 | 升级（escalate） | 终止 run 或在最终产物显式暴露"系统无法完成"，交人 | 暂无（需新增） |

### 7.1 各节点阶梯配置（v1 建议值）

| 节点 | 允许 Tier | 回环预算 | 说明 |
|---|---|---|---|
| `collect_raw_facts` | 0,4,2 | 1 次补充采集 | 数据缺失优先补采一次，仍缺则降级带 gap |
| `run_analysis_engines` | 0,2,3 | 0 | 确定性引擎无重试价值，缺数据降级 |
| `explore_conflicts` | 0,2,3 | 0 | 探索失败不阻断，降级为"无冲突记录" |
| `verify_hypotheses` | 0,1,2 | 0 | 验证失败保持 unknown（现状语义正确，纳入协议） |
| `synthesize_insights` | 0,1,2,3 | 0 | 现状四级已实现 ✅，仅需协议化声明 |
| `run_thesis` | 0,1,2 | 0 | 引擎确定性为主，LLM 增强失败退引擎结果 |
| `generate_report` | 0,2,4 | ≤2（现状 `max_report_review_rounds`） | 现状 ✅，协议化 |
| 宏观环行动建议 | 0,5 | 0 | 仓位/状态迁移类输出**只允许 0 或 5**（§9.4） |

### 7.2 降级传导规则（协议硬约束）

1. **降级必须显式**：任何 Tier≥2 的产物必须带 `degraded=true` + `degradation_reason`（沿用 insight 先例，扩展到所有 typed contract）；
2. **降级必须传导**：下游消费 `degraded` artifact 时，自身 confidence 必须保守化（insight `low` → thesis ×0.85 是正确先例，泛化为规则：消费降级输入的结论不得输出 `high` confidence）；
3. **降级必须诚实**：确定性兜底只转述上游结构化内容，不虚构（继承 `INSIGHT_DEGRADATION_DESIGN.md` 的 H1–H4 硬规则）；
4. **Tier 4 唯一性**：图回环只允许出现在契约声明 `max_retries>0` 的节点，且回环原因必须写进偏离账本（D5）。

---

## 8. 放大标注（Amplification Labeling）—— A 维度的核心动作

**核心动作：给每条"上游 artifact → 下游消费"的边标注传输模式，对 α>1 的边强制审计。**

```text
VERBATIM  转述（α=1）：下游只搬运，不改判断方向。
          例：generate_report 转述 thesis/anomaly/conflict 内容。
WEIGHTED  加权（α>1）：下游用上游的量去乘/扣/投，改变判断强度。
          例：_apply_insight 的 confidence 乘法；已验证冲突的维度扣分；
              anomaly 投影回 fact_values 再触发 signal。
OVERRIDE  裁决（方向可变）：下游可以推翻上游。
          例：review_thesis 对 run_thesis；report gate 对 report。
```

### 8.1 现网高风险 WEIGHTED 边（需审计）

| 边 | 放大机制 | 风险 | 审计要求 |
|---|---|---|---|
| `insight → thesis` | confidence ×0.85 等乘法 | insight 错 → 全维度错 | `review_thesis` 必须检查"加权方向是否与信号/冲突证据一致"（现状是盲传） |
| `anomaly → fact_values → signal` | 异常投影触发规则 | 伪异常（口径问题）被放大成信号 | 投影时记录 `source=anomaly_engine`，review 抽查伪异常率 |
| `verified conflict → dimension 扣分` | 冲突扣分 | 结算错误 → 维度误判 | 结算状态（verified/partial/rejected）已是先决条件 ✅，补"扣分幅度 vs 冲突严重度"一致性检查 |
| `insight → report 主线` | core_view 主导全文 | 观点漂移主导表达 | report gate 已有 `cross_source_consistency`，补"主线 vs thesis 判断方向一致性"检查 |

### 8.2 规则

1. WEIGHTED 边的**权重值必须显式**（如 ×0.85、-1 档），不得埋在 prompt 里；
2. WEIGHTED 边的下游必须有一个 review 节点检查方向一致性，检查结果写 Decision（`maker="amplification_audit"`）；
3. 权重调整属于行为变更，必须在 `docs/roadmap/ROADMAP.md` 登记（沿用现有习惯）。

---

## 9. 宏观环：thesis 生命周期的偏离控制（长时间跟踪）

> 宏观环的模型资产已经非常完整（`alphabee/midterm/`），本框架只补两件事：**偏离的显式定义**与**自动调度闭环**。

### 9.1 宏观偏离的显式定义

宏观环的 $\delta$ 不是"当前跑偏了多少"，而是"**信念距 thesis 前提仍然成立**的距离"，三个可计算量：

1. **信念漂移量**：`StateShift.tv_distance`（0.5·Σ|ΔP|）与 `entropy_delta`（变模糊为正）——已有模型，接监控；
2. **置信度漂移**：`ConfidenceDelta` 的 posterior − prior 与 log_odds_delta——已有；
3. **thesis 完整性**：`exit_conditions_met`（证伪条件是否已触发）——已有字段，缺自动检查。

### 9.2 偏离的来源（跟踪场景特有）

| 来源 | 对应机制 | 现状 |
|---|---|---|
| 行情/叙事重写买入理由 | `ThesisVersion` append-only | ✅ 已建模 |
| 忘记证伪条件 | `what_would_change_my_mind`、`next_evidence_to_watch`、`exit_conditions` | 字段已有，**无每帧自动检查** |
| 数据陈旧后继续持有旧结论 | `CompanyStateArtifact.stale_after` | 字段已有，**无"到期自动触发重新采集"** |
| 证据选择性吸收（只信支持证据） | `bayes.py` log-odds 更新 | ⚠️ 需补"反证强制入账"规则：每帧 diff 必须同时入账支持与反对证据（`ChangeAttribution` 已能承载） |
| 快照 diff 无法归因 | `ChangeAttribution` evidence → factor → decision | ✅ 已建模 |

### 9.3 自动调度闭环（新增最小调度器）

> **代码核实更新（2026-09）**：宏观环的引擎资产几乎全部已建成但存在六条"建好未接线"的 gap——`midterm/persistence.py`（append-only JSONL）**零调用点**、`midterm/diff.py`（五层差分）**零调用点**、`financial_report/`（财报 OCR 原文管线）未接入证据抽取（`nodes/midterm.py` 的 `_window_texts()` 因"主链不提供原文"而置空）、`stale_after`/`exit_conditions` 只有字段无调度、`ThesisVersion` 模型无读写逻辑。完整接线清单（W1–W6）与 T0–T4 实施计划见 `docs/design/RESEARCH_CONTINUUM_DESIGN.md` §3/§10。

在 `main.py` 之外新增一个轻量跟踪入口（如 `alphabee/tracking/scheduler.py`，v1 可先做 CLI 命令而非常驻进程）：

```text
trigger（财报发布 / 公告 / 行情异动 / stale_after 到期 / 手动）
  → collect 增量事实 → 构建新 FactorSnapshot
  → CompanyStateDiff（五层差分 + 归因）
  → 检查 exit_conditions_met 与 falsification conditions
     ├─ 触发 → 产出"thesis_broken / alpha_exhausted"信号（D3，escalate 级）
     └─ 未触发 → bayes 更新 P(H|E) → StateShift 检查
         ├─ tv_distance > 阈值 → 触发一次"复核 run"（走微观环流水线）
         └─ 正常 → 追加 evidence_log，更新 next_evidence_to_watch
```

### 9.4 宏观环的 C 维度升级策略

跟踪闭环一旦产出**行动类建议**（状态迁移、仓位带变化，`position.py` 已建模）：

- 行动类输出的 gate 语义从"质量检查"变为"放行确认"：只允许 Tier 0（完整）或 Tier 5（升级人工）；
- 不可逆操作（真实下单）**永不自动执行**——框架红线：AlphaBee 是分析系统，行动层必须人工确认（对应偏离论中"错误代价 C 的任务需要人工 gate"）。

---

## 10. 预算与升级（Deviation Budget & Escalation）

**核心动作：把 severity 从静态标签升级为"代价敞口"，给整个 run 一个偏离预算。**

### 10.1 代价敞口

```text
cost_exposure(issue) =
    severity_weight(issue.severity)
    × amplification_factor(issue.amplified_by)     # §8 标注的下游放大
    × (1 − recoverability(issue.recovery_action))  # 已恢复则 ≈0
```

`severity_weight` 初始建议：low=1、medium=3、high=10、critical=30（可在 `config.yaml` 调整）。

### 10.2 Run 级预算

```text
D_cum(run) = Σ cost_exposure(open issues)
if D_cum > D_max(run):   # D_max 按任务类型配置，分析场景默认宽松
    → 选择升级路径：
      1) degrade 最终产物（置信度强制下调一档，报告标注"分析完整性受限"）
      2) escalate 人工（D_cum 极高或命中 critical）
      3) abort（数据层完全失效等极端场景）
```

### 10.3 验证预算（与 ROADMAP 1.5 合并落地）

`explore_conflicts` / `verify_hypotheses` 的 B 维度控制：每个冲突最多验证 2–3 个最高价值假设、每个假设最多 N 次工具调用、优先"最快排除路径"。预算耗尽 → D5 偏离 `budget_exhausted` + 未探索区域记入账本（§5.3）。

---

## 11. 度量层：Deviation Telemetry（ATDI 的 AlphaBee 落地）

**目标：把 §3 的 8 维从定性框架变成每 run 可计算的指标，验证"偏离指标是否比步数更能预测 run 质量"。**

### 11.1 每 run 指标（由 §5 账本 + 现有 `EvaluateMetrics` 直接计算）

| 指标 | 定义 | 数据源 |
|---|---|---|
| 检测率 $\hat O$ | 节点级检测器发现的偏离 ÷（节点级发现 + report gate 兜底发现的上游遗留） | 账本 `detected_at_step` vs `related_step` |
| 平均检测时延 | `detected_at_step` 的节点序 − 产生节点的节点序 | 账本 |
| 恢复率 $\hat R$ | 有 `recovery_action` 且 resolved 的 issue ÷ 全部 issue | 账本 |
| 恢复半衰期 | 从检测到 resolved 的节点数中位数 | 账本 |
| 加权边推翻率 | `amplification_audit` Decision 中"方向不一致"占比 | §8.2 |
| 在轨曲线 | P(节点产出无 D1–D5 偏离) 随节点序的衰减 | 账本 + Step 状态 |
| 预算消耗 | `D_cum / D_max` | §10 |
| 静默劣化率 | gate 兜底发现的 D2/D1 ÷ 全部（越接近 0 越好） | 账本 |

### 11.2 跨 run 指标

- 偏离指纹的复发率（同指纹重复出现 → 系统性缺陷而非偶发漂移，直接转 `data_fix_tasks` 或 prompt 校准）；
- per-symbol 偏离画像（§5.3）；
- 降级率趋势：`degraded=true` artifact 占比升高 → prompt/schema 与模型输出漂移加剧（insight 设计文档 10.3 已有此建议，泛化到全链路）。

### 11.3 验证方法

离线回放（ENGINEERING_ROADMAP 第 10 条已有"离线回放"建议）：用历史 run 的 artifact 落盘数据回放，比较"步数 H"与"偏离指标集"对 run success（gate passed + 用户评分）的预测力。若偏离指标显著更优，则 ATDI 路线在 AlphaBee 上成立，可反哺难度评估与测试用例生成。

---

## 12. 与现有 ROADMAP 的关系

本框架**不新增业务方向**，而是给既有条目一个统一坐标系。映射：

| 既有条目 | 状态 | 本框架归属 |
|---|---|---|
| P0 Decision 补齐 evidence refs | 🟡 | §6.2 `evidence_refs_present` 检测器 |
| 0.6 用户/调试输出分层 | ⬜ | D2/D5 偏离的呈现边界（debug 视图 = 账本视图） |
| Phase 1.5 验证预算 / 最短排除路径 / 未探索区域 | 🟡 | §10.3 + §5.3 |
| Phase 3 Claim-Evidence Graph | ⬜ | §6.3 假设登记簿是前置；D3 检测的结构化载体 |
| 0.4 降级阶梯 | ✅ | §7 协议的原型，泛化即可 |
| 0.5 冲突生命周期 | ✅ | O 维度的结算语义 ✅ |
| ENGINEERING_ROADMAP E5 错误处理统一 | ⬜ | §5 账本 + §6 检测器的工程底座 |
| ENGINEERING_ROADMAP E6 契约测试 | ⬜ | §6 节点契约的可测形式 |
| ENGINEERING_ROADMAP 第 10 条离线回放 | ⬜ | §11.3 度量验证 |
| 中期决策层（midterm） | 🟡 | §9 宏观环，只补调度 |
| 数据采集失败库 | ✅ | §5.2 账本持久化的复用底座 |

**建议在 `docs/roadmap/ROADMAP.md` 推荐优先级表新增一行**（实施本框架时执行）：

> | P1 | 偏离控制框架 F0（账本 + 分类法） | 统一现有零散检测/恢复机制，产出可观测的偏离指标 | ⬜ 未实现 |

---

## 13. 分期实施

> 每期独立可交付、可回滚。F0–F1 是纯增量（只加字段与检测器），风险最低。
> **代码层面规格（文件/签名/接线点/测试/PR 切分）见 §14 对应小节**，本节只给范围与工期。

### F0：偏离账本 + 分类法（约 1 天）→ 代码规格 §14.1

- `core/schemas.py`：`DeviationClass` + `Issue` 新增 5 字段（§4）；
- `orchestrator/services/deviation.py`：`record_deviation` helper（节点不写库，run 尾部统一 sink）；
- `data_fetch/models.py` 新增 `deviation_events` 表 + `deviation_store.py` + 指纹（复用失败库模式）；
- 测试：字段向后兼容、旧 issue 序列化/反序列化、账本写入/去重。

### F1：节点契约 + 后置检测器（约 2 天）→ 代码规格 §14.2

- `orchestrator/node_contracts.py`：`NodeContract` + `NODE_CONTRACTS` 全节点登记 + `validate_contracts()`；
- 落地 §6.2 首批 6 个检测器（`orchestrator/detectors.py`），经 `with_deviation_detection` 包装器
  接入节点注册处（**不改任何节点函数体**）；
- 测试：每个检测器的触发/不触发用例；契约测试（节点全覆盖/权重一致/ladder 合法）；现有 orchestration 测试无回归。

### F2：恢复阶梯协议化（约 2 天）→ 代码规格 §14.3

- 各节点契约声明 ladder 配置（§7.1）；`collect_raw_facts` 补一次补充采集回环（`supplement_round` 已有状态位，接线即可）；
- `orchestrator/recovery.py::choose_recovery` 统一裁决 Tier 0–5；降级传导规则落地
  （`services/degradation.py::damp_confidence`，一档封顶）；
- 测试：阶梯路由、回环预算上限、传导规则（**唯一改变既有产物 confidence 的阶段**）。

### F3：放大标注 + 加权边审计（约 2 天）→ 代码规格 §14.4

- 权重显式化（`INSIGHT_CONFIDENCE_WEIGHTS`）+ `NodeContract.amplification_labels` 接线；
- `reviewer.audit_amplification` 产 `amplification_audit` Decision（§8.1 第 1 行优先）；
- 测试：权重与契约常量一致性、审计 Decision 产出、方向不一致 → D3。

### F4：宏观环自动调度（约 3 天）→ 代码规格 §14.5-A

- `alphabee/tracking/`（`triggers.py` + `scheduler.py` + `__main__.py`）：stale_after 到期 /
  exit_conditions_met / tv_distance 阈值 → 复核 run；
- 行动类输出 gate 收紧（§9.4：Tier 0/5 二值，`require_human_confirm` 为唯一放行口，v1 恒 False）；
- 测试：假快照序列驱动的状态迁移与告警。

### F5：度量层 + per-symbol 画像（约 2 天）→ 代码规格 §14.5-B

- `orchestrator/services/telemetry.py` 计算 §11.1 指标，落 `deviation_events` 同库；
- CLI `--deviations` 视图（`render_deviation_timeline`）展示账本时间线；
- per-symbol 偏离画像反哺 `resolve_industry_context` / `resolve_company_track`。

**合计约 12 个工作日**（不含 F4 若需常驻调度器的额外工程量）。依赖关系：F0 → F1 → F2 → F3；F4/F5 可与 F2/F3 并行（F4 依赖 midterm 现有模型，不依赖 F1–F3）。

---

## 14. 代码层面详细设计（F0–F5）

> §13 给出的是**交付物粒度**，本节给出**可直接开工的代码粒度**：文件清单、类/函数签名、
> 接线点、序列化兼容策略、配置项、测试矩阵、PR 切分。所有签名已对照现有代码核实
> （`core/schemas.py` / `orchestrator/state.py` / `orchestrator/collectors.py` /
> `data_fetch/models.py` / `data_fetch/fingerprint.py`）。

### 14.0 通用工程约定（所有阶段共同遵守）

**文件清单**

| 文件 | 动作 | 阶段 | 说明 |
|---|---|---|---|
| `alphabee/core/schemas.py` | 改 | F0 | `DeviationClass` 枚举 + `Issue` 追加 5 字段 |
| `alphabee/orchestrator/services/deviation.py` | 新增 | F0 | 分类法惰性映射 + `record_deviation` + 节点序工具 |
| `alphabee/data_fetch/models.py` | 改 | F0 | `DeviationEvent` ORM 表 |
| `alphabee/data_fetch/deviation_store.py` | 新增 | F0 | 账本读写 + 指纹 upsert + per-symbol 聚合 |
| `alphabee/orchestrator/nodes/record_deviations.py` | 新增 | F0 | run 尾部账本 sink（§14.1-D） |
| `alphabee/orchestrator/node_contracts.py` | 新增 | F1 | `NodeContract` + `NODE_CONTRACTS` 注册表 |
| `alphabee/orchestrator/detectors.py` | 新增 | F1 | 检测器注册表 + 首批 6 个确定性检测器 |
| `alphabee/orchestrator/services/detection.py` | 新增 | F1 | `with_deviation_detection` 节点包装器 |
| `alphabee/orchestrator/recovery.py` | 新增 | F2 | `RecoveryTier` + `choose_recovery` |
| `alphabee/orchestrator/services/degradation.py` | 新增 | F2 | 降级输入扫描 + confidence 阻尼 |
| `alphabee/agents/thesis/reviewer.py` | 改 | F3 | `audit_amplification`（§14.4） |
| `alphabee/agents/thesis/engine.py` | 改 | F3 | 权重显式常量化（`_apply_insight` 的 ×0.85） |
| `alphabee/tracking/{__init__,scheduler,triggers}.py` | 新增 | F4 | 宏观环调度（CLI） |
| `alphabee/orchestrator/services/telemetry.py` | 新增 | F5 | §11.1 指标计算 |
| `alphabee/config/__init__.py` + `config.yaml` | 改 | F2 | `Settings.deviation: DeviationSettings`（§14.6）（deviation.ledger.enabled 随 DeviationSettings 在 F2 落地；§15.9 开关验证自 F2 起可实际执行） |

**依赖方向**（禁止反向，防止 import 环）

```text
core/schemas.py            ← 只依赖 pydantic/enum（Issue 字段）
  ↑
orchestrator/services/*    ← 可依赖 core、orchestrator/state、contracts
  ↑
orchestrator/nodes/*       ← 可依赖 services；不得被 services import
data_fetch/*               ← 只依赖 core；不得 import orchestrator（保持数据层独立）
```

**兼容策略**

1. `Issue` 只 **append** 字段，全部带默认值；旧 run 的历史 artifact/issue JSON 反序列化不变；
2. 新增枚举只新增成员，不改旧成员字符串值；
3. 检测器 / 账本写入**必须可失败**：任何异常 → `logger.warning` + 返回空列表，绝不打断节点
   （沿用 `data_fetch/helper.py::_report_tushare_failure` 的 "never let failure recording break the caller" 纪律）；
4. 所有新行为由 `config.yaml` 开关控制（§14.6），默认开启检测、默认不写库 fail-open；
5. 新增 ORM 表只 `create_all`（`init_db()` 已有），不改既有表结构 → 回滚只需 drop 新表，无需数据迁移。

---

### 14.1 F0 代码设计：分类法 + 偏离账本

#### A. `core/schemas.py`：枚举与字段

```python
class DeviationClass(enum.StrEnum):
    """偏离分类法（§4）。新增字段的取值域；旧 issue 为 None → 惰性映射。"""
    D1_DATA = "d1_data"          # 数据偏离
    D2_STRUCTURE = "d2_structure"  # 结构偏离（parse/schema/降级）
    D3_ARGUMENT = "d3_argument"  # 论证偏离
    D4_STATE = "d4_state"        # 状态偏离（假设失效/上下文截断/重复调查）
    D5_CONTROL = "d5_control"    # 控制偏离（回环打满/预算耗尽）
```

`Issue`（现 `core/schemas.py` 的 `Issue`，实测 **10 字段**）追加：

```python
    deviation_class: DeviationClass | None = Field(
        default=None, description="归一化的偏离分类（§4）；None 表示按 category 惰性映射。"
    )
    detected_at_step: str | None = Field(
        default=None, description="后置检测器所在节点 id（≠ related_step 的产生地）。"
    )
    recovery_action: str | None = Field(
        default=None, description="恢复动作标识，如 rerun_round=1 / degraded_tier=2 / escalated。"
    )
    recovery_cost: int | None = Field(
        default=None, description="恢复代价：重跑轮数 / 降级层数；0=未恢复；None=未尝试。"
    )
    amplified_by: list[str] = Field(
        default_factory=list, description="被哪些下游放大边传导（§8 标注的边 key）。"
    )
```

> 与 §4 草案的唯一差异：`amplified_by` 用 `list[str] = Field(default_factory=list)` 而非
> `list[str] | None`——与 `Issue` 既有风格一致，且避免下游 `for x in (issue.amplified_by or [])`
> 的防御式写法散落各处。

#### B. `orchestrator/services/deviation.py`（新增）

```python
NODE_ORDER: tuple[str, ...] = (
    "collect_raw_facts", "resolve_industry_context", "resolve_company_track",
    "resolve_driver_profile", "run_analysis_engines", "explore_conflicts",
    "verify_hypotheses", "synthesize_insights", "run_thesis", "review_thesis",
    "resolve_midterm_decision", "midterm_decision_reporter", "generate_report",
    "review_report", "record_deviations", "finalize_message",
)  # 与 agent.py 实装配线一致；新增节点必须同步登记（契约测试断言）

CLASS_BY_CATEGORY: dict[str, DeviationClass] = {
    "missing_data": D1_DATA, "blocked": D1_DATA, "stale": D1_DATA,
    "numeric_inconsistency": D1_DATA, "cross_source_conflict": D1_DATA,
    "parse_error": D2_STRUCTURE, "schema": D2_STRUCTURE, "degraded": D2_STRUCTURE,
    "thesis_gap": D3_ARGUMENT, "thesis_conflict": D3_ARGUMENT,
    "evidence_chain_incomplete": D3_ARGUMENT, "unverified_claim": D3_ARGUMENT,
    "assumption_invalidated": D4_STATE, "context_truncated": D4_STATE, "state_drift": D4_STATE,
    "report_rewrite_needed": D5_CONTROL, "budget_exhausted": D5_CONTROL,
    # ── F0 实测覆盖扩展（现网真实生产点，R1 前经 captain 批准）──
    "company_track_missing": D1_DATA, "company_track_stale": D1_DATA,
    "peer_group_missing": D1_DATA, "peer_group_benchmarks_missing": D1_DATA,
    "industry_context_missing": D1_DATA, "industry_benchmarks_missing": D1_DATA,
    "insight_degraded": D2_STRUCTURE, "driver_profile_degraded": D2_STRUCTURE,
    "context_build_failure": D2_STRUCTURE, "empty_response": D2_STRUCTURE,
    "thesis_warning": D3_ARGUMENT, "verified_conflict": D3_ARGUMENT,
    "conflict": D3_ARGUMENT, "verification_needed": D3_ARGUMENT,
    "market_regime": D4_STATE,
    "subagent_failure": D5_CONTROL, "midterm_decision_failed": D5_CONTROL,
    "midterm_decision_report_failed": D5_CONTROL, "failure": D5_CONTROL,
}
# 覆盖守卫：扫描 alphabee/ 全部 Issue(category=...) 生产点，未登记即测试失败
# （tests/orchestrator/test_deviation_service.py::test_every_produced_issue_category_is_registered）

def resolve_deviation_class(issue: Issue) -> DeviationClass:
    """显式字段优先，否则按 category 惰性映射；未知 category → D2（保守归为结构偏离）。"""

def step_index(node_id: str | None) -> int | None:
    """节点 id → 节点序（§11 检测时延 = step_index(detected_at_step) − step_index(related_step)）。"""

def record_deviation(
    class_: DeviationClass,
    severity: IssueSeverity,
    message: str,
    *,
    detected_at_step: str,
    related_step: str | None = None,
    related_artifact: str | None = None,
    category: str | None = None,
    scope: IssueScope = IssueScope.REPORT,
    recovery_action: str | None = None,
    recovery_cost: int | None = None,
    amplified_by: list[str] | None = None,
) -> Issue:
    """统一上报入口（§5.1）。category 缺省取该检测器的稳定名（账本指纹依赖它，必须稳定）。"""
    return Issue(
        id=make_id("issue"), severity=severity, category=category or class_.value,
        message=message, related_step=related_step or detected_at_step,
        related_artifact=related_artifact, scope=scope,
        deviation_class=class_, detected_at_step=detected_at_step,
        recovery_action=recovery_action, recovery_cost=recovery_cost,
        amplified_by=list(amplified_by or []),
    )

def detection_latency(issue: Issue) -> int | None:
    """检测时延（节点数）。任一端节点未知 → None（不猜测）。"""
```

**设计决策：节点不做 IO。** `record_deviation` 只产 `Issue` 对象并由节点返回的 partial state 携带，
**不在节点内写数据库**——理由：(1) LangGraph 节点应保持确定性、可重放，检测器与账本写入属副作用；
(2) `OrchestratorState.issues` 已有 `_merge_by_id` reducer（`state.py:23`），按 id 幂等；
(3) 测试可直接断言节点返回的 issues，无需 fixture 数据库。持久化统一在 run 尾部（见 D）。

#### C. `data_fetch/models.py` + `deviation_store.py`（账本）

```python
class DeviationEvent(Base):          # data_fetch/models.py 追加（与 DataFetchEvent 同 Base）
    __tablename__ = "deviation_events"
    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    symbol: Mapped[str | None] = mapped_column(String(32), index=True)
    step_id: Mapped[str | None] = mapped_column(String(64))        # 产生地（related_step）
    detected_at_step: Mapped[str | None] = mapped_column(String(64))  # §6 检测器所在节点
    deviation_class: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    message: Mapped[str | None] = mapped_column(Text)
    recovery_action: Mapped[str | None] = mapped_column(String(64))
    recovery_cost: Mapped[int | None] = mapped_column(Integer)
    amplified_by: Mapped[list[str] | None] = mapped_column(JSON)
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
```

- **存储选择**：复用 `fetch_events.db`（同一 `Base.metadata`、同一 `init_db()`、
  同一 `DATA_FETCH_DB_PATH` 覆盖口），独立表名。理由：少一个数据库文件、复用连接与测试
  fixture 模式；替代方案（独立 `deviation.db`）在写并发/清理粒度上更干净，但 v1 收益不足。
- **指纹**：复用 `data_fetch/fingerprint.py::compute_fingerprint`（sha256 前 16 位），
  输入 `(deviation_class, category, normalized_message, symbol, detected_at_step)`；
  `normalized_message` = 正则 `\d+(\.\d+)?` → `#`、压缩空白 —— 让"同一偏离不同数字"
  归并为同一指纹（否则复发率统计失效）。
- `deviation_store.py` API：

```python
def record_event(issue: Issue, *, run_id: str, symbol: str | None, step_id: str | None) -> DeviationEvent:
    """按指纹 upsert：命中 → occurrence_count+1、last_seen_at 刷新；未命中 → 插入。"""
def mark_resolved(issue_id: str, *, recovery_action: str, recovery_cost: int) -> bool:
    """标记已恢复。注意 issue_id 实为**账本身份 fingerprint**（表内无 Issue.id 列，见下方注记）。"""
def list_events(*, symbol=None, run_id=None, deviation_class=None, since=None) -> list[DeviationEvent]: ...
def summarize_symbol(symbol: str) -> dict[str, Any]:
    """per-symbol 偏离画像（§5.3）：各类偏离计数 / 复发指纹 top-N / 平均检测时延。"""
def purge_before(cutoff: datetime) -> int: ...
```

> **身份语义（F0 实测补注）**：`deviation_events` 表**没有 `Issue.id` 列**（字段表见 §14.1-C 上方），
> 因此 `mark_resolved(issue_id, ...)` 的 `issue_id` 实为**账本身份 `fingerprint`**
> （16 位 hex：取自 `record_event(...)` 返回行的 `.fingerprint`，或用
> `compute_deviation_fingerprint(...)` 重算）；传 `Issue.id`（形如 `issue-<hex12>`）必然匹配不到、
> 返回 `False`（已有用例锁定）。F2 恢复阶梯调用时须用 fingerprint。
> 另：`summarize_symbol` 的"平均检测时延"需要节点序（orchestrator 层），数据层不反向 import，
> 故实际签名为 `summarize_symbol(symbol, *, step_index=None)`——由调用方注入
> `services/deviation.step_index`；未注入时该指标为 `None`。

#### D. run 尾部 sink：`orchestrator/nodes/record_deviations.py`（新增节点）

```python
# alphabee/orchestrator/nodes/record_deviations.py
from alphabee.orchestrator.collectors import _finalize_step   # 与 insights.py:23 同模式
from alphabee.orchestrator.services.deviation import resolve_deviation_class

async def record_deviations(state: OrchestratorState, config: RunnableConfig) -> dict[str, Any]:
    """把本 run 的偏离写入账本（副作用集中在此节点，失败只记 warning）。

    F0 只做账本写入；F5 在此节点追加 compute_deviation_metrics + store_metrics。
    返回 partial state（与其他节点一致），不修改 state["issues"]。
    """
    step = Step(id="record_deviations", kind="record_deviations", status=StepStatus.RUNNING)
    try:
        run = state.get("run")
        for issue in state.get("issues", []):
            resolved = issue if issue.deviation_class is not None else issue.model_copy(
                update={"deviation_class": resolve_deviation_class(issue)}
            )
            record_event(resolved, run_id=run.id if run else "", symbol=..., step_id=issue.related_step)
    except Exception as exc:                       # fail-open：绝不打断 run（§14.0 约定 3）
        logger.warning("deviation ledger write failed", error=str(exc))
    return {"steps": [_finalize_step(step, [], [])]}
```

- **图接线**（`agent.py`）：`finalize_message` 之前插入**三条**边（F0 实装，勿简化为两条）——
  `_graph.add_edge("generate_report", "record_deviations")`（默认路径必经 sink，**替换**原来的
  `generate_report → finalize_message` 直连）、
  `_graph.add_edge("review_report", "record_deviations")`（report gate 回环启用后生效）、
  `_graph.add_edge("record_deviations", "finalize_message")`（`finalize_message` 只保留 sink 这一个
  入边，避免绕过账本）。注意 F4 会引入第二条入口（tracking），该节点必须对"无 midterm /
  无 review_report 路径"同样可达（只依赖 `state["issues"]`，不依赖上游 artifact）。
- **可达性理由（不要照原稿"还原"成两条边）**：`review_report` 目前**不可达**（report quality gate
  回环仍被注释），只接 `review_report → record_deviations` 会让 sink 在默认路径**永不执行**，
  违反上一条"无 review_report 路径同样可达"的要求（把 sink 变成死节点）。
  另注意：LangGraph（1.2.9 实测）的可绘制图 `get_graph()` 会剪掉"源不可达"的边（例如当前
  `review_report → record_deviations`），判据应以 `builder.edges` 为准，勿被 `get_graph()` 显示误导
  （F4 接入 tracking 入口时尤其注意）。
- **开关**：节点运行时读 `deviation.ledger.enabled` / `deviation.ledger.fingerprint_normalize`（§14.6），
  配置段缺失或读取异常 → 默认开启（fail-open）；Step 的 `inputs` 记录
  `written / failed / skipped`（满足三者之和 = 本 run issue 数），便于观测被 fail-open 丢弃的量。
- **为什么单独成节点而不是塞进 `finalize_message`**：`finalize_message` 已有"组装 AIMessage"
  单一职责；账本写入涉及 DB 与网络无关的 IO，单独节点便于 feature flag 关闭与单测替换。

---

### 14.2 F1 代码设计：节点契约 + 后置检测器

#### A. `orchestrator/node_contracts.py`

```python
class AmplificationMode(enum.StrEnum):
    VERBATIM = "verbatim"; WEIGHTED = "weighted"; OVERRIDE = "override"

class NodeContract(BaseModel):
    node_id: str
    preconditions: list[str] = Field(default_factory=list)
    postconditions: list[str] = Field(default_factory=list)
    detectors: list[str] = Field(default_factory=list)          # detectors.py 注册名
    recovery_ladder: tuple[int, ...] = ()                       # §7.1 允许 Tier
    amplification_labels: dict[str, AmplificationMode] = Field(default_factory=dict)
    max_retries: int = 0                                        # 0 = 禁止 Tier 4

NODE_CONTRACTS: dict[str, NodeContract] = { ... }   # 16 节点全登记（含 F0 的 record_deviations sink）（值见 §7.1 与 §8.1）

def get_contract(node_id: str) -> NodeContract | None: ...
def validate_contracts() -> list[str]:
    """返回契约违规清单（启动/CI 断言）：未登记节点、recovery_ladder 非法、
    max_retries>0 但 state 无对应计数器、WEIGHTED 边无审计节点。"""
```

`validate_contracts()` 的断言（写进契约测试）：
1. `set(NODE_CONTRACTS) == set(NODE_ORDER)`；
2. `max_retries > 0` 的节点只允许 `collect_raw_facts`（`supplement_round`）与 `generate_report`（`report_review_round`），且上限等于 `OrchestratorState` 里的 `max_*_rounds`；
3. 每条 `WEIGHTED` 边在 `amplification_audit` 覆盖表（§14.4）中有对应审计项；
4. 每个 `detectors` 名都在 `DETECTORS` 注册表中存在。

#### B. `orchestrator/detectors.py`

```python
@dataclass(frozen=True)
class NodeContext:
    node_id: str
    step: Step | None
    new_artifacts: list[Artifact]          # 本节点产物
    view: dict[str, Any]                   # state ⊕ update 的合并视图（见 C）

@dataclass(frozen=True)
class DetectionResult:
    detector: str
    passed: bool
    deviation_class: DeviationClass | None = None
    severity: IssueSeverity = IssueSeverity.LOW
    category: str = ""                     # 稳定名（不进指纹的随机部分）
    message: str = ""
    related_artifact: str | None = None

Detector = Callable[[NodeContext], DetectionResult]
DETECTORS: dict[str, Detector] = {}
def detector(name: str, category: str) -> Callable[[Detector], Detector]: ...  # 注册装饰器
def run_detectors(contract: NodeContract, ctx: NodeContext) -> list[Issue]:
    """执行契约声明的检测器；单个检测器异常 → warning + 跳过，不影响其他检测器。"""
```

首批 6 个检测器**实现要点**（对照 §6.2）：

| 检测器 | 代码要点 |
|---|---|
| `derived_facts_nonempty` | `find_artifact_model(ctx.new_artifacts, ArtifactType.DERIVED_FACTS, DerivedFactsArtifact)`；`results` 空 + `ctx.view["fact_values"]` 空 → D1 低severity（"可能真无数据"，模板见 §16 反模式表）；`results` 空但 `fact_values` 非空 → D1 high（"引擎故障"） |
| `artifact_schema_valid` | 遍历 `ctx.new_artifacts`，按 `ArtifactType → model` 映射表调 `find_artifact_model(..., coerce=True)`；无可登记类型的 artifact 跳过（不误报） |
| `insight_artifacts_present` | `synthesize_insights` 出口断言 `INSIGHT_ANALYSIS` 存在且 `core_view` 非空（现状由四级降级保证，此处只做协议化登记） |
| `evidence_refs_present` | 遍历本节点新增 `Decision`（`ctx.view["decisions"]` 与本节点 step 关联项）：`verdict` 类 Decision 必须 `evidence_refs` 非空 → 否则 D3 |
| `downstream_inputs_present` | 复用 `payload_builders.build_report_generation_payload`：thesis / insight / anomaly / conflict 四段任一为空 → D1/D2，并**建议** Tier 2 分支（不自动执行） |
| `assumption_still_valid` | 读 §6.3 `ASSUMPTION_REGISTRY` artifact：存在 `status="invalidated"` 且被本节点引用 → D4 |

#### C. 接线方式：包装器而非改节点函数体（关键决策）

```python
# alphabee/orchestrator/services/detection.py
def with_deviation_detection(node_id: str, fn: NodeFn) -> NodeFn:
    async def wrapper(state: OrchestratorState, config: RunnableConfig) -> dict[str, Any]:
        update = await fn(state, config)
        contract = get_contract(node_id)
        if contract is None or not contract.detectors or not settings.deviation.detection.enabled:
            return update
        ctx = NodeContext(
            node_id=node_id,
            step=_last_step(update),
            new_artifacts=list(update.get("artifacts") or []),
            view=_merge_state_view(state, update),     # 复用 state.py 的 _merge_by_id / _append_items
        )
        issues = run_detectors(contract, ctx)
        return {**update, "issues": [*(update.get("issues") or []), *issues]}
    return wrapper
```

- **注册处包装**（`agent.py`）：`_graph.add_node("run_analysis_engines", with_deviation_detection("run_analysis_engines", run_analysis_engines))`；
  选择理由：零侵入节点函数体、可全局开关、测试可直接调用 `wrapper`（构造 state + 假 update）；
  替代方案（节点内显式调用 `run_detectors`）耦合更高、30+ 处改动，不采纳。
- `_merge_state_view(state, update)`：浅合并 + 复用 `state.py::_merge_by_id` / `_append_items`
  （避免检测器看到的 `artifacts/issues` 与 LangGraph reducer 结果不一致）；
  返回普通 dict（检测器只读）。
- 检测器**只读**，不得改 `update`（防止"检测器修数据"式隐式恢复，恢复必须走 §7 阶梯）。

#### D. 假设登记簿（§6.3）序列化位置

`AssumptionEntry` 放入 `orchestrator/contracts.py`，注册 Artifact 类型
`ArtifactType.ASSUMPTION_REGISTRY`（`core/schemas.py` 的 `_ARTIFACT_TYPE_TO_ROLE_GROUP`
映射同步登记 → 走 `alphabee-pipeline-contract-steward` 流程）；`conflicts.py` / `verification.py`
在产假设时追加条目，`synthesize_insights` / `run_thesis` / `gates.py` 只读消费。

---

### 14.3 F2 代码设计：恢复阶梯协议化

#### A. `orchestrator/recovery.py`

```python
class RecoveryTier(IntEnum):
    TIER_0_COMPLETE = 0; TIER_1_PATCH = 1; TIER_2_DEGRADE = 2
    TIER_3_SKELETON = 3; TIER_4_RERUN = 4; TIER_5_ESCALATE = 5

@dataclass(frozen=True)
class RecoveryDecision:
    tier: RecoveryTier
    action: str        # "rerun_round=1" / "degraded_tier=2" / "escalated" / "keep"
    cost: int          # 0 / 1 / 2 / 3 / 4 / 5（直接作为 Issue.recovery_cost）
    reason: str

def choose_recovery(
    node_id: str,
    issues: list[Issue],
    *,
    contract: NodeContract,
    state: OrchestratorState,
) -> RecoveryDecision:
    """按契约声明的 ladder + state 计数器决定恢复档位（§7.1 表 → 分支逻辑）：

    **评估顺序（F2-4 澄清：分支编号 ≠ 评估顺序）**：
    第 0 步先做一次**短路裁决** —— 若命中 critical 且 §10 cost_exposure 超阈值，
    直接返回 TIER_5（**越过所有中间档**，不再评估 1–4）；否则按 1→5 顺序评估，
    都不中再落 6。这样编号与"越过中间档"的语义不再互相矛盾。

    0. （短路，优先于 1–5）命中 critical 且 §10 cost_exposure 超阈值 → 直跳 TIER_5；
    1. 无 issue → TIER_0；
    2. 有 issue 且命中"局部可修"检测器（schema 可 coerce）且 1 ∈ ladder → TIER_1；
    3. 命中降级条件（数据缺口 / 产物空洞）且 2 ∈ ladder → TIER_2；
    4. 命中骨架条件（核心输入整体缺失）且 3 ∈ ladder → TIER_3；
    5. 命中"可重试"条件且 max_retries>0 且当前轮次 < max → TIER_4（回环）；
    6. 其余（含 5 的预算耗尽）→ TIER_5（escalate）。
    """
```

- 与既有计数器的唯一接线点：`state["supplement_round"] < state["max_supplement_rounds"]`、
  `state["report_review_round"] < state["max_report_review_rounds"]`（`state.py:63-67`）；
  `choose_recovery` 只**读**，轮次自增仍由既有路由函数负责（`route_after_report_review`，`gates.py:550`）。
- Tier 2/3 的产物必须带 `degraded=True` + `degradation_reason`：由
  `services/degradation.py::apply_degradation(artifact_value, tier, reason)` 统一写入，
  避免各节点手写字段名漂移。

#### B. 降级传导（§7.2 规则 2 的代码化）

```python
# alphabee/orchestrator/services/degradation.py
def degraded_inputs(artifacts: list[Artifact]) -> list[str]:
    """扫描所有 typed artifact 的 degraded 字段，返回降级 artifact id 列表。"""

_CONF_DOWNGRADE = {"high": "medium", "medium": "low", "low": "low"}   # 一档封顶（防保守化螺旋）

def damp_confidence(value: str | float | None, degraded_inputs: list[str]) -> str | float | None:
    """消费降级输入的结论 → confidence 下调一档（离散）/ ×0.85（连续，沿用 insight 先例）。"""
```

- 接入点：`agents/thesis/engine.py`（维度 confidence）、`agents/insights/*`（insight confidence）；
- 单测断言"下调一档且只一档"（多次降级输入不叠加）——对应 §16 的保守化螺旋风险。

#### C. `collect_raw_facts` 补充采集回环（§7.1 第 1 行）

现状已具备：`supplement_round` / `max_supplement_rounds` 状态位与 D1 缺口 issue。
F2 只需在契约里声明 `recovery_ladder=(0, 4, 2)`、`max_retries=1`，并把
"是否回环"从隐式判断改由 `choose_recovery` 统一裁决（行为等价，语义统一）。

---

### 14.4 F3 代码设计：放大标注 + 加权边审计

#### A. 权重显式化（先做，审计才能引用）

`agents/thesis/engine.py::_apply_insight` 的 ×0.85 提到模块常量：

```python
INSIGHT_CONFIDENCE_WEIGHTS: dict[str, float] = {"high": 1.0, "medium": 0.92, "low": 0.85}
```

契约 `NODE_CONTRACTS["run_thesis"].amplification_labels = {"insight->thesis": WEIGHTED}`；
契约测试断言 `INSIGHT_CONFIDENCE_WEIGHTS` 与文档 §8.1 表中登记值一致（防"文档写 0.85、代码是 0.9"漂移）。

#### B. `audit_amplification`（`agents/thesis/reviewer.py` 追加）

```python
class AmplificationAudit(BaseModel):
    edge: str                      # "insight->thesis" 等契约 key
    upstream_artifact: str
    weight: float | None
    direction_consistent: bool     # 加权方向 vs 信号/冲突证据方向
    rationale: str

def audit_amplification(
    thesis: ThesisArtifact,
    insight: InsightArtifact | None,
    signals: SignalAnalysisArtifact | None,
    conflicts: ConflictAnalysisResult | None,
    *,
    use_llm: bool = False,
) -> AmplificationAudit:
    """L1 确定性优先：insight 方向（core_view 关键词 + supporting/counter 计数）
    vs signal/anomaly 方向分（复用 score_engine 口径的方向符号）比对；
    use_llm=True 时叠加 L2 LLM 判读（沿用 ThesisReviewer 的 L1/L2 双审计模式）。

    **v1 口径（F3 定稿）**：L2 为**具名顺延项**——`use_llm` 参数**接受但忽略**（两取值均零
    LLM 调用，有用例钉住）。理由：§16 明示"不做 LLM 检测器"，与本行原描述冲突；以 §16 为准，
    待后续期需要时再实现 L2，并在此之前保持"审计结论完全确定可复现"。"""
```

- 产出两条落点：`Decision(maker="amplification_audit", ...)` 进 `state["decisions"]`；
  方向不一致 → `record_deviation(D3_ARGUMENT, severity=high, category="amplification_direction_conflict")`；
- 契约测试断言 §8.1 表中每条 WEIGHTED 边都有对应 `audit_amplification` 分支覆盖。

---

### 14.5 F4/F5 代码设计

#### A. F4：`alphabee/tracking/`（宏观环调度）

```python
# tracking/triggers.py
class TriggerKind(enum.StrEnum):
    FINANCIAL_REPORT = "financial_report"; ANNOUNCEMENT = "announcement"
    PRICE_MOVE = "price_move"; STALE_EXPIRED = "stale_expired"; MANUAL = "manual"

@dataclass(frozen=True)
class Trigger:
    kind: TriggerKind; symbol: str; reason: str; payload: dict[str, Any]

def detect_triggers(symbol: str, *, as_of: str, artifact: CompanyStateArtifact | None) -> list[Trigger]:
    """纯函数：stale_after 到期 / exit_conditions_met / tv_distance 阈值（阈值读 config）。
    复用 midterm 已有模型字段，不新增契约。"""

# tracking/scheduler.py
def reconcile(symbol: str, *, as_of: str) -> CompanyStateArtifact:
    """增量事实 → FactorSnapshot → diff → bayes 更新 → 状态检查（§9.3 的确定性内核；复用 midterm 引擎）。"""
def run_once(symbol: str, *, as_of: str | None = None) -> TrackingReport: ...
def run_watchlist(symbols: list[str]) -> list[TrackingReport]: ...
def main(argv: list[str] | None = None) -> int:      # python -m alphabee.tracking --symbol 600519 --once
def require_human_confirm(action: PositionDecision | StateTransition) -> bool:
    """§9.4：行动类输出只允许 Tier 0（完整）或 Tier 5（升级人工）——此函数是唯一放行口，
    v1 恒返回 False（永不自动执行），保留接口以便未来接入人工确认 UI。"""
```

- CLI 归属：`python -m alphabee.tracking`（新增 `__main__.py`），与 `data_fetch/__main__.py` 同模式；
- 调度器 v1 只做"一次性 reconcile + 告警落盘"，常驻循环留待后续（§13 F4 备注）。

#### B. F5：`orchestrator/services/telemetry.py`

```python
class DeviationMetrics(BaseModel):
    detection_rate: float | None          # §11.1 检测率
    mean_detection_latency: float | None  # 平均检测时延（节点数）
    recovery_rate: float | None
    recovery_half_life: float | None      # 中位数节点数
    amplification_overturn_rate: float | None
    on_track_curve: list[float]           # P(节点无 D1–D5 偏离) 随节点序
    budget_consumption: float | None      # D_cum / D_max
    silent_degradation_rate: float | None

def compute_deviation_metrics(state: OrchestratorState, ledger: list[DeviationEvent]) -> DeviationMetrics:
    """全部指标由账本 + state 直接计算；缺失分母 → None（不静默回退 0）。"""
def store_metrics(metrics: DeviationMetrics, *, run_id: str) -> None: ...
def render_deviation_timeline(run_id: str) -> str:
    """CLI --deviations 视图：节点序 × 偏离 × 检测时延 × 恢复动作 的文本时间线（§15 验收 1）。"""
```

- 视图接入口：`alphabee/apps/cli/`（与 `--debug` 同层），只读账本，不改编排。

---

### 14.6 配置项（`config.yaml` + `alphabee/config/__init__.py`）

```yaml
deviation:
  detection:
    enabled: true          # 检测器总开关（回滚用）
    llm_detectors: false   # v1 恒 false（§16 反模式：不做 LLM 检测器）
  budget:
    severity_weight: {low: 1, medium: 3, high: 10, critical: 30}
    d_max: {analysis: 60, tracking: 20}
    on_exceed: degrade     # degrade | escalate | abort
  ledger:
    enabled: true          # 账本写入总开关（fail-open）
    fingerprint_normalize: true
    retention_days: 365
  amplification:
    audit_enabled: true
    overturn_severity: high
  recovery:
    enabled: true
```

```python
class DeviationSettings(BaseModel):
    detection: DetectionSettings = DetectionSettings()
    budget: BudgetSettings = BudgetSettings()
    ledger: LedgerSettings = LedgerSettings()
    amplification: AmplificationSettings = AmplificationSettings()
    recovery: RecoverySettings = RecoverySettings()

class Settings(BaseModel):
    llm: LLMConfig
    langfuse: LangfuseConfig = Field(default_factory=LangfuseConfig)
    web_search: WebSearchConfig = Field(default_factory=WebSearchConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    deviation: DeviationSettings = Field(default_factory=DeviationSettings)   # 本框架追加（alphabee/config/__init__.py）
```

> 注意 `alphabee/config/__init__.py:59` 有模块级 `settings = get_settings()`（import 即读配置），
> 因此**新增字段必须带默认值**，否则缺少 `deviation` 段的 `config.yaml` 会在 import 期抛错；
> 检测器/账本运行时读 `get_settings().deviation`（或注入），不在模块导入期读取。

阈值（`tv_distance` 复核触发、`entropy` 等）沿用 §9.3 与 `MIDTERM_*` 既有常量，**不重复定义**——
配置只新增"偏离控制专用"参数，避免同一阈值两处维护。

---

### 14.7 测试矩阵

| 阶段 | 测试文件 | 关键用例（断言） |
|---|---|---|
| F0 | `tests/core/test_issue_deviation_fields.py` | 新字段默认值；旧 issue JSON 反序列化不变；`amplified_by` 默认空列表 |
| F0 | `tests/orchestrator/test_deviation_service.py` | 惰性映射全 category 覆盖；未知 category → D2；`detection_latency` 计算与 None 分支；`record_deviation` id 唯一 |
| F0 | `tests/data_fetch/test_deviation_store.py` | 同指纹 upsert 计数；数字不同但归一化同指纹；`summarize_symbol` 聚合；DB 不可用时不抛（fail-open） |
| F1 | `tests/orchestrator/test_node_contracts.py` | `validate_contracts()` 空违规；未登记节点被捕获；WEIGHTED 边审计覆盖；`max_retries` 与 state 计数器一致 |
| F1 | `tests/orchestrator/test_detectors.py` | 6 个检测器各"触发/不触发"双例；`derived_facts_nonempty` 的"真无数据 vs 引擎故障"分支；检测器抛异常不影响节点返回 |
| F1 | `tests/orchestrator/test_detection_wrapper.py` | 包装器注入 issue 到 partial state；开关关闭时零改动；`_merge_state_view` 与 reducer 一致 |
| F2 | `tests/orchestrator/test_recovery.py` | 阶梯决策树 7 条分支；预算耗尽 → Tier 5 + `budget_exhausted`；`recovery_cost` 赋值 |
| F2 | `tests/orchestrator/test_degradation_damping.py` | 一档封顶（多次降级输入不叠加）；离散/连续两条路径 |
| F3 | `tests/agents/thesis/test_amplification_audit.py` | 方向一致/不一致双例；Decision 落点；权重与契约常量一致 |
| F4 | `tests/tracking/test_triggers.py` + `test_scheduler.py` | 假快照序列驱动 `stale_after`/`exit_conditions` 触发；`require_human_confirm` 恒 False |
| F5 | `tests/orchestrator/test_telemetry.py` | 各指标公式对合成账本的正确值；分母为 0 → None；时间线渲染含节点序 |

**回归要求**：每期结束 `poetry run pytest`（当前 479 个 midterm/orchestrator 测试 + 全量）全绿；
`ruff check` 干净；契约变更跑 `alphabee-pipeline-contract-steward` 检查清单。

---

### 14.8 PR 切分与回滚

| PR | 内容 | 可独立合并 | 回滚方式 |
|---|---|---|---|
| PR1 | F0a：`DeviationClass` + `Issue` 字段 + `deviation.py` | ✅（无行为变化） | 还原字段（历史 JSON 仍可读） |
| PR2 | F0b：账本表 + `deviation_store` + `record_deviations` 节点 | ✅（副作用可关） | `deviation.ledger.enabled=false` 或 drop 表 |
| PR3 | F1a：`node_contracts.py` + 契约测试 | ✅（无行为变化） | 还原文件 |
| PR4 | F1b：`detectors.py` + 包装器 + 首批检测器接线 | ✅ | `deviation.detection.enabled=false` |
| PR5 | F2：recovery + degradation damping + config | ⚠️ 行为变更（降级传导） | 关 `deviation.recovery.enabled`；残留 issue 无害（**纯回滚**：关闭 = 逐格回到 pre-F2 —— 回环能力保留、`RunStatus` 一致、**不新增** D5 `budget_exhausted`；「残留 issue 无害」指回滚后账本中**既有**记录无需清理，而非回滚必须新增记录） |
| PR6 | F3：amplification audit | ✅ | `deviation.amplification.audit_enabled=false` |
| PR7 | F4：tracking 调度 CLI | ✅（新入口） | 不调用即可 |
| PR8 | F5：telemetry + CLI 视图 | ✅（只读） | 不调用即可 |

> 纪律：PR1–PR4 属"只加字段/只加检测"，应与业务 PR 分开提交；PR5 是唯一会改变既有
> 产物 confidence 的 PR，必须单独回归并核对 §16 保守化螺旋限制。

---

## 15. 验收标准

1. 任何一次 run 结束后，可从账本重建"偏离时间线"：哪个节点偏了 → 谁检测到（时延多少）→ 采取什么恢复 → 是否被下游放大 → 是否恢复；
2. `run_analysis_engines` 产出空 derived facts 时，出口即报 D1 偏离（检测时延 = 0 节点），而非等 report gate；
3. 所有 Tier≥2 降级产物带 `degraded=true` 且下游 confidence 实际下调（传导规则有测试）；
4. 加权边（§8.1）至少完成第 1 条审计接入，`amplification_audit` Decision 可在 artifacts 中追溯；
5. 宏观环：`exit_conditions_met=true` 的快照必然产出 thesis_broken/alpha_exhausted 信号，且行动类输出永不自动执行；
6. F0–F5 各自的单测与回归全绿；`docs/roadmap/ROADMAP.md` 状态行同步更新。

**代码级验收（对应 §14，逐条可自动化）**：

7. `validate_contracts()` 返回空违规清单，并作为 CI 断言生效：新增节点未登记、
   新增 `WEIGHTED` 边无审计覆盖、`max_retries>0` 与 state 计数器不一致 → 测试失败；
8. **fail-open 验证**：给检测器与账本写入注入异常（monkeypatch 抛错），run 的
   artifacts/issues/最终消息与实施前逐字段一致（只多一条 `logger.warning`）；
9. **开关验证**：`deviation.detection.enabled=false` + `deviation.ledger.enabled=false` 时，
   黄金样本 run 的输出序列化结果与实施前哈希一致（保证纯增量、可回滚）。
   **归属与比对口径（F0 review 修订）**：开关验证归 **F2**——`DeviationSettings` 在 F2 落地，
   届时 `deviation.detection.enabled` / `deviation.ledger.enabled` 才可执行（§14.0 文件表原写 F1b，
   口径以本条为准）。比对口径上，`Issue` 的 5 个新字段是 schema 追加（§14.0 约定 1），
   `finalize_message` 用 `model_dump()` 落盘时这 5 个键恒存在，因此 `issues` 的**全字段哈希必然
   变化，不属行为变更**。开关验证应比对 **artifacts / final_report / decisions 的完整序列化 +
   issues 的行为相关投影**（id / category / severity / message / related_step），或显式排除这 5 个
   新默认键；`task_records/recorder.py` 的 `IssueRecord` 只投影 4 个旧字段，天然满足该口径；
10. **契约兼容验证**：旧版本落盘的 `Issue` JSON（无 5 个新字段）能直接 `model_validate`
    通过并取到默认值，无需迁移脚本；`amplified_by` 默认 `[]` 而非 `None`。

---

## 16. 风险与反模式

| 风险 | 缓解 |
|---|---|
| **过度工程**：检测器/账本比业务本身还重 | 每期以"1 张表 / 1 个 helper / N 个确定性断言"为粒度；检测器必须是确定性断言，不做 LLM 检测器（v1） |
| **保守化螺旋**：降级传导 → 全链路 confidence 一路下调 → 输出失去信息量 | 传导只下调**消费降级输入的结论**，且下调一档封顶；insight 设计文档已提示同风险，沿用其"有意保守"判定 |
| **回环失控**：Tier 4 被滥用导致成本爆炸 | 回环预算只存在于契约声明的节点；每次回环写 D5 账本；`max_*_rounds` 硬上限现状保留 |
| **检测器误报**：把"真无数据"误判为"引擎故障" | §6.2 `derived_facts_nonempty` 的双重检查模式（artifact 空 + fact_values 空 → 判 D1 但 severity=low，标注"可能真无数据"）作为模板 |
| **账本噪声**：同一偏离每个 run 重复上报 | §5.2 指纹去重；复发率单独统计（复发 → 转修复任务而非继续记录） |
| **契约漂移**：加字段/加检测器破坏下游消费 | 所有契约变更走 `alphabee-pipeline-contract-steward`；只 append 字段、不语义迁移 |
| **宏观环假阳性告警**：tv_distance 阈值误触发复核 run | 阈值进 `config.yaml`；复核 run 本身走完整微观环（有自身预算），代价可控 |

---

## 17. 一句话总结

> AlphaBee 的架构演进方向不是"更多 Agent、更多工具"，而是**把已经零散存在的检测（Issue/gate/reviewer/降级/失败库/漂移度量）统一成一套"偏离分类 → 即时检测 → 阶梯恢复 → 放大审计 → 预算升级"的控制协议**，让"长时间跟踪 + 深度研究"的能力边界从"能跑多远"变成可测量的"能在轨多远"。
