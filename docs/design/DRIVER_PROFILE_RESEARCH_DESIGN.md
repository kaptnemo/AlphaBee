# 驱动画像研究化改造设计（DriverProfile：路由快照 → 驱动假设 + 研究议程）

> **状态：✅ 设计已拍板（2026-10 用户决议；§14.1 + §14.2 全部按建议采纳）；代码尚未实施。**
> **已拍板（§14.1）**：R-1 `web_search_guard` 词边界匹配、R-2 驱动变量查询窄白名单、
> R-3 `check_message_limit` 阈值可配置（默认仍 50）——三者是 D1 的前置条件，待 D0/D1 落地时一并实现。
> **已拍板（§14.2，2026-10，均按建议）**：Tier B 采用 **flag 门控起步**；`candidates` **本设计只落字段**（博弈留 P1）；
> 缓存落 `data/driver_profiles/<symbol>.json`；`novel_drivers` 由离线工具产候选、**人工 review 后**才成 PR；
> **D0 独立交付、D1/D2 成对交付**；**同期修复 golden 夹具保真度**（`sub_industry=""` + "仅 track_label 命中"用例）；
> **同期引入 `sw_code` 前缀匹配**（修「农林牧渔→hog_cycle」宽 L1 误配）。详见 §14.2。
> 关联文档：`docs/roadmap/DOMAIN_CONTEXT_ROADMAP.md`（P0 已落地；§"设计稿：覆盖扩展与兜底洞察升级"仍待拍板）、
> `docs/roadmap/COMPANY_TRACK_ROADMAP.md`、`docs/design/DEVIATION_CONTROL_FRAMEWORK.md`（节点契约与恢复阶梯）、
> `docs/design/SEMANTIC_JUDGMENT_CONTRACT_DESIGN.md`（"结构化契约 + 受控 judge"范式）。
> **契约改动须走 `alphabee-pipeline-contract-steward` 流程；涉及 canonical 字段须走 `alphabee-schema-steward`。**

---

## 0. 结论先行

`resolve_driver_profile` 现在是一个**纯规则路由**：把 4 个浅层字符串与 3 个 YAML 做子串匹配，
命中即抄 playbook 的常量、未命中即回退 `generic_fundamental`。问题不是"实现得笼统"，而是
**它把一件需要研究的事做成了一次查表**：

1. **覆盖率结构性为零**：专用 playbook 只有 `hog_cycle` / `mining_services` 两个，
   87 条历史 task record 中**零次**命中专用框架；13 条 / 9 个标的显式记录为 `generic_fundamental`（§1.1）。
2. **知识内容是 dead-end**：`generic_fundamental` 的 `primary_drivers` 为空，
   于是 CLI 恒显示「驱动: —」（`apps/cli/renderer.py:301`）；
   而即便命中，primitive 的 `key_variables` / `disconfirming_signals` / `preferred_sources`
   **全仓库无任何消费者**（§1.2）。当前框架下"驱动画像"实质只有 `priority_questions` + `report_angles` 两个字段起作用。
3. **信息带宽浪费了三个数量级**：节点只读 4 个字符串，而此刻 state 里已有 `COMPANY_TRACK.segments`
   （分部营收/占比/同比/分部毛利率）、全量 `fact_values`、行业基准，磁盘上还有 36 家公司的**已解析年报全文**（§1.3）。
4. **它不是"研究切入点"，因为没人读它**：`driver_profile` 目前**只被 `synthesize_insights` 一个下游消费**
   （经 `build_insight_context`），`explore_conflicts` / `verify_hypotheses` / `run_thesis` / `generate_report` 全部不读（§1.4）。
   接线层另有 10 处硬缺口（§1.6），其中三处尤其致命：
   **报告层 `ReportGenerationPayload` 根本没有 `driver_profile` 字段**（G-5）；
   **insight 的 Tier 2 确定性兜底完全不读驱动画像**（G-4）；
   **契约声明的 `artifact_schema_valid` 对 `DRIVER_PROFILE` 是空操作、该节点也没被检测器包装**（G-3）。

**改造方向（一句话）**：把 DriverProfile 从「**这次命中了哪套 YAML**」升级为
「**这家公司这次的驱动假设 + 待验证研究议程**」——框架（可复用知识资产）与假设（标的特异、本次生成）
分离；规则负责提供**锚与守卫**，模型负责在稀疏区域**生成假设与研究议程**，确定性层负责**收敛与留痕**。

---

## 1. 现状实证（全部可复核，非推断）

### 1.1 覆盖率：历史 run 中专用框架命中率为 0

扫描 `data/task_records/**/*.json`（87 条记录 / 37 个标的）：

| 指标 | 数值 |
|---|---|
| task record 总数 | 87 |
| 提及 `generic_fundamental` 的记录 / 标的 | 13 / **9** |
| 提及 `hog_cycle` / `mining_services` | **0 / 0** |
| 提及 `driver_profile` 的记录 / 标的 | 17 / 11 |

涉及的 9 个标的横跨覆铜板（600183）、封测（600584）、电子材料（002130/002436）、
游戏互联网（300418）、医疗器械（300760）、FA 零部件（301029）、黄金矿业（601899）、
漆包线（600577）——**全部 fallback**。其中紫金矿业（601899）需要的是"**资源品生产商**"
（商品价格 × 产量 × 单位成本），与 `mining_services`（矿服工程、订单交付）语义不同，
现有 catalog 里没有任何框架覆盖它。

> 注：task record 不含 artifact 本体，上述为**报告文本中的显式自述**，是覆盖率的下界；
> 但它给出的结论很强：**专用框架在真实运行中从未命中过。**

### 1.2 内容层：artifact 里 90% 的知识没有任何消费者

`load_primitives()` → `build_driver_profile` 把 primitive 的 5 个内容字段拷进 `ActivatedPrimitive`
（`domain_context/driver_profile.py:56-68`）。但 `payload_builders._build_driver_profile_summary`
（`orchestrator/services/payload_builders.py:62-80`）只向下游透出 `id` / `priority_questions` / `report_angles`。

全仓库 Python 引用统计（`alphabee/**/*.py`）：

| 字段 | Python 引用 | 实际消费者 |
|---|---|---|
| `priority_questions` | schema + 契约 + payload_builders | ✅ `build_insight_context` |
| `report_angles` | 同上 | ✅ `build_insight_context` |
| `key_variables` | schema + 契约 + driver_profile（仅拷贝） | ❌ **无** |
| `disconfirming_signals` | schema + 契约 + driver_profile（仅拷贝） | ❌ **无** |
| `preferred_sources` | schema + 契约 + driver_profile（仅拷贝） | ❌ **无** |
| `causal_paths` | schema（仅声明） | ❌ **无** |
| `key_conflicts`（playbook） | schema（仅声明） | ❌ **无** |
| `recommended_verification_order`（playbook） | schema（仅声明） | ❌ **无** |
| `report_questions`（playbook） | schema（仅声明） | ❌ **无** |
| `when_to_activate` / `obsolescence_triggers` | schema（仅声明） | ❌ **无** |

**这是本设计最重要的发现**：DOMAIN_CONTEXT_ROADMAP 精心设计的"分析原语"知识层，
在当前接线里**基本是死代码**——`causal_paths`（因果链）、`disconfirming_signals`（证伪信号）、
`recommended_verification_order`（验证顺序）这些真正决定"研究怎么打"的字段，写进了 YAML、过了 schema 校验、
落进了 artifact，然后**没有任何一行代码读它**。

### 1.3 信息带宽：只用了 4 个字符串，丢掉了全部结构化信号

`resolve_driver_profile` 构造 `RouterInput` 时只取 4 个字符串：`track_label` / `industry` /
`sub_industry` / `business_model`（`nodes/resolve_driver_profile.py:58-64`）。

而该节点执行时 state 中**已可用**的东西（按 graph 顺序 `collect_raw_facts → resolve_industry_context
→ resolve_company_track → resolve_driver_profile` 确认）：

