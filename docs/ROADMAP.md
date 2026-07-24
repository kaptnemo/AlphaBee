# AlphaBee Analysis Agent Roadmap

## 背景判断

AlphaBee 当前已经具备较完整的“事实采集 → 衍生指标 → 风险信号 → 异常检测 → 冲突探索 → 论点汇总 → 报告生成”流水线，但核心短板是：系统能发现大量指标和风险，却还没有形成真正像公司财务分析师一样的主观点。

当前输出容易表现为：

- 指标很多，但主次不清
- 风险信号很多，但没有中心矛盾
- 结论是维度打分，而不是可争辩的投资论点
- 报告像数据堆砌，而不是观点驱动的研究备忘录

后续建设目标应从“财务指标检测系统”升级为“有洞见的公司财务分析 Agent”。

---

## 当前关键问题

### 1. ThesisEngine 只是聚合器，不是观点引擎

当前 `ThesisEngine` 主要做：

```text
signal level × thesis_impact direction → 按维度平均 → judgment
```

这会得到 `financial_quality=negative`、`earnings_quality=neutral` 这类结构化判断，但不会自然形成：

```text
公司当前核心矛盾是：市场仍按高成长定价，但财务数据已经显示增长质量下降；
应收扩张快于收入、现金流没有同步兑现，当前估值需要更强利润兑现能力支撑。
```

### 2. anomaly/conflict 没有充分进入 thesis 判断

已完成第一步：`AnomalyEngine` 现在会在 `SignalEngine` 之前运行，并把 anomaly facts 注入 signal 计算，使 anomaly signals 可以进入 thesis。

下一步仍需让 `ThesisEngine` 显式消费：

- `anomaly_report`
- `conflict_analysis`
- `verification_results`
- `company_context`

这样勾稽异常和已验证冲突才会真正改变核心论点，而不是只出现在报告附录。

### 3. Report Generator 被限制为格式化器

当前报告 prompt 明确要求“不是分析师，只做格式化和文字润色”。这能避免幻觉，但也意味着如果上游没有强观点，最终报告一定会变成结构化信息堆砌。

更具体地说，当前链路仍然是：

```text
facts / signals / anomaly / conflict / review / issues
→ 一次性塞给 report generator
→ 报告被动汇总
```

而不是：

```text
事实 → 冲突假设 → 验证裁决 → 中心观点 → 最终报告
```

因此最终产物更像“信息汇总器”，而不是“观点驱动的研究 memo”。

### 4. Reviewer 维度覆盖落后

Thesis dimensions 已扩展到：

```text
financial_quality
earnings_quality
growth_quality
credit_risk
valuation_fit
competitive_moat
capital_efficiency
operational_stability
```

但 reviewer prompt 仍明显偏向旧的 `financial_quality / earnings_quality / credit_risk` 三维度，需要同步升级审查逻辑。

### 5. 缺少重要性排序

当前系统更擅长覆盖，而不是取舍。真正的分析师需要回答：

- 最关键变量是什么？
- 哪个事实最能改变判断？
- 哪些异常是主矛盾？
- 哪些指标只是背景噪音？

### 6. InsightAgent 已接入，但稳定性不足

`synthesize_insights` 已经进入主流程，且理论上负责输出：

- `core_view`
- `central_tension`
- `main_driver`
- `supporting_evidence / counter_evidence`
- `base_case / bull_case / bear_case`
- `what_would_change_my_mind`

但当前仍存在：

- schema 过严导致整层容易因枚举值小偏差直接 parse fail
- insight 失败后，下游会退回“维度判断 + 模板解释”模式
- report 虽然支持消费 insight，但观点骨架经常缺失

这意味着“观点层”已经有形，但还没有稳定成为最终报告的主轴。

### 7. 冲突探索与验证的状态边界不够清晰

当前高严重度 conflict 会很早被上升为高优先级 issue。这样容易把：

```text
待验证怀疑
```

误传导成：

```text
已经成立的问题
```

后果是：

- thesis 过早被高压负面信号牵引
- report 把“冲突线索”写成“事实结论”
- quality gate 把探索阶段的不确定性当成最终不一致

需要显式区分：

