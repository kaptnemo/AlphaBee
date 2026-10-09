# 投资 Agent 的 Harness 范式设计（Investment Harness Paradigm）

> **定位条目**：把 "harness 工程" 从 code agent 域抽象为**可判定的四器官定义**，映射到投资 Agent 域，
> 补上偏离论八维中缺失的 **V 轴（验证信号保真度）**，并据此给出 AlphaBee 的现状体检（附复现命令）与改造次序 R0–R5。
> **关联文档**：`docs/design/DEVIATION_CONTROL_FRAMEWORK.md`（偏离论八维 / 双层执行模型 / 恢复阶梯）、
> `docs/design/BEAT_MISS_LR_RESEARCH_DESIGN.md`（后验校准雏形，宏观 oracle）、
> `docs/design/SEMANTIC_JUDGMENT_CONTRACT_DESIGN.md`（受控 judge —— 本设计 §6 的降解工程实例）、
> `docs/design/INSIGHT_DEGRADATION_DESIGN.md`（四级降级 —— 本设计 §8.1 三值验收的先例）、
> `docs/design/RESEARCH_CONTINUUM_DESIGN.md`（宏观环）、
> `docs/roadmap/ENGINEERING_ROADMAP.md`。
> **状态**：📝 **设计待评审（未实施）**。§10 现状体检的每条结论均附复现命令，可逐条核对；
> 未实测的内容一律标注 "待核实"，不臆造。

---

## 1. 问题陈述

### 1.1 现象

"harness" 在 code agent 域是一个**可被开发者直接感知**的概念：给它接上测试回环，它就能自己修 bug，
收益在秒级兑现。但在投资 Agent 域，同样一套机制接上去之后，**开发者和使用者都感觉不到它**。

AlphaBee 正是这个现象的实例。2026-10 的实测体检（§10）显示：

- `alphabee/harness/` 的运行时已在 `bb80e7d` 被整体删除（净 −2041 行），只剩 4 个提示词常量；
- 该常量唯一的消费者 `review_report`（"Harness-as-library 质量门控"）在**编译图中零入边、零出边**，永远不可执行；
- 与此同时，约 1000 行测试仍在守护这个不可达节点，且 `tests/orchestrator/test_node_contracts.py:211`
  把 "review_report 当前不可达" **固化成了一条绿色断言**。

也就是说：**harness 既不在运行时起作用，也不在测试里被检出。** 但它并非"没做好"——
它是被一种**范式误迁移**做掉的：把 code agent 的 harness 形状（回路）直接搬进了一个没有 oracle 的域。

### 1.2 本文档要回答的三个问题

| # | 问题 | 对应章节 |
|---|---|---|
| Q1 | harness 范式的**领域无关定义**是什么？ | §2 |
| Q2 | 它映射到投资 Agent 时，**哪里同构、哪里断裂**？ | §3–§5 |
| Q3 | 断裂处应当如何重新设计？AlphaBee 现在该做什么？ | §6–§11 |

### 1.3 纪律声明

- §10 的每条现状结论都附**可复现命令**；未跑过的内容标注 "待核实"。
- 本文档提出的结构性改动（R1 的契约字段、R3 的检测语义）会触碰 `NodeContract` / `Issue`，
  **实施前必须走 `alphabee-pipeline-contract-steward` / `alphabee-schema-steward` skill 流程**。

---

## 2. Harness 的可判定定义

### 2.1 四器官

"Harness" 字面是马具：套在一匹有力但不可控的马上，**不增加马力，只提供约束、传导与仪表**。

抽掉领域，一个系统只有同时具备**四个器官**才构成 harness：

| # | 器官 | 职责 | 缺了它你会得到 |
|---|---|---|---|
| 1 | **动作面** Action surface | Agent 能对世界做的操作集合 | 那是 RAG，不是 agent |
| 2 | **观察面** Observation surface | 动作产生的、可被读回的世界状态 | 那是脚本 |
| 3 | **验证信号** Verification signal | 判定"这一步到底对不对"的权威信号 | **那是 pipeline**：只能往前走，无法自我纠偏 |
| 4 | **迭代控制器** Iteration controller | 依据信号重试 / 降级 / 中止 + 预算 | 那是 workflow |

### 2.2 判据

这个定义的价值在于它是**可判定的**，可以直接用来体检任何 agent 系统：

> 缺器官 3 的系统，无论节点多少、Agent 多强，都不是 harness —— 它是一条带账本的 pipeline。
> 缺器官 4 的系统，信号再好也无法转化为行为修正。

**四器官不是并列的**：器官 3 是决定性的。器官 1/2/4 决定 harness 的**吞吐**，
器官 3 决定 harness 的**存在与否**。

### 2.3 与比喻的关系

"马具"比喻容易被误读为"安全护栏"（sandbox、权限、审批）。这些确实属于 harness，
但它们只覆盖器官 1 与治理层。**harness 的核心不是限制 Agent 能做什么，而是让它知道自己做错了。**
本设计后续所有推论都从器官 3 的缺失出发。

---

## 3. 参照系：Code Agent 的 harness

### 3.1 四器官的实例

| 器官 | Code Agent 的实现 | 关键属性 |
|---|---|---|
| 动作面 | 读写文件、bash、git | 世界可被任意改写，**且完全可逆**（`git checkout` 抹掉一切） |
| 观察面 | stdout / stderr / diff | 机器产生，**无噪声** |
| 验证信号 | 编译器、类型检查、lint、测试 | **充分性信号**：便宜、快、确定、与意图高度相关 |
| 迭代器 | 红了就改，改了再跑，几轮预算 | 收敛性由信号保证 |