| 已有产物 | 关键字段 | 当前是否被路由使用 |
|---|---|---|
| `fact_values`（全量 canonical 数值事实） | 毛利率/研发费率/资产结构/存货占比… | ❌ |
| `COMPANY_TRACK.segments` | 分部营收、**占比**、同比、**分部毛利率** | ❌ |
| `COMPANY_TRACK` 其他 | `dominant_segment` / `fastest_segment` / `peer_group` / `review_notes`（漂移） | ❌ |
| `INDUSTRY_CONTEXT` | `peers_universe` / 三组数值基准 / `key_drivers` / `risk_factors` / `sw_code` | ❌（只用了 `industry`/`sub_industry`） |
| 磁盘 `reports/`（36 家公司已解析年报全文 + 章节树） | 「第三节 管理层讨论与分析 → 主要业务」 | ❌ |
| 工具 `query_financial_report` | 对已解析报告做章节级问答 | ❌ |
| 工具 `query_tushare` / `web_search` / `get_industry_fundamentals` | 结构化/定性外部证据 | ❌ |

分部结构恰恰是识别"真实驱动"的最强信号：工业富联按申万是"通信设备"，
但 `segments` 会直接显示"云计算/服务器"分部的占比与增速——这就是它真正的驱动。
现在这条信息**在同一 state 里躺着，路由不看**。

### 1.4 消费面：只有 1 个下游，所以它不是"切入点"

`grep driver_profile` 的全部代码落点：

| 文件 | 角色 |
|---|---|
| `nodes/resolve_driver_profile.py` | 生产者 |
| `domain_context/*` | 契约 + 组装 |
| `services/payload_builders.py:62` | `_build_driver_profile_summary` |
| `services/payload_builders.py:659` | **唯一调用点**：`build_insight_context` 的 `"driver_profile"` 键 |
| `agents/insights/prompts.py:29-34` | prompt 原则 9（唯一真正"读"它的地方） |
| `apps/cli/renderer.py:286` / `web/src/lib/constants.ts:14` | 渲染 |
| `orchestrator/node_contracts.py:179` / `services/deviation.py:46` | 契约与偏离分类 |

即：`driver_profile` → **只进 `synthesize_insights`**。
`explore_conflicts` / `verify_hypotheses`（**研究**节点）与 `run_thesis` / `generate_report`（**结论**节点）
完全不消费它。DOMAIN_CONTEXT_ROADMAP 的 P2 计划"改 explore_conflicts / verify_hypotheses prompt"
从未落地——**这就是"驱动画像本该是研究切入点、现在却不是"的直接原因**。

### 1.5 五个结构性缺陷

| # | 缺陷 | 证据 | 后果 |
|---|---|---|---|
| D-1 | **知识封闭**：框架只能来自 3 个 YAML | §1.1 | 覆盖率为 0，"越跑越需要人写 YAML" |
| D-2 | **信号浪费**：只消费 4 个字符串 | §1.3 | 真实驱动（分部结构）不可见 |
| D-3 | **驱动是常量**：`primary_drivers` 抄 playbook | §1.2 + YAML | 同框架下所有公司主线相同，与 roadmap"同行业不同公司主线不同"目标矛盾 |
| D-4 | **无证据无置信**：契约里没有 evidence/confidence | `domain_context/contracts.py:29-58` | 无法回答"凭什么认为驱动是这几个变量"，不可审计不可反驳 |
| D-5 | **无下游**：只进 insight | §1.4 | 不改变"研究什么"，只改变"最后怎么措辞" |

**D-5 是最致命的**：一个不改变研究过程的"研究切入点"，无论内容多丰富都不会提升分析质量。

### 1.6 接线层的额外缺口（补充核实，全部有代码位置）

| # | 事实 | 位置 | 影响 |
|---|---|---|---|
| G-1 | **生产环境 `INDUSTRY_CONTEXT.sub_industry` 恒为空串**（唯一生产者硬编码 `sub_industry=""`） | `nodes/resolve_industry_context.py:153,168-175` | 权重 2 的 `sub_industry_match` **实际只能由申万 L1 `industry` 触发**；`mining_services.match_sub_industries=["采掘服务"]`（申万**二级**名）永远不可能命中 |
| G-2 | **golden 夹具不保真**：`tests/orchestrator/test_resolve_driver_profile.py:82` 与 `tests/domain_context/test_context_router.py:36` 都显式注入了生产不可能出现的 `sub_industry="采掘服务"` | 同上 | 金诚信在真机形状下**仅靠 `track_label_match`** 命中；该 golden 通过并不代表生产可用 |
| G-3 | **`artifact_schema_valid` 对 `DRIVER_PROFILE` 是空操作**：`_ARTIFACT_MODELS` 未登记该类型（也未登记 `INDUSTRY_CONTEXT`/`COMPANY_TRACK`），检测器遇 `None` 直接 `continue`；且该节点在图中**未被 `with_deviation_detection` 包装** | `orchestrator/detectors.py:141-152,266-267`；`agent.py:472` | 契约声明的 `postconditions[0]`「DriverProfile schema 合法」**在运行时没有任何执行路径** |
| G-4 | **`build_fallback_insight`（Tier 2）完全不读 `context["driver_profile"]`**：它读 6 个 key，不含 driver_profile | `agents/insights/rescue.py:431-522` | 一旦 insight 严格解析失败（最需要确定性主线一致性的场景），驱动画像被**静默绕过**，且不产任何 issue |
| G-5 | **report 层零消费**：`ReportGenerationPayload` 无 `driver_profile` 字段 | `orchestrator/contracts.py:371-382`；`payload_builders.py:291-502` | 报告感知框架的唯一通路是 `payload.insight` 里 LLM 写的字段——**LLM 不照原则 9 写，"hog_cycle/猪价"在报告里零痕迹且无断言能发现** |
| G-6 | **prompt 与 payload 字段矛盾**：prompt 指示 LLM 可从"`activated_primitives` 的关键变量"取 `main_driver`，但 payload 根本不给 `key_variables` | `agents/insights/prompts.py:31` vs `payload_builders.py:73-80` | 该分支对 LLM 不可用（§1.2 的具体后果） |
| G-7 | **playbook 级知识字段连 artifact 都没进**：`key_conflicts` / `recommended_verification_order` / `report_questions` 既不在 `RouterResult` 也不在 `DriverProfile`；primitive 的 `causal_paths` / `when_to_activate` 在快照展开时被丢弃 | `context_router.py:76-88`；`contracts.py:29-58`；`driver_profile.py:56-68` | 所以 §9"消费 `recommended_verification_order`"**必须先把它加进契约**，不是改下游就能读到的 |
| G-8 | **`coerce_driver_profile` 是零调用方死代码**；`why_selected` / `playbook_version` / `score` / `trend` 只产不消 | `orchestrator/contracts.py:479-485`；全仓 grep | 可解释性字段写了没人看 |
| G-9 | **`NODE_CONTRACTS` 有 SHA 指纹测试锚**：`test_f4_does_not_touch_node_contracts` 对 `sorted(NODE_CONTRACTS)` 做键集 + 指纹（`1857784a5cb634e3`）机读锚 | `tests/tracking/test_scheduler.py:858-885` | **改 driver_profile 的节点契约会直接打破该测试**，须同期更新指纹（§11-D2 已列入） |
| G-10 | 契约文本与实装不符：`preconditions[0]` 说"fact_values 已提供公司标识"（实读 `run.context["symbol"]`）；`preconditions[1]` 说"资产不可用走降级"（`load_primitives()` 异常**无 try/except**，会抛穿 LangGraph 而非降级） | `node_contracts.py:181`；`resolve_driver_profile.py:34-35,70-74` | 契约语义漂移，测试只断言"条件字符串非空"，发现不了 |

> G-1/G-2 直接证明：**现有 golden 的"通过"是弱证据**。§6 的 `anchor_strength` 设计因此
> **不能依赖 `sub_industry`**，必须改用 `sw_code` + `track_label`（见 §6 修正）。
> G-9 说明 D2 期不是"纯加代码"，而是**必须同期更新既有测试锚**。

---

## 2. 根因

把两件本质不同的事**混在同一个对象里**，并且只实现了其中一件：

| 概念 | 回答的问题 | 变化频率 | 来源 | 现状 |
|---|---|---|---|---|
| **Framework（框架）** | 这类生意"通常"看什么 | 慢（年） | YAML 知识资产（primitive×playbook） | ✅ 已实现（但只有 3 个、且内容 dead-end） |
| **Hypothesis（假设）** | **这家公司这次**由什么驱动、当前争议在哪、要什么证据 | 每次 run | **研究**（年报 + 分部 + 行业 + 联网） | ❌ **完全缺失** |

