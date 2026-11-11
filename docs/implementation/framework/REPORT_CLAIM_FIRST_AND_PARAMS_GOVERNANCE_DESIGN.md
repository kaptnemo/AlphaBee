# 报告主张化与参数治理设计（Claim-First Report & Parameter Governance）

> **状态**：设计稿（2026-11-08），未实施。
> **范围**：两条**不依赖语料、快照平台、第二评审**即可启动的线——
> ① 报告质量线（Q0 基线/规范 → Q1-lite 减法 → Q2 claims + 门禁）；
> ② 常量治理线（P0 盘点 → P1 注入 → P2 指纹/影子）。
> **本文采用的三项决策**（对应待拍板三问，推荐值写死；要改只影响对应小节）：
> 1. 报告目标形态 = **投资备忘录**（3–5 条 claim，主文 6.5–10k 字，明细进附录）；
> 2. 参数面 = **`params/registry.yaml` + `config.yaml` 的 `parameters.overrides`**（不是把常量直接塞进 config）；
> 3. 迁移方式 = **三态 flag 灰度**（`report.mode: legacy | memo_lite | memo_claims`，默认 `legacy`），旧报告保留为 A/B 基线。
> **关联**：`docs/design/SINGLE_FRAME_QUALITY_DESIGN.md`（质量模型/DoD 上位文档）、`docs/roadmap/REPORT_QUALITY_FIX_ROADMAP.md`（伪风险传导链修复，与本文不重叠）、`docs/design/GOLDEN_FRAME_FACTORY_EXPERIMENT.md`（本文的指纹与评估集是其种子）、`docs/design/BACKTEST_SNAPSHOT_REPLAY_DESIGN.md`（冻结帧/回放同源思路）、`docs/roadmap/ROADMAP.md`（行为变更登记）。
> **Skill**：`alphabee-pipeline-contract-steward`（§3.3/§3.4 的契约传播）、`alphabee-schema-steward`（§4 的字段与常量治理）。

---

## 0. 一页摘要

### 0.1 三个结论

1. **报告不是"没有论证"，是"论证被非论证块淹没 + 没有被组织成可反驳的单元"。** 600760 报告 42,605 字符中，13 个章节只有 21,178（49.7%），系统问题清单 12,861（30.2%）、中期决策控制台块 7,880（18.5%）；主文里的"投资观点展开"是一段 3,168 字符的连续论述，读者必须自己挖出主张。**只改 prompt 不够，必须同时改渲染顺序、折叠非论证块。**
2. **报告形状是 prompt 设计的。** `REPORT_GENERATOR_PROMPT`（`alphabee/orchestrator/prompts.py:8`）明确要求"风险信号逐条分析""逐条分析检测到的数据矛盾""各维度投资论点"；13 个 section 与引擎输出一一对应。因此第一步是做减法 + 换骨架，不是加评分卡。
3. **常量面不是 169 个，是 15 个。** `classifier_v2.EvidenceParams` 已存在但 `classifier_dispatch.classify_state` 调用时**没有传 `params`**（`classifier_dispatch.py:115`）；`score_engine` 的 F/E 决策常量仍是模块级、无参数入口。工作量是"15 个常量 + 2 处接线"，不是"全量迁移"。

### 0.2 关键数字（本机实测，2026-11-08）

| 指标 | 值 | 含义 |
|---|---|---|
| 5 份报告总字符 | 18,694 – 45,590 | 体量差 2.4 倍，说明没有统一长度预算 |
| 13 节正文占全文 | 49.7% – 74.1% | 剩下 26%–50% 是系统问题/中期块 |
| 明细节占正文 | 56.7% – 63.9% | 与 prompt 的"逐条"要求一一对应 |
| 600760 数字 token | 1,846 个（43.3 个/千字） | "数据淹没"的量化形态 |
| 600760 issue 数 | 34 条（2 critical / 8 high / 23 medium / 1 low），合计 12,808 字符 | 单条最长 1,213 字符，存在重复句拼接 |

### 0.3 首两周动作（详见 §6）

- **W1**：Q0 基线 + 规范（2 天）；Q1-lite prompt + 渲染重排 + 非论证块折叠（2 天）；P0 常量盘点 + `params/registry.yaml` v0（2 天，与 Q 并行）。
- **W1 末 go/no-go**：3 个标的、同 payload 的 legacy vs memo 对比——主文降幅 ≥50% 且至少 2/3 标的"更清楚"才进 Q2；否则先修上游 insight/thesis，不急着上 claim 框架。
- **W2**：Q2 `claims` schema + 4 个确定性门禁（4 天）+ 10 标的盲评（2 天）；P1 `ScoreParams` + dispatch 接线 + 默认行为不变测试（4 天）。
- **W3**：P2 行为指纹 + 影子对比（3 天）；报告阈值登记进同一 registry；收口。

### 0.4 明确不做

全量 169 常量迁移、rubric 20 维、kappa/第二评审、自动调参、全量快照、黄金帧工厂、重写 issue 生成器、重排主图。

---

## 1. 基线：现状到底长什么样（Q0 输入）

### 1.1 测量口径

- 字符数 = Python `len()`（中文按 1 字符计），基于 `data/reports/*.md` 的 UTF-8 文本；
- "13 节正文" = 13 个 `##` 章节正文之和，不含标题行、头部元信息、页面分隔；
- "叙事节" = executive_summary / investment_viewpoint / scenario_analysis / key_metrics / falsification_conditions / risks / disclaimer；
- "明细节" = signal_analysis / anomaly_detection / conflict_analysis / dimension_analysis / company_track / review_findings；
- "系统问题" = `## 系统问题` 到文件尾（600760 另含 `══` 分隔的中期决策块，已单独拆出）；
- 数字 token = 正则 `\d+(?:\.\d+)?%?`（粗口径，用于密度比较，不做语义校验）。

复现命令见 §9.4；Q0 应把该口径固化成 `scripts/report_baseline.py`，输出 CSV + 上述表。

### 1.2 五份报告成分（实测）

| 报告 | 总字符 | 13 节正文 | 叙事节 | 明细节 | 明细/正文 | 系统问题 | 中期块 | 非论证占比 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 300504.SZ 2026-10-30 | 45,590 | 32,468 | 13,108 | 19,360 | 59.6% | 12,467 | — | 27.3% |
| **600760.SH 2026-11-01** | **42,605** | **21,178** | 9,167 | 12,011 | 56.7% | **12,861** | **7,880** | **48.7%** |
| 兴森科技 2026-10-17 | 18,694 | 12,651 | 5,061 | 7,590 | 60.0% | 5,681 | — | 30.4% |
| 海康威视 2026-10-22 | 42,921 | 30,311 | 10,944 | 19,367 | 63.9% | 12,000 | — | 28.0% |
| 生益科技 2026-10-19 | 39,291 | 29,103 | 11,349 | 17,754 | 61.0% | 9,527 | — | 24.2% |

> 合计值与总字符的差额来自标题、头部元信息与空行，不影响结论。

### 1.3 600760 逐节解剖

| 章节 | 字符 | 章节 | 字符 |
|---|---:|---|---:|
| 数据矛盾 | 3,902 | 风险信号 | 1,542 |
| 投资观点展开 | 3,168 | 主要风险 | 1,531 |
| 维度分析 | 2,691 | 情景分析 | 1,296 |
| 审查发现 | 1,968 | 核心指标 | 1,183 |
| 公司赛道与对标组 | 1,058 | 可证伪条件 | 898 |
| 核心观点摘要 | 889 | 勾稽关系异常 | 850 |
| 免责声明 | 202 | **系统问题 + 中期块** | **20,741** |

系统问题 34 条的严重度分布：**critical 2 / high 8 / medium 23 / low 1**；单条消息最长 1,213 字符，中位数 257 字符，合计 12,808 字符。其中 `driver_profile_research_claim_uncovered` 的消息把同一条 `claim.snippet` 按每个数字重复拼接（`alphabee/domain_context/research.py:1610-1626`），是"issue 体积膨胀"的直接根因之一。

### 1.4 五个发现

**F1｜最大的"淹没源"不是 13 节，是系统问题与中期块。** 600760 有 30.2% 的字符是 issue 清单、18.5% 是中期决策控制台块；这两块对读者理解论证的边际价值低，却排在正文之后、占据一半篇幅。只压缩 13 节，文件仍接近 30k 字符。

**F2｜明细节占正文 57%–64%，与 prompt 的"逐条"要求一一对应。** `signal_analysis` 要求"每条信号与 core_view 的关系"、`conflict_analysis` 要求"每个冲突 + 每个假设 + 每个关系"、`dimension_analysis` 要求"每个维度四字段 + 与 core_view 一致性"。这不是模型写坏，是规格要求的。

**F3｜论证骨架已经存在，但没有"主张边界"。** `executive_summary` 里有 core_view、central_tension、正反证据、三情景、证伪条件；`investment_viewpoint` 用（一）–（五）五段展开。质量不低，但**主张埋在连续段落里**：没有 Claim / Grounds / Warrant / Rebuttal / Qualifier / Falsification 的显式结构，读者无法逐条检验，也无法逐条打分。

**F4｜数字密度 23–43 个/千字。** 600760 42.6k 字符含 1,846 个数字 token。高密度本身不是错，但在"没有 claim 过滤"的线性叙述里，数字取代了判断，读者看到的是研究底稿。

**F5｜两个渲染面（终端/markdown）各自维护一套 13 节映射。** `alphabee/apps/cli/task_report.py:41`（markdown）与 `alphabee/apps/cli/renderer.py:743`（终端）必须同时改；现有测试钉住 13 个标题。任何布局重构都必须先抽出共享的 layout 模块，否则必然漂移。

### 1.5 Q0 产出：《报告论证规范》草案（一页）

**实质主张（material claim）定义**（沿用 `SINGLE_FRAME_QUALITY_DESIGN.md` §3，并给出可操作判据）：

> 一句话同时满足以下三条，计为 1 条实质主张：
> ① 含方向/判断词（回升、下移、低估、高估、成立、不成立、领先、落后、不可持续…）；
> ② 含数字、比较或时间窗（至少一项）；
> ③ 不是对输入字段的同义复述（把"毛利率 10.07%"改写成"毛利率承压"不算新主张）。

**论证六要素（Toulmin 落位）**：

| 要素 | 报告中的位置 | 硬要求 |
|---|---|---|
| Claim | 每条主张的首句 | 可反驳；不含"可能/或许"搪塞 |
| Grounds | 依据段 | ≥1 条带数字与口径的证据；或显式标 `inferred` |
| Warrant | 推理段 | 说明"依据为何支持主张"的机制，不得只写"综上" |
| Rebuttal | 反证段 | 至少一条最强反证，并说明为什么没推翻主张 |
| Qualifier | 限定段 | 置信度（高/中/低）+ 适用时间窗 |
| Falsification | 失效条件 | 四字段齐全：指标 / 阈值 / 时间窗 / 数据来源 |

**长度预算（建议初值，全部登记为 `placeholder`）**：

| 单元 | 预算 | 说明 |
|---|---:|---|
| 一页结论 | ≤800 字 | 1 主张 + 3 证据 + 2 反证 + 1 失效条件 |
| 核心主张 | 3 条 × ≤500 字 | 每条六要素齐全；Q2 上限 5 条 |
| 情景与触发 | ≤1,200 字 | base/bull/bear 各 ≤300 字 + 触发条件 |
| 我们可能错在哪里 | ≤1,200 字 | 集中回应反证，不写模板风险词 |
| 观察清单 | ≤1,000 字 | 表格 ≤6 行：看什么/何时变/什么情况下错/来源 |
| 关键指标 | ≤800 字 | 6–8 行表格 + 一段"支撑/挑战" |
| 主文合计 | ≤10,000 字 | 硬上限（门禁 G4） |
| 附录 | 不限 | 但必须与主文不重复论证 |

**好坏对照（直接用 600760 的真实内容改写）**：

> **主张**：中航沈飞当前的核心矛盾不是收入下滑，而是毛利率中枢从 13.7% 下移到 10% 附近；若 2026Q4 单季毛利率不能回到 12% 以上，"交付节拍错配"的解释不成立。
> **依据**：前三季毛利率 10.07%（同比 −1.76pp）；Q3 单季收入 +23.29% 而毛利率仅 9.63%；7 家对标中 5 家毛利率同比上升。
> **推理**：若是固定成本摊薄，收入放量应带来毛利率回升；量增而毛利降，指向定价或型号结构位移。
> **反证**：Q3 单季归母 +144%、扣非 +40.7%，2026 年零存货计提——恶化尚未兑现。
> **限定**：中等置信，适用于 2026Q4–2027Q1。
> **失效条件**：2026 年报 Q4 单季毛利率 ≥12%，且合同负债回补至 44 亿元以上；来源：2026 年报 + 资产负债表。

