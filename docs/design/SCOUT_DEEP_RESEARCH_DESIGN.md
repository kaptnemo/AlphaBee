# Scout 深度探索与能力沉淀设计（Deep Research Scout Design）

> 状态：设计稿（未实施）
> 定位：回答 Scout 如何产生"有建设性的发现"、如何在不同角度/层次切换、理论上能工程化到什么程度、有哪些可借鉴的研究与工程实践。
> 关联：`docs/design/APIARY_RESEARCH_OS_DESIGN.md`（总体架构）、`docs/implementation/apiary/POC_SPEC.md`（实施范围）、`docs/roadmap/DOMAIN_CONTEXT_ROADMAP.md`（playbook/primitive）、`docs/roadmap/AGENT_STRATEGY_ROADMAP.md`（W3/W5）、`docs/design/SINGLE_FRAME_QUALITY_DESIGN.md`（claim/论证质量）。
> 纪律：本文只定义机制、契约与验收；不含实现细节；不替代总体架构文档。

---

## 0. 结论摘要

1. **固定 pipeline 是底座，不是敌人。** 它负责已知世界的快速路径；行业/阶段异质性由 playbook/primitive + Scout 补足。
2. **Scout 是发现机制，不是改进引擎。** 确定性提升来自"毕业 → 测试 → 黄金案例 → 复用"，不是来自自由探索。
3. **有用发现 = 非显然 + 决策相关 + 可检验 + 可转换。** 不能落到资产/方法/数据/负结果的发现，不进 backlog。
4. **发现分四族**：知识型、资产型、方法型、负结果型；按"形式 × 内容层"分类，而不是按"听起来深不深"。
5. **抽象分五层**：案例观察 → 案例问题 → 机制模板 → playbook 变体 → 通用原语。先具体，再定期抽象；机制模板不是行业标签。
6. **视角切换可以工程化"机制"，不能保证"合适"。** 机制 ~80%，合适性 ~40–60%，真正洞见 ~10–30%，保证提升 0%。
7. **工程形态**：常驻 TraceRecorder + 按需拉取 trace 的 Scout；不做 live sidecar。
8. **相关研究与工程很多，但没有完全同构的"研究能力工程"**；最接近的是 Voyager 的技能库、AI co-scientist 的辩论、Experience Factory 的度量、DeepResearch Bench 的评测。

---

## 1. 背景与问题

### 1.1 固定 pipeline 的瓶颈

AlphaBee 的固定 pipeline 是"一个研究模板"：取数 → 派生/信号/异常 → 冲突 → 验证 → 洞察 → 论点 → 审查 → 报告。它对多数标的能产出"及格线"研究，但：

- 行业/阶段异质性被压扁：军工总装、周期资源、消费品牌、创新药、银行，用的是同一套问题；
- 专用 playbook 覆盖太薄：当前只有 `generic_fundamental`、`hog_cycle`、`mining_services` 三个；
- 大多数公司走 fallback：600760（军工总装）的 `driver_profile fallback=true`，只能走通用路径；
- 研究问题被 schema 框定：只能问"工具能算"的问题，问不出"这个行业真正该问什么"。

**这不是代码写得不够，是研究本身的异质性没有被建模。**

### 1.2 已有资产

| 资产 | 现状 |
|---|---|
| `alphabee/domain_context/domain_playbooks/` | 3 个 playbook（generic_fundamental / hog_cycle / mining_services） |
| `alphabee/domain_context/domain_primitives/` | 6 个 primitive（commodity_cycle / capacity_cycle / cost_curve / working_capital_stress / biological_inventory / project_delivery） |
| `ContextRouter` | 规则版公司 → playbook 匹配，含 fallback/降级 |
| `driver_profile` | 驱动假设 + 研究议程，但多数标的 fallback |
| `company_track` | business model + peer + segment |
| `industry_context` | 行业基线 |
| `TaskRecord` | 任务摘要，不是完整 trace |
| Langfuse | 可选，默认关闭；不保证 tool/API/artifact 全覆盖 |

结论：**异质层的骨架已经有了，缺的是内容覆盖、抽象层级、验证和 trace。**

### 1.3 目标与非目标

**目标**

- 让 Scout 产出"可吸收的发现"，而不是"更多问题"。
- 让发现能按形式/内容分层，分别进入能力库、方法库、数据 backlog、负结果库。
- 让 Scout 能在角度/层次间切换，并记录切换理由与效果。
- 让"能力提升"可验证：held-out 案例、A/B/C/D、回归集。

**非目标**

