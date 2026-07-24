# AlphaBee Domain Context Roadmap

## 目标

让 AlphaBee 的分析方向更符合具体标的的真实驱动，而不是所有公司都套用同一套通用财务模板。

目标效果：

- 牧原股份优先围绕猪周期、成本曲线、出栏节奏、产能去化展开
- 金诚信优先围绕矿业资本开支周期、海外项目执行、天气冲击、资源价格展开
- 同一个外部事件（如厄尔尼诺）可以被动态映射到不同公司的不同传导链条

核心原则：

```text
不要把 domain context 做成静态行业知识库，
而要做成“可组合的分析框架 + 动态事件覆盖层”。
```

---

## 为什么不能只做一堆固定 YAML

类似下面这种设计：

```text
alphabee/domain_context/
  hog_cycle.yaml
  copper_gold_mining.yaml
  weather_shock.yaml
  project_mining_services.yaml
```

可以作为起点，但不能作为最终架构。原因是：

1. **概念是动态的**
   猪周期、资源 CAPEX 周期、天气冲击、政策扰动，本身都会随着年份、行业阶段和市场结构变化。

2. **同一个 context 会跨行业复用**
   `weather_shock` 不只影响农业，也影响矿业、电力、运输。

3. **同一个公司会同时命中多个 context**
   金诚信可能同时需要：
   - `project_delivery`
   - `overseas_execution`
   - `commodity_capex_cycle`
   - `weather_shock`

4. **真正动态的不是“知识骨架”，而是“本次分析实例”**

5. **公司身份本身可能正在漂移（Identity Drift）**
   京东方A以前是纯面板周期股，但现在增加了AI硬件概念并主动强调"去周期化"；
   潍柴动力从重卡发动机向多元化能源动力转型；中国中免从政策牌照红利走向免税零售运营。
   这类公司的共同特征是：旧的驱动变量解释力在下降、新的驱动变量尚未完全确立，
   而且**管理层叙事、市场定价、财务现实三者之间存在差距**——静态 YAML 无法刻画这种过渡态。

因此更合理的实现方式是分层。

---

## 三层架构

### Layer 1：稳定的分析原语（Domain Primitives）

这层沉淀变化慢、可跨行业复用的分析积木：

```text
domain_primitives/
  commodity_cycle.yaml
  capacity_cycle.yaml
  project_delivery.yaml
  overseas_execution.yaml
  weather_shock.yaml
  policy_shock.yaml
  cost_pass_through.yaml
  working_capital_stress.yaml
  narrative_transition.yaml    # 元原语：描述框架切换过程本身
  competing_frameworks.yaml    # 元原语：竞争性假设的组织与验证
```

每个 primitive 只回答一类稳定问题：

- 什么时候激活
- 关键变量有哪些
- 典型因果链条是什么
- 常见误判是什么
- 优先验证哪些证据

示例：

```yaml
id: weather_shock
version: 1
when_to_activate:
  - overseas_project_exposure
  - agri_supply_chain_exposure
key_variables:
  - region_exposure
  - rainfall_anomaly
  - transport_disruption
  - power_water_constraint
causal_paths:
  - extreme_weather -> logistics disruption -> delivery delay
  - drought -> power/water shortage -> production inefficiency
preferred_sources:
  - company_announcements
  - project_region_mapping
  - industry_news
  - weather_event_feed
```