**反模式清单**（评审时逐条对照）：形容词堆砌、信号堆砌、无源断言、单源支撑、无 Rebuttal、不可证伪、因果跳跃、概率误用（把 softmax 权重说成概率）。

### 1.6 Q0 脚本规格（`scripts/report_baseline.py`）

```text
输入：--task-records data/task_records（默认） 或 --reports data/reports
输出：CSV（每报告一行）+ Markdown 表 + 可选 JSON
列：symbol, date, total_chars, body13_chars, narrative_chars, detail_chars,
    issue_chars, midterm_chars, detail_ratio, nonarg_ratio, numeric_tokens,
    numeric_per_kchar, issue_count, issue_max_msg_chars
附加：--layout legacy|memo（memo 时按 §3.1 的新口径重算 main_chars）
纪律：纯函数、禁 LLM、只读；口径必须与门禁 G4 的 count_main_chars 共用同一实现。
```

Q0 另外产出 `docs/implementation/report/REPORT_ARGUMENT_SPEC.md`（一页），内容即本节 §1.5 的固化版；**不改代码**。

---

## 2. 目标与非目标

### 2.1 目标骨架（投资备忘录）

```text
1. 一页结论（≤800 字）                 ← executive_summary
2. 核心主张 1–3（每条 ≤500 字）        ← investment_viewpoint（Q1）/ claims[]（Q2）
   每条：Claim / Grounds / Warrant / Rebuttal / Qualifier / Falsification
3. 情景与触发（≤1,200 字）              ← scenario_analysis
4. 我们可能错在哪里（≤1,200 字）         ← risks（语义重定义）
5. 观察清单（≤1,000 字）                ← falsification_conditions（语义重定义）
6. 关键指标（≤800 字）                  ← key_metrics
────────────────────────────────────
附录 A 风险信号明细                      ← signal_analysis
附录 B 勾稽异常明细                      ← anomaly_detection
附录 C 数据矛盾明细                      ← conflict_analysis
附录 D 维度评分明细                      ← dimension_analysis
附录 E 公司赛道与对标组                  ← company_track
附录 F 审查发现                          ← review_findings
附录 G 系统问题清单（全量 N 条）          ← TaskRecord.issues
附录 H 中期决策明细                      ← TaskRecord.midterm
免责声明
```

### 2.2 长度预算

- **主文** ≤10,000 字（硬门禁）；目标区间 6,500–8,000 字（prompt 预算加总）。**这是对你给的 8–10k 的收紧**：预算是上限，写满约 6.6k；若盲评显示"信息不足"，再放宽到 8–10k，而不是先写长再砍；
- **附录** 不限，但要求"不与主文重复论证"（附录只放明细/清单/表格，不再写与 core_view 的关系）；
- **系统问题** 主文只保留摘要（≤5 条、每条 ≤160 字），全量进附录 G；
- **中期块** 主文只保留头部一行，全量进附录 H。

### 2.3 非目标（本轮明确不做）

| 不做 | 原因 | 何时再做 |
|---|---|---|
| 169 个常量全量迁移 | 大部分是结构/技术常量，迁移只增加间接层 | 15 个跑通后按需扩展 |
| rubric 20 维 / 第二评审 / kappa | 单人评审先看方向；一致性没有第二人无从谈起 | 有第二评审后 |
| 自动调参 | 语料 1 帧，任何"校准"都是过拟合 | 真实帧 ≥50 |
| 全量快照 / 黄金帧工厂 | 先有评估集，再谈工厂 | Q2 评估集稳定后 |
| 重写 issue 生成器 | 属上游数据契约修复，不阻塞形状验证 | 作为 P1 后续工单（§8 R4） |
| 重排主图 / 新增节点 | 本轮只动 reporter、CLI 渲染面、params 包 | 另立一期 |

---

## 3. 报告线设计

### 3.1 章节映射与渲染顺序

| 旧 section key | 新位置 | 新标题 | 预算（字） | 内容要求 |
|---|---|---|---:|---|
| （新增）`company_overview` | 主文 0（摘要之前） | 公司概况 | 450 | 面向零基础读者：做什么/卖给谁、主导业务线与收入结构、行业与上市地、规模锚点；只写有据可查的事实 |
| `executive_summary` | 主文 1 | 一页结论 | 800 | 1 主张 + 3 证据 + 2 反证 + 1 失效条件 |
| `investment_viewpoint` | 主文 2 | 核心主张 | 1,600 | Q1：2–3 条 claim 段落；Q2：改由 `claims[]` 渲染 |
| `scenario_analysis` | 主文 3 | 情景与触发 | 1,200 | base/bull/bear 各 ≤300 + 触发条件 |
| `risks` | 主文 4 | 我们可能错在哪里 | 1,200 | 反证集中回应（≥3 条"如果…则…"） |
| `falsification_conditions` | 主文 5 | 观察清单 | 1,000 | 表格：看什么 / 何时变 / 什么情况下错 / 来源 |
| `key_metrics` | 主文 6 | 关键指标 | 800 | 6–8 行表 + 一段支撑/挑战 |
| `signal_analysis` | 附录 A | 风险信号明细 | 不限 | 保留逐条，去掉"与 core_view 关系"的强制要求 |
| `anomaly_detection` | 附录 B | 勾稽异常明细 | 不限 | 同上 |
| `conflict_analysis` | 附录 C | 数据矛盾明细 | 不限 | 同上 |
| `dimension_analysis` | 附录 D | 维度评分明细 | 不限 | 同上 |
| `company_track` | 附录 E | 公司赛道与对标组 | 不限 | 原样 |
| `review_findings` | 附录 F | 审查发现 | 不限 | 原样 |
| `TaskRecord.issues` | 主文摘要 + 附录 G | 审查与数据问题（摘要）/ 系统问题清单 | 摘要 800 | 主文只列 critical/high 前 5 条 |
| `TaskRecord.midterm` | 附录 H | 中期决策明细 | 不限 | 完整控制台块 |
| `disclaimer` | 主文尾 | 免责声明 | 200 | 原样 |

**渲染顺序唯一来源**：新增 `alphabee/apps/cli/report_layout.py`，定义 `OVERVIEW_SECTIONS`、`MAIN_SECTIONS`、`APPENDIX_SECTIONS`、`OVERVIEW_TITLES`、`MAIN_TITLES`、`APPENDIX_TITLES`、`LEGACY_ORDER` 与 `ordered_blocks(sections, layout)`；`task_report.py` 与 `renderer.py` 都只消费该模块。这是修 F5 的最小改动。

### 3.2 Q1-lite：只做减法与重排（W1，2 天）

Q1-lite 的目标不是"写短"，而是**验证形状假设**：同一批证据，压到主文预算后，读者是否觉得更清楚。它不做 claim schema、不做门禁、不做 A/B 统计。

#### 3.2.1 Prompt v2（memo_lite）改动清单

新增 `REPORT_GENERATOR_PROMPT_MEMO`（与现有 `REPORT_GENERATOR_PROMPT` 并存，按 `report.mode` 选择）。保留：输入 JSON 结构说明、insight 主线原则、driver_profile 优先、数字不得编造、不做买卖建议、口径标注。**改写**：§"报告格式"与硬约束。

主文段落的指令（全文见附录 A）：

| 章节 | 新指令（要点） | 删除的旧要求 |
|---|---|---|
| company_overview（新增） | ≤450 字，渲染在摘要之前：做什么/卖给谁、主导业务线与收入结构、行业与上市地、规模锚点；事实只取自 `company.profile` / `company_track`，缺失即省略 | — |
| executive_summary | ≤800 字；固定四段：主张 / 3 条证据（带数字与口径）/ 2 条反证 / 1 条失效条件 | "2–3 段话，含置信度与关键不确定性" |
| investment_viewpoint | 输出 2–3 条主张；每条用固定六段：主张/依据/推理/反证/限定/失效条件；每条 ≤500 字；只引用支撑该主张的 2–4 条证据 | "逐一讨论 supporting_evidence（至少2条）和 counter_evidence（至少2条）" |
| scenario_analysis | ≤1,200 字；三情景各 ≤300 字 + 触发条件；不再回扣 materiality_rank 每项 | 各情景"回扣 main_driver 与 materiality_rank 关键变量" |
| risks | 改题为"我们可能错在哪里"；集中回应 counter_evidence（≥3 条），每条写"如果…则…"；禁止模板风险词 | "综合 thesis.primary_risks、review.blocking_issues、counter_evidence" |
| falsification_conditions | 改题为"观察清单"；表格 ≤6 行：看什么/何时变/什么情况下错/数据来源；失效条件必须四字段 | 按 kind 分组 + disconfirm 澄清文案（保留 kind 语义即可） |
| key_metrics | 6–8 行表 + 一段"这些指标如何支撑/挑战主张" | 不变（仅压长度） |
| 附录六节 | **保留现有逐条格式**，但删除"与 core_view 关系"的强制要求；明确"附录是明细，不与主文重复论证，主文用『见附录 X』引用" | — |

> 关键设计：**不是让 LLM 自由写短，而是给它固定骨架 + 字数预算 + 章节职责**。附录保留明细，主文只留论证。

#### 3.2.2 渲染重排

`task_report.build_report_markdown` 与 `renderer.render_final_report` 改为调用 `report_layout.ordered_blocks(sections, layout)`：

- `layout="legacy"`：完全维持现状顺序（13 节），零行为变化；
- `layout="memo"`：公司概况（摘要之前）→ 主文 6 节 → 附录 A–F → 免责声明；
- 附录标题统一 `## 附录 A 风险信号明细`，不使用旧标题（避免读者误以为仍是正文）；
- 终端渲染对附录只打印标题 + 前 800 字（可配置），完整内容在 markdown 文件。

#### 3.2.3 系统问题折叠（确定性，无 LLM）

`task_report.py:150-158` 现在无条件输出全部 issue。改为：

```python
def render_issue_digest(issues, *, max_items=5, max_chars=160) -> tuple[str, str]:
    """返回 (主文摘要, 附录全量)。纯函数，无 LLM。"""
    critical_high = [i for i in issues if i.severity in ("critical", "high")]
    head = critical_high[:max_items]
    digest = "\n".join(f"- [{i.severity}] {i.category}: {_clip(i.message, max_chars)}" for i in head)
    digest += f"\n\n共 {len(issues)} 条问题（critical {n1} / high {n2} / medium {n3} / low {n4}），完整清单见附录 G。"
    appendix = "\n".join(f"- [{i.severity}] {i.category}: {_clip(i.message, 600)}" for i in issues)
    return digest, appendix
```

- 主文摘要上限：5 条、每条 160 字；
- 附录 G：全量、每条 ≤600 字（原文保留在 `TaskRecord.issues`，报告里标注"完整 message 见任务记录"）；
- 这样 600760 的 12,861 字符 → 主文 ~300 字 + 附录 ~6,000 字（34 条 × 中位 257，受 600 上限约束）。

#### 3.2.4 中期决策块折叠

`task_report.py:160-183` 现在把 `render_midterm_decision_summary` 的完整控制台块（7,880 字符）输出到正文尾部。改为：

- 头部元信息保留现有 `**中期决策**: 状态 S5 置信度 0.61 证据 16 条 · 顺序 tef · formula v2 …` 一行；
- 完整块移到 `## 附录 H 中期决策明细`（memo 布局）；
- legacy 布局保持现状（回归钉）。

#### 3.2.5 唯一 schema 变更：`ReportOutput.layout`

严格"零 schema"无法同时满足两件事：① `--task-report` 对旧记录可复现地重渲染；② A/B 归档后可复核布局。若布局只由当前配置决定，翻转配置会让旧记录"换一种排版重渲染"，A/B 证据不可回溯。

因此 Q1-lite 增加**一个 additive 字段**：

```python
# alphabee/agents/schemas.py::ReportOutput
layout: Literal["legacy", "memo"] = "legacy"
```

- 默认 `"legacy"`：旧 JSON 反序列化、旧记录重渲染、旧测试全部不受影响；
- `generate_report` / `build_deterministic_report` 按 `report.mode` 写入；
- 渲染器读 `report.get("layout", "legacy")`，**不读当前配置**；
- 回滚 = `report.mode: legacy`（不写新字段，旧路径逐字节不变）。

> 这是本文对"Q1-lite 不改 schema"的唯一偏离，理由与代价都在此明示。若坚持零字段，可退化为"布局由配置决定"，代价是旧记录重渲染不保真、A/B 必须固定配置并只归档文件；两种都可实施，本文采用前者。
>
> **为什么不用 `report.claim_first: bool` 单开关**：Q1-lite（无 claims、只换布局/prompt）与 Q2（有 claims、门禁）是两个不同的 schema 面，回滚粒度也不同；一个布尔只能表达两态，无法同时覆盖"memo 布局但无 claims"的灰度阶段。三态 `report.mode` 更准确；若你坚持布尔，等价映射是 `false=legacy`、`true=memo_claims`，Q1-lite 另需一个布局开关。

