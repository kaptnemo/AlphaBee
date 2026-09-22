# AlphaBee Research Continuum 设计（Temporal Long-Horizon）

> **状态：🟡 部分实施（2026-09 核实）**。本文档是 `DEVIATION_CONTROL_FRAMEWORK.md` 的姊妹篇：前者解决"单次 run 内如何控制偏离"，本文档解决"跨时间尺度如何维持一个持续演化的研究对象"——即 **Session Long-Horizon 之上的 Temporal Long-Horizon**。
> **实施对齐摘要（逐条核实见 §14）**：本文档 §7 的"一次性 reconcile + 触发判定 + 入口"已作为**框架文档 F4 期**落地于 `alphabee/tracking/`（提交 `d65164f`，7 路径 2581 行，经第三方独立认证），并与框架 §9.4 行动红线、§9.2 反证强制入账一并交付；§3 的 W2/W4 已闭合，W1 仅闭合 tracking 路径。**仍未实现**：§6.2 对账后的路由（触发复核 run / 定向研究）、§8 L2 引擎协议、W3（财报原文进证据窗口）、W5（`ThesisVersion` 读写）；§5 研究对象生命周期轴已于 **§14.3 决议为不建**（降级为可选派生视图）。
> **三项分歧点已决议（2026-09，见 §14.3；本期只写文档、不动代码）**：**D1** 对账产物维持 `TrackingReport`、**不注册新 ArtifactType**，但 tracking 观测到的偏离**写入账本**（只接事件层，不造 run 形状指标）；**D2** **不自动触发主图**，改为**入口前置校验 + 告警只读消费者**（自动触发的前置条件与顺序见 §14.3）；**D3** **不建独立研究状态轴**，如需单一状态字则实现为**派生投影**（单一真源 = `check_exit` + `monitor_triggers` + `detect_triggers`），§5 不再计入缺口。决议后的剩余工作与量级见 **§14.4（约 6–7 天）**。
> 核心结论先行：**AlphaBee 的 Persistent Research Object 不需要从零设计——数据契约与引擎已基本建成，真正的缺口是"建好未接线"（wiring gap）+ 生命周期 envelope + 事件驱动 + 恢复前对账（State Reconciliation）。**（该结论经 F4 落地验证：实现确实以"复用 midterm 引擎"为主，未新写引擎。）
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
| Research Object 生命周期视图 | 无持久化 envelope；**已决议不建**（§14.3 D3），如需单一状态字则做派生投影 | ⬜→**已决议** |

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

这是本次代码核实的最大发现。Temporal Long-Horizon 的资产几乎都已建成，但**六条关键连线缺失**（下表"现状"列为 2026-09 二次核实结果）：

| # | 已建成 | 缺失的连线 | 现状（2026-09） | 证据 |
|---|---|---|---|---|
| W1 | `midterm/persistence.py` `append_artifact`（append-only JSONL） | `resolve_midterm_decision` 产出 artifact 后调一次 `append_artifact` | 🟡 **仅闭合 tracking 路径** | `tracking/scheduler.py` 的 `reconcile()` 内调 `persistence.append_artifact(curr, state_dir)`；**普通 `--midterm` run 仍不落帧**（`nodes/midterm.py` 无调用） |
| W2 | `midterm/diff.py` 五层差分 + `diff_series` | 任何消费方（reconciliation / scheduler） | ✅ **已闭合** | `scheduler.py` `midterm_diff(prev, curr)` + `diff_consumers.check_exit` / `monitor_triggers` |
| W3 | `financial_report/`（财报原文管线） | `_window_texts()` 补回原文输入（OCR 章节文本 → `collect_evidence` 的 window） | ⬜ **未闭合** | `nodes/midterm.py` `_window_texts()` 仍是 `_conflict_explanations(artifacts) or None`，注释原文未变（"当前主链不提供真正原文"） |
| W4 | `stale_after` / `exit_conditions` / `next_evidence_to_watch` 字段 | 调度器触发 | ✅ **已闭合** | `tracking/triggers.py` `STALE_EXPIRED` 触发 + `scheduler.py` 从上一帧继承 `exit_conditions`/`next_evidence_to_watch`/`stale_after`（`_stale_after`，缺省 7 天可配） |
| W5 | `ThesisVersion`（append-only） | thesis 变更时的版本写入 + 读取 | ⬜ **未闭合** | 全仓仍只有 `midterm/models.py` 定义与 `midterm/__init__.py` 导出，无写入/读取逻辑 |
| W6 | `framework_monitor` 告警（new/ongoing/resolved） | 告警 → 触发 reconciliation（而非仅展示） | 🟡 **间接闭合** | `triggers.py` 的 `MANUAL` 类承接 `monitor_triggers`（payload 自报 `source="monitor_triggers"`）；但 `framework_monitor` 本体与 `tracking` 之间**无直接调用** |

**推论**：AlphaBee 从 Session Long-Horizon 到 Temporal Long-Horizon 的升级，主体不是"写新引擎"，而是"接线 + 补 envelope"。这大幅降低实施风险（引擎均为确定性纯函数，可独立测试）。**该推论已被 F4 的实现方式证实**：`tracking/` 的 docstring 明写"复用而非重写"，六项既有资产（`factors.get_factor_snapshot` / `decision_model.evaluate` / `diff.diff` / `diff_consumers.check_exit` / `diff_consumers.monitor_triggers` / `persistence.*`）全部按模块属性调用，未新写引擎。

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

**结论**：Persistent Research Object = `CompanyStateArtifact` 帧序列（append-only JSONL）+ 11 个字段的组合映射 + **一个生命周期视图**。

> **决议（2026-09，§14.3 D3）**：**不落地**下列 `ResearchObjectStatus` / `ResearchObjectEnvelope` 持久化契约——四个语义状态已全部有单一真源投影（`check_exit` / `monitor_triggers` / `detect_triggers`），再建一份即是"第三份同类判定"，与本仓库 F4-L2 的既有决策相悖。以下代码块**保留为设计记录**；若将来确需**单一状态字**，实现为**纯派生函数**（无持久化、无新阈值，复用 `monitor_triggers` 既有常量 0.3 / 0.5 与 `stale_after`），其字段映射见 §14.3 D3 的"落地形状"。