- provisional conflict（候选冲突 / 待验证）
- verified conflict（已验证冲突 / 可进入最终判断）

### 8. 证据链没有稳定闭环

quality gate 已经开始检查 `evidence_coverage / grounding_score / disclosed_issue_ids`，但上游很多 Decision 仍没有系统性填充 `based_on` / `evidence_refs`。

后果是：

- 报告虽然写了很多结论，但“源可追”能力弱
- gate 会持续提示 evidence_coverage 低
- report rewrite 会越来越像“修措辞”，而不是“补证据”

### 9. 用户报告与系统调试信息混在一起

当前最终输出会把 parse error、rewrite_needed、内部冲突、调试问题直接暴露在“系统问题”段落中。
这虽然对开发排障有帮助，但会显著破坏用户看到的成品感，也会让报告从“研究结论”退化成“运行日志”。

---

## Roadmap

## Phase 0：修正当前链路的结构性问题

### 0.1 anomaly 进入 signal/thesis

状态：已完成第一版。

已实现：

```text
DerivedFacts
→ AnomalyEngine
→ 注入 anomaly fact_values
→ SignalEngine
→ ThesisEngine
```

已明确 anomaly signal dependencies：

- `anomaly_cluster_risk`
- `cross_validation_break`

后续可继续增强 anomaly signals，让二阶异常模式直接映射到 thesis 维度。

### 0.2 ThesisEngine 显式消费 anomaly/conflict

建议扩展接口：

```python
ThesisEngine.run(
    symbol,
    period,
    signal_results,
    anomaly_report=None,
    conflict_analysis=None,
    verification_results=None,
    company_context=None,
)
```

目标：

- 已验证 high/critical conflict 可以下调相关维度
- anomaly pattern 可以直接生成 thesis evidence
- rejected hypotheses 可以作为反向证据进入 thesis
- unknown hypotheses 可以进入 missing evidence

### 0.3 修复 canonical field / signal rule 不一致

重点检查：

- signal rules 是否只依赖 canonical fields
- `operating_cash_flow` vs `operating_cashflow` 这类字段不一致
- anomaly facts 是否有统一 schema 记录
- blocked/missing_fact 是否被误判为 none

### 0.4 修复 Insight schema 脆弱性

目标：不要让观点骨架因为轻微枚举值漂移而整层失效。

建议：

- 对 `moderate -> medium` 这类常见枚举值做 normalize
- 对 `importance/confidence/weight` 等字段增加容错映射
- parse fail 时保留降级 insight，而不是整层丢弃
- 将 parse error 视为“观点层失败”，而不是普通可忽略 warning

### 0.5 分离“待验证冲突”与“已验证冲突”

目标：探索可以更自由，但最终判断只消费已结算结果。

建议：

- `explore_conflicts` 只产出 provisional conflicts，不直接升格为 high issue
- `verify_hypotheses` 之后再决定哪些冲突进入 thesis/review/gate
- 对 `verified / partial / rejected / unknown` 做显式状态传播
- `rejected` 假设进入 counter evidence，避免所有疑点都悬而未决

### 0.6 修复用户输出与调试输出串层

目标：默认交付“分析结果”，而不是“系统运行诊断”。

建议：

- 默认报告中只保留用户有意义的不确定性披露
- parse_error / report_rewrite_needed / 内部调试信息转入 debug 视图或附录
- 区分“分析结论中的风险”和“系统实现层的问题”

---

## Phase 1：把 InsightAgent 从“已接入”升级为“稳定观点骨架”

这是从“数字堆砌”变成“观点驱动”的关键阶段。
当前不是“要不要有 InsightAgent”的问题，而是“如何让它稳定地主导下游表达”。

现状：

```text
alphabee/agents/insights/
  models.py
  prompts.py
  agent.py
```

在 orchestrator 中插入：

```text
verify_hypotheses
→ synthesize_insights
→ run_thesis / review_thesis
→ generate_report
```

### 目标输出结构