#### 3.2.6 Payload 捕获与离线 A/B（Q1-lite 的第二块）

A/B 的可比性取决于**同一份输入**。现有 `TaskRecord` 只存 `report_raw`（LLM 输出），不存 `ReportGenerationPayload`，重跑会混入数据时点与上游变化。因此：

1. 新增 `ReportSettings.capture_payload: bool = False`；`generate_report` 在开关打开时把 payload 写到 `<data_root>/report_payloads/<run_id>.json`（fail-open，写失败只 warning）；
2. 把 `generate_report` 的模型调用抽成 `_generate_report_value(payload, mode, rewrite_reason, prior_report) -> dict`，LLM 重试/解析/降级逻辑不变；
3. 新增 `scripts/report_ab.py`：

```text
python scripts/report_ab.py --payload data/report_payloads/<run>.json \
    --modes legacy,memo_lite --out tmp/ab/<symbol>/
输出：<mode>.json（ReportOutput）、<mode>.md（渲染后 markdown）、metrics.json（主文字符/数字数/章节数）
纪律：同一 payload、同一模型参数；两种模式各跑 1 次；不写 data/ 以外的路径。
```

这样 3 个标的只需各跑一次完整 pipeline（捕获 payload），之后可以反复离线对比，不重复取数。

#### 3.2.7 Q1-lite 验收（W1 末 go/no-go）

| 项 | 判据 | 工具 |
|---|---|---|
| 主文降幅 | 3 个标的中 ≥2 个主文字符降幅 ≥50%（试点 = 600760.SH / 300504.SZ / 002415.SZ，基线正文均 ≥21k；基线 = 13 节正文） | `report_ab.py` metrics |
| 主文预算 | 3 个标的全部 ≤10,000 字 | 同上 |
| 明细不丢 | 附录 A–F 均非空，且数字 token 数不低于基线的 80% | 同上 |
| 读者判断 | 你（单评审）对 3 组盲评"哪份更清楚"至少 2 组选 memo，且无一组认为"信息更少" | 人工 |
| 回归 | 全量测试通过；legacy 渲染逐字节不变 | `pytest` + 指纹 |

**决策规则**：以上全过 → 进 Q2；主文降幅达标但"更清楚"不达标 → 问题在主张质量而非长度，先修 insight/thesis 的 claim 化，不上 schema；降幅不达标 → prompt 骨架或模型服从度有问题，先调 prompt，不加门禁。

#### 3.2.8 已知限制

- LLM 可能不遵守字数预算：Q1-lite 只在 metrics 里报告，不阻断；Q2 由 G4 报警。
- 附录仍是 LLM 写的散文，总长度不会显著下降（附录不限是显式选择）；把附录改为确定性表格是 P2/Q3 的优化，不在本轮。
- issue 消息本身的重复拼接（§1.3）未修，只是被折叠进附录；修它属上游工单（§8 R4）。

---

### 3.3 Q2：claim schema（W2，2 天）

#### 3.3.1 数据模型（additive，旧 JSON 全部可 `model_validate`）

```python
# alphabee/agents/schemas.py（契约所有者）

class ReportClaimEvidence(BaseModel):
    """一条可核对的依据。ref 优先填 artifact/observation id；也可填 canonical 字段名+口径。"""
    statement: str = ""   # 这条证据说明什么
    ref: str = ""         # artifact/obs id，或 canonical 字段名（如 gross_margin 20260930）
    value: str = ""       # 显示值（含单位，如 "10.07%（同比 -1.76pp）"）
    source: str = ""      # 数据来源（如 "income 20260930" / "fina_indicator 20260930"）


class ReportClaim(BaseModel):
    id: str
    text: str                                   # 主张：一句可反驳的话
    grounds: list[ReportClaimEvidence] = Field(default_factory=list)
    warrant: str = ""                           # 推理：依据为何支持主张
    rebuttal: str = ""                          # 反证：最强反证 + 为什么没推翻
    qualifier: str = ""                         # 限定：置信度 + 适用时间窗
    falsification: list[FalsificationCondition] = Field(default_factory=list)  # ≥1 条
    material: bool = True                       # 是否实质主张（默认是）
    inferred: bool = False                      # 无 ref 时必须为 true
    claim_type: Literal["fact", "causal", "comparative", "predictive", "judgment"] = "judgment"


class ReportOutput(BaseModel):
    ...
    layout: Literal["legacy", "memo"] = "legacy"     # Q1-lite 已加
    claims: list[ReportClaim] = Field(default_factory=list)  # Q2 新增
```

**`FalsificationCondition` 扩展**（`alphabee/agents/schemas.py:106`，additive；旧的纯字符串/`condition` 反序列化不变）：

```python
class FalsificationCondition(BaseModel):
    condition: str = ""
    kind: str = "disconfirm"
    direction: str = ""
    metric: str = ""      # 指标（如 "Q4 单季毛利率"）
    threshold: str = ""   # 阈值（如 ">=12%"）
    window: str = ""      # 时间窗（如 "2026 年报"）
    source: str = ""      # 数据来源（如 "2026 年报 + 资产负债表"）
```

> **为什么必须扩展**：现有 `what_would_change_my_mind` 只有自由文本 `condition`，门禁 G2 的"四字段齐全"在旧模型上**不可实现**；新增四字段是唯一不做文本解析的路径。

**模型保持宽容、门禁负责严格**：不在 Pydantic validator 里拒绝缺字段，避免一次解析失败导致整份报告降级为确定性版本；缺字段由 G1/G2 产 finding。

#### 3.3.2 Prompt 变更（memo_claims）

在 memo_lite 的基础上：

1. `claims` 必须是 3–5 条，按重要性排序；每条包含 `text/grounds/warrant/rebuttal/qualifier/falsification`；
2. `grounds` 至少 1 条；`ref` 从输入 JSON 中已有的 artifact id / canonical 字段名复制，**不得编造 id**；没有可引用来源时 `inferred=true` 并在 `grounds[].statement` 说明推断依据；
3. `falsification` 至少 1 条，`metric/threshold/window/source` 四字段必须齐全；
4. 主文 section（exec/scenario/risks/falsification/key_metrics）只做摘要与观察清单，**不得重复 claims 的论证**；`investment_viewpoint` 置空字符串；
5. 附录六节保留逐条明细，不再写"与 core_view 的关系"。

`json_instruction(ReportOutput)` 的示例（`model_config.json_schema_extra["example"]`）必须同步补一条 claim 示例，否则模型看不到字段样例（`alphabee/agents/schemas.py:267`）。

#### 3.3.3 渲染

`report_layout.render_claims(claims) -> str`：

```markdown
### 主张 1：<text>

- **依据**：
  - <statement>（<value>；来源：<source>；ref: <ref>）
- **推理**：<warrant>
- **反证**：<rebuttal>
- **限定**：<qualifier>
- **失效条件**：<metric> <threshold>（<window>；来源：<source>）
```

- `claims` 非空 → 主文第 2 节用 claim 块；`investment_viewpoint` 忽略；
- `claims` 为空 → 回退到 `investment_viewpoint`（等价 legacy 内容），并在 gate 记一条 warning（`claim_missing`）；
- 终端渲染同一模板，附录只打印标题 + 前 800 字。

#### 3.3.4 确定性降级

`build_deterministic_report`（`reporter.py:71`）在 LLM 连续失败时仍要产出可消费报告。新增 `derive_claims_from_insight(payload) -> list[ReportClaim]`：

| 来源 | 派生 | 约束 |
|---|---|---|
| `insight.core_view` + `supporting_evidence[:2]` | claim 1 | 证据的 `ref` 留空、`inferred=true`；`warrant` 用 `central_tension` 或 `main_driver` 原文；`rebuttal` 取 `counter_evidence[0]` |
| `insight.central_tension` | claim 2（仅当非空且与 claim 1 不同） | 同上 |
| `insight.what_would_change_my_mind[:1]` | claim 1 的 falsification | 四字段可能为空；降级报告的门禁降为 warning |

- 无 insight → `claims=[]`，走 legacy 章节渲染；
- 降级报告在 `summary` 前缀 `【降级】`，并保留 `subagent_failure` issue；G1/G2 对降级报告只告警不阻断（避免 LLM 故障时反复重写）。

#### 3.3.5 契约传播表（按 `alphabee-pipeline-contract-steward`）

| 变更 | 生产者 | 消费者 | 必须新增/更新的测试 |
|---|---|---|---|
| `ReportOutput.claims` | `reporter.generate_report` / `build_deterministic_report` | `gates.compute_report_metrics`、`gates._load_report_output`、`apps/cli/task_report`、`apps/cli/renderer`、`TaskRecord.report_raw` 透传 | 旧 JSON 反序列化（claims 默认空）、claims 渲染、coverage 口径、降级派生 |
| `ReportOutput.layout` | 同上 | 两个渲染面、`task_report` 附录折叠 | 旧 JSON 默认 legacy、memo/legacy 双渲染 |
| `FalsificationCondition` 四字段 | insight prompt、reporter prompt | `_falsification_markdown`、G2、`render_claim` | 旧字符串反序列化、四字段渲染、G2 判定 |
| `EvaluateMetrics` / `expected_sections` | `compute_report_metrics` | `review_report` | memo 下 `investment_viewpoint` 换 `claims` 的覆盖口径 |

`compute_report_metrics`（`gates.py:222`）的 `expected_sections` 改为模式感知：

```python
expected = {...}  # 现有 12 项
if report_payload.get("layout") == "memo" and report_payload.get("claims"):
    expected = (expected - {"investment_viewpoint"}) | {"claims"}
```

### 3.4 Q2：四个确定性门禁（W2，2 天）

#### 3.4.1 规则

新增纯函数模块 `alphabee/orchestrator/services/report_quality.py`（禁 LLM、禁网络、可单测）：

```python
@dataclass(frozen=True)
class QualityFinding:
    gate: str          # "G1" | "G2" | "G3" | "G4"
    severity: str      # "blocker" | "warning"
    claim_id: str      # 非 claim 级问题为空
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)


def check_report_quality(
    report: ReportOutput,
    payload: ReportGenerationPayload,
    allowed_refs: set[str],
    params: ReportQualityParams,
) -> list[QualityFinding]: ...
```

| 门禁 | 规则 | 判定 | 初期严重度 |
|---|---|---|---|
| **G1 证据覆盖** | 每条 `material=true` 且 `inferred=false` 的 claim：`grounds` 中 ≥1 条 `ref` 非空；若 `ref` 形如已知 id 前缀（artifact/obs/decision/issue）则必须在 `allowed_refs` 内；无 ref 时必须 `inferred=true` | 缺一即 finding | blocker（enforce 后） |
| **G2 可证伪** | 每条 claim 的 `falsification` ≥1 条，且 `metric/threshold/window/source` 四字段全部非空 | 缺一即 finding | blocker（enforce 后） |
| **G3 数字成员** | 主文（claims + exec/scenario/risks/falsification/key_metrics）中的数字 token，归一化后必须落在 `allowed_numbers(payload)` ∪ 白名单 内；未命中比例 > `tolerance_ratio` 即 finding | 列出未命中 token + 上下文 | warning（观察期）→ blocker（误报实测为 0 后） |
| **G4 长度/数量** | 主文 ≤ `main_max_chars`；`3 ≤ len(claims) ≤ 5`；单条 claim 块 ≤ `max_chars_per_claim`；exec ≤800、scenario/risks ≤1,200、observation ≤1,000 | 超一即 finding | warning（避免重写风暴） |

**降级报告豁免**：`summary` 以 `【降级】` 开头或存在 `subagent_failure` issue 时，G1/G2 降为 warning、G3 跳过（数字来自确定性渲染，仍可校验，但优先级低）。

#### 3.4.2 G3 数字归一化算法（明确边界，不做语义溯源）

```python
def collect_allowed_numbers(payload) -> set[float]:
    """递归收集 payload 中所有数值叶子（int/float），并加入三种量纲解释。"""
    # 对每个原始值 v：加入 v、v*100、v/100（兼容 RATIO/PERCENT 两种 canonical 口径）
    # 另加：v 的 6 位小数四舍五入；绝对值 ≤1e-9 归零

def extract_numbers(text: str) -> list[tuple[float, str]]:
    # 正则 r"(?<![\w.])(-?\d[\d,]*\.?\d*)\s*(万亿|亿|万|千|%|x|倍)?"
    # 去逗号；单位换算：万亿=1e12、亿=1e8、万=1e4、千=1e3、%=/100；"x/倍" 保留原值

def is_member(x: float, allowed: set[float], rel=0.005, abs_=0.01) -> bool:
    # 任一 allowed 满足 |x-a| <= max(abs_, rel*|a|)
```

