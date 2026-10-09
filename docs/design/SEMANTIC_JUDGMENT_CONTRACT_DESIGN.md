# 语义判读改造设计（关键词语表 → 结构化契约 + 受控 judge）

> 目标条目：把流水线里"用中文标记表读自然语言"的判读点（以 `alphabee/agents/thesis/engine.py::conflict_direction` 为代表）改造为**上游结构化契约优先 + 受控 judge 兜底 + 关键词表降级为兜底**的三级判定，并建立可度量的**覆盖度（coverage）**体系。
> 关联条目：`docs/roadmap/ROADMAP.md` 交叉核对修复 **P0-2**（2026-10-04）、**P0-2R3**（2026-10-04）、**P1-4**（2026-10-04）；残留项 **RC-D3**、**RC-DIR-LOW**（含 finding **F-4**：docstring 零命中计数与实测不符，明文要求"下次因其他原因触碰 `conflict_direction` 时顺带收口，勿为它单独开提交"——**本设计即该"其他原因"**）；`docs/design/DEVIATION_CONTROL_FRAMEWORK.md` §8（放大标注与加权边审计）、§7.2（降级传导阻尼）；`docs/design/INSIGHT_DEGRADATION_DESIGN.md`（相邻但**不在本设计范围**，见附录 B）。
> 状态：📝 **设计待评审（未实施）**。文中所有"实测"数据均附复现命令，语料范围逐字披露（F-4 纪律）。

---

## 1. 背景与问题

### 1.1 动机：三个已确诊缺陷都指向同一处（真实 run 证据）

以 `data/task_records/002916.SZ/2026-10-05/task-7fe88386b437.json`（`logs/alpha_arena.log` 同一 run，`total_duration_s=950.8`）为证据：

| 编号 | 缺陷 | 证据（可直接核对） | 直接后果 |
|---|---|---|---|
| **D-1** | Insight 降级口径过宽：Tier 1「宽松救援」与 Tier 2/3「确定性兜底」同标 `degraded=true`，报告层按布尔走死句式 | issue `insight_degraded`：`Insight 降级产出（tier=1）: lenient_rescue: importance coerced: 'important'`；`orchestrator/prompts.py:62` 的降级分支 | 观点层内容 100% 完好却被判"降级"，`investment_viewpoint` 退化为摘要 |
| **D-2** | 维度置信度被**绝对减法**打穿：覆盖度是**比例**（分母 = 信号总数 22），扣减是**绝对常数**（−0.05/−0.1），且按"每条假设 × 每个相关维度"重复执行、与 severity 无关 | 重放原始覆盖度 0.045~0.545（§1.3 表）；记录值 7 个 `0.0` + 1 个 `0.03`；`engine.py:691/717/727` | 7/8 维度 `insufficient`、8 条 high blocking issue、`_compute_overall` 只用 1 个维度（`overall_score = −0.362` = `credit_risk` 单维得分，见 `alpha_arena.log:887`） |
| **D-3** | `unknown` 被**单边映射为负面**：4 个消费点全部按 `!= "benign"` 处理，判据覆盖度的每个缺口都一对一转为扣分 | `engine.py:675-676`、`reviewer.py:657/792/888` | 良性扩张解释被扣到 `earnings_quality/financial_quality = −1.0`；本 run 的维度方向与 `core_view` 全面冲突 |

> D-1 的完整修复由 `INSIGHT_DEGRADATION_DESIGN.md` 已描述的四级阶梯 + 一条"报告层 tier 感知"补充构成，**不在本设计范围**（附录 B 登记）。本设计覆盖 **D-2、D-3 及其根因（语义词表判读）**。

### 1.2 现网"关键词判读"点位盘点

按**判据性质**分四类（只有 K1 进入本设计）：