```json
{
  "core_view": "一句话核心观点",
  "central_tension": "最关键矛盾",
  "main_driver": "决定结论的核心变量",
  "supporting_evidence": [],
  "counter_evidence": [],
  "materiality_rank": [],
  "business_model_context": "",
  "base_case": "",
  "bull_case": "",
  "bear_case": "",
  "what_would_change_my_mind": []
}
```

### InsightAgent 核心职责

- 从 signals / anomaly / conflicts / verification 中提炼中心矛盾
- 识别最重要的 1-3 个判断变量
- 区分主证据、反证和缺失证据
- 输出可证伪的观点，而不是指标摘要

### 本阶段新增要求

- 下游 `run_thesis / generate_report` 必须优先消费 `core_view / central_tension`
- 如果 insight 缺失，报告应明确降级为“结构化摘要模式”
- `what_would_change_my_mind` 必须进入最终报告，作为观点可证伪条件
- `materiality_rank` 要真正影响报告排序，而不只是存档

---

## Phase 1.5：建立“探索自由，结论收敛”的中间层契约

目标不是简单增加 agent 自由度，而是：

```text
探索可以发散，结论必须收敛
允许提出怀疑，不允许把怀疑伪装成事实
允许多轮验证，不允许无来源结论进入 final report
```

建议将中间层明确拆成三种职责：

### 1. Explore layer

- 允许提出多个候选冲突 / 假设
- 允许使用较开放的模式识别和跨维度联想
- 输出必须保持 provisional，不得直接改写最终判断

#### Explore layer 的具体增强方向

##### 1. 探索目标从“找风险”升级为“解释矛盾”

探索节点的核心任务不应只是继续罗列风险，而应围绕一个核心矛盾生成解释空间，例如：

- 真恶化：基本面正在变差
- 周期/季节性波动：短期数据偏离但不代表趋势反转
- 商业模式导致的正常错位：项目制、账期、扩产节奏带来的表观异常
- 会计口径或一次性因素：政策变更、并表、税务、补贴等扰动
- 市场预期先行：估值先反映未来，而财务兑现暂时滞后

目标是让 ExploreAgent 回答：

```text
为什么这些事实会互相打架？
```

而不只是：

```text
这里还有哪些风险？
```

##### 2. 强制“多假设并存”，避免单路径早收敛

每个高价值冲突至少保留三类解释：

- 主假设（当前最可能）
- 替代假设（第二解释）
- 反向假设（解释为什么它可能并不是问题）

这样可以避免系统看到一个 high signal 就一路向负面叙事滑坡。

##### 3. 引入“验证预算”机制，而不是无限自由

探索自由度应该通过预算控制，而不是完全放开 prompt。建议对每个 conflict 设置：

- 最多验证 2-3 个最高价值假设
- 每个假设最多调用 N 次工具
- 优先选择“最快能排除”的证据
- 严重度 × 可验证性 × 对最终判断影响度 共同决定预算分配

这样探索会更像 research triage，而不是无边界扩散。

##### 4. 引入“最短排除路径”策略

对每个候选假设，不只输出“还可以查什么”，还要输出：

```text
只要再确认哪 1-2 个事实，就能基本排除这个解释？
```

这会显著提升验证效率，也能减少 agent 为了显得勤奋而堆工具调用。

##### 5. 区分“异常”与“可解释异常”

探索层应显式回答：

- 这是经营异常？
- 这是会计口径变化？
- 这是扩产/项目制/行业周期下的正常偏离？

也就是说，不把 z-score 高自动等同于问题，而是把“发现偏离”推进到“解释偏离”。

##### 6. 行业/商业模式特化探索模板

探索不能只依赖通用 prompt。建议按 business model 切探索模板：

- 制造业：库存、产能、capex、毛利率传导
- To B / 项目制：应收、验收节奏、合同负债、回款滞后
- 周期行业：价格、库存、盈利弹性、资本开支周期
- 金融类：杠杆、资产质量、久期错配、流动性

这样 agent 才会像 analyst，而不是 generic summarizer。

##### 7. 记录“未探索区域”

探索质量不只取决于查了什么，也取决于是否知道自己没查什么。建议输出：

- 已验证方向
- 已排除方向
- 未验证但重要的方向
- 为什么没继续查（缺数据 / 工具不适合 / 性价比低）

