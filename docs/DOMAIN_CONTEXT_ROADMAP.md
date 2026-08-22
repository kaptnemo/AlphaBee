# AlphaBee Domain Context Roadmap

> **定位（三角分工，与两份兄弟文档对齐）**
>
> 本仓库的"公司语境注入"分三层，本路线图只负责**定性叙事层**：
>
> ```text
> 语境注入栈
> ├─ industry-context-injection-plan：申万行业基线（可计算数值层）
> │   └─ 行业中位数基准（industry_*）+ 阈值机制 + 注入通道
> ├─ COMPANY_TRACK_ROADMAP：公司级覆盖（可计算数值层）
> │   └─ 业务线解构 + 商业模式 archetype + 对标组基准（peer_*）
> └─ DOMAIN_CONTEXT_ROADMAP（本文档）：定性叙事层
>     └─ primitives / playbooks / ContextRouter / EventOverlay / FrameworkCompetition
> ```
>
> 依赖方向：本文档**只消费**上面两层的产物（`INDUSTRY_CONTEXT` / `COMPANY_TRACK` artifact、
> `industry_*` / `peer_*` fact_values、`business_model` archetype、`detect_track_drift` 漂移笔记），
> 不重复建设它们已经覆盖的"数值/结构化标签"部分。

> **实现状态（2026-08 与代码对齐）**
> - P0 第 1-2 步 ✅：`PrimitiveSchema` / `PlaybookSchema` + `loader`（加载 + 目录闭合校验）+ 6 primitive + 2 playbook + 1 兜底清单（见「P0 落地记录」）。
> - P0 第 3 步 ✅：`ContextRouter`（`context_router.py`）——规则版公司 → playbook 匹配 + fallback + 降级。
> - P0 第 4 步 ✅：`DriverProfile` 契约 + `build_driver_profile` + `ArtifactType.DRIVER_PROFILE` 注册 + `coerce_driver_profile`。
> - P0 第 5 步 ✅：`resolve_driver_profile` 节点（`orchestrator/nodes/resolve_driver_profile.py`）——读 INDUSTRY_CONTEXT + COMPANY_TRACK → 路由 → 落 DRIVER_PROFILE artifact，并注入 `synthesize_insights`（driver_profile 进 insight context + prompt 原则 9）。**P0 五步全部完成。**
> - 其余未落地（P1/P2）：`detect_transition_state` / `EventOverlay` / `FrameworkCompetition` / context score 均未开始。
> - 但"身份漂移"的**可计算种子已存在**：`alphabee/company_track/label.py::detect_track_drift` 已实现跨年报期业务主线漂移检测，写入 `CompanyTrackArtifact.review_notes`。本文档的 `detect_transition_state` / `narrative_transition` 是**其上的定性层**，应消费它而非从零推导。
> - 报告主线切换的**落点已存在**：`InsightArtifact`（`orchestrator/contracts.py`）已带 `central_tension` / `main_driver` / `business_model_context`，Phase E 应写进这里而非新增 report 字段。
> - 新鲜度/版本机制已存在：`CompanyTrackArtifact.stale_after` / `degraded` / `review_notes`，本文档复用而非另造。
> - 行业感知雏形已收敛：`_FINANCIAL_INDUSTRIES` / `_HIGH_LEVERAGE_INDUSTRIES` 等硬编码常量已迁移为 `alphabee/industry/names.py` + `industry_names.yaml` groups + `industry_in_group()`。
> - **设计评审（2026-08）**：已评审，结论「方向正确、骨架健康」，P0 已收紧为 5 步，记录 6 项必须解决 + 3 项次要 + 4 项待决（见下方「Review 记录」）。

## Review 记录（2026-08，设计评审结论）

> 评审结论：**方向正确、骨架健康，可在其上实现，但开工前需收紧 3 处定义、修正 1 处数据可行性判断、
> 确认 4 个决策**。评审意见已并入下文各 Phase。

### 保留项（勿动）