- 不做通用 AGI / 全自主研究。
- 不保证洞见或收益。
- 不替代人的判断与领域知识。
- 不追求"更多能力/更多问题"的数量指标。

---

## 2. 定位：Scout 是发现机制，不是改进引擎

### 2.1 四个角色

| 角色 | 职责 | 确定性 |
|---|---|---|
| **Pipeline** | 已知世界的快速路径 | 编排固定 + 有界 LLM + 确定性计算 |
| **Scout** | 未知世界的探索、盲区、问题 | 随机/启发式 |
| **Capability/Method Library** | 可复用资产 | 确定性（能力）/半结构化（方法） |
| **Apiary** | 发现 → 立项 → 开发 → 毕业 | 流程确定性 |

### 2.2 改进链

```text
Scout 探索
  → Discovery Card（发现卡）
  → Apiary triage（形式/内容/去向）
  → 能力 / 方法 / 数据 / 负结果
  → 测试 + 黄金案例 + held-out 验证
  → 毕业进能力库/方法库
  → pipeline / Scout 复用
  → 回归监控
```

**确定性在"毕业"之后，不在"探索"之中。**

### 2.3 Scout 能/不能保证什么

| 能 | 不能 |
|---|---|
| 发现 pipeline 没问的问题 | 保证问题有价值 |
| 发现口径/证据/反例 | 保证结论正确 |
| 提出候选机制/playbook/原语 | 保证机制成立 |
| 暴露"不可识别/数据不足" | 保证能力提升 |
| 生成 held-out 评估案例 | 保证泛化 |

### 2.4 工程形态：Recorder + Pull-based Scout

Live sidecar 的唯一不可替代价值是"在 run 中干预"；如果 Scout 比 pipeline 慢，这个价值很小，代价却很大（事件总线、背压、实时耦合）。因此：

```text
AlphaBee runtime
  ├─ TraceRecorder（常驻、轻量、不调 LLM）
  │    ├─ LLM span：model / prompt_hash / response_hash / tokens
  │    ├─ Tool span：name / args_hash / result_hash
  │    ├─ API span：endpoint / params_hash / response_hash
  │    └─ artifact / decision / issue refs
  ↓
Trace Store（JSONL + 内容寻址 blob）
  ↑
TraceReader（按 run / node / claim / span 查询）
  ↑
Scout（盲读 → 落稿 → 按需读 trace → 对账）
```

- `TaskRecord` 只做**索引**：找 run、看摘要。
- 细节去 Trace Store；Scout 按需拉 L2/L3，不把整份日志塞进上下文。
- Trace 分四级：L0 artifact/decision/issue；L1 元数据+hash；L2 完整 prompt/response/tool args；L3 原始 API 响应/文档快照。
- 外部新闻/研报按"数据，不是指令"处理；redaction + 保留期。

---

## 3. 什么算"有用的发现"

### 3.1 定义

> **有用发现 = 非显然（non-obvious）+ 决策相关（decision-relevant）+ 可检验（testable）+ 可转换（convertible）。**

- **非显然**：不在现有报告/能力库/常识里。
- **决策相关**：改变判断、仓位、观察清单，或明确"不影响决策"。
- **可检验**：有证据需求、竞争解释、证伪条件。
- **可转换**：能变成能力/方法/playbook/口径规则/黄金案例/数据请求/负结果。

### 3.2 形式分类

| 形式 | 例子 | 去向 | 验证 |
|---|---|---|---|
| **新问题** | “验收确认 + 政策/订单驱动的收入集中度是否可预测？”跨 3 案例重复 | Playbook 问题集 | 跨案例覆盖 |
| **新假设/机制** | 交付节拍 → 固定成本吸收 → 毛利率 | 原语/测试 | 机制证据 + 反例 |
| **反例** | 规则在 600760 上判错 | 规则修订/退役 | 回放 |
| **失败模式** | FY vs 9M 混用；长报告埋没核心论证 | 口径规则/门禁/prompt | 已知缺陷 fixture |
| **口径规则** | 13.70% FY 与 10.07% 9M 不可直接比；同口径 11.83%→10.07% | 硬门禁 | 单测 + 真实案例 |
| **原语** | 固定成本吸收、Q4 交付集中度、合同负债覆盖率 | Layer 1 能力库 | 确定性测试 + 2 案例 |
| **Playbook** | 军工/项目交付 archetype | Layer 2 | 3 案例 + fallback 率 |
| **方法卡** | 如何区分交付节奏与竞争力下降 | 方法库 | held-out 案例 |
| **黄金案例** | 600760 期间混用错误 | 回归集 | 回归测试 |
| **数据请求** | 9M 折旧、分部成本、订单/产能 | 数据 backlog | 可得性审计 |
| **负结果** | 无量价数据，量价分解不可识别 | 明确 park | 记录“不可识别” |
| **退役/降级** | 规则重叠/失效 | 规则注册表 | 消融/回放 |
| **分类学补丁** | 新增军工总装/出口制造/创新药 archetype | Playbook 路由 | 覆盖矩阵 |
| **评估案例** | A/B/C/D brief 对比 | 评估集 | 盲评 |

