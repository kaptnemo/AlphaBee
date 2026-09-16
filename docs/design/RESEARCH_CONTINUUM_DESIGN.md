# AlphaBee Research Continuum 设计（Temporal Long-Horizon）

> **状态：📋 设计提案（未实施）**。本文档是 `DEVIATION_CONTROL_FRAMEWORK.md` 的姊妹篇：前者解决"单次 run 内如何控制偏离"，本文档解决"跨时间尺度如何维持一个持续演化的研究对象"——即 **Session Long-Horizon 之上的 Temporal Long-Horizon**。
> 核心结论先行：**AlphaBee 的 Persistent Research Object 不需要从零设计——数据契约与引擎已基本建成，真正的缺口是"建好未接线"（wiring gap）+ 生命周期 envelope + 事件驱动 + 恢复前对账（State Reconciliation）。**
> 关联文档：`docs/midterm/MIDTERM_STATE_DIFF_DESIGN.md`、`docs/midterm/MIDTERM_DECISION_MODEL.md`、`docs/midterm/MIDTERM_INVESTMENT_ROADMAP.md`、`docs/roadmap/COMPANY_TRACK_ROADMAP.md`、`docs/design/DEVIATION_CONTROL_FRAMEWORK.md`。
> 实现契约改动时必须走 `alphabee-pipeline-contract-steward` / `alphabee-schema-steward` skill 流程。

---

## 1. 问题定义：Session Long-Horizon ≠ Temporal Long-Horizon

普通 DeepResearch 解决的是 **Session Long-Horizon**：

```text
Start → Research 1h → 500 tool calls → Report → End
```

AlphaBee 真正要解决的是 **Temporal Long-Horizon**：

```text
2026-09-01 建立宁德时代 Thesis
    → 2026-09-15 行业价格变化 → 更新 Evidence
    → 2026-10-20 三季报 → 重新验证 Thesis
    → 2026-12 海外政策变化 → 更新风险概率
    → 2027-03 年报 → 重新估值
```

Horizon 不是 500 次工具调用，而是 **6 个月甚至 3 年**。二者不是程度差异而是类别差异：

| | Session Long-Horizon | Temporal Long-Horizon |
|---|---|---|
| 状态 | 单个 run 的 in-memory state | 跨 run 的持久化状态 |
| 偏离 | 一次执行内的轨迹偏离（δ_t = D(s_t, S*_t)） | 跨时间的信念/前提漂移（thesis 与证据的持续性） |
| 失败模式 | 第 15 步无法恢复 | 半年后不知道"我为什么相信它、哪些预测被证伪了" |
| 关键机制 | 检测器 + 恢复阶梯 + 回环 | **持久化 + 事件驱动 + 对账（reconciliation）+ 贝叶斯更新** |

由此引出 AlphaBee 的定位：**不是 Research Agent Model，而是 Investment Research Operating System**——DeepResearch（MiroThinker / Tongyi / Claude 等）是其中的 L2 Research Engine，AlphaBee 的壁垒在 L3+L4+L5（持久研究状态 + thesis 演化 + 决策层）。

---

## 2. 五层架构 → 现有代码映射（逐层核实）

> 状态标记：✅ 已实现　🟡 部分（引擎在/接线缺）　⬜ 未实现

### L1 信息与事件层 ✅（引擎在，部分未接线）

| 组件 | 代码位置 | 状态 |
|---|---|---|
| 行情/财务/行业数据采集 | `alphabee/collectors/`（tushare/akshare/baostock/eastmoney/consensus/market_regime） | ✅ 主链在用 |
| 业绩预告/快报（数值证据源） | `alphabee/agents/facts/tools/expectation_fact.py` | ✅ `collect_evidence` 在用 |
| 盈利预测修正（consensus） | `alphabee/collectors/consensus/eastmoney.py` `build_consensus` | ✅ `collect_evidence` 在用 |
| **财报/研报 PDF 全文管线**（巨潮财报 + 东财研报 → OCR → 章节解析 → 受限 agent 问答） | `alphabee/financial_report/`（`links.py`/`pipeline.py`/`fetch_deepagents.py`/`report_parser.py`） | 🟡 **零接线**：orchestrator 与 main.py 均无引用 |
| 新闻摘要 | `alphabee/tools/news.py` | ✅ `framework_monitor` 在用，主链未用 |
| 数据失败库（事件→指纹→问题单→修复任务） | `alphabee/data_fetch/`（`data/fetch_events.db` 已在落数据） | ✅ 在用 |