### 3.2 为什么它在 code 域 "可被感知"

两个原因，缺一不可：

1. **信号是充分性的**：测试绿了，代码就确实满足断言。不存在"测试全绿但代码是错的"这种结构性可能
   （只可能是"断言写错了"，那是另一个问题，且同样可以被检出）。
2. **闭环时间尺度是秒级的**：改一行 → 跑测试 → 看红绿 → 再改。开发者能在一次呼吸内完成一次控制循环。

**因此 harness 的收益在 code 域可以被即时归因**：「我什么都没干，只是接了个 pytest 回环，它就能自己修 bug 了。」
这句话之所以成立，是因为功劳可以毫无争议地记在 harness 头上。

> 记住这一点。§9 会说明，投资域里 harness 的收益**在原理上不可被即时归因**，
> 这正是"感觉不到"的根因，比"代码被注释掉了"更深一层。

---

## 4. 投资 Agent 的器官映射

### 4.1 映射表

| 器官 | Code Agent | 投资 Agent（AlphaBee 现状） | 保真度 |
|---|---|---|---|
| 动作面 | 编辑 / bash / git | 取数 / 计算 / 检索 / DeepAgents 调查 / 生成结论 | 高 |
| 观察面 | stdout / diff | `Observation` / `Artifact` / 财报原文 | **中 —— 数据本身不可信** |
| 验证信号 | 编译 + 测试 + lint | 见 §5.1 阶梯 | **决定性差距** |
| 迭代器 | 重试到绿 | 恢复阶梯（`RECOVERY_TIERS` 0–5）/ 四级降级 / 预算 | 中 |
| 记忆 | 文件系统 / git | `Artifact` / 偏离账本 / midterm 快照 | 高 |
| 治理 | 权限门 / 人工审批 | 决策留痕 / 不可逆动作 | **投资域权重更高** |
| 遥测 | trace / token / cost | 偏离账本 / 后验 beat-miss | 高 |

### 4.2 同构良好的三项

**动作面、记忆、遥测**同构得相当好 —— 因为它们与"世界是文件系统还是财务报表"无关。

- 动作面：AlphaBee 已有 `collectors/`（多源取数）、`agents/derived_facts/`（计算）、
  `agents/explore_conflicts` / `verify_hypotheses`（DeepAgents 开放式调查）；
- 记忆：`Artifact` 列表 + `alphabee/task_records/` + `alphabee/midterm/` 快照；
- 遥测：`alphabee/tracking/ledger.py` + `services/telemetry.py`。

**乐观推论**：投资 Agent 的 harness 有大约一半的工程量可以复用 code agent 的成熟做法，不需要重新发明。

### 4.3 决定性差距

唯一但致命的断裂是**器官 3**。下一节专门处理。

---

## 5. 关键断点：充分性 vs 必要性

### 5.1 验证信号阶梯 L1–L6

投资域能造出来的验证信号，按性质与代价分层如下：

| 层 | 信号 | 性质 | 保真度 V | AlphaBee 对应实现 |
|---|---|---|---|---|
| **L1** 确定性 | schema 校验、canonical 字段名、单位/口径、公式 AST 求值 | 必要性 | ≈1.0 | `alphabee/schemas/`（10 域 / 210 canonical 字段）、`agents/derived_facts/`（21 条 YAML 规则 + 安全 AST 求值） |
| **L2** 数值自洽 | 三表勾稽、跨期口径一致、异常值边界 | 必要性 | 高 | `derived_facts` 规则 + `numeric_consistency` 指标 |
| **L3** 跨源一致 | tushare vs akshare vs 财报原文同一数字 | 必要性 | 中高 | `alphabee/adapters/` + `cross_source_consistency` 指标 |
| **L4** 结构完整 | 证据引用非空、artifact 契约、无未结算高严重度冲突 | 必要性 | 中 | `node_contracts.py`（16 条契约）、`detectors.py`、`review_thesis` |
| **L5** LLM 审议 | critic / evaluator / thesis reviewer | **无 ground truth** | **≈0.2** | `gates.py::_llm_assessment`、`agents/thesis/reviewer.py` |
| **L6** 后验对拍 | 预测 vs 实际、beat/miss、校准曲线 | **唯一充分性近似** | 需样本量 | `docs/design/BEAT_MISS_LR_RESEARCH_DESIGN.md`、`midterm/bayes.py`、`tracking/` |

### 5.2 结论一：投资 harness 只能强制必要条件

> **通过 L1–L5 的全部闸门，结论仍然可能是错的。这是结构性事实，不是工程水平问题。**

因为在投资域里，`数据自洽 ∧ 跨源一致 ∧ 结构完整 ∧ 读起来合理` 并不蕴含 `判断正确`。
L1–L5 全部是**必要性条件**：违反它们一定有问题，满足它们不保证没问题。

这一点必须在设计上被正面接受，而不是试图用更强的 LLM 审议去"补上充分性"—— 见 §9.3。

### 5.3 结论二：验收输出从二值变三值

Code harness：**绿 / 红**（编译要么过要么不过）。
Investment harness：**通过 / 降级带标注 / 阻断**。

> **"证据不足" 是一个合法的交付状态。**