四族：

- **知识型**：问题、假设、反例、失败模式；
- **资产型**：口径规则、原语、playbook、黄金案例；
- **方法型**：方法卡、分类学补丁、评估案例；
- **负结果型**：不可识别、数据缺口、退役/降级。

### 3.3 内容层

| 内容层 | 典型发现 | 常见形式 |
|---|---|---|
| 数据与口径 | 期间混用、单位错、修订未追踪、stale peer、派生/直报冲突 | 口径规则、数据请求、黄金案例 |
| 计算与分解 | 量价/结构/成本分解、杜邦、现金桥、估值反推 | 原语、能力卡 |
| 因果机制 | 交付节拍→固定成本吸收→毛利率；产能周期→价格 | 假设、原语、方法卡 |
| 行业/archetype | 军工总装、出口制造、周期资源、消费品牌、创新药 | Playbook、分类学补丁 |
| 预期/估值 | 市场在交易 PE 折价还是 PB-ROE；一致预期修正 | 方法卡、评估案例 |
| 会计/治理 | 收入确认、资本化、关联交易、激励 | 口径规则、方法卡 |
| 方法论 | 如何区分竞争解释、设证伪条件、选基期 | 方法卡、评估案例 |
| 评估/元 | 报告形状、prompt 偏差、Scout 继承 pipeline 框架 | 失败模式、评估案例、门禁 |

### 3.4 有用性评分

**四道必过门**：

1. 有证据（artifact/observation/原文 locator）；
2. 有决策相关性（改变判断/仓位/观察清单，或明确不影响）；
3. 有去向（能力/方法/数据/负结果）；
4. 有验证计划（fixture/held-out/回放/A-B）。

**分级**：

- **A 级（资产就绪）**：可进 Layer 1/2，有测试和 2 案例；
- **B 级（待验证）**：有潜力，需更多案例/数据；
- **C 级（方法/数据）**：不能代码化，进方法库/数据 backlog；
- **D 级（负结果）**：明确不可回答/不可识别，park 并记录原因；
- **E 级（噪声）**：泛泛而谈、不可证伪、重复、无证据，丢弃。

### 3.5 发现卡

```text
discovery_id
form            # question | hypothesis | counterexample | failure_mode |
                # caliber_rule | primitive | playbook | method | test_case |
                # data_request | negative_result | deprecation | taxonomy_patch | evaluation_case
content_layer   # data | computation | mechanism | archetype | expectation |
                # accounting | methodology | evaluation | meta
statement
evidence_refs[]
recurrence      # cases / industries
decision_impact # high / medium / low
identification  # exact / partial / not_identifiable
convertibility  # asset / method / data / none
destination
verification_plan
status          # candidate / triaged / validated / graduated / rejected
```

### 3.6 好/坏例子（600760）

| 发现 | 形式 | 价值 |
|---|---|---|
| 13.70% 是 FY2025，10.07% 是 9M2026；同口径 2025 9M 是 11.83%，降幅 1.76pp 不是 3.6pp | 口径规则 + 黄金案例 | A |
| 折旧/收入从 2025 年 2.19% 升到 2026H1 8.59%；9M 折旧缺失 | 原语 + 数据请求 | A/B |
| 军工总装没有专用 playbook；`project_delivery` 原语已存在但未组合 | Playbook + 分类学补丁 | A |
| 没有销量/单价，量价分解不可识别 | 负结果 | A |
| peer 数据 as_of=20260630，stale；peer_revenue_yoy 是单成分缺陷 | 口径规则 | A |
| “管理层很重要” | 泛泛而谈 | E |
| “需要更多数据” | 不具体 | E/C |
| “毛利率可能下降” | 已知/不可证伪 | E |

**最有价值的发现往往很“土”**：口径规则、原语、playbook、负结果、黄金案例。

### 3.7 Scout 不应输出

- 泛泛的行业评论；
- 不可证伪的断言；
- 已有答案的问题；
- 无数据路径的问题；
- 已有能力的重复；
- “需要更多研究”但没有具体证据/数据；
- 没有 falsifier 的建议。

