# Apiary 研究操作系统架构设计（双速研究 × 能力飞轮）

> 状态：设计稿（未实施；v0.3 修订见 §13.5）
> 定位：横向架构设计。回答"研究系统最终形态是什么、能力如何持续增长、固定编排 pipeline 与开放式研究如何协作"。
> 关联：`docs/roadmap/AGENT_STRATEGY_ROADMAP.md`、`docs/design/LLM_AGENT_ROLE_MAP_DESIGN.md`、`docs/design/SINGLE_FRAME_QUALITY_DESIGN.md`、`docs/design/BACKTEST_SNAPSHOT_REPLAY_DESIGN.md`、`docs/design/GOLDEN_FRAME_FACTORY_EXPERIMENT.md`。
> 纪律：本文只定义架构、契约、流程与验收；不含实现细节；文中阈值与数量均为**建议初值**，需用真实样本校准。

---

## 0. 结论摘要

1. 最终形态不是 pipeline 或 Scout 二选一，而是 **能力库 + 双速执行 + 学习回路**。
2. **pipeline 是骨架，Scout 是感官，能力库是肌肉，Apiary 是教练。**
3. pipeline 负责"已知世界"的快速路径；Scout 负责"未知世界"的深度与广度；能力库是复利资产；Apiary 把"答不了的问题"变成"能力库里的工具"。
4. 纯 sidecar 的 Scout 会被 pipeline 的事件、schema、顺序、能力、摘要五种偏差束缚。**Scout 必须是 pipeline 的同行，不是下游**：共享数据层，不共享流程。
5. 成败不在 Agent 技术，而在三件工程纪律：**能力抽象、数据可达、评估闭环**。
6. 先 MVA（最小可行架构），再自动化。先证明"问题有用率 / 能力复用率 / 研究时间下降 / 人工 triage 可控"，再谈能力飞轮。

---

## 1. 背景与问题

### 1.1 现有 pipeline 的价值与边界

AlphaBee 现有主图是一条确定性研究管线：取数 → 派生/信号/异常 → 冲突探索 → 假设验证 → 洞察 → 论点 → 审查 → 报告。它的价值是：

- 快、可复现、可审计；
- 有统一的事实口径与契约；
- 能对大多数标的产出"及格线"研究。

它的边界是：

- 研究问题被固定的节点顺序和 schema 框定；
- 只能问"工具能算"的问题；
- 报告容易变成数据与观点的堆砌，而不是围绕核心争议的论证；
- 无法系统性地发现"自己没问的问题"。

### 1.2 研究分析的真实需求

股票/公司研究涉及的方面太广。固定流程能产出结果，但很难抓住根本。真实研究需要：

- **深度**：从事实 → 分解 → 机制 → 竞争解释 → 预期/定价 → 跟踪；
- **广度**：商业模式、财务质量、行业、竞争、预期、估值、催化剂、治理；
- **反方**：最强反证、替代解释、证伪条件；
- **连续性**：观点版本、观察清单、变化跟踪；
- **可扩展**：遇到答不了的问题，能识别缺口并补能力。

### 1.3 纯 sidecar 的五种偏差

把 Scout 只做成"订阅 pipeline 事件"的旁路，仍然会被现有流程束缚：

| 偏差 | 表现 |
|---|---|
| **事件选择偏差** | pipeline 决定发什么事件；没被计算的东西，Scout 永远看不到 |
| **本体偏差** | artifacts 已按 signal / dimension / conflict 分类；Scout 继承这套范畴 |
| **顺序偏差** | 事件按 DAG 顺序到达；Scout 看到的是切片，不是完整研究状态 |
| **能力偏差** | pipeline 只取"工具能算"的数据；Scout 问不到没被取的数据 |
| **摘要偏差** | Scout 读的是 summary；摘要之外的东西，它不知道存在 |

因此，**sidecar 适合观察，不适合研究**。Scout 需要自己的数据入口和研究计划。

### 1.4 目标与非目标

**目标**

- 建立一个双速研究系统：快速、可复现的基线 + 开放、深入的探索。
- 把研究问题持续转化为可复用能力，并让稳定能力"毕业"进快速路径。
- 让研究过程可对账、可跟踪、可积累。
- 让系统能识别"答不了什么"，并形成能力建设 backlog。

**非目标**

- 不做端到端全自主分析师。
- 不替代人的判断与裁决。
- 不做通用多 Agent 框架。
- 不重建一个 Langfuse。
- v1 不追求 Apiary 全自动。
- 不追求"零 backlog"；长尾永远存在。

---

## 2. 核心概念

### 2.1 术语

| 术语 | 定义 |
|---|---|
| **AlphaBee** | 运行时（数据面）：数据、计算、研究、简报 |
| **Apiary** | 能力发展系统（控制面，独立仓库）：问题/缺口管理、能力审计、开发编排、评估与毕业 |
| **Pipeline（System 1）** | 固定编排 + 有界 LLM + 确定性计算：编排固定，但部分节点调用 LLM；可复现性分三层，见 §2.6 |
| **Scout（System 2）** | 独立研究 Agent（**AlphaBee 运行时**）：自己的数据入口、研究计划、问题与备忘；问题发现是它向 Apiary 输出的一类结果 |
| **Capability（能力）** | 一个可复用、可测试、输入输出明确，且**带适用条件与可识别性声明**的确定性工具 |
| **Capability Library（能力库）** | 所有已毕业能力的集合，pipeline 与 Scout 共用 |
| **Capability Gap（能力缺口）** | 研究问题需要、但当前能力库/数据层不支持的东西 |
| **Graduation（毕业）** | 能力从实验性实现进入能力库、被稳定复用的过程 |
| **Reconciliation（对账）** | 比较 pipeline 与 Scout 的 claim、覆盖、分歧，产出盲区与问题 |
| **Question Bank（问题库）** | 跨标的、去重后的研究问题集合 |
| **Research Session** | 一个标的/问题的研究线程：问题、claim、简报、观察清单 |
| **Brief（研究简报）** | 1–2 页的研究交付物：答案、claim、反证、caveat、下一轮问题 |

### 2.2 能力与能力家族

能力不是"一个标的的特殊问题"，而是**可跨标的复用的确定性工具**。例如：

```text
收入/毛利分解（分部口径，标注可识别性）/ 杜邦分解 / 现金桥 / 分部与产品结构 /
同行同口径基准 / 一致预期修正 / 事件时间线 /
估值反推（隐含 ROE/增长）/ 会计质量评分 /
订单与产能跟踪 / 行业景气与政策 / 治理与关联交易
```

每个能力有固定卡片：

```yaml
id: margin.segment_decompose
name: 毛利额/毛利率分解（分部口径）
inputs: [income_statement, segment_data]
outputs: [revenue_effect, segment_mix_effect, segment_margin_effect, residual]
applicability: 有分部收入/成本/毛利率披露，且分部期间与财报期间可对齐
min_data: 连续两期分部收入 + 分部成本或毛利率；否则不可分解
identification: exact | partial | not_identifiable
assumptions: [...]
sensitivity: [...]
output_nature: deterministic | estimate | scenario | cannot_answer
determinism: high
existing_tool: null
tests: [...]
status: missing | partial | ready | deprecated
owner: ...
version: 1
```