```yaml
id: narrative_transition
version: 1
description: >
  当公司的业务模式、定价框架或核心驱动变量正在发生结构性迁移时激活。
  这是一个描述"框架切换过程"的元原语，而非替代原有框架的"新框架"。
when_to_activate:
  - management_actively_claiming_business_model_change
  - emerging_revenue_stream_exceeding_20pct
  - market_consensus_drivers_in_conflict
  - external_environment_making_historical_pattern_invalid
key_variables:
  - old_driver_strength
  - new_driver_strength
  - narrative_evidence_gap
  - market_perception_lag
  - transition_velocity
causal_paths:
  - emerging_business_growth -> revenue_mix_shift -> old_cycle_sensitivity_decline
  - management_narrative -> market_repricing_attempt -> valuation_gap_widening_or_closing
  - external_disruption -> historical_pattern_breakdown -> framework_void
priority_questions:
  - 旧框架的核心假设哪些已经失效、哪些仍然成立？
  - 新叙事是否已经反映在收入结构和资本开支方向上？
  - 管理层主张的"去周期化"是否有可验证的结构性证据？
  - 市场目前主要在定价哪一套框架？
  - 过渡完成需要什么条件？这些条件正在出现还是恶化？
disconfirming_signals:
  - 收入结构中新业务占比停滞或倒退
  - 旧周期变量仍然主导利润波动
  - 管理层叙事在多次季报中未兑现为结构性数据变化
  - 资本开支仍主要投向旧业务而非新方向
preferred_sources:
  - segment_revenue_breakdown
  - capex_allocation_by_segment
  - management_discussion_analysis
  - industry_chain_verification
  - sell_side_consensus_driver_analysis
report_angles:
  - 市场是否高估了去周期化的速度？
  - 新业务的真实驱动因素是什么——自身α还是仍然依附于旧周期景气？
  - 过渡期的价值锚在哪里——当前应如何定价一个"半旧半新"的公司？
```

### Layer 2：专题组合框架（Domain Playbooks）

这层不是再定义“新知识”，而是把 primitives 组合成公司/行业常用分析框架：

```text
domain_playbooks/
  hog_cycle.yaml
  mining_services.yaml
  copper_gold_mining.yaml
  project_engineering.yaml
```

例如：

```text
hog_cycle
= commodity_cycle
+ biological_inventory
+ feed_cost
+ epidemic_risk
+ capacity_cycle

mining_services
= project_delivery
+ overseas_execution
+ commodity_capex_cycle

copper_gold_mining
= commodity_cycle
+ reserve_grade
+ geopolitical_risk
+ weather_shock
```

Playbook 负责定义：

- 适用标的特征
- 主驱动变量
- 次驱动变量
- 最重要的冲突模板
- 推荐验证顺序
- 报告应该围绕哪些问题写

### Layer 3：运行时上下文（Runtime Context）

真正动态的是这层。

```text
runtime_context/
  context_router.py
  context_ranker.py
  event_overlay.py
  company_driver_profile.py
```

它要做的不是“存知识”，而是根据当前标的、问题和外部环境生成：

```json
{
  "activated_contexts": ["weather_shock", "project_delivery"],
  "primary_driver": "overseas_execution",
  "secondary_drivers": ["commodity_capex_cycle", "weather_shock"],
  "company_specific_path": [
    "El Nino",
    "Peru/Chile rainfall anomaly",
    "mine project disruption",
    "delivery and margin pressure"
  ]
}
```

---

## 核心对象设计

### 1. DriverProfile / CompanySpecificContext

比当前 `company_context` 更细，输出公司真正受什么变量驱动。

建议结构：

```json
{
  "business_model": "hog_farming | mining_services | copper_gold_mining | project_engineering",
  "cycle_type": ["hog_cycle", "commodity_capex_cycle"],
  "key_driver_variables": [
    "hog_price",
    "feed_cost",
    "full_cost_per_head"
  ],
  "external_shocks_to_watch": [
    "weather_shock",
    "policy_shock"
  ],
  "industry_specific_questions": [
    "盈利修复来自价格还是成本？",
    "当前是否处于周期反转前段？"
  ],
  "priority_evidence_sources": [
    "tushare",
    "company_announcements",
    "industry_news"
  ]
}
```

### 2. ContextRouter

负责把公司映射到可激活的 contexts。

输入来源：

- 行业 / 子行业
- 公司业务描述
- 地域暴露
- 用户问题
- 当前事件环境

输出：

```json
{
  "activated_contexts": ["hog_cycle", "cost_curve_competition"],
  "primary_driver": "hog_cycle",
  "secondary_drivers": ["feed_cost", "capacity_cycle"],
  "why_selected": [
    "sub_industry_match",
    "business_description_match",
    "user_query_intent"
  ]
}
```

### 3. EventOverlay

负责把动态事件叠加到静态框架上。

例如：

- `El Nino active`
- `commodity price spike`
- `policy tightening`
- `regional epidemic`