---

## 4. 抽象层级：从案例到机制，再到 playbook/原语

### 4.1 五层

| 层级 | 形态 | 例子 | 上升条件 |
|---|---|---|---|
| **L0 案例观察** | 单公司事实 | 600760 的 2025Q4 收入占比 53.85% | — |
| **L1 案例问题** | 单公司可证伪问题 | 2026Q4 收入占比能否重复 50%+？ | 有数据、有决策用途 |
| **L2 机制模板** | 跨案例机制假设 | 验收确认 + 政策/订单驱动 → 收入集中度可预测 | ≥3 案例同一机制 |
| **L3 Playbook 变体** | 机制 + 匹配 + 问题集 + 原语 | 军工总装 / 船舶 / 电网设备 / 建筑 | 模板在 held-out 有效 |
| **L4 通用原语** | 可计算变量 | revenue_recognition_concentration / backlog_coverage / seasonality_baseline | 跨行业可复用 |

### 4.2 对“政策/订单驱动行业”的抽象评价

“强政策驱动、周期性订单驱动行业的订单交付集中度是否可预测”：

- **优点**：比单案例宽，指向机制；
- **问题**：把“行业标签、需求驱动、收入确认方式、可预测性”混在一起；
- **更准确**：**当收入按交付/验收节点确认，且需求受政策/预算或订单周期驱动时，收入确认集中度是否可由订单/合同负债/交付节点/政策日历预测，并优于季节性基准？**

四层拆解：

| 层 | 变量 | 例子 |
|---|---|---|
| 收入确认模式 | 时点 / 完工百分比 / 交付验收 | 军工按验收单；船舶按完工进度 |
| 需求驱动 | 政策预算 / 订单 backlog / 项目周期 | 国防预算、电网投资、船东订单 |
| 集中度表现 | Q4 收入占比、单季毛利率、合同负债/收入 | 600760 的 2025Q4 收入占比 53.85% |
| 可预测性 | 预测变量、基准、误差 | 合同负债、订单、交付节点、政策日历 vs 季节均值 |

### 4.3 问题模板

```yaml
id: q.revenue_recognition_concentration
version: 1

applies_when:
  recognition_mode: [acceptance, delivery, milestone]
  demand_driver: [policy_budget, order_backlog, project_cycle]
  concentration: [q4, project_milestone]

question: >
  {{symbol}} 在 {{period}} 的收入确认集中度，
  是否可由 {{predictors}} 预测，并优于 {{baseline}}？

predictors:
  - contract_liabilities
  - order_backlog
  - delivery_milestones
  - policy_calendar
  - prior_year_same_quarter

targets:
  - q4_revenue_share
  - q4_gross_margin
  - next_quarter_revenue

baseline:
  - historical_seasonal_mean
  - prior_year_same_quarter

falsifier: >
  out-of-sample 误差不低于 baseline，
  或加入订单/政策变量后没有增量解释力。

decision: >
  是否把 Q4 集中度写入基准情景；
  是否在 Q4 前调整观察清单/仓位。
```

### 4.4 原语 / 案例库 / 验证

| 原语 | 作用 |
|---|---|
| `revenue_recognition_concentration` | 季度收入占比、Q4 集中度、历史分布 |
| `backlog_coverage` | 合同负债/下一季收入、订单/收入 |
| `delivery_milestone` | 交付节点、验收事件、项目进度 |
| `policy_calendar` | 预算/审批/政策事件时间线 |
| `seasonality_baseline` | 季节均值、去年同期、同比/环比基准 |

案例库至少 3–5 个同机制、不同子行业案例：

- 军工总装：600760、000768、600038；
- 船舶/重工：600150、601989；
- 电网/电力设备：600406（需确认收入确认模式）；
- 建筑/工程：601668（项目进度确认）。

每案例标注 `recognition_mode / demand_driver / customer_concentration / project_duration / policy_exposure / q4_revenue_share_history / contract_liabilities_history`。

验证：

- baseline：过去 3 年 Q4 占比均值、去年同期；
- 模型/规则：合同负债 + 订单 + 交付节点 + 政策日历；
- 指标：out-of-sample MAE/MAPE、方向准确率；
- 结论：不比 baseline 好，就不上升为 playbook。

### 4.5 先具体，再定期抽象

```text
3 个案例（具体问题）
  → 提炼机制（L2 模板）
  → 2 个 held-out 案例验证
  → 有效 ⇒ playbook（L3）
  → 跨 3 个相邻行业验证
  → 有效 ⇒ 通用原语（L4）
```