> **可识别性优先于确定性。** 代码每次输出相同的结果，不等于识别出了真实因果。缺少销量、单价、单位成本或足够细的产品结构时，"量/价/结构"无法分离；能力必须声明 `identification` 与 `output_nature`，否则工具越成熟，越容易让使用者高估结论。

### 2.3 四类缺口

不是所有"答不了"都要开发。必须区分：

| 类型 | 含义 | 处理 |
|---|---|---|
| **能力缺口** | 数据有、方法有，缺一个确定性工具 | 进开发 backlog |
| **数据缺口** | 数据不存在/拿不到 | 进数据 backlog 或 park，不开发 |
| **方法缺口** | 缺分析框架/模型 | 进研究 backlog，先做方法设计 |
| **知识缺口** | 需要专家/调研/定性判断 | 留给人工，不自动化 |

**只有能力缺口进入开发。** 否则 backlog 会被不可开发的请求淹没。

### 2.4 毕业

一个能力的一生：

```text
问题（Scout）
  → 能力请求（Capability Auditor）
  → 立项（人 + Portfolio Manager）
  → 开发（Builder Agent + 人）
  → 回归（Verifier Agent + 人）
  → 毕业进能力库
  → pipeline / Scout 调用
  → Apiary 转向下一个缺口
```

毕业门槛（建议硬门槛）：

| 维度 | 条件 |
|---|---|
| 确定性 | 不需要 LLM 判断就能算，输入输出稳定 |
| 复用性 | 至少 2 个标的/场景需要 |
| 可验证 | 有单测 + 黄金案例，结果可复现 |
| 频率 | 高频或高价值，不是一次性 |
| 成本 | 比每次让 Scout 现算更便宜/更快 |
| 接口 | 有明确输入输出 schema，能被 pipeline 组合 |

### 2.5 双速与对账

- **System 1（pipeline）**：快、编排固定，但**不是全确定性**（部分节点调用 LLM），负责基线。
- **System 2（Scout）**：慢、开放、发散，负责挑战基线、发现盲区。
- 两者**共享数据层**，内部表示可以不同，但**必须共享最小交换契约**（标的、期间、claim、来源、证据定位）；否则对账成本失控。
- **对账层**比较两个视图，产出"pipeline 没问的问题、没看的证据、没解释的分歧"。
- 不要求两个系统达成一致；**分歧本身是产出**。

### 2.6 三层可复现性

"固定 DAG"只意味着编排固定；节点内部的 LLM 仍可能变化。必须分开记录：

| 层 | 含义 | 可复现手段 |
|---|---|---|
| **编排** | 节点顺序、分支、状态转移 | DAG 固定 + 版本化 |
| **计算** | 数值/分解/规则 | 确定性函数 + 参数对象 + 黄金案例 |
| **生成** | LLM 解释、叙事、判断 | 冻结输出 / 固定 model+prompt+seed / 多次采样 |

没有这三层，不能笼统宣称"pipeline 可复现"。v1 至少要做到：计算层逐值可复现；生成层记录 model/prompt/params/输出，必要时冻结。

---

## 3. 总体架构

### 3.1 分层

```text
┌──────────────── AlphaBee（运行时，仓库 A）───────────────────────┐
│  ┌──────────────── Research Session / Brief ────────────────┐  │
│  │  用户问题、简报、claim、观察清单、下一轮问题              │  │
│  └───────────────▲──────────────────────────▲───────────────┘  │
│                  │                          │                   │
│     ┌────────────┴───────────┐   ┌──────────┴─────────────┐     │
│     │ System 1: Pipeline     │   │ System 2: Scout        │     │
│     │ 固定编排 + 有界 LLM     │   │ 独立研究循环            │     │
│     │ 组合能力库              │   │ 调用能力库 + 原始数据    │     │
│     └────────────▲───────────┘   └──────────▲─────────────┘     │
│                  │                          │                   │
│          ┌───────┴──────────────────────────┴───────┐           │
│          │        Capability Library（能力库）        │           │
│          │  毛利分解 / 杜邦 / 现金桥 / peer / 估值反推│           │
│          └───────────────────▲──────────────────────┘           │
│                              │                                  │
│          ┌───────────────────┴──────────────────────┐           │
│          │ Data Layer（canonical facts / 原始证据）  │           │
│          └───────────────────▲──────────────────────┘           │
└──────────────────────────────┼──────────────────────────────────┘
                               │ HTTP API / 异步任务 / 快照引用
┌──────────────────────────────┴──────────────────────────────────┐
│ Apiary（控制面，仓库 B）：问题/缺口管理 → 能力审计 → 开发 → 毕业 │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 两条执行路径

| 路径 | 触发 | 特点 | 产出 |
|---|---|---|---|
| **快速路径** | 常规标的/跟踪 | 固定编排、计算可复现、低延迟 | 基线简报、changelog |
| **开放路径** | 新标的/重大事件/用户追问 | 开放研究、慢、贵 | 研究备忘、盲区、问题 |

### 3.3 学习回路

```text
Scout（AlphaBee 运行时）提出问题 / 盲区 / 数据缺口
  → Apiary 接收、去重、归类
  → Capability Auditor 判断"数据/工具/框架是否支持"
  → 不支持 ⇒ 能力请求
  → 打分/排序
  → 人工 + Portfolio Manager 立项
  → Builder 在 AlphaBee 仓库开分支/PR
  → Verifier 回归 + 研究 A/B
  → 毕业进能力库（AlphaBee 发布）
  → pipeline / Scout 复用
  → Scout 转向更高层的问题
```

### 3.4 路由器

不是所有标的都跑全套。按不确定性选择：

| 场景 | 主力 | 比例（建议） |
|---|---|---|
| 成熟覆盖、常规跟踪 | pipeline | 80/20 |
| 新标的、次新股、转型 | Scout | 50/50 或更高 |
| 财报暴雷、重大事件 | pipeline + Scout 并跑 | 50/50 |
| 用户追问"为什么/如果" | Scout | 以 Scout 为主 |
| 定期跟踪 | pipeline | 以 pipeline 为主 |

v1 用**人工路由**；等覆盖率/成本数据够了再自动化。

---

## 4. 组件设计

### 4.1 Research Session / Brief

**Session** 是一个标的/问题的研究线程：

```text
ResearchSession
  session_id / symbol / question / scope
  thesis_version / run_ids[] / claim_ids[]
  coverage_map / watchlist / status