核心思想：

```text
静态框架：天气冲击通常如何传导
+
动态事件：这一次究竟发生在哪里、强度多大、影响谁
=
本次分析上下文
```

### 4. FrameworkCompetition / 竞争性框架验证

当 `narrative_transition` 被激活时，系统不应只搜索事实冲突，而应围绕公司身份的不确定性
生成**竞争性假设**，并针对每个假设建立独立的验证清单。

以京东方A为例，系统应生成三组竞争假设：

```text
假设A（旧框架派）：京东方A仍然是面板周期股，利润由面板ASP和稼动率决定，
                      AI硬件只是面板周期上行期的附加概念，不具备独立定价意义。

假设B（叙事派）：京东方A正在经历结构性质变，面板周期波动对利润的影响在
                     减弱，AI/物联网将逐步成为主要利润引擎，估值框架应切换。

假设C（折中派）：面板周期仍主导中短期利润（1-2年），但AI硬件提供了长期
                     re-rating的期权价值，不应完全按周期股估值。
```

针对每组假设，系统生成结构化验证清单：

| 验证项 | 假设A正确应看到 | 假设B正确应看到 | 实际数据 |
|--------|----------------|----------------|----------|
| 面板价格 vs 季度利润相关性 | R² > 0.7 且稳定 | R² 在趋势性下降 | 待收集 |
| AI/物联网收入增速 vs 面板收入增速 | 高度正相关 | 弱相关或独立 | 待收集 |
| 新业务capex占比趋势 | 不增长或缓慢 | 持续提升且加速 | 待收集 |
| 卖方估值框架变化 | 仍以PB/cycle定位 | 逐步切换至PE/growth | 待收集 |
| 管理层叙事落地证据 | 多次季报未兑现 | 收入结构/CAPEX出现拐点 | 待收集 |

核心思想：

```text
竞争性假设验证
= 识别身份漂移（narrative_transition）
+ 列出可能正确的互斥框架
+ 生成每个框架的验证条件
+ 收集区分性证据
→ 输出框架适用度评估（而非选择单一答案）
```

每个 activated context 在输出时还应附带趋势信息：

```json
{
  "activated_contexts": [
    {
      "context": "commodity_cycle",
      "score": 0.75,
      "trend": "declining",
      "expected_obsolescence": "2027Q2",
      "superseded_by": ["technology_adoption"]
    },
    {
      "context": "narrative_transition",
      "score": 0.90,
      "trend": "stable"
    },
    {
      "context": "technology_adoption",
      "score": 0.35,
      "trend": "rising"
    }
  ],
  "company_state": "in_transition",
  "central_tension": "管理层叙事 vs 数据现实：去周期化主张与周期收入仍占主导的矛盾",
  "narrative_evidence_gap": "high"
}
```

---

## 扩展机制：如何让它可持续演进

### 1. 优先新增“原语”和“组合规则”，而不是无限新增行业 YAML

扩展顺序建议是：

1. 先加 primitive
2. 再加 playbook
3. 最后加 router 映射规则

而不是每遇到一个新行业就直接复制出一个大 YAML。

### 2. 给每个 context 加版本和适用期

建议每个 primitive / playbook 都带：

- `version`
- `valid_from`
- `valid_to`
- `trigger_conditions`
- `deprecated_by`
- `assumptions`

这样它代表的是“当前可用框架”，而不是“永远正确知识”。

### 3. 支持 context score，而不是二元命中

很多 context 不是“适用/不适用”，而是“适用程度”。

建议 router 输出：

```json
{
  "context": "weather_shock",
  "score": 0.72,
  "why": [
    "overseas exposure",
    "region overlap",
    "current event active"
  ]
}
```

这允许系统：

- 高分：进入主线分析
- 中分：作为替代解释
- 低分：仅保留为备选，不展开

### 4. 拆开“静态知识”和“动态事件”

不要把“厄尔尼诺”直接写死在矿业 playbook 中。
正确分工应该是：

- `weather_shock.yaml`：稳定的传导逻辑
- `event_overlay.py`：当前是否存在 El Nino / La Nina、影响哪些区域

