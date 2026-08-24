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

## 实现状态跟踪（2026-08 与当前代码对齐）

> 本节是对下文各 Phase 的**最新落地状态**盘点，用于和当前 `alphabee/orchestrator/` 代码保持同步。
> 状态标记：✅ 已实现　🟡 部分实现　⬜ 未实现

| 条目 | 状态 | 现状说明（代码位置） |
|---|---|---|
| 0.1 anomaly 进入 signal/thesis | ✅ | `nodes/analyze.py` 将 AnomalyEngine 输出投影回 `fact_values`，`anomaly_cluster_risk` / `cross_validation_break` 等异常信号规则可命中 |
| 0.2 ThesisEngine 显式消费 anomaly/conflict/verification/context | ✅ | `nodes/thesis.py` 全量传入；`agents/thesis/engine.py` 的 `run()` 已接收 `anomaly_report / conflict_analysis / verification_results / company_context / insight` |
| 0.3 canonical field / signal rule 一致性 | 🟡 | 无系统性 schema 校验（工程侧 E3 未落地）；`services/gap_recorder.py` 已把 blocked/missing_fact 信号记录进失败库 |
| 0.4 Insight schema 脆弱性 | ✅ | 枚举归一化 + 四级降级阶梯（`agents/insights/rescue.py`：严格解析 → 宽松救援 → 确定性兜底 → 最小骨架），任何失败模式下 insight artifact 必然存在；降级标记随 artifact 落库（degraded / fallback_tier / degradation_reason），报告 prompt 有对应降级分支（见 `tests/orchestrator/test_insight_degradation.py`） |
| 0.5 待验证/已验证冲突分层 | ✅ | `explore_conflicts` 不再把 provisional 冲突升格为 issue；`verify_hypotheses` 作为结算层：verified/partial 高严重度冲突升格为 `verified_conflict` issue、rejected 沉淀为 decision、状态显式回写 `conflicts_result`；`review_thesis` 只保留 thesis_conflict（见 `tests/orchestrator/test_conflict_lifecycle.py`） |
| 0.6 用户输出与调试输出分层 | ⬜ | `main.py` `_render_final_report()` 仍把全部 issues（含 parse_error / rewrite 信息）打印到“🐞 系统问题”段 |
| Phase 1 InsightAgent 稳定观点骨架 | 🟡 | 已接入主图（`nodes/insights.py` + `agents/insights/`），报告 prompt 以 `insight.core_view` 为主线（`prompts.py`）；`what_would_change_my_mind → falsification_conditions` 已贯通；parse fail 已有四级降级（0.4）；`materiality_rank` 未显式驱动报告排序 |
| Phase 1.5 探索/验证/结算分层 | 🟡 | 结算层已随 0.5 落地（provisional 不升格、verified/partial 升格为 issue、rejected 沉淀 decision、状态回写 `conflicts_result`）；剩余：验证预算 / 最短排除路径 / 未探索区域记录、evidence refs 硬约束 |
| Phase 2 BusinessModelContext | 🟡 | `services/company_context.py` 已有 industry / sub_industry / market_cap_category / lifecycle_stage / business_model_summary；无 BusinessModelClassifier、无 playbooks/primitives（见 `docs/roadmap/DOMAIN_CONTEXT_ROADMAP.md`） |
| Phase 3 Claim-Evidence Graph | ⬜ | 未实现；`gates.py` 已有 `evidence_coverage / grounding_score` 检查，但上游 Decision 普遍未填 `based_on / evidence_refs` |
| Phase 4 ExpectationFitAgent | ⬜ | 未实现 |
| Phase 5 报告备忘录化 | 🟡 | 报告已重构为“观点驱动”（`REPORT_GENERATOR_PROMPT`：insight 主线 + 12 章节 + 三情景 + 可证伪条件），LLM 空输出有确定性降级报告（`reporter.py` `build_deterministic_report`）；“系统问题”段仍在 CLI 暴露 |

“当前关键问题”中的 #2（anomaly/conflict 进入 thesis）、#3（Report Generator 被限制为格式化器）、#4（Reviewer 维度覆盖落后）、#7（冲突状态边界）已解决：
- `nodes/thesis.py` 全量传入 anomaly/conflict/verification/context，`engine.py` 已显式消费（0.2）。
- `prompts.py` 的 `REPORT_GENERATOR_PROMPT` 已改为“有观点、有论证、可证伪”的忠实裁决模式，不再要求“只做格式化”。
- `agents/thesis/reviewer.py` 的 `ThesisReviewer` 遍历全部 8 个维度生成 `dimension_verdicts`，审查逻辑已随 `dimensions/` 目录（8 个 YAML）同步扩展。
- `explore_conflicts` / `verify_hypotheses` / `review_thesis` 已按 provisional / settled 分层（0.5）。