### L2 Deep Research Engine 🟡（功能在，抽象不在）

- 当前实现：FactCollectorAgent（8 tools + web_search）、`explore_conflicts` / `verify_hypotheses`（DeepAgents）、`synthesize_insights`，全部 `create_deep_agent` 内嵌于主图；
- **缺口**：没有 ResearchEngine 协议边界——DeepAgents 与编排层是直接依赖关系，未来换 MiroThinker / MiroFlow / Tongyi 需要定义接口（§9）。

### L3 持久研究状态 🟡（契约✅ 引擎✅ **持久化零接线**）

| 组件 | 代码位置 | 状态 |
|---|---|---|
| 核心状态契约：thesis H、prior/posterior、`StateBelief`（软状态+entropy+drift）、`evidence_log`、`next_evidence_to_watch`、`exit_conditions`、`position`、`stale_after`、`degraded` | `alphabee/midterm/models.py` `CompanyStateArtifact` | ✅ 已实现 |
| 五层差分契约 + 归因链 | 同文件 `CompanyStateDiff`/`ChangeAttribution`/`StateShift` 等 | ✅ 已实现 |
| **append-only 持久化**（`data/midterm/state/<symbol>.jsonl`，幂等追加、反漂移） | `alphabee/midterm/persistence.py` `append_artifact`/`load_artifacts`/`latest_artifact` | 🟡 **零调用点**：全仓库仅 `midterm/__init__.py` 导出，没有任何生产代码写入 |
| run 执行历史（每 run 一份：stage timing、signals、verdicts、issues） | `alphabee/task_records/`（`TaskStore` 按 symbol/date 目录落 JSON） | ✅ CLI streaming 已接 `TaskRecorder`/`TaskStore` |
| Research Object 生命周期 envelope | 无 | ⬜ 本设计 §5–§6 |

### L4 Thesis 演化引擎 🟡（引擎✅ **入口缺**）

| 组件 | 代码位置 | 状态 |
|---|---|---|
| 贝叶斯更新（log-odds、反证不对称权重 `_REFUTING_WEIGHT=1.2`、后验 clamp 0.05–0.95、场景概率） | `alphabee/midterm/bayes.py` | ✅ 已实现 |
| 五层 diff 引擎（字段差→评分差→状态漂移 tv_distance/entropy→置信度差→赔率差→仓位差 + 归因 + 退出检查 + `diff_series` 速度序列） | `alphabee/midterm/diff.py` | ✅ 已实现，**零调用点** |
| 数值证据规则（beat/miss 预告口径、revision 幅度、符号退化） | `alphabee/midterm/evidence_rules.py` | ✅ `collect_evidence` 在用 |
| 定性证据抽取（Stage A/B，LLM 带降级） | `alphabee/midterm/evidence_extractor.py` | ✅ 在用，但**输入窗口为空** |
| insight 证据适配（纯规则映射） | `alphabee/midterm/insight_evidence_adapter.py` | ✅ 在用 |
| thesis 版本管理（append-only 反重写） | `alphabee/midterm/models.py` `ThesisVersion` | 🟡 模型在，**无写入/读取逻辑** |
| 主链接线节点（flag 门控） | `alphabee/orchestrator/nodes/midterm.py` `resolve_midterm_decision` | ✅ 已接图 |

**关键缺口（代码自证）**：`nodes/midterm.py:160-168` 的 `_window_texts()` 注释写明：

> "原 fact_text 组件被移除：主链的 FACT_COLLECTION.raw_response 是叙事摘要而非财报/公告/研报原文……**当前主链不提供真正原文**，故该组件置空；若未来主链能提供原文，再以显式原文字段补回。"

而 `financial_report/pipeline.py` 恰好能产出这份原文（财报 PDF → OCR → 章节结构）——**两个已建成的模块之间缺一条连线**。

### L5 投资决策层 🟡（引擎✅ 接线部分）