### 5. 用统一接口规范新 context

新增 context 时，统一填写：

- `when_to_activate`
- `key_variables`
- `causal_paths`
- `priority_questions`
- `disconfirming_signals`
- `preferred_sources`
- `report_angles`

这样扩展的是“实现插槽”，不是“概念列表”。

### 6. 给 primitives/playbooks 增加适用边界和过时标记

每个原语或 playbook 在应用于特定标的时，应记录其适用度的变化趋势：

```yaml
# commodity_cycle.yaml 对京东方A的适用边界
applicability_to_company:
  boe_a:
    current_score: 0.75
    score_trend: "declining_by_5pct_per_quarter"
    superseded_by: ["technology_adoption"]
    expected_obsolescence: "2027Q2"
    last_score_review: "2025Q4"
    obsolescence_triggers:
      - new_business_revenue_exceeds_30pct
      - panel_price_profit_correlation_r2_below_0.4
```

这样框架就有了"在特定标的上正在过时"的自我认知能力，
而不是只能被静态命中。趋势信息同时反哺 ContextRouter 的评分，
使 `score` 不再是孤立快照，而是带有方向性和临界条件。


---

## 如何接入现有流水线

### Phase 0：插入过渡态检测节点

在 `collect_raw_facts` 之后、`context_router` 之前，新增一个轻量检测节点：

```text
collect_raw_facts
→ detect_transition_state    ← 新增
→ context_router             ← 增强（接收 transition_state）
→ build_company_driver_profile
→ run_analysis_engines
→ explore_conflicts
```

`detect_transition_state` 负责：

1. 对比当前收入结构与3年前的变化趋势
2. 对比管理层叙事（年报MD&A关键词）与实际财务数据
3. 检测新旧框架驱动变量的解释力是否在相对变化
4. 输出 `company_state`（`stable` | `in_transition` | `redefined`）
   和 `narrative_evidence_gap`（`low` | `medium` | `high`）

ContextRouter 接收 `transition_state` 后，对处于 `in_transition` 的标的
自动激活 `narrative_transition` 元原语，并将活跃 contexts 的输出格式扩展为
带 `trend` / `expected_obsolescence` 的结构。

### Phase A：增强 company context

在当前 `company_context` 基础上新增：

- `business_model`
- `cycle_type`
- `driver_variables`
- `event_sensitive_exposures`

### Phase B：引入 ContextRouter

在 facts 收集后、explore/conflicts 之前执行：

```text
collect_raw_facts
→ build_company_driver_profile
→ context_router
→ run_analysis_engines
→ explore_conflicts
```

### Phase C：改 explore_conflicts prompt（含过渡态升级）

让探索不再是纯通用冲突模板，而是：

```text
generic conflict patterns
+ activated contexts
+ company specific drivers
+ event overlay
```

当 `company_state == "in_transition"` 时，`explore_conflicts` 应升级为
`generate_competing_hypotheses`：不只在事实层面搜索冲突，而是在框架层面
生成互斥的竞争性假设，识别每个假设的验证条件，并收集区分性证据。

### Phase D：改 verify_hypotheses plan

验证计划按 context 动态切换优先级：

- 牧原：猪价 / 仔猪价 / 能繁母猪 / 完全成本 / 出栏节奏
- 金诚信：项目区域暴露 / 极端天气影响 / 海外执行 / 矿业 CAPEX 周期

当 `company_state == "in_transition"` 时，验证计划不按单一主线切换，
而是为每一条竞争性假设分别生成验证子计划，以假设为维度收集证据，
确保"支持H1的证据"和"支持H2的证据"都被采集，而非预设某一方为正确。

### Phase E：改 insight / report 主线

强制最终报告围绕：

- `primary_driver`
- `central_tension`
- `driver-specific falsification conditions`

而不是所有公司都先写 ROE / PEG / 信号列表。

---

## 场景示例

### 牧原股份

应激活：

- `hog_cycle`
- `feed_cost`
- `capacity_cycle`

报告主线问题：

- 当前盈利修复来自猪价还是成本下降？
- 行业处于反转前段还是反弹后段？
- 牧原的成本优势是周期内变量还是长期结构优势？