这不是妥协，而是领域正确性。AlphaBee 已经在无意中实现了这个三值模型 ——
`degraded` / `fallback_tier` / `degradation_reason`（`orchestrator/contracts.py::InsightArtifact`）、
`Issue.severity` / `status` / `resolution_evidence`、insight 四级降级阶梯。
**这些机制在 code agent 里全都不需要存在**，它们的必要性完全来自 §5.2。

---

## 6. 核心工程命题：把判断降解成算术

### 6.1 命题

既然充分性信号不可得，投资 harness 的核心工程任务就不是"验证结论对不对"，而是：

> **尽可能多地把 "判断" 降解成 "算术"。**

每当你把一句 "这家公司护城河强" 降解成
`毛利率行业分位 + 连续三年超额毛利 + 研发费用率趋势 + 客户集中度`，
你就把一个**无 oracle 的判断**搬到了**有 oracle 的层**。

降解后的部分满足 §5.2 的必要性判据，可被 L1–L3 自动检查；
**剩下的残差才是真正需要判断的部分**，也是 L5 审议层唯一该管的范围。

### 6.2 AlphaBee 已有的降解器（盘点）

| 降解器 | 落点 | 降解了什么 |
|---|---|---|
| Canonical Schema | `alphabee/schemas/*.yaml`（10 域 / 210 字段）+ `alphabee/adapters/` | 把"各数据源字段名"降解成"内部标准字段名"（观察面规范化） |
| Derived Facts 引擎 | `alphabee/agents/derived_facts/rules/*.yaml`（21 条）+ 拓扑 DAG + 安全 AST 求值 | 把"财务健康度"降解成"可复算的比值与阈值判定" |
| Signal 引擎 | `alphabee/agents/signal/rules/*.yaml` | 把"风险信号"降解成"带等级的规则命中" |
| 冲突生命周期 | `nodes/conflicts.py` + `nodes/verification.py`（provisional/verified/partial/rejected/unknown） | 把"有矛盾"降解成"矛盾的状态机" |
| 受控 judge | `docs/design/SEMANTIC_JUDGMENT_CONTRACT_DESIGN.md` | 把"语义词表判读"降解成"结构化契约优先 + judge 兜底" |
| 贝叶斯证据更新 | `alphabee/midterm/bayes.py`（log-odds） | 把"这条证据有多重要"降解成"似然比参数" |

**核心判断**：`agents/derived_facts/` 是整个项目里**最 "harness" 的模块**。
它不是分析模块，它是一条把判断编译成算术的流水线 —— 也就是 §6.1 命题的直接实现。

### 6.3 推论：预算应当投在降解上，而不是投在回环上

由 §5.1 的 V 值可立即得到一条判据：

> **不要把预算花在 V≈0.2 的层上做回环，而要把预算花在把 V≈0.2 的判断往 V≈0.6/1.0 降解上。**

这条判据同时解释了两个既有的工程决策（§10.4、§12）：

- `3de7066` 停用 `review_report` 回环；
- `config.yaml` 中 `deviation.detection.llm_detectors: false  # v1 恒 false（§16 反模式：不做 LLM 检测器）`。

---

## 7. 补轴：V（验证信号保真度）

### 7.1 偏离论八维的缺口

`docs/design/DEVIATION_CONTROL_FRAMEWORK.md` §1 定义了八维：H / B / O / R / A / S / U / C。
其中：

> **O（Observability）** = 发生偏离后**能被检测到的概率**。

但 **检测到 ≠ 检测对了**。O 维刻画的是"检测的灵敏度"，没有刻画"检测的正确性"。
缺的正是：

> **V（Verification fidelity）** = 检测信号与"真的错了"之间的一致性。

### 7.2 V 的定义与刻度

对任一检测器 $d$，定义：

$$
V(d) = P(\text{真的偏离} \mid d \text{ 报警}) \times P(d \text{ 报警} \mid \text{真的偏离})
$$

即**精确率与召回率的联合刻画**。工程上取粗刻度即可：

| V | 含义 | 判据 | 典型实现 |
|---|---|---|---|
| ≈1.0 | 报警 ⇒ 真的错 | 可机械复算，无解释空间 | 编译、schema 校验、AST 求值 |
| ≈0.6 | 报警 ⇒ 很可能是真的错，但漏报多 | 有客观锚点，但覆盖不全 | 数值勾稽、跨源一致 |
| ≈0.2 | 报警 ⇒ 只是"读起来不对" | 无 ground truth、同源、不可回放 | LLM 审议、语义 judge |

### 7.3 V 轴的直接判据

V 一旦显式化，若干设计决策立刻有判据：

1. **回环只允许挂在 V ≥ 0.6 的层上**（否则重试不收敛，见 §8.2）；
2. **V≈0.2 的层只能做 "标注" 与 "升级到人工"，不能做 "阻断"** ——
   用不可靠的信号阻断交付，会把噪声放大成产出损失；
3. **V 应作为检测器的登记属性进入契约**，与 `recovery_ladder` 同级（见 R1）。

---

## 8. 由断点派生的四个设计差异

### 8.1 差异一：验收输出三值化（见 §5.3）

**设计含义**：harness 的产出物不只是"通过/不通过"的布尔值，还必须包含**置信度标注与降级标记**，
且这些标记必须**向下游传导**（`degradation_reason` / `fallback_tier` 已实现）。