| 组件 | 代码位置 | 状态 |
|---|---|---|
| S0–S5 状态机 + 合法迁移判定 | `alphabee/midterm/classifier.py` | ✅ |
| 七因子评分（F/E/T/V/C/R/M 0-100 + 方向分） | `alphabee/midterm/score_engine.py` | ✅ |
| 三层仓位决策 | `alphabee/midterm/position.py` | ✅ |
| 赔率/期望值 | `alphabee/midterm/models.py` `ExpectedValue` + `decision_model.get_decision` | ✅ |
| 决策摘要渲染（只进日志/载荷，不进报告） | `alphabee/orchestrator/nodes/midterm_reporter.py` | ✅ |
| 跨 run 决策历史（读取历史仓位/状态变化） | 依赖 `persistence.py` | 🟡 被持久化零接线卡住 |
| 行动类输出 gate（只允许 Tier 0 通过 / Tier 5 升级人工） | 无 | ⬜ `DEVIATION_CONTROL_FRAMEWORK.md` §9.4 |

### 横向：现有 temporal 循环原型

`alphabee/workflow/framework_monitor.py`（CLI `--monitor-framework`，✅ 已接线）：快照对比式监控（fundamentals/market/news → 阶段判断 + 告警 `new/ongoing/resolved` + JSON 快照落盘）。它是**市场监控层**的 temporal 循环原型，但快照里没有 thesis 后验、没有 evidence_log、没有 exit_conditions——与 midterm 状态机互不相通。本设计把它定位为 L1→L3 的一个事件源，而非独立循环。

---

## 3. 核心发现："建好未接线"清单（wiring gap）

这是本次代码核实的最大发现。Temporal Long-Horizon 的资产几乎都已建成，但**六条关键连线缺失**：

| # | 已建成 | 缺失的连线 | 接线成本 |
|---|---|---|---|
| W1 | `midterm/persistence.py` `append_artifact`（append-only JSONL） | `resolve_midterm_decision` 产出 artifact 后调一次 `append_artifact` | 1 行 + 测试 |
| W2 | `midterm/diff.py` 五层差分 + `diff_series` | 任何消费方（reconciliation / scheduler） | 复用纯函数 |
| W3 | `financial_report/`（财报原文管线） | `_window_texts()` 补回原文输入（OCR 章节文本 → `collect_evidence` 的 window） | 1 个适配函数 |
| W4 | `stale_after` / `exit_conditions` / `next_evidence_to_watch` 字段 | 调度器触发（§8） | 新模块 |
| W5 | `ThesisVersion`（append-only） | thesis 变更时的版本写入 + 读取 | 2 个函数 |
| W6 | `framework_monitor` 告警（new/ongoing/resolved） | 告警 → 触发 reconciliation（而非仅展示） | 1 个路由 |

**推论**：AlphaBee 从 Session Long-Horizon 到 Temporal Long-Horizon 的升级，主体不是"写新引擎"，而是"接线 + 补 envelope"。这大幅降低实施风险（引擎均为确定性纯函数，可独立测试）。

---

## 4. Persistent Research Object：组合而非重造

用户提出的 Research Object（Thesis / Evidence Graph / Industry Model / Financial Model / Valuation / Expectations / Risks / Catalysts / Open Questions / Research History / Watch Conditions）**在现有 typed contracts 中几乎全部已有对应物**：