```

**Brief** 是 1–2 页的主交付物：

- 问题与答案（含置信度）；
- 3–5 条 claim（证据/反证/竞争解释/证伪）；
- 最强反证；
- 数据 caveat；
- 覆盖地图；
- 下一轮 3 个问题；
- 观察清单。

长报告降级为"证据附录"，按需展开。

### 4.2 Capability Library

**定位**：唯一的能力事实源，pipeline 与 Scout 共用。

**原则**：

- 一个能力 = 一个确定性函数 + schema + 测试 + 黄金案例。
- **适用条件与可识别性是能力契约的一部分**：不能计算的东西要能说"不可识别"，不能输出一个稳定但无意义的数。
- pipeline 节点只做编排，不写业务逻辑。
- Scout 调用能力，不重复实现。
- 能力有 owner、版本、状态、退役机制。
- 能力库只增不减是反模式；必须能 deprecated。

**能力清单（manifest）由 AlphaBee 生成**，不是手维护。Apiary 只缓存/消费。

### 4.3 Pipeline（System 1）

**定位**：已知世界的快速路径。

**改造原则**：

- 保持现有主图可运行；不重写。
- 节点内业务逻辑逐步下沉到能力库。
- 不因为 Scout 发现一个问题就新增一个节点；新增能力，不新增节点。
- 节点只做：调用能力 → 检查 → 组装 artifact。

**反模式**：pipeline 巨石化。每发现一个问题就加一个节点，最终变成一堆特例。

### 4.4 Scout（System 2）

**定位**：未知世界的独立研究 Agent。

**归属**：Scout 属于 **AlphaBee 运行时**，不属于 Apiary。它需要完整的研究状态、工具、数据与证据管理；放在 Apiary 会迫使控制面重建一套研究运行环境。Scout 对 pipeline 的独立，来自**自己的研究计划、数据访问和上下文**，不是仓库位置。

**输出流向**：Scout 的问题、盲区、数据缺口写入 AlphaBee 的研究产物；Apiary 通过 API/快照拉取，进入问题库与能力审计。

**核心纪律**：

1. **自己的数据入口**：和 pipeline 相同的数据适配器 + 通用工具（财报、公告、新闻、行情、peer）。
2. **自己的研究计划**：不走 pipeline 的 DAG。
3. **pipeline artifacts 只作参照**：可以读、可以不同意，但不是唯一事实源。
4. **强制引用**：每条 claim 必须带 artifact/observation/原文引用。
5. **不阻塞**：Scout 挂了，runtime 不受影响。

**执行协议（独立性保障）**：

1. **盲读阶段**：只读原始数据与标的背景，形成问题、初步解释、证据需求；此阶段**不读 pipeline artifacts**。
2. **落稿**：把盲读结论写入独立 memo（带时间戳 / run_id）。
3. **对账阶段**：再读 pipeline brief/artifacts，做针对性挑战与对账。
4. **记录**：memo 标注哪些结论在盲读阶段已形成、哪些是对账后修正。

**注意**：同一模型、同一数据、同一 prompt 仍会产生相关错误；两路一致不等于独立验证。要降低相关性，需要不同的检索路径、不同的策略 prompt，必要时不同模型。

**研究循环**：

```text
target + question + taxonomy
  → 自选策略
  → 取原始证据（工具，带引用）
  → 形成 claim + 反证 + 竞争解释
  → 问"还缺什么 / 什么会改变判断"
  → 输出：研究备忘 + 问题清单 + 数据缺口
  → 与 pipeline brief 对账：盲区 / 分歧
```

**多策略**（同标的、不同视角）：

| 策略 | 起点 | 擅长发现 |
|---|---|---|
| Bottom-up | 事实/异常/勾稽 | 数据里的异常和未解释残差 |
| Top-down | 一个假设 | 证伪路径、关键判别证据 |
| Peer/Industry | 同行/产业链 | 相对位置、行业共性 vs 公司特有 |
| Contrarian | 反方立场 | 基线观点的最强反驳 |
| Data-quality | 证据链 | 口径、时点、单位、缺失 |

**独立性测试**：Scout 能否在不读 pipeline artifacts 的情况下，仅凭原始数据对一个新标的提出有价值的问题？如果不能，它还是下游。

### 4.5 Reconciliation

**定位**：比较两个视图，产出盲区、分歧、缺口。

**对账项**：

| 对账项 | 输出 |
|---|---|
| 结论分歧 | pipeline 看多/看空，Scout 的相反证据 |
| 覆盖盲区 | pipeline 没查的维度/数据/竞争解释 |
| 证据冲突 | 同一事实的不同口径/来源 |
| 未回答问题 | Scout 提出但当前答不了的问题 |
| 数据缺口 | 需要新数据源/新工具的问题 |

**原则**：

- 共享 canonical facts，但**保留原始观察、修订链与事实冲突**；"一个当前值"不等于"没有冲突"。
- Scout 有权挑战规范化结果；事实冲突进入 conflict registry，不静默取最新。
- 不强制合并；分歧是产出。
- 高影响分歧由人工裁决。

### 4.6 Apiary（Capability Factory）

**定位**：能力发展系统（控制面），不直接做股票分析。

**五个角色 + 人工门**：

> 命名说明：**Scout 在 AlphaBee 运行时内执行研究并产出问题**；Apiary 不再拥有 Scout 执行环境，只负责**问题接收、去重、归类和能力审计**。原“Agent1 问题发现”从 Apiary 角色改为 AlphaBee 内 Scout 的职责，Apiary 侧对应角色为 **Question Curator**。

| 角色 | 职责 | 关键要求 |
|---|---|---|
| **Question Curator（接收 Scout 的问题）** | 去重、归类、补"改变什么决策/需要什么证据"；不重新做研究 | 每问必须能追溯到 Scout 产物；每标的限 10–15 问 |
| **Capability Auditor** | 判断问题需要的数据/工具/框架是否存在 | 必须引用 manifest/代码/数据目录；允许 `unknown` |
| **Portfolio Manager（人 + Agent）** | 去重、打分、立项、限流 | 频率/决策影响/数据可得/成本/依赖/风险 |
| **Builder（人 + Agent）** | 开发能力 | spec + 验收 + 测试 + 至少 2 个使用场景 |
| **Verifier（人 + Agent）** | 回归 + 研究 A/B | 代码测试 + "新能力是否改变原问题答案" |

**支撑角色**（可先手动）：

- **Librarian**：维护能力清单、数据目录、问题模板。
- **Deduper**：语义聚类 + 能力 ID 去重。
- **Research Evaluator**：评估问题有用率、决策影响。

**请求状态机**：

```text
proposed → triaged → approved → building → verifying
  → graduated → active → deprecated
  ↘ rejected / parked（数据缺口、知识缺口）