**反面**：`docs/design/DEVIATION_CONTROL_FRAMEWORK.md` §2 把"静默劣化
（无 issue 但 artifact 空洞，下游一路带伤）"定义为微观环的危险方向 —— 这正是三值模型要消灭的对象。

### 8.2 差异二：迭代器的职责从"重试到绿"改为"降级 + 标注"

**原因**：无 oracle 时重试不保证收敛，且成本高。
降级并如实标注，比再赌一次更理性。

**投资域特有的额外风险**：无 oracle 的回环是一个**无锚定的优化回路**。
若 critic 与 writer 同源（§9.3 实测：确为同源），回路会向"critic 的偏好"漂移，
而不是向"正确答案"漂移 —— 多轮之后可能**越改越迎合 critic、越偏离事实**。
这正是 §12 反模式 A-2。

**设计含义**：微观环内的回环次数应**恒为 0 或 1**，且只在 V ≥ 0.6 的判据上触发。
`NodeContract.max_retries`（0 = 禁止 Tier 4）已经是正确的机制表达，问题在于它被绑在了一条死边上（§10.2）。

### 8.3 差异三：观察面不可信 → 血缘层成为必需器官

Code agent 的 `pytest` 输出可信，不需要怀疑工具本身。
投资 Agent 的数据是**对抗性**的：复权方式、口径变更、财务重述、停牌借壳、数据源打架、财务造假。

**设计含义**：需要一个 code 域不存在的器官 —— **数据血缘与来源治理层**。
AlphaBee 的对应实现是 `alphabee/schemas/INDEX.yaml`（canonical 字段单一事实来源）、
`alphabee/adapters/{tushare,akshare,eastmoney}/`（源 → canonical 映射）、
`ObservationFreshness`（时效分类）、以及 `data-source-contract` skill 要求的"冒烟验证后写调用代码"纪律。

### 8.4 差异四：动作不可逆 → 治理层权重远高于 code

`git checkout` 能撤销一切；一笔仓位不能，一份发给客户的报告也不能。

**设计含义**：治理层（决策留痕、人工审批、不可逆动作的显式确认）在投资域是**一级器官**，
而不是 code agent 里的辅助设施。AlphaBee 的雏形：`alphabee/midterm/` 决策层、
`alphabee/task_records/`（run 级记录）、`tracking/ledger.py`（偏离账本）、
以及 `midterm` 节点的 "决策摘要完整落日志、暂不进报告" 的分离设计。

---

## 9. 双时间尺度：真正的 oracle 在 run 之外

### 9.1 微观环的能力上界

Code agent 的 oracle 是**局部的、当下的**（在同一个 run 内闭环）。
投资域不存在这样的 oracle，因此：

> **单次 run 内的 harness（微观环）在原理上不可能自证结论正确。**
> 它能做的只有三件事：强制必要条件（L1–L4）、降级并标注、留下可审计的痕迹。

### 9.2 宏观环 = 真 oracle

唯一真正的 oracle 是**后验统计**（校准），它跨越很多次 run 和很长时间：

```text
run 产出结论 → 时间推进 → 事实兑现/落空 → 对拍 → 校准曲线 → 修正 L5 层的判据与阈值
```

**因此投资 harness 天然是双时间尺度（对应偏离论的微观环/宏观环），且一半的 harness 不在 pipeline 里。**

AlphaBee 的宏观 oracle 雏形已经存在：

| 组件 | 落点 | 状态 |
|---|---|---|
| 证据级经验 LR 标定（用历史频率替换手工阈值 5%/15%） | `docs/design/BEAT_MISS_LR_RESEARCH_DESIGN.md` | 📝 设计完成，未实施 |
| 贝叶斯证据更新 | `alphabee/midterm/bayes.py` | ✅ 已实现 |
| 状态机 + 快照 + 五层差分 | `alphabee/midterm/` | ✅ 已建模，缺调度 |
| 跨 run 调度器 | `alphabee/tracking/scheduler.py` | ✅ 已实现 |
| thesis 版本 append-only（防重写） | `alphabee/midterm/models.py::ThesisVersion` | ✅ 已实现 |

### 9.3 同源 LLM 不构成独立 oracle（实测）

**实测**：`alphabee/utils/llm.py:218` 的 `create_chat_model(component, **kwargs)` 中，
**所有 component 共用同一个 `settings.llm.model`**；`component` 仅用作 callback 标签与 `lru_cache` key，
不参与模型选择：

```python
def create_chat_model(component: str, **kwargs: Any) -> ChatOpenAI:
    callbacks = list(kwargs.pop("callbacks", []) or [])
    callbacks.append(TokenUsageCallbackHandler(component))
    tags = list(kwargs.pop("tags", []) or [])
    tags.append(f"component:{component}")
    return ChatOpenAI(model=settings.llm.model, ...)   # ← component 不影响 model
```

因此 critic / evaluator 与 writer **是同一个模型、只换提示词**。
L5 层的 "独立审查" 实际上**不独立** —— 它是**自我一致性检查（self-consistency）**：

> 能发现**结构性缺失**（漏了章节、结论无出处、口径打架未披露），
> 不能发现**事实错误**（因为它没有外部锚点）。

这解释了 `gates.py` 的指标为何全是结构性的
（`evidence_coverage` / `grounding_score` / `cross_source_consistency` / `issue_handling`），
以及 `gates.py` 为何要用 `_deterministic_assessment` 先跑一遍确定性判定。