这既有助于控制幻觉，也有助于后续人机协同接力。

### 2. Verification layer

- 可以自主决定用 Tushare / Eastmoney / web_search 查询什么
- 但每个裁决必须回填：
  - `supporting_evidence`
  - `refuting_evidence`
  - `gaps`
  - `confidence`
- unknown 不是失败，而是明确的“证据未闭环”

#### Verification layer 的执行原则

- 数值优先于叙述
- 优先查能最快区分多个竞争假设的证据
- 不追求“查得更多”，而追求“把解释空间缩小得更明确”
- 每次验证应服务于 hypothesis ranking，而不是重复采样已有结论

### 3. Settlement layer

- 只有经过验证结算的冲突和假设，才能进入 thesis / report
- 所有结论必须映射到 evidence refs
- report 不允许新增任何中间层没出现过的新判断

#### Settlement layer 的核心要求

- provisional hypothesis 不得直接进入 final judgment
- verified / partial / rejected / unknown 必须显式传播到 thesis 与 report
- report 只消费“已结算结果”，不直接消费探索阶段的自由文本
- 若仍存在多个未分胜负的解释，报告必须把它表述为“竞争性解释”，而不是伪装成单一确定结论

---

## Phase 2：建立 Business Model Context 层

当前 company context 只有行业、生命周期、市值分类，无法支撑高质量财务解释。

关于这一层如何进一步扩展为 **公司特定驱动画像 + ContextRouter + Domain Playbooks + EventOverlay**，已单独整理为：

```text
docs/DOMAIN_CONTEXT_ROADMAP.md
```

该子 roadmap 的核心主张是：

- 不把 domain context 做成静态行业词典
- 用 `domain_primitives/ + domain_playbooks/ + runtime_context/` 三层架构
- 让上下文在运行时根据标的、问题、地域暴露和事件环境动态激活
- 让最终分析主线更像“牧原看猪周期、金诚信看矿业 CAPEX + 天气扰动”

建议新增：

```text
BusinessModelClassifier
```

输出：

```json
{
  "revenue_model": "to_b_credit_sales | to_c_cash_sales | project_based | subscription | commodity_cycle",
  "asset_intensity": "light | medium | heavy",
  "working_capital_pattern": "receivable_heavy | inventory_heavy | advance_payment | cash_conversion_fast",
  "cycle_sensitivity": "low | medium | high",
  "key_financial_pressure_points": [
    "accounts_receivable",
    "inventory",
    "capex",
    "gross_margin"
  ]
}
```

同样的财务信号在不同行业和商业模式下含义不同：

- 白酒的应收增长可能高度异常
- 军工的应收增长可能来自结算周期
- 软件公司的应收可能来自项目验收节奏
- 医药流通企业天然账期较重
- 光伏制造要结合库存、价格周期和资本开支

---

## Phase 3：从风险信号升级到论证图谱

建议引入 claim-evidence graph：

```json
{
  "claims": [
    {
      "claim": "公司增长质量下降",
      "stance": "bearish",
      "confidence": 0.72,
      "evidence_for": [],
      "evidence_against": [],
      "missing_evidence": [],
      "depends_on": []
    }
  ]
}
```

目标是让报告从：

```text
列指标、列风险、列维度
```

升级为：

```text
提出观点 → 给出证据 → 给出反证 → 指出还缺什么 → 说明什么会改变判断
```

本阶段的真正落点不是“多一个图结构”，而是让以下约束变成硬契约：

- 每个核心 claim 必须有 `evidence_for`
- 每个强判断必须允许 `evidence_against`
- 缺失证据必须显式挂在 `missing_evidence`
- 只有进入 claim graph 的结论，才允许进入最终报告

### Decision / EvidenceRef 改造目标

建议所有进入 review / gate / report 的关键 Decision 都补齐：

```text
based_on / evidence_refs
```

使 quality gate 不再只是检查“文案写得像不像”，而是检查“结论是否真的有来源、能回放、能审计”。

示例：

```text
Claim: 增长质量下降

Evidence for:
- 应收增速高于收入增速
- 经营现金流/净利润下降
- 利润增速未显著超过收入增速

Evidence against:
- 毛利率仍稳定
- 行业账期可能普遍拉长

Missing:
- 应收账龄
- 前五大客户变化
- 同行应收周转天数
```

