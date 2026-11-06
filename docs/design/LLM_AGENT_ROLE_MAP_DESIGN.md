# LLM / 通用 Agent 在 AlphaBee 中的角色地图与融入设计

> 状态：设计稿（未实施）
> 定位：回答"在整个工程中，大语言模型（LLM）与通用 Agent 能如何发挥作用、如何融入流程"。
> 关联：`docs/roadmap/AGENT_STRATEGY_ROADMAP.md` W3；`docs/design/INVESTMENT_HARNESS_DESIGN.md`；`docs/design/BACKTEST_SNAPSHOT_REPLAY_DESIGN.md`；`docs/design/GOLDEN_FRAME_FACTORY_EXPERIMENT.md`。
> 纪律：本文只定义角色、边界、形态与验收；不含实现。

---

## 0. 一句话结论

LLM / 通用 Agent 在 AlphaBee 中的正确位置是**"推理与语言层"**，由**确定性 harness 包裹**：
它扩展系统"能想到什么、查到什么、怎么解释"，但不接管"算得对不对、能不能执行"。

> **LLM 扩大搜索空间与表达空间，代码守住度量与裁决。**

目标形态不是"一个全能 Agent"，而是：

```
确定性 harness（契约 / 计算 / 风控 / 回放 / 评估）
        +
多个有界 LLM 推理单元（抽取 / 研究 / 假设 / 批判 / 综合 / 叙述）
```

---

## 1. 八种 LLM 角色

先分清角色，再谈流程。同一个模型可以扮演多个角色，但每个角色的输入、输出、权限、验收不同。

| 角色 | 做什么 | 输入 | 输出 | 典型落点 | 不该做 |
|---|---|---|---|---|---|
| **Extractor** | 非结构化 → 结构化 | 财报/新闻/研报/网页 | canonical 事实 + 证据引用 | 事实抽取 | 直接写库、绕过校验 |
| **Researcher / Planner** | 决定查什么、怎么查、何时停 | 问题 + 已有证据 + 工具 | 检索计划 / 下一步动作 | 假设验证、driver research | 无边界自主运行 |
| **Hypothesizer** | 生成冲突解释与候选假设 | 上游信号/异常/冲突 | 可证伪假设 + 验证清单 | `explore_conflicts` | 把假设当结论 |
| **Verifier / Critic** | 反证、证据核对、审查 | 主张 + 证据 | 裁决 / 质疑清单 / 缺口 | `verify_hypotheses`、`review_*` | 用"感觉"代替证据 |
| **Synthesizer / Narrator** | 综合、解释、写报告 | 已结算证据 | 中心观点 / 报告文本 | `synthesize_insights`、`generate_report` | 生成脱离事实的数字 |
| **Evaluator / Judge** | 评分卡、失败挖掘、对抗样本 | 产出 + rubric | 分数 / 标签 / 案例 | 黄金帧、报告质量 | 当成 ground truth |
| **Author / Coder** | 写规则、技能、测试、映射建议 | 需求 + 领域约束 | YAML / 代码 / 测试 | `numeric-to-state` skill、规则生成 | 无审查直接合并 |
| **Simulator** | 情景推演、what-if 叙述 | 假设输入路径 | 条件投影 / 叙述 | 情景投影、压力测试 | 把合成情景当现实真值 |

---

## 2. 按流水线逐层映射