| RO 字段 | 现有契约 | 承载位置 |
|---|---|---|
| Thesis | `thesis`（H 文本）+ `thesis_confidence`（后验）+ `prior_confidence` | `CompanyStateArtifact` |
| Evidence Graph | `evidence_log`（EvidenceEvent 列表）+ `Decision.evidence_refs/based_on` | `CompanyStateArtifact` + `core/schemas.py` |
| Industry Model | `INDUSTRY_CONTEXT` + `DRIVER_PROFILE` artifacts | `alphabee/industry/`、`alphabee/domain_context/` |
| Financial Model | `FactorSnapshot`（七因子 F/E/T/V/C/R/M） | `CompanyStateArtifact.factor_snapshot` |
| Valuation | `ExpectedValue`（EV/RiskAdjustedEV/三情景概率）+ V 因子 | `CompanyStateArtifact.expected_value` |
| Expectations | `ExpectationGap` + consensus revisions（`build_consensus`） | `CompanyStateArtifact` + `collectors/consensus/` |
| Risks | Issues（D1–D5 偏离分类，`DEVIATION_CONTROL_FRAMEWORK.md` §4）+ `exit_conditions` | `core/schemas.py` + RO |
| Catalysts | `next_evidence_to_watch` | `CompanyStateArtifact` |
| Open Questions | verification `unknown` 假设 + 未探索区域（偏离账本 D4） | `conflicts_result` + 账本 |
| Research History | midterm JSONL 帧序列 + `task_records` | `data/midterm/state/` + `data/task_records/` |
| Watch Conditions | `exit_conditions` + `what_would_change_my_mind`（证伪条件）+ `stale_after` | `CompanyStateArtifact` + `InsightArtifact` |

**结论**：Persistent Research Object = `CompanyStateArtifact` 帧序列（append-only JSONL）+ 11 个字段的组合映射 + **一个尚未存在的生命周期 envelope**。缺的 envelope 是：

```python
# alphabee/tracking/contracts.py（新增，v1 最小）
class ResearchObjectStatus(enum.StrEnum):
    ACTIVE = "active"            # 复核 run 执行中
    WAITING = "waiting"          # 无触发条件，等待下一事件
    NEEDS_RESEARCH = "needs_research"   # 有到期 watch condition / 数据陈旧
    THESIS_CHANGED = "thesis_changed"   # 信念漂移超阈值，需重新研究
    RISK_ALERT = "risk_alert"    # 出现 critical 级偏离/风险信号
    INVALIDATED = "invalidated"  # thesis_broken / alpha_exhausted

class ResearchObjectEnvelope(BaseModel):
    symbol: str
    status: ResearchObjectStatus
    latest_artifact_id: str      # 最新 CompanyStateArtifact 持久化 id（symbol:as_of_date）
    last_reconciled_at: str | None
    pending_triggers: list[str]  # 未消费的触发条件（stale / exit_met / drift / 外部事件）
    open_questions: list[str]    # 未探索区域摘要（引用账本 D4 记录）
```

RO 永远不是 Finished——状态机在 §5。

---

## 5. 生命周期状态机：与 S0–S5 正交

**关键设计决策**：Research Object 生命周期（ACTIVE/WAITING/NEEDS_RESEARCH/THESIS_CHANGED/RISK_ALERT/INVALIDATED）与 S0–S5 认知状态是**两个正交轴**：

- S0–S5 是**仓位生命周期**（认知阶段 → 持仓动作），由 `classifier.py` 判定；
- RO 状态是**研究生命周期**（这份研究现在该做什么），由本设计的 envelope 判定。

一个标的可以 `S3_CONSENSUS + WAITING`（核心持有、无新事件）或 `S2_CONFIRM + THESIS_CHANGED`（突发反向证据，需要复核）。混淆两轴会把"该不该调仓"和"该不该重新研究"耦合在一起。

### 5.1 转移规则（全部确定性，复用 diff/bayes 引擎）

| 转移 | 触发条件（可计算） | 引擎 |
|---|---|---|
| → INVALIDATED | `diff.exit_conditions_met` 为真（`CompanyStateDiff` 已有该字段） | `diff.py` |
| → THESIS_CHANGED | 新增 evidence 后 \|Δlog-odds\| > θ_c，或 `StateShift.tv_distance` > θ_d，或 `entropy_delta` > θ_e | `bayes.py` + `diff.py` |
| → NEEDS_RESEARCH | `stale_after` 到期，或 `next_evidence_to_watch` 中任一条到期且未消费 | envelope + `stale_after` |
| → RISK_ALERT | 新帧出现 critical 级 Issue（D1–D5 分类账本）或 `framework_monitor` 产生 high 告警 | 账本 + monitor |
| → ACTIVE | 复核 run 启动（reconciliation 路由为 resume） | 调度 |
| → WAITING | 对账完成、无 pending trigger | 对账收尾 |

### 5.2 阈值进配置