白名单（避免误伤结构性数字）：年份 `1900–2100`、状态 `S0–S5`、小整数 `{0..10}`、常见阈值 `{15,20,25,30,40,50,60,70,80,90,100}`、股票代码 `\d{6}\.(SH|SZ|BJ)`。

已知限制（显式登记，不假装完整）：派生算术（如 `446.56 - 206.07 = 240.49`）若不等于任一 payload 值，会被判未命中；因此 G3 在观察期只统计不阻断，并把未命中样本写入 `tmp/report_quality/g3_misses.jsonl` 供人工裁决。**先证明误报率，再谈硬门禁。**

#### 3.4.3 接入 `review_report`

在 `gates.review_report`（`gates.py:455`）中，`_deterministic_assessment` 之后、`rewrite_needed` 计算之前：

```python
findings = check_report_quality(report_output, payload, allowed_refs, rq_params)
for f in findings:
    updated_issues.append(Issue(..., category="report_claim_gate", message=f"[{f.gate}] {f.message}", ...))
if gate_mode == "enforce":
    assessment.blocking_issues.extend(f.message for f in findings if f.severity == "blocker")
```

- `report.gates.mode: observe | enforce`，默认 `observe`：只产 issue、不改 `passed`、不触发重写；
- enforce 需同时满足：G1/G2 blocker 误报为 0（10 份 memo 报告人工核对）+ G3 观察期 ≥10 份且未命中率可解释；
- 新增 issue category 需在 `orchestrator/services/deviation.py` 的 `CLASS_BY_CATEGORY` 登记（默认 D3_ARGUMENT），否则偏离账本落不到类。

#### 3.4.4 判别力：10 个注入变异（验收要求 10/10 报警）

| # | 变异 | 期望 |
|---|---|---|
| M1 | 删掉 claim 的 `grounds` 全部 ref | G1 |
| M2 | ref 改成不存在的 `artifact-deadbeef` | G1 |
| M3 | `inferred=false` 且 grounds 为空 | G1 |
| M4 | 删掉 claim 的 `falsification` | G2 |
| M5 | falsification 缺 `threshold` | G2 |
| M6 | falsification 缺 `window` | G2 |
| M7 | falsification 缺 `source` | G2 |
| M8 | 主文注入 payload 中不存在的数字 `123.456` | G3 |
| M9 | claims 改成 6 条 | G4 |
| M10 | 单条 claim 块写到 800 字 | G4 |

测试文件：`tests/orchestrator/test_report_claim_gates.py`，每个变异独立用例 + 一个"合法报告 0 finding"用例 + 一个"legacy 报告 0 finding"用例。

#### 3.4.5 不误伤控制

- **按构造**：G1–G3 只在 `layout=memo` 且 `claims` 非空时运行；135 条历史记录（legacy）天然跳过；
- **回归**：对 5 份归档报告 JSON（legacy）跑 gate → 0 finding（负对照）；
- **抽样**：对 20 条历史 `TaskRecord.report_raw` 跑 `build_report_markdown` + gate → 0 finding；
- **memo 正样本**：手工构造 3 份合规 memo 报告 → 0 finding；3 份各含 1 个缺陷 → 只报对应 gate。

### 3.5 A/B 盲评协议（W2，2 天）

#### 3.5.1 评估集（10 个标的）

| # | 标的 | 行业 | 入选理由 |
|---|---|---|---|
| 1 | 600760.SH 中航沈飞 | 国防军工 | S5、low confidence、34 条 issue、论证密度最高 |
| 2 | 300504.SZ 天邑股份 | 通信设备 | 基线正文 32.5k，明细占比 59.6% |
| 3 | 002415.SZ 海康威视 | 安防 | 明细占比 63.9%，行业语境强 |
| 4 | 600183.SH 生益科技 | 覆铜板 | 数字密度 42.1/千字 |
| 5 | 300760.SZ 迈瑞医疗 | 医疗器械 | 大盘蓝筹，论证风格不同 |
| 6 | 603259.SH 药明康德 | CXO | 多期记录，可测稳定性 |
| 7 | 002130.SZ 沃尔核材 | 电子材料 | 历史质量修复对象，含已知缺陷 |
| 8 | 601138.SH 工业富联 | 电子制造 | 大体量、报表结构复杂 |
| 9 | 002916.SZ 深南电路 | PCB | 对标组语境 |
| 10 | 300037.SZ 新宙邦 | 电池化学品 | 周期成长混合 |

取每标的最新一条含 `report_raw` 的记录；W2 用 `capture_payload` 各跑一次 live run 拿 payload（数据时点统一在 W2 内）。

#### 3.5.2 流程

1. 每个标的：capture payload → `report_ab.py --modes legacy,memo_claims` 各 1 次 → 2 份 JSON + 2 份 md；
2. 客观指标自动计算：主文字符、数字 token、claim 数、附录完整性、gate findings；
3. 盲评：文件重命名为 `R01-A.md` / `R01-B.md`（随机 A/B，种子记录），只给 md，不给 JSON/模式；
4. 你按 8 维打 1–5 分（下表），填入 `scores.csv`；同一标的两个版本必须一起看、可反复读；
5. 稳定性：抽 2 个标的，每模式再各生成 1 次（共 4 次额外调用），衡量 LLM 随机性；若同模式两次均分差 >0.5，需在结论中标注噪声；
6. 解盲、统计、写结论。

#### 3.5.3 8 维评分表

| 维度 | 1 分 | 3 分 | 5 分 |
|---|---|---|---|
| 主张明确 | 读完不知道结论 | 能找到结论但要自己总结 | 首段即主张，且可复述 |
| 证据充分 | 断言无依据 | 关键主张有依据 | 每条主张有数字+口径+来源 |
| 推理链 | 因果跳跃 | 有机制但略含糊 | 依据→机制→结论可检验 |
| 反证回应 | 只列支持证据 | 提到反证但未回应 | 最强反证被正面回应 |
| 可证伪 | 无失效条件 | 有定性条件 | 指标/阈值/时间窗/来源齐全 |
| 限定准确 | 过度自信 | 有置信度 | 置信度与证据强度匹配 |
| 内部一致 | 自相矛盾 | 个别口径混用 | 数字/方向/口径一致 |
| 可行动 | 无法跟踪 | 有观察项 | 看什么/何时变/错在哪都明确 |

#### 3.5.4 验收与统计

- 主文降幅：基线 ≥15k 的 4 份报告，memo 主文降幅 ≥50%；兴森（12.7k）只要求不增长；
- 8 维均分提升 ≥1.0；任一维不得下降 >0.5；
- ≥6/10 标的整体偏好 memo（方向性门槛）；若 ≥8/10，另报单侧符号检验 p≈0.055，仅作方向性证据，不宣称统计显著；
- 4 个门禁 10/10 报警；归档报告 0 误伤；
- 单评审，报描述统计（均值/中位数/配对差），**不报 kappa、不假装一致性**。

### 3.6 评估集与夹具（与 P2 共用）

```text
data/eval/report_eval_v1/            # gitignored，本地评估资产
  <symbol>/
    payload.json                     # 同一份输入
    legacy.json / legacy.md
    memo_claims.json / memo_claims.md
    metrics.json
  scores.csv                         # 盲评结果
  README.md                          # 生成日期、模型、prompt 版本、盲评种子
tests/fixtures/reports/              # 提交入库的冻结夹具（5 份 report_raw）
  <symbol>-<date>.json
tests/fixtures/midterm/600760_frame.json
```

- 评估集是未来 B/C 类黄金帧的**种子**：payload + 两份输出 + 评分，构成"同输入、不同表达"的对照；
- 冻结夹具只存**报告 JSON**（不含公司原始数据），用于渲染/门禁回归；
- 任何夹具更新必须在 ROADMAP 登记（原因、旧值哈希、新值哈希）。

---

## 4. 常量线设计

### 4.1 现状：三个具体缺口

```text
classifier_v2.EvidenceParams（classifier_v2.py:111）
        │  支持 temperature / uncertain_p_top / break_lambda / s0_floor …
        ▼
classifier_dispatch.classify_state（classifier_dispatch.py:93）
        │  调用 v2 时只传 order/previous —— 没有 params（第 115 行）
        ▼
真实运行永远用 DEFAULT_PARAMS；改值只能改代码。
```

```text
score_engine（score_engine.py:45-128）
        │  _FUNDAMENTAL_YOY_SCALE / _OCF_NEGATIVE_PENALTY / _BEAT_BONUS /
        │  _MISS_PENALTY / _MISS_GAP_THRESHOLD / _SEGMENT_DIVERGENCE_* /
        │  F_MARGINAL_WEIGHT / _ANCHOR_WEIGHT / _RESEARCH_WEIGHT / _MIN_CONSENSUS_COVERAGE
        ▼
fundamental_breakdown / compress_scores 只接受 mode 参数，不接受数值参数
        ▼
任何数值实验都要 fork 代码或 monkeypatch 常量（scripts/f_calibration.py 现在就是这么做的）。
```

```text
_MIN_CONSENSUS_COVERAGE = 3 在两个模块各定义一次：
  score_engine.py:51
  evidence_rules.py:90
两份常量无同步机制 —— 改一处、另一处不动，E 分数与 revision 证据会静默分叉。
```

**结论**：工作量不是"169 个常量搬进 config"，而是 **15 个决策关键常量 + 2 处接线 + 1 个重复定义治理**。

### 4.2 三层分类与判据

| 类型 | 判据 | 例子 | 放哪 | 能否运行时改 |
|---|---|---|---|---|
| **结构常量** | 改动会改变公式形状/状态空间/单位，而不只是数值 | S0–S5 集合、`EVIDENCE_CAPS`、softmax 形式、`_REVISION_REF_LOG` | 代码 | 否（改即版本变更） |
| **策略常量** | 数值型设计选择，影响方向分/状态/仓位；有业务含义 | beat/miss 奖惩、OCF 惩罚、温度、阈值、权重 | `params/registry.yaml` + overrides | 是，但默认冻结、改动留痕 |
| **技术常量** | 超时、重试、分页、并发、路径 | tavily timeout、max_results | `config.yaml` / env | 是 |

**判据自检三问**（三问全"是"才是策略常量）：
1. 改这个数会改变 F/E/T/V/C/R 或状态分布/仓位吗？
2. 它有一个业务上可辩护的取值（而不是公式恒等式）吗？
3. 它现在没有配置入口，但实验时确实需要扫描它吗？

### 4.3 `params/registry.yaml` schema

```yaml
schema_version: 1
registry_version: 1
entries:
  - id: midterm.score.beat_bonus
    value: 0.2
    unit: score
    domain: midterm
    kind: policy              # structure | policy | technical
    status: placeholder       # active | shadow | placeholder | frozen | deprecated
    source: design            # backtest | expert | literature | design | placeholder
    version: 1
    owner: captain
    review_condition: "真实帧 >= 50 且 beat/miss 可结算"
    wired_in: ScoreParams.beat_bonus
    overridable: true
    bounds: [0.0, 0.5]
    notes: "net_profit_yoy 超预告上限的加分；miss 信息量更大，故 0.3 > 0.2"
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | 是 | `domain.area.name` 小写点分；全局唯一 |
| `value` | 是 | 数值（int/float）；不要写字符串 |
| `unit` | 是 | `score` / `pp` / `ratio` / `count` / `nats` |
| `domain` | 是 | `midterm` / `report` / `data` |
| `kind` | 是 | 见 §4.2；`structure` 不允许出现在本文件（防止误放） |
| `status` | 是 | 见状态机 |
| `source` | 是 | 取值证据来源；`design` 表示"设计初值，未校准" |
| `version` | 是 | 该条目自身版本，改值 +1 |
| `owner` | 是 | 变更责任人（当前 `captain`） |
| `review_condition` | 是 | 何时必须复核（样本量/指标） |
| `wired_in` | 否 | 绑定到哪个 typed params 字段；未接线必须留空并在 `notes` 说明 |
| `overridable` | 是 | 默认 `true`；`frozen` 条目只允许 experiment 覆盖 |
| `bounds` | 否 | `[min, max]`，越界即非法 |
| `group` / `constraint` | 否 | 组约束（如 `sum=1.0`） |

**状态机**：

```text
placeholder ──(语料/证据达标)──> active ──(退役)──> deprecated
     │                              │
     └──(需要观察但不生效)──> shadow ┘