1. 三层架构（primitives / playbooks / runtime）切分正确，是解决"静态知识库僵化"的关键。
2. 扩展机制 §6「静态 YAML 只存条件、运行时状态存值」是全文最重要工程洞察。
3. 复用纪律（`detect_track_drift` / `InsightArtifact` 字段 / `CompanyTrackArtifact` 新鲜度 / conflicts 模型）正确。
4. 命名修正（`playbook` vs `business_model` archetype）正确。
5. 目录闭合约束 +「playbook 展开为 primitive」两个护栏正确。

### 必须解决（开工前）

| # | 问题 | 结论 |
|---|---|---|
| 1 | router 映射表无 schema / owner / 版本 | ✅ 已落地：映射改为 playbook `match_*` 字段（schema + version 由 `PlaybookSchema` 承载），见「P0 落地记录」 |
| 2 | primitive 无 canonical schema（两个示例字段集不一致） | 定义 `PrimitiveSchema` / `PlaybookSchema`（required/optional + 校验器），对齐 derived_facts 的 YAML 规则规范 |
| 3 | "5–8 个 primitive"与 14 个列表冲突、无闭包规则 | P0 定死清单（见下方收紧版 P0），不按 14 个全量开工 |
| 4 | `detect_transition_state` 的"MD&A 文本已有"判断有误 | 已核实：MD&A/管理层讨论文本当前未采集（`financial_report` OCR 是独立 pipeline，未进 orchestrator fact collection）。Phase 1 item 2 需先补 MD&A 采集或降级 |
| 5 | FrameworkCompetition 复用 conflicts 模型的字段级映射未定义 | 落到字段级：theme / severity / hypothesis.predictions 如何承载框架假设 A/B/C |
| 6 | EventOverlay × web_search_guard 是"可能做不了"级风险 | 若 guard 放行策略定不下，EventOverlay 直接砍，不阻塞 P0/P1 |

### 次要问题

- `DriverProfile` vs `InsightArtifact` vs `CompanyContext` 三处"context"表示可能冗余，需一句 source-of-truth 声明。
- 缺 router 确定性单测 / schema 校验单测 / DRIVER_PROFILE 契约测试（仓库文化要求 YAML 规则可单测）。
- router 命中不了 playbook 时的 fallback 行为未定义（这是大多数公司的常态路径，必须定义）。

### 待拍板决策

1. ✅ P0 清单已定（6 primitive + 2 playbook + 1 兜底，见「P0 落地记录」）。
2. ✅ 兜底行为已定：`generic_fundamental` playbook 兜底；「是否产 issue」随 P0 第 3 步 router 落地再定。
3. MD&A 文本是否现在补采集（决定 Phase 1 item 2 能否做）。
4. EventOverlay 是"争取做"还是"先砍，P0/P1 优先"。

### 收紧后的 P0（5 步，逐步验收）

| 步 | 交付物 | 测试 |
|---|---|---|
| 1 | `PrimitiveSchema` + `PlaybookSchema`（canonical 字段 + 校验器 + 目录闭合校验） | `test_schema.py` |
| 2 | 定死 P0 清单：6 primitive + 2 playbook + 1 通用兜底（非 14 个） | schema 校验覆盖 |
| 3 | `router_mapping`（playbook `match_*` 字段）+ `context_router.py`（含 fallback / 缺失降级 / version） | `test_context_router.py`（确定性） |
| 4 | `ArtifactType.DRIVER_PROFILE` + `DriverProfile` + `coerce_driver_profile` | 契约测试 |
| 5 | 只注入 1 个节点（`synthesize_insights`，写 central_tension/main_driver）跑通牧原/金诚信 golden | `test_golden.py` |

> 第 5 步刻意只注入一个节点：先在一个落点证明"激活的 context 真的改变观点主线"，再扩到
> explore/verify/thesis/report，否则一次性注入 4 个 prompt 点无法判断哪一步有效。

### P0 落地记录（第 1–5 步 ✅，P0 全部完成）

已落地（`alphabee/domain_context/` + orchestrator 集成）：