`docs/design/BEAT_MISS_LR_RESEARCH_DESIGN.md` §0 边界表独立得出了同一结论：

> "LLM 判断的校准 —— C 类，需在线运行积累（**同源 LLM 判断无法离线回放**）"

**设计含义**：L5 层的价值上界是"结构性完整性"。若某条质量要求必须由 L5 承担，
正确的动作是**把它降解到 L1–L4**（§6.1），而不是加强 L5 的提示词。

### 9.4 对 "感觉不到" 的完整解释

到这里可以完整回答 §1.1 的现象：

| 层次 | 原因 |
|---|---|
| 表层 | `review_report` 节点被注释掉（`3de7066`），技术门控零执行 |
| 中层 | 残存的 harness 骨架是**记账层而非裁决层**（`services/detection.py` 明文"除新增 issues 外逐字段不变"） |
| **深层** | **投资 harness 的正确产出不是"更好的这一份报告"，而是"跨很多份报告之后的校准曲线"。单次使用天然感受不到 —— 它的收益以季度为单位兑现。** |

深层原因才是根本的：**即使把 `review_report` 重新接上，你依然"感觉不到 harness"**，
因为你感觉到的只会是"这次报告好像严了一点"，而不是"我的判断力变准了"。
后者只有宏观环能给。

---

## 10. AlphaBee 现状体检（实测）

### 10.1 四器官打分

| 器官 | 评分 | 依据 |
|---|---|---|
| 1 动作面 | ✅ 齐备 | `collectors/` 多源取数、`derived_facts` 计算、DeepAgents 开放式调查 |
| 2 观察面 | ✅ 强 | `Artifact` / `Observation` / `Decision` / `Issue` 结构化货币贯穿全流程 |
| 3 验证信号 | ❌ **近乎为零** | 见 §10.2：L5 的落点不可达，L1–L4 只做记账 |
| 4 迭代控制器 | ⚠️ 存在但失联 | `recovery.py` 的 `RecoveryDecision` / 恢复阶梯只被 `gates.py` 的死路径消费 |

**结论**：按 §2.2 的判据，AlphaBee 当前**不是 harness，是一条带账本的 pipeline**。

### 10.2 证据：harness 遗迹三层

**第 1 层 —— 包还在，实体没了**

```bash
ls alphabee/harness/                      # 仅 __init__.py + prompts.py
ls alphabee/harness/__pycache__/          # runtime.cpython-313.pyc (49K) / state_compressor.cpython-313.pyc (35K)
git show --stat bb80e7d | tail -3         # 14 files changed, 675 insertions(+), 2041 deletions(-)
```

`bb80e7d`（`refactor(orchestrator): 接入报告质量门控并清理 harness`）删除了：

```
alphabee/harness/runtime.py            | 1089 ------------------
alphabee/harness/state_compressor.py   |  894 --------------
alphabee/harness/utils.py              |   12 -
```

被删的是**一个完整的通用多智能体运行时**：`HarnessState` TypedDict、
planner/reporter/critic/evaluator 四个 LLM 节点工厂、`_route_after_critic`（受控回环）、
`HarnessStateCompressor`（按角色裁剪上下文，artifact 分 full/summary/meta-only 三档）、
`HarnessRuntime.recover_state / replay / diff_stored_snapshots`、`diff_harness_states`。

**第 2 层 —— 唯一幸存消费者是死节点**

```bash
grep -rn "alphabee\.harness" --include=*.py alphabee/ tests/ | grep -v __pycache__
# → 仅 1 处：alphabee/orchestrator/gates.py:30  (EVALUATOR_NODE_PROMPT)
```

`gates.py::review_report` 是 "Harness-as-library 质量门控" 的唯一落点。
但编译后的图中它**零入边、零出边**：

```bash
HOME=$(pwd)/tmp/home .venv/bin/python -c "
from alphabee.orchestrator.agent import alphabee_agent as g
for e in g.get_graph().edges: print(e.source, '->', e.target)
"
# generate_report -> record_deviations      ← 直接跳过 review_report
# review_report 与 review_report -> record_deviations 均不出现（源不可达，边被剪掉）
```

原因在 `alphabee/orchestrator/agent.py:508-520`：

```python
# 暂时不启用 report quality gate 回环
# _graph.add_edge("generate_report", "review_report")
# _graph.add_conditional_edges("review_report", route_after_report_review, {...})
_graph.add_edge("generate_report", "record_deviations")   # ← 实际生效
```

停用于 `3de7066`（`chore(orchestrator): 暂时停用报告质量门回环`）。
`route_after_report_review` 仍定义在 `gates.py:746`，但 `agent.py` 的导入已被清理。

**两处文档失实**（应随 R0 一并修正）：
`agent.py:14` 的 docstring 与 `docs/guide/ARCHITECTURE.md:33` 仍写着
`review_report — Harness-as-library quality gate with optional rewrite`。

**第 3 层 —— 剩下的骨架是"记账"而非"裁决"**

`alphabee/orchestrator/services/detection.py:12-13` 明文规定：

> 检测器自身异常由 `run_detectors` 吞掉（warning + 跳过），节点返回的 `update`
> 除新增 `issues` 外**逐字段不变**；无偏离时返回原 `update` 对象本身。

即：**今天残存的 harness 骨架记录偏离，但从不改变输出。** 这就是"使用端感觉不到它"的物理原因。

