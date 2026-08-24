# AlphaBee Orchestrator ↔ Midterm 集成设计（分析核心拆分 + update_belief 调用契约）

> **定位**：本文档回答一个结构性问题——`alphabee/orchestrator/` 在新中期决策模型里
> 还有没有价值、该怎么继续演进。结论是：**orchestrator 是 Analyst（单次研究引擎），
> 继续演进出「可被调用的分析核心」；`alphabee/midterm/` 是 OS（跨 run 决策层），
> 新起一个决策层去调它。两者共享 `alphabee/core/` 数据模型与独立引擎，通过 artifact
> 契约对接——这正是 `market_regime` 已经走过的路。**
>
> 本文档是 [`MIDTERM_INVESTMENT_ROADMAP.md`](MIDTERM_INVESTMENT_ROADMAP.md) 的配套实现级设计，
> 只谈「orchestrator 如何拆、midterm 如何调它、中间契约是什么」，不重复 ROADMAP 里的
> Phase 规划与业务框架。

> **状态（2026-08 与代码对齐）**：本文档为设计，尚未落地。以下所有代码位置与字段名
> 均已与当前仓库核对。

---

## 1. 一句话结论

```text
不推倒、也不硬塞。
orchestrator 保留为「单次研究引擎」，拆成「分析核心（可调用）+ 报告尾巴（展示层）」；
midterm 新建为「跨 run 决策层」，通过 run_analysis_core() 调分析核心，
再用 AnalysisCoreResult → SnapshotDiff → 贝叶斯更新 完成 update_belief。
```

---

## 2. 现状：orchestrator 的图形状与产物

当前 `alphabee/orchestrator/agent.py` 编译出的 `alphabee_agent`（`_graph.compile(store=InMemoryStore())`）
是**单 symbol、one-shot、以 Report 为终点**的流水线：

```text
collect_raw_facts → resolve_industry_context → resolve_company_track
→ run_analysis_engines → explore_conflicts → verify_hypotheses
→ synthesize_insights → run_thesis → review_thesis
→ generate_report → review_report → finalize_message → END
```

各节点产物（均为 `Artifact`，通过 `contracts.py` 的 `find_artifact_model` / `coerce_*` 消费）：

| 节点 | 产物（ArtifactType） | 对 midterm 的意义 |
|---|---|---|
| collect_raw_facts | FACT_COLLECTION | 事实底稿（`fact_values`） |
| run_analysis_engines | DERIVED_FACTS / SIGNAL_ANALYSIS / ANOMALY_REPORT | F / R 变量的规则证据 |
| resolve_industry_context / resolve_company_track | INDUSTRY_CONTEXT / COMPANY_TRACK | 赛道 + 对标组 + `peer_*`（F 变量的域上下文） |
| explore_conflicts / verify_hypotheses | CONFLICTS_RESULT / VERIFICATION_RESULTS | 已验证反证（反漂移的关键证据源） |
| synthesize_insights | INSIGHT_ANALYSIS | 观点主轴（core_view / central_tension / main_driver / what_would_change_my_mind） |
| run_thesis | THESIS_ANALYSIS | 8 维度判断 |
| review_thesis | THESIS_REVIEW + Decision/Issue | 维度审计 + 已结算冲突 |
| generate_report / review_report | REPORT | **展示层**（不进入 midterm 决策） |
| finalize_message | JSON AIMessage | 流式输出（CLI/报告用） |

**关键观察**：`finalize_message` 产出的是「一篇报告」，天然产物不是「一个可延续的状态」。
这正是它做不了 S0-S5 / Belief_t / 组合级决策的原因——不是能力不足，是**图形状就是 one-shot 的**。

---

## 3. 拆分边界：分析核心 vs 报告尾巴

切点选在 **`review_thesis` 之后**，理由：`review_thesis` 已经完成了「研究 + 论点 + 冲突结算 + 维度审计」，
`INSIGHT_ANALYSIS` / `THESIS_ANALYSIS` / `THESIS_REVIEW` / `CONFLICTS_RESULT` / `Decision` / `Issue`
**在这一点已经全部产出**。之后的 `generate_report → review_report → finalize_message` 只是「把已结算结果渲染成文本」。

```text
分析核心（可调用，产出 Insight/Thesis/Review/Conflict/Decision/Issue）
  collect_raw_facts → resolve_industry_context → resolve_company_track
  → run_analysis_engines → explore_conflicts → verify_hypotheses
  → synthesize_insights → run_thesis → review_thesis
                        │
                        ▼  ┌─────────────────────────────────────┐
报告尾巴（展示层，非决策）  │ generate_report → review_report → finalize │
                        └─────────────────────────────────────┘
```