现有实现把第二件事当成第一件事的"查表结果"：`primary_drivers = playbook.primary_drivers`（常量拷贝）。
所以"发挥模型的研究探索能力"不是给现有 router 加个 LLM 复核就够的——
**要新增的是假设层本身**，而 LLM 恰好在稀疏区域（没有 YAML 覆盖的绝大部分标的）
是唯一可行的假设生成器。

同时必须承认相反的约束（这是本仓库的既定架构原则，不能推翻）：
*候选方案 A：让 LLM 直接输出 playbook id 并取代规则路由* —— **否决**。
理由：路由结果会不可回放、不可单测、成本每次 run 都要付、且与 DEVIATION_CONTROL_FRAMEWORK
的确定性检测器/恢复阶梯冲突。DOMAIN_CONTEXT_ROADMAP §L880 已定调"LLM 只做可选复核，不进路由主线"。

---

## 3. 设计目标与非目标

### 3.1 目标

1. **任意 A 股标的都拿到有内容、有证据、标的特异的驱动画像**——`primary_drivers` 不再为空，
   且不再等于行业常量。
2. **驱动画像成为真正的研究切入点**：其产物直接决定 `explore_conflicts` 生成什么假设、
   `verify_hypotheses` 按什么顺序验证、`generate_report` 写什么主线。
3. **能回答"凭什么"**：每条驱动假设都带可点击回源的证据与可观测指标；无证据的主张不得进入主线。
4. **越跑越准**：run 中发现的、YAML 无法覆盖的新驱动可沉淀为新原语，反向提高 Tier A 命中率。
5. **不破坏确定性内核**：规则路径保持确定性可单测；LLM 路径可开关、有预算、有缓存、有降级链；
   既有 golden（牧原 = `hog_cycle`、金诚信 = `mining_services`）行为不变。

### 3.2 非目标

- ❌ 不用 LLM 取代规则路由主线（见 §2 否决理由）。
- ❌ 不让 LLM 直接生成并写库 YAML（`novel_drivers` 只落 artifact，沉淀走离线人工 review）。
- ❌ 不新建 `ArtifactType`（`DRIVER_PROFILE` 契约向后兼容扩展即可；遵守 orchestrator artifact 契约：
  不往 `OrchestratorState` 加专用字段）。
- ❌ 不做"驱动变量越多越好"——设条数上限与 role 结构约束，避免 prompt 膨胀与噪声。
- ❌ 不顺带重做 `narrative_transition` / EventOverlay（roadmap P1/P2 的独立议题，本设计只预留 `candidates` 字段）。

---

## 4. 总体架构：三级瀑布（先验锚定 → 模型研究 → 确定性收敛）

```text
┌─ Tier A  先验锚定（确定性 · 每次必跑 · 零 LLM 成本）────────────────────────┐
│ 输入：身份信号 + 结构化信号（新增，见 §6）                                  │
│   · segments 分部结构摘要（主力分部/占比/同比/分部毛利率）                   │
│   · sw_code 精确前缀（消灭「农林牧渔 → hog_cycle」这类宽 L1 误配）           │
│   · fact_values 结构摘要（毛利率/研发费率/存货占比，声明式阈值匹配）         │
│ 动作：增强版 _score_playbook → 产出 anchored_playbooks + anchor_strength     │
│ 路由：strong → 直接采用，**跳过 Tier B**（保护既有 golden + 省成本）          │
│       weak / none → 进 Tier B                                              │
└───────────────────────────────────────────────────────────────────────────┘
                              ↓ weak / none
┌─ Tier B  模型研究（LLM · 按需触发 · 有预算有缓存）──────────────────────────┐
│ 输入包（§7.2）：结构事实快照 + 本地年报 MD&A 片段 + 行业基准 + 已锚定原语     │
│ 工具（§7.3）：query_financial_report / query_tushare / web_search /          │
│                get_industry_fundamentals / get_stock_news_summary           │
│ 任务（§7.4）：① 写这家公司的盈利公式（钱从哪来）                             │
│               ② 提 3~7 条驱动假设（变量/传导链/可观测/证伪/证据）             │
│               ③ 生成研究议程（最不确定且最能改变结论的问题，带定论证据）      │
│ 产出：driver_hypotheses + research_agenda + candidates + novel_drivers        │
└───────────────────────────────────────────────────────────────────────────┘
                              ↓ 原始输出
┌─ Tier C  确定性收敛（守卫 · 非生成 · 可单测）──────────────────────────────┐
│ ① schema 校验（Pydantic）                                                   │
│ ② 证据可溯：每条假设 ≥1 条 evidence，且 ref 必须来自本轮真实工具返回          │
│    → 无证据者降级进 unverified_drivers，**不得进 main_driver**               │
│ ③ 事实交叉校验：假设的 observable 与 segments/fact_values 是否自洽            │
│    → 矛盾则降权 + 显式 issue（不静默）                                       │
│ ④ 原语对齐：能对齐 → 挂 matched_primitive（复用 YAML 因果链）；对不上 → novel │
│ ⑤ 派生 primary/secondary_drivers（保证老消费方零改动即受益）                 │
│ → 落 DRIVER_PROFILE artifact（v2）                                          │
└───────────────────────────────────────────────────────────────────────────┘
```

**设计要点**：
- **规则提供锚与守卫，模型负责生成，确定性层负责收敛**——三者职责不重叠。
- Tier A 的 `strong` 门槛**必须提高**（现状：`business_model` 单独命中 1 分即中；宽 L1 行业也中）。
  新门槛建议：`track_label` 命中，**或** `sub_industry`（L2/L3）命中；且分部结构与框架不矛盾。
- Tier B **不是"复核"，是"稀疏区域的生成主力"**——这是与既有限调的实质差别：
  复核只做"对不对"，生成做"是什么"。

---

## 5. 数据契约：DriverProfile v2（纯增量，向后兼容）

`domain_context/contracts.py` 扩展。**不改既有字段语义**，老消费方（`payload_builders` / renderer /
InsightAgent prompt 原则 9 / deviation）零改动即受益。

**同时必须补齐两处"知识字段丢失"**（G-7，否则 §9 的下游接线读不到东西）：
① `ActivatedPrimitive` 增 `causal_paths` / `when_to_activate`（YAML 6/6 有内容，但现在在快照展开时被丢弃）；
② `DriverProfile` 增 playbook 级 `key_conflicts` / `recommended_verification_order` / `report_questions`
（现在**连 `RouterResult` 都没带**，`_build_result` 也不透传）。