---

## 当前关键问题

> 状态标记：✅ 已解决（见上方状态跟踪）　🟡 部分解决　⬜ 未解决
> 以下 9 项为历史梳理，已按当前 `alphabee/orchestrator/` 代码逐项核对（2026-08）。

### 1. ThesisEngine 只是聚合器，不是观点引擎 🟡

当前 `ThesisEngine` 的主体仍是：

```text
signal level × thesis_impact direction → 按维度平均 → judgment
```

这会得到 `financial_quality=negative`、`earnings_quality=neutral` 这类结构化判断，但不会自然形成：

```text
公司当前核心矛盾是：市场仍按高成长定价，但财务数据已经显示增长质量下降；
应收扩张快于收入、现金流没有同步兑现，当前估值需要更强利润兑现能力支撑。
```

已通过 `_apply_insight`（`agents/thesis/engine.py`）把 InsightAgent 的 `central_tension / counter_evidence / confidence` 作为定性语境注入维度并调节置信度；但 `core_view` 尚未显式参与维度判断，`materiality_rank` 也未驱动报告排序——观点主轴目前主要靠报告 prompt（`prompts.py`）承载，而不是 thesis 本身生成。

### 2. anomaly/conflict 没有充分进入 thesis 判断 ✅

已完成（0.2）：`nodes/thesis.py` 全量传入 `anomaly_report / conflict_analysis / verification_results / company_context / insight`；`engine.py` 的 `_apply_conflict_analysis` 会把 verified/partial 冲突计入维度扣分与 evidence、rejected 进入 counter_evidence、unknown 进入 missing_evidence，`_apply_anomaly_report` 把异常模式投影为维度 evidence。

### 3. Report Generator 被限制为格式化器 ✅

已完成：`REPORT_GENERATOR_PROMPT`（`prompts.py`）已改为“有观点、有论证、可证伪”的忠实裁决模式——允许压缩/排序/合并，只禁止引入 payload 之外的新事实；LLM 空输出有确定性降级报告（`reporter.py` `build_deterministic_report`）。

### 4. Reviewer 维度覆盖落后 ✅

已完成：`agents/thesis/reviewer.py` 的 `ThesisReviewer` 遍历全部 8 个维度（`dimensions/` 8 个 YAML）生成 `dimension_verdicts`，审查逻辑已随维度定义同步扩展。

### 5. 缺少重要性排序 🟡

`InsightOutput.materiality_rank` 已产出并随报告 payload 下发（`payload_builders.py`），报告 prompt 也要求 investment_viewpoint / scenario_analysis 引用其中关键变量；但尚无机制强制“按 materiality_rank 决定报告内部排序”，仍依赖 LLM 自觉执行。

### 6. InsightAgent 已接入，但稳定性不足 🟡

`synthesize_insights`（`nodes/insights.py`）已进入主流程，负责输出：

- `core_view`
- `central_tension`
- `main_driver`
- `supporting_evidence / counter_evidence`
- `base_case / bull_case / bear_case`
- `what_would_change_my_mind`

当前仍存在（经代码确认）：

- ✅ 枚举归一化 + 四级降级阶梯已落地（`agents/insights/rescue.py`）：严格解析 → 宽松救援 → 确定性兜底 → 最小骨架，任何失败模式下 insight artifact 必然存在（见 `tests/orchestrator/test_insight_degradation.py`）
- 🟡 `materiality_rank` 仍未显式驱动报告排序（依赖 LLM 自觉引用）

这意味着“观点层”已经稳定存在（不再整层丢失），但降级产出的观点质量仍低于 LLM 综合，且重要性排序尚未成为硬约束。

### 7. 冲突探索与验证的状态边界不够清晰 ✅

已完成（0.5）：`explore_conflicts` 只产出 provisional 冲突，不再直接升格 issue；`verify_hypotheses` 作为结算层（verified/partial 高严重度 → `verified_conflict` issue、rejected → decision、状态显式回写 `conflicts_result`）；`review_thesis` 只对“正向维度 vs 已验证冲突”制造 `thesis_conflict`；quality gate 只统计已结算类别。见 `tests/orchestrator/test_conflict_lifecycle.py`。