```

**WIP 限制**：同时开发的能力不超过 2–3 个；请求生成必须受开发容量约束。

### 4.7 可观测性与跨仓通信

**定位**：AlphaBee 内部保留事件/追踪（Langfuse/OTEL 可选）；**跨仓通信不需要事件总线**，初期用 HTTP API + 异步任务 + 快照引用 + 轮询。

**事件模型**：

```json
{
  "event_id": "evt-...",
  "trace_id": "run-...",
  "run_id": "run-...",
  "session_id": "session-...",
  "symbol": "600760.SH",
  "stage": "thesis.dimension",
  "phase": "completed",
  "artifact_refs": ["artifact-..."],
  "summary": "盈利质量维度 contested，依赖的应收/折旧派生事实被标记口径污染",
  "payload": {},
  "timestamp": "..."
}
```

**规则**：

- AlphaBee 内部事件只做观测/触发；不要求 Apiary 订阅。
- Apiary 通过 API 拉取 manifest、状态、研究结果；通过任务 API 发起评估/回放。
- 任务可查询、提交可去重、失败可恢复；初期轮询即可，不引入 Kafka。
- 跨系统调用带 `correlation_id`；记录 `contract_version / capability_release / manifest_hash / data_snapshot_id`。
- 大文件走不可变快照（Parquet/对象存储），API 只返回清单、校验值和访问位置。

---

## 5. 关键契约

### 5.1 CapabilityManifest

```yaml
version: 1
generated_at: ...
capabilities:
  - id: margin.segment_decompose
    name: 毛利额/毛利率分解（分部口径）
    inputs: [...]
    outputs: [...]
    applicability: [...]
    min_data: [...]
    identification: partial
    output_nature: estimate
    assumptions: [...]
    sensitivity: [...]
    determinism: high
    tool: research.decompose.margin
    data_requirements: [...]
    tests: [...]
    status: ready
    owner: ...
    version: 1
```

**唯一事实源在 AlphaBee**，由代码/数据目录生成。

### 5.2 CapabilityQuery / Response

```json
// query
{
  "query_id": "q-...",
  "correlation_id": "session-...",
  "target": "600760.SH",
  "question": "毛利率下移是结构性还是交付节拍？",
  "depth": "L2",
  "required_outputs": ["volume_effect", "margin_effect", "seasonal_base"]
}

// response
{
  "status": "supported",
  "capability_id": "margin.decompose",
  "tool": "research.decompose.margin",
  "workaround": null,
  "gap_type": null,
  "request_id": null
}
```

`status` 只能是 `supported | partial | gap`；`gap_type` 只能是 `data | tool | method | knowledge`。

### 5.3 ResearchQuestion

```json
{
  "question_id": "q-...",
  "target": "600760.SH",
  "question": "Q4 毛利率同比降到多少会推翻交付节拍解释？",
  "why_it_matters": "决定 core_view 是 timing 还是 structural",
  "decision_impact": "high",
  "evidence_needed": ["2026Q4 单季毛利率", "2025Q4 同口径毛利率", "收入占比"],
  "depth_level": "L4",
  "breadth_dimension": "margin",
  "trigger_event_id": "evt-...",
  "status": "open",
  "confidence": 0.7
}
```

### 5.4 CapabilityRequest

```yaml
id: req-...
question_ids: [...]
capability_family: margin.decompose
gap_type: tool
data_requirements: [...]
current_coverage: none | partial | workaround
frequency: high
decision_impact: high
cost_estimate: ...
dependencies: [...]
acceptance: [...]
status: proposed
```

### 5.5 Claim

```text
claim_id
text
claim_type            # fact / causal / comparative / predictive / judgment
materiality           # material / background
evidence_refs[]       # artifact / observation / document
counter_refs[]
rival_explanations[]  # causal/comparative/predictive 至少 2 个；fact 不强制
warrant
qualifier
falsification[]       # observable / threshold / time_window / source / gray_zone
assumptions[]         # 依赖的参数/假设
status                # proposed / supported / contested / refuted / stale
confidence
last_updated
```

**按 claim_type 的证据要求**（不要一律套用）：

| 类型 | 必须 | 不必须 |
|---|---|---|
| **fact** | 口径、来源、期间、单位、修订状态核对 | 两个替代解释 |
| **causal** | 机制 + 至少 2 个竞争解释 + 判别证据 | — |
| **comparative** | 同口径基准 + 选择偏差检查 | 机制 |
| **predictive** | 时间窗 + 可观测证伪条件 + 基础率 | — |
| **judgment** | 标准 + 限定 + 反方 | 精确数值 |

**成功标准不只有"改变答案"**。新能力/新证据可能：

1. 改变答案（且有更好证据）；
2. 缩小不确定性（答案不变，但置信度/范围更清楚）；
3. 正确地承认无法回答（并说明缺什么）。

三者都可能是高质量研究；"改变决策"本身不等于正收益。

### 5.6 ResearchBrief

```text
question
answer
confidence
claims[]
strongest_counter
caveats[]
coverage_map[]
next_questions[]
watchlist[]
```

### 5.7 所有权与版本

| 资产 | 所有者 | 写入方 |
|---|---|---|
| 数据层 / canonical facts | AlphaBee | 数据管线 |
| 能力库 / manifest | AlphaBee | 开发 + 毕业流程（Apiary 经 PR 提交） |
| pipeline artifacts | AlphaBee | pipeline |
| **Scout 执行 / memo / 原始问题** | **AlphaBee** | Scout |
| research session / claim / brief / reconciliation | AlphaBee | Session / Reconciler |
| question bank / backlog / specs / eval records | Apiary | Apiary 角色 |
| 公共契约包 `alphabee-contracts` | AlphaBee 发布 | 双方按版本依赖 |
| 开发分支 / 候选实现 | AlphaBee 仓库（Apiary 创建分支/PR） | Builder |

**单一正式能力归属**：正式能力实现只在 AlphaBee 仓库；Apiary 保留实验、候选和评估记录，不保留第二份能力实现。

**写代码的纪律**：Builder 只能开分支/PR，不能直接写 main；AlphaBee 的测试、变异、冻结门禁照旧。

---

## 6. 执行流程

### 6.1 运行时研究流程

```text
User
  ↓
Research Session（AlphaBee）
  ↓ 需要能力
Capability Gateway
  ├─ supported → 调确定性工具 → 回答
  └─ gap → Capability Broker（Apiary）
             ├─ workaround → 部分回答 + 缺口
             └─ 记 gap → 异步能力建设
```

### 6.2 能力建设流程

```text
Scout 问题
  → Capability Auditor 判断 gap
  → gap event
  → 去重/聚类
  → 人工 + Portfolio Manager 立项
  → Builder 开发（分支/PR）
  → Verifier 回归 + 研究 A/B
  → 毕业进能力库
  → manifest 刷新
```

### 6.3 跟踪流程

```text
新数据/新财报
  → pipeline 快跑基线
  → 对比 thesis 版本 / claim 状态
  → 输出 changelog：哪条加强/削弱/证伪
  → Scout 按需补充反方/盲区