- **分析核心**：midterm 的 `Form Thesis` / `Update Belief` 消费它。
- **报告尾巴**：仍保留，是「某个 snapshot 要给人看时」的渲染器；优先级排在状态/决策之后（对应 ROADMAP §2 定位）。

---

## 4. 分析核心的可调用契约

### 4.1 新增 `AnalysisCoreResult`（`orchestrator/contracts.py`）

把「一次分析核心运行」的产物定型成一个 stable 的返回契约，midterm 不依赖内部 node 细节：

```python
class AnalysisCoreResult(BaseModel):
    """一次「分析核心」运行的定型产物，供 midterm.update_belief 消费。"""
    run_id: str = ""
    symbol: str = ""
    as_of_date: str = ""                      # 分析基准日（报告期，非今天）
    generated_at: str = ""
    insight: InsightArtifact | None = None
    thesis: ThesisArtifact | None = None
    thesis_review: dict[str, Any] | None = None
    conflicts: ConflictAnalysisResult | None = None
    verification: VerificationArtifact | None = None
    decisions: list[Decision] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    fact_values: dict[str, float] = Field(default_factory=dict)
    degraded: bool = False                    # 洞察层降级（fallback_tier>0）等
    degradation_reason: str = ""
```

配套 `coerce_analysis_core(value) -> AnalysisCoreResult | None`，与既有 `coerce_*` 对齐。

### 4.2 新增入口 `run_analysis_core()`（`orchestrator/analysis_core.py`）

```python
async def run_analysis_core(
    symbol: str,
    query: str,
    *,
    prior_state: dict[str, Any] | None = None,   # 连续性注入（§5）
    enhance: bool = True,
    llm_review: bool = True,
    config: RunnableConfig | None = None,
) -> AnalysisCoreResult:
    """跑「分析核心」并返回定型产物，不生成报告。"""
```

实现方式（二选一，推荐 A）：

- **A（推荐，单图 + mode 路由）**：在 `agent.py` 的 `review_thesis` 后加条件边，
  `output_mode == "analysis"` 时 `review_thesis → END`（不跑报告尾巴），调用方直接读
  `state["artifacts"]` / `state["decisions"]` / `state["issues"]` 组装 `AnalysisCoreResult`。
  `output_mode` 默认 `"report"`，**向后兼容**，`main.py` 行为不变。
- **B（更轻量）**：单独编译一个 `analysis_core_agent = StateGraph(...).compile()` 到
  `review_thesis` 为止。缺点是两张图要同步维护，先不选。

```python
# agent.py（设计示意）
def route_after_thesis(state: OrchestratorState) -> str:
    return "generate_report" if state.get("output_mode") == "report" else "__end__"

_graph.add_conditional_edges(
    "review_thesis", route_after_thesis,
    {"generate_report": "generate_report", "__end__": END},
)
```

### 4.3 唯一的结构性改动清单（orchestrator 侧）

| # | 改动 | 类型 | 影响面 |
|---|---|---|---|
| 1 | `OrchestratorState` 增 `output_mode`（默认 `"report"`） | 加字段 | 向后兼容 |
| 2 | `OrchestratorState` 增 `prior_state`（可选，序列化 CompanyStateArtifact） | 加字段 | 向后兼容 |
| 3 | `review_thesis` 后加条件边 + `route_after_thesis` | 加路由 | 向后兼容 |
| 4 | 新增 `AnalysisCoreResult` + `coerce_analysis_core` + `run_analysis_core()` | 加契约 | 纯新增 |

> **没有任何既有 node 被重写**，没有删除任何能力。报告路径原样保留。

---

## 5. 连续性注入：prior_state → orchestrator（第二个结构点）

midterm 触发 re-analysis 时，如果只是「冷启动」重新分析一遍，会丢掉认知连续性（ROADMAP §2 的
第一价值）。因此 `run_analysis_core` 把上次 `CompanyStateArtifact` 序列化后作为 `prior_state` 注入：

| 注入到哪个节点 | 注入内容 | 目的 |
|---|---|---|
| synthesize_insights | 上次 thesis + main_driver + next_evidence_to_watch + open ResearchTask | 新观点锚定旧信念，而非重讲新故事 |
| explore_conflicts / verify_hypotheses | 上次的 open unknowns + 已验证冲突 | 优先验证「决定结论的不确定项」，验证计划有连续性 |
| run_thesis / review_thesis | 上次的 ExitCondition + confidence | 审查时对照「哪些证伪条件已逼近」 |