θ_c / θ_d / θ_e 进 `config.yaml`（沿用 `bayes.py` 顶部常量集中、回测可调参的既有纪律）。v1 给保守初值：θ_c=log-odds 0.7（约 2:1 证据比）、θ_d=0.3、θ_e=+0.3。

---

## 6. State Reconciliation：恢复挂起研究前的对账协议

> 这是用户指出的最关键机制：**Agent 每次恢复一个挂起 N 天的研究，不允许直接继续，必须先跑对账（Temporal Deviation Correction）**。挂起期间世界变了，直接继续等于在过期状态上叠加新偏离。

### 6.1 用户七问 → 代码映射

| # | 对账问题 | 代码实现 |
|---|---|---|
| 1 | 上次研究状态是什么？ | `persistence.load_artifacts(symbol)` + `latest_artifact` + `task_records` 最近 run |
| 2 | 期间发生了什么？ | `diff.py`(prev, curr) + `financial_report.links`（期间新公告/财报）+ consensus revisions + news |
| 3 | 哪些事实已经过期？ | `ObservationFreshness` 检查 + `stale_after` 判定 |
| 4 | 哪些 assumptions 失效？ | 假设登记簿（`DEVIATION_CONTROL_FRAMEWORK.md` §6.3）+ verification 状态复核 |
| 5 | 哪些 predictions 已经可以验证？ | 逐条核对 `next_evidence_to_watch` 与 `what_would_change_my_mind`：能判 → 验证；不能判 → 保留 |
| 6 | 有没有反向证据？ | 新证据入 `evidence_log`（**反证强制入账**：`bayes._REFUTING_WEIGHT=1.2` 已在，缺的是"每帧必须同时收集支持与反对证据"的收集纪律） |
| 7 | 当前 thesis 是否仍成立？ | `exit_conditions_met` + 贝叶斯后验更新 + `ThesisVersion` 比对 |

### 6.2 节点设计

新增 `alphabee/tracking/reconcile.py`（确定性核心 + 可选 LLM 叙事）：

```text
reconcile(symbol, as_of_date)
  1. load: latest_artifact + task_records 最近 run            # 问 1
  2. diff: CompanyStateDiff(prev=latest, curr=新采集帧)        # 问 2（含 exit_conditions_met）
  3. freshness: stale_after / ObservationFreshness             # 问 3
  4. assumptions: 登记簿 active 项 × 新证据比对                  # 问 4
  5. predictions: next_evidence_to_watch 逐条判定 verified/rejected/pending  # 问 5
  6. evidence: 新 EvidenceEvent 入账（含强制反证收集）             # 问 6
  7. thesis: bayes 更新 P(H|E) + ThesisVersion 比对              # 问 7
  → 产出 ReconciliationReport artifact（新 ArtifactType 注册）
  → 路由：
      INVALIDATED   → 停止研究，产出 thesis_broken 信号（escalate）
      THESIS_CHANGED → 触发一次完整复核 run（走主图流水线）
      NEEDS_RESEARCH → 触发定向研究（只研究 pending 问题）
      WAITING       → 只更新 envelope，不重跑研究
```

**对账纪律**（继承既有降级纪律）：对账是纯规则 + 可选 LLM 叙事；任何环节失败 → 显式 `reconciliation_degraded` issue（D5），**不允许在未对账状态下继续研究**（宁可 WAITING + 告警，不可带过期状态推进）。

### 6.3 与主图的关系

对账路由到"复核 run"时，**复用现有主图**（`alphabee_agent`），只注入对账上下文：把 `CompanyStateArtifact`（thesis/evidence_log/next_evidence_to_watch）作为 `run.context` 传入，让 `synthesize_insights` / `run_thesis` 在既有信念基础上工作，而不是从零研究。v1 不做主图改动，仅靠 context 注入。

---

## 7. 事件驱动层：从 CLI 到调度

### 7.1 v1：手动触发（零新基础设施）

CLI 新增 `--reconcile <symbol>`（沿用 `main.py` 现有分派模式，与 `--monitor-framework` 平级）：

```text
poetry run python main.py --reconcile 300750
```

内部跑 `reconcile()` + 渲染 ReconciliationReport（复用 `framework_monitor` 的 `render_*` 风格）。