- `schemas.py`：`PrimitiveSchema` / `PlaybookSchema`（strict `extra="forbid"`，字段漂移即报错）。
- `loader.py`：`load_primitives` / `load_playbooks` / `load_catalog` + `validate_closure`（目录闭合校验）。
- `context_router.py`：`route` / `RouterInput` / `RouterResult` / `ActivatedContext`——规则版公司 → playbook 匹配（含 fallback / 降级 / version）。
- `contracts.py`：`DriverProfile` / `ActivatedPrimitive`（`ArtifactType.DRIVER_PROFILE` 契约）。
- `driver_profile.py`：`build_driver_profile`（路由 + 展开激活原语完整内容）。
- `alphabee/core/schemas.py`：注册 `ArtifactType.DRIVER_PROFILE = "driver_profile"`（role group DATA）。
- `alphabee/orchestrator/contracts.py`：re-export `DriverProfile` + `coerce_driver_profile`。
- `alphabee/orchestrator/nodes/resolve_driver_profile.py`：在线节点（读 INDUSTRY_CONTEXT + COMPANY_TRACK → 路由 → 落 DRIVER_PROFILE artifact，降级留痕）。
- `alphabee/orchestrator/agent.py`：graph 接线 `resolve_company_track → resolve_driver_profile → run_analysis_engines`。
- `alphabee/orchestrator/services/payload_builders.py`：`_build_driver_profile_summary` + 注入 `build_insight_context`。
- `alphabee/agents/insights/prompts.py`：InsightAgent 系统提示新增原则 9（driver_profile 优先）。
- `domain_primitives/`：6 个原语。
- `domain_playbooks/`：2 个框架 + 1 兜底。
- 测试：`tests/domain_context/`（26 用例）+ `tests/orchestrator/test_resolve_driver_profile.py`（6 用例）全绿。

**清单决策（已定）**：

| 项 | 值 |
|---|---|
| 6 primitive | commodity_cycle / capacity_cycle / cost_curve / working_capital_stress / biological_inventory / project_delivery |
| 2 playbook | hog_cycle = commodity_cycle + biological_inventory + cost_curve + capacity_cycle；mining_services = commodity_cycle + project_delivery + cost_curve + capacity_cycle |
| 1 兜底 | generic_fundamental = cost_curve + capacity_cycle + working_capital_stress |

**路由决策（已定，落地 Review 问题 #1）**：映射表**不单独建 `router_mapping.yaml`**，而是落在
playbook 的 `match_*` 字段（`match_track_labels` / `match_sub_industries` / `match_business_models`）——
它们已随 `PlaybookSchema` 拥有 schema + 版本，数据驱动、非硬编码，比独立映射文件更少重复、更好审计。
匹配权重：track_label=3 > sub_industry/industry=2 > business_model=1，同分按 playbook id 决平；
`business_model` 只作低权输入信号，不产 playbook（见「与 business_model archetype 的边界」）。

**与正文示例的偏差（已记录）**：P0 最小集优先「高频 + 覆盖两个 golden + 含通用项供兜底」，
正文 Layer 1/2 示例中的 `feed_cost` / `epidemic_risk` / `overseas_execution` / `commodity_capex_cycle`
未纳入 P0，按扩展机制 #1「先加 primitive 再加 playbook」后续补入；`mining_services` 比正文示例
多引用了 `cost_curve`（成本曲线对矿业服务同样关键）。

**golden 路由验证（已过）**：牧原（track_label=生猪养殖 + sub_industry=养殖业）→ hog_cycle；
金诚信（track_label=矿业服务 + sub_industry=采掘服务）→ mining_services；贵州茅台（白酒）→
generic_fundamental（fallback，非降级）；空输入 → generic_fundamental（fallback + degraded）。

---

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

   > 注：第 5 点的"收入结构漂移"部分，代码层已有 `detect_track_drift` 提供可观测信号
   > （`CompanyTrackArtifact.segments` 跨年报期对比 + `review_notes` 漂移笔记）。
   > 本文档的 `narrative_transition` 是在该信号之上叠加"管理层叙事 vs 数据现实"的定性判断，
   > 不是重复实现漂移检测。