```python
class ActivatedPrimitive(BaseModel):
    # ── 既有字段不变 ──────────────────────────────────────────
    id: str
    score: float = 1.0
    trend: str = "stable"
    description: str = ""
    key_variables: list[str] = []
    priority_questions: list[str] = []
    disconfirming_signals: list[str] = []
    preferred_sources: list[str] = []
    report_angles: list[str] = []
    # ── v2 新增：停止丢弃 YAML 已有内容（G-7）────────────────
    causal_paths: list[str] = []        # 因果链——框架的推理骨架
    when_to_activate: list[str] = []    # 激活条件（供人工复核与沉淀）

class DriverObservable(BaseModel):
    """一条驱动假设的可观测抓手（决定"后续怎么跟踪/验证"）。"""
    name: str                  # "高频高速 CCL 分部毛利率"
    source: str = ""           # "annual_report:第三节" / "tushare:income" / "segment:20251231"
    cadence: str = ""          # 月度 / 季度 / 事件驱动

class DriverEvidence(BaseModel):
    """证据（可回源，禁止无来源主张）。"""
    kind: str = ""             # annual_report / segment / tushare / industry_benchmark / web
    ref: str = ""              # 精确定位（章节名 / api+params / URL / 字段名）
    quote: str = ""            # 原文片段（限长，供人工复核）

class DriverHypothesis(BaseModel):
    """标的特异的驱动假设。这是 v2 的核心新增。"""
    variable: str                       # 驱动变量名（**公司特异**，不是行业常量）
    role: str = "primary"               # primary / secondary / risk
    mechanism: str = ""                 # 传导链：变量 → 中间环节 → 盈利结果
    company_form: str = ""              # 本公司特有形态（带数值/结构）
    observables: list[DriverObservable] = []
    falsifiers: list[str] = []          # 什么情况这个变量不再主导
    evidence: list[DriverEvidence] = []
    confidence: float = 0.0
    matched_primitive: str = ""         # 对齐到的 YAML 原语 id（"" = novel）

class ResearchQuestion(BaseModel):
    """研究议程条目——直接喂给 explore_conflicts / verify_hypotheses。"""
    question: str
    why_matters: str = ""
    decisive_evidence: list[str] = []   # 什么证据能定论（可执行）
    preferred_sources: list[str] = []
    priority: str = "high"              # critical / high / medium
    status: str = "open"                # open / answered（下游回写）

class DriverProfile(BaseModel):
    # ── 既有字段保持原义（不改、不删）──────────────────────────
    schema_version: str = "2"           # 1 → 2
    symbol: str = ""
    generated_at: str = ""
    playbook: str = ""
    playbook_version: int = 1
    activated_primitives: list[ActivatedPrimitive] = []
    primary_drivers: list[str] = []     # v2：由 driver_hypotheses 派生（role=primary）
    secondary_drivers: list[str] = []   # v2：由 driver_hypotheses 派生（role=secondary）
    why_selected: list[str] = []
    fallback: bool = False
    degraded: bool = False
    degraded_reason: str = ""

    # ── v2 新增（全部有默认值）────────────────────────────────
    provenance: str = "rule"            # rule / llm / hybrid
    anchor_strength: str = ""           # strong / weak / none（Tier A 判定结果）
    # playbook 级知识（G-7：现在连 artifact 都没进，下游无从消费）
    key_conflicts: list[str] = []                 # 框架下最常见的多空分歧模板
    recommended_verification_order: list[str] = []  # 验证优先级（供 verify_hypotheses）
    report_questions: list[str] = []              # 报告应围绕的问题（供 report 层）
    driver_hypotheses: list[DriverHypothesis] = []
    research_agenda: list[ResearchQuestion] = []
    novel_drivers: list[str] = []       # 无法对齐任何 YAML 原语的新驱动（沉淀来源）
    candidates: list[str] = []          # 备选 playbook（多主线/过渡态，供框架竞争）
    unverified_drivers: list[str] = []  # 无证据支撑，**禁止进 main_driver**
    research_confidence: float = 0.0
    research_meta: dict = {}            # 触发原因 / 工具调用数 / 耗时 / 缓存命中 / 降级原因
```

**兼容性论证**：
- `DriverProfile` 是普通 `BaseModel`（无 `extra="forbid"`），新增可选字段不影响旧 artifact 反序列化；
  `coerce_driver_profile`（`orchestrator/contracts.py:479`）用 `model_validate`，旧 dict 直接通过。
- `ActivatedPrimitive` **只增不减**：既有 9 个字段名与语义全部不变，新增 2 个带默认值的可选字段
  （§5 ③），故旧 artifact 仍可 `model_validate`，且新增字段在下游被逐步消费（§9）。
- `test_driver_profile.py::test_coerce_driver_profile_roundtrip` 断言 roundtrip，增量字段不影响。

---

## 6. Tier A 增强（确定性，可单测）

`context_router.py` 的输入与打分扩展：

1. **`RouterInput` 扩容**（全部来自已落地产物，不重复取数）：
   ```python
   sw_code: str = ""                       # INDUSTRY_CONTEXT.sw_code → 一级前缀精确匹配
   dominant_segment: str = ""              # COMPANY_TRACK.dominant_segment
   dominant_share: float | None = None     # 主力分部占比
   segment_summary: list[str] = []         # ["云计算/服务器 42%(+58%)", …] 供 match_segments 子串
   financial_structure: dict[str, float] = {}  # 从 fact_values 取的结构指标（毛利率/研发费率/存货占比）
   ```
2. **`PlaybookSchema` 增声明式匹配字段**（先改 schema 再写 YAML；注意 `extra="forbid"`）：
   `match_sw_codes` / `match_segments` / `match_financial_structures`（阈值字典，如
   `{"inventory_ratio": {"gt": 0.25}}`）。
3. **`anchor_strength` 三档判定**（取代现在的"命中/未命中"二元）。
   ⚠️ **修正**：判据**不得依赖 `sub_industry`**——生产环境该字段恒为空串（G-1），
   现权重 2 实际只能由申万 L1 `industry` 触发，而宽 L1 正是误配来源（"农林牧渔 → hog_cycle"）。

   | 档位 | 条件 | 路由行为 |
   |---|---|---|
   | `strong` | `track_label` 命中（权重 3），**或** `sw_code` 精确前缀命中（L2/L3），**且**分部结构不矛盾 | 采用，跳过 Tier B |
   | `weak` | 仅靠 `business_model` 命中，或**仅靠宽 L1 `industry`** 命中（正是当前 `农林牧渔` 误配路径） | 采用作为先验**锚**，**但仍进 Tier B** 复核 |
   | `none` | 无命中 | 进 Tier B（现状 fallback 路径） |

   配套：`sw_code` 前缀匹配（申万代码本身可区分 L1/L2/L3），以取代"宽 L1 子串"这一不可靠判据。
4. **`generic_fundamental` 补驱动**（D0 立即消灭「驱动: —」，且**不是编造**）：
   兜底驱动改为**从本公司结构化事实确定性推导**的标的特异结构变量，每条自带 source：
   - `"主力分部 <segment_name> 收入占比 <x>%"`（source=`segment:<report_date>/<segment_name>`）
   - `"<fastest_segment> 同比 <y>%（快于主力）"`（若有）
   - `"毛利率 <x>%（行业基准 <y>%，差 <z>pp）"`（当 `peer_/industry_` 基准可得）
   这条很关键：它让"兜底"变成**有信息量、可回源的结构性驱动**，
   而不是把通用财务题眼（收入增速/毛利率/ROE）堆成另一个行业模板。

> Tier A 全部增强都是纯函数 + 数据，保持"确定性内核可单测"（现有
> `tests/domain_context/` 26 例为基线，扩展而非改写）。

---

## 7. Tier B：模型研究层（本设计的核心）

### 7.1 触发条件（成本与收益的平衡点）

```python
def should_research(profile_input, anchor) -> tuple[bool, str]:
    # ① 稀疏区域（线上绝大多数标的）：无专用框架但有身份信号
    if anchor.strength == "none" and identity_signals_present: return True, "no_anchor"
    # ② 弱锚：需复核（business_model 单命中 / 宽 L1 行业命中）
    if anchor.strength == "weak":                             return True, "weak_anchor"
    # ③ 多主线公司：主力分部占比低 → 单一框架不足以刻画（比亚迪式）
    if dominant_share is not None and dominant_share < 0.50:   return True, "multi_segment"
    # ④ 身份漂移：COMPANY_TRACK.review_notes / detect_track_drift 有信号
    if track_drift_detected:                                  return True, "identity_drift"
    return False, ""
```
外加**缓存命中即跳过**（§7.6）与**开关门控**（§14 待拍板 1）。

### 7.2 输入包（组装成注入 prompt 的上下文，而非让模型自己去翻）

| 块 | 来源 | 作用 |
|---|---|---|
| 公司身份 | `track_label` / `sw_industry` / `business_model` / `sw_code` | 定位 |
| **分部结构** | `COMPANY_TRACK.segments`（多期，含占比/同比/分部毛利率） | 识别真实驱动（最关键的一块） |
| 财务结构 | `fact_values` 关键项 + `peer_*` / 行业基准 | 交叉校验 |
| 已锚定原语 | Tier A `weak` 命中时的 `key_variables` / `causal_paths` / `disconfirming_signals` | 先验知识（**首次真正被消费**） |
| 漂移笔记 | `COMPANY_TRACK.review_notes` | 过渡态线索 |
| 年报片段 | `company_track.peer_report.fetch_local_report_fragments(symbol)`（MD&A，上限 12000 字符） | 一手材料（已有能力，直接复用） |

### 7.3 工具与 middleware（复用既有资产，不新建取数逻辑）

Tool set（全部已存在）：