**升级门槛**：

- ≥3 个案例同一机制；
- 模板能区分竞争解释；
- held-out 优于 baseline；
- 能落到现有/可获取数据；
- 能改变一个决策。

### 4.6 过度抽象的风险

| 风险 | 表现 | 对策 |
|---|---|---|
| 标签化 | “政策驱动行业”匹配太多公司 | 用机制特征匹配，不用行业名 |
| 伪抽象 | 问题更宏大，但不可证伪/无 baseline | 每个模板必须有 observables + baseline + falsifier |
| 过度合并 | 军工/船舶/电网/建筑的预测变量不同 | 模板共享，变体分开 |
| 数据不可得 | 抽象问题需要订单/产能，但拿不到 | 先数据审计，再决定抽象层级 |
| 抽象早于证据 | 只有 1 个案例就推广 | 3 案例门槛 + held-out |

---

## 5. 角度与层次的灵活切换

### 5.1 切换的维度

| 维度 | 取值 |
|---|---|
| 分析单元 | 公司 / 业务线 / 行业 / 产业链 / 宏观 |
| 时间 | 季度 / 周期 / 3–5 年 / 长历史 |
| 因果层 | 会计 / 经营 / 竞争 / 政策 / 资本 |
| 利益相关者 | 管理层 / 客户 / 竞争对手 / 监管 / 股东 |
| 证据类型 | 定量 / 定性 / 案例 / 反例 |
| 模态 | 事实 / 机制 / 预期 / 估值 |

### 5.2 算子库

| 算子 | 作用 |
|---|---|
| `zoom_in` / `zoom_out` | 公司 ↔ 行业 ↔ 宏观 |
| `analogize` | 跨行业/跨案例类比 |
| `contrast` | 对照两个解释/两个标的 |
| `invert` | 反过来问（“如果 Scout 不提升呢？”） |
| `decompose` / `recompose` | 拆解/重组 |
| `change_stakeholder` | 换视角（管理层/客户/监管） |
| `change_horizon` | 换时间尺度 |
| `challenge_assumption` | 挑战前提 |
| `find_contradiction` | 找矛盾/异常 |
| `quantify` / `qualify` | 量化/定性 |
| `historical_analogy` | 历史类比 |
| `cross_industry` | 跨行业迁移 |

### 5.3 Switch Controller

```text
state: current_focus / explored_nodes / uncertainty / evidence / budget
triggers:
  stuck（无新证据）
  contradiction（矛盾累积）
  low_evidence（证据不足）
  diminishing_returns（边际收益下降）
  overconfidence（过度自信）
  scope_drift（跑题）
value: expected_info_gain × decision_impact / cost
policy: 规则 + LLM 提议 + 人工 override
```

**不要让 LLM 自由决定“切不切”**；让它提议，让控制器按规则/预算/人工裁决。

### 5.4 Critic checklist

```text
1. 同口径？期间/单位/范围/会计政策
2. 效应量？变化 vs 水平；是否用最戏剧化的基期
3. 竞争解释？至少 2 个；有没有判别证据
4. 反事实？没有这个原因会怎样
5. 反证相关？挑战机制还是只挑战时点
6. 证伪判别力？单条件、阈值可推导、有灰区
7. 数据质量？stale/derived/conflict/缺失
8. 洞见？共识是什么、变体是什么、payoff 是什么
```

### 5.5 切换质量评估

- **视角覆盖率**：每案例覆盖多少维度/层次；
- **切换效用**：人工评“这次切换是否带来新证据/新解释”；
- **洞见产出率**：每案例多少“非显然 + 决策相关 + 可检验” claim；
- **下游采用率**：多少发现卡变成能力/方法/playbook/测试；
- **成本**：token/时间/人工；
- **A/B**：单角度 vs 多角度控制器。

### 5.6 理论依据

| 理论 | 启示 |
|---|---|
| **Bounded rationality / satisficing（Simon）** | 不穷举；预算内“够好就停” |
| **Computational rationality / meta-reasoning（Russell；Cox & Raja）** | 选哪个角度/层次是一次计算选择 |
| **Information foraging（Pirolli & Card）** | 边际收益下降时切换“信息斑块” |
| **Explore/exploit（bandits）** | 角度/层次是“臂”；切换是探索 |
| **Analogy / conceptual blending（Holyoak & Thagard；Fauconnier & Turner）** | 洞见常来自跨域映射 |
| **Double-loop learning / reframing（Argyris；Schön）** | 不只改答案，还要改框架 |
| **Case-based reasoning（Leake）** | 复用旧案例的视角与失败模式 |
| **Means-ends analysis（Newell & Simon）** | 在手段—目的层级上下移动 |