### 10.3 证据：测试在守护不可达节点

```bash
wc -l tests/orchestrator/test_report_review_gate.py tests/orchestrator/test_recovery.py
#   339 tests/orchestrator/test_report_review_gate.py
#   627 tests/orchestrator/test_recovery.py
```

`test_report_review_gate.py` 的 8 个用例（含 1 个参数化用例）全部直接把 `review_report(state, {})`
当函数调用并断言通过 —— **门控逻辑是完整且正确的，只是图里连不上**：

```bash
HOME=$(pwd)/tmp/home .venv/bin/python -m pytest \
  tests/orchestrator/test_report_review_gate.py tests/orchestrator/test_node_contracts.py -q
# 52 passed in 4.27s
```

约 1000 行测试在守护一个不可达节点，且**全绿**。

更值得注意的是测试套件**知道**这件事，并把它固化成了断言（`test_node_contracts.py:211`）：

```python
def test_graph_edge_reader_sees_edges_pruned_by_compiled_graph():
    """契约校验必须用 builder 的边：编译图会剪掉源不可达的边（本条证明该选择有实效）。"""
    # §14.1-D 记在案的坑：review_report 当前不可达，它到 sink 的边只存在于 builder 声明里
    assert ("review_report", "record_deviations") in builder_edges
```

配套的 `test_node_contracts_cover_every_pipeline_node` 断言 `len(NODE_CONTRACTS) == 16` 且包含 `review_report`
（实测 `validate_contracts()` 返回 `[]`，全绿）：

```bash
HOME=$(pwd)/tmp/home .venv/bin/python -c "
from alphabee.orchestrator.node_contracts import NODE_CONTRACTS, NODE_BUDGETS, validate_contracts
print(len(NODE_CONTRACTS), len(NODE_BUDGETS), validate_contracts())"
# 16 2 []
```

> **治理缺口**：契约覆盖断言检查的是"**已声明**"，不是"**可达**"。
> 因此它能查出"某个节点没写契约"，查不出"某个节点写了契约却连不上"。
> 绿了，但绿在一个孤岛上。

### 10.4 结论

1. harness 的**定义**在 AlphaBee 里被实现了一半（器官 1/2/4），**器官 3 缺位**；
2. 唯一实现器官 3 的代码路径（L5 门控）被注释掉，且其测试与契约治理均无法检出；
3. 残存机制的语义已从"裁决"退化为"记账"，因此在运行时可感知度为零；
4. `NODE_BUDGETS` 实测仅 2 条（`collect_raw_facts` / `generate_report`），
   其中 `generate_report` 的回环预算绑在死边上，属于失联资产。

---

## 11. 改造次序 R0–R5

> 按 "零风险 → 高收益 → 结构性" 排序。每条给出目标 / 动作 / 落点 / 验收判据 / 风险。
> R1 与 R3 触碰契约结构，**必须走 contract-steward skill 流程**。

### R0 —— 清理失实与遗迹（零风险，立即可做）

| 项 | 内容 |
|---|---|
| 目标 | 消除"文档说它有、实际没有"的认知负担 |
| 动作 | ① 修正 `agent.py:14` docstring 与 `docs/guide/ARCHITECTURE.md:33` 对 `review_report` 的描述，标注"当前未接线"；② 清理 `alphabee/harness/__pycache__/` 的 `runtime` / `state_compressor` 残留 `.pyc`（源码已删，字节码仍在）；③ 明确 `alphabee/harness/prompts.py` 的定位（保留为 L5 提示词资产并登记，或一并移除） |
| 落点 | `alphabee/orchestrator/agent.py`、`docs/guide/ARCHITECTURE.md`、`alphabee/harness/` |
| 验收 | 文档不再声称 `review_report` 在流水线中执行；`git grep runtime.cpython` 无残留 |
| 风险 | 无 |

### R1 —— V 轴显式化（结构性，收益最高）

| 项 | 内容 |
|---|---|
| 目标 | 让 "这个检测器能不能信" 成为可查询、可断言的契约属性（§7） |
| 动作 | 在 `NodeContract` 的检测器登记上增加保真度属性（如 `DetectorFidelity` 枚举，刻度 ≈1.0 / ≈0.6 / ≈0.2），并扩展 `validate_contracts()` 断言"每个已登记检测器都有 fidelity 且与其实现层一致" |
| 落点 | `alphabee/orchestrator/node_contracts.py`、`alphabee/orchestrator/detectors.py` |
| 验收 | ① 新增 CI 断言：`validate_contracts()` 覆盖 fidelity 完整性，现有 16 条契约全部补登；② 同时锁死 T4⟺budget 不变量（附录 C 已核实项 C-3）；③ 补一条**可达性**断言：`NODE_CONTRACTS` 中的每个节点必须能从 `START` 到达（关闭 §10.3 的治理缺口） |
| 风险 | 契约结构变更影响所有消费点（`gates.py` / `services/detection.py` / `recovery.py` / 多份测试）→ 走 contract-steward |
| 依赖 | R0 |

> **实施提示（③ 的陷阱）**：可达性断言在**今天**会失败 —— `review_report` 正是那个不可达节点。
> 这是断言的目的，但要按正确顺序落地：**先由 R2/R3 决定 `review_report` 的最终归宿**
> （接线为闸门 / 正式注销契约），再启用该断言。不要为了让断言变绿而草率接上回环 —— 那会违反 §8.2。
> 若最终决定注销，`NODE_CONTRACTS` 应从 16 条降为 15 条，
> 需同步修改 `test_node_contracts_cover_every_pipeline_node` 中 `!= 15` 的断言与文档 §14.2-A 口径。