显式区分已经落地：

- provisional conflict（候选冲突 / 待验证）→ 不进入 issue
- verified conflict（已验证冲突 / 可进入最终判断）→ 进入 issue / thesis / gate

### 8. 证据链没有稳定闭环 🟡

quality gate 已检查 `evidence_coverage / grounding_score / disclosed_issue_ids`（`gates.py`），但上游 Decision 仍普遍未填 `based_on / evidence_refs`——目前沉淀的 decision 主要来自 thesis 维度 verdict（`thesis_reviewer`）与 conflict rejected（`conflict_verifier`），且大多没有证据引用。

后果是：

- 报告虽然写了很多结论，但“源可追”能力弱
- gate 会持续提示 evidence_coverage 低
- report rewrite 会越来越像“修措辞”，而不是“补证据”

### 9. 用户报告与系统调试信息混在一起 ⬜

`main.py` `_render_final_report()` 仍把全部 issues（含 parse_error / report_rewrite_needed / subagent_failure 等调试信息）打印到“🐞 系统问题”段（对应 0.6，未落地）。这虽然对开发排障有帮助，但会显著破坏用户看到的成品感，也会让报告从“研究结论”退化成“运行日志”。

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

状态：✅ 已实现。

接口已落地（`agents/thesis/engine.py` 的 `run()` 实际接收，`nodes/thesis.py` 全量传入）：

```python
ThesisEngine.run(
    symbol,
    period,
    signal_results,
    anomaly_report=None,
    conflict_analysis=None,
    verification_results=None,
    company_context=None,
    insight=None,
)
```

落地效果：

- 已验证 high/critical conflict 下调相关维度（`_apply_conflict_analysis`）
- anomaly pattern 直接生成 thesis evidence（`_apply_anomaly_report`）
- rejected hypotheses 作为反向证据进入 thesis（counter_evidence）
- unknown hypotheses 进入 missing evidence
- insight 的 central_tension / counter_evidence / confidence 注入维度（`_apply_insight`）

### 0.3 修复 canonical field / signal rule 不一致

重点检查：

- signal rules 是否只依赖 canonical fields
- `operating_cash_flow` vs `operating_cashflow` 这类字段不一致
- anomaly facts 是否有统一 schema 记录
- blocked/missing_fact 是否被误判为 none

### 0.4 修复 Insight schema 脆弱性

状态：✅ 已实现（2026-08），落地方式见 `docs/design/INSIGHT_DEGRADATION_DESIGN.md`。

目标：不要让观点骨架因为轻微枚举值漂移而整层失效。

已实现：

- `_coerce_confidence / _coerce_importance`（`agents/insights/models.py`）：`moderate -> medium` 等常见枚举值归一化
- 对 `importance/confidence/weight` 等字段增加容错映射
- **四级降级阶梯**（`agents/insights/rescue.py`）：严格解析（Tier 0）→ 宽松救援 `lenient_parse`（Tier 1，只修结构不补内容）→ 确定性兜底 `build_fallback_insight`（Tier 2，从 `build_insight_context` 的 dict 转述合成，只转述不虚构）→ 最小骨架 `build_minimal_insight`（Tier 3）
- 任何失败模式下 `INSIGHT_ANALYSIS` artifact 必然存在；降级标记（`degraded / fallback_tier / degradation_reason`）随 artifact 落库，报告 prompt 有对应降级分支（`prompts.py`）
- 测试：`tests/orchestrator/test_insight_degradation.py`（14 用例，含诚实性硬规则 H1 断言）

### 0.5 分离“待验证冲突”与“已验证冲突”

**状态：✅ 已实现（2026-08）**，落地方式：

- `explore_conflicts` 只产出 provisional conflicts，不再把 high/critical 冲突直接升格为 issue（`nodes/conflicts.py`）
- `verify_hypotheses` 作为结算层：verified/partial 高严重度冲突升格为 `verified_conflict` issue；rejected 假设沉淀为 decision；unknown 保持 provisional（`nodes/verification.py`）
- 结算状态显式回写进 `conflicts_result` artifact（`verified / partial / rejected / unknown`），下游只读该 artifact 即可拿到真实状态
- `review_thesis` 只对“正向维度 vs 已验证冲突”制造 `thesis_conflict`，不再重复制造 verified_conflict / rejected decision（`orchestrator/agent.py`）
- 测试：`tests/orchestrator/test_conflict_lifecycle.py`