### 5.7 工程化上限

| 能力 | 可工程化程度 | 瓶颈 |
|---|---:|---|
| 生成多个角度/层次 | ~80% | 相关性、领域知识 |
| 表示当前角度/层次 | ~80% | 本体设计 |
| 执行切换动作 | ~80% | 切换是否合适 |
| 判断该不该切/切到哪 | ~40–60% | 没有客观价值函数 |
| 产生真正洞见 | ~10–30% | 模型能力、领域知识、运气 |
| 评估洞见质量 | ~40–60% | 主观性、滞后 |
| 保证能力提升 | 0% | No Free Lunch |

---

## 6. 工程实现

### 6.1 Scout 协议

```text
frame
  → propose（3–5 个 claim）
  → evidence（工具，带引用）
  → critique（逐条挑战）
  → revise（改 claim / 标 contested）
  → abstract（重复模式 → 发现卡）
  → judge（覆盖/分歧/预算）
  → 不收敛且未超预算 ⇒ 回到 propose/evidence
  → 收敛/超预算 ⇒ finalize（memo + 发现卡 + 开放问题）
```

### 6.2 状态

```text
ScoutState
  case: CaseSnapshot
  question / decision
  round
  claims[]
  evidence[]
  critiques[]
  open_questions[]
  discovery_cards[]
  budget
  status: framing | proposing | evidence | critiquing | revising | judging | done | blocked
```

### 6.3 工具

只读：

- facts / financials / segments / market / peer / consensus；
- `TaskRecord` 索引；
- `TraceReader`（按 run/node/claim 拉 trace）；
- 公告/新闻检索（带 provenance）；
- 代码/文档检索（仅审计，不用于生成结论）。

**每条 material claim 必须带 evidence_ref，或显式标 `inferred`。**

### 6.4 Judge

确定性检查：

- material claim 无证据 → 不通过；
- causal claim 少于 2 个竞争解释 → 不通过；
- core claim 无 falsifier → 不通过；
- 事实错误/期间混用 → 不通过；
- 预算超限 → 停止；
- 连续两轮无新证据/无 claim 变化 → 停止；
- 高影响分歧未解决 → 升级人工。

### 6.5 输出

```text
ScoutMemo
  question / answer / confidence
  claims[]
  strongest_counter
  data_gaps[]
  blind_spots[]
  discovery_cards[]
  open_questions[]
```

### 6.6 文件落点

```text
alphabee/research/scout/
  state.py
  graph.py
  prompts.py
  tools.py
  judge.py
  cards.py
  trace.py
scripts/alphabee_poc/
  run_scout.py
tests/research/
  test_scout_protocol.py
  test_scout_blind_phase.py
  test_scout_stop_conditions.py
```

### 6.7 预算

- **Fast Scout**：2 轮、8 次工具、5 分钟；常规标的。
- **Deep Scout**：5 轮、30 次工具、20 分钟；重要标的/事件。
- 超预算返回部分结果 + 未完成问题，不抛。

### 6.8 人工门

- 研究问题/范围确认；
- 高影响分歧裁决；
- 发现卡进 backlog；
- 能力毕业批准。

### 6.9 反模式

- 自由聊天、无状态、无停止条件；
- 自己批自己、无工具、无证据；
- 无限循环；
- transcript 当产出；
- 只加问题、不加资产；
- 用“问题数/能力数”当质量。

---

## 7. 已有研究与工程实践

> 说明：2026 年的部分条目来自公开检索，未逐篇核验；仅作方向参照。

### 7.1 Deep Research / 研究型 Agent