| 层 | LLM / Agent 能做什么 | 护栏 | 明确不做 |
|---|---|---|---|
| **取数与映射** | 建议字段映射、实体消歧、数据故障分诊 | 映射落 YAML、人工/测试确认 | 运行时临时猜字段 |
| **事实抽取**（`FactCollectorAgent` 8 工具） | 从公告/研报/新闻抽 canonical 事实与证据 | 结构化 schema + evidence refs + 原文回链 | 编造数字、覆盖已有事实 |
| **派生/信号/异常**（`derived_facts` / `signal` / `anomaly`） | 提出规则 YAML、阈值假设、异常解释 | 安全 AST、`validate_spec`、回测证据 | 在运行时计算/裁决 |
| **冲突与假设**（`explore_conflicts`） | 生成冲突模式、候选解释、验证计划 | 假设必须可证伪、带验证清单 | 把 provisional 升格为结论 |
| **假设验证**（`verify_hypotheses`） | 有界 planner 选工具、综合证据、判定 | 工具白名单、预算、唯证据论 | 无证据下结论 |
| **洞察综合**（`synthesize_insights`） | 提炼中心观点、反证、证伪条件 | 证据回链、降级标记 | 脱离上游证据自由发挥 |
| **论点**（`ThesisEngine`） | 定性维度判断、行业语境、情景叙述 | 数值/权重由代码定，LLM 只填语义 | 改分数/权重/仓位 |
| **报告**（`generate_report`） | 叙述、受众适配、结构 | 数字来自 canonical；claim 核对 | 生成/推断未提供的数字 |
| **审查**（`review_thesis` / `review_report`） | 批判、找漏洞、评分卡 | 确定性 gate + rubric + 人工校准 | 充当唯一裁决者 |
| **中期决策**（`midterm/`） | 证据抽取、事件分类、状态变化解释 | 数值链路（classifier/bayes/position）全代码 | 算仓位/改状态概率 |
| **跟踪监控**（`tracking/`） | 新闻/公告分诊、事件摘要、异动解释 | 触发器/阈值由代码；只读 | 触发交易/改阈值 |
| **评估与校准** | 生成对抗/合成案例、失败挖掘、judge | 标签/oracle 独立、judge 先与人校准、`synthetic` 标注 | 用 LLM 当真值 |
| **工程与运维** | 规则/技能/测试生成、PR 审查、事故复盘 | 沙箱、CI、人工合并、只读诊断 | 直接改生产契约/写库 |
| **回测/情景** | 生成情景假设、归因、叙述 | 输出冻结、注意知识泄露、L2/L3 回放 | 假装点时可复现 |

---

## 3. 通用 Agent 的六种融入形态（按自主度排序）

| 形态 | 描述 | 适用 | 风险 | AlphaBee 现状/机会 |
|---|---|---|---|---|
| **A. 工具增强的固定步骤** | DAG 某节点内 ReAct/工具调用 | 事实抽取、验证 | 低（默认首选） | 已有（FactCollector / Verify） |
| **B. 有界 Planner-Executor** | 子问题内动态规划工具序列与停止条件 | 假设验证、研究议程 | 中（需预算/停止条件） | 机会 |
| **C. Critic / Reflection 环** | 生成后自我反驳、重写 | 报告、论点审查 | 中（易空转） | 已有雏形（review gate） |
| **D. 专业化 Subagent** | 研究/风险/市场分工 | 已有 | 中（协调成本） | 已有 |
| **E. 元 Agent（工程/运维）** | 规则生成、数据故障分诊、黄金帧生成 | 开发/运维 | 中高（需沙箱+人工） | 机会（`numeric-to-state` 已是范式） |
| **F. 模拟/情景 Agent** | what-if、压力测试、合成案例 | 情景投影、评估 | 中（合成≠真值） | 机会（接黄金帧工厂） |

> 原则：**能用 A 就不用 B，能用 B 就不用 F。** 自主度越高，越需要预算、护栏、可回滚。

---

## 4. 判断权威矩阵（谁说了算）

| 结论类型 | 数值/规则权威 | LLM 权威 | 仲裁规则 |
|---|---|---|---|
| 财务数值、比率、时点 | ✅ | ❌ | 以 canonical 事实为准 |
| 异常/信号触发 | ✅ | ❌ | 规则阈值 + 证据回链 |
| 冲突是否存在 | 🟡 | ✅ | LLM 生成、验证层结算 |
| 假设成立/证伪 | 🟡 | ✅ | 唯证据论 + 置信度 |
| 中心观点/叙述 | ❌ | ✅ | 以已结算证据为边界 |
| 仓位/风控/合规 | ✅ | ❌ | 代码裁决，LLM 不得越权 |