| 分类 | 点位 | 规模 | 结论 |
|---|---|---|---|
| **K1 语义判读**（散文 → 语义标签） | `engine.conflict_direction`（`engine.py:250-323`，依赖 8 张标记表） | 29 良性 + 32 恶性 + 4 症状 + 6 语境 + 16 否定 + 8 拉丁恶性 + 14 拉丁白名单 + 7 项目制；约 140 行逻辑 + **574 行测试 / 70 个 collected 用例**（P0-2 登记为 39 例） | ✅ **本设计对象** |
| **K1 语义判读** | `reviewer._insight_direction`（`reviewer.py:465-466, 579-596`） | 10 多空词 `_BULLISH_KEYWORDS` / `_BEARISH_KEYWORDS` | ✅ 本设计对象（RC-D3 的关键一侧） |
| **K1 语义判读** | `engine._PROJECT_BASED_KEYWORDS`（`engine.py:57`） | 7 词（项目/验收/军工/工程/软件/集成/to_b） | ⚠️ 低优先，同法可迁移 |
| **K2 格式抽取** | `services/report_window.py:_PERIOD_KEYWORDS`、`loader/pdf_ocr_loader.py` 标题正则 | — | ❌ 不换：规则更准、更便宜、可解释 |
| **K3 安全/合规门** | `middleware/web_search_guard.py:_FORBIDDEN_PATTERNS` | 4 类预调用正则拦截（股价/估值指标/财务数字/行业行情） | ❌ **禁止替换**：安全门必须确定性、可审计、不可被话术绕过 |
| **K4 数值阈值（产品定义）** | `_CONFLICT_PENALTY` / `SIGNAL_LEVEL_TO_SCORE` / `JUDGMENT_THRESHOLDS` / derived-fact 公式 / anomaly z-score 规则 | — | ❌ 不换：这是产品口径，不是语言理解 |

### 1.3 覆盖度体检（实测，含语料范围披露）

**语料**：`data/task_records/*/*/*.json` 中 `issues[].category == "verified_conflict"` 的全部文本。
**范围逐字披露（F-4 纪律）**：**259 条 / 31 个 task record**。注意此处语料为 issue 里的**合并文本**（`[冲突已验证] {theme}: {explanation}. 结论: … 缺口: …`），与运行时 `conflict_direction` 真正消费的字段（`HypothesisItem.explanation`）**不是同一分布**——task record 目前**不落** `explanation`（见 §8.1）。模块 docstring 自述为"258 条"，与本次实测差 1 条（新增 run）。

| 指标 | 实测 | 说明 |
|---|---|---|
| 判向分布 | `negative 119 (46.0%)` / `unknown 100 (38.6%)` / `benign 40 (15.4%)` | 39% 判不出，且在消费侧 = 扣分（D-3） |
| 零命中标记（`_marker_hits` 消费路径） | 良性 **6/29**、恶性 **14/32**、否定 **3/16** → 合计 **23 个** | 良性零命中：`惯例` `客户驱动` `未发现异常` `无异常` `无虞` `好转`；恶性零命中：`利益输送` `违规担保` `内幕交易` `造假` `虚报` `欺诈` `挪用` `掏空` `隐瞒` `暴雷` `失控` `风险积聚` `实质风险` `不明显` |
| 拉丁白名单覆盖 | 白名单 **14** 项；语料中**白名单外**拉丁 token **159 种 / 740 次**；**71.0%** 的文本含白名单外 token | 高频：`PEG(98)` `VS(55)` `FY(34)` `PP(29)` `CAPEX(23)` `TTM(17)` `OCF(16)` —— **PEG 是估值核心指标却不在名单** |
| `unknown` 成因拆解（100 条） | 无任何良性标记命中（中文表覆盖不到）**56**；良性命中但被拉丁回落 **44** | 拉丁白名单一项造成 **44%** 的 `unknown` |
| 症状词 + 语境合取 | `异常` tail=∅ ×42 vs 有语境标记 ×13；`恶化` ×28 vs ×9；`占用` ×15 vs ×3；`积压` ×9 vs ×7 | 约 3/4 症状词命中走 `negative`（`_symptoms_in_benign_context` 要求"每个"症状词满足，一票否决） |
| 表内子串冗余 | 4 组：`正常`⊃`正常现象`/`正常波动`/`正常范围`、`可控`⊃`风险可控` | 冗余不致命，但抬高"覆盖度观感" |

**与既有登记的对照**：ROADMAP 的 **F-4** 记载 verifier 实测"恶性 14/22 + **12 条零命中**（漏披露 `利益输送/违规担保/内幕交易/欺诈/失控/不明显`）"，与本次独立实测（14/32 零命中，含上述 6 词）**结论一致**，仅分母因 P0-2R3 后又扩表（22→32）而不同。**本设计顺带收口 F-4**：把 docstring 的出处计数改为按实测口径 + 标注语料范围与命令（见 §9 阶段 P0）。