```python
# 【已决议不落地，仅作设计记录】alphabee/tracking/contracts.py（原 v1 最小方案）
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

RO 永远不是 Finished——状态视图的**派生口径**见 §5（已降级为可选增强）与 §14.3 D3。

---

## 5. 生命周期状态机：与 S0–S5 正交 ——【已决议：可选增强，不作为缺口】

> **决议（2026-09，§14.3 D3）：不建独立的持久化研究状态轴。** 本节整体**降级为可选增强**，**不再计入剩余工作缺口清单**。若将来确需一个单一状态字展示，实现为**纯派生投影**（单一真源 = `check_exit` + `monitor_triggers` + `detect_triggers`，复用既有阈值），而非本节所画的独立状态机。
>
> **决议依据**：RO 想表达的四个**语义**状态已全部有单一真源（INVALIDATED ← `check_exit`；THESIS_CHANGED ← `monitor_triggers` 的 `tv_distance>0.3`；NEEDS_RESEARCH ← `detect_triggers` 的 `STALE_EXPIRED` 等；RISK_ALERT ← `monitor_reasons`/`exit_reasons`），ACTIVE/WAITING 属调度器关注点；且本仓库已在框架顺延项 F4-L2 就同型问题决策"**不新增第二份判定**"。**被否决**：建独立持久化轴（C1）——会与既有投影形成双份判定并重复维护阈值。
>
> **原实施状态（2026-09 核实）**：全仓 `grep` `ResearchObjectStatus` / `ResearchObjectEnvelope` / `ACTIVE|WAITING|NEEDS_RESEARCH|THESIS_CHANGED|RISK_ALERT|INVALIDATED` **零命中**（仅 `mcp/server_manager.py` 的 `_ACTIVE_MANAGERS` 同名不同义）。F4 的对账产物是 `TrackingReport`（`state` 直接放 S0–S5 的 argmax + `triggers` + `escalation_tier`）。
>
> 以下内容保留为**设计记录**（若将来判定确需独立轴，仍以此为基础，但须先推翻上面的决议并说明为何既有投影不足）。


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

> **实施状态（2026-09）：5/7 已覆盖**（"现状"列为二次核实结果）。Q4/Q5 未落地，Q1 的 `task_records` 部分未接。

| # | 对账问题 | 代码实现 | 现状（2026-09） |
|---|---|---|---|
| 1 | 上次研究状态是什么？ | `persistence.load_artifacts(symbol)` + `latest_artifact` + `task_records` 最近 run | 🟡 `latest_artifact` 已用（`scheduler.py` 取 `prev`）；`task_records` **未接** |
| 2 | 期间发生了什么？ | `diff.py`(prev, curr) + `financial_report.links`（期间新公告/财报）+ consensus revisions + news | 🟡 `midterm_diff(prev, curr)` 已用；`financial_report.links` 未接（见 W3） |
| 3 | 哪些事实已经过期？ | `ObservationFreshness` 检查 + `stale_after` 判定 | ✅ `STALE_EXPIRED` 触发 + `_stale_after` 自动回填（缺省 7 天，`deviation.tracking.stale_after_days` 可覆盖） |
| 4 | 哪些 assumptions 失效？ | 假设登记簿（`DEVIATION_CONTROL_FRAMEWORK.md` §6.3）+ verification 状态复核 | ⬜ 假设登记簿已在**流水线内**落地（F1c `assumption_still_valid` 检测器），但 **tracking 对账未消费它** |
| 5 | 哪些 predictions 已经可以验证？ | 逐条核对 `next_evidence_to_watch` 与 `what_would_change_my_mind`：能判 → 验证；不能判 → 保留 | ⬜ 仅**继承**（`curr.next_evidence_to_watch = prev.next_evidence_to_watch`），无逐条判定 |
| 6 | 有没有反向证据？ | 新证据入 `evidence_log`（**反证强制入账**） | ✅ `enforce_attribution_accounting` + `ContradictionAccounting`（且实现比本设计更严：按**逐条**判定，见框架文档顺延项 F4-L1） |
| 7 | 当前 thesis 是否仍成立？ | `exit_conditions_met` + 贝叶斯后验更新 + `ThesisVersion` 比对 | 🟡 `check_exit(diff)` → `ExitSignal` + `decision_model.evaluate(prior_confidence=上一帧后验)` 已用；`ThesisVersion` 比对未接（W5） |


### 6.2 节点设计

> **实施状态（2026-09）：🟡 内核已实现（作为 F4），但与本节设计有两处实质分歧**。
>
> **已实现**：`alphabee/tracking/scheduler.py` 的 `reconcile(symbol, *, as_of, ...)` 实现了第 1–3、6、7 步（load → snapshot → evaluate(含 bayes) → diff → 反证强制入账 → check_exit/monitor_triggers → append_artifact），且 CLI 为 `python -m alphabee.tracking`。第 4/5 步未实现（见 §6.1）。
>
> **分歧 1（契约归属）**：本节原设计"产出 `ReconciliationReport` artifact（新 `ArtifactType` 注册）"，实现方按框架 §14.5-A「不新增契约」改为 **`TrackingReport` 返回模型**——明写"不进 `OrchestratorState`、不进 `artifacts`、不注册 `ArtifactType`"。**影响**：对账结果不进 run 的 artifacts 与 D1–D5 偏离账本，因此对账阶段自身的偏离**不被账本收录**（`--deviations` 时间线看不到）。**已决议（§14.3 D1）**：维持 `TrackingReport`、不注册新 ArtifactType，但**把 tracking 观测到的偏离写入账本**（只接事件层；不造 run 形状指标、不改账本表结构）。
>
> **分歧 2（路由）**：本节原设计的四类路由（INVALIDATED / THESIS_CHANGED / NEEDS_RESEARCH / WAITING，其中两类会**触发复核 run**）**未实现**。F4 只做"一次性 reconcile + 告警落盘"：产出 `TrackingReport`（`triggers` / `exit_reasons` / `monitor_reasons` / `blocked_actions` / `escalation_tier`）+ 写 `data/tracking/alerts/*.jsonl`，**从不调用主图**（全仓 `tracking/` 无 `alphabee_agent` / `OrchestratorState` 引用）。即"对账"有了，"对账后决定要不要重新研究"仍由人读告警决定。**已决议（§14.3 D2）**：**不**自动触发主图；改为**入口前置校验 + 告警只读消费者**，并把本节的四类路由降级为"自动触发的前置条件达成后才考虑"。

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

> **实施状态（2026-09）：✅ 已实现（形态与本设计不同）**。实际入口是**独立模块**而非 `main.py` 参数：`python -m alphabee.tracking --symbol 300750 [--watchlist ...] [--as-of YYYY-MM-DD] [--json] [--trigger manual] [--no-persist]`；`--loop` 被**显式拒绝**（exit 2，具名非目标）。告警落盘 `data/tracking/alerts/*.jsonl`（append-only）。测试：`tests/tracking/test_scheduler.py` + `tests/tracking/test_triggers.py`。以下保留原始设计以便对照。

### 7.1 v1：手动触发（零新基础设施）

**原设计**：CLI 新增 `--reconcile <symbol>`（沿用 `main.py` 现有分派模式，与 `--monitor-framework` 平级）：

```text
poetry run python main.py --reconcile 300750
```

内部跑 `reconcile()` + 渲染 ReconciliationReport（复用 `framework_monitor` 的 `render_*` 风格）。

### 7.2 v2：轻量调度器（非 daemon 起步）

**实际实现**：`alphabee/tracking/scheduler.py` 的 `run_once()` / `run_watchlist(symbols, ...)` + `triggers.py` 的五类 `TriggerKind`（`PRICE_MOVE` / `FINANCIAL_REPORT` / `ANNOUNCEMENT` / `STALE_EXPIRED` / `MANUAL`），由**外部调度器**（cron / 任务队列）按需调用；常驻循环是具名非目标。

**原设计的 sweep 伪码**（对照用；`watch_due` / `risk_alert` 两类触发当前不存在独立枚举，分别落到 `MANUAL` 与 `monitor_triggers` 委托）：

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

> **实施状态（2026-09）：⬜ 未实现**。全仓 `grep` `ResearchEngine|ResearchContext|ResearchOutput` **零命中**；`alphabee/tracking/` 无 `engine.py`。当前 L2 仍是 DeepAgents 内嵌于主图（`collect_raw_facts` / `explore_conflicts` / `verify_hypotheses` / `synthesize_insights`），**没有可替换的引擎边界**。

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

> **实施状态（2026-09）：本 T 计划已被"框架 F4 期"部分吸收**。实现方按 `DEVIATION_CONTROL_FRAMEWORK.md` §9.3/§9.4/§14.5-A 的规格，在提交 `d65164f`（7 路径 2581 行）一次性交付了"一次性 reconcile + 触发判定 + CLI + 行动红线 + 反证强制入账"。因此 **T1/T3 的主体、T2 的反证入账部分已并入 F4**；本 T 计划的剩余增量收敛为下表"剩余"列。
>
> 依赖关系：T0 → T1 → T2/T3；T4 收尾。T 期不依赖 F 期，但 T1 的假设登记簿与 F 文档 §6.3 是同一组件（**F1c 已先落地**，故 T1 的对账侧只需接线复用）。

| 期 | 原计划 | 实施状态（2026-09） | 剩余增量 |
|---|---|---|---|
| **T0** | 接线持久化 + envelope 契约 | 🟡 持久化接线**已做**（但只在 `tracking` 路径：`scheduler.reconcile` → `persistence.append_artifact`）；**envelope 已决议不建**（§14.3 D3） | ① ~~研究生命周期状态机~~（**D3 决议：降级为可选派生视图，不计入缺口**）；② 决定普通 `--midterm` run 是否也落帧（当前不落） |
| **T1** | 对账协议 + CLI 入口 | 🟡 **内核+CLI 已做**（F4 `reconcile()` + `python -m alphabee.tracking`）；**七问只覆盖 5/7**；**四类路由未做**；产物是 `TrackingReport` 而非新 ArtifactType | ① 对账第 4/5 步（assumptions × 新证据、predictions 逐条判定）；② 对账后路由（触发复核 run / 定向研究）；③ 对账产出是否进偏离账本（§14.3 分歧决策） |
| **T2** | 事件层接线 | 🟡 **反证强制入账已做**（且按逐条判定，强于原设计）；**W3/W5 未做** | ① W3：`financial_report` 财报原文 → `_window_texts()`（`_window_texts` 至今未变）；② W5：`ThesisVersion` 写入/读取 |
| **T3** | 调度器 v1 + 状态机落地 | 🟡 **调度器已做**（`run_once`/`run_watchlist` + 5 类 TriggerKind + 告警落盘 + `--loop` 显式拒绝）；**状态机未做** | ① §5 状态转移规则落地（阈值进 `config.yaml`）；② `--track-status <symbol>` 查看 RO 状态 |
| **T4** | 引擎协议 + 集成收尾 | ⬜ **全部未做** | ① L2 `ResearchEngine` 协议 + 当前实现适配器；② 复核 run 的 context 注入；③ RO 与 D1–D5 账本打通；④ ROADMAP 新增"Temporal Long-Horizon"状态行 |

**原估计**：合计约 8.5–9.5 个工作日。**按当前状态与 §14.3 决议重估**：F4 已覆盖约 40%（对账内核 + 调度 + 反证入账 + 红线）；三项决议（D1 只接事件层 / D2 入口校验 / D3 派生视图）+ W3 + W5 + §8 的**剩余合计约 6–7 天**（明细与顺位见 **§14.4**）。其中 D1/D2/D3 全部落在"接线 + 薄投影"量级，**不引入新的判定或阈值**。

#### 原始分项描述（保留对照）

**T0（约 0.5 天）**：`resolve_midterm_decision` 成功后调用 `append_artifact`（W1）；新增 `ResearchObjectStatus` + `ResearchObjectEnvelope`；测试幂等追加/读回排序/序列化。

**T1（约 2 天）**：`reconcile.py` 七步对账（复用 `diff.py`/`bayes.py`/`persistence.py`，零新引擎）；新 `ArtifactType.RECONCILIATION_REPORT` 注册；CLI `--reconcile`；测试四类路由 + 对账失败拒绝继续。

**T2（约 2 天）**：W3 财报原文 → `_window_texts()`；`ThesisVersion` 读写（W5）；反证强制入账收集端；测试含真实财报样本的 window 抽取（`data/eastmoney_reports/` 有现成数据）。

**T3（约 2 天）**：`sweep()` 触发规则（§7.2）+ W4/W6 接线；envelope 状态转移（§5.1）；CLI `--track-status`；测试触发路由与转移表全覆盖。

**T4（约 2–3 天）**：L2 `ResearchEngine` 协议 + 适配器（§8）；复核 run 的 context 注入（§6.3）；与 D1–D5 账本打通；ROADMAP 状态行。


---

## 11. 验收标准

> **当前核对（2026-09）**：6 条中 **1 条达标、1 条部分达标、4 条未达标**。

| # | 标准 | 现状 |
|---|---|---|
| 1 | 跑完的每个标的，`data/midterm/state/<symbol>.jsonl` 都有对应帧（W1 闭环） | 🟡 **部分**：只有走 `tracking` 对账的标的会落帧；普通 `--midterm` run 不落帧（`nodes/midterm.py` 无 `append_artifact`） |
| 2 | **（口径已修订，见 §14.3 D2）** 过期状态下的"直接继续"**不可能静默发生**——继续是允许的，但必须显式且入账 | ⬜ **未达标**：`reconcile` 只产告警、不触发也不阻断主图；当前"直接继续"仍是**静默**的（用户可直接重跑分析，既不告警也不入账）。达标路径 = **§14.4 顺位 2（D2-B2 入口前置校验）** |
| 3 | 期间出现的新财报被 OCR 原文窗口接进证据抽取（`_window_texts` 非空） | ⬜ **未达标**：`_window_texts` 至今仍是 `_conflict_explanations(artifacts) or None` |
| 4 | thesis 被证伪必然产生 INVALIDATED + 人工可见信号，且不自动产生仓位动作 | 🟡 **部分**：`check_exit` → `exit_reasons` 已入 `TrackingReport` 与告警；**无 INVALIDATED 状态**；"不自动产生仓位动作"已由 `require_human_confirm` 恒 `False` + `ACTION_CLASS_GATE_TIERS=(0,5)` + `escalation_tier=5` 严格保证 ✅ |
| 5 | 反证强制入账：每帧 evidence 入账日志同时包含 supporting 与 refuting 的收集尝试记录 | ✅ **已达标**（`enforce_attribution_accounting` + `ContradictionAccounting.forced_sides`；实现强于原设计，按逐条判定） |
| 6 | 六条 wiring gap（W1–W6）全部闭合，且各期单测 + 回归全绿 | ⬜ **未达标**：W2/W4 ✅、W1 🟡、W6 🟡、W3/W5 ⬜（见 §3） |


---

## 12. 风险与边界

| 风险 | 缓解 |
|---|---|
| 对账过度触发复核 run（成本） | 阈值进 config 且回测；v1 复核 run 也有自身预算（主图现有限额） |
| OCR 原文窗口污染证据抽取（长文本噪声） | 窗口只取"管理层讨论/经营情况/风险章节"摘要（`report_parser` 已有章节树，可按章节裁剪） |
| ~~envelope 与 S0–S5 语义混淆~~ | **已由 §14.3 D3 决议消解**：不建独立状态轴，故不存在双轴混淆；若做派生视图，则其单一真源即 S0–S5 与 diff 投影，天然一致 |
| 持久化数据损坏（JSONL 手工编辑） | append-only + `drop_artifact` 逃生口（既有设计）；损坏行跳过（`_read_rows` 已有容错） |
| 调度器被误用于自动交易 | 框架红线：行动类输出永不自动执行（`DEVIATION_CONTROL_FRAMEWORK.md` §9.4 沿用） |
| 与 F 期账本重复建设 | **已由 §14.3 D1 划清**：tracking **只往账本写偏离事件**（复用 fingerprint 去重与 upsert 语义，不改表结构），**不**造 run 形状指标、**不**建独立状态轴——两者是"同一账本的另一个观测来源"，不是两套记录 |

---

## 13. 一句话总结

> AlphaBee 的 Temporal Long-Horizon 不是"做一个更深的搜索 Agent"，而是把**已经建成但未接线的五层引擎**（L1 财报原文、L3 append-only 状态、L4 diff/bayes、L5 决策）接成一个**事件驱动、恢复必先对账、thesis 版本防漂移、行动永不自动**的 Research Continuum——研究对象从"生成报告"变成"半年后仍然知道自己为什么相信、哪些预测被证伪、下一步该验证什么"。

---

## 14. 实施对齐（2026-09 逐条核实）

> 核实时点：工作区 `HEAD`。核实方式：全仓 `grep` + 读取 `alphabee/tracking/{__init__,scheduler,triggers}.py`、`alphabee/orchestrator/nodes/midterm.py`、`alphabee/orchestrator/services/{deviation,detection,degradation,telemetry}.py`、`docs/roadmap/ROADMAP.md`、`git log`。
> 结论提要：**本文档的 T 计划被"框架 F4 期"部分吸收**——`tracking/` 的实现是按 `DEVIATION_CONTROL_FRAMEWORK.md` §9.3/§9.4/§14.5-A 的规格交付的（提交 `d65164f`），因此本文档 §7 已实现、§6 内核已实现、§9.2/§9.4 超额实现；**§6.2 路由与产物归属已由 §14.3 决议定案**（D1 只接事件层 / D2 入口校验 / D3 派生视图），§8 与 W3/W5 仍是净新增。

### 14.1 逐节对齐表

| 本文档章节 | 论断 | 实际实现 | 判定 |
|---|---|---|---|
| §2 L1 | `financial_report` 零接线 | 仍零接线；`_window_texts()` 未变 | ⬜ 未闭合 |
| §2 L2 | 无 ResearchEngine 协议 | 零命中 | ⬜ 未闭合 |
| §2 L3 | `persistence.append_artifact` 零调用点 | 已接线，但**仅 tracking 路径**（`scheduler.reconcile`）；普通 run 不落帧 | 🟡 部分闭合 |
| §2 L3 | 无 RO envelope | 零命中 | ⬜→**已决议不建**（§14.3 D3） |
| §2 L4 | `diff.py` 零调用点 | 已闭合（`midterm_diff` + `diff_consumers`） | ✅ 闭合 |
| §2 L4 | bayes 无入口 | 已闭合（`decision_model.evaluate(prior_confidence=上一帧后验)`） | ✅ 闭合 |
| §2 L4 | `ThesisVersion` 无读写 | 仍只有定义 + 导出 | ⬜ 未闭合 |
| §3 W2 / W4 | 差分、触发器接线 | ✅ | ✅ 闭合 |
| §3 W1 / W6 | 持久化、monitor 告警 | 🟡 tracking 路径 / MANUAL 委托 | 🟡 部分 |
| §3 W3 / W5 | 财报原文窗口、ThesisVersion | ⬜ | ⬜ 未闭合 |
| §5 状态机 | RO 生命周期 6 状态 | 零命中 | ⬜→**已决议不建**（D3：降级为可选派生视图） |
| §6.1 七问 | 对账 7 问 | 5/7（Q4、Q5 未落地；Q1 的 `task_records` 未接、Q2 的 `financial_report.links` 未接） | 🟡 |
| §6.2 内核 | `reconcile()` 七步 | 内核已实现（1–3、6、7 步） | 🟡 |
| §6.2 产物 | 新 `ArtifactType.RECONCILIATION_REPORT` | 改为 `TrackingReport` 返回模型（**明写不进流水线契约**） | ⚠️→**已决议 A2**（维持 `TrackingReport` + 偏离入账本） |
| §6.2 路由 | 4 类路由（含触发复核 run） | 无路由；只产告警，**从不调用主图** | ⬜→**已决议 B2**（不自动触发；改入口前置校验 + 告警消费者） |
| §6.3 主图注入 | RO 上下文进 `run.context` | 未做 | ⬜ |
| §7 事件层 | CLI + 扫描式调度 | ✅ 实现（独立模块入口 + `run_watchlist` + 告警落盘 + `--loop` 拒绝） | ✅ 已实现（形态不同） |
| §8 L2 协议 | `ResearchEngine` Protocol | 零命中 | ⬜ |
| §9.2 反证入账 | 每帧同时入账支持/反对 | ✅ 且**强于原设计**（按逐条判定） | ✅ 超额 |
| §9.4 行动红线 | 只允许 Tier 0/5 | ✅ `require_human_confirm` 恒 `False` + `ACTION_CLASS_GATE_TIERS=(0,5)` + `blocked_actions`/`escalation_tier` | ✅ 超额 |
| §11 验收 6 条 | — | 1 ✅ / 2 🟡 / 3 ⬜ | 见 §11 表 |

### 14.2 实现新增的、本文档未设计的能力

F4 交付物中有四项超出本文档原设计，属于净增能力：

1. **`--watchlist` 多标的扫描**（`run_watchlist`）——本设计只写了单标的 `sweep()`；
2. **告警落盘** `data/tracking/alerts/*.jsonl`（append-only），使一次性调度有可审计痕迹；
3. **`stale_after` 自动回填**（`_stale_after`，缺省 7 天，`deviation.tracking.stale_after_days` 可覆盖）——本设计只把它当输入字段；
4. **上一帧字段继承**（`exit_conditions` / `next_evidence_to_watch` / `stale_after` 由 tracking 层从 `prev` 继承并回写 `curr`），补齐了"字段已有、无调度"的缺口。

另有若干**降级纪律**超出原设计：差分被引擎拒绝（symbol/schema 漂移）时降级为不差分并记 `skipped_reason`；同日重跑不差分（`diff` 要求严格递增）；落盘失败 fail-open（不阻断帧产出）。

### 14.3 分歧点决议（2026-09 定案）

> **定案说明**：以下三项由产品/架构判断定案，**本期只写文档、不动代码**。每项给出：决策、决策依据（均为已核实事实）、落地形状（将来实现时照此执行）、明确不做的部分、被否决的选项。将来实现任一项时，仍须走 `alphabee-pipeline-contract-steward` / `alphabee-schema-steward` 流程，并按 ROADMAP 规则登记行为变更。

#### D1 —— 对账产物的契约归属 → **采纳 A2「只接事件层」**

| 项 | 内容 |
|---|---|
| **决策** | ① **不注册** `ArtifactType.RECONCILIATION_REPORT`（维持 `TrackingReport` 作为模块返回模型的定位，不进 `OrchestratorState`/`artifacts`）；② **但** tracking 观测到的偏离要写入 `deviation_events` 账本，使 `--deviations` 覆盖跟踪路径；③ **不**为 tracking 伪造 run 形状的 8 项指标。 |
| **依据（已核实）** | 账本的真实形状不是"每 run 一份日志"，而是**按 fingerprint（16 位 hex）去重的"偏离身份表"**，`run_id` 只是"最近一次观测所在 run"的元数据（`data_fetch/deviation_store.py` 的 upsert 写 `event.run_id = run_id or event.run_id`）。因此接入 tracking **不需要进图**——直接调 `record_event(issue, run_id=…, symbol=…, step_id=…)` 即可；同一偏离被 run 与 tracking 反复观测时收敛为**一行**、`run_id` 刷新为最近观测者，这**正是**「指纹复发率」（§11.2 / 框架顺延项 F5-L2）所需的形状。 |
| **落地形状（将来实现）** | ① tracking 侧加一个薄适配：把 `TrackingReport` 的 `exit_reasons` / `monitor_reasons` / `degraded` 等转成 `Issue`（`deviation_class` **显式给定**，不依赖 `category` 惰性回退）后调 `record_event`；② `run_id` 用**前缀约定**（如 `track:<symbol>:<as_of>`）区分"分析 run"与"跟踪帧"；③ **不改 `deviation_events` 表结构**——17 列已冻结（框架 §14.1-C），加列成本远高于前缀约定；④ `--deviations` 明确展示口径：跟踪帧按前缀独立分组，或混排但标注来源。 |
| **明确不做** | 不为 tracking 计算 §11.1 的 8 项 run 指标：`compute_deviation_metrics(state, ledger)` 读 `state` 的 `steps`/`decisions`/`issues`/`run`，而 tracking 产出 `TrackingReport`（无 steps/decisions），硬造形状无意义。tracking 的度量需求由 §11.2 **跨 run 指标**（指纹复发率）承接。 |
| **被否决** | **A1**（维持现状）：跟踪路径的偏离长期不可见、F5-L2 继续缺数据源。**A3**（注册新 ArtifactType）：在当前**没有下游消费者**的前提下是空转契约，与仓库既有的"避免 dead-end artifact"纪律相悖。 |

#### D2 —— 对账后是否自动触发复核 run → **采纳 B2「入口前置校验 + 告警消费者」，B3/B4 推迟**

| 项 | 内容 |
|---|---|
| **决策** | ① **不自动触发主图 run**；② 改为在**入口**做前置校验：若该标的最新帧陈旧（`stale_after` 到期）或存在未消费的 exit/monitor 触发，则落一条**显式偏离**并（开关控制、默认仅告警）要求显式确认才继续；③ 给 `data/tracking/alerts/*.jsonl` 一个**只读消费者**（CLI 视图）。 |
| **依据（已核实）** | 触发阈值**从未在真实数据上标定**：`_TV_TRIGGER=0.3`、`_EVIDENCE_RATE_TRIGGER=0.5`、`stale_after=7 天` 全为默认常量，且 `FINANCIAL_REPORT`/`ANNOUNCEMENT` 目前是 **proxy 探测**（非真实数据源）。一次主图 run = 4 个 DeepAgents（facts/conflicts/verification/insights，各带工具循环）+ report（+可选 evaluator）；自动触发 × watchlist × 每 7 天 stale ⇒ **成本无上界**。另：`data/tracking/alerts/*.jsonl` **全仓零消费者**（grep 确认，`tracking/` 之外无命中）⇒ 当前是 dead-end 产物，必须修。B2 约 1.5 天即可把"直接继续"从**静默**变为**显式且入账**，拿到 §11 验收 2 的**实质**。 |
| **落地形状（将来实现）** | ① 主图入口（或 CLI 预检）新增"未对账 / 陈旧状态"判定，命中即 `record_deviation`（D1/D5 类）+ 明确提示；确认开关进 `deviation.tracking` 段；② CLI 增加告警只读视图（与 `--deviations` 同级的只读形态，回滚 = 不调用即可）。 |
| **明确不做** | 不自动跑完整主图（B3）；不做定向研究触发（B4）——B4 依赖"pending 问题"的结构化载体（§6.1 的 Q5），该载体**当前未落地**。 |
| **B3/B4 的前置条件（顺序不可反）** | ① `deviation.tracking` 段真实落地并登记行为变更（**该段当前不存在**，`thresholds_from_settings()` 是 fail-open 读，全回落 midterm 常量）；② 用 `--deviations` 与告警历史在真实数据上**标定阈值**；③ per-symbol 冷却期 + 每次触发硬上限；④ 预算执行语义接入（`on_exceed` 属框架顺延项 F5-D3，**当前未接线**，预算目前是仪表盘不是刹车）。**四步完成前不得打开自动触发。** |
| **被否决** | **B1**（维持现状）：§11 验收 2 永远不成立，且告警持续 dead-end。 |
| **对 §11 验收 2 的影响** | 口径**修订**为：「**不可能静默地**带过期状态继续」——继续是允许的，但必须**显式且入账**（原措辞"在协议上不可能"作废）。 |

#### D3 —— 研究生命周期轴是否单独建模 → **采纳 C2「降级为派生视图」，明确否决 C1**

| 项 | 内容 |
|---|---|
| **决策** | **不建独立的持久化研究状态轴**（`ResearchObjectStatus` / `ResearchObjectEnvelope` 不落地）。若需要单一状态字展示，实现为**纯投影函数**：单一真源 = `check_exit` + `monitor_triggers` + `detect_triggers`；不落盘、不新增阈值。 |
| **依据（已核实）** | RO 想表达的四个**语义**状态**已全部有单一真源**：`INVALIDATED` ← `check_exit`（`exit_conditions_met` / 状态降级 / 仓位带背离）；`THESIS_CHANGED` ← `monitor_triggers`（`tv_distance > 0.3` → 语义即"深度研究"）；`NEEDS_RESEARCH` ← `detect_triggers`（`STALE_EXPIRED` 等 5 类）；`RISK_ALERT` ← `monitor_reasons`/`exit_reasons`（已进 `TrackingReport`）。`ACTIVE`/`WAITING` 属**调度器关注点**（在不在跑 / 无事可做），**不是研究语义**。且本仓库**已就同型问题做过一次决策**：框架顺延项 F4-L2 明写"v1 **不新增第二份判定**，只复用 midterm `diff_consumers.check_exit` 的既有投影"——§5 对上述四态而言正是**第三份**同类判定。 |
| **落地形状（将来实现）** | ① 纯函数 `research_status(symbol) -> str`（无 IO、无持久化、无新阈值）；② 阈值**复用** `monitor_triggers` 既有常量（0.3 / 0.5）与 `stale_after`，**不引入**本文档原提的 θ_c=0.7 / θ_d=0.3 / θ_e=+0.3——那会构成并行的第二份阈值定义；③ 若最终判定"不需要单一状态字"，C2 **零工作退化**为 C3。 |
| **净新增需求的归属（不在本轴内）** | ① **冷却期 / 去重**（同一标的 N 天内不重复触发研究）→ 属**调度器**对象，不是研究状态；② **"thesis 语义是否变了"**（文本层版本比对）→ 属 **W5 `ThesisVersion`**，与状态轴正交。 |
| **被否决** | **C1**（建独立持久化轴）：会与 `check_exit`/`monitor_triggers` 形成**双份判定**（可能不一致）并重复维护阈值，与本仓库既有决策相悖。 |
| **对 §5 的处置** | §5 由"缺口"**降级为「可选增强（派生视图）」**，**不再计入剩余工作缺口清单**（该节标题与状态行已同步）。 |

### 14.4 决议后的剩余工作（按依赖与价值排序）

三项决议把剩余工作从"重构级"收敛到"接线 + 薄投影"量级——**无一项引入新的判定或阈值**：

| 顺位 | 工作 | 量级 | 依赖 / 说明 |
|---|---|---|---|
| 1 | **W3：`financial_report` 财报原文 → `_window_texts()`** | ~1.5–2 天 | **收益最直接**：定性证据抽取（Stage A/B）当前在主链**从未真正生效**，中试证据日志只有数值类（beat/miss、revision）；`data/eastmoney_reports/` 有现成样本；window 需按 `report_parser` 章节树裁剪（管理层讨论 / 经营情况 / 风险章节）以控噪声 |
| 2 | **D2-B2：入口前置校验 + 告警只读视图** | ~1.5 天 | 达成 §11 验收 2 的修订口径；同时修掉 alerts 的 dead-end。开关进 `deviation.tracking` 段（新增段 ⇒ 需行为变更登记） |
| 3 | **D1-A2：tracking 偏离入账本** | ~0.5 天 | 顺序无关，但先做 D2-B2 可让"入口校验命中"直接复用同一入账路径；`run_id` 前缀约定与 `--deviations` 展示口径需一并定 |
| 4 | **D3-C2：`research_status()` 派生视图** | ~0.5 天（或 0） | 仅当需要单一状态字时做；否则零工作退化为 C3 并关闭 §5 |
| 5 | **W5：`ThesisVersion` 读写** | ~1 天 | 反漂移最后一块：当前 thesis 文本随帧演进但无版本比对；也是 D3 所排除的"thesis 语义变更"的真实归属 |
| 6 | **§8：L2 `ResearchEngine` 协议 + 适配器** | ~1–2 天 | 接入 MiroThinker / MiroFlow 一类外部引擎的前置；无外部引擎接入需求时可暂缓 |

**剩余合计约 6–7 天**（对比 F4 前的原估 8.5–9.5 天，F4 已覆盖约 40%；本表不含已完成的 F0–F5）。

---

## 15. 代码层面详细设计（P1–P6）

> §14.4 给的是**顺位与量级**，本节给**可直接开工的代码粒度**：文件清单、类/函数签名、接线点（含既有代码锚点）、序列化与兼容策略、配置项、测试矩阵、PR 切分与回滚。
> **阶段编号 P1–P6 与 §14.4 顺位一一对应**，与框架文档的 F0–F5（已完成）无重叠、无编号冲突。
> 本节所有签名均已对照现有代码核实：`orchestrator/services/deviation.py`（`record_deviation` 真实签名）、`core/schemas.py`（`Issue` 15 字段）、`orchestrator/collectors.py`（`collect_raw_facts` 的 symbol 解析与 Run 构建，约 166–179 行）、`orchestrator/nodes/midterm.py`（`_window_texts`，160–168 行）、`midterm/{decision_model,evidence_extractor,persistence,models,diff_consumers}.py`、`data_fetch/deviation_store.py`（`record_event`）、`tracking/{scheduler,triggers}.py`（`run_once` / `TrackingReport` / `TriggerKind`）、`apps/cli/{args,main}.py`、`financial_report/{links,pipeline,report_parser}.py`，以及真实 `reports/` 目录结构与 `.report_manifest.json` 字段。

### 15.0 通用工程约定（六期共同遵守）

#### A. 文件清单

| 文件 | 动作 | 阶段 | 说明 |
|---|---|---|---|
| `alphabee/orchestrator/services/report_window.py` | 新增 | P1 | 财报原文窗口选择（章节裁剪 + 字符预算 + 溯源） |
| `alphabee/orchestrator/nodes/midterm.py` | 改 | P1 | `_window_texts()` 接入原文窗口；无窗口时**显式**发 D1 issue |
| `alphabee/orchestrator/services/preflight.py` | 新增 | P2 | 入口前置校验纯函数 + `PreflightVerdict` |
| `alphabee/orchestrator/collectors.py` | 改 | P2 | `collect_raw_facts` 在 symbol 解析后调用校验并把偏离记入 `issues` |
| `alphabee/apps/cli/args.py` | 改 | P2 | `--allow-stale` / `--track-alerts` |
| `alphabee/apps/cli/main.py` | 改 | P2 | 入口 gate（拒绝/放行）+ 告警只读视图分派 |
| `alphabee/tracking/ledger.py` | 新增 | P3 | `TrackingReport → Issue` 适配 + `record_event` 落账 |
| `alphabee/tracking/scheduler.py` | 改 | P3/P4/P5 | 帧末尾接账本；派生状态字段；thesis 版本登记 |
| `alphabee/tracking/status.py` | 新增 | P4 | `ResearchStatus` + `research_status()` 纯投影 |
| `alphabee/midterm/versions.py` | 新增 | P5 | `ThesisVersion` append-only 读写 |
| `alphabee/tracking/engine.py` | 新增 | P6 | `ResearchEngine` Protocol + `ResearchContext` / `ResearchOutput` |
| `alphabee/tracking/engines/pipeline_engine.py` | 新增 | P6 | 现有主图的引擎适配器（唯一允许 import orchestrator 的 tracking 模块） |
| `alphabee/config/__init__.py` + `config.yaml`(+`.example`) | 改 | P1/P2 | `report_window` 段 + `deviation.tracking` 段 |
| `alphabee/orchestrator/services/telemetry.py` | 改 | P3 | `--deviations` 时间线的跟踪帧标注（**只读展示**，不改数据与指标） |
| `docs/roadmap/ROADMAP.md` | 改 | P2/P3 | 行为变更登记（`report_window` / `deviation.tracking`）+ 本设计状态行 |
| `tests/orchestrator/test_report_window.py` | 新增 | P1 | — |
| `tests/orchestrator/test_preflight.py` | 新增 | P2 | — |
| `tests/tracking/test_ledger.py` | 新增 | P3 | — |
| `tests/tracking/test_status.py` | 新增 | P4 | — |
| `tests/midterm/test_versions.py` | 新增 | P5 | — |
| `tests/tracking/test_engine.py` | 新增 | P6 | — |

#### B. 依赖方向（禁止反向，防 import 环与重依赖传递）

```text
core/schemas.py                      ← 只依赖 pydantic/enum
  ↑
data_fetch/*（账本 ORM/读写）         ← 只依赖 core；不得 import orchestrator / tracking
  ↑
orchestrator/services/*              ← 可依赖 core / state / contracts
  ↑
orchestrator/nodes/* + collectors    ← 可依赖 services；不得被 services import
  ↑
tracking/*（除 engines/）            ← 可依赖 midterm / core / data_fetch / orchestrator.services
  ↑
tracking/engines/*                   ← 唯一允许 import orchestrator.agent 的 tracking 子包
```

两条**显式许可与理由**（避免被"依赖方向"误判为违规）：

1. `tracking/* → orchestrator.services.deviation` **允许**：该模块 docstring 自述"只依赖 `alphabee.core` 与 `alphabee.utils`，不 import 任何 orchestrator 节点/图/数据层模块"，不引入重依赖、不产生环；
2. `tracking/engines/* → orchestrator.agent` **允许，但 import 必须写在方法体内**（不写模块顶层）——沿用 `orchestrator/nodes/record_deviations.py` 的既有纪律（其 docstring 第 5 条：不 import `orchestrator.collectors`，"那条链会拉起 tushare 等重依赖"），目的是让 `import alphabee.tracking` 不触发 `tushare.set_token` 副作用。

#### C. 兼容策略（硬约束）

1. **只 append 字段**：`Issue` / `TrackingReport` / `CompanyStateArtifact` 的新增字段一律带默认值；历史 JSONL 反序列化行为不变；
2. **账本指纹依赖 category 稳定性**：任何**新增** `Issue(category=...)` 字面量，必须同步登记进 `orchestrator/services/deviation.CLASS_BY_CATEGORY`——否则 `tests/orchestrator/test_deviation_service.py::test_every_produced_issue_category_is_registered` 必然失败。这是**门禁**，不是建议；
3. **新模块一律 fail-open**：任何异常 → `logger.warning` + 返回空/`None`，绝不打断 run 或跟踪帧（沿用 `data_fetch/helper.py::_report_tushare_failure` 的 "never let failure recording break the caller" 纪律）；
4. **新行为由开关控制**：默认值必须不改变现有行为（唯一例外见 §15.1 F 对 `report_window.enabled` 的说明，且必须登记行为变更）；
5. **不新增判定与阈值**（§14.3 三决议的核心约束）：P4 的状态投影必须复用 `monitor_triggers` 既有常量（`_TV_TRIGGER=0.3` / `_EVIDENCE_RATE_TRIGGER=0.5`），**不得**引入 θ_c/θ_d/θ_e；
6. **提交门**：沿用 `docs/roadmap/ROADMAP.md`「偏离控制框架工程实践登记」1–5 条（基线必须是提交号 / 清单用命令枚举 / 冻结协议 444 / 暂存后逐路径哈希核对 / 双跑 `ruff check` + `ruff format --check`）。

---

### 15.1 P1（W3）：财报原文窗口 → 证据抽取

#### A. 现状与目标

现状（已核实）：`nodes/midterm.py::_window_texts()` 返回 `_conflict_explanations(artifacts) or None`，其 docstring 自述"当前主链不提供真正原文"；`decision_model.collect_evidence(symbol, thesis=…, window_texts=…)` 在 `window_texts=None` 时**只跑数值类证据**（`build_numeric_evidence`：forecast / express / revision），Stage A/B（`extract_facts` → `judge_facts` → `assemble_events`）**从不触发**。

目标：在不新增在线下载、不改变 run 延迟量级的前提下，把**本地已解析的财报章节文本**接进窗口；并且"没有窗口"这件事**必须显式可见**（D1 issue），不再静默。

#### B. 新模块 `alphabee/orchestrator/services/report_window.py`（新增）

```python
import re
from pathlib import Path
from pydantic import BaseModel, Field

class WindowSection(BaseModel):
    title: str = ""
    text: str = ""
    chars: int = 0
    group: str = ""          # 命中的章节组 key（便于追溯"这条证据来自哪类章节"）

class ReportWindow(BaseModel):
    symbol: str = ""
    report_name: str = ""
    report_period: str = ""
    page_count: int | None = None
    source_path: str = ""     # 取自 manifest 的 full_text_path（reports_full 全文副本）
    sections: list[WindowSection] = Field(default_factory=list)
    chars: int = 0
    truncated: bool = False
    reason: str = ""          # 未取到的原因；空串 = 成功（有 sections）

#: 章节白名单：组内无序、**组间有序**（先命中前面的组）。
#: 只取"叙事章节"；显式排除 财务报表附注 / 审计报告 / 公司治理 / 董监高 / 股本变动
#: （超长且对 thesis 方向判定是噪声）。
SECTION_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("mda",      ("管理层讨论与分析", "经营情况讨论与分析", "管理层讨论", "经营分析")),
    ("business", ("主营业务", "经营模式", "所处行业", "行业情况", "核心竞争力")),
    ("risk",     ("风险因素", "面临的风险", "主要风险", "风险提示")),
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

def select_report_window(
    symbol: str,
    *,
    as_of: str | None = None,
    reports_root_path: str | Path | None = None,
    max_chars: int = 12_000,
    max_sections: int = 12,
    enabled: bool = True,
) -> ReportWindow:
    """从**本地已解析**的财报中选出叙事章节窗口（纯规则、禁 LLM、无网络、不抛异常）。

    步骤：
    1. glob ``<reports_root>/*(<6位代码>)/财报/*/.report_manifest.json`` → 解析 manifest；
    2. 若给 ``as_of``：只取 ``report_period``/``created_at`` 不晚于 ``as_of`` 的报告；
       按 ``created_at`` 倒序取**最新一期**；
    3. 读 manifest 的 ``full_text_path``（``reports_full`` 全文副本；缺失则回退报告目录下的 ``*.md``）；
    4. 按 :data:`_HEADING_RE` 把全文切为 (title, body) 段；
    5. 依 :data:`SECTION_GROUPS` 顺序选段，每段截断到剩余预算，累计到 ``max_chars`` / ``max_sections``；
    6. 任何一步失败 → 返回带 ``reason`` 的空窗口。

    Returns:
        :class:`ReportWindow`；``reason`` 非空表示未取到（**调用方负责显式记账**）。
    """
```

**设计决策（三条，均有代码依据）**

1. **只读本地、不新增下载**：`reports/` 与 `reports_full/` 已存在真实数据（例如 `reports/工业富联(601138)/财报/…/.report_manifest.json` → `full_text_path`）。在 run 内做"下载 → OCR → 解析"会把**分钟级**延迟引入分析主链，故 v1 不做；数据填充由既有 `financial_report.pipeline`（CLI）或后续时点任务承担（见 D）。
2. **以 manifest 的 `full_text_path` 为主入口，而非章节树**：实测 `reports/` 下同时存在"嵌套章节树"与"平铺 `<报告名>.md`"两种历史布局；读全文副本 + 按标题切分对两种布局都稳健。
3. **按 6 位代码 glob，而非按公司名拼路径**：公司名需联网解析（`links._resolve_company`），而目录名已含 `(<代码>)`，可离线确定性匹配。

#### C. 接线点：`nodes/midterm.py`

```python
# 现状（160-168 行）：
def _window_texts(artifacts: list[Artifact]) -> list[str] | None:
    return _conflict_explanations(artifacts) or None

# 改为（保留原语义：无任何窗口文本 → None，仍是 collect_evidence 的合法输入）：
def _window_texts(
    artifacts: list[Artifact], *, symbol: str | None, as_of_date: str
) -> tuple[list[str] | None, ReportWindow]:
    window = select_report_window(symbol or "", as_of=as_of_date or None)
    texts = [s.text for s in window.sections if s.text.strip()]
    texts.extend(_conflict_explanations(artifacts))   # 原文在前，已验证冲突解释在后
    return (texts or None), window
```

节点内（`try` 块内、`collect_evidence(...)` 调用**之前**）：

```python
window_texts, window = _window_texts(artifacts, symbol=symbol, as_of_date=as_of_date)
if window.sections == [] and window.reason and report_window_enabled():
    new_issues.append(record_deviation(
        DeviationClass.D1_DATA, IssueSeverity.MEDIUM,
        f"财报原文窗口不可用（{window.reason}），本次仅数值类证据参与方向判定。",
        detected_at_step="resolve_midterm_decision",
        category="report_window_unavailable",           # ← 必须登记进 CLASS_BY_CATEGORY（D1）
        scope=IssueScope.DATA,
        recovery_action="numeric_only",
    ))
elif window.truncated:
    new_issues.append(record_deviation(
        DeviationClass.D1_DATA, IssueSeverity.LOW,
        f"财报原文窗口被预算截断（{window.chars} 字符）。",
        detected_at_step="resolve_midterm_decision",
        category="report_window_truncated",             # ← 同上登记（D1）
    ))
```

> **硬约束**：`report_window_unavailable` / `report_window_truncated` 必须同步登记进 `CLASS_BY_CATEGORY`（D1），否则 §15.0 C-2 的覆盖守卫测试失败。
> **位置约束**：`window_texts` 必须在 `collect_evidence(...)` 之前算好（现有代码该行已在正确位置）；`as_of_date` 沿用节点既有取值逻辑（`run.context["as_of_date"]` → 缺省 `date.today()`）。
> **`record_deviation` 签名提醒**（已核实）：`detected_at_step` 为**必填关键字参数**，`related_step` 缺省取之。

#### D. 数据填充（不在本节点内，不阻塞 P1 验收）

窗口依赖"本地已有解析报告"。填充复用既有能力，**v1 只写文档不改代码**：

```bash
# 既有能力（financial_report.pipeline 的 CLI），按季度/年度节奏离线执行：
python -m alphabee.financial_report.pipeline --company-code 601138 --company-name 工业富联 \
    --link-kind financial --report-type annual --question "……"
```

`select_report_window` 每次 run 只读本地，**不因缺数据而失败**。

#### E. 降级纪律

| 情形 | `reason` | 记账 |
|---|---|---|
| 无该标的目录 | `no_local_report` | D1 MEDIUM issue |
| manifest 不可解析 | `manifest_unreadable` | D1 MEDIUM issue |
| `full_text_path` 指向缺失 | `full_text_missing` | D1 MEDIUM issue |
| 白名单未命中任何章节 | `no_matching_section` | D1 MEDIUM issue |
| 预算截断 | 空串 + `truncated=True` | D1 LOW issue |
| 内部异常 | `internal_error: …` | D1 MEDIUM issue（**不抛出**） |

#### F. 配置项（顶层新增 `report_window` 段）

```yaml
report_window:
  enabled: true          # false → 完全回到现状（只喂冲突解释）
  max_chars: 12000       # 窗口字符预算（约 8k token 量级）
  max_sections: 12
  reports_root: null     # null → report_parser.reports_root() 默认（<PROJECT_ROOT>/reports）
```

**默认 `enabled: true` 的行为影响**：仅当"本地存在该标的已解析财报"时窗口内容才变化（由"仅冲突解释"变为"原文 + 冲突解释"）；无报告时新增一条 D1 issue（**新增 issue 即改变 run 的可观测面**）⇒ 本段落地必须在 ROADMAP「行为变更登记」追加一行。

#### G. 测试矩阵 / 验收 / 回滚

| 用例 | 断言 |
|---|---|
| 真实样本 `reports/工业富联(601138)` | 取到 sections、`reason==""`、至少命中 `mda`/`business`/`risk` 两组 |
| `as_of` 早于所有报告 | 空窗口 + `reason` 非空 |
| `max_chars=200` | `truncated=True`、`chars` 不超预算上界、至少 1 段 |
| 无该标的目录 | `reason=="no_local_report"`，不抛异常 |
| manifest 半截 JSON | `reason=="manifest_unreadable"` |
| 节点级（monkeypatch 返回带 reason 的空窗口） | 产出 `report_window_unavailable` issue，且 `collect_evidence` **仍被调用** |
| 回归 | `tests/orchestrator/test_midterm_node.py` 全绿；`window_texts` 仍可为 `None` |

**验收**：§11 验收 3（`_window_texts` 非空、`collect_evidence` 产出定性证据）由 ⬜ → ✅。
**回滚**：`report_window.enabled=false`。

---

### 15.2 P2（D2-B2）：入口前置校验 + 告警只读视图

#### A. 新模块 `alphabee/orchestrator/services/preflight.py`（新增）

```python
class PreflightVerdict(BaseModel):
    symbol: str = ""
    checked: bool = False              # False = 无持久化帧（首次研究，不算陈旧、不阻断）
    latest_frame_id: str = ""
    as_of_date: str = ""
    stale: bool = False
    stale_after: str = ""
    days_since: int | None = None
    pending: list[str] = Field(default_factory=list)   # 未消费触发摘要（来自告警末行）
    blocking: bool = False
    note: str = ""

def check_preflight(
    symbol: str | None,
    *,
    state_dir: str | Path | None = None,
    alert_dir: str | Path | None = None,
    today: str | None = None,
    block_enabled: bool = True,
) -> PreflightVerdict:
    """入口前置校验（§14.3 D2 的 B2）。纯规则、无网络、无 LLM、**永不抛异常**。

    数据源（都是既有产物，不新增存储）：
    1. ``midterm.persistence.latest_artifact(symbol, state_dir)`` → 最新帧；无 → ``checked=False`` 返回；
    2. 帧的 ``stale_after`` 与 ``today`` 比较 → ``stale``；
    3. ``data/tracking/alerts/<symbol>.jsonl`` **最后一行**（TrackingReport JSON）→
       ``triggers`` / ``exit_reasons`` / ``monitor_reasons`` 非空 ⇒ ``pending``（逐条摘要）。
    """
```

**设计决策**

1. **只读既有产物、不新增存储**：帧来自 `data/midterm/state/<symbol>.jsonl`（F4 已写）、触发来自 `data/tracking/alerts/<symbol>.jsonl`（F4 已写）——本阶段**只消费**；
2. **无帧 ⇒ 不阻断**：首次研究某标的不属于"带过期状态继续"，必须放行，否则破坏现有分析流程；
3. **"记录"与"阻断"解耦**：记录（B 节）恒发生；阻断（C 节）受 CLI 开关约束。用户即使 `--allow-stale` 放行，**账本仍留下"这次是在陈旧状态下跑的"**——这正是 §11 验收 2 修订口径（"不可能**静默地**继续"）的落点。

#### B. 接线点 1：`orchestrator/collectors.py::collect_raw_facts`（记录，恒发生）

锚点（已核实）：`query = _latest_query(...)` → `symbol = _first_symbol(query)` → `run = Run(...)`（第 173 行）。

```python
# 在 Run 构建之后、进入采集之前插入（此时 symbol 与 state["run"] 均已可用）：
verdict = check_preflight(symbol, block_enabled=False)      # 记录侧不阻断
if verdict.blocking:
    issues.append(record_deviation(
        DeviationClass.D5_CONTROL, IssueSeverity.MEDIUM,
        f"在未对账的陈旧状态下启动分析：最新帧 {verdict.latest_frame_id or '—'}"
        f"（as_of={verdict.as_of_date}，{verdict.days_since} 天前；待消费触发 {len(verdict.pending)} 条）。"
        "本次结论可能滞后于最新事件。",
        detected_at_step="collect_raw_facts",
        category="stale_state_run",                          # ← 必须登记进 CLASS_BY_CATEGORY（D5）
        scope=IssueScope.PLANNING,
        recovery_action="proceeded_without_reconcile",
    ))
```

> **为何放在 `collect_raw_facts` 而非新增图入口节点**（重要）：`Run`（含 `symbol`）是在 `collect_raw_facts` **内部**创建的（`collectors.py:173`），START 时 `state["run"]` 不存在；新增前置节点必须改 `agent.py` 图结构**并**同步 `services/deviation.NODE_ORDER`（该元组被冻结契约测试断言），成本与风险都高于在既有入口节点内加约 10 行。

#### C. 接线点 2：CLI gate 与告警视图

```python
# apps/cli/args.py 追加：
parser.add_argument("--allow-stale", action="store_true", default=False,
    help="允许在最新跟踪帧已陈旧/存在未对账触发时继续分析（默认阻断，仅影响交互入口）")
parser.add_argument("--track-alerts", nargs="?", const="", default=None, metavar="SYMBOL",
    help="打印跟踪告警（只读视图，不运行流水线）；省略 SYMBOL 时列出全部标的的最近告警")
```

```python
# apps/cli/main.py：在 task / deviations 分派之后、进入 run_query/chat 之前：
if args.track_alerts is not None:
    print_track_alerts_view(args.track_alerts or None)
    return

verdict = check_preflight(_symbol_from_query(args.query or ""))
if verdict.blocking and not args.allow_stale:
    print(color("  ⚠ 该标的存在未对账的陈旧状态，已阻断；如需继续请加 --allow-stale", Color.YELLOW))
    print_footer(0, time.monotonic() - start_ts, enhance=args.enhance,
                 llm_review=args.llm_review, midterm=args.midterm)
    raise SystemExit(3)          # 与 tracking `--loop` 的 exit 2 同风格：明确的"未执行"退出码
```

- `print_track_alerts_view(symbol)`：读 `data/tracking/alerts/*.jsonl`（每标的取末 N 行），用 `TrackingReport.model_validate` 还原后渲染 `as_of / research_status / triggers / exit_reasons / blocked_actions`；**import 推迟到函数内**（沿用 `print_deviations_view` 的既有写法与理由）；
- **这是"告警 dead-end"的修复点**：修好之后 `data/tracking/alerts/*.jsonl` 才有消费者。

#### D. 配置项（新增 `deviation.tracking` 段）

```yaml
deviation:
  tracking:
    stale_after_days: 7        # 既有：triggers.thresholds_from_settings() 已在读（fail-open）
    block_stale_runs: true     # 新：入口是否阻断（false = 只记录不阻断）
    tv_distance: 0.3           # 新：显式登记既有默认（== diff_consumers._TV_TRIGGER）
    evidence_rate: 0.5         # 新：显式登记既有默认（== _EVIDENCE_RATE_TRIGGER）
    max_alerts_shown: 20       # 新：--track-alerts 显示条数
```

> **强制要求**：本段是**新段 + 新默认值**，按 §14.3 D2 与 ROADMAP 规则必须在「行为变更登记」追加一行（写明：`block_stale_runs=true` 会阻断陈旧状态下的 CLI 分析请求；`tv_distance`/`evidence_rate` 虽与代码常量同值，仍属显式登记）。
> `thresholds_from_settings()` 已 fail-open 读取本段（段缺失 → 回落 midterm 常量），故新增段**不改变未配置者的行为**——除 `block_stale_runs` 默认值需在登记行明示。

#### E. 测试矩阵 / 验收 / 回滚

| 用例 | 断言 |
|---|---|
| 无帧（新标的） | `checked=False`、`blocking=False`、节点不产 issue |
| 帧 `stale_after` 已过 | `stale=True`、`blocking=True` |
| 告警末行有 triggers | `pending` 非空、`blocking=True` |
| 告警含损坏行 | 跳过该行、不抛异常 |
| 节点级：陈旧状态下跑 `collect_raw_facts` | 产出 `stale_state_run` issue（D5/MEDIUM），采集继续 |
| CLI：`blocking` 且无 `--allow-stale` | `SystemExit(3)`、不进入 `run_query` |
| CLI：`blocking` 且有 `--allow-stale` | 放行，且**仍**产生 `stale_state_run` issue |
| `--track-alerts 601138` | 打印末 N 行、只读（无写库、无 run） |
| 覆盖守卫 | `stale_state_run` 已登记进 `CLASS_BY_CATEGORY` |

**验收**：§11 验收 2 由 ⬜ → ✅（修订口径）。
**回滚**：`deviation.tracking.block_stale_runs=false`（只剩记录）；或删除 CLI gate 调用（视图独立可留）。

---

### 15.3 P3（D1-A2）：tracking 偏离入账本

#### A. 新模块 `alphabee/tracking/ledger.py`（新增）

```python
TRACKING_RUN_PREFIX = "track"          # run_id 前缀约定（§14.3 D1）

def tracking_run_id(symbol: str, as_of: str) -> str:
    """跟踪帧的账本 run_id 约定：``track:<symbol>:<as_of>``。

    用前缀约定而非新增表列：``deviation_events`` 的 17 列已冻结（框架 §14.1-C），
    加列成本远高于前缀；前缀同时让 ``--deviations`` 能按来源分组。
    """

def tracking_issues(report: TrackingReport) -> list[Issue]:
    """把一次跟踪帧投影为偏离（``deviation_class`` **显式给定**，不依赖 category 惰性回退）。"""

def record_tracking_deviations(report: TrackingReport) -> int:
    """逐条 ``record_event(issue, run_id=tracking_run_id(...), symbol=…, step_id=…)`` 写账本。

    fail-open：任何异常 → 0（只 warning）。
    Returns: 成功写入条数。
    """
```

**投影表**（category 必须稳定——账本指纹依赖它；**每条都要登记进 `CLASS_BY_CATEGORY`**）

| `TrackingReport` 条件 | `category` | `deviation_class` | severity | `recovery_action` |
|---|---|---|---|---|
| `exit_reasons` 非空 | `tracking_exit_signal` | `D3_ARGUMENT` | HIGH | `escalate` |
| `monitor_reasons` 非空 | `tracking_drift_trigger` | `D4_STATE` | MEDIUM | `deep_research_due` |
| `contradiction.forced_sides` 非空 | `tracking_contradiction_forced` | `D3_ARGUMENT` | MEDIUM | `forced_accounting` |
| `degraded=True` | `tracking_frame_degraded` | `D2_STRUCTURE` | MEDIUM | `degraded_frame` |
| `skipped_reason` 非空 | `tracking_frame_skipped` | `D5_CONTROL` | LOW | `diff_skipped` |

> `Issue` 构造：`severity` 用 `IssueSeverity`，`message` 带上 `report.as_of` 与具体 reason 文本（**不要**只写类别名——账本 message 存最近一次原始文本，便于人读），`related_step` 填 `"tracking"`（tracking 帧不在 `NODE_ORDER` 内，`detection_latency` 会返回 `None`，这是**预期**行为）。

#### B. 接线点：`tracking/scheduler.py::run_once`

```python
# 在 _write_alerts(...) 之后、return report 之前：
if persist:                                  # 与帧落盘同一开关：只读预演（persist=False）不得写库
    report.deviations_recorded = record_tracking_deviations(report)
```

`TrackingReport` 追加字段（默认值，向后兼容）：

```python
    deviations_recorded: int = 0    # P3：本次写入账本的偏离条数（0 = 无偏离或写入失败）
```

**为何放在 `run_once` 而非 `reconcile`**：`reconcile()` 在 docstring 与测试中被定位为"纯推进内核"（`persist` 参数即为此设计），账本写入是**副作用**，必须留在上层 `run_once`。

#### C. `--deviations` 展示口径（`services/telemetry.py` 最小改动）

- `render_deviation_timeline(run_id)` 保持既有语义；新增**来源标注**：`run_id` 以 `track:` 开头 → 行首标 `[track]`；
- **`latest_run_id()` 的语义必须明确定义**：新增跟踪帧后，跟踪帧不应抢占"最近一次分析 run"的位置。**决策**：`latest_run_id()` 只返回**非 `track:` 前缀**的 run（查看跟踪帧需显式传 `run_id`），并在 docstring 写明理由——`--deviations` 不传参的语义是"看我最近一次分析"。

#### D. 测试矩阵 / 验收 / 回滚

| 用例 | 断言 |
|---|---|
| 投影表逐条（5 行） | 条件命中 → 对应 category / class / severity |
| 与既有指纹去重协同 | 同标的连续两帧同类偏离 → 账本 `occurrence_count` 递增、`run_id` 刷新 |
| `persist=False` | 不写帧、不写账本（`deviations_recorded == 0`） |
| DB 不可用（monkeypatch `record_event` 抛异常） | 返回 0、不抛 |
| 覆盖守卫 | 5 个新 category 均已登记进 `CLASS_BY_CATEGORY` |
| `--deviations` | 跟踪帧带 `[track]` 标注；`latest_run_id()` 不返回跟踪帧 |

**验收**：§14.3 D1 的"跟踪路径可见"落地；`--deviations` 同时能看到分析 run 与跟踪帧。
**回滚**：不调用 `record_tracking_deviations`（旁路写入，回滚=不调用）。

---

### 15.4 P4（D3-C2）：`research_status` 派生视图

#### A. 新模块 `alphabee/tracking/status.py`（新增）

```python
class ResearchStatus(enum.StrEnum):
    INVALIDATED = "invalidated"        # thesis 被证伪（退出条件触发）
    RISK_ALERT = "risk_alert"          # 状态降级 / 仓位背离 / 行情异动
    THESIS_CHANGED = "thesis_changed"  # 信念漂移超阈（tv_distance）
    NEEDS_RESEARCH = "needs_research"  # 陈旧 / 新财报公告 / watch 到期
    WAITING = "waiting"                # 无触发

def research_status(
    *,
    exit_reasons: Sequence[str] = (),
    monitor_reasons: Sequence[str] = (),
    triggers: Sequence[Trigger] = (),
    stale: bool = False,
) -> ResearchStatus:
    """研究生命周期**派生视图**（§14.3 D3 的 C2）。

    **单一真源**：``exit_reasons``（← ``diff_consumers.check_exit``）、
    ``monitor_reasons``（← ``monitor_triggers``，阈值即其既有常量 0.3 / 0.5）、
    ``triggers``（← ``triggers.detect_triggers``）。本函数**不读 config、不新增阈值、
    不落盘、无 IO**——只把已有判定投影成一个状态字。

    **无 ACTIVE**：ACTIVE 表达"正在执行复核 run"，那是**调度器**的在途状态（由外部
    cron/进程持有），不是研究对象的语义；塞进本投影会导致同一标的在不同进程里状态不同。
    """
```

**优先级（判定顺序即优先级，互斥）**

| 序 | 状态 | 条件 |
|---|---|---|
| 1 | `INVALIDATED` | `exit_reasons` 中任一条以"退出条件触发"开头（← `diff.exit_conditions_met`） |
| 2 | `RISK_ALERT` | `exit_reasons` 中任一条为"状态降级"/"仓位带…背离"，或 `triggers` 含 `TriggerKind.PRICE_MOVE` |
| 3 | `THESIS_CHANGED` | `monitor_reasons` 中任一条以 `tv_distance=` 开头 |
| 4 | `NEEDS_RESEARCH` | `stale`，或 `triggers` 含 `STALE_EXPIRED` / `FINANCIAL_REPORT` / `ANNOUNCEMENT` |
| 5 | `WAITING` | 其余 |

> **匹配口径必须与来源同源**：上表的字符串前缀（如 `"退出条件触发"` / `"状态降级"` / `"tv_distance="`）**来自** `diff_consumers.check_exit` 与 `monitor_triggers` 的既有格式化输出（已核实）。实现时应把这三个前缀提为模块常量并写**同源断言测试**（用真实 `check_exit` / `monitor_triggers` 对假帧的输出驱动本函数），避免将来上游改文案导致静默失配。

#### B. 接线点

```python
# TrackingReport 追加字段：
    research_status: str = ""      # P4：派生视图（"" = 未计算，如降级/异常路径）

# run_once 内（report 组装完成后、_write_alerts 之前）：
report.research_status = research_status(
    exit_reasons=report.exit_reasons,
    monitor_reasons=report.monitor_reasons,
    triggers=report.triggers,
    stale=any(t.kind == TriggerKind.STALE_EXPIRED for t in report.triggers),
).value
```

CLI `_render_text(report)` 增加一行展示（只读渲染）。

#### C. 测试矩阵 / 验收 / 回滚

| 用例 | 断言 |
|---|---|
| 五态转移表 | 每态 ≥1 组输入 → 期望状态；同时命中多条 → 取序小者 |
| **同源一致性** | 用真实 `check_exit` / `monitor_triggers` 对假帧的输出驱动，断言状态符合预期 |
| 无阈值引入 | 断言 `status.py` 不 import `get_settings`（AST 或 import 检查） |
| 降级路径 | `run_once` 异常 → `research_status == ""`（不猜） |

**验收**：§5 由"缺口"正式转为"可选增强已交付（派生视图）"；`--track-alerts` 显示状态字。
**回滚**：删除 `run_once` 赋值与 CLI 展示行。

---

### 15.5 P5（W5）：`ThesisVersion` 读写

#### A. 新模块 `alphabee/midterm/versions.py`（新增，与 `persistence.py` 同构）

```python
DEFAULT_VERSION_DIR = Path("data") / "midterm" / "thesis_versions"

def _version_path(symbol: str, data_dir: str | Path | None = None) -> Path: ...
def _version_id(version: ThesisVersion) -> str:      # f"{as_of_date}#{version}"
def append_version(symbol: str, version: ThesisVersion, *, data_dir=None) -> str:
    """按 id 幂等追加（append-only JSONL ``<symbol>.jsonl``）；已存在 → 直接返回 id。"""
def load_versions(symbol: str, *, data_dir=None) -> list[ThesisVersion]:
    """按 (as_of_date, version) 升序；损坏行跳过（沿用 persistence._read_rows 的容错口径）。"""
def latest_version(symbol: str, *, data_dir=None) -> ThesisVersion | None: ...

def register_thesis_if_changed(
    symbol: str, *, as_of: str, thesis: str,
    invalidation: Sequence[str] = (), data_dir=None,
) -> tuple[ThesisVersion | None, str]:
    """thesis 文本与最新版本不一致 → 追加新版本，否则不动。

    反漂移语义（``ThesisVersion`` 的既有意图："原始买入理由与证伪条件不可被行情重写"）：
    - **首个版本**（无历史）：``version=1``、``buy_rationale=[thesis]``、``invalidation=list(invalidation)``；
    - **后续版本**（thesis 变了）：``version=prev.version+1``、``thesis=新文本``，
      **``buy_rationale`` / ``invalidation`` 继承首版**（不得被后续行情/叙事重写）。

    Returns: ``(新版本 | None, reason)``；reason ∈ {first_registered, changed, unchanged}。
    """
```

> **注意**：`ThesisVersion` 模型字段为 `version / as_of_date / thesis / buy_rationale / invalidation`——**没有 `symbol` 字段**，标的由文件名承载。因此本模块**不改 `midterm/models.py`**（不动契约面）。

#### B. 接线点：`tracking/scheduler.py::_reconcile_frame`

```python
# 在 persistence.append_artifact(curr, state_dir) 成功之后：
if persist:
    version, reason = register_thesis_if_changed(
        symbol, as_of=as_of, thesis=str(curr.thesis or ""),
        invalidation=[c.condition for c in (curr.exit_conditions or [])],
    )
```

`TrackingReport` 追加字段：`thesis_version: int = 0`、`thesis_version_reason: str = ""`。

#### C. 与 D3 的关系（避免重复建设）

`ThesisVersion` 承载的正是 §14.3 D3 所排除的**"thesis 语义是否变了"**——它与研究状态轴正交：状态轴回答"现在该做什么"，版本轴回答"当初为什么这么想、后来改了没有"。本阶段**只做登记与比对**，不做自动回滚或结论改写。

#### D. 测试矩阵 / 验收 / 回滚

| 用例 | 断言 |
|---|---|
| 首次登记 | `version=1`、`buy_rationale==[thesis]`、`reason=="first_registered"` |
| thesis 未变（同日重复调用） | 不追加、`reason=="unchanged"`、文件行数不变 |
| thesis 改变 | `version=2`、`thesis` 为新文本、**`buy_rationale` 仍为首版**（反漂移核心断言） |
| 幂等 | 同 id 重复 append → 文件仍 1 行 |
| 损坏行 | 跳过、其余可读、不抛异常 |
| `persist=False` | 不写版本文件 |

**验收**：W5 由 ⬜ → ✅；`load_versions(symbol)[0].buy_rationale` 恒为"当初为什么买"。
**回滚**：不调用 `register_thesis_if_changed`；新目录可安全保留或删除。

---

### 15.6 P6（§8）：L2 `ResearchEngine` 协议

#### A. 新模块 `alphabee/tracking/engine.py`（新增）

```python
class ResearchContext(BaseModel):
    """L2 引擎输入（研究对象的上下文，不绑定主图内部类型）。"""
    symbol: str
    question: str = ""
    as_of: str = ""
    thesis: str = ""
    prior_confidence: float | None = None
    evidence_ids: list[str] = Field(default_factory=list)     # 已有证据 id（跨 run 复用）
    open_questions: list[str] = Field(default_factory=list)    # 待研究问题（§6.1 Q5 / 未探索区域）

class ResearchOutput(BaseModel):
    """L2 引擎输出（序列化产物，不要求与主图 Artifact 同构）。"""
    engine: str = ""
    summary: str = ""
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    issues: list[dict[str, Any]] = Field(default_factory=list)
    degraded: bool = False
    degradation_reason: str = ""

class ResearchEngine(Protocol):
    name: str
    async def run(self, context: ResearchContext) -> ResearchOutput: ...
```

#### B. 适配器 `alphabee/tracking/engines/pipeline_engine.py`（新增）

```python
class PipelineEngine:
    """把现有主图（``orchestrator.agent.alphabee_agent``）适配为 L2 引擎。

    **import 必须延迟到方法体内**（§15.0 B-2）：模块顶层 import 会拉起
    ``orchestrator.collectors`` → tushare ``set_token`` 副作用。
    """
    name = "pipeline"

    async def run(self, context: ResearchContext) -> ResearchOutput:
        from alphabee.orchestrator.agent import alphabee_agent   # 延迟 import
        initial = {
            "messages": [HumanMessage(content=context.question or context.symbol)],
            # 控制标志（enhance / llm_review / midterm）由构造参数或 settings 决定
            ...
        }
        final = await alphabee_agent.ainvoke(initial)
        return _to_output(final)     # 从最终 payload 抽 artifacts / issues / degraded
```

**v1 明确不做**（写成非目标，防范围膨胀）：不重构主图为插件系统；不改 `agent.py` 图结构、不改 `NODE_ORDER`；不接入任何外部引擎（MiroThinker / MiroFlow / Tongyi）——只交付**协议 + 现有实现适配器 + 一个 stub 引擎测试**，作为将来替换的接缝。

#### C. `ResearchContext → run.context` 的注入边界

v1 只把 `symbol` / `as_of` / `thesis`（作 `thesis_prior`）/ `prior_confidence` 四个键注入 `Run.context`——**不注入** `evidence_ids`（主图当前无消费者，注入即 dead-end）。主图要真正消费这些键属**后续独立一期**，并需按 steward 流程登记 `OrchestratorState`/节点契约变更。

> 注意：`Run` 目前是在 `collect_raw_facts` 内部创建的（`collectors.py:173`），会**覆盖**调用方传入的 `run`。因此 P6 若要让注入生效，需在 `collectors.py` 改为"已有 run 则复用/合并 context"（**这一处改动必须写进 P6 的 inScope，并加回归测试**）；否则注入是 dead-end。这是 P6 最容易被忽略的实现细节。

#### D. 测试矩阵 / 验收 / 回滚

| 用例 | 断言 |
|---|---|
| `PipelineEngine` 满足 Protocol | 结构性检查（`name` + `run` 存在且可调用） |
| 上下文映射 | monkeypatch `alphabee_agent.ainvoke` → 捕获 initial state，断言 `run.context` 四键正确 |
| **延迟 import** | `import alphabee.tracking.engines.pipeline_engine` **不**使 `orchestrator.collectors` 进入 `sys.modules` |
| stub 引擎可替换 | stub 实现 Protocol 并跑通一个消费方调用（证明接缝可用） |
| run 复用回归 | `collect_raw_facts` 在已有 `run` 时合并而非覆盖 context |

**验收**：§8 由 ⬜ → ✅（协议存在、现有实现可替换）。
**回滚**：删除 `engines/` 子包；协议模块无副作用可留。

---

### 15.7 配置项汇总

| 段 | 键 | 默认 | 阶段 | 行为变更登记 |
|---|---|---|---|---|
| `report_window` | `enabled` | `true` | P1 | **是**（无报告时新增 D1 issue；有报告时窗口内容变化） |
| `report_window` | `max_chars` / `max_sections` / `reports_root` | `12000` / `12` / `null` | P1 | 否（仅上限参数） |
| `deviation.tracking` | `stale_after_days` | `7` | P2 | 是（显式登记既有默认） |
| `deviation.tracking` | `block_stale_runs` | `true` | P2 | **是**（新增阻断行为） |
| `deviation.tracking` | `tv_distance` / `evidence_rate` | `0.3` / `0.5` | P2 | 是（显式登记既有默认，值不变） |
| `deviation.tracking` | `max_alerts_shown` | `20` | P2 | 否（只读视图） |

> 全部新段的实现方式与 `DeviationSettings` 一致：`alphabee/config/__init__.py` 的 Pydantic 模型 + `config.yaml.example` 同步 + **旧 config 缺段仍可 import**（默认值保证，需专测 + 变异坐实）。

### 15.8 测试矩阵汇总

| 阶段 | 测试文件 | 关键断言（不可省） |
|---|---|---|
| P1 | `tests/orchestrator/test_report_window.py` | 真实 `reports/` 样本命中；无报告 → `reason` 非空不抛；预算截断；节点级 issue 产出 |
| P2 | `tests/orchestrator/test_preflight.py` | 无帧不阻断；陈旧/触发 → `blocking`；CLI 两路径；`stale_state_run` 入账 |
| P3 | `tests/tracking/test_ledger.py` | 投影表 5 条；指纹去重与 `occurrence_count`；`persist=False` 不写；fail-open 返回 0 |
| P4 | `tests/tracking/test_status.py` | 五态 + 优先级互斥；**同源驱动**（真实 `check_exit`/`monitor_triggers` 输出）；无新阈值 |
| P5 | `tests/midterm/test_versions.py` | 首版登记；未变不追加；变更后 `buy_rationale` **保持首版**；幂等 |
| P6 | `tests/tracking/test_engine.py` | Protocol 结构化满足；context 映射；**延迟 import 不拉起 collectors**；run 复用 |
| 横向 | `tests/orchestrator/test_deviation_service.py`（既有） | 新 category 全部已登记（覆盖守卫）——**P1/P2/P3 新增 category 后必须为绿** |
| 横向 | 全量门禁 | `poetry run pytest` + `ruff check` + `ruff format --check`。**基线口径**：本次核实 `--collect-only` 为 **1836 collected / 11 errors**；11 个收集错误全部来自 `chmod 444` 冻结文件（如 `tests/orchestrator/test_recovery.py`、`tests/company_track/test_e3_consumption.py`）与 `tmp/pre-commit-home/` 残留，**非本设计引入**——按提交门第 3 条，跑门禁前须先 `chmod 644` 解冻、跑完复冻 444 |

### 15.9 PR 切分与回滚

| PR | 范围（inScope） | 回滚方式 |
|---|---|---|
| PR1 | `services/report_window.py`、`nodes/midterm.py`、`config`、`test_report_window.py`、ROADMAP 登记 | `report_window.enabled=false` |
| PR2 | `services/preflight.py`、`collectors.py`、`apps/cli/{args,main}.py`、`config`、ROADMAP 登记、`test_preflight.py` | `block_stale_runs=false` + 移除 CLI gate |
| PR3 | `tracking/ledger.py`、`tracking/scheduler.py`、`services/telemetry.py`、`test_ledger.py` | 不调用 `record_tracking_deviations` |
| PR4 | `tracking/status.py`、`tracking/scheduler.py`、`test_status.py` | 删除赋值与展示行 |
| PR5 | `midterm/versions.py`、`tracking/scheduler.py`、`test_versions.py` | 不调用 `register_thesis_if_changed` |
| PR6 | `tracking/engine.py`、`tracking/engines/`、`collectors.py`（run 复用）、`test_engine.py` | 删除 `engines/` 子包 |

**依赖**：PR1 / PR2 相互独立可并行；PR3 与 PR2 共享"入口命中即入账"的形状但不硬依赖；PR4 复用 PR3 的展示口径（`--track-alerts` 显示状态字）；PR5 / PR6 独立。**每期先 review（verdict=pass）后提交**，并遵守 §15.0 C-6 的提交门 5 条。

### 15.10 验收映射

| 本文档条款 | 由哪期达成 |
|---|---|
| §3 W3（财报原文进窗口） | P1 |
| §3 W5（`ThesisVersion` 读写） | P5 |
| §11 验收 2（不可能**静默**继续） | P2（口径已按 §14.3 D2 修订） |
| §11 验收 3（`_window_texts` 非空） | P1 |
| §11 验收 6（wiring gap 闭合） | P1（W3）+ P5（W5）；W1 已由 F4 闭合 tracking 路径 |
| §14.3 D1（跟踪路径可观测） | P3 |
| §14.3 D2（入口校验 + 告警消费者） | P2 |
| §14.3 D3（派生视图） | P4 |
| §8（L2 引擎协议） | P6 |