> 任何"LLM 有最终裁决权"的结论类型，都必须有证据回链与可证伪条件；否则降级为"叙述"。

---

## 5. 硬边界与护栏（不可谈判）

1. **数值/时点/风控/合规/组合 → 代码**：LLM 不参与计算与最终裁决。
2. **结构化输出 + schema 校验 + evidence refs**：不可解析即失败，不静默降级成自由文本。
3. **工具白名单 + 沙箱 + 最小权限**：查询工具只读；写操作必须经代码与人工。
4. **外部文本一律不可信（prompt injection）**：新闻/研报/网页内容不得改变系统指令；工具结果与指令严格分区。
5. **预算/超时/重试/停止条件**：每个 agent 步骤都有上限。
6. **版本化与冻结**：model id / prompt hash / params / 输出快照；回测用冻结输出。
7. **高风险 human-in-the-loop**：仓位、合规、契约变更。
8. **可观测与评估**：trace、失败分类、黄金帧、judge 校准。

### 5.1 Prompt Injection 专项

AlphaBee 会消费新闻、公告、研报、网页等**不可信文本**。必须假设其中可能含指令注入：

- 外部文本只作为"数据"，永远不进入系统指令区；
- 工具调用参数只能来自结构化字段，不从自由文本解析后直接执行；
- 高风险动作（写库、发请求、改配置）必须经代码白名单；
- 记录 provenance（来源、抓取时点、内容哈希），便于追责与回放。

---

## 6. 与回测 / 黄金帧 / 情景投影的衔接

| 场景 | LLM 的作用 | 边界 |
|---|---|---|
| **回测** | 假设生成、失败归因、叙述 | 输出必须冻结；历史回测警惕"模型知识含未来" |
| **情景投影** | 生成情景假设与叙述 | 数值后果由代码算；合成情景标 `synthetic` |
| **按时核对** | 失败模式挖掘、归因 | 命中/未命中由规则与数据判定 |
| **黄金帧** | 批量生成对抗/合成案例、rubric judge | judge 先与人校准；不能当真值 |
| **校准** | 提出阈值假设 | 是否采纳由回测决定 |

**原则**：LLM 层做**影子评估**，数值层做**严格回测**；两者结论分开报告，不得混比。

---

## 7. 落地优先级（ROI / 风险）

**P0（已有基础，强化护栏）**
- 事实抽取结构化（schema + 原文回链）
- 冲突/假设生成 + 验证规划
- 报告叙述 + claim 核对
- 审查 critic + 确定性 gate

**P1（高杠杆）**
- 评估侧：对抗/合成案例生成、失败挖掘、judge 校准
- 工程侧：规则/技能代码生成（`numeric-to-state` 已是范式）、映射建议、测试生成
- 数据侧：故障分诊、实体消歧

**P2（有条件）**
- 有界 planner（验证/研究）
- 模拟/情景 agent
- 多智能体辩论（仅高分歧、可证伪场景）

---

## 8. 评价 LLM 组件的方法

| 维度 | 方法 | 注意 |
|---|---|---|
| 正确性 | 黄金帧（事实/论证/预测）、claim 核对 | 标签需独立 |
| 一致性 | 同输入重复、扰动鲁棒性 | LLM 非确定，需多次采样 |
| 校准 | Brier / log loss / ECE（概率类输出） | 先确认输出语义 |
| 贡献度 | 开/关对比、消融 | 区分"叙述"与"决策"影响 |
| 成本 | token/时延/调用次数 | 路由与缓存 |
| 安全 | 注入测试、越权测试、泄露测试 | 红队用例 |

**LLM-as-Judge 的使用纪律**：先用人工集校准（kappa/ICC），再放大；位置/顺序随机化；rubric 锚定；定期复核漂移。