### 1.4 为什么"再补词表"不是出路

1. **覆盖度不可闭合**：自然语言的同义/反语/转折是开放集，而词表是人工"拟定词典"；git log 显示已连续 3 轮补表（`c1e917e` P0-2 → `98ceade` P0-2R3 → t22 F1–F5），仍有 23 个标记零命中、39% 判不出。
2. **零命中标记是负资产**：占全表约 30%（23/77），不贡献召回，只贡献维护成本与"覆盖度幻觉"。
3. **窄集合守护宽决策**：14 项拉丁白名单 gate 了"是否相信中文判据"这个宽决策，方向还是反的——**"不认识"默认等于"可疑"**。
4. **可达性依赖消费路径**：`_CLAUSE_SPLIT_RE` 含 `而`，故 `而非` 不可能出现在任何 clause 内部；它只能经 `_symptoms_in_benign_context` 的 sentence-tail 被消费（实测命中 31 次）。**同一张表的可达性随消费点变化，靠读代码/写用例难以保证一致**——这是方案的结构性上限，而非实现疏忽。
5. **`unknown` 放大了以上全部**：判不出不是"沉默"，而是"看空"（D-3）。

---

## 2. 设计目标与非目标

### 2.1 目标

1. **判定语义化**：K1 类判读点的输入不再是"散文 → 事后猜"，而是"生成方直接给出枚举标签"。
2. **覆盖度可度量**：定义并落地 coverage 指标 + 每 run 报表，使"词表够不够"从信仰问题变为数字问题。
3. **缺口不放大**：`unknown` 从"单边负面"改为"双向不确定"，并留痕；判据缺陷不再直接转化为投资结论偏差。
4. **口径单一**：engine 与 reviewer 的 4 个消费点读**同一份**判决（现在各自调同一函数，一旦换成 LLM 必然漂移）。
5. **可复现**：判决必须落 artifact + 带版本，保住"用 task record 确定性重放打分"的回归能力。
6. **改动可回滚**：任一新判据都能一键退回关键词兜底，且返回后行为逐字等价。

### 2.2 非目标

- ❌ 不做"把整个 thesis 引擎交给 LLM"：`node_contracts.py` 把 `run_thesis` 登记为"确定性引擎无重试价值"，本设计**不动这条契约**——judge 放在 `run_thesis` **之前**，engine 仍只读结构化字段。
- ❌ 不改 K2/K3/K4 类点位（格式抽取、安全门、数值阈值）。
- ❌ 不修复 D-1（Insight 降级口径，另立，见附录 B）。
- ❌ 不承诺"换 judge 后冲突判向更准"——本设计先交付**度量与止损**，判据替换以 golden set 上"恶性漏判率"为准入条件。

---

## 3. 总体架构：三级判定 + 单一留痕

```text
第一级  确定性阈值/规则          （K4，不变）
第二级  上游结构化契约字段        ← 新增（首选，零额外调用）
           HypothesisItem.direction: benign | negative | unknown
第三级  受控 judge（可选启用）    ← 新增（仅当第二级缺失时才调用）
           批量 · 缓存 · 结构化输出 · 超时回落

                    ↓ 判决合并（单一来源）
        CONFLICT_DIRECTIONS artifact（判决 + 来源 + 版本 + 依据）
                    ↓
   engine._apply_conflict_analysis / reviewer._conflict_votes /
   _audit_conflict_penalty / _edge_applicable   ← 4 个消费点读同一份
```

**为什么第二级优先于第三级**：被判读的文本本来就是**同一条流水线的 LLM 写的**（`HypothesisItem.explanation` 来自 `explore_conflicts` / `verify_hypotheses`）。让生成方顺手打标签，比事后用关键词或第二个 LLM 去猜，更准、更便宜、**无信息损失**。仓库已有两处先例：commit `96896f5`「用结构化契约替换关键词判断」给 `ConflictItem` 加了 `related_dimensions: list[ThesisDimensionId]`（prompt 内显式枚举）并删除下游关键词判读；`HypothesisItem.disputed_pattern_ids/disputed_signal_ids`（`agents/schemas.py:47-54`）同法，且**明确保留关键词兜底**（`engine.py:805 _infer_disputed_evidence`）。