目标：探索可以更自由，但最终判断只消费已结算结果。

建议（历史记录）：

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

当前进度（2026-08 与代码对齐）：

- ✅ 已接入主图（`nodes/insights.py`），报告 prompt 以 `core_view / central_tension` 为主线（`prompts.py`）
- ✅ `what_would_change_my_mind → falsification_conditions` 已贯通，insight 的 central_tension / counter_evidence / confidence 进入 thesis（`_apply_insight`）
- ✅ parse fail 已有四级降级（0.4，`agents/insights/rescue.py`），观点层不再整层丢失
- 🟡 `materiality_rank` 未显式驱动报告排序（已产出并下发，排序仍依赖 LLM 自觉）

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

状态：🟡 核心结算层已随 0.5 落地（provisional 不升格 issue、verified/partial 升格、rejected 沉淀 decision、状态回写 `conflicts_result`）；剩余增强项见下文（验证预算 / 最短排除路径 / 未探索区域记录 / evidence refs 硬约束）。

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
docs/roadmap/DOMAIN_CONTEXT_ROADMAP.md
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

| 优先级 | 事项 | 价值 | 当前状态 |
|---|---|---|---|
| P0 | Insight 降级：parse fail 保留观点骨架（0.4 收尾） | 保住观点主轴，不因 LLM JSON 漂移退回模板模式 | ✅ 已实现（四级降级，见 `agents/insights/rescue.py` + `test_insight_degradation.py`） |
| P0 | Decision 补齐 evidence refs | 解决 evidence_coverage 低和结论不可追溯 | 🟡 部分（gate 已检查，多数 Decision 仍未填） |
| P0 | anomaly 进入 signal/thesis | 修正当前链路断点 | ✅ 已实现 |
| P0 | ThesisEngine 显式消费 conflict/anomaly | 让高价值发现影响结论 | ✅ 已实现 |
| P0 | provisional / verified conflict 分层 | 提高探索自由度，同时避免怀疑冒充事实 | ✅ 已实现 |
| P1 | 用户输出与调试输出分层（0.6） | 提升成品感，减少“运行日志感” | ⬜ 未实现 |
| P1 | materiality_rank 驱动报告排序 | 让重要性真正影响表达顺序 | 🟡 部分（已产出并下发，未强制排序） |
| P1 | 稳定 InsightAgent 成为观点主轴 | 从数字堆砌变成观点生成 | 🟡 已接入主图，report 已以其为主线 |
| P1 | 报告结构改成核心观点优先 | 立刻改善用户感知 | ✅ 已实现（观点驱动报告重构） |
| P2 | BusinessModelContext | 提升行业/公司语境判断 | 🟡 基础字段已有，Classifier/Playbook 未做 |
| P2 | Claim-Evidence Graph | 让观点可追踪、可审查 | ⬜ 未实现 |
| P3 | ExpectationFitAgent | 打通财务质量与投资价值 | ⬜ 未实现 |
| P3 | 同行基准 / 行业分位 | 降低固定阈值误判 | 🟡 Phase 0 已落地（`resolve_industry_context` + 相对基准阈值 + `market_share_change` 复活，见 `docs/industry/industry-context-injection-plan.md`）；完整研究工作流/报告层未做 |

---

## 下一步建议

短期最值得做的 2 件事（更新于 2026-08 状态跟踪，按收益排序）：

1. 给关键 Decision 补齐 evidence refs（Phase 3 前置）：thesis 维度 verdict、conflict rejected、insight 判断都填上 `based_on / evidence_refs`，让 gate 的 evidence_coverage / grounding_score 从“文案检查”变成“来源审计”。
   **状态**：🟡 部分（gate 已检查，多数 Decision 未填）。
2. 落地用户输出与调试输出分层（0.6）：`main.py` `_render_final_report()` 只打印用户侧不确定性披露（verified_conflict / thesis_conflict / thesis_gap 等），parse_error / report_rewrite_needed / subagent_failure 转入 debug 视图或附录。
   **状态**：⬜ 未做（CLI 仍打印“🐞 系统问题”段）。

已完成（2026-08）：

- ✅ Insight 降级（0.4 收尾）：四级降级阶梯落地于 `agents/insights/rescue.py`，任何失败模式下 insight artifact 必然存在（见 `tests/orchestrator/test_insight_degradation.py`）。