---

## 9. 反模式（出现即纠正）

- 让 LLM 算比率、算仓位、定阈值。
- 把 LLM judge 当 ground truth。
- 叙述里的数字没有 canonical 来源。
- Agent 直接写 canonical facts / 数据库 / 生产契约。
- 无预算、无沙箱、无工具白名单的"自主"。
- 把外部新闻/研报内容当系统指令（prompt injection）。
- 用 LLM 的"知识"替代点时数据做历史回测。
- 为了"像 Agent"而把确定性链路重写成自主循环。

---

## 10. 非目标

- 不追求端到端全自主 Agent。
- 不让 LLM 接管数值、风控、合规、仓位。
- 不为"智能体化"重写已验证的确定性链路。
- 不把 LLM 输出当成可复现的数值输入（除非冻结）。

---

## 11. 验收清单（引入一个 LLM/Agent 单元前必须回答）

- [ ] 这个步骤是**开放/模糊/非结构化**的吗？（否则用代码）
- [ ] 输出能否**结构化 + schema 校验**？
- [ ] 是否有**证据回链**（artifact/observation/原文）？
- [ ] 工具是否**只读/白名单/沙箱**？
- [ ] 是否有**预算、超时、停止条件**？
- [ ] 失败时是否**显式降级**（不静默补 0/编造）？
- [ ] 是否记录了 **model / prompt / params / 输出** 版本？
- [ ] 是否有**评估方案**（黄金帧/人工/消融）？
- [ ] 是否涉及**高风险动作**？如有，是否有人工确认？

> 任一不满足，不应进入生产链路。

---

## 12. 开放问题

1. 有界 planner 的停止条件如何定义（证据充分性/预算/不确定性阈值）？
2. LLM judge 与人工的一致性阈值与复核频率？
3. 多智能体辩论的收益是否能被量化，还是只是"看起来更全面"？
4. LLM 输出进入决策的"权重"如何随证据强度变化？
5. 外部文本注入防护的测试集如何维护？

---

## 附录 A：角色 × 流水线 速查

```
取数/映射   → Extractor + Author（建议，人工确认）
事实抽取    → Extractor
派生/信号   → Author（写规则） + Critic（解释）
冲突/假设   → Hypothesizer + Researcher
验证        → Verifier + Researcher(有界 planner)
洞察        → Synthesizer
论点        → Synthesizer + Critic（语义层）
报告        → Narrator
审查        → Critic + Evaluator
中期决策    → Extractor（证据）+ Narrator（解释），数值全代码
跟踪        → Extractor（事件）+ Narrator（摘要）
评估        → Evaluator + Simulator
工程/运维   → Author + Critic（沙箱）
```

## 附录 B：与现有资产的对应

| 现有资产 | 对应角色 | 建议 |
|---|---|---|
| `FactCollectorAgent`（8 工具） | Extractor | 强化 schema 与原文回链 |
| `agents/derived_facts` + 安全 AST | Author 产出、代码执行 | LLM 只写 YAML，不运行时计算 |
| `explore_conflicts` | Hypothesizer | 强化"可证伪 + 验证清单" |
| `verify_hypotheses` | Verifier + 有界 Researcher | 引入停止条件与预算 |
| `synthesize_insights` | Synthesizer | 强化证据回链与降级 |
| `ThesisEngine` + reviewer | Synthesizer / Critic | 数值权重保持代码 |
| `generate_report` / `review_report` | Narrator / Critic | claim 核对 + rubric |
| `midterm/` 确定性引擎 | —（不由 LLM 接管） | LLM 只在证据抽取/解释 |
| `tracking/` | Extractor / Narrator | 数值触发保持代码 |
| `financial_report` 受限 agent | Researcher | 已是"有界工具"范式 |
| `numeric-to-state` skill | Author | 生成→校验→人工合并 |
| `data_fetch` | Author / Critic（运维） | 故障分诊，修复由代码执行 |