- OpenAI Deep Research、Google Gemini Deep Research、Perplexity Deep Research：多步检索、长时研究、带引用报告。
- Anthropic [Building effective agents](https://www.anthropic.com/research/building-effective-agents)：workflow 与 agent 的分工。
- 评测：[DeepResearch Bench](https://iclr.cc/virtual/2026/poster/10008065)、GAIA、BrowseComp；专家咨询工作评测（[rubric + cognitive traps](https://export.arxiv.org/pdf/2605.17554)）。

**可借**：多步检索、引用、长任务编排、评测。
**缺口**：优化“一次回答”，不积累能力。

### 7.2 Self-evolving / Skill Library

- [Voyager](https://arxiv.org/abs/2305.16291)：技能库 + 自动课程 + 迭代提示。
- Dynamic Agent Skills：技能库 [生命周期综述与分类](https://ar5iv.labs.arxiv.org/html/2607.10113)。
- Ratchet：[self-evolving agent 的最小卫生配方](https://export.arxiv.org/pdf/2605.22148)。
- ToolMaker / CREATOR / ToolLLM、Agent Workflow Memory / ExpeL。

**可借**：技能生命周期、自动课程、验证后入库、退役。
**缺口**：多在代码/具身/任务环境；没有金融证据/时点/口径约束。

### 7.3 AI Scientist / 自动化科研

- [Sakana AI Scientist](https://sakana.ai/ai-scientist/)：idea → 实验 → 论文 → 评审。
- [Google AI co-scientist](https://research.google/blog/accelerating-scientific-breakthroughs-with-an-ai-co-scientist/)：多 Agent 生成/辩论/进化假设。
- FutureHouse Kosmos/Robin、InternAgent、FirstResearch、[STORM](https://arxiv.org/abs/2402.14207)。

**可借**：假设生成、辩论/进化、问题形成、闭环验证。
**缺口**：产出论文/发现，不是可回归的研究能力。

### 7.4 Critic / Debate / Perspective

- [Multi-agent debate](https://arxiv.org/abs/2305.14325)、ChatEval、MAD。
- [Reflexion](https://arxiv.org/abs/2303.11366)、[CRITIC](https://arxiv.org/abs/2305.11738)、Self-Refine。
- DebaToM 等。

**可借**：Critic 角色、对抗检查、视角切换。
**缺口**：同模型相关错误；辩论易变附和/为不同而不同。

### 7.5 Meta-reasoning / Abstraction / Search

- [Tree of Thoughts](https://arxiv.org/abs/2305.10601)、Graph of Thoughts、Buffer of Thoughts。
- [Self-Discover](https://arxiv.org/abs/2402.03620)。
- 理论：Computational Rationality、Bounded Rationality、Meta-reasoning、Information Foraging。

**可借**：推理路径搜索、算子组合、预算。
**缺口**：没有客观“合适性”函数。

### 7.6 Experience Factory / CBR / ResearchOps

- Experience Factory / QIP / GQM（Basili）：经验打包 + 度量。
- Case-Based Reasoning（Leake）：案例检索/适配/复用。
- Feature Store / Metric Layer、Design System、Paved Road。
- 工具：[LangGraph](https://github.com/langchain-ai/langgraph)、[Langfuse](https://langfuse.com/)、[DSPy](https://github.com/stanfordnlp/dspy)、[MemGPT/Letta](https://arxiv.org/abs/2310.08560)。

**可借**：能力/方法库、指标、可观测性、案例复用。
**缺口**：通用工程实践，不是金融研究能力。

### 7.7 金融垂直 Agent

- [FinRobot](https://github.com/AI4Finance-Foundation/FinRobot)、FinGPT、FinMem、Alpha-GPT、FinAgent。
- 多为交易/报告/信号，不是研究能力工程；数据/工具可借。

### 7.8 还没解决的核心问题

1. **Insight 质量评估**：没有标准 benchmark；DeepResearch Bench 评报告，不评能力。
2. **合适的视角切换**：只有启发式，没有价值函数。
3. **研究能力毕业**：Voyager 有技能库，但没有金融研究毕业门/回归集。
4. **跨行业泛化**：AI Scientist 多在 ML；金融异质性更强。
5. **数据约束**：深度常卡在数据；公开项目很少处理负结果。

---

## 8. 评估

### 8.1 A/B/C/D

| arm | 配置 | 隔离变量 |
|---|---|---|
| A | 现有 pipeline | 基线 |
| B | pipeline + 同等预算额外研究 | 单纯加算力 |
| C | pipeline + 独立 Scout + 对账 | 双路径组织 |
| D | C + 新毕业能力 | 能力沉淀 |

控制：同数据截止、同模型、同预算、同 brief 渲染、同评分者。

### 8.2 指标

- 视角覆盖率；
- 切换效用（人工评）；
- 洞见产出率（非显然 + 决策相关 + 可检验）；
- 发现卡采用率（能力/方法/playbook/测试）；
- 证据质量、事实错误；
- 成本（token/时间/人工）；
- held-out 案例上的论证质量。

### 8.3 纪律

- 预注册案例/rubric/arm；
- 盲评、随机顺序；
- 开发/验收分离；
- point-in-time，防未来数据；
- n 小只做描述统计，不做显著性宣称。

---

## 9. 落地路线

| 阶段 | 做什么 | 验收 |
|---|---|---|
| **P1（1–2 周）** | 问题空间 + 算子库 + 显式状态；prompt 要求“当前角度/层次” | 每案例列出覆盖维度/层次 |
| **P2（2–3 周）** | Switch Controller：触发器 + 规则 + LLM 提议 + 人工 override；切换日志 | 日志可审计；A/B 有差异 |
| **P3（3–4 周）** | Case/Method/Playbook 记忆；类比检索；跨行业迁移 | 类比带来的新 claim 可量化 |
| **P4（后续）** | 人工反馈 + 策略学习（bandit over operators） | 切换命中率提升；成本可控 |
| **P5** | 盲评 + 下游采用率 | 不是“看起来深”，而是“真的被吸收” |

**MVA**：一个主题、3 个能力、1 个 Scout、6 个案例、trace recorder；先证“发现卡采用率”和“held-out 论证质量”。

---

## 10. 与现有文档/代码的关系

| 资产 | 关系 |
|---|---|
| `docs/design/APIARY_RESEARCH_OS_DESIGN.md` | 总体架构；本文补 Scout 的探索质量/发现分类/切换 |
| `docs/implementation/apiary/POC_SPEC.md` | 实施范围；本文的 P1–P5 对应其 W1–W8 |
| `docs/roadmap/DOMAIN_CONTEXT_ROADMAP.md` | playbook/primitive 的现有骨架；本文的 L2/L3 直接落到这里 |
| `docs/roadmap/AGENT_STRATEGY_ROADMAP.md` | W3（LLM 判断进入决策）、W5（评测/可观测） |
| `docs/design/SINGLE_FRAME_QUALITY_DESIGN.md` | claim/论证质量；发现卡的证据/证伪字段与其对齐 |
| `alphabee/domain_context/` | 3 playbook + 6 primitive + ContextRouter；扩覆盖的第一站 |
| `alphabee/task_records/` | 任务摘要；只做索引，不是 trace |
| `alphabee/orchestrator/services/telemetry.py` | 现有指标；可与 TraceRecorder 对接 |

---

## 11. 开放问题

1. **洞见质量**：用什么指标/盲评协议？是否引入外部专家？
2. **切换价值函数**：如何估计 expected info gain？是否用 bandit/人工反馈？
3. **Trace 完整性**：L2/L3 存多少、存多久、如何 redaction？
4. **能力毕业**：谁批准？标准是否按风险分级？
5. **数据约束**：哪些深度问题值得买数据？哪些明确 park？
6. **模型能力**：小模型能否承担 Scout？是否需要不同模型做 Critic？
7. **人工容量**：每周多少小时做裁决？超过怎么办？
8. **与现有 roadmap**：本文的 P1–P5 如何并入 `POC_SPEC` 的 W1–W8？

---

## 附录 A：发现卡 schema

```text
discovery_id
form
content_layer
statement
evidence_refs[]
recurrence
decision_impact
identification
convertibility
destination
verification_plan
status
```

## 附录 B：算子库

```text
zoom_in / zoom_out
analogize / contrast / invert
decompose / recompose
change_stakeholder / change_horizon
challenge_assumption / find_contradiction
quantify / qualify
historical_analogy / cross_industry
```

## 附录 C：参考

- Voyager: https://arxiv.org/abs/2305.16291
- AI Scientist: https://sakana.ai/ai-scientist/
- AI co-scientist: https://research.google/blog/accelerating-scientific-breakthroughs-with-an-ai-co-scientist/
- STORM: https://arxiv.org/abs/2402.14207
- Reflexion: https://arxiv.org/abs/2303.11366
- CRITIC: https://arxiv.org/abs/2305.11738
- Multi-agent debate: https://arxiv.org/abs/2305.14325
- Tree of Thoughts: https://arxiv.org/abs/2305.10601
- Self-Discover: https://arxiv.org/abs/2402.03620
- MemGPT: https://arxiv.org/abs/2310.08560
- DSPy: https://github.com/stanfordnlp/dspy
- LangGraph: https://github.com/langchain-ai/langgraph
- Langfuse: https://langfuse.com/
- FinRobot: https://github.com/AI4Finance-Foundation/FinRobot
- Anthropic effective agents: https://www.anthropic.com/research/building-effective-agents
- DeepResearch Bench: https://iclr.cc/virtual/2026/poster/10008065
- Dynamic Agent Skills: https://ar5iv.labs.arxiv.org/html/2607.10113
- Ratchet: https://export.arxiv.org/pdf/2605.22148
- Consulting benchmark: https://export.arxiv.org/pdf/2605.17554