**判定优先级（规则）**：

| 优先级 | 来源 | 触发条件 | 留痕 `judge_source` |
|---|---|---|---|
| 1 | 结构化字段 | `HypothesisItem.direction` 非空 | `contract` |
| 2 | judge | 字段缺失 且 judge 已启用且返回合法值 | `judge:<model>:<prompt_version>` |
| 3 | 关键词兜底 | 前两级都不可用 | `keyword_fallback` |

**judge 的三条硬约束**（若启用）：

1. **批量**：一次调用判该 run 的全部待判假设。本 run 待判量 ≈ 18 条（6 冲突 × ~3 假设）；逐条调用 = +18 次往返，而当前 run 已 950.8s（`verify_hypotheses` step elapsed 460s、`synthesize_insights` 615s）。
2. **缓存 + 版本键**：`sha256(explanation) + judge_version + prompt_version` 落 `data/judge_cache/`。命中即零成本，且**同一文本永远同一判决**——这是保住可复现性的唯一手段。
3. **结构化输出 + 依据**：`{hypothesis_id: {label, reason, evidence_span}}`，`label ∈ {benign, negative, unknown}`；`evidence_span` 必须是原文子串（可审计、可人工抽检）。

---

## 4. 数据契约变更

> 遵循 `CLAUDE.md` 的编排契约：**不把节点产物加成 `OrchestratorState` 字段**，一律进 `artifacts` 并登记 `ArtifactType` + typed contract，下游用 `find_artifact_model(...)` 消费。

### 4.1 `HypothesisItem`（`alphabee/agents/schemas.py:28-54`）

```python
direction: Literal["benign", "negative", "unknown"] = "unknown"
# 该假设 explanation 的方向语义：benign=良性归因（正常现象/主动安排/有支撑…）、
# negative=恶性指控（操纵/滞销/减值/资金占用…）、unknown=判不清（默认，保守）。
# 由 explore_conflicts 产出、verify_hypotheses 按证据在必要时修订；为空/unknown 时下游走
# judge → 关键词兜底（与 disputed_*_ids 的既有模式一致）。
```

- 默认值 `"unknown"` 保证**向后兼容**：旧 artifact / 旧 LLM 输出缺字段时行为与今天逐字一致（走兜底）。
- prompt 侧（`agents/explore_conflicts/prompts.py`）按 `related_dimensions` 的既有写法补齐"枚举值 + 语义定义 + 正反例"；`verify_hypotheses` prompt 允许在证据翻转时修订该字段。

### 4.2 新增 artifact：`CONFLICT_DIRECTIONS`

```python
# alphabee/core/schemas.py::ArtifactType
CONFLICT_DIRECTIONS = "conflict_directions"  # 冲突假设方向判决（判决 + 来源 + 版本）
```

```python
# alphabee/orchestrator/contracts.py
class ConflictDirectionItem(BaseModel):
    hypothesis_id: str
    conflict_id: str = ""
    label: Literal["benign", "negative", "unknown"]
    judge_source: str = ""        # contract | judge:<model>:<ver> | keyword_fallback
    reason: str = ""              # judge 给的理由（keyword_fallback 留空）
    evidence_span: str = ""       # 必须为 explanation 的子串（可审计）

class ConflictDirectionsArtifact(BaseModel):
    items: list[ConflictDirectionItem] = Field(default_factory=list)
    judge_version: str = ""       # 空 = 未启用 judge
    fallback_count: int = 0       # judge_source == keyword_fallback 的条数（可观测）
```

- 登记方式对齐既有 `ASSUMPTION_REGISTRY`（只进 artifacts、多只读消费者）。
- **4 个消费点统一改读**该 artifact：`engine.py:675`、`reviewer.py:657 / 792 / 888`；缺失时回落 `conflict_direction()`（回滚路径 = 直接不产出该 artifact）。
- 生产者：新增节点或并入既有节点出口（见 §9 阶段 P2 裁定项 O-2）。

### 4.3 task record 落 `explanation`（度量前提）