| 工具 | 位置 | 用途 | 约束 |
|---|---|---|---|
| `query_financial_report` | `tools/financial_report.py:451` | **一手**年报章节级问答（`reports/` 36 家已解析全文） | 首选工具，prompt 强制第一步用它 |
| `query_tushare` | `tools/tushare_query.py` | 结构化行情/财务/行业数据 | 数字唯一权威来源 |
| `get_industry_fundamentals` | `tools/industry_fundamentals.py` | 行业景气/估值/成分 | 同上 |
| `web_search` | `tools/common.py:191` | 行业变量的**定性**当前状态与争议点 | 挂 `web_search_guard`，见下 |
| `get_stock_news_summary` | `tools/news.py:7` | 近期事件（akshare，最近 100 条标题） | ⚠️ 当前**未注册到任何 agent**；若要用需显式挂进 `tools=` |

Agent 构造（照抄 `agents/verify_hypotheses/agent.py:19-59`，它是仓库里唯一形态完整的取证 agent）：

```python
def driver_profile_research_agent_factory() -> CompiledStateGraph[Any, Any, Any, Any]:
    from alphabee.tools.financial_report import query_financial_report   # 重依赖：延迟导入
    backend = FilesystemBackend(root_dir=str(PROJECT_ROOT), virtual_mode=True)
    system_prompt = DRIVER_PROFILE_RESEARCH_PROMPT + "\n\n" + json_instruction(DriverResearchOutput)  # 必须自己拼
    return create_deep_agent(
        model=create_chat_model("agent.driver_profile"),
        system_prompt=system_prompt,
        tools=[query_financial_report, query_tushare, web_search, get_industry_fundamentals],
        middleware=[
            check_message_limit,      # ⚠️ ≥50 条消息**直接终止**（见 §7.7-2）
            web_search_guard,         # ⚠️ 见 §7.7-3
            ToolRetryMiddleware(),
            # ★ 必须：create_deep_agent 的 tools= 是**增量**的，不排除就等于给了 fs + shell(execute)
            _ToolExclusionMiddleware(excluded=frozenset(
                {"ls", "glob", "grep", "read_file", "write_file", "edit_file", "execute"})),
        ],
        backend=backend,
        skills=["alphabee/skills/tushare", "alphabee/skills/eastmoney"],
        # 不设 response_format=（端点不支持 json_schema，走 Provider/ToolStrategy 会失败）
    )
```

Middleware 三件套与两条硬约束：`check_message_limit` / `web_search_guard` / `ToolRetryMiddleware`
（§7.7 给出与预算的冲突及处置）。

### 7.4 任务分解与 prompt 骨架（"发挥研究探索能力"的落点）

工厂：`alphabee/agents/driver_profile/agent.py::driver_profile_research_agent_factory()`，
沿用仓库范式（`create_deep_agent` + `FilesystemBackend(PROJECT_ROOT, virtual_mode=True)` +
`json_instruction(DriverResearchOutput)` + 渲染见 `agents/explore_conflicts/agent.py`）。

prompt 三段式（骨架，非最终文案）：

```text
你是 AlphaBee 的驱动画像研究代理。你的任务不是给公司做行业分类，
而是回答一个问题：**这家公司的利润由什么变量驱动，当前最不确定的是什么。**

## 证据纪律（硬约束，违反即视为失败）
1. 先读一手材料：调用 query_financial_report 读该公司最近一期年报/半年报的
   「管理层讨论与分析 → 报告期内公司从事的主要业务」，**用你自己的话**写出这家公司的盈利公式
   （收入 = 什么 × 什么；利润 = 收入 − 什么）。必须给出章节与原文片段。
2. 再看结构化事实：分部收入结构（占比/同比/分部毛利率）、关键财务指标、行业基准。
   分部结构与盈利公式**必须自洽**。若公司自称"AI 服务器龙头"而该分部收入占比仅 8%，
   这是一个必须写进研究议程的矛盾，不是可以忽略的噪声。
3. 只在需要确认"行业变量当前状态与争议点"时才联网；**搜索结果的数字一律不得当财务数据使用**
   （数字只能来自 tushare / 年报工具）。
4. 你无法验证的猜测可以提出，但必须进 open_questions，不得伪装成结论。

## 驱动假设的要求：必须标的特异
❌ 不合格：「驱动：毛利率」——任何公司都成立，等于没做研究。
✅ 合格：「驱动：高频高速 CCL 收入占比 × 铜箔-树脂价差」——
   给出公司自身的分部结构数值、与行业基准的差、以及传导到利润的链条。
每条假设必须能给：变量 / 传导链 / 本公司特有形态（带数值）/ 可观测指标+数据源 /
证伪信号（什么情况它不再主导）/ 支撑证据（可回源）。

## 研究报告的落点：research_agenda
把"**最不确定、但一旦定论就会改变结论**"的问题排成清单，每条要写清
「需要什么证据才能定论」与「去哪拿」。它是下游冲突探索与假设验证的输入，
不是给人看的摘要——写得没法执行的议程等于没写。

## 输出：json（字段见 json schema 指令）
{... driver_hypotheses / research_agenda / candidates / novel_drivers / open_questions ...}
```

### 7.5 结构化输出与降级链（受端点能力限制）

已核实约束（`utils/llm.py:236-248` 注释 + 冒烟结论）：
`api.deepseek.com` 端点 **不支持 `json_schema`，也不支持强制 `tool_choice` 的 function calling**。

因此沿用仓库既有链，**不用 `with_structured_output`**，也**不能用 `response_format`**
（deepagents 站点会走 ProviderStrategy/ToolStrategy，本端点不可用——`utils/llm.py:235-248` 已定论）：

```
create_deep_agent(model=create_chat_model("agent.driver_profile"),
                  system_prompt=PROMPT + json_instruction(DriverResearchOutput))  # 含 "json" 字样
  → _extract_final_text(result)
  → parse_json（utils/pipeline.py:194，五层递进：栅栏 → 原文 → 首尾括号 → 平衡括号匹配 → json_repair）
  → DriverResearchOutput.model_validate
  → 失败 → 走下面的降级阶梯
```

**降级阶梯照抄 `synthesize_insights`（`nodes/insights.py:209-250`），不要照抄 `explore_conflicts`**：

| 档 | 动作 | 是否需要 LLM |
|---|---|---|
| Tier 0 | `parse_json` + `model_validate` 成功 | — |
| Tier 1 | 宽松救援（只修结构、不补内容）——本节点简化为"丢弃非法单条假设、保留合法条目" | 否（确定性） |
| Tier 2 | **Tier A 结果 + D0 结构性兜底驱动**（保留锚，`provenance="rule"`） | 否（确定性） |
| Tier 3 | Tier A 有锚但无结构化事实 → 最小骨架（playbook + 空驱动 + `degraded_reason`） | 否 |

选择理由（§1.4 与调研结论）：`explore_conflicts` 的"agent 异常 → 直接返回、不产 artifact"
（`nodes/conflicts.py:79-104`）是本仓库最脆弱的一环——LLM 超时会让结构化 artifact **整体消失**，
下游全部降级为"无冲突"。本节点必须遵守既有"**DRIVER_PROFILE 恒产出**"契约，故采用 insights 阶梯。

降级元数据**只走唯一写入点** `apply_degradation(base, tier, reason, threshold=1)`
（`orchestrator/services/degradation.py:93-141`，同时写 `degraded` + `degradation_reason`，
防字段名漂移），节点另补 `fallback_tier`。Tier≥1 时产 issue
（`category="driver_profile_research_degraded"`，MEDIUM，进偏离账本）。

⚠️ **两个已核实的坑**：
1. **无 LLM 级重试机制**（`ToolRetryMiddleware` 只管工具）。若要做"附解析错误 re-ask 一次"，
   需在本节点内自行实现，且必须计入预算（§7.6）。
2. **`extract_text` 会把 `{"type":"thinking"}` 块拼进正文**（`utils/pipeline.py:43-44`）。
   推理模型下思考文本会污染 `raw_text`、直接降低 `parse_json` 成功率。
   → 建议本节点自带只取 `text` 块的提取函数（不要复用 `_extract_final_text` 的宽口径）。

### 7.6 预算、缓存与并发

现状：**全仓库没有 run 级 token/工具调用预算**（`config.yaml` 无相关段，节点无 recursion limit）。
新增是必须的：