frozen：设计冻结；默认值不可改，只允许 experiment override（输出标注 experiment=true）
```

规则：
- `active`：生产生效，默认值即当前值；必须有行为变更登记或校准记录支撑（`notes` 指向 ROADMAP 行/证据），`source=design` 也允许；
- `shadow`：计算并记录，但不进入决策（沿用 `f_marginal_mode=shadow` 语义）；
- `placeholder`：占位初值，允许 config override 做实验，但不得宣称"已校准"；
- `frozen`：默认值不动；experiment override 必须走 CLI 文件，输出带 `experiment=true`；
- `deprecated`：保留 id 防止旧配置报错，读取时 warn 并回默认。

### 4.4 v0 清单（15 条，P0 产出）

| # | id | 当前值 | 位置 | kind | status | source | 绑定字段 | 复核条件 |
|---|---|---:|---|---|---|---|---|---|
| 1 | `midterm.score.fundamental_yoy_scale` | 30.0 | `score_engine.py:45` | policy | placeholder | design | `ScoreParams.fundamental_yoy_scale` | 50 帧 + 饱和率统计 |
| 2 | `midterm.score.ocf_negative_penalty` | 0.5 | `score_engine.py:66` | policy | placeholder | design | `ScoreParams.ocf_negative_penalty` | 50 帧 + OCF 分组收益 |
| 3 | `midterm.score.beat_bonus` | 0.2 | `score_engine.py:74` | policy | placeholder | design | `ScoreParams.beat_bonus` | 50 帧 + beat 结算 |
| 4 | `midterm.score.miss_penalty` | 0.3 | `score_engine.py:79` | policy | placeholder | design | `ScoreParams.miss_penalty` | 50 帧 + miss 结算 |
| 5 | `midterm.score.miss_gap_threshold` | 20.0 | `score_engine.py:80` | policy | placeholder | design | `ScoreParams.miss_gap_threshold` | 50 帧 + gap 分布 |
| 6 | `midterm.score.segment_divergence_threshold` | 15.0 | `score_engine.py:83` | policy | placeholder | design | `ScoreParams.segment_divergence_threshold` | 50 帧 + 分部样本 |
| 7 | `midterm.score.segment_divergence_bonus` | 0.2 | `score_engine.py:84` | policy | placeholder | design | `ScoreParams.segment_divergence_bonus` | 同上 |
| 8 | `midterm.score.f_marginal_weight` | 0.30 | `score_engine.py:125` | policy | **frozen** | design | `ScoreParams.f_marginal_weight` | 边际开权 G-8 全绿 |
| 9 | `midterm.score.anchor_weight` | 0.6 | `score_engine.py:52` | policy | active | design | `ScoreParams.anchor_weight` | 组约束 sum=1.0 |
| 10 | `midterm.score.research_weight` | 0.4 | `score_engine.py:53` | policy | active | design | `ScoreParams.research_weight` | 组约束 sum=1.0 |
| 11 | `midterm.data.min_consensus_coverage` | 3 | `score_engine.py:51` / `evidence_rules.py:90` | policy | placeholder | design | `ScoreParams.min_consensus_coverage` + `build_numeric_evidence(min_consensus_coverage=)` | 50 帧 + coverage 分布 |
| 12 | `midterm.evidence.temperature` | 0.22 | `classifier_v2.py:90` | policy | active | backtest | `EvidenceParams.temperature` | 50 帧后复核分位数门 |
| 13 | `midterm.evidence.uncertain_p_top` | 0.55 | `classifier_v2.py:91` | policy | placeholder | design | `EvidenceParams.uncertain_p_top` | 50 帧后复核 |
| 14 | `midterm.evidence.break_lambda` | 2.0 | `classifier_v2.py:89` | policy | active | backtest | `EvidenceParams.break_lambda` | 关键格比例复核 |
| 15 | `midterm.evidence.s0_floor` | 0.10 | `classifier_v2.py:83` | policy | active | backtest | `EvidenceParams.s0_floor` | 偏多帧 S0 占比 |

**候选但暂不纳入**（写入 `notes`，不进 v0 表）：`_REVISION_REF`（100.0，结构性归一化参考）、`F_ACCEL_SCALE` / `F_MARGIN_DELTA_SCALE` / `F_MARGIN_QUALITY_FLOOR`（仅 `marginal_mode=on` 生效，当前 shadow）、`_SEGMENT_MIN_SHARE`（10.0，与阈值同组，P1.5 一并处理）、`_E_REVISION_EXEMPT` / `_CROWDING_EXEMPT_HALF` / `_REGIME_LIFT`（影响 T/C 的软约束，P2 评估再定）。

**组约束**：
- `anchor_weight + research_weight = 1.0`（`constraint: {group: e_blend, sum: 1.0}`）；
- `bounds` 见 §4.3 示例；越界配置一律 fail-open。

### 4.5 解析优先级与 fail-open

**优先级**（高 → 低）：

```text
1. 函数显式参数（score_params=... / evidence_params=...）  ← 测试、离线回放
2. CLI --param-override experiment.yaml                    ← 实验，输出必须标注 experiment=true
3. config.yaml parameters.overrides.<id>                   ← 持久化覆盖
4. config.yaml parameters.profile                          ← v0 只有 "default"
5. params/registry.yaml 的 value                           ← 默认来源
```

`config.yaml` 示例：

```yaml
parameters:
  profile: default
  overrides:
    midterm.score.beat_bonus: 0.1        # 实验性覆盖；会写入 params_hash 与报告元信息
```

**fail-open 矩阵**：

| 场景 | 行为 |
|---|---|
| registry 缺失/解析失败 | 用 `ScoreParams()` / `EvidenceParams()` 内置默认；warn 一次 |
| registry 有未知 id | 忽略该条；warn |
| config override 未知 id | 忽略；warn（旧配置兼容） |
| CLI experiment 未知 id | **报错退出**（实验必须显式） |
| 值类型非法/非数值 | 回默认；warn；CLI 场景报错 |
| 值越界（bounds） | 回默认；warn；CLI 场景报错 |
| `frozen` + config override | 忽略；warn（"冻结参数只允许 experiment 覆盖"） |
| `frozen` + CLI experiment | 允许；输出标注 `experiment=true` |
| 组约束不满足 | 整组回默认；warn |
| profile 未知 | 回 `default`；warn |

**禁止静默**：任何被忽略/回退的覆盖都必须出现在 `param_overrides_resolved` 与日志里；报告元信息只展示**实际生效**的覆盖。

### 4.6 typed params 与接线

#### 4.6.1 新包结构

```text
alphabee/params/
  __init__.py        # 导出 ScoreParams / EvidenceParams / ReportQualityParams / resolve_*
  models.py          # 三个 frozen dataclass（无外部依赖）
  registry.py        # 读 YAML、校验、hash、profile/overrides 解析
  resolvers.py       # resolve_score_params / resolve_evidence_params / resolve_report_quality_params
```

**import 方向**（无环）：

```text
params.models  ←  score_engine / classifier_v2 / report_quality
params.registry → params.models + config
decision_model  → params.resolvers
classifier_dispatch → params.resolvers
```

`EvidenceParams` 从 `classifier_v2.py:111` **迁到** `params/models.py`，并在 `classifier_v2` re-export：

```python
from alphabee.params.models import EvidenceParams, DEFAULT_EVIDENCE_PARAMS
DEFAULT_PARAMS = DEFAULT_EVIDENCE_PARAMS   # 兼容旧导入（tests 已引用 DEFAULT_PARAMS）
```

> `EvidenceParams` 目前只有 `classifier_v2` 与一个 parity 测试引用，迁移成本低；`DEFAULT_PARAMS` 别名保证 `tests/midterm/test_classifier_v2_parity.py:97` 不改。

#### 4.6.2 签名变更（P1）

```python
# alphabee/params/models.py
@dataclass(frozen=True)
class ScoreParams:
    fundamental_yoy_scale: float = 30.0
    ocf_negative_penalty: float = 0.5
    beat_bonus: float = 0.2
    miss_penalty: float = 0.3
    miss_gap_threshold: float = 20.0
    segment_divergence_threshold: float = 15.0
    segment_divergence_bonus: float = 0.2
    f_marginal_weight: float = 0.30
    anchor_weight: float = 0.6
    research_weight: float = 0.4
    min_consensus_coverage: int = 3


DEFAULT_SCORE_PARAMS = ScoreParams()
```

```python
# score_engine.py（内部全部改为读 params，不再读模块常量）
def _saturate(value, scale): ...                                   # 调用方传 params.fundamental_yoy_scale
def _operating_cashflow_penalty(f, basis=..., *, params=DEFAULT_SCORE_PARAMS): ...
def _beat_bonus(f, e, *, params=DEFAULT_SCORE_PARAMS): ...
def _miss_penalty(f, e, *, params=DEFAULT_SCORE_PARAMS): ...
def _segment_divergence(f, *, params=DEFAULT_SCORE_PARAMS): ...
# 注：_SEGMENT_MIN_SHARE（10.0）不在 v0，仍读模块常量；P1.5 与阈值同组接线
def _segment_bonus(f, mode=..., *, params=DEFAULT_SCORE_PARAMS): ...
# F_ACCEL_SCALE / F_MARGIN_DELTA_SCALE / F_MARGIN_QUALITY_FLOOR 不在 v0，P2 再接线
def _marginal(f): ...
def _revision(e, *, params=DEFAULT_SCORE_PARAMS): ...
def fundamental_breakdown(f, e, *, ..., params=DEFAULT_SCORE_PARAMS): ...
def _fundamental_trend(f, e, *, ..., params=DEFAULT_SCORE_PARAMS): ...
def compress_scores(snapshot, *, ..., params=DEFAULT_SCORE_PARAMS): ...
```

**默认值策略**：`params` 缺省 = `DEFAULT_SCORE_PARAMS`，其值**逐项等于**现有模块常量；模块常量保留为"文档锚 + 回归对照"，并由 `tests/params/test_registry.py::test_defaults_match_module_constants` 钉住。这样：
- 所有既有直接调用点（含单测 `compress_scores(snap)`）不改一行，行为逐值不变；
- 常量与 dataclass 漂移会被测试立即抓住；
- 未来切换"registry 为唯一真相"时，只需删常量与对照测试。

**decision_model**：

```python
def evaluate(
    ...,
    score_params: ScoreParams | None = None,
    evidence_params: EvidenceParams | None = None,
) -> CompanyStateArtifact:
    resolved_score = score_params or resolve_score_params()
    resolved_evidence = evidence_params or resolve_evidence_params()
    scores = compress_scores(snapshot, ..., params=resolved_score)
    cls = classify_state(scores, order=..., formula=..., params=resolved_evidence)
    ...
    _writeback_f_breakdown(snapshot, ..., params=resolved_score)   # 分解必须与方向分同源
    artifact.params_hash = compute_params_hash(resolved_score, resolved_evidence)
    artifact.params_profile = resolved_profile
    artifact.param_overrides = resolved_overrides
```

> `_writeback_f_breakdown`（`decision_model.py:398`）会再次调用 `fundamental_breakdown` 生成报告里的 F 分解；**必须传入同一个 `resolved_score`**，否则 override 后"方向分用一个参数、分解用另一个参数"，报告会解释一个并不存在的 F。这是 `alphabee-schema-steward` 的单一来源要求，也是 P1 最容易漏的接线点。

**classifier_dispatch**：

```python
def classify_state(..., params: EvidenceParams | None = None):
    resolved_params = params if params is not None else resolve_evidence_params()
    if resolved_formula == FORMULA_V2:
        return classifier_v2.classify_state(..., params=resolved_params)
    if resolved_params != DEFAULT_EVIDENCE_PARAMS:
        logger.warning("v1 分类器不支持 evidence 参数注入，已忽略（formula=%s）", resolved_formula)
    ...