因此更合理的实现方式是分层。

---

## 三层架构

### Layer 1：稳定的分析原语（Domain Primitives）

这层沉淀变化慢、可跨行业复用的分析积木：

```text
domain_primitives/
  commodity_cycle.yaml
  capacity_cycle.yaml
  cost_curve.yaml
  project_delivery.yaml
  overseas_execution.yaml
  weather_shock.yaml
  policy_shock.yaml
  cost_pass_through.yaml
  working_capital_stress.yaml
  biological_inventory.yaml     # 生物性资产（猪/鸡存栏、疫病）专用
  feed_cost.yaml                # 饲料/原材料成本传导
  epidemic_risk.yaml            # 区域性疫病冲击
  reserve_grade.yaml            # 资源品位/储量质量
  geopolitical_risk.yaml        # 资源国政治/地缘风险
  narrative_transition.yaml     # 元原语：描述框架切换过程本身
```

> **目录闭合约束**：playbook 只能引用本目录已声明的 primitive（可加 schema 校验强制）。
> 上表已补全正文 playbook 示例引用到的所有原语（`biological_inventory` / `feed_cost` /
> `epidemic_risk` / `reserve_grade` / `geopolitical_risk` / `cost_curve`），避免"组合引用了
> 不存在的积木"。原 `competing_frameworks` 元原语并入 `narrative_transition`——
> 竞争性假设是 `narrative_transition` 触发后由 Runtime 层（复用现有 conflicts 机制）执行的动作，
> 不是一个独立原语（见下文 FrameworkCompetition）。

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
  - company_state == in_transition      # 由 detect_transition_state 输出，见接入流水线
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
  - segment_revenue_breakdown            # 消费 COMPANY_TRACK 的 segments（跨年报期）
  - capex_allocation_by_segment
  - management_discussion_analysis
  - industry_chain_verification
  - sell_side_consensus_driver_analysis  # 可选增强，非硬依赖
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

- 适用标的特征（映射到 `company_track` 的 `track_label` / `business_model` archetype / `sw_industry`）
- 主驱动变量
- 次驱动变量
- 最重要的冲突模板
- 推荐验证顺序
- 报告应该围绕哪些问题写

> **语义约定**：playbook 是"命名的 primitive 集合"，不是独立的一级概念。
> ContextRouter 匹配到 playbook 后，**展开为其 primitive 集合**再进入下游；下游消费者只看到
> primitive 列表，避免 `activated_contexts` 同时混入 primitive 和 playbook 两种单位。

### Layer 3：运行时上下文（Runtime Context）

真正动态的是这层。

```text
runtime_context/
  context_router.py        # 公司 → 可激活 context（含展开 playbook + 打分）
  event_overlay.py         # 动态事件叠加到静态框架
  company_driver_profile.py # 公司驱动画像（DriverProfile，落 DRIVER_PROFILE artifact）
```

> 原草案里的 `context_ranker.py` 并入 `context_router.py`：打分（rank）是 router 的一个纯函数，
> 无需独立文件。

它要做的不是“存知识”，而是根据当前标的、问题和外部环境生成：