```yaml
domain_context:
  research:
    enabled: false            # 见 §14 待拍板 1（建议先 flag 门控）
    max_hypotheses: 7         # 驱动假设条数上限
    max_agenda_items: 6
    max_tool_calls: 20        # 硬上界 → 超限强制收口
    wall_timeout_s: 180
    cache_ttl_days: 90
    cache_dir: "data/driver_profiles"
```

- **缓存**：key = `symbol + 最新报告期 + track_label + playbook_version`；
  命中 → 跳过 Tier B 直接复用（`research_meta.cached=true`）。
  与既有 `PeerGroupStore` / `data/peer_eval_cache/` 同构，有先例可抄。
  失效条件：新报告期 / track_label 漂移 / playbook 版本升级。
- **并发**：单 run 单标的，无需 fan-out；但批量回放（eval）时按 symbol 并发，
  需自行加 `asyncio.Semaphore`（仓库现有 fan-out **无并发上限**，`verification.py:153-158` 是先例与警示）。
- **超时/超限**：`wall_timeout_s` 到期或 `max_tool_calls` 超限 → 用**已有工具返回**收口，
  `research_meta["truncated"]=true` + issue（LOW）。
- **必须显式设 `recursion_limit`**：现有研究节点**全部未设**（`agent.ainvoke(..., config=config)`
  透传 CLI 的 `{"callbacks": [...]}`），走 LangGraph 默认值。既有先例是
  `tools/financial_report.py:505` 用 `recursion_limit=40`——本节点照此显式设置并进配置。

### 7.7 工程约束与坑（本轮核实，逐条影响实现）

| # | 约束 | 证据 | 处置 |
|---|---|---|---|
| 1 | **`create_deep_agent` 的 `tools=` 是增量的**，内建工具含 `execute`(shell) + fs 读写。不排除就等于把 shell 交给 agent | `.venv/deepagents/graph.py:329`（"never removes a built-in"）；`agents/verify_hypotheses/agent.py:38-40` 是唯一显式排除者 | **必须挂 `_ToolExclusionMiddleware`**（见 §7.3 代码块）；`explore_conflicts` / `insights` 目前都处于"持有 shell"状态，本节点不重蹈 |
| 2 | **`check_message_limit` 是"≥50 条消息直接终止"，不是截断**（阈值 50 硬编码） | `middleware/common.py:8-14` | ⚠️ 与 `max_tool_calls=20` **正面冲突**：每轮工具调用产生 `AIMessage(tool_calls)` + `ToolMessage` ≈ 2 条，20 次调用 ≈ 40+ 条，逼近阈值后 agent 被强制终止 → **工具预算实际由 `check_message_limit` 决定，配置里的 `max_tool_calls` 会变成装饰**。处置：把该中间件阈值**做成可配置**（默认仍 50 保持既有行为），并保证 `2 × max_tool_calls + 余量 < 阈值`；或把 `max_tool_calls` 下调到 ≤ 18 |
| 3 | **`web_search_guard` 的 `pe\|pb\|ps` 是子串匹配**，且 pre-call 命中即短路 | `middleware/web_search_guard.py:33-38`（`re.I` + `re.search`） | ⚠️ 会**误拦合法驱动变量查询**：`capex`（含 `pe`）、`pipeline`（含 `pe`）、任何含 `roe/eps/pb` 子串的英文查询。而"资本开支周期""管线进展"恰是核心驱动变量 → 见 §14 待拍板 2，建议改为**词边界匹配** + 窄白名单 |
| 4 | **guard 拦截后推荐的替代工具没接线**：`get_market_data` / `get_fundamentals` / `get_industry_fundamentals` **未注册到任何 agent** | 全仓仅 middleware 字符串常量 + `workflow/framework_monitor.py:271-273` 直接函数调用 | 本节点应把 `get_industry_fundamentals` 真正挂进 `tools=`（§7.3 已列），否则拦截文案在误导模型 |
| 5 | **无 LLM 级重试、无节点级超时**（`asyncio.wait_for` 在 orchestrator+agents 零命中） | 全仓 grep | Tier B 需自行实现"重试一次"与 `asyncio.wait_for(wall_timeout_s)`，并计入预算 |
| 6 | **`extract_text` 会把 `{"type":"thinking"}` 块拼进正文** | `utils/pipeline.py:43-44` | 本节点自带只取 `text` 块的提取函数（§7.5） |
| 7 | `_finalize_step` 状态语义：`issues and not artifacts`→FAILED；`issues`→PARTIAL；否则 SUCCEEDED | `orchestrator/collectors.py:117-125` | 因本节点恒产 artifact，研究失败只会落 PARTIAL——**验收 8 需按 issue 而非 step 状态断言** |
| 8 | `json_instruction` **不会被工厂自动拼**（`insights` 就没拼；`verify_hypotheses` 拼了两次） | `agents/insights/agent.py:15-30`；`verify_hypotheses/agent.py:27` + `verification.py:76` | 自己拼**一次**（§7.3），并同步维护 `DriverResearchOutput` 的 `json_schema_extra.example` |

**测试范式**（仓库既有三式，`tests/conftest.py` **无 LLM fixture**，集成测试走 `@pytest.mark.integration`）：
- 范式 A：`FakeAgent.ainvoke` 返回 `{"messages":[obj(content=json)]}` + monkeypatch **agent 工厂**。
  ⚠️ 节点内若**延迟 import** 工厂，必须 patch **源模块**属性
  （`__import__("alphabee.agents.driver_profile.agent", fromlist=[...])`），
  参照 `tests/orchestrator/test_conflict_lifecycle.py:107-136`。
- 范式 B：patch 节点内部 seam（顶层 import 的符号 → 直接 patch 节点模块），
  参照 `test_conflict_lifecycle.py:82-101`。
- 范式 C：降级阶梯函数单独测（Tier 0/1/2/3 逐档），参照 `tests/orchestrator/test_insight_degradation.py:42-61`。
- 开关类断言"严格 no-op"（不产 artifact、行为逐字段不变），参照
  `tests/orchestrator/test_assumption_registry.py:50,53`（`monkeypatch.setattr(detection, "detection_switches", ...)`）。

---

## 8. Tier C：确定性收敛（守卫，不生成）

| 步骤 | 规则 | 失败行为 |
|---|---|---|
| C1 schema 校验 | Pydantic `DriverResearchOutput` → `DriverHypothesis` | 单条丢弃；全丢 → 走 §7.5 降级链 |
| C2 证据可溯 | 每条假设 ≥1 条 `DriverEvidence` 且 `ref` 非空 | 移入 `unverified_drivers`，**不得进 main_driver** |
| C3 事实交叉校验 | 假设的 `observables` 与 `segments`/`fact_values` 无明显矛盾（如声称分部占比与实测差异 > 阈值） | 该假设降权（conf ≤0.3）+ issue（MEDIUM，显式留痕） |
| C4 原语对齐 | `variable` 与 primitive `key_variables` 语义对齐 → 填 `matched_primitive`，继承其 `causal_paths` / `disconfirming_signals` | 对不上 → `novel_drivers`（不是丢弃） |
| C5 结构约束 | `role` 分布合理（≥1 primary；primary ≤3）；条数 ≤ `max_hypotheses` | 超限按 confidence 截断 |
| C6 派生 | `primary_drivers` / `secondary_drivers` ← `driver_hypotheses`（老消费方零改动受益） | — |

新增检测器（DEVIATION_CONTROL_FRAMEWORK §14.2 风格）：
`driver_profile_evidence_present`（要求 `fallback=false 或 provenance!=rule` 时
`driver_hypotheses` 非空且证据可溯），并把 `resolve_driver_profile` 的
`NodeContract.detectors` 从 `["artifact_schema_valid"]` 扩为该两项、`postconditions` 补"驱动非空且可溯"。

---

## 9. 下游接线（价值兑现——没有这一节，前面全是 dead-end artifact）