```

### 6.4 失败与降级

| 故障 | 行为 |
|---|---|
| Apiary 挂了 | runtime 继续；gap 先写本地 pending，恢复后补 |
| AlphaBee 挂了 | Apiary 继续整理 backlog/spec；回归等 CI 恢复 |
| Scout 超预算/超时 | 返回部分结果 + 未完成问题；不阻塞 |
| 重复 gap | 按 `capability_family + question_pattern` 去重 |
| manifest 漂移 | session 记录 `manifest_hash`；下一次 run 才生效 |
| 高影响分歧 | 人工裁决，不自动覆盖 |

---

## 7. 深度与广度模型

### 7.1 深度阶梯

| 层 | 回答什么 | 方法 | 输出 |
|---|---|---|---|
| **L1 事实** | 发生了什么 | 取数、核对、口径 | 数字 + 来源 + 时点 |
| **L2 分解** | 由什么构成 | 量/价/结构、杜邦、现金桥 | 分解表 + 贡献占比 |
| **L3 机制** | 为什么 | 竞争、定价、成本、会计、激励 | 因果链 + 机制证据 |
| **L4 竞争解释** | 还有别的解释吗 | 反事实、替代假设、判别证据 | 竞争解释表 |
| **L5 预期/定价** | 市场在交易什么 | 一致预期、估值反推、持仓 | 隐含假设 + payoff |
| **L6 跟踪** | 变了吗 | thesis diff、证伪触发 | changelog + 观点更新 |

每次给结论时，必须标注**当前深度**和**下一层需要什么数据**。

### 7.2 广度覆盖图

状态只用五种：**已查 / 部分 / 未查 / 数据不足 / 不重要**。

建议维度：

```text
商业模式与收入结构 / 收入质量 / 毛利率 / 现金流 /
折旧与资本开支 / 竞争与同行 / 行业与政策 /
一致预期 / 估值 / 催化剂 / 治理与关联交易
```

覆盖图的作用不是"列全"，而是让"没查"和"不重要"显式化。

### 7.3 问题质量门

Scout 的问题要过四道门：

1. **决策相关性**：不改变判断/仓位/跟踪的，丢弃。
2. **可回答性**：必须写清需要什么证据；空话丢弃。
3. **去重**：按 `capability_family + question_pattern + target` 去重。
4. **分层**：capability / data / method / knowledge；只有第一类进开发。

---

## 8. 工程落地

### 8.1 MVA（最小可行架构，6–8 周）

**只做一个研究主题**，例如"利润改善是否具有可持续性"。

- 能力库 v0：3 个能力
  1. 收入/毛利/净利分解（分部口径，标注 `identification` / `output_nature`）
  2. 口径与修订校验（`ann_date` / `update_flag` / 单位 / 期间）
  3. 同口径 peer 对比（含 stale 标记）
- Pipeline 不动；Scout 按 §4.4 的"盲读 → 落稿 → 对账"协议运行。
- 对账只做 claim/覆盖度 diff；人工维护缺口清单。
- Apiary 手动 backlog；不上五角色自动化。
- 路由器人工。

**验证方式**：不是看四个数字，而是做 A/B/C/D 四组比较（见 §9.4）。MVA 的目标是回答"在这个主题上，双路径是否比现有系统更有帮助"，不是"能力库有多大"。

**时间说明**：本仓的 DoD（测试 + 变异 + 冻结）比一般原型重；6–8 周是窄范围 POC 的现实区间，4–6 周只在数据入口、证据定位、工具都已成熟时成立。

### 8.2 阶段计划

| 阶段 | 形态 | 重点 |
|---|---|---|
| **P1** | pipeline + Scout sidecar | 先让 Scout 能独立研究 |
| **P2** | 能力库出现 | pipeline 节点改为调用能力，Scout 也用同一批能力 |
| **P3** | 毕业机制 | Scout 发现 → 能力库 → pipeline；脚手架拆除 |
| **P4** | 路由器 | 按覆盖度/不确定性选择 pipeline/Scout/并跑 |

### 8.3 代码结构建议（两仓库 + 共享契约）

```text
alphabee/                  # 仓库 A：运行时 + 正式能力
  alphabee/
    research/              # Session / Claim / Brief / Reconciler / Scout
    capabilities/          # 正式能力库（唯一归属）
    ...
  contracts/               # 或独立发布包 alphabee-contracts
  tests/

apiary/                    # 仓库 B：控制面
  intake/                  # 接收 Scout 问题、去重、归类
  audit/                   # 能力审计
  backlog/                 # 缺口 / 立项 / spec
  forge/                   # 开发编排（对 AlphaBee 开分支/PR）
  regress/                 # 回归 / 评估
  client/                  # AlphaBee API/任务客户端