实现上只在 `OrchestratorState` 加一个可选 `prior_state`，各节点的 prompt 拼装处消费它，
缺失时按现状跑（不破坏单次分析）。

---

## 6. midterm.update_belief 调用契约与映射

### 6.1 数据流

```text
[midterm 触发] ──(event)──▶ trigger.py
                              │ 需要一次新鲜研究
                              ▼
                    run_analysis_core(symbol, query, prior_state=last_state)
                              │
                              ▼ AnalysisCoreResult
                    update_belief(prev_state, core_result, midterm_scores)
                              │
                              ▼
                    new CompanyStateArtifact + SnapshotDiff + (PositionDecision)
```

### 6.2 职责切分（谁算哪个变量）

```text
orchestrator（分析核心）负责：F（fundamental/财务趋势）、R（三层风险里的 FundamentalRisk/
  ThesisRisk 证据）、冲突/反证、Insight/Thesis 框架、Decision/Issue
midterm（自身引擎）负责：E（RevisionEngine）、T（RelativeStrengthEngine）、C（CrowdingEngine）、
  V（valuation percentile）、M（消费 market_regime）、贝叶斯后验、状态机、仓位
```

> 关键点：**E/T/C/V 是 midterm 自己的确定性引擎，orchestrator 不产这些**——因为一致预期、
> 相对强度、拥挤度是 midterm Phase 0-1 的新数据源（orchestrator 目前没有）。

### 6.3 orchestrator 产物 → EvidenceEvent / Belief 映射表

| orchestrator 产物 | midterm 消费 | 效果 |
|---|---|---|
| `InsightArtifact.core_view` / `main_driver` | `CompanyStateArtifact.thesis`（H） | 建立/刷新核心假设 |
| `InsightArtifact.what_would_change_my_mind` | `ExitCondition[]` | 证伪条件 |
| `InsightArtifact.supporting_evidence` | `EvidenceEvent(kind=fundamental, effect=confirming)` | P(H) 上调 |
| `InsightArtifact.counter_evidence` | `EvidenceEvent(effect=refuting)` | P(H) 下调 |
| `CONFLICTS_RESULT` 中 verified/partial 冲突 | `EvidenceEvent(effect=refuting)` | P(H) 下调（反证） |
| `CONFLICTS_RESULT` 中 rejected 假设 | `EvidenceEvent(effect=neutral)`（已排除记录） | 不调 P(H)，沉淀 decision |
| `THESIS_ANALYSIS` 维度 judgment 变化 | `VariableScores.f_fundamental_trend` | 基本面趋势差分 |
| `E` 引擎（midterm 自算）EPS revision 转正/转负 | `EvidenceEvent(kind=expectation, effect=confirming/refuting)` | **核心证据**，P(H) 显著调整 |

### 6.4 update_belief 伪契约

```python
def update_belief(
    prev: CompanyStateArtifact | None,
    core: AnalysisCoreResult,
    scores: VariableScores,          # midterm 引擎的 E/T/C/V 得分
) -> tuple[CompanyStateArtifact, SnapshotDiff]:
    evidence = derive_evidence(prev, core, scores)     # 上表映射
    posterior = bayes_update(prev.prior_confidence if prev else 0.55, evidence)
    next_state = classifier.transition(prev.state, evidence, scores)   # S0-S5 含回退
    diff = compute_snapshot_diff(prev, next_state, evidence)
    return next_state, diff
```

- **首次（prev=None）**：prior 取默认 0.55（或由 thesis confidence 初始化），仅进入 S1 试探仓。
- **贝叶斯更新**：log-odds，每条 `EvidenceEvent` 携带 `effect_on_thesis` + `confidence_delta`，
  更新可回放、可审计。
- **证伪优先**：verified conflict 或 `what_would_change_my_mind` 命中 → 直接触发 S5（thesis_broken），
  不等「-10% 止损」。

---

## 7. EventDriven 触发契约（谁调 run_analysis_core）

`trigger.py` 监听事件，判定「信息变化 → 是否投资状态变化」，只在需要时调 `run_analysis_core`：

```text
事件类型：财报/业绩预告披露、研报/评级变更、EPS Revision 突变、股价异常（RS/量异常）、
          关键未知（ResearchTask）的答案出现
```

- **去重/幂等**：`CompanyStateArtifact` 携带 `last_analysis_date` + `last_evidence_id` 水位线；
  同一 `(symbol, report_period)` 已分析且无新证据时跳过，输出「无变化」而非重新分析。
- **只推认知变化**：触发结果若 `state_from == state_to` 且无 evidence 变化 → 不提醒；
  仅当 S 阶段迁移、P(H) 越过阈值、或证伪条件命中时才推送（ROADMAP §2.2 能力 #2/#5）。