### 金诚信

应激活：

- `mining_services`
- `project_delivery`
- `overseas_execution`
- `weather_shock`

报告主线问题：

- 订单增长对应的是短期景气还是矿业 CAPEX 周期延续？
- 极端天气会不会影响项目执行与利润兑现？
- 海外项目集中度是否放大气候与地域风险？

### 过渡期公司（以京东方A为代表）

公司特征：历史上是强面板周期股，管理层正在推动"去周期化"叙事，同时AI硬件、
物联网等新业务方向开始贡献收入，但旧周期收入仍占主导。

应激活 context：

- `commodity_cycle`          # 旧框架，正在过时但短期仍有解释力
- `narrative_transition`     # 元原语，描述框架切换过程
- `technology_adoption`      # 新框架候选，解释力在上升

central_tension：
管理层主张"去周期化"，但70%+收入仍来自面板周期，市场对不同时间尺度
该用哪套框架定价存在根本性分歧。

竞争性假设：

```text
H1（周期派）：面板ASP和稼动率仍是利润核心驱动，AI硬件是周期上行附带的
                概念炒作，不应作为独立定价因子。

H2（叙事派）：公司正在经历结构性质变，面板利润波动在减弱，
                AI/物联网将逐步成为主要利润引擎，应切换到成长估值框架。

H3（折中派）：中短期（1-2年）面板周期主导利润，但AI硬件提供了长期
                 re-rating的期权价值，估值应在周期底和高之间找到新均衡。
```

验证清单：

| 验证项 | 支持H1 | 支持H2 |
|--------|--------|--------|
| 面板价格对季度利润的解释力（R²） | R²持续高且稳定 | R²趋势性下降 |
| AI/物联网收入增速 vs 面板收入增速 | 两者高度正相关 | 弱相关或独立 |
| 新业务 capex 占比趋势 | 停滞或缓慢 | 持续提升且加速 |
| 卖方报告估值框架 | 仍以PB/cycle定位 | 出现PE/growth定位 |
| MD&A叙事落地证据 | 未兑现为结构性变化 | 收入结构/CAPEX出现拐点 |

报告主线问题：

- 面板周期对利润的解释力正在结构性地下降，还是周期性地下降？
- AI硬件/物联网收入的增速是否独立于面板景气周期？
- CAPEX的转向速度是否支持2-3年内收入结构发生质变？
- 市场目前在定价哪一套框架——周期还是成长？这个定价是否合理？
- 过渡完成需要满足什么条件？这些条件当前是在出现还是恶化？
- 如果过渡失败（新业务增速低于预期），公司的下行风险是什么？

---

## 推荐落地顺序

### P0

1. 新增 `DriverProfile / CompanySpecificContext`
2. 建立 `domain_primitives/`，优先沉淀 5-8 个高频 primitives
3. 新增 `narrative_transition` 元原语和 `detect_transition_state` 节点

### P1

1. 建立 `domain_playbooks/`
2. 上线 `ContextRouter`（含 `company_state` 和 context trend 输出）
3. 给 explore/verify prompt 注入 activated contexts
4. 上线 `FrameworkCompetition` 竞争性假设生成与验证机制

### P2

1. 上线 `EventOverlay`
2. context score / ranking（含趋势维度）
3. 报告主线切换为 driver-first
4. `narrative_evidence_gap` 量化与追踪
5. primitives/playbooks 适用边界与过时标记

### P3

1. 引入更多外部事件源
2. 做 context effectiveness 回放（含过渡态判断准确率）
3. 让 task records 反哺 context 迭代
4. 过渡期公司分析效果专项评估

---

## 成功标准

如果这条 roadmap 生效，最终应表现为：

- 牧原的报告自动像“猪周期分析”
- 金诚信的报告自动像“矿业项目与天气扰动分析”
- 同一行业内不同公司，报告主线也能不同
- context 不是越积越僵，而是能通过 primitives + playbooks + runtime overlay 持续扩展
- 处于过渡期的公司能自动识别身份漂移，报告围绕"新旧框架博弈"展开，
  而不是违和地套用纯周期或纯成长的单一框架