```

**原则**：

- 正式能力代码只在 AlphaBee；Apiary 不保留第二份能力实现。
- Apiary 通过 `alphabee-contracts` + API + Git PR 对接，不 import AlphaBee 内部实现。
- 初期同机 / 同 Compose 部署；分成两个仓库，**不要求**两个微服务集群。
- 公共契约先由 AlphaBee 仓库发布，不急着建第三个仓库。

### 8.4 单开发者可行性

| 范围 | 可行性 | 时间 |
|---|---|---|
| MVA | 可行 | 6–8 周 |
| 能力库 + Scout 并行 | 可行，需严格切范围 | 3 个月 |
| 毕业机制 | 可行 | 6 个月 |
| Apiary 全自动 | 风险高 | 6–12 个月，不建议先做 |
| 五角色全自动流水线 | 不建议 | 容易变成基础设施项目 |

### 8.5 风险与缓解

| 风险 | 说明 | 缓解 |
|---|---|---|
| 基础设施陷阱 | 搭了 Apiary，研究产出没变 | MVA + 6–8 周内跑 A/B/C/D 验证 |
| 能力库腐化 | 接口不清、只增不减 | 毕业门槛 + 退役机制 + owner |
| 评估缺失 | 用问题数/能力数代替质量 | 决策影响指标 + 小样本人工评估 |
| 数据天花板 | 生成大量答不了的问题 | 四类缺口分流；数据缺口 park |
| 人工瓶颈 | triage/裁决占满时间 | 自动去重/打分；只升级高影响 |
| 两个真相 | pipeline 与 Scout 各说各话 | 共享 canonical facts + 对账 + 人工裁决 |
| 样本偏差 | 标的集中在少数行业 | 分层抽样（行业/商业模式/市值） |

### 8.6 分仓、部署与数据边界

> 结论：**两仓库、同环境、公共契约、单一正式能力归属。** 代码仓库、部署边界、数据边界是三个独立决定，不必一起改变。

#### 8.6.1 三个独立决定

| 决定 | 回答的问题 | 是否必须一起改变 |
|---|---|---|
| **代码仓库** | 代码在哪里维护、评审和发布 | 否 |
| **部署边界** | 是否运行在独立进程、容器或机器上 | 否 |
| **数据边界** | 谁拥有数据、谁能读写哪些数据 | 否 |

因此可以：AlphaBee / Apiary 分仓；同机或同 Compose 部署；共用 PostgreSQL/对象存储；通过 API、包、任务结果交换；用独立账号限制写权限。**分仓不意味着微服务，也不意味着复制两份数据。**

#### 8.6.2 资产所有权

| 资产 / 功能 | 所有者 | 另一方如何使用 |
|---|---|---|
| 原始资料、财务数据、规范化事实 | AlphaBee | 按快照读取 |
| 正式能力库 | AlphaBee | 通过能力接口调用 |
| Pipeline | AlphaBee | Apiary 发起评估运行 |
| **Scout 研究执行** | **AlphaBee** | Apiary 提交研究/评估任务，读取结果 |
| Session / Claim / Brief / Reconciliation | AlphaBee | Apiary 读取结果与证据 |
| 问题库、缺口、立项与开发规格 | Apiary | AlphaBee 接收已发布改进 |
| 开发实验、候选实现、验收记录 | Apiary | 通过 PR 和发布流程交付 |
| 接口契约 | 明确版本的公共接口 | 双方按版本依赖 |

#### 8.6.3 共用能力：包 / API / 任务

| 能力类型 | 推荐形式 |
|---|---|
| 纯计算（杜邦、现金桥、给定表格的分解） | 可发布的计算包 |
| 依赖数据源/凭证/运行时（查询某时点财务、拉同行并统一口径） | AlphaBee 能力接口 |
| 完整研究或历史回放 | AlphaBee 异步任务接口 |

**陷阱**：双方各自安装一个能力包，不等于调用的是同一个版本。每次实验必须记录实际执行版本。

#### 8.6.4 共用数据：接口与快照，不共享内部表

不要让 Apiary 直接依赖 AlphaBee 内部数据库表。AlphaBee 改字段、口径或清洗流程都会影响 Apiary；更严重的是，离线实验可能悄悄读到更新后的数据，失去可复现性。

| 数据规模/用途 | 访问形式 |
|---|---|
| 少量事实、元数据 | API |
| 原始文档、证据片段 | 文档引用 + 受控读取 |
| 大规模回归、批量计算 | 不可变快照（Parquet / 对象存储） |

每个实验至少固定：

```text
data_snapshot_id
as_of                    # 研究截止时点，不是财报报告期
capability_release
manifest_hash
research_config_version
model_and_prompt_version
```

快照必须落实"哪些信息当时已经公开"；仅固定财报报告期不能避免未来信息泄漏。物理上可共用存储，逻辑上分命名空间：

- AlphaBee 写正式数据与研究结果；
- Apiary 写实验、backlog 与评估记录；
- 候选实验结果与正式结果分开命名。

#### 8.6.5 Apiary 如何开发 AlphaBee 的能力

Apiary 是开发流程的组织者，正式能力代码仍属于 AlphaBee。

```text
1. Scout（AlphaBee）输出研究缺口
2. Apiary 判断是工具 / 数据 / 方法问题
3. 人工批准规格与验收标准
4. Builder 在隔离工作区检出 AlphaBee 指定提交
5. 在 AlphaBee 仓库创建候选分支并修改
6. 用固定数据快照运行测试与研究回归
7. 生成 PR，附实现、适用条件、评估结果与限制
8. 合并后由 AlphaBee 发布新版本
9. Apiary 记录发布结果，后续研究选择新版本
```

**编排代码在 Apiary，目标代码在 AlphaBee。** 不能因为 Builder 属于 Apiary，就把正式能力实现放进 Apiary；否则正式能力会出现两个归属。

#### 8.6.6 版本一致性

一个 manifest 只能描述某个版本的能力，不能独立保证该版本仍可调用。正式运行固定一个版本组合：

| 项目 | 含义 |
|---|---|
| API 契约版本 | 请求/响应兼容范围 |
| 能力发布版本 | 实际执行的能力实现 |
| manifest 哈希 | 此次看到的能力清单 |
| 数据快照 | 此次使用的数据 |
| 配置版本 | 参数、研究策略、提示词 |

初期固定一个 AlphaBee 容器镜像版本即可，不做复杂多版本路由。能力调用区分两层判断：

- **系统支持**：这个发布版本包含该能力（manifest 能回答）；
- **本次可执行**：这个标的、快照和输入满足能力要求（运行时检查）。

否则会出现"工具存在，所以问题一定能回答"的误判。

#### 8.6.7 通信机制

| 操作 | 建议 |
|---|---|
| 获取 manifest、查询状态 | HTTP API |
| 发起研究、评估、回放 | 异步任务 API |
| 读取文档与快照 | 引用 / 文件访问 |
| 接收完成通知 | 初期轮询 |
| 提交能力改进 | Git 分支与 PR |

暂时不用 Kafka 或复杂事件总线；核心要求是**任务可查询、提交可去重、失败可恢复**。

#### 8.6.8 边界健康检查

这是分仓是否成立的验收标准：

- **Apiary 停机** → AlphaBee 仍能正常研究（pipeline + Scout + reconciliation + brief）；
- **AlphaBee 停机** → Apiary 仍能整理规格与已有实验；新的研究执行等待。

如果任一方向不成立，说明边界划错了（通常是 Scout 或 brief 被放到了 Apiary）。

---

## 9. 评估与指标

### 9.1 四类指标

| 类别 | 指标 |
|---|---|
| **研究质量** | 问题有用率、决策影响率、简报可读性、追问回答率 |
| **能力库** | 能力数、复用率、毕业率、退役率、测试覆盖 |
| **回路** | 新增能力家族/批、重复率、覆盖率、开发周期 |
| **成本** | 每标的研究时间、Scout token/延迟、人工 triage 小时 |

### 9.2 收敛判据（建议）

- 连续 3 个 batch（每批 10 个标的），新增能力家族 < 2 个/批；
- top 20 能力家族覆盖 > 80% 的高价值问题；
- 重复请求率 < 15%；
- 新标的"当前可回答问题覆盖率" > 70%。

达到即宣布"核心能力收敛"，剩下的是长尾个案。

**防假收敛**：

- 加入**外部专家问题**与**未参与开发的行业/标的**；只用 Scout 自己生成的问题，覆盖率会因同源而虚高。
- 覆盖率高不等于价值高；同时看"问题有用率"和"决策质量"。

### 9.3 Kill criteria

- Scout 问题有用率长期 < 30%；
- 能力毕业率 < 20%；
- 每标的成本/人工时间超过收益；
- 3 个月内研究质量无明显改善；
- 数据缺口占 backlog 多数，且无法获得数据。

任一成立，暂停扩展，回到"pipeline + 少量 Scout + 手动能力开发"。

### 9.4 效果验证（A/B/C/D）

避免"做完后自我认可"，必须做四组比较：

| 组 | 配置 | 回答的问题 |
|---|---|---|
| **A** | 当前 Pipeline | 基线 |
| **B** | Pipeline + 同等预算的进一步研究 | 单纯加资源能改善多少 |
| **C** | Pipeline + 独立 Scout + 对账 | 双路径组织是否有额外价值 |
| **D** | C + 新毕业能力 | 能力沉淀是否产生增量收益 |

控制相同数据截止、可用来源、模型版本和总预算；记录人工审核时间。

**盲评维度**：

- 关键证据是否覆盖，引用是否真正支持主张；
- 是否处理最强反证，而不是容易反驳的弱反方；
- 竞争解释是否有区分性证据；
- 可证伪条件是否具体且有依据；
- 是否存在事实错误、逻辑矛盾、无依据的确定表达；
- 达到相同质量所需的总成本（时间 + token + 人工）。

**纪律**：

- 开发案例与验收案例分开；验收案例不参与调 prompt/规则。
- 涉及历史研究必须防止使用当时尚未公开的信息（point-in-time）。
- "改变决策"不直接记为正收益；错误地改变判断比保持正确判断更差。正确增加不确定性也是高质量研究。
- 样本量不足时只报描述统计，不做显著性宣称。

---

## 10. 架构评价

### 10.1 工程可行性

- **分阶段可行，整体复杂。** 单开发者必须严格切范围。
- 关键路径：能力契约（含可识别性）→ Scout 接地 → 对账 → 方法与评估 → 毕业机制。
- Agent1–5 的自动化排在后面，不是第一优先级。
- 真正难的不是 Agent 实现，而是数据语义、方法验收和研究评估。
- 组件难度：能力库高、Scout 中高、对账高、Apiary 中、路由器低。

### 10.2 效果预测

**会明显改善**：覆盖与一致性、广度覆盖、能力复用、研究记忆、新标的 onboarding；深度分解只在**数据可识别**的前提下改善。

**不会自动改善**：真正的 alpha、定性判断、数据缺口、研究质量的量化评估。

**三种情景**（假设执行到位）：

| 情景 | 结果 |
|---|---|
| 保守 | 简报 + 10–20 个能力；效率提升，深度有限 |
| 基准 | 30–50 个能力；Scout 发现盲区；常规研究时间下降 30–50% |
| 乐观 | 80+ 能力；形成研究复利；仍是人机协作，不是自主分析师 |

> "研究时间下降 30–50%"是**目标**，不是基准预测。前期增加 Scout、对账与人工审核后，总耗时完全可能先上升。

### 10.3 创新性

**借用的部分**：pipeline/ETL、feature store、TDD、可观测性、多 Agent 研究、explore/exploit、能力成熟度模型。

**已有先例**：Voyager 的可执行技能库、The AI Scientist 的研究闭环。"探索 → 沉淀技能 → 复用"和"Agent 驱动研究开发"本身都不新；本文不宣称这些思想原创。

**可能的差异点**：

1. 在**证据、时点、口径约束**下的能力毕业：不是所有技能都能毕业，必须先过可识别性与数据契约。
2. **方法资产与能力资产分开**：能固化的进能力库，不能固化的进方法库，必须保留未知的进 open questions。
3. 双速对账产出的是盲区/分歧/缺口，不是"谁对"。
4. 毕业后的能力必须能在**未见过的标的**上改善论证质量；否则不算成功。

**判断**：不是单点技术突破，而是**工程/组织创新**，且创新性目前是"待验证的假设"，不是既成事实。写成"自扩展的研究操作系统"是愿景，不是结论；做不成会被看成"过度设计的 multi-agent 架构"。

### 10.4 最大风险

不是技术，而是**范围蔓延、能力库腐化、评估缺失、数据瓶颈、人工瓶颈**。这五件事任何一件失控，架构都会变成负担。

---

## 11. 反模式

- ❌ 让两套 Agent 自由对话（不可审计、贵、容易跑偏）。
- ❌ 每发现一个问题就新增一个 pipeline 节点（巨石化）。
- ❌ 能力库没有 schema/测试/owner/退役机制（腐化）。
- ❌ Scout 只读 pipeline artifacts（下游化，重新被框住）。
- ❌ 把 Scout 的问题数/能力数当研究质量。
- ❌ 把"确定性"当"可识别性"：稳定输出一个无意义的数。
- ❌ 只有能力开发，没有方法沉淀与独立评估。
- ❌ 把数据缺口当能力缺口（开发永远补不完）。
- ❌ 在 MVA 之前搭全套 Apiary 自动化。
- ❌ 没有 `correlation_id` / 版本 / 回滚。
- ❌ 两个系统同时写同一张表/同一个目录（所有权不清）。

---

## 12. 开放问题

1. **数据获取策略**：哪些深度数据值得买/爬/人工？数据缺口的边界在哪？
2. **Scout 预算**：每标的的 token/时间上限？何时用 light/deep 模式？
3. **毕业裁决权**：谁批准能力进能力库？标准是否按风险分级？
4. **评估方法**：研究质量用什么指标？是否引入小样本人工盲评？
5. **仓库拆分**：何时把 Apiary 拆成独立 repo？契约稳定性的判据？
6. **人工容量**：每周能投入多少小时做 triage/裁决？超过怎么办？
7. **路由自动化**：v1 人工路由，何时可以自动？
8. **与现有 roadmap 的关系**：本文与 `AGENT_STRATEGY_ROADMAP` 的 W1–W6 如何合并实施？
9. **可识别性标准**：每个能力的 `identification` 由谁判定？判定规则如何版本化？
10. **方法库验证**：方法资产如何验收？用哪些 held-out 案例、什么判别指标？

---

## 13. 外部评审与分仓修订（v0.2 / v0.3）

> 本节记录一次外部评审（未查看代码库）的吸收结果。评审的核心判断：**值得做严格缩小范围的验证版，不宜按当前全文直接展开建设；工程可实现性高于效果确定性，应用架构价值高于底层技术创新性。**

### 13.1 接受并已修改

| 评审意见 | 本版修改 |
|---|---|
| 确定性计算 ≠ 可识别性 | 能力卡片新增 `applicability / min_data / identification / assumptions / sensitivity / output_nature`；把"量价分解"降级为"分部口径的毛利额/毛利率分解" |
| 方法与知识缺口只有分流、没有积累 | 新增"方法资产"概念（见 §13.3），与能力库并列 |
| Scout 独立性应是执行协议 | §4.4 新增"盲读 → 落稿 → 对账"三段协议；明确同模型同数据会产生相关错误 |
| "事实只有一份"过于理想 | §4.5 改为"共享 canonical facts + 保留原始观察/修订链/冲突"；新增 fact conflict registry |
| 论证要求应按 claim type 设置 | §5.5 新增按 `claim_type` 的证据要求表；fact 不强制两个替代解释 |
| MVA 范围偏大 | §8.1 缩为"一个主题 + 3 个能力 + 1 个 Scout + 最小对账 + 人工 backlog"，6–8 周 |
| 效果验证不能自我认可 | §9.4 新增 A/B/C/D 四组比较、盲评维度、开发/验收分离、point-in-time |
| "改变决策"不等于正收益 | §5.5 与 §9.4 明确：改变答案 / 缩小不确定性 / 正确承认无法回答，三者都可能是高质量 |
| 创新性表述过强 | §10.3 承认 Voyager / The AI Scientist 先例，差异点收敛到"证据/时点/口径约束下的能力毕业" |

### 13.2 对评审的修正（基于代码库事实）

| 评审的假设 | 代码库事实 | 结论 |
|---|---|---|
| Pipeline 是确定性的 | 主图多个节点调用 LLM（conflicts / verification / insights / thesis / report / review） | "确定性"过强；正确说法是"固定编排 + 有界 LLM + 确定性计算"；新增 §2.6 三层可复现性 |
| 缺少数据，量价不可识别 | `SegmentSnapshot` 已有 `revenue / cost / gross_margin / revenue_share / revenue_yoy`；但没有销量/单价/单位成本 | 量/价仍不可识别，但**分部结构/毛利率**部分可识别；能力应降级并声明 `identification: partial` |
| 事实可能存在多版本冲突 | 现有代码按 `ann_date / update_flag` **取最新**，旧版本不保留 | 问题不是"两份事实"，而是"修订链被隐藏"；修复应是保留原始观察 + 修订链 + 冲突登记，而不是简单共享一份 canonical |
| 4–6 周可做窄 POC | 本仓 DoD 含测试、变异、冻结、复审，单次改动成本高 | 6–8 周更现实；4–6 周只在数据入口/工具已成熟时成立 |
| 方法资产缺失 | `docs/design/` 与 `alphabee/skills/` 已有方法/工具素材 | 不是"没有"，而是"未结构化、不可检索、未做案例评估"；方法库可从现有文档索引起步 |

### 13.3 新增：方法资产（Method Library）

能力库固化"可计算的东西"；方法库固化"如何判断"的东西。两者并列，不合并。

```yaml
id: method.delivery_vs_competition
name: 区分交付节奏与竞争力下降
applicability: 收入确认集中、交付周期长的行业
inputs: [revenue_series, contract_liabilities, peer_revenue, segment_data]
procedure:
  - 同口径对比（季度/累计/同比）
  - 量价/结构分解（标注可识别性）
  - 竞争解释列表与判别证据
  - 证伪条件与观察窗口