### 7.2 v2：轻量调度器（非 daemon 起步）

`alphabee/tracking/scheduler.py`：**扫描式而非常驻**（cron / CI 定时调用 `alphabee track sweep`）：

```text
sweep()
  for each symbol with envelope:
    triggers = []
    if stale_after 到期: triggers.append("stale")
    if next_evidence_to_watch 有到期项: triggers.append("watch_due")
    if 新公告/财报出现（financial_report.links 增量）: triggers.append("new_report")
    if framework_monitor 产生 high 告警: triggers.append("risk_alert")   # W6 接线
    if triggers: → 触发 reconcile(symbol)（§6 路由）
```

事件源清单（全部已有）：财报公告（`financial_report.links`，巨潮+东财）、业绩预告/快报（`expectation_fact`）、盈利预测修正（`build_consensus`）、行情异动（`collectors/market_regime`）、新闻（`tools/news`）。

---

## 8. L2 Research Engine 抽象

AlphaBee 作为 Research OS，DeepResearch 是**可替换引擎**。v1 最小抽象：

```python
# alphabee/tracking/engine.py（新增）
class ResearchEngine(Protocol):
    """L2 引擎协议：输入 RO 上下文，输出结构化研究产物。"""
    async def run(self, context: ResearchContext) -> ResearchOutput: ...
```

- 当前实现 = 主图深研节点（`collect_raw_facts` + `explore_conflicts` + `verify_hypotheses` + `synthesize_insights`）；
- 替换点 = 只换 engine 实现，L3/L4/L5 不变（这正对应"AlphaBee 的壁垒在 L3+L4+L5"的定位）；
- 不在 v1 强制重构成插件系统——先定义协议与当前适配器，等真实接入 MiroThinker/MiroFlow 时再演化。

---

## 9. 与偏离控制框架的横向关系

`DEVIATION_CONTROL_FRAMEWORK.md` 的 D1–D5 分类在时间轴上各有一个对应的**Temporal Drift**形态（用户横向层的落地）：

| 横向层 | 微观环对应（单 run） | 宏观环对应（本设计） |
|---|---|---|
| Goal Drift | 节点偏离任务目标 | RO 忘记原始研究问题 → 对账问 1/7 + `ThesisVersion` |
| Evidence Drift | 证据链断裂（D3） | 证据选择性吸收 → 反证强制入账 + `_REFUTING_WEIGHT` |
| Thesis Drift | 论点矛盾（`thesis_conflict`） | 行情/叙事重写买入理由 → `ThesisVersion` append-only |
| State Drift | 上下文截断/忘记约束（D4） | 过期状态上继续研究 → **对账前置（§6）** |
| Temporal Drift | —（本类不存在） | 数据陈旧/预测到期未核对 → `stale_after` + 调度（§7） |
| Confirmation Bias | 验证阶段假设驱动搜索 | 只收集支持证据 → 强制反证收集纪律 |

对账协议（§6）因此可以精确表述为：**在宏观环的每个恢复点，运行一次横向五类 Temporal Drift 的检测 + 修正**——即用户所说的 Temporal Deviation Correction。

---

## 10. 分期实施（T0–T4，与 F 期并行）

> 依赖关系：T0 → T1 → T2/T3；T4 收尾。T 期不依赖 `DEVIATION_CONTROL_FRAMEWORK.md` 的 F 期，但 T1 的假设登记簿与 F 文档 §6.3 是同一组件，需协调落地顺序（F 先做则 T1 复用）。

### T0：接线持久化 + envelope 契约（约 0.5 天）

- `resolve_midterm_decision` 成功后调用 `append_artifact`（W1）；
- 新增 `alphabee/tracking/contracts.py`：`ResearchObjectStatus` + `ResearchObjectEnvelope`；
- 测试：幂等追加、读回排序、envelope 序列化。

### T1：对账协议 + CLI 入口（约 2 天）

- `alphabee/tracking/reconcile.py`：§6.2 七步对账（复用 `diff.py`/`bayes.py`/`persistence.py`，零新引擎）；
- 新 `ArtifactType.RECONCILIATION_REPORT` 注册（`core/schemas.py`，走 steward 流程）；
- CLI `--reconcile <symbol>` + 渲染；
- 测试：四类路由（INVALIDATED/THESIS_CHANGED/NEEDS_RESEARCH/WAITING）各一组假帧序列；对账失败 → 拒绝继续研究的断言。