```

**`evidence_rules`**：

```python
def _revision_evidence(values, symbol, as_of_date, *, min_consensus_coverage: int = 3): ...
def build_numeric_evidence(..., *, min_consensus_coverage: int = 3): ...
# decision_model 调用点透传 resolved_score.min_consensus_coverage
```

#### 4.6.3 修改文件清单（P1）

| 文件 | 改动 |
|---|---|
| `alphabee/params/__init__.py` | 新增 |
| `alphabee/params/models.py` | 新增：`ScoreParams` / `EvidenceParams` / `ReportQualityParams` |
| `alphabee/params/registry.py` | 新增：YAML 加载、校验、hash |
| `alphabee/params/resolvers.py` | 新增：优先级解析、fail-open |
| `params/registry.yaml` | 新增：15 条 |
| `params/CHANGELOG.md` | 新增：变更记录模板（append-only） |
| `alphabee/midterm/score_engine.py` | 11 处常量读取改为 `params.*` |
| `alphabee/midterm/classifier_v2.py` | `EvidenceParams` 迁出 + re-export |
| `alphabee/midterm/classifier_dispatch.py` | 补传 `params`（第 115 行） |
| `alphabee/midterm/decision_model.py` | `evaluate/get_decision` 新增参数、解析、`params_hash` 写入、`_writeback_f_breakdown` 分解透传 |
| `alphabee/midterm/evidence_rules.py` | `min_consensus_coverage` 透传 |
| `alphabee/midterm/models.py` | `CompanyStateArtifact` 追加 3 个 additive 字段 |
| `alphabee/config/__init__.py` | `Settings.parameters` 段（`profile` + `overrides`） |
| `config.yaml.example` | `parameters` 示例 |
| `alphabee/task_records/models.py` | `TaskRecord.params_hash` / `params_profile` / `param_overrides`（additive） |
| `alphabee/apps/cli/task_report.py` | 元信息行渲染参数指纹 |
| `tests/midterm/test_v1_artifact_golden.py` | `_NEW_FIELDS` 追加 3 字段（golden 比较剔除） |

### 4.7 `params_hash` 与溯源

**算法**（稳定、可复现、与注释/状态无关）：

```python
def compute_params_hash(score: ScoreParams, evidence: EvidenceParams) -> str:
    values = {
        **{f"midterm.score.{k}": v for k, v in asdict(score).items()},
        **{f"midterm.evidence.{k}": v for k, v in asdict(evidence).items()},
    }
    payload = "\n".join(f"{k}={float(v):.12g}" for k, v in sorted(values.items()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
```

- 只对**最终生效值**做哈希：override 改变值 → 哈希改变；改注释/状态/owner → 哈希不变；
- 哈希覆盖 score + evidence 两组（midterm 帧的决策参数）；报告质量阈值（§5.1）**不进帧哈希**——它们只影响报告门禁，且帧产出于报告之前，混入会造成不必要的耦合；
- `profile` 与 overrides 分开记录，不参与哈希（否则同一组值会因来源不同产生不同指纹）；
- 哈希不包含 `formula_revision` / `classifier_order`（它们已有独立 provenance 字段）。

**写入点**：

| 位置 | 字段 | 说明 |
|---|---|---|
| `CompanyStateArtifact` | `params_hash: str = ""`、`params_profile: str = ""`、`param_overrides: dict[str, float] = {}` | 新帧写入；旧帧回落空（不伪装成 default） |
| `TaskRecord` | `params_hash: str = ""`、`params_profile: str = ""`、`param_overrides: dict[str, float] = {}` | 报告记录携带；旧记录无字段 |
| 报告元信息 | `**参数指纹**: <hash>（profile=default，override=1）` | 仅当 hash 非空时渲染，旧记录不出现空标签 |
| 报告 JSON | 不新增（避免报告正文承担 provenance） | 由 TaskRecord 承担 |

**验收**：同一输入、默认参数下连续两次 run 的 `params_hash` 相同；改一个 override 后 hash 变化且报告元信息出现 override 计数；旧记录重渲染不出现新行。

### 4.8 影子对比（P2）

```text
python scripts/params_shadow.py \
    --frame data/midterm/state/600760.SH.jsonl \
    --override midterm.score.beat_bonus=0.1 \
    --out tmp/params_shadow/
```

流程（离线、只读、不改决策）：

1. 载入 `CompanyStateArtifact`，取 `factor_snapshot` + `evidence_log` + `as_of_date`；
2. 用默认参数算一遍 `evaluate`，用 override 参数算一遍；
3. 用 §4.9 的 canonicalizer 输出字段级 diff：`f/e/t/v/c/r`、状态分布、argmax、`uncertain`、EV、仓位档；
4. 写 `shadow.json`（含 `params_hash_old` / `params_hash_new` / `changed_fields` / `decision_changed`）与 `shadow.md`（人读）；
5. 若 `decision_changed=false`，判定"等价变更"，可直接登记；若 `true`，必须附影响报告与回滚方式。

**不做运行时双算**：生产路径双算会把成本翻倍且引入分支；影子只用于离线实验与变更前评估。

### 4.9 行为指纹（P2，与报告线共用夹具）

#### 4.9.1 中期指纹

```text
tests/fixtures/midterm/600760_frame.json          # 从 data/midterm/state 复制（冻结）
tests/midterm/fixtures/params_cases.py            # 9 个合成 FactorSnapshot
tests/midterm/golden/params_fingerprint.json      # 期望输出
```

9 个合成用例覆盖：beat、miss、OCF<0、segment bonus、marginal on/off、缺失值、Q1 降级、金融股、高增长。canonical 输出：

```json
{
  "schema_version": 1,
  "params_hash": "…",
  "cases": {
    "600760_frame": {
      "scores": {"f": -0.7334, "e": -0.5460, "t": 1.0, "v": 0.6012, "c": -0.1904, "r": -0.0874},
      "state": "S5",
      "distribution": {"S0": 0.0300, "S1": 0.0190, "S2": 0.0234, "S3": 0.0190, "S4": 0.0190, "S5": 0.8895},
      "entropy": 0.5233,
      "uncertain": false,
      "consistency": "divergent",
      "adverse_severity": 0.8458,
      "expected_value": {"...": 0.0},
      "position_band": "…"
    }
  },
  "aggregate_sha256": "…"
}
```

更新纪律：测试失败时打印**字段级 diff**（不只哈希）；更新必须用 `scripts/update_fingerprint.py --reason "..."` 生成，并在 ROADMAP 登记（旧哈希 → 新哈希、原因、影响面）。

#### 4.9.2 报告指纹

```text
tests/fixtures/reports/<symbol>-<date>.json       # 5 份 report_raw
tests/apps/golden/report_render_fingerprint.json  # legacy/memo 两种布局的 hash + main_chars
```

- 对每份报告渲染 `legacy` 与 `memo` 两份 markdown，记录 `sha256` 与 `main_chars`；
- 任何渲染器/布局/折叠规则改动都会改变哈希 → 必须显式更新并登记；
- 与 §3.6 的评估集共用同一批 JSON：评估集是"输入+输出+评分"，指纹是"输出的稳定摘要"。

### 4.10 校准纪律（"可修正"≠自动调参）

**三条通道**（只有第一条会改默认值）：

1. **离线校准**：`scripts/f_calibration.py` 式网格/回放，样本门槛 ≥50 真实帧、≥20 标的、≥4 报告季度；不达标只输出"样本不足，不校准"；
2. **影子**：§4.8，不改决策，只产出差异；
3. **人工登记**：registry 改值 + `params/CHANGELOG.md` + ROADMAP 行为变更登记。

**变更记录格式**（`params/CHANGELOG.md`，append-only）：

```markdown
## 2026-11-XX / midterm.score.beat_bonus
- 旧值 → 新值: 0.2 → 0.1
- 样本: 真实帧 0（实验帧 1 / 合成 9）；**不足，不宣称校准**
- 指标变化: F/E 与状态分布的具体变化以 shadow.json 为准（示例：600760 帧 F=-0.7334；改权后 ΔF 与是否翻转 argmax 由脚本输出）
- 是否达标: 否（仅等价性观察）
- 回滚: 改回 0.2 + 重跑指纹
- owner: captain
```

**登记纪律**：
- 默认值变更 = 行为变更，必须登记 ROADMAP（日期/变更/依据/影响面/回滚）；
- `active` 状态需要证据（backtest 或 ≥50 帧人工评审），否则只能是 `placeholder`；
- `frozen` 参数只能 experiment override，**不得**通过 config 持久化；
- 报告质量阈值（§5.1）与 midterm 参数走同一 registry、同一套状态机与登记格式。

---

## 5. 两条线如何咬合

### 5.1 报告阈值进同一 registry（Q2 时追加）

| id | 初值 | 状态 | 绑定 |
|---|---:|---|---|
| `report.quality.main_max_chars` | 10000 | placeholder | `ReportQualityParams.main_max_chars` |
| `report.quality.exec_summary_max_chars` | 800 | placeholder | 同上 |
| `report.claims.min_count` | 3 | placeholder | 同上 |
| `report.claims.max_count` | 5 | placeholder | 同上 |
| `report.claims.max_chars_per_claim` | 500 | placeholder | 同上 |
| `report.claims.min_grounds` | 1 | placeholder | 同上 |
| `report.numeric.rel_tolerance` | 0.005 | placeholder | 同上 |
| `report.numeric.max_unmatched_ratio` | 0.0 | placeholder | 同上 |

**理由**：如果长度预算、claim 数、证据门槛这些新阈值散落在 prompt 与代码里，两周后就会制造第二批"未校准常量"，正好复制 §4.1 的问题。**新阈值必须从第一天就有 id、有状态、有复核条件。**

### 5.2 共用一套冻结夹具

| 夹具 | 报告线用途 | 常量线用途 |
|---|---|---|
| `tests/fixtures/midterm/600760_frame.json` | 报告输入的事实源（评估集 payload 的底座） | 中期指纹的 1 号用例 |
| `tests/fixtures/reports/*.json` | 渲染/门禁回归（legacy 负对照） | 报告指纹 |
| `data/eval/report_eval_v1/` | A/B 盲评 | 未来黄金帧种子 |
| 9 个合成 snapshot | 报告边界用例 | 参数指纹 |

### 5.3 评估集是黄金帧的种子

- 每个评估样本 = payload + legacy 输出 + memo 输出 + 评分 + gate findings；
- 这正好是黄金帧工厂需要的"输入-输出-判定"三元组，但**现在不建工厂**：先把 10 个样本跑稳定，再谈 B/C 类帧的批量生产；
- 顺序：Q2 评估集 → P2 指纹 → 未来黄金帧。反序（先建工厂）会得到一堆没有判定标准的帧。

---

## 6. 实施计划

### 6.1 依赖关系

```text
Q0 基线/规范 ──┬─> Q1-lite（prompt+布局+折叠）──> G0 决策 ──> Q2 claims ──> Q2 门禁 ──> A/B 盲评 ──> G1
               │                                      │
P0 registry v0 ─┴──────────────────────────────> P1 注入 ──> P1 默认不变测试 ──> G2
                                                                   │
                                                                   └──> P2 指纹/影子 ──> G3
```

- Q0 与 P0 可完全并行（不同文件、不同评审面）；
- Q2 依赖 G0 通过；P1 不依赖 G0（可与 Q2 并行）；
- P2 依赖 P1 的 typed params 与 Q2 的评估集（共用夹具）。

### 6.2 W1（5 天）

| 日 | 报告线 | 常量线 | 产出 |
|---|---|---|---|
| D1 | `report_baseline.py` 口径实现 + 跑 5 份报告 | 15 条常量盘点（值/位置/来源/绑定） | 基线 CSV + 常量清单 |
| D2 | 《报告论证规范》一页 + 基线复核 | `params/registry.yaml` v0 + schema 校验测试 | 规范文档 + registry v0 |
| D3 | prompt v2 + `report_layout.py` + 两渲染面接线 | `params/models.py`（先只定义，不接线） | memo 布局可渲染 |
| D4 | issue/中期块折叠 + `capture_payload` + `report_ab.py` | `params/registry.py` loader + hash | 3 标的 payload |
| D5 | 3 标的 legacy vs memo_lite 盲读 + G0 决策 + 回归 | registry 一致性测试（值与常量对照） | G0 结论 |

### 6.3 W2（5–7 天）

| 日 | 报告线 | 常量线 |
|---|---|---|
| D6–D7 | `ReportClaim` + `FalsificationCondition` 四字段 + prompt + 渲染 + 降级派生 | `ScoreParams` + `score_engine` 内部改造 |
| D8–D9 | G1–G4 + `report_quality.py` + `review_report` 接入 + 10 变异 | `classifier_dispatch` 补传 + `decision_model` 参数/`params_hash` |
| D10–D11 | 10 标的 A/B + 盲评 + 统计 | 默认行为不变测试 + `TaskRecord`/元信息 + config 段 |

### 6.4 W3（3–5 天）

| 日 | 内容 |
|---|---|
| D12 | 中期指纹（600760 + 9 合成） |
| D13 | 报告指纹（5 份 × legacy/memo） |
| D14 | `params_shadow.py` + 报告阈值登记进 registry |
| D15 | 文档收口（CONFIGURATION.md、ROADMAP 登记、索引）+ 全量回归 |

### 6.5 PR 切分

| PR | 内容 | 关键文件 | 测试 | 回滚 |
|---|---|---|---|---|
| PR-1 | Q0 基线脚本 + 规范 | `scripts/report_baseline.py`、`docs/implementation/report/REPORT_ARGUMENT_SPEC.md` | 脚本单测（口径/空输入） | 删文件 |
| PR-2 | Q1-lite 布局 + prompt | `agents/schemas.py`（layout）、`orchestrator/prompts.py`、`apps/cli/report_layout.py`、两渲染面 | `test_report_layout.py`、`test_task_report.py` | `report.mode: legacy` |
| PR-3 | Q1-lite 折叠 + A/B 工具 | `task_report.py`、`reporter.py`、`scripts/report_ab.py`、config `report` 段 | `test_task_report` 新用例、`report_ab` 冒烟 | 同 PR-2 |
| PR-4 | Q2 claims 契约 | `agents/schemas.py`、prompt、`report_layout.render_claims`、降级派生 | `test_report_claims_contract.py` | 不写 `claims` 即旧路径 |
| PR-5 | Q2 门禁 | `orchestrator/services/report_quality.py`、`gates.py`、`deviation.py` 登记 | `test_report_claim_gates.py`（10 变异） | `report.gates.mode: observe` |
| PR-6 | P0 registry | `params/*`、`params/registry.yaml`、`params/CHANGELOG.md`、`config/__init__.py` | `tests/params/test_registry.py` | 删包（无接线，行为零变化） |
| PR-7 | P1 注入 | `score_engine.py`、`classifier_dispatch.py`、`decision_model.py`、`evidence_rules.py`、`models.py`、`task_records` | `test_params_injection.py`、golden 扩展 | `parameters` 段删除 + 代码 revert |
| PR-8 | P2 指纹/影子 | fixtures、`scripts/params_shadow.py`、`scripts/update_fingerprint.py` | `test_params_fingerprint.py` | 删夹具/脚本 |
| PR-9 | 文档 | `docs/guide/CONFIGURATION.md`、ROADMAP 登记、`docs/implementation/README.md`、`docs/_sidebar.md` | 链接检查 | 文档 revert |

### 6.6 决策门（go/no-go）

| 门 | 时点 | 通过条件 | 不通过怎么办 |
|---|---|---|---|
| **G0** | W1 末 | §3.2.7 全过 | 降幅不够 → 调 prompt；"更清楚"不够 → 先修 insight/thesis 的主张化，不上 schema |
| **G1** | Q2 A/B 后 | §3.5.4 全过 | gate 误伤 → 保持 observe；均分不够 → claims 模板/证据引用规则返工 |
| **G2** | P1 后 | 默认输出逐字节不变；override 生效可证；指纹通过 | 任何默认变化视为 blocker，回退接线 |
| **G3** | P2 后 | 指纹稳定；影子 diff 可解释；阈值全部登记 | 指纹不稳定先查非确定性来源（排序/浮点/时间） |

---

## 7. 验收标准

### 7.1 报告线

| # | 验收项 | 判据 | 命令/工具 |
|---|---|---|---|
| R-1 | legacy 零变化 | 5 份归档报告 legacy 渲染 sha256 与基线一致 | `pytest tests/apps/test_task_report.py -q` |
| R-2 | memo 布局正确 | 标题顺序、附录编号、claim 块格式符合 §3.1/§3.3.3 | `pytest tests/apps/test_report_layout.py -q` |
| R-3 | 主文预算 | 3 个试点 ≤10,000 字；基线 ≥15k 的报告降幅 ≥50% | `scripts/report_ab.py` metrics |
| R-4 | 门禁判别力 | 10 个变异 10/10 报警 | `pytest tests/orchestrator/test_report_claim_gates.py -q` |
| R-5 | 门禁不误伤 | 5 份归档 + 20 条历史记录 0 finding；3 份合规 memo 0 finding | 同上 |
| R-6 | 盲评 | 8 维均分 +≥1.0；任一维不降 >0.5；≥6/10 偏好 memo | `data/eval/report_eval_v1/scores.csv` |
| R-7 | 降级可用 | LLM 连续失败时确定性报告含 1–3 条 claim 且可渲染 | `pytest tests/orchestrator/test_report_fallback.py -q` |
| R-8 | 契约兼容 | 旧 `report_raw`（无 layout/claims）反序列化 + 渲染不变 | `pytest tests/orchestrator/test_report_contracts.py -q` |

### 7.2 常量线

| # | 验收项 | 判据 | 命令 |
|---|---|---|---|
| P-1 | registry 合法 | 15 条 id 唯一、值类型/边界通过、`kind=policy` | `pytest tests/params/test_registry.py -q` |
| P-2 | 常量一致 | dataclass 默认值逐项等于模块常量 | 同上 |
| P-3 | 默认零变化 | 默认参数下 `evaluate` 输出与 golden 逐字节一致（剔除 additive 字段） | `pytest tests/midterm/test_v1_artifact_golden.py tests/midterm/test_params_injection.py -q` |
| P-4 | 注入生效 | 覆盖 `beat_bonus` 后仅预期字段变化；状态分布变化可解释；`score_breakdown.f` 与 `variable_scores.f_fundamental_trend` 同源一致 | `pytest tests/midterm/test_params_injection.py -q` |
| P-5 | dispatch 真接线 | v2 收到传入 params（monkeypatch 捕获）；v1 忽略时有 warning | `pytest tests/midterm/test_params_injection.py -q` |
| P-6 | hash 稳定 | 同参数两次相同；override 后变化；旧帧为空 | `pytest tests/params/test_hash.py -q` |
| P-7 | 指纹 | 600760 + 9 合成用例通过；报告 5×2 布局指纹通过 | `pytest tests/midterm/test_params_fingerprint.py tests/apps/test_report_fingerprint.py -q` |
| P-8 | 影子 | `params_shadow.py` 对 600760 输出 old/new/diff，`decision_changed` 正确 | 脚本冒烟 |
| P-9 | 全量回归 | `pytest -q` 全绿；`ruff check` 全绿 | 仓库标准 |

### 7.3 防"自证"要求

- 每个门禁至少一个**负对照**（合法报告 0 finding）和一个**正对照**（变异必红）；
- 每个"默认零变化"断言必须用**逐字节**比较，不允许"字段等价"代替；
- 指纹更新必须有人工理由与 ROADMAP 行，不允许测试自动写回；
- A/B 结论必须同时报告客观指标（字符/数字/claim 数）与主观 8 维分，不允许只报均分。

---

## 8. 风险与回滚

| # | 风险 | 触发信号 | 缓解 | 回滚 |
|---|---|---|---|---|
| R1 | LLM 不服从字数预算 | 主文 >10k 比例高 | prompt 固定骨架 + 明确"超预算即失败" + G4 报警；重写时带"压缩到 X 字"提示 | `report.mode: legacy` |
| R2 | claims 编造 evidence ref | G1 命中率高 | `allowed_refs` 校验 + `inferred` 显式 + 降级报告豁免 | 关闭 claims（`memo_lite`） |
| R3 | G3 数字误伤 | 观察期未命中样本多 | 先 observe、记录 misses、白名单、三种量纲解释；FP=0 才 enforce | `report.gates.mode: observe` |
| R4 | issue 消息重复拼接 | 附录 G 体积异常 | 报告侧 600 字截断 + 完整 message 留 TaskRecord；上游 `research.py:1610` 去重作为 P1 工单 | 还原截断 |
| R5 | 参数注入改变默认行为 | 默认输出 diff 非空 | dataclass 默认=常量 + 逐字节测试 + `params_hash` | 回退 PR-7 |
| R6 | frozen 参数被持久化覆盖 | config 出现 `f_marginal_weight` | config 拒绝 frozen + 只允许 experiment + 输出标注 | 删除 override |
| R7 | 两个渲染面漂移 | 同一报告终端/文件标题不一致 | 单一 `report_layout.py` + 双面测试 | 回退 PR-2 |
| R8 | 指纹被随意更新 | 指纹 PR 无 ROADMAP 行 | `update_fingerprint.py --reason` + 评审检查 | revert 指纹 |
| R9 | A/B 被 LLM 随机性混淆 | 同模式两次均分差 >0.5 | 每模式重复 2 次（抽 2 标的）+ 盲评 + 报告噪声 | 扩大重复次数 |
| R10 | 评估集数据过期 | payload 日期与报告日期不一致 | 同一 W2 窗口内 capture；README 记日期/模型/prompt 版本 | 重新 capture |
| R11 | 报告阈值又成新常量 | prompt 里出现硬编码 10000/500 | 所有阈值进 registry（§5.1），prompt 只引用变量名 | 补登记 |

**总回滚开关**：

```yaml
report:
  mode: legacy          # legacy | memo_lite | memo_claims
  gates:
    mode: observe       # observe | enforce

parameters:
  profile: default
  overrides: {}
```

默认值 = 当前行为；任何一项回到默认即完全回滚（新增字段为 additive，可留在 JSON 中）。

---

## 9. 附录

### 附录 A：memo prompt 主文段（可直接落进 `prompts.py`）

> 说明：附录六节沿用现有 `REPORT_GENERATOR_PROMPT` 的对应段落原文；以下只给**新增/替换**的主文段与全局约束。`{{...}}` 占位沿用现有模板。

```text
## 本次输出模式：投资备忘录（memo）

你只写"论证"，不写"底稿"。主文回答"结论是什么、为什么、哪里可能错、看什么"；
明细全部放附录，附录不重复主文的论证。

### 全局约束
1. 主文六个 section 合计 ≤10000 字；每个 section 不得超过其预算。
2. 主文不得逐条罗列信号/异常/冲突/维度；只引用支撑当前主张的 2–4 条证据。
3. 每个数字必须带口径（财报期/行情日）；不得出现输入 JSON 之外的数字。
4. 附录允许长，但只放明细与清单，不再写"与 core_view 的关系"。
5. 主文引用附录时写"见附录 X"，不要复制附录内容。

### company_overview（≤450 字，渲染在摘要之前）
面向"完全不了解这家公司的人"：做什么/卖给谁、主导业务线与收入结构、
行业与上市地、规模锚点；事实只取自 `company.profile` / `company_track`，
缺失即省略，不写观点与评价。

### executive_summary（≤800 字）
固定四段，不要写成散文：
【主张】一句话，可反驳。
【三条最强证据】每条 ≤60 字，含数字与口径。
【两条最强反证】每条 ≤60 字，说明它削弱了什么。
【失效条件】指标 + 阈值 + 时间窗 + 来源，四者齐全。

### investment_viewpoint（≤1600 字，2–3 条主张）
每条主张固定六段，段首用加粗标签：
**主张**：一句可反驳的话（≤80 字）。
**依据**：2–4 条，每条含数字、口径、来源；没有来源就写"推断"。
**推理**：依据为什么支持主张（机制，不写"综上"）。
**反证**：最强的一条反证，以及为什么它没有推翻主张。
**限定**：置信度（高/中/低）+ 适用时间窗。
**失效条件**：指标 + 阈值 + 时间窗 + 来源。
禁止：把 supporting_evidence 逐条复述；禁止"可能/或许/值得关注"式空话。

### scenario_analysis（≤1200 字）
base / bull / bear 各 ≤300 字；每个情景写"前提条件 + 推演 + 触发条件"。
不再逐项回扣 materiality_rank。

### risks（≤1200 字，标题语义 = 我们可能错在哪里）
从 counter_evidence 选 ≥3 条，逐条写"如果…则…"；说明它会如何改变主张与仓位含义。
禁止模板风险词（宏观下行、竞争加剧）除非有本标的证据。

### falsification_conditions（≤1000 字，标题语义 = 观察清单）
Markdown 表格 ≤6 行，列：看什么 | 何时变 | 什么情况下错 | 数据来源。
每行必须可观测、可判定；不得写"若基本面恶化则降级"这类不可观测条件。

### key_metrics（≤800 字）
6–8 行表格（指标/数值/口径/解读）+ 一段"这些指标如何支撑或挑战主张"。
```

### 附录 B：`params/registry.yaml` v0（全文）

```yaml
schema_version: 1
registry_version: 1

entries:
  - id: midterm.score.fundamental_yoy_scale
    value: 30.0
    unit: pp
    domain: midterm
    kind: policy
    status: placeholder
    source: design
    version: 1
    owner: captain
    review_condition: "真实帧 >= 50 且饱和率统计完成"
    wired_in: ScoreParams.fundamental_yoy_scale
    overridable: true
    bounds: [10.0, 60.0]
    notes: "营收/净利/EPS YoY 饱和点；30% -> ±1"

  - id: midterm.score.ocf_negative_penalty
    value: 0.5
    unit: score
    domain: midterm
    kind: policy
    status: placeholder
    source: design
    version: 1
    owner: captain
    review_condition: "真实帧 >= 50 且 OCF 分组收益统计完成"
    wired_in: ScoreParams.ocf_negative_penalty
    overridable: true
    bounds: [0.0, 1.0]
    notes: "仅 OCF<0 扣分；FCF 不再参与（2026-11-01 变更）"

  - id: midterm.score.beat_bonus
    value: 0.2
    unit: score
    domain: midterm
    kind: policy
    status: placeholder
    source: design
    version: 1
    owner: captain
    review_condition: "真实帧 >= 50 且 beat/miss 可结算"
    wired_in: ScoreParams.beat_bonus
    overridable: true
    bounds: [0.0, 0.5]
    notes: "net_profit_yoy 超预告上限加分"

  - id: midterm.score.miss_penalty
    value: 0.3
    unit: score
    domain: midterm
    kind: policy
    status: placeholder
    source: design
    version: 1
    owner: captain
    review_condition: "真实帧 >= 50 且 beat/miss 可结算"
    wired_in: ScoreParams.miss_penalty
    overridable: true
    bounds: [0.0, 0.5]
    notes: "miss 信息量更大，故 0.3 > beat 0.2"

  - id: midterm.score.miss_gap_threshold
    value: 20.0
    unit: pp
    domain: midterm
    kind: policy
    status: placeholder
    source: design
    version: 1
    owner: captain
    review_condition: "真实帧 >= 50 且 gap 分布统计完成"
    wired_in: ScoreParams.miss_gap_threshold
    overridable: true
    bounds: [5.0, 50.0]
    notes: "实际低于预告下限 >20pp 判显著 miss"

  - id: midterm.score.segment_divergence_threshold
    value: 15.0
    unit: pp
    domain: midterm
    kind: policy
    status: placeholder
    source: design
    version: 1
    owner: captain
    review_condition: "真实帧 >= 50 且分部样本足够"
    wired_in: ScoreParams.segment_divergence_threshold
    overridable: true
    bounds: [5.0, 50.0]
    notes: "最快分部增速跑赢整体 >15pp 视为结构性亮点"

  - id: midterm.score.segment_divergence_bonus
    value: 0.2
    unit: score
    domain: midterm
    kind: policy
    status: placeholder
    source: design
    version: 1
    owner: captain
    review_condition: "真实帧 >= 50 且分部样本足够"
    wired_in: ScoreParams.segment_divergence_bonus
    overridable: true
    bounds: [0.0, 0.5]
    notes: "F 方向分正向修正"

  - id: midterm.score.f_marginal_weight
    value: 0.30
    unit: ratio
    domain: midterm
    kind: policy
    status: frozen
    source: design
    version: 1
    owner: captain
    review_condition: "F 边际开权 G-8 全绿（真实帧 >= 50 + 四项翻转门槛）"
    wired_in: ScoreParams.f_marginal_weight
    overridable: true
    bounds: [0.0, 0.4]
    notes: "结构性示意；默认冻结，只允许 --param-override 实验并标注 experiment=true"

  - id: midterm.score.anchor_weight
    value: 0.6
    unit: ratio
    domain: midterm
    kind: policy
    status: active
    source: design
    version: 1
    owner: captain
    review_condition: "与 research_weight 组约束复核"
    wired_in: ScoreParams.anchor_weight
    overridable: true
    bounds: [0.0, 1.0]
    group: e_blend
    constraint: {sum: 1.0}
    notes: "预告锚权重（P0-6 重平衡后）"

  - id: midterm.score.research_weight
    value: 0.4
    unit: ratio
    domain: midterm
    kind: policy
    status: active
    source: design
    version: 1
    owner: captain
    review_condition: "与 anchor_weight 组约束复核"
    wired_in: ScoreParams.research_weight
    overridable: true
    bounds: [0.0, 1.0]
    group: e_blend
    constraint: {sum: 1.0}
    notes: "研报修正权重"

  - id: midterm.data.min_consensus_coverage
    value: 3
    unit: count
    domain: midterm
    kind: policy
    status: placeholder
    source: design
    version: 1
    owner: captain
    review_condition: "真实帧 >= 50 且 coverage 分布统计完成"
    wired_in: "ScoreParams.min_consensus_coverage + build_numeric_evidence(min_consensus_coverage=)"
    overridable: true
    bounds: [1, 10]
    notes: "修复两处重复定义（score_engine.py:51 / evidence_rules.py:90），P1 统一透传"

  - id: midterm.evidence.temperature
    value: 0.22
    unit: nats
    domain: midterm
    kind: policy
    status: active
    source: backtest
    version: 1
    owner: captain
    review_condition: "真实帧 >= 50 后复核语料分位数门"
    wired_in: EvidenceParams.temperature
    overridable: true
    bounds: [0.05, 1.0]
    notes: "形状约束 (a)(b) 确认；语料分位数门待复核"

  - id: midterm.evidence.uncertain_p_top
    value: 0.55
    unit: ratio
    domain: midterm
    kind: policy
    status: placeholder
    source: design
    version: 1
    owner: captain
    review_condition: "真实帧 >= 50 后复核"
    wired_in: EvidenceParams.uncertain_p_top
    overridable: true
    bounds: [0.3, 0.9]
    notes: "P_top < 阈值 => uncertain"

  - id: midterm.evidence.break_lambda
    value: 2.0
    unit: score
    domain: midterm
    kind: policy
    status: active
    source: backtest
    version: 1
    owner: captain
    review_condition: "关键格比例复核（两序均 >= 90%）"
    wired_in: EvidenceParams.break_lambda
    overridable: true
    bounds: [1.0, 3.0]
    notes: "F×E 双击证伪通路权重；S5 cap 必须同步 max(1.5, lambda)"

  - id: midterm.evidence.s0_floor
    value: 0.10
    unit: score
    domain: midterm
    kind: policy
    status: active
    source: backtest
    version: 1
    owner: captain
    review_condition: "偏多帧 S0 占比与无信号帧 P(S0) 复核"
    wired_in: EvidenceParams.s0_floor
    overridable: true
    bounds: [0.0, 0.3]
    notes: "2026-11-01 由 0.30 下调；解决 S0 恒定比较优势"
```

> `structure` 类型不允许出现在 registry（见 §4.3）；`EVIDENCE_CAPS` 等结构常量仍留在代码。

### 附录 C：claims JSON 示例（供 prompt 示例与测试夹具）

```json
{
  "title": "600760.SH 中航沈飞 投资分析报告 — 财报口径 20260930 / 行情口径 20261030",
  "layout": "memo",
  "claims": [
    {
      "id": "claim-1",
      "text": "中航沈飞当前的核心矛盾是毛利率中枢从 13.7% 下移到 10% 附近，而非收入下滑本身。",
      "grounds": [
        {
          "statement": "前三季毛利率同比继续下移",
          "ref": "gross_margin",
          "value": "10.07%（同比 -1.76pp）",
          "source": "fina_indicator 20260930"
        },
        {
          "statement": "Q3 单季收入放量而毛利率未回升",
          "ref": "income",
          "value": "Q3 单季收入 +23.29%，毛利率约 9.63%",
          "source": "income 20260930"
        }
      ],
      "warrant": "若为固定成本摊薄，收入放量应带来毛利率回升；量增而毛利降，指向定价或型号结构位移。",
      "rebuttal": "Q3 单季归母 +144%、扣非 +40.7%，2026 年零存货计提，恶化尚未兑现。",
      "qualifier": "中等置信，适用于 2026Q4–2027Q1。",
      "falsification": [
        {
          "condition": "Q4 单季毛利率回到 12% 以上，合同负债回补",
          "kind": "disconfirm",
          "metric": "Q4 单季毛利率",
          "threshold": ">=12%",
          "window": "2026 年报",
          "source": "2026 年报 + 资产负债表"
        }
      ],
      "material": true,
      "inferred": false,
      "claim_type": "causal"
    }
  ],
  "sections": {
    "executive_summary": "……",
    "investment_viewpoint": "",
    "scenario_analysis": "……",
    "risks": "……",
    "falsification_conditions": "……",
    "key_metrics": "……",
    "signal_analysis": "……",
    "anomaly_detection": "……",
    "conflict_analysis": "……",
    "dimension_analysis": "……",
    "company_track": "……",
    "review_findings": "……",
    "disclaimer": "……"
  }
}
```

### 附录 D：`count_main_chars` 与 `report_shape` 规范（三个消费者共用）

```python
def report_shape(markdown: str, layout: str) -> dict[str, int]:
    """返回 main_chars / appendix_chars / issue_chars / total_chars。

    layout="legacy"：main_chars = 13 节正文之和（即读者必须读的全部论证）
    layout="memo"  ：main_chars = 公司概况 + 一页结论..关键指标 六节之和（不含附录）
    两口径都从**渲染后的 markdown** 计算，保证与门禁 G4、A/B 指标、基线脚本一致。
    """
```

- 分割依据：`## ` 标题 + `report_layout` 的 key→位置映射，不靠正则猜语义；
- 元信息、标题行、空行不计；
- `issue_chars` / `midterm_chars` 单独统计（避免主文降了、总量没降的错觉）。

### 附录 E：基线复现命令与原始输出

```bash
poetry run python - <<'PY'
import pathlib, re
for p in sorted(pathlib.Path('data/reports').glob('*.md')):
    t = p.read_text(encoding='utf-8')
    print(p.name, len(t), len(re.findall(r'\d+(?:\.\d+)?%?', t)))
PY
# 输出（2026-11-08）：
# 300504.SZ-2026-10-30.md 45590 1614
# 600760.SH-2026-11-01.md 42605 1846
# 兴森科技-2026-10-17.md 18694 437
# 海康威视-2026-10-22.md 42921 1685
# 生益科技-2026-10-19.md 39291 1653
```

分节统计口径见 §1.1；Q0 脚本应把该口径固化，不再用一次性脚本。

### 附录 F：ROADMAP 行为变更登记模板（每个 PR 一行）

```markdown
| 2026-11-XX | **报告 memo 布局 / Q1-lite（PR-2/3）**：新增 `report.mode`（legacy/memo_lite）、`ReportOutput.layout`（additive，默认 legacy）；memo 下主文六节 + 附录 A–H、系统问题摘要 + 附录 G、中期块移附录 H | 本文 §3.2；`docs/implementation/report/REPORT_ARGUMENT_SPEC.md` | 默认 `legacy` ⇒ 渲染逐字节不变；新增字段为 additive、旧 JSON 可反序列化；回滚 = `report.mode: legacy` |
| 2026-11-XX | **报告 claims 契约 / 门禁（PR-4/5）**：`ReportOutput.claims` + `FalsificationCondition` 四字段；G1–G4 默认 observe | 本文 §3.3/§3.4 | 不写 claims ⇒ 旧路径；门禁 observe 不改 passed；回滚 = `report.mode: memo_lite` + `report.gates.mode: observe` |
| 2026-11-XX | **参数治理 P0/P1（PR-6/7）**：新增 `params/registry.yaml`（15 条）与 `ScoreParams`/`EvidenceParams` 注入；`params_hash` 写入帧/记录/报告元信息 | 本文 §4；`docs/guide/CONFIGURATION.md` | 默认值逐项等于模块常量 ⇒ 默认输出逐字节不变（golden + 指纹双钉）；回滚 = 删除 `parameters` 段 + revert PR-7 |
| 2026-11-XX | **行为指纹 P2（PR-8）**：600760 + 9 合成用例、5 份报告 × legacy/memo 渲染指纹 | 本文 §4.9 | 纯测试资产，不改运行时；回滚 = 删夹具/脚本 |
```

### 附录 G：实施锚点速查

| 事项 | 文件:行 |
|---|---|
| 报告 prompt | `alphabee/orchestrator/prompts.py:8` |
| 报告生成/降级 | `alphabee/orchestrator/reporter.py:232` / `:71` |
| 报告 schema | `alphabee/agents/schemas.py:248`（Sections）/ `:264`（ReportOutput） |
| 13 节 markdown 渲染 | `alphabee/apps/cli/task_report.py:41` / `:111` / `:150` |
| 13 节终端渲染 | `alphabee/apps/cli/renderer.py:743` |
| 报告 gate | `alphabee/orchestrator/gates.py:455` / `:222` / `:345` |
| 报告 payload | `alphabee/orchestrator/services/payload_builders.py:415` |
| 分类器 dispatch | `alphabee/midterm/classifier_dispatch.py:93`（缺 params 的调用在 `:115`） |
| EvidenceParams | `alphabee/midterm/classifier_v2.py:111`（常量 `:75-91`） |
| F 评分常量 | `alphabee/midterm/score_engine.py:45-128` |
| F 分解唯一入口 | `alphabee/midterm/score_engine.py:434` |
| 决策装配 | `alphabee/midterm/decision_model.py:635` |
| 状态 artifact | `alphabee/midterm/models.py:626` |
| 证据规则（coverage 重复定义） | `alphabee/midterm/evidence_rules.py:90` / `:714` |
| 任务记录 | `alphabee/task_records/models.py:63` |
| golden 测试 | `tests/midterm/test_v1_artifact_golden.py`（`_NEW_FIELDS`） |
| 行为变更登记 | `docs/roadmap/ROADMAP.md`「行为变更登记」表 |
| 配置说明 | `docs/guide/CONFIGURATION.md` |

### 附录 H：术语

| 术语 | 含义 |
|---|---|
| memo / memo_lite / memo_claims | 三种输出模式：只换布局 / 布局+prompt 减法 / 布局+claims schema+门禁 |
| material claim | 同时含方向词、数字/比较、非复述的判断句（§1.5） |
| core claim | `claims[]` 中 `material=true` 的条目（Q2 默认全部） |
| 主文 | 一页结论 + 核心主张 + 情景 + 反证 + 观察清单 + 关键指标 |
| 附录 | A–H：明细、清单、中期块；不参与主文字数 |
| params_hash | 15 个生效参数值的 sha256 前 16 位（§4.7） |
| 指纹 | 冻结输入 → canonical 输出 → 哈希；任何行为变化都会改哈希（§4.9） |
| G0/G1/G2/G3 | 实施决策门，不是报告门禁（§6.6） |