`alphabee/task_records/models.py` + `recorder.py`：在 issue 摘要之外，落该 run 每条假设的 `{hypothesis_id, conflict_id, status, explanation, direction, judge_source}`。
理由：现在只落**合并文本**，与运行时输入分布不同（同一事实在短句/拼接句下判向不同，§1.3 已披露），导致**判据的输入分布没有留痕**，覆盖率无法计算、历史回归无法做。

### 4.4 相邻契约缺口（顺带登记，改动极小）

`ReportInsightPayload`（`contracts.py:323-339`）只有 `degraded: bool`，而报告 prompt（`prompts.py:45`）要求模型参考 `fallback_tier` —— **该字段根本不在载荷里**。补 `fallback_tier: int = 0`，让报告层能区分"Tier 1 内容完好"与"Tier 3 空骨架"。（D-1 的完整修复另立。）

---

## 5. `unknown` 语义止损（D-3）

### 5.1 设计口径

| 侧 | 今天 | 改造后 |
|---|---|---|
| engine 扣分 | `not benign` ⇒ 扣分（含 `unknown`） | 仅 `negative` ⇒ 扣分；`unknown` **不扣分**，落 issue + `missing_evidence` |
| reviewer 计负票 | `!= "benign"` ⇒ 负票 | 仅 `negative` ⇒ 负票；`unknown` 不进票池，单列计数 |
| 可观测 | 无 | issue `conflict_direction_unknown`（LOW/`scope=data`，含条数与来源），并进 §8 报表 |

**理由（逐字）**：`unknown` 是**双向不确定**（既可能是良性也可能是恶性）。把它单边映射成负面，等于把"判据覆盖率不足"直接翻译成"看空结论"。本 run 的 `−1.0` 与置信度归零正是这种伤害。

**代价（显式登记）**：改造后 `unknown` 不再保守扣分 ⇒ 存在"恶性但判不清 ⇒ 少扣分"的风险敞口。缓解：① `negative` 判定不受影响（恶性标记优先）；② 报表暴露 `unknown` 占比与来源，超阈值可要求人工抽检；③ 若 golden set 上"恶性→unknown"占比高于阈值，本阶段即熔断（§8.3 准入条件）。

**待裁定（O-1）**：若 captain 要求保留安全侧，可退为"`unknown` 不扣分但**降低该维度 confidence 一档**"（不直接改 score），二者择一。

### 5.2 与 P0-2R3 的关系

P0-2R3 明文写的"判不清 ⇒ `unknown` 保守回落既有负贡献语义（与修复前逐字一致，安全侧）"将被本设计**替换**为"不扣分 + 留痕"。这是**一次行为变更**，必须按仓库纪律登记（ROADMAP 行 + 判别力测试 + 回滚说明）。

---

## 6. 维度置信度口径修复（D-2）

### 6.1 问题算式

```python
# engine.py:454   覆盖度 = 比例（分母 = 信号总数）
confidence = min(1.0, len(contribs) / max(1, total_signals))
# engine.py:691 / 717 / 727   扣减 = 绝对常数，按「每条假设 × 每个相关维度」重复执行，与 severity 无关
dim.confidence = max(0.0, dim.confidence - 0.05)   # verified/partial 且有 gaps
dim.confidence = max(0.0, dim.confidence - 0.05)   # rejected
dim.confidence = max(0.0, dim.confidence - 0.1)    # unknown/pending
```

002916 实证：原始覆盖度 0.045~0.545，扣减后 7 个维度为 `0.0`、`credit_risk` 为 `0.03`（`4/22 − 0.1 − 0.05 = 0.0318 → round(·,2)=0.03`，与记录逐位吻合）。

### 6.2 修复方案（推荐 B1，O-3 待裁定）

| 方案 | 形式 | 评价 |
|---|---|---|
| **B1（推荐）** | `dim.confidence *= 0.85`，**一档封顶**（复用 `degradation.DAMPING_FACTOR` 语义） | 与仓库既有"降级传导阻尼一档封顶"口径一致；有贡献的维度永不归零 |
| B2 | `dim.confidence = max(FLOOR, conf - ded)`，`FLOOR = 1/total_signals` | 保底可见，但引入新常量（§15.0 C-5 反对魔法常量） |
| B3 | 拆字段：覆盖度（`coverage`）与可靠度（`reliability`）分离 | 语义最正确，改动面最大（4 处消费 + reviewer 文案 + 报告载荷） |