---

## Phase 4：接入市场预期 / 估值隐含假设

当前估值更多是静态指标：PE、PB、PEG、历史估值位置。

真正有洞见的分析需要回答：

```text
市场价格隐含了什么预期？
财务质量能不能支撑这个预期？
如果不能，风险在哪里？
```

建议新增：

```text
ExpectationFitAgent
```

输入：

- `pe_ttm`
- `pb`
- `roe`
- `net_profit_yoy`
- `revenue_yoy`
- 行业估值
- 历史估值
- 可选分析师预期

输出：

```json
{
  "implied_expectation": "市场当前定价隐含未来仍需维持高利润增长",
  "fundamental_support": "weak | medium | strong",
  "expectation_gap": "估值要求的增长质量高于当前财务数据能证明的水平",
  "de_rating_risk": "high"
}
```

---

## Phase 5：报告升级为投资研究备忘录

当前报告偏“体检报告”。建议升级为“观点优先”的研究备忘录。

目标结构：

```text
1. 核心观点
2. 最关键矛盾
3. 支撑证据
4. 反向证据
5. 商业模式语境
6. 估值 / 预期匹配
7. 情景分析：Bull / Base / Bear
8. 需要继续验证的 3 个问题
9. 结论置信度
```

原则：

```text
数字服务观点，而不是观点附着在数字后面。
```

补充原则：

```text
报告负责裁决，不负责转储全部中间结果。
```

建议最终用户报告固定围绕 4 个问题组织：

1. 一句话观点：当前最值得相信/最该怀疑的是什么？
2. 核心矛盾：哪两个事实或预期在打架？
3. 裁决依据：支持观点的 2-3 条关键证据和最强反证是什么？
4. 证伪条件：未来看到什么数据，这个判断需要改变？

### Report Generator 升级方向

将当前“忠实转写”升级为“忠实裁决”：

- 允许压缩、排序、合并相近信息
- 允许统一语气和改善可读性
- 不允许引入 payload 中不存在的新事实或新数字
- 每个维度最多保留“2 条支持 + 1 条反证 + 1 个裁决”
- 高优先级 issue 以“分析不确定性披露”形式进入正文
- 内部调试问题默认不进入用户主报告

---

## 推荐优先级

| 优先级 | 事项 | 价值 |
|---|---|---|
| P0 | anomaly 进入 signal/thesis | 修正当前链路断点 |
| P0 | ThesisEngine 显式消费 conflict/anomaly | 让高价值发现影响结论 |
| P0 | 修复 Insight schema 脆弱性 | 保住观点骨架，不因 parse fail 退回模板模式 |
| P0 | provisional / verified conflict 分层 | 提高探索自由度，同时避免怀疑冒充事实 |
| P0 | Decision 补齐 evidence refs | 解决 evidence_coverage 低和结论不可追溯 |
| P1 | 稳定 InsightAgent 成为观点主轴 | 从数字堆砌变成观点生成 |
| P1 | 报告结构改成核心观点优先 | 立刻改善用户感知 |
| P1 | 用户输出与调试输出分层 | 提升成品感，减少“运行日志感” |
| P2 | BusinessModelContext | 提升行业/公司语境判断 |
| P2 | Claim-Evidence Graph | 让观点可追踪、可审查 |
| P3 | ExpectationFitAgent | 打通财务质量与投资价值 |
| P3 | 同行基准 / 行业分位 | 降低固定阈值误判 |

---

## 下一步建议

短期最值得做的 3 件事：

1. 修复 `InsightOutput` 的 schema 脆弱性，并让 insight 失败时有可控降级，而不是直接失去观点骨架。
2. 调整 conflict 生命周期：`explore_conflicts` 产出 provisional，`verify_hypotheses` 之后再升级为最终可消费冲突。
3. 重构 `REPORT_GENERATOR_PROMPT` 和最终渲染逻辑，让报告按“观点 → 证据 → 反证 → 证伪条件”组织，并把内部调试信息移出主报告。