| 下游 | 现状 | 改造 | 语义 |
|---|---|---|---|
| `payload_builders._build_driver_profile_summary` | 只传 `id`/`priority_questions`/`report_angles` | **补全** `key_variables` / `causal_paths` / `disconfirming_signals` / `preferred_sources` + 新增 `driver_hypotheses` / `research_agenda` / `unverified_drivers` / `provenance` | 修掉 §1.2 的 dead-end，并消除 G-6（prompt 要 `key_variables`、payload 不给的硬矛盾） |
| `agents/insights/rescue.py::build_fallback_insight`（Tier 2） | **读 6 个 key，不含 driver_profile**（G-4） | 读 `driver_profile`：`main_driver` 优先取 `primary_drivers`，`materiality_rank` 用 `driver_hypotheses` | 让**降级路径**也保持主线一致（此前恰在最需要时静默绕过） |
| `explore_conflicts` | 完全不吃 | **首选**以 `research_agenda` 的 open questions 生成假设（"框架层冲突"）；消费 `key_conflicts`；`candidates` 非空 → 框架竞争假设 A/B/C | 让"研究议程"真正驱动研究 |
| `verify_hypotheses` | 不吃 | 按 `priority` 决定验证顺序；消费 `recommended_verification_order`（**字段至今零消费，且需先按 §5 加进契约**） | 让"先验证什么"由驱动画像决定 |
| `run_thesis` / `generate_report` | 不吃 | **① `ReportGenerationPayload` 增 `driver_profile` 字段**（G-5：报告层当前零通路）；② `main_driver` 取 `driver_hypotheses[role=primary].variable`（公司特异）；③ `central_tension` 围绕 `research_agenda[priority=critical]`；④ 报告可见 `report_questions` | 兑现 roadmap P2"报告主线切换为 driver-first"，而非只靠 LLM 自觉 |
| `synthesize_insights` prompt 原则 9 | 已消费（唯一） | 增加"引用 `driver_hypotheses` 时必须保留其 `evidence`；`unverified_drivers` 不得写成结论" | 防幻觉进主线 |
| CLI / Web 渲染 | 显示 `playbook` + `primary_drivers` | 增加 `provenance`、`anchor_strength`、`research_agenda` 摘要、`novel_drivers`、`why_selected`（该字段至今无消费方，G-8） | 可观测性 |

**偏离控制接线（G-3，必须同期做，否则新契约仍是"声称保证"）**：
1. 把 `ArtifactType.DRIVER_PROFILE` 登记进 `detectors._ARTIFACT_MODELS`（`detectors.py:141-152`）；
2. 用 `with_deviation_detection("resolve_driver_profile", ...)` 包装该节点（`agent.py:472`，
   与其余节点一致）；
3. 契约 `detectors` 扩为 `("artifact_schema_valid", "driver_profile_evidence_present")`，
   `postconditions` 补"驱动非空且证据可溯"；
4. 顺手修正 G-10 的两条错误 `preconditions` 文本（读 `run.context["symbol"]`；
   `load_primitives()` 需补 try/except 才配得上"不可用走降级"）。

⚠️ **回归代价（G-9）**：`tests/tracking/test_scheduler.py::test_f4_does_not_touch_node_contracts`
对 `sorted(NODE_CONTRACTS)` 做键集 + SHA 指纹锚（`1857784a5cb634e3`），
`tests/orchestrator/test_node_contracts.py` 的 `_EXPECTED_DETECTORS` / `_EXPECTED_LADDERS`
也逐字比对。**改契约必然打破这三处**，须同期更新（属预期改动，不是缺陷）。

**设计选择：不新增节点**。Tier B 在既有 `resolve_driver_profile` 节点内扩展，
从而**避开** `NODE_ORDER` / `NODE_CONTRACTS` / `test_contracts_match_graph_builder_registration`
（读 `agent.py` AST 校验节点与边）的连带改动，把改动面收敛到契约字段与消费点。

`FrameworkCompetition` 复用现有 `ConflictAnalysisResult` / `VerificationResultItem`，
**不新建数据模型**（遵守 roadmap §"实现约定"）。

---

## 10. 反馈闭环：让知识库跟着 run 长

```text
run 中 Tier B 产出 novel_drivers（如「铜箔-树脂价差」「AI 服务器高速 CCL 结构占比」）
   → 落 artifact（不写库）
   → 离线聚合：按 variable 聚类 + 频次统计（复用 alphabee/task_records/ 的蒸馏思路）
   → 人工/半自动 review → 新增 domain_primitives/*.yaml（+ 必要时 playbook）
   → 下次 Tier A 直接命中，Tier B 不必再跑
```

这正面回应 DOMAIN_CONTEXT_ROADMAP 的核心焦虑（"不要做成静态行业知识库"）：
**知识资产不再靠人预先穷举，而是由真实 run 反哺**。同时它天然给出
"下一批该写哪个原语"的优先级（出现频次 × 覆盖标的数），避免拍脑袋扩 YAML。

---

## 11. 分期实施（每期独立可测、向后兼容）

| 期 | 内容 | 风险 | 预估 |
|---|---|---|---|
| **D0 契约与信息带宽**（零 LLM） | DriverProfile v2 契约（含 §5 的两处"知识字段补齐"）；`RouterInput` 扩容（segments/`sw_code`/结构摘要）；`anchor_strength` 三档（**不依赖 `sub_industry`**，G-1）；`generic_fundamental` 结构性兜底驱动；`payload_builders` 补全（含 G-6 矛盾）；`build_fallback_insight` 接 `driver_profile`（G-4）；renderer 展示 | 低（纯函数 + 增量字段）；**会打破 3 处既有测试锚**（G-9） | 2~3 天 |
| **D1 模型研究层** | **前置 R-1/R-2/R-3（§14.1，默认值不变、可独立提交）**；`agents/driver_profile/`（factory + prompt + output schema + `_ToolExclusionMiddleware`）；`domain_context/research.py`（触发/输入包/调用/Tier C/缓存/预算/显式 `recursion_limit`）；节点接线；mock 测试 | 中（LLM 节点 + 外部调用 + 中间件改动） | 4~6 天 |
| **D2 下游接线** | explore_conflicts / verify_hypotheses / thesis 消费 `research_agenda` / `key_conflicts` / `recommended_verification_order` / `driver_hypotheses`；**`ReportGenerationPayload` 增 `driver_profile`**（G-5）；登记 `_ARTIFACT_MODELS` + 包装 `with_deviation_detection` + 新检测器；契约 `pre/postconditions` 修正（G-10）；更新 3 处测试锚；CLI/Web 渲染 | 中（跨节点契约传播，走 contract-steward） | 3~4 天 |
| **D3 闭环与度量** | `novel_drivers` 聚合蒸馏工具；eval harness（覆盖率/命中率/消费率三指标）；接入 CI | 低 | 持续 |

**D0 单独就有交付价值**：它立刻消灭「驱动: —」（§1.2/§1.1 的直接症状），
修掉 G-4/G-6 两个"知识到不了下游"的硬矛盾，且是 Tier A 的锚与 Tier B 输出的对齐目标。

**依赖关系**：**D1 与 D2 必须成对交付**。只做 D1 会让 `driver_hypotheses` / `research_agenda`
重复 §1.2/§1.4 的错误——又一个内容丰富但没人读的 dead-end artifact。

> 与既有 roadmap 的关系：本设计的 D0 ≈ roadmap「Phase A（纯 YAML）+ Phase B（schema/新信号）」
> 的一部分，但**用"本公司结构推导"取代"人工补 8 个原型 playbook"**——
> 前者零人月且标的特异，后者是必要但可后置的知识沉淀（恰好由 §10 闭环按频次驱动）。

---

## 12. 验收标准（可量化、可回放）

1. **回归**（硬门）：`tests/domain_context/`（26 例）+ `tests/orchestrator/test_resolve_driver_profile.py`
   + `test_node_contracts.py`（49 例）全绿；牧原 → `hog_cycle`、金诚信 → `mining_services` 且 `fallback=false` 不变。
   *（基线已于本次设计核实：26 + 49 全绿；`tests/orchestrator` 需 `HOME` 指向可写目录，
   否则 tushare `set_token` 写 `~/tk.csv` 会中断 collection。）*
   **允许且必须的破例**：`_EXPECTED_DETECTORS` / `_EXPECTED_LADDERS` /
   `test_f4_does_not_touch_node_contracts` 的指纹锚（G-9）随契约变更**同期更新**——
   这三处变更需在提交说明中显式登记，视为预期改动而非回归。
2. **覆盖率**：`primary_drivers` 非空比例 = 100%（当前远低于此，且命中专用框架时为常量）；
   `fallback=true` 且 `driver_hypotheses` 为空的比例 = 0。