### T2：事件层接线（约 2 天）

- W3：`financial_report` 财报原文 → `_window_texts()`（OCR 章节文本做窗口，`nodes/midterm.py` 注释的"未来主链能提供原文"兑现）；
- `ThesisVersion` 写入/读取逻辑（W5）；
- 反证强制入账纪律：对账第 6 步同时收集 supporting + refuting 证据（`bayes` 已有不对称权重，只补收集端）；
- 测试：含真实财报样本的 window 抽取用例（有 `data/eastmoney_reports/` 现成数据）。

### T3：调度器 v1 + 状态机落地（约 2 天）

- `alphabee/tracking/scheduler.py`：`sweep()` 触发规则（§7.2）+ W4/W6 接线；
- envelope 状态转移规则落地（§5.1，阈值进 `config.yaml`）；
- CLI `--track-status <symbol>` 查看 RO 状态；
- 测试：stale 触发、watch 到期触发、告警触发路由；状态机转移表全覆盖。

### T4：引擎协议 + 集成收尾（约 2–3 天）

- L2 `ResearchEngine` 协议 + 当前实现适配器（§8）；
- 复核 run 的 context 注入（§6.3：RO 上下文进主图 run）；
- 与 `DEVIATION_CONTROL_FRAMEWORK.md` 账本打通：对账产出写 D1–D5 偏离账本；
- 文档：`docs/roadmap/ROADMAP.md` 新增"Temporal Long-Horizon"状态行。

**合计约 8.5–9.5 个工作日**，且每期独立可交付。

---

## 11. 验收标准

1. `--midterm` 跑完的每个标的，`data/midterm/state/<symbol>.jsonl` 都有对应帧（W1 闭环）；
2. `--reconcile <symbol>` 对挂起研究先对账后研究：过期状态下的"直接继续"在协议上不可能（对账失败 → 拒绝继续）；
3. 期间出现的新财报被 OCR 原文窗口接进证据抽取（`_window_texts` 非空，`collect_evidence` 产出定性证据）；
4. thesis 被证伪（exit_conditions_met）必然产生 INVALIDATED + 人工可见的 thesis_broken 信号，且不自动产生任何仓位动作；
5. 反证强制入账：每帧 evidence 入账日志同时包含 supporting 与 refuting 的收集尝试记录；
6. 六条 wiring gap（W1–W6）全部闭合，且各期单测 + 回归全绿。

---

## 12. 风险与边界

| 风险 | 缓解 |
|---|---|
| 对账过度触发复核 run（成本） | 阈值进 config 且回测；v1 复核 run 也有自身预算（主图现有限额） |
| OCR 原文窗口污染证据抽取（长文本噪声） | 窗口只取"管理层讨论/经营情况/风险章节"摘要（`report_parser` 已有章节树，可按章节裁剪） |
| envelope 与 S0–S5 语义混淆 | 双轴显式建模（§5），任何输出同时标注两轴 |
| 持久化数据损坏（JSONL 手工编辑） | append-only + `drop_artifact` 逃生口（既有设计）；损坏行跳过（`_read_rows` 已有容错） |
| 调度器被误用于自动交易 | 框架红线：行动类输出永不自动执行（`DEVIATION_CONTROL_FRAMEWORK.md` §9.4 沿用） |
| 与 F 期账本重复建设 | 账本（F0）与 envelope（T0）分属"偏离记录"与"研究对象"两个对象，只在 T4 打通 |

---

## 13. 一句话总结

> AlphaBee 的 Temporal Long-Horizon 不是"做一个更深的搜索 Agent"，而是把**已经建成但未接线的五层引擎**（L1 财报原文、L3 append-only 状态、L4 diff/bayes、L5 决策）接成一个**事件驱动、恢复必先对账、thesis 版本防漂移、行动永不自动**的 Research Continuum——研究对象从"生成报告"变成"半年后仍然知道自己为什么相信、哪些预测被证伪、下一步该验证什么"。