```json
{
  "activated_contexts": [
    {"context": "weather_shock", "score": 0.72, "trend": "stable"},
    {"context": "project_delivery", "score": 0.68, "trend": "stable"}
  ],
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

比当前 `CompanyContext`（`agents/thesis/models.py`，只有 industry / lifecycle / business_model
archetype 等）更细，输出公司真正受什么变量驱动。**这是一个新的 artifact 契约，不是 `CompanyContext`
的扩展，也不进 `OrchestratorState` 顶层。**

建议结构（`ArtifactType.DRIVER_PROFILE = "driver_profile"`，role group 建议 DATA）：

```json
{
  "symbol": "002714.SZ",
  "playbook": "hog_farming",
  "playbook_evidence": "company_track.track_label=生猪养殖 + sw_industry=养殖业",
  "cycle_type": ["hog_cycle", "capacity_cycle"],
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

> **命名修正**：原草案用 `business_model` 承载 playbook 名（`hog_farming | mining_services | ...`），
> 但 `CompanyContext.business_model` 已被 archetype 占用（`brand/odm/component/integrator/other`，
> `company_track/business_model.py`）。两者不同概念同名会打架，故 DriverProfile 改用 `playbook`，
> `business_model` 保留给 archetype。

### 2. ContextRouter

负责把公司映射到可激活的 contexts。

输入来源（**全部来自已落地产物**，不重复取数）：

- `INDUSTRY_CONTEXT` artifact：`industry` / `sub_industry` / `sw_code`
- `COMPANY_TRACK` artifact：`track_label` / `business_model`（archetype）/ `segments` / `review_notes`（漂移笔记）
- 公司业务描述（`build_company_context` 的 `business_model_summary`）
- 用户问题
- 当前事件环境（EventOverlay，Phase 2 起）

输出（统一为 object 数组，playbook 已展开为 primitive）：

```json
{
  "activated_contexts": [
    {"context": "commodity_cycle", "score": 0.75, "trend": "declining"},
    {"context": "biological_inventory", "score": 0.70, "trend": "stable"},
    {"context": "cost_curve", "score": 0.60, "trend": "stable"}
  ],
  "primary_driver": "hog_cycle",
  "secondary_drivers": ["feed_cost", "capacity_cycle"],
  "why_selected": [
    "sub_industry_match",
    "business_description_match",
    "user_query_intent"
  ]
}
```

> 注：`hog_cycle` 是 playbook（命名 bundle），已展开为 `commodity_cycle` / `biological_inventory`
> / `feed_cost` / `epidemic_risk` / `capacity_cycle` 等 primitive 进入 `activated_contexts`。
> 是否保留 playbook 名作展示层别名，由报告层决定，不进路由结果。

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

> **事件数据源（必须明确，否则会与 web_search_guard 冲突）**：
> - 结构化事件优先走 tushare/akshare 的宏观/商品/政策接口（不触发 guard 的价格拦截）；
> - 若走新闻流，`web_search_guard` 会拦价格/估值/财务类搜索，需提前定义事件查询的放行白名单
>   （例如"厄尔尼诺 影响 矿业"这类天气/政策事件，而非"铜价 走势"这类价格查询）。
> - 事件源与 guard 的交互必须在 Phase 2 落地 EventOverlay 前定稿，否则实现会被 guard 卡死。

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

> **实现约定（避免重复建设）**：FrameworkCompetition **复用现有** `explore_conflicts` /
> `verify_hypotheses` 的数据模型（`ConflictAnalysisResult` / `VerificationResultItem`）与结算逻辑，
> 只是在"抽象级别"上从"事实冲突"提升到"框架冲突"——假设 A/B/C 就是 hypotheses，
> 验证清单就是 verification plan，区分性证据就是 evidence。**不新建数据模型**，只改 prompt
> 与 `related_dimensions` 的语义（维度 → 框架）。

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

### 5. 与 business_model archetype 的边界（source-of-truth）

`company_track/business_model.py` 的 archetype（brand/odm/component/integrator/other）与本层的
primitives/playbooks 是**两个正交的分类轴**，不重复、不竞争：

| 概念 | 回答的问题 | 输入依据 | 下游作用 | 归属 |
|---|---|---|---|---|
| `business_model` archetype | 公司怎么赚钱（价值捕获方式） | 财务结构（毛利率/研发费率/客户集中度） | 校准「怎么解读」财务信号（口径切换，如 ODM 低毛利≠恶化） | company_track |
| playbook / primitives | 什么变量驱动盈利（驱动框架） | 行业身份（track_label / sw_industry / 业务构成） | 决定「看什么」（选题切换，如猪价 vs 矿价） | domain_context |

一句话：**`business_model` 管「怎么读」，playbook 管「看什么」。**

关系：`business_model` archetype 是 ContextRouter 的**输入信号之一**（与 track_label、sw_industry
并列），不是输出，也不被 playbook 覆盖；`BUSINESS_MODEL_FOCUS` 只写「口径级」内容，不写「行业驱动
变量」（后者归 `primitive.key_variables`）。

---

## 扩展机制：如何让它可持续演进

### 1. 优先新增“原语”和“组合规则”，而不是无限新增行业 YAML

扩展顺序建议是：

1. 先加 primitive
2. 再加 playbook
3. 最后加 router 映射规则

而不是每遇到一个新行业就直接复制出一个大 YAML。

### 2. 给每个 context 加版本和适用期（复用 company_track 的新鲜度机制）

建议每个 primitive / playbook 都带（与 `CompanyTrackArtifact` 的 `stale_after` / `degraded` /
`review_notes` 同族，不另造一套）：

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

### 6. 适用边界与过时标记：静态 YAML 只存“条件”，运行时状态存“值”

原草案把 per-company 的适用度评分（`current_score` / `score_trend` / `expected_obsolescence` /
`last_score_review`）直接写进 `commodity_cycle.yaml`，这违反了本文档开头的核心原则——
把运行时状态烤进静态知识，正是"静态行业知识库僵化"的翻版。

正确分工：

```yaml
# commodity_cycle.yaml —— 只存“不变的触发条件”，不存“会变的当前值”
id: commodity_cycle
obsolescence_triggers:          # 框架失效的判定条件（静态）
  - new_business_revenue_exceeds_30pct
  - panel_price_profit_correlation_r2_below_0.4
```

```json
// runtime_context/store —— per-(symbol, context) 的运行时状态，由 context_ranker 读写
{
  "key": "000725.SZ::commodity_cycle",
  "current_score": 0.75,
  "score_trend": "declining_by_5pct_per_quarter",
  "superseded_by": ["technology_adoption"],
  "expected_obsolescence": "2027Q2",
  "last_score_review": "2025Q4"
}
```

趋势信息同时反哺 ContextRouter 的评分，使 `score` 不再是孤立快照，而是带有方向性和临界条件。

> **维护归属（避免又一批不更新的 YAML）**：`last_score_review` / `expected_obsolescence`
> 这类字段必须有刷新触发流程（报告期披露、季度回放、人工复核），并在 owner 处落一个
> `reviews/` 审计记录。否则它会重蹈 `_HIGH_LEVERAGE_INDUSTRIES` 那类硬编码的覆辙。

---

## 如何接入现有流水线

当前主图（`orchestrator/agent.py`）已经包含两个语境节点，本文档的新节点插在其后：

```text
START
→ collect_raw_facts
→ resolve_industry_context      ← 已有（INDUSTRY_CONTEXT artifact + industry_* fact_values）
→ resolve_company_track         ← 已有（COMPANY_TRACK artifact + peer_* fact_values）
→ resolve_driver_profile        ← 新增 ✅（本文档：DriverProfile + ContextRouter，P0 已落地）
→ detect_transition_state       ← 新增（消费 COMPANY_TRACK segments + MD&A，P1）
→ run_analysis_engines
→ explore_conflicts             ← 增强（注入 activated contexts + 过渡态升级）
→ verify_hypotheses             ← 增强（按 context / 竞争假设切换验证优先级）
→ synthesize_insights           ← 增强（central_tension / main_driver 落点）
→ run_thesis → review_thesis → generate_report → review_report → finalize_message → END
```

> 注意：原文档的"接入流水线"画的是旧图（`collect_raw_facts → run_analysis_engines` 直连），
> 已过时。上述为对齐代码的版本。

### Phase 0：DriverProfile + 规则版 ContextRouter（最小闭环）

在 `resolve_company_track` 之后新增一个节点，**规则优先、LLM 复核可选**：

1. 读 `INDUSTRY_CONTEXT` + `COMPANY_TRACK` artifact（`find_artifact_model`）；
2. 用规则把 `track_label` / `business_model`（archetype）/ `sw_industry` 映射到 5–8 个 primitive，
   展开命中的 playbook；
3. 输出 `DRIVER_PROFILE` artifact（`ArtifactType.DRIVER_PROFILE`）；
4. 注入 explore/verify/thesis/report 的 prompt（activated contexts + 驱动变量 + 专属问题）。

> **artifact 契约（仓库约定，必须遵守）**：`DriverProfile` / `RuntimeContext` 走
> `ArtifactType` + `find_artifact_model(...)`，**不给 `OrchestratorState` 增加顶层字段**
> （现有 `state.py` 无 `transition_state` / `domain_context`，也无需加）。

### Phase 1：detect_transition_state 节点

在 `resolve_driver_profile` 之后、`run_analysis_engines` 之前，新增轻量检测节点：

`detect_transition_state` 负责：

1. 对比当前收入结构与 3 年前的变化趋势——**直接消费 `CompanyTrackArtifact.segments` 跨年报期数据**
   （已有，勿重复取数），趋势骨架可复用 `detect_track_drift` 的输出；
2. 对比管理层叙事（年报MD&A关键词）与实际财务数据；
3. 检测新旧框架驱动变量的解释力是否在相对变化；
4. 输出 `company_state`（`stable` | `in_transition` | `redefined`）
   和 `narrative_evidence_gap`（`low` | `medium` | `high`）。

ContextRouter 接收 `transition_state` 后，对处于 `in_transition` 的标的
自动激活 `narrative_transition` 元原语，并将活跃 contexts 的输出格式扩展为
带 `trend` / `expected_obsolescence` 的结构。

> **数据可行性（降级契约，2026-08 评审修正）**：①依赖已有数据（segments，可靠）；②"MD&A 文本"
> **当前未采集**（`financial_report` OCR 是独立 pipeline，未进 orchestrator fact collection），
> 需先补 MD&A 采集或将 item 2 降级为可选；③的"驱动变量解释力 R²"需历史时序回归，样本不足时
> 降级为定性判断并留痕；"卖方共识 driver"（研报抽取）难拿，**作为可选增强**，不作为
> `company_state` 判定的硬依赖。

### Phase 2：改 explore_conflicts / verify_hypotheses prompt（含过渡态升级）

让探索不再是纯通用冲突模板，而是：

```text
generic conflict patterns
+ activated contexts
+ company specific drivers
+ event overlay
```

当 `company_state == "in_transition"` 时，`explore_conflicts` 升级为
`generate_competing_hypotheses`：在**框架层面**生成互斥的竞争性假设（复用现有
conflict/hypothesis 数据结构），识别每个假设的验证条件，收集区分性证据。
验证计划按 context 动态切换优先级：

- 牧原：猪价 / 仔猪价 / 能繁母猪 / 完全成本 / 出栏节奏
- 金诚信：项目区域暴露 / 极端天气影响 / 海外执行 / 矿业 CAPEX 周期

当 `company_state == "in_transition"` 时，为每条竞争性假设分别生成验证子计划，
以假设为维度收集证据，确保"支持H1的证据"和"支持H2的证据"都被采集。

### Phase 3：改 insight / report 主线

强制最终报告围绕：

- `primary_driver`
- `central_tension`
- `driver-specific falsification conditions`

而不是所有公司都先写 ROE / PEG / 信号列表。

> **落点**：`primary_driver` / `central_tension` 写入 `InsightArtifact`（`contracts.py`）已有的
> `central_tension` / `main_driver` 字段，由 `synthesize_insights` 消费 `DRIVER_PROFILE` artifact
> 填充，报告层读 `InsightArtifact`，不新增 report 字段。

---

## 场景示例

### 牧原股份

应激活：

- `hog_cycle`（= commodity_cycle + biological_inventory + feed_cost + epidemic_risk + capacity_cycle）
- `feed_cost`
- `capacity_cycle`

报告主线问题：

- 当前盈利修复来自猪价还是成本下降？
- 行业处于反转前段还是反弹后段？
- 牧原的成本优势是周期内变量还是长期结构优势？

### 金诚信

应激活：

- `mining_services`（= project_delivery + overseas_execution + commodity_capex_cycle）
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

## 推荐落地顺序（重排：先最小闭环，再过渡态，最后事件覆盖）

### P0（最小闭环，最快见效，风险最低）——已收紧为 5 步（详见「Review 记录」）

1. ✅ `PrimitiveSchema` + `PlaybookSchema`（canonical 字段 + 校验器 + 目录闭合校验）；
2. ✅ 定死 P0 清单：6 primitive + 2 playbook + 1 通用兜底（见「P0 落地记录」）；
3. ✅ `router_mapping`（落在 playbook `match_*` 字段）+ 规则版 `context_router.py`（含 fallback / 缺失降级 / version）；
4. ✅ `ArtifactType.DRIVER_PROFILE` + `DriverProfile` + `coerce_driver_profile`；
5. ✅ 新增 `resolve_driver_profile` 节点 + 注入 `synthesize_insights`（driver_profile 进 insight context + prompt 原则 9），
   牧原/金诚信 golden 已跑通；后续按需扩到 explore/verify/thesis/report。

### P1（过渡态，依赖 P0 且须配 eval）

1. `detect_transition_state` 节点（消费 COMPANY_TRACK segments + MD&A）；
2. `narrative_transition` 元原语 + `company_state` / `narrative_evidence_gap` 输出；
3. `FrameworkCompetition`（复用 conflicts 数据结构，框架层假设 A/B/C）。

### P2（事件与主线，依赖 P1）

1. `EventOverlay`（先定事件源 + guard 放行策略）；
2. context score / ranking（含 trend 维度）；
3. 报告主线切换为 driver-first（写 `InsightArtifact.central_tension/main_driver`）；
4. primitives/playbooks 适用边界与过时标记（运行时 store 化，见扩展机制 6）。

### P3（评估与演进）

1. 引入更多外部事件源；
2. context effectiveness 回放（见验收标准）；
3. 让 task records 反哺 context 迭代；
4. 过渡期公司分析效果专项评估。

---

## 验收标准（可量化，配合仓库测试文化）

成功标准不能只靠定性描述，需配可回放、可打分的指标：

1. **报告主线命中率**：建 golden 样本集（牧原→猪周期主线、金诚信→矿业项目+天气扰动、
   京东方A→过渡期框架博弈），判据 = 报告摘要含 `primary_driver` 关键词 + `central_tension`
   被显式论述。目标：golden 样本命中率 100%，回归不下降。
2. **context effectiveness 回放**：每次 run 记录 `activated_contexts` + 报告主线，回放后用
   LLM-as-judge（或人工抽检）评"该 context 是否真的改变了报告结构"，追踪
   "激活但未消费"的浪费率。
3. **过渡态判断准确率**：`company_state` 判定的 ground truth 需先标注一批已知过渡期公司
   （京东方A / 潍柴动力 / 中国中免），P3 专项评估准确率与误判方向。
4. **降级契约**：`INDUSTRY_CONTEXT` / `COMPANY_TRACK` 缺失或降级时，ContextRouter 必须显式
   issue 留痕（复用 `industry_context_missing` / `company_track_missing` 模式），不静默回退。

---

## 成功标准

如果这条 roadmap 生效，最终应表现为：

- 牧原的报告自动像“猪周期分析”
- 金诚信的报告自动像“矿业项目与天气扰动分析”
- 同一行业内不同公司，报告主线也能不同
- context 不是越积越僵，而是能通过 primitives + playbooks + runtime overlay 持续扩展
- 处于过渡期的公司能自动识别身份漂移，报告围绕"新旧框架博弈"展开，
  而不是违和地套用纯周期或纯成长的单一框架
- 上述表现可被"主线命中率 / effectiveness 回放 / 过渡态准确率"三组指标持续量化追踪