---

## 8. 反向注入：midterm state → orchestrator 报告（简要）

与上述「midterm 调 orchestrator」相反方向的集成，供人类读报告时带上决策上下文：

- 个股流水线新增 `resolve_midterm_state` 节点（MIDTERM_ROADMAP Phase 4.2）：读最近
  `CompanyStateArtifact`，把「当前 S 阶段 + 预期差 + 建议仓位 + 证伪条件」注入
  `generate_report` 的 payload（`ReportGenerationPayload` 新增 `midterm` 章节）。
- **默认不阻塞**：midterm state 缺失时该节点 skip，个股流水线照常产出纯研究报告。

两条方向都走 `alphabee/core/` 的 Artifact + `find_artifact_model`，不引入新的耦合面。

---

## 9. 失败与降级契约

| 场景 | midterm 行为 |
|---|---|
| `run_analysis_core` 抛异常/超时 | 保留旧 `CompanyStateArtifact`，记 `EvidenceEvent(kind=system, effect=neutral)` + Issue（`analysis_core_failure`），**不静默丢弃、不伪造更新** |
| `InsightArtifact.degraded`（fallback_tier>0） | 仍应用更新，但 `degraded=True`，贝叶斯更新置信度**阻尼**（降级观点当弱证据） |
| E/T/C/V 数据缺失 | 对应 `VariableScores` 项为 `None`，PositionScore 重归一化，显式 `*_missing` issue（MIDTERM_ROADMAP Phase 0/1 约定） |
| `prior_state` 缺失 | 冷启动，退化为单次分析（等价现状），不报错 |

---

## 10. 持久化与 replay

- `AnalysisCoreResult` 不必单独落盘——它由 run 的 `artifacts`（已持久化）+ `Decision`/`Issue`
  天然可重建；`run_analysis_core` 的 `run_id` 就是审计锚点。
- `CompanyStateArtifact` + `SnapshotDiff` + `PositionDecision` 落 `data/midterm/{symbol}.json`
  （版本化、latest-wins、人工可编辑）。
- **回放性**：`update_belief` 是确定性的（证据→log-odds→状态迁移），给定同一串
  `AnalysisCoreResult` + `VariableScores`，必得同一 `CompanyStateArtifact`——这是
  「可追溯/可审计」的工程保证。

---

## 11. 落地顺序（与 MIDTERM_ROADMAP Phase 对齐）

| 步骤 | 内容 | 对应 Phase | 风险 |
|---|---|---|---|
| 1 | 先不碰 orchestrator graph，midterm 做 E/T/C 数据 + 引擎 | Phase 0-1 | 零 |
| 2 | orchestrator 拆分：`AnalysisCoreResult` + `run_analysis_core` + `output_mode` 路由 + `prior_state` 字段 | 本文档 §4/§5 | 低（纯新增 + 向后兼容） |
| 3 | `update_belief` 消费 `run_analysis_core` 输出 → diff + 贝叶斯 + 状态机 | Phase 2 | 中 |
| 4 | 三层仓位 + Portfolio Allocator | Phase 3 | 中 |
| 5 | EventDriven trigger + Decision Journal + 三模块入口 + 反向注入 | Phase 4 | 中 |

---

## 12. 验收标准

1. **向后兼容**：`output_mode` 缺省时 `main.py` 产出与现状完全一致（报告路径不变）。
2. **可调用**：`run_analysis_core(symbol, query)` 不生成报告、返回 `AnalysisCoreResult`，
   `INSIGHT_ANALYSIS` / `THESIS_ANALYSIS` / `THESIS_REVIEW` / `CONFLICTS_RESULT` 齐备。
3. **可回放**：同一 `AnalysisCoreResult` + `VariableScores` → 同一 `CompanyStateArtifact`（确定性）。
4. **连续性**：`prior_state` 注入后，`synthesize_insights` 输出的 `central_tension` 显式引用
   上次 thesis 的延续/转折（不冷启动重讲）。
5. **降级**：核心失败/洞察降级时不丢状态、不伪造更新，留 `Issue` 与 `degraded` 标记。
6. **去重**：同一报告期无新证据时 trigger 跳过，不重复跑分析。

---

## 13. 一句话总结

**orchestrator 不是被替换，而是被「定位」：把它的研究内核抽成一个可调用的
`run_analysis_core()`，报告降为可选的渲染尾巴；midterm 站在它上面做跨 run 的
认知状态、预期差、仓位与复盘。改动是「加一个 mode、加一个 prior_state、加一个返回契约」，
不重写任何既有 node。**