### 6.3 配套不变量（新增钉子）

1. **不归零不变量**：`len(contribs) > 0 ⇒ confidence > 0`（任意冲突条数下成立）。
2. **口径一致**：`_compute_overall`（`engine.py:530`）与 `_thesis_direction`（`reviewer.py:688`）均只取 `confidence > 0` 的维度——修复后二者必须对新分布给出一致结论。
3. **文案分流**：`reviewer.py:137` 的 Rule 1 文案 `"该维度无信号覆盖，判断不可靠"` 只在 `not evidence` 时使用；`confidence==0 且 evidence 非空` 改报"冲突重罚导致置信度归零"（当前文案与 15 条证据并排出现，属误导性诊断）。

---

## 7. 覆盖度度量体系（coverage）

### 7.1 指标定义（每 run 输出）

| 指标 | 定义 | 目标 |
|---|---|---|
| `unknown_rate` | 判决为 `unknown` 的假设数 / 待判假设数 | 观察项；启用 judge 后应下降 |
| `judge_source_mix` | `contract` / `judge` / `keyword_fallback` 三者占比 | `keyword_fallback` 应随上游 prompt 收敛而下降 |
| `zero_hit_markers` | 在最近 N 条真实语料上零命中的标记 | 清理项（当前 23 个） |
| `latin_uncovered_tokens` | 白名单外拉丁 token 词表（按频次） | 扩表输入 |
| `symptom_benign_context_ratio` | 症状词命中中满足良性语境的比例 | 观察项（当前约 1/4） |
| `malignant_leak_rate` | golden set 上"真值 = negative 但判为 benign"的比例 | **准入门（§7.3）** |

### 7.2 golden set 建设

- **来源 A**：259 条真实 `[冲突已验证]` 文本（可重建，§1.3）。
- **来源 B**：`tests/agents/thesis/test_conflict_direction.py`（289 行）+ `test_conflict_direction_r3.py`（285 行），`pytest --collect-only` 计 **70 个用例**（含参数化、19 例对抗探针、002916 五冲突真实原文钉）。
- **来源 C**：每次 run 落地的 `explanation` + 判决（§4.3），人工抽检回流。
- **必须补的是真值标注**：当前语料**无真值**，所以只能算"可达性/分布"，算不出覆盖率与漏判率。标注按三分类（`benign/negative/unknown`）+ 一行理由；真值单列存放，禁止与判据输出混存。

### 7.3 准入条件（换判据前必须满足）

1. golden set 真值标注完成（≥200 条，覆盖来源 B 与 A）；
2. 基线已测：现有关键词表在上述集合上的混淆矩阵（含 `malignant_leak_rate`）；
3. judge/契约字段的 `malignant_leak_rate` **不高于**基线（非对称代价：放过恶性 ≫ 误伤良性）；
4. 缓存命中率与单 run 增时在预算内（当前 run 950.8s，增量成本须显式登记）。

---

## 8. 分阶段实施

> 每阶段独立可交付、可回滚；**阶段 P0/P1 不改判据结论**（除 P0 明确登记的 D-3 止损）。

### 阶段 P0 —— 止损 + F-4 收口（最小面）

- **改动**：① `unknown` 单边负面 → 双向不确定（§5，4 个消费点 + 新 issue 类型登记）；② 维度置信度改乘性一档封顶（§6，O-3 裁定后）；③ reviewer Rule 1 文案分流；④ `conflict_direction` docstring 的出处计数按实测口径重写 + 标注语料范围与复现命令（**F-4 顺带收口**）；⑤ 报告载荷补 `fallback_tier`（§4.4）。
- **inScope**：`alphabee/agents/thesis/engine.py`、`alphabee/agents/thesis/reviewer.py`、`alphabee/orchestrator/contracts.py`、`alphabee/orchestrator/services/deviation.py`（issue 登记）、对应 `tests/agents/thesis/*`。
- **验收**：002916 重放：7 个维度不再 `insufficient`；`_compute_overall` 的有效维度数 ≥ 2；`unknown` 不再产生扣分（用 §1.3 语料做整表回归）。
- **判别力**：M1（把 `unknown` 改回扣分）⇒ 双向断言必红；M2（把乘性改回绝对减法）⇒ 不归零不变量必红。

### 阶段 P1 —— 度量落地（不改行为）