### R2 —— 回环维持停用，但把确定性指标改为节点级闸门

| 项 | 内容 |
|---|---|
| 目标 | 恢复器官 3 的部分能力，同时**不引入无锚定回环**（§8.2） |
| 动作 | ① `review_report` 回环**继续停用**，直到 L5 有独立信号源（当前不可能，§9.3）；② 把 `gates.py` 的确定性指标（`EvaluateMetrics`，`alphabee/core/schemas.py:422`，14 字段）前移为**节点级后置条件**：在 `review_thesis` 出口检查 `evidence_coverage` / `cross_source_consistency` / `issue_handling`，不达标则降级标注而非重写 |
| 落点 | `alphabee/orchestrator/agent.py`、`alphabee/orchestrator/gates.py`、`alphabee/orchestrator/node_contracts.py` |
| 验收 | 确定性指标成为闸门（产生 Issue / 降级标记），且**全程不触发 LLM 重写**（断言 `report_review_round` 恒为 0） |
| 风险 | 中 —— 需要 `report_review_round` / `_report_rerun_decision` 的预算语义与 R2 新语义对齐，避免两套计数 |
| 依赖 | R1 |

### R3 —— 检测语义从"观测"升格为"裁决"（V≥0.6 的子集）

| 项 | 内容 |
|---|---|
| 目标 | 让 harness 的产出**进入行为**，而不只是进入日志（§10.2 第 3 层） |
| 动作 | 为检测器增加 `on_violation` 策略：对 V ≥ 0.6 的检测器，blocker 级命中时**让节点走 `recovery_ladder`（降级 + 标注）而非静默 append issue**；V≈0.2 的检测器**永不允许阻断**（§7.3 判据 2） |
| 落点 | `alphabee/orchestrator/services/detection.py`（当前明文"逐字段不变"）、`alphabee/orchestrator/node_contracts.py` |
| 验收 | 构造 blocker 级偏离，断言节点产出带 `degraded` / `fallback_tier` 标记且下游可见；V≈0.2 检测器的阻断尝试被拒绝 |
| 风险 | 高 —— 这是"从记账到裁决"的语义变更，需保证 fail-open 纪律不被破坏（配置缺失时仍应降级为记账） |
| 依赖 | R1 |

### R4 —— 宏观环补后验对拍（真 oracle）

| 项 | 内容 |
|---|---|
| 目标 | 建立**唯一**的充分性近似信号（§9.2） |
| 动作 | ① 定义"预测可对拍化"契约：每个 run 落一条机器可判的预测记录（标的 / 方向 / 时间窗 / 判据 / 置信度），复用 `alphabee/task_records/`；② 实施 `docs/design/BEAT_MISS_LR_RESEARCH_DESIGN.md` 的证据级 LR 标定，替换 `alphabee/midterm/evidence_rules.py` 的手工阈值（5% / 15%）；③ 用 `alphabee/tracking/scheduler.py` 做定时对拍 |
| 落点 | `alphabee/task_records/`、`alphabee/midterm/evidence_rules.py`、`alphabee/tracking/` |
| 验收 | 积累 N 个 run 后能产出校准表（预测置信度分桶 vs 实际命中率），并回写修正 L5 层阈值 |
| 风险 | 中 —— 对拍口径（时间窗、基准、剔 β）设计不当会产出**假 oracle**，比没有更危险；建议先只做 beat/miss 这一条最干净的通道 |
| 依赖 | 无（可与 R1–R3 并行） |

### R5 —— 让 harness 的产出可见

| 项 | 内容 |
|---|---|
| 目标 | 解决 §9.4 的深层问题：让跨 run 的收益至少**部分**可被感知 |
| 动作 | ① run 级质量摘要（证据完备度 / 未结算冲突数 / 降级标记）进**报告尾部**与 CLI footer；② 偏离账本按 symbol 汇总，进入公司跟踪视图 |
| 落点 | `alphabee/orchestrator/reporter.py`、`alphabee/apps/cli/renderer.py`、`alphabee/apps/web/` |
| 验收 | 交付物中可直接看到本次 run 的必要条件通过情况；跨 run 视图可看到趋势 |
| 风险 | 低 —— 但需注意不要把这些指标**当作结论质量分**呈现（V≈0.6 的指标不能冒充充分性信号） |
| 依赖 | R2、R3 |

---

## 12. 反模式清单

| # | 反模式 | 为什么错 | 本文档对应 |
|---|---|---|---|
| **A-1** | 把 LLM 审议当成独立 oracle | critic 与 writer 同源（§9.3 实测），无外部锚点，只能发现结构性缺失 | §9.3 |
| **A-2** | 在 V≈0.2 的判据上做无界回环 | 无锚定优化回路会向 critic 偏好漂移，可能越改越迎合、越偏离事实 | §8.2 / §7.3 |
| **A-3** | 用 LLM 检测器做偏离检测 | 已被项目自身识别：`config.yaml` `llm_detectors: false # §16 反模式` | §6.3 |
| **A-4** | 把必要条件指标当作质量分呈现 | V≈0.6 的指标全绿 ≠ 结论正确，会造成过度自信 | §5.2 / R5 |
| **A-5** | 只做检测记账、不做行为裁决 | 运行时零可感知度，harness 退化为日志 | §10.2 第 3 层 |
| **A-6** | 契约治理只断言"已声明"、不断言"可达" | 死节点可以在全绿测试中长期存活 | §10.3 |
| **A-7** | 用同源 LLM 对拍自身的历史判断 | 不可离线回放，无独立信号 | §9.3 |
| **A-8** | 追求"单次 run 内自证结论正确" | 结构上不可能，是 §5.2 的直接否定 | §9.1 |