3. **证据可溯率**：`driver_hypotheses` 中 ≥1 条 `evidence` 且 `ref` 非空的比例 = 100%；
   `unverified_drivers` 出现在 `main_driver`/`central_tension` 中的次数 = 0（可断言）。
4. **标的特异率**：同一 playbook 覆盖的标的中，`primary_drivers` 完全相同的比例 < 50%
   （当前为 100%，因为全是常量）——直接量化 roadmap"同行业不同公司主线不同"目标。
5. **下游消费率**：`research_agenda` 的 open questions 被 `explore_conflicts` 引用的比例 ≥ 80%；
   激活原语的 `key_variables`/`causal_paths` 进入 insight prompt 的比例 = 100%；
   **`main_driver` 在 Tier 2 降级路径下也与 `primary_drivers` 一致**（修 G-4，可直接断言）。
6. **沉淀闭环**：每 N 次 run 产出的 `novel_drivers` 去重后形成的新原语 PR 数（过程指标）。
7. **成本上界**：Tier B 单标的 p95 工具调用数 ≤ `max_tool_calls`、p95 耗时 ≤ `wall_timeout_s`；
   缓存命中率 ≥ 60%；**并断言 agent 未因 `check_message_limit` 被提前终止**
   （§7.7-2：`research_meta["truncated"]` 不得为"message_limit"）。
8. **偏离留痕**：研究失败/截断/证据不足/事实矛盾四类情形均产显式 issue，无静默降级。
   ⚠️ 断言对象是 **issue 与 artifact 载荷**，不是 step 状态——因本节点恒产 artifact，
   研究失败只会落 `PARTIAL`（`_finalize_step` 语义，§7.7-7）。
9. **契约有效性**：`artifact_schema_valid` 对 `DRIVER_PROFILE` **不再是空操作**
   （已登记 `_ARTIFACT_MODELS` 且节点已包装，G-3）；可加断言"检测器确实执行过"。

---

## 13. 风险、边界与反模式

| 风险 | 缓解 |
|---|---|
| **LLM 编造驱动变量**（幻觉） | C2 证据强制可溯 + `unverified_drivers` 隔离 + C3 与结构化事实交叉校验 + 验收 3 可断言 |
| **LLM 把行业模板当公司特异** | prompt 内置反例（❌"驱动：毛利率" vs ✅ 带公司分部数值）；验收 4 直接度量 |
| **成本/时延失控** | 触发条件收窄 + 缓存 + 工具调用/时长硬上界（现在**全仓库无此机制**，必须补） |
| **与 `web_search_guard` 冲突** | 词边界匹配 + 窄白名单（§14 待拍板 2）；结构化数据走 tushare/年报工具；prompt 禁止引用搜索数字 |
| **工具预算被 `check_message_limit` 静默接管** | 阈值可配置，并断言未因 `message_limit` 被终止（§7.7-2、§12-7） |
| **agent 持有 shell/fs 权限**（`tools=` 是增量的） | 必须挂 `_ToolExclusionMiddleware`（§7.7-1） |
| **端点不支持 json_schema** | 沿用 `json_instruction` + `parse_json` + Pydantic + 重试 + 降级链（§7.5，仓库已验证） |
| **破坏确定性内核** | Tier A 全纯函数；Tier B 可关；Tier C 可单测；旧 artifact 可反序列化 |
| **artifact 再次变 dead-end** | D2 与本设计同期交付；验收 5 把"消费率"设为硬指标（这是本次设计最该吸取的教训） |
| **与 framework 无关的越界** | 非目标清单（§3.2）写死；不碰 narrative_transition / EventOverlay |

**明确的反模式（不要做）**：
- ❌ 让 LLM 输出 playbook id 并覆盖 Tier A（回放性/可测性崩坏）。
- ❌ 无证据的驱动进 `main_driver`。
- ❌ LLM 直接生成 YAML 写库（沉淀必须人工 review）。
- ❌ 新增 `OrchestratorState` 顶层字段或新 `ArtifactType`（遵守仓库 artifact 契约）。
- ❌ 把 `research_agenda` 写成给人看的摘要（不可执行 = 无效）。
- ❌ 为了"消灭兜底"而把通用财务题眼堆进 `primary_drivers`（那是换了个模板，不是做研究）。

---

## 14. 待拍板决策

### 14.1 已拍板（2026-10，用户决议，落地时机见 §11）

| 决议 | 内容 | 影响面 |
|---|---|---|
| **R-1 `web_search_guard` 误拦修复** | 把 `pe\|pb\|ps` 从**子串**匹配改为**词边界**匹配（`web_search_guard.py:33-38`）；中文侧禁词规则保持不变 | 仅 middleware 一处正则；消除 `capex` / `pipeline` 等驱动变量查询的误拦（§7.7-3） |
| **R-2 驱动变量类查询窄白名单** | 为「**非本公司财务**」的行业/商品变量查询开窄白名单，同时 prompt 明令禁止引用搜索结果中的数字（数字一律走 tushare/年报） | middleware + Tier B prompt 双改；需明确白名单判据，避免变成"全面放开" |
| **R-3 `check_message_limit` 阈值可配置** | 阈值 50 放开为配置项，**默认仍 50**（保持既有行为不变），使 `max_tool_calls` 真正生效而非被静默接管 | `middleware/common.py:8-14` + config；注意该中间件被 6 个 agent 共用 |

> **R-1/R-2/R-3 是 D1 的前置条件**，不是可选项：不修则 Tier B 的研究深度会被静默削弱——
> 想查的行业变量查不了（R-1/R-2），设的工具预算不生效（R-3）。
> 三者均**不影响既有节点行为**（默认值不变），可独立提交、独立验证。

### 14.2 已拍板（2026-10，用户决议：**全部按建议采纳**）

| # | 决策 | 决议（按建议） | 状态 |
|---|---|---|---|
| 1 | Tier B 默认开启还是 flag 门控？ | **flag 门控起步**（与 `midterm` 同构：`state["driver_research"]`，默认关，验证后默认开） | ✅ 已决 |
| 4 | `candidates` 是否同时用于报告层"过渡态框架博弈"（即提前做 roadmap P1 的 `narrative_transition`）？ | **本设计只落字段**，博弈逻辑留给 P1 专项（避免一次性吞下两件事） | ✅ 已决 |
| 5 | 缓存落盘位置与失效策略 | `data/driver_profiles/<symbol>.json`；失效 = 新报告期 / track 漂移 / playbook 版本（§7.6） | ✅ 已决 |
| 6 | `novel_drivers` 沉淀的 review 门槛与责任人 | 离线工具产出候选清单，**人工 review 后**才生成 PR（避免知识库被自动污染） | ✅ 已决 |
| 7 | D0/D1/D2 的推进顺序是否可以裁剪 | D0 可独立交付并立即见效；**D1/D2 必须成对**（只做 D1 会让 artifact 再次 dead-end） | ✅ 已决 |
| 9 | golden 夹具保真度是否同期修复（改为生产形状 `sub_industry=""`，并补"仅 track_label 命中"用例）？ | **是**（G-1/G-2）：现有 golden 在真机形状下并不覆盖金诚信的路径，属"通过但不可信" | ✅ 已决 |
| 10 | 是否同步引入 `sw_code` 前缀匹配（修掉「农林牧渔 → hog_cycle」宽 L1 误配）？ | **是**（§6，成本低、收益明确；需先改 `PlaybookSchema` 再改 YAML，注意 `extra="forbid"`） | ✅ 已决 |

> **落地时机（§11）**：R-1/R-2/R-3 与 D0 先行（零 LLM、可独立交付/验证）；D1/D2 **成对**交付；
> D3 闭环与度量持续。本表决议一旦实施即视为「实现契约」，测试锚更新（G-9）为预期改动、非回归。

---

## 15. 一句话总结

当前 `resolve_driver_profile` 把"研究"做成了"查表"：3 个 YAML、4 个字符串、0 条证据、
1 个下游，且这个下游只在最后一步决定措辞。改造的关键不是"给 router 加个 LLM 复核"，
而是**补上缺失的假设层**——让模型基于公司自己的年报与分部结构提出**标的特异的驱动假设与可执行研究议程**，
让规则提供锚与守卫，让确定性层负责收敛、留痕与反哺知识库，
并**用验收指标（§12-4/§12-5）保证它这次真的被下游消费**。