- **改动**：① `scripts/audit_conflict_direction.py`（§1.3 四张表 + 未覆盖清单 + 复现命令，可重复跑）；② task record 落 `explanation` 与判决（§4.3）；③ 报表接入 `record_deviations` 的可观测面（§7.1）。
- **验收**：任意历史 run 可复算出 `unknown_rate` / `judge_source_mix` / 零命中标记；与本文 §1.3 数字一致（±新增 run）。

### 阶段 P2 —— 结构化契约（路线 A，推荐主力）

- **改动**：① `HypothesisItem.direction`（§4.1）+ `explore_conflicts` / `verify_hypotheses` prompt 枚举要求；② `CONFLICT_DIRECTIONS` artifact（§4.2）+ 4 个消费点改读；③ 关键词兜底保留（`judge_source=keyword_fallback`）。
- **验收**：新 run 上 `judge_source=contract` 占比 ≥ 目标值（裁定项 O-4）；契约缺失时行为与阶段 P1 逐字等价（有判别力测试）。
- **判别力**：删掉字段回落 ⇒ 兜底路径必红；两处消费点读不同来源 ⇒ 口径一致测试必红。

### 阶段 P3 —— judge 兜底（路线 B，仅在必要时）

- **准入**：§7.3 四条全满足。
- **改动**：批量 judge + 缓存 + 结构化输出 + 超时回落（§3 三条硬约束）。
- **验收**：`malignant_leak_rate` ≤ 基线；单 run 增时 ≤ 预算；缓存命中下两次 run 判决逐字相同（可复现性钉子）。

### 阶段 P4 —— 推广到 `_insight_direction`（RC-D3 收口）

- 现状：`_insight_direction` = 支撑/反证**计数** + core_view **关键词计数**（`reviewer.py:588-590`）。002916 实测 `支撑 4 / 反证 4` ⇒ 计数抵消，最终由 `core_view 多空关键词 1/0` 一枚词决定方向 ⇒ 判为"正面"，与证据方向 −0.359 冲突（issue `amplification_direction_conflict`）。
- 改造：方向改为读结构化档位（insight 自带 `confidence` + 新增/复用的方向字段），关键词仅作兜底；**这是 RC-D3 明文要求的"另一次口径变更"**，需单独登记行为变更。

---

## 9. 测试计划

| 层 | 钉子 | 判别力要求 |
|---|---|---|
| 判据单元 | `unknown` 双向语义；乘性封顶；`judge_source` 优先级 | 变异体必红（M1/M2，见各阶段） |
| 口径一致 | 4 个消费点读同一份 `CONFLICT_DIRECTIONS`；缺失时四处同时回落 | 只改一处 ⇒ 必红 |
| 契约传播 | `HypothesisItem.direction` → artifact → 4 消费点 → task record，全链路字段可追溯 | 断链必红 |
| 重放回归 | 以 002916 为 fixture 重放 step-1 打分（`competitive_moat +0.133`、`credit_risk −0.362`、`valuation_fit −0.300`）| 打分链路漂移必红 |
| 语料整表 | §1.3 的 259 条整表判向分布钉（分布变化需显式更新） | 判据改动未登记 ⇒ 必红 |
| 不变量 | `len(contribs) > 0 ⇒ confidence > 0`；`_compute_overall`/`_thesis_direction` 同口径 | 见 §6.3 |

---

## 10. 兼容性、风险与回滚

| 风险 | 缓解 | 回滚 |
|---|---|---|
| 换判据后**放宽**了恶性识别（少扣分） | `malignant_leak_rate` 准入门（§7.3）+ 报表暴露 | 撤回 `CONFLICT_DIRECTIONS` 消费，恢复 `conflict_direction()` 直调 |
| judge 引入非确定性，摧毁重放能力 | 缓存 + 版本键 + 判决落 artifact；judge 放在 `run_thesis` 之前，engine 仍确定性 | 关闭 judge（`judge_version=""`） |
| 增本增时（当前 run 950.8s） | 批量 + 缓存 + 仅对字段缺失的假设触发 | 同上 |
| 契约变更造成下游断链 | 默认值 `unknown` 保证旧 artifact 行为不变；`find_artifact_model` 缺失即回落 | 不产出该 artifact |
| `unknown` 不扣分导致结论变乐观 | 阶段 P0 单独提交、单独登记行为变更，可回滚 | 恢复 `!= "benign"` 口径（M1 钉子） |