failure_modes:
  - 把季节性当趋势
  - 用累计口径掩盖单季变化
  - 用同业共性解释公司特有变化
validation: 在未参与开发的案例上比较判别效果
cases: [...]
status: draft | validated | deprecated
```

**与能力库的区别**：

- 能力库：确定性计算，输入输出可精确比对。
- 方法库：研究程序与判据，输出是"如何判断"，需要案例验证，不能只用单测。
- 两者可以互相引用：方法调用能力，能力为方法提供证据。

### 13.4 修订后的最小验证

**主题**：只做一个，例如"利润改善是否具有可持续性"。

**三个能力**：

1. 收入/毛利/净利分解（分部口径，标注识别状态）。
2. 口径与修订校验（ann_date / update_flag / 单位 / 期间）。
3. 同口径 peer 对比（含 stale 标记）。

**一个 Scout**：盲读 → 落稿 → 对账。

**一个方法**：例如"区分交付节奏与竞争力下降"。

**一个评估**：A/B/C/D 四组，5–8 个案例，盲评，控制数据截止/模型/预算。

**成功标准**：在未见过的案例上，论证质量（证据覆盖、反证处理、竞争解释、可证伪性）优于基线，且总成本可接受；**能力数量不是主指标**。

**实施规范**：见 [POC_SPEC.md](../implementation/apiary/POC_SPEC.md)（案例集、能力卡、方法卡、Scout 协议、A/B/C/D 评估、验收与回滚）。

### 13.5 v0.3：分仓与 Scout 归属

本版根据分仓分析做以下调整：

1. **Scout 归 AlphaBee 运行时**：Scout 需要完整的研究状态、工具、数据与证据管理；放在 Apiary 会迫使控制面重建研究环境。Scout 对 pipeline 的独立来自研究计划、数据入口和上下文，不是仓库位置。
2. **两仓库、同环境**：AlphaBee 拥有数据、能力、Pipeline、Scout 与正式研究产物；Apiary 拥有问题/缺口管理、能力审计、开发编排、评估与毕业。初期同机/同 Compose，不要求微服务集群。
3. **公共契约**：由 AlphaBee 发布 `alphabee-contracts`（请求/响应、ID/状态/错误、manifest、快照引用、能力/任务契约）；Apiary 固定版本依赖。不急着建第三个仓库。
4. **共用能力**：纯计算用包；依赖数据/凭证/运行环境的走 AlphaBee 接口或异步任务。双方各装一个包不等于同一版本，必须记录实际执行版本。
5. **共用数据**：API / 文档引用 / 不可变快照；不依赖内部表结构。每个实验固定 `data_snapshot_id / as_of / capability_release / manifest_hash / research_config_version / model_and_prompt_version`。
6. **开发归属**：Builder 的编排代码在 Apiary，目标代码在 AlphaBee；正式能力实现只在 AlphaBee，经分支/PR 合并与发布。
7. **版本一致性**：固定 API 契约版本 + 能力发布版本 + manifest 哈希 + 数据快照 + 配置版本；能力调用区分"系统支持"与"本次可执行"。
8. **通信**：HTTP API + 异步任务 + 快照引用 + 轮询 + Git PR；不引入 Kafka。
9. **健康检查**：Apiary 停机 → AlphaBee 继续研究；AlphaBee 停机 → Apiary 继续规格与既有实验，新研究等待。任一方向不成立，说明边界划错。

详见 §8.6。

---

## 附录 A：术语表

| 术语 | 英文 | 含义 |
|---|---|---|
| 能力 | Capability | 可复用、可测试、带适用条件与可识别性声明的工具 |
| 能力库 | Capability Library | 已毕业能力的集合 |
| 能力缺口 | Capability Gap | 需要但当前不支持的确定性工具 |
| 毕业 | Graduation | 从实验实现进入能力库 |
| 对账 | Reconciliation | 比较 pipeline 与 Scout 的视图 |
| 问题库 | Question Bank | 去重后的研究问题集合 |
| Apiary | — | 能力发展系统（控制面，独立仓库） |
| Scout | — | 独立研究 Agent（AlphaBee 运行时） |
| Contracts | — | 版本化公共接口包 `alphabee-contracts` |
| System 1 | — | 固定编排 + 有界 LLM + 确定性计算 |
| System 2 | — | 开放式研究 Agent（AlphaBee 运行时） |

## 附录 B：与现有文档的关系

| 现有文档 | 关系 |
|---|---|
| `docs/roadmap/AGENT_STRATEGY_ROADMAP.md` | 本文是其 W3/W5 的架构落地：LLM 判断如何进入研究、如何度量 |
| `docs/design/LLM_AGENT_ROLE_MAP_DESIGN.md` | 本文的 Scout/Auditor/Builder/Verifier 对应其角色地图 |
| `docs/design/SINGLE_FRAME_QUALITY_DESIGN.md` | 本文的 claim/简报/论证质量是其单帧质量的上层组织 |
| `docs/design/BACKTEST_SNAPSHOT_REPLAY_DESIGN.md` | 能力库的可复现性与回放依赖其快照/版本契约 |
| `docs/design/GOLDEN_FRAME_FACTORY_EXPERIMENT.md` | 毕业回归与研究 A/B 可复用其黄金帧资产 |
| `docs/roadmap/ROADMAP.md` | 本文不替代其研究目标，只补"能力如何增长"的架构层 |