---

## 附录 A：复现命令

```bash
# A-1 harness 遗迹现状
ls -la alphabee/harness/ alphabee/harness/__pycache__/
git show --stat bb80e7d | tail -5
grep -rn "alphabee\.harness" --include=*.py alphabee/ tests/ | grep -v __pycache__

# A-2 编译图中的实际接线（review_report 应为孤岛）
HOME=$(pwd)/tmp/home .venv/bin/python -c "
from alphabee.orchestrator.agent import alphabee_agent as g
for e in sorted(g.get_graph().edges, key=lambda e: e.source):
    print(f'{e.source} -> {e.target}')"

# A-3 契约与预算实际条数
HOME=$(pwd)/tmp/home .venv/bin/python -c "
from alphabee.orchestrator.node_contracts import NODE_CONTRACTS, NODE_BUDGETS, validate_contracts
print('contracts:', len(NODE_CONTRACTS))
print('budgets  :', sorted(NODE_BUDGETS))
print('validate :', validate_contracts())"

# A-4 死节点的测试规模
wc -l tests/orchestrator/test_report_review_gate.py tests/orchestrator/test_recovery.py

# A-5 同源 LLM 证据（component 不参与模型选择）
sed -n '218,232p' alphabee/utils/llm.py

# A-6 canonical 字段规模
grep -n "field_count" alphabee/schemas/INDEX.yaml | awk -F'field_count: ' '{s+=$2} END {print "TOTAL:", s}'
```

> **注**：A-2 / A-3 / A-6 需要可写的 `HOME`。Tushare 的 `set_token` 会向 `$HOME/tk.csv` 写文件，
> 只读 `HOME` 会导致 `OSError: [Errno 30] Read-only file system`。

---

## 附录 B：术语对照

| 术语 | 定义 | 出处 |
|---|---|---|
| Harness | 具备 §2.1 四器官的闭环控制系统 | §2 |
| 四器官 | 动作面 / 观察面 / 验证信号 / 迭代控制器 | §2.1 |
| 充分性信号 | 满足即蕴含正确的信号（code 域的测试） | §5.2 |
| 必要性信号 | 违反一定有问题、满足不保证没问题（投资域的 L1–L4） | §5.2 |
| **V（验证信号保真度）** | 检测信号与"真的错了"之间的一致性，**偏离论八维之外的补充轴** | §7 |
| 降解 | 把无 oracle 的判断改写为可机械判定的算术 | §6.1 |
| 微观环 / 宏观环 | 单次 run / 跨 run 跟踪，投资 harness 的双时间尺度 | §9、`DEVIATION_CONTROL_FRAMEWORK.md` §2 |

---

## 附录 C：待核实项

以下内容未能在本次体检中实测，标注为待核实，实施前需补：

| # | 待核实 | 建议动作 |
|---|---|---|
| C-1 | `review_report` 的回环保环在恢复接线后对报告质量的实际影响（是否有 A/B 数据） | 恢复前先跑离线对拍，用 R4 的对拍口径度量 |
| C-2 | `alphabee/harness/prompts.py` 中 planner / reporter / critic 三个提示词是否还有复用价值 | 全仓检索后决定保留或删除（R0） |
| C-4 | L5 层各判据的实际 V 值 | 需 R4 的宏观对拍数据支撑，当前只有先验估计（§7.2） |

### 已核实：C-3（原 "待核实"，本次体检关闭）

原疑虑：`NODE_BUDGETS` 仅 2 条，是否覆盖不全？**实测结论：不变量当前成立。**

```bash
HOME=$(pwd)/tmp/home .venv/bin/python -c "
from alphabee.orchestrator.node_contracts import NODE_CONTRACTS, NODE_BUDGETS
mr = {k for k,v in NODE_CONTRACTS.items() if v.max_retries > 0}
t4 = {k for k,v in NODE_CONTRACTS.items() if 4 in v.recovery_ladder}
print('max_retries>0 :', sorted(mr))      # ['collect_raw_facts', 'generate_report']
print('ladder has T4 :', sorted(t4))      # ['collect_raw_facts', 'generate_report']
print('NODE_BUDGETS  :', sorted(NODE_BUDGETS))
print('T4 无预算     :', sorted(t4 - set(NODE_BUDGETS)))   # []
"
```

即 `max_retries > 0` ⟺ `4 ∈ recovery_ladder` ⟺ `∈ NODE_BUDGETS`，三者严格一致。

**但该不变量目前只靠"人工保持"**，没有 CI 断言守护。建议随 R1 一并加一条：

> 断言：`{k for k,v in NODE_CONTRACTS.items() if v.max_retries > 0} == set(NODE_BUDGETS)`

否则未来新增一个带 Tier 4 的契约时会**静默失去预算保护** —— 而这正是 `generate_report`
当前的状态（它的预算绑在一条死边上，见 §10.4 第 4 条）。