---

## 11. 验收标准

1. §1.3 的 benchmark 可一键复算，且与文档数字一致（±语料增量）。
2. 002916 重放：`insufficient` 维度由 7 → ≤1；有效维度数 ≥ 2；`overall_score` 不再等于单维得分。
3. `unknown` 在 4 个消费点均不再进入负面路径，且每条 `unknown` 有 issue/留痕可查。
4. `CONFLICT_DIRECTIONS` artifact 在所有 run 中存在，且 `judge_source` 分布可观测。
5. 全部 `malignant_leak_rate` ≤ 关键词基线；关键词兜底路径行为与改造前逐字等价。
6. F-4 的 docstring 计数按实测口径重写并标注语料范围/命令；ROADMAP 增加本设计的行为变更登记行。
7. `pytest` / `ruff` / `mypy` 零回归（按仓库既有门禁口径）。

---

## 12. 开放问题（待 captain 裁定）

| 编号 | 问题 | 备选 |
|---|---|---|
| **O-1** | `unknown` 的止损形态 | (a) 完全不扣分（本设计默认） / (b) 不扣分但 confidence 降一档 / (c) 维持现状（则 D-3 不修，需另找方案） |
| **O-2** | `CONFLICT_DIRECTIONS` 的生产位置 | (a) 新增独立节点（`NODE_CONTRACTS`/`NODE_ORDER` 同步登记） / (b) 并入 `run_thesis` 首部（省一次节点但弱化"engine 无 LLM"契约） |
| **O-3** | 置信度修复方案 | B1（乘性，推荐） / B2（保底常量） / B3（拆字段） |
| **O-4** | 阶段 P2 的 `judge_source=contract` 占比目标 | 待 P1 度量后定值（不得先定数字后凑） |
| **O-5** | 是否本期就启用 judge（P3） | (a) 仅 A 线，judge 另立 / (b) 同期启用 |
| **O-6** | K1 类第三处（`_PROJECT_BASED_KEYWORDS`）是否本期并入 | 建议否（低优先、低风险） |

---

## 附录 A：体检复现（命令与语料范围）

```bash
# 语料范围披露（F-4 纪律）：259 条 / 31 个 task record
python - <<'PY'
import json, glob
n = sum(1 for p in glob.glob("data/task_records/*/*/*.json")
        for i in json.load(open(p)).get("issues", [])
        if i.get("category") == "verified_conflict")
print("verified_conflict texts =", n)
PY

# 四张体检表（判向分布 / 零命中 / 拉丁覆盖 / 症状词语境）
poetry run python scripts/audit_conflict_direction.py   # 阶段 P1 交付
```

**口径披露**：体检语料为 issue 里的**合并文本**，与运行时 `HypothesisItem.explanation` 分布不同（§4.3 落地后应改用真实 `explanation` 复算，届时本附录数字需重采）。

## 附录 B：不在本设计范围的相邻缺陷

| 缺陷 | 归属 | 说明 |
|---|---|---|
| **D-1** Insight Tier 1/2/3 同标 `degraded` + 报告层 tier-blind | `docs/design/INSIGHT_DEGRADATION_DESIGN.md` + 一条"报告层 tier 感知"补充 | 本设计仅顺带补 `ReportInsightPayload.fallback_tier`（§4.4），不改分级语义 |
| **`MaterialityRank.importance` 同义词缺口**（`important` 未映射 ⇒ Tier 0 失败 ⇒ 降级） | 同 D-1 | 一行级修复（补 `important → high`，并与 `rescue._IMPORTANCE_MAP` 对齐） |
| **RC-D3**（insight 方向票伪正面 / signal 侧票口径） | 本设计阶段 P4（仅 insight 一侧） | signal 票口径（none 级是否计票、冲突票权重）仍是"另一次口径变更" |
| **RC-DIR-LOW F-1/F-2/F-3**（单独否定词 ⇒ benign、未入表恶性语义、症状词只判句尾） | 本设计阶段 P0/P2 顺带收口 | F-4 为本设计明文收口项；F-1~F-3 由 `unknown` 语义变更与契约字段自然缓解 |
