# CompanyStateArtifact 变化分析模型设计（Snapshot Diff）

> 同一标的、两个时间点的 `CompanyStateArtifact` 之间的变化如何建模、计算、归因。
>
> 关联文档：
> - `docs/midterm/MIDTERM_S1_S4.md` §29–§35（State≠Confidence、Velocity/Acceleration、Snapshot、为什么变）、§44（相对建仓时）
> - `docs/midterm/MIDTERM_DECISION_MODEL.md` §2b（软状态）、§6（三轴正交决策）
> - `alphabee/midterm/models.py` — 现有 `CompanyStateArtifact` / `SnapshotDiff`（待升级）typed contracts
>
> 本文档是 diff 引擎（`diff.py`）的实现依据；当前代码里 `SnapshotDiff` 仍是薄壳（`prev_date/curr_date/state_from/state_to/variable_deltas/evidence_changed/thesis_delta`），无计算引擎。

---

## 0. 定位：这个 diff 回答什么

| 问题 | 出处 |
|------|------|
| ① 变了吗？哪个因子、哪个字段变了？ | §34 `Snapshot_t − Snapshot_{t-1}` |
| ② 决策跟着变了吗？（状态/置信度/赔率/仓位） | §35 |
| ③ 变得多快？在加速吗？ | §30/§31 StateVelocity / StateAcceleration |
| ④ **为什么变？**（证据 → 因子 → 决策 的因果链） | §35 每次变化回答"为什么" |
| ⑤ 是否触发了退出条件？ | §37 ExitEngine 五层 |

**核心立场**：diff 的产物不是"两个 JSON 的差"，而是**一条可解释的因果链**——
`EvidenceEvent（新增）→ FactorDelta（哪些因子变了）→ 决策层变化（状态漂移/置信度/仓位）`。

---

## 1. 设计原则

1. **五层正交、分层求差**：`CompanyStateArtifact` 天然分五层（数据/评分/状态/赔率/仓位），每层是下一层的原因，diff 必须分层承载，不得压成一个扁平 dict。
2. **State 变化 = 分布漂移，不是 argmax 跳变**（软状态，§2b）：比较 `StateBelief.distribution` 的质量流动，argmax 变/不变只是漂移的一个投影。
3. **字段级、canonical 名对齐**：diff 只在同名同口径字段间计算（`revision_1m → revision_1m`），最小粒度是 `FieldDelta`。
4. **区分"增减"与"出现/消失"**：`None→有值` = `appeared`，`有值→None` = `disappeared`，不得与 `up/down` 混淆。
5. **引用不内嵌（append-only 反漂移）**：diff 只存 `prev/curr` 的持久化 id 引用，不复制整帧。
6. **支持两种基准**：连续帧（`t vs t-1`，驱动状态迁移）与锚点帧（`t vs entry`，回答"相对建仓时变了什么"，§44）。
7. **归因必须落到证据 id**：`thesis_delta` 是归因的**可读投影**，不是 LLM 事后自由发挥的散文；先有结构化归因，再有叙述。

---

## 2. 五层变化结构（CompanyStateArtifact 分层）

```text
CompanyStateArtifact
 ├─ L1 数据层   factor_snapshot（七因子原始值 + missing_facts）   ← 根因
 ├─ L2 评分层   variable_scores（七方向分）                        ← L1 的压缩
 ├─ L3 状态层   state（StateBelief 软分布）+ thesis_confidence     ← L2 的模式翻译
 ├─ L4 赔率层   expected_value（scenarios / EV / RAEV）            ← L3 + V/E
 └─ L5 仓位层   position（exposure / stock_weight / band）         ← L3+L4+M+组合
```

diff 按这五层组织；**归因方向自下而上**（L1 的字段变化解释 L3–L5 的决策变化），
消费方向自上而下（先看 L5 仓位要不要动，再下钻 L3 为什么状态漂了，最后 L1 哪个字段变了）。

---

## 3. 具体模型定义（typed contracts）

```python
# ── 快照引用（不内嵌，反漂移）────────────────────────────────
class ArtifactRef(BaseModel):
    id: str                  # CompanyStateArtifact 持久化 id
    date: str                # YYYY-MM-DD
    symbol: str = ""

# ── L1 字段级变化（最小粒度）────────────────────────────────
class FieldDelta(BaseModel):
    field: str               # canonical 字段名
    prev: float | None
    curr: float | None
    delta: float | None      # 绝对差 curr - prev（单位随 canonical）
    rel_delta: float | None  # 相对差 curr/prev - 1（跨量纲可比）
    change: str              # appeared | disappeared | up | down | unchanged

# ── L1 因子级变化（聚合）────────────────────────────────────
class FactorDelta(BaseModel):
    factor: str              # F / E / T / V / C / R / M
    direction: str           # improving | neutral | deteriorating
    fields: list[FieldDelta] = []          # 仅含发生变化的字段
    consistency: str = ""    # resonant | divergent | independent（与其它因子）

# ── L3 软状态漂移 ───────────────────────────────────────────
class StateShift(BaseModel):
    argmax_from: str | None  # 首帧为 None
    argmax_to: str
    mass_delta: dict[str, float]          # {S_k: P_curr − P_prev} 质量流动
    tv_distance: float       # 0.5·Σ|ΔP|，信念位移总量 0-1
    entropy_from: float | None
    entropy_to: float
    entropy_delta: float | None           # 变确定（负）/ 变模糊（正）
    drift: dict[str, float] | None        # 质量流向（复用 classifier._drift 口径）
    legal: bool              # 迁移合法性（classifier 判定）
    kind: str                # upgrade | downgrade | same | reopen

# ── 置信度变化（L3，与状态漂移正交）─────────────────────────
class ConfidenceDelta(BaseModel):
    prior: float | None
    posterior: float | None
    delta: float | None      # posterior − prior
    log_odds_delta: float | None          # logit(posterior) − logit(prior)
    evidence_ids: list[str] = []          # 驱动变化的 EvidenceEvent id

# ── L4 赔率变化 ─────────────────────────────────────────────
class EVDiff(BaseModel):
    ev_from: float | None
    ev_to: float | None
    ev_delta: float | None
    risk_adjusted_ev_delta: float | None
    scenario_probability_delta: dict[str, float] = {}   # {bull/base/bear: ΔP}
    scenario_return_delta: dict[str, float | None] = {} # {bull/base/bear: ΔR}
    probability_source_change: str = ""   # 如 state_prior → bayes_posterior

# ── L5 仓位变化 ─────────────────────────────────────────────
class PositionDiff(BaseModel):
    stock_weight_delta: float | None
    actual_weight_delta: float | None
    exposure_delta: float | None          # M 的市场暴露变化
    band_from: str = ""
    band_to: str = ""
    band_weight_divergence: bool = False  # ⚠️ 标签与实际仓位背离（实跑已踩坑）
    drivers: list[str] = []               # 归因：state / confidence / ev / market / portfolio

# ── 归因（为什么变）─────────────────────────────────────────
class ChangeAttribution(BaseModel):
    evidence_ids: list[str] = []          # 触发变化的证据
    factor_deltas: list[str] = []         # 受影响的因子（如 ["E","F"]）
    decision_effects: list[str] = []      # 决策层影响（如 ["state:S2→S3","confidence:+0.2"]）
    note: str = ""                        # 一句话因果解释

# ── 主模型：CompanyStateDiff（升级现有 SnapshotDiff）────────
class CompanyStateDiff(BaseModel):
    symbol: str
    prev: ArtifactRef | None              # 首帧为 None
    curr: ArtifactRef
    anchor: ArtifactRef | None = None     # 建仓锚点（可选，§44）
    is_first: bool = False
    elapsed_days: int

    # 五层变化（首帧时各层按"基线登记"处理）
    factors: list[FactorDelta] = []                  # L1
    scores: dict[str, float | None] = {}             # L2：canonical 方向分名 → Δ
    state_shift: StateShift | None = None            # L3
    confidence: ConfidenceDelta | None = None        # L3'
    ev: EVDiff | None = None                         # L4
    position: PositionDiff | None = None             # L5

    # 证据与归因
    new_evidence: list[EvidenceEvent] = []
    attribution: list[ChangeAttribution] = []
    thesis_delta: str = ""                # 可读总结（由 attribution 投影 / LLM 润色）

    # 退出检查（§37 ExitEngine）
    exit_conditions_met: list[str] = []   # 新满足的退出条件 kind

    # 元信息
    degraded_flip: str = ""               # 降级状态翻转（False→True / True→False）说明
    missing_appeared: list[str] = []      # 新出现（数据源补齐）的字段
    missing_disappeared: list[str] = []   # 新缺失（降级/覆盖消失）的字段
```

> 与现有 `SnapshotDiff` 的关系：`SnapshotDiff`（薄壳）**升级替换**为 `CompanyStateDiff`；
> 原字段 `state_from/state_to` 由 `StateShift.argmax_from/argmax_to` 承载，
> `variable_deltas` 由 `scores` + `factors[].fields` 分层承载，`evidence_changed` 由
> `new_evidence` + `attribution[].evidence_ids` 承载。

---

## 4. 计算规则（diff 引擎 `diff.py` 伪代码，含 LLM 边界标注）

> 标注约定：`【纯规则】` = 确定性计算，禁 LLM；`【规则+LLM】` = 规则算数值、LLM 做语义/叙述；
> `【LLM 必需】` = 无 LLM 该步只能降级。完整分类表见 §12。

```text
diff(prev: CompanyStateArtifact | None, curr: CompanyStateArtifact,
     anchor: CompanyStateArtifact | None = None) -> CompanyStateDiff:

  0. 校验：同 symbol、同 schema_version；prev 存在时 curr.date > prev.date    【纯规则】
  1. prev is None → is_first=True：                                          【纯规则】
     所有字段 change="appeared"（基线登记）；state_shift=None；不产归因
  2. L1 因子差：                                                             【纯规则】
     for factor in [F,E,T,V,C,R,M]:
         for field in canonical_fields(factor):          # factors.py 的 *_FIELDS 元组
             Δ = curr − prev（None 语义：appeared/disappeared/unchanged）
         direction = 聚合关键字段的 rel_delta 方向
         consistency = 与其它因子 direction 同向？resonant : divergent
  3. L2 评分差：                                                             【纯规则】
     scores[name] = curr.variable_scores[name] − prev.variable_scores[name]（数值项）
  4. L3 状态漂移：                                                           【纯规则】
     mass_delta = {S_k: P_curr − P_prev}
     tv_distance = 0.5 · Σ|mass_delta|
     argmax/entropy 差；drift 复用 classifier._drift
     legal = classifier 判定（S1→S3 非法跳级等）
  5. L3' 置信度差：                                                          【纯规则】
     Δ = posterior − prior；log_odds_delta = logit 差
     evidence_ids = 本窗口新增 EvidenceEvent 的 id
     # ↑ 数值纯规则；但 EvidenceEvent 本身来自上游 LLM 抽取（§12.3-①）
  6. L4 赔率差：                                                             【纯规则】
     ΔEV、ΔRAEV、各情景 ΔP / ΔR、probability_source 变化
  7. L5 仓位差：                                                             【规则+LLM】
     Δstock_weight / Δactual_weight / Δexposure / band 变化
     band_weight_divergence = (band 未变 且 |Δactual_weight| > 阈值)
                           或 (band 升/降 与 weight 变化方向相反)
     drivers = 数值分解：state / confidence / market / ev / portfolio
     # ↑ 数值分解纯规则；"为什么减仓"的语义叙述 → LLM 增强（§12.3-④）
  8. 证据：new_evidence = curr.evidence_log − prev.evidence_log（按 id 差集）  【纯规则】
     # ↑ 集合差纯规则；EvidenceEvent 生成在上游 → LLM 必需（§12.3-①）
  9. 归因（§5）：候选生成+排序=规则；因果确认+note=LLM 必需，模板兜底           【LLM 必需】
 10. 退出检查：exit_conditions_met = curr.exit_conditions 中 met=True 且 prev 中未 met 的 kind
     # ↑ 数值类 stop（revision/state/price）=纯规则；
     #   thesis_broken / evidence 语义判断 = LLM 必需（§12.3-③）              【LLM 必需】
 11. degraded_flip / missing_appeared / missing_disappeared（对比 missing_facts 差集） 【纯规则】
```

---

## 5. 归因机制：为什么变（三层因果链）

diff 的价值 80% 在归因。规则如下：

```text
┌ 证据层   new_evidence（本窗口新增 EvidenceEvent）
│            │ effect_on_thesis / confidence_delta
├ 因子层   FactorDelta（L1 字段变化，|rel_delta| 超过阈值的因子入选）
│            │ 方向一致性
├ 决策层   StateShift.drift（质量往哪流）· ConfidenceDelta · PositionDiff.drivers
│
└ 输出     ChangeAttribution：
           evidence_ids → factor_deltas → decision_effects → note
```

**匹配规则**：
- `ConfidenceDelta.evidence_ids` 直接取 `EvidenceEvent.confidence_delta` 的贡献者（bayes log-odds 的加法性保证可归因）；
- `StateShift` 的 `drift`（质量流向 S2→S3）与因子方向比对：与 drift 同向且 `|rel_delta|` 大的因子 = 状态漂移的 `trigger_factors`；
- `PositionDiff.drivers` 按轴分解：`state`（band 变化）/ `confidence`（Δconf）/ `market`（Δexposure）/ `ev`（ΔRAEV）/ `portfolio`（组合调整）；
- `thesis_delta` = 归因的投影：先结构化，再叙述。

**规则/LLM 分工**（§12.3-②）：
- 以上四条匹配全部是**纯规则**（数值同向比对 + 集合运算），产出的是**候选**归因；
- `ChangeAttribution.note` 的**因果确认与叙述是 LLM 必需**：多个因子同时动时，哪个是"因"哪个是"果"
  （如"revision 上修是因为财报超预期" vs "因为行业事件"）、一次性 vs 经常性——规则只能排序候选，给不出因果判断；
- LLM 失败时降级为**模板 note**（如 `"E 上修 +45% 驱动 S2→S3"`），diff 不中断。

**对齐 §35 的示例**：

```text
State S1→S2，原因 +Q2收入 +毛利率 +EPS上修8%，负面 -海外需求弱，Confidence 58→72

→ factors:    F(improving: revenue_yoy/gross_margin up)、E(improving: revision_1m +8%)
→ state_shift: argmax S1→S2, drift 质量流向 S2, kind=upgrade
→ confidence: 58→72 (+14)，evidence_ids=[e1,e2,e3]
→ attribution.note: "Q2 收入与毛利率证据(e1,e2) + EPS 上修(e3) 驱动状态迁移与置信度上升；海外需求弱为反向证据"
→ thesis_delta: 同 note（或 LLM 润色后）
```

---

## 6. 速度与加速度（diff 序列层，不落在单帧内）

单帧 diff 只存**原始量**（`elapsed_days`、`tv_distance`、`len(new_evidence)`）；速度/加速度是
diff 序列的派生量（加速度天然需要第三帧）：

```text
belief_velocity        = tv_distance / elapsed_days        # 信念位移速度
evidence_arrival_rate  = len(new_evidence) / elapsed_days  # 证据到达率（§30 的真正含义）
belief_acceleration    = velocity_t − velocity_{t-1}       # 需要 diff 历史
```

实现层：`diff.py` 提供 `diff_series(diffs: list[CompanyStateDiff]) -> list[DiffVelocity]`，
**不把 velocity 硬存在单帧 diff 里**（避免单帧自指）。

---

## 7. 边界情况

| 情况 | 处理 |
|------|------|
| **首帧**（无 prev） | `is_first=True`，全部字段 `appeared`（基线登记），不产归因/exit 检查 |
| **数据出现/消失** | `appeared`/`disappeared`，与 `up/down` 严格区分；`missing_appeared/disappeared` 单独成列 |
| **无变化**（两帧几乎相同） | 仍产出一帧 diff：`factors=[]`、`tv_distance≈0`——**稳定性是信息**（S3 持有阶段的"无变化"正是信号） |
| **降级翻转** | `degraded_flip` 显式记录；降级前后字段以 `disappeared/appeared` 呈现，不参与归因 |
| **band 与仓位背离** | `PositionDiff.band_weight_divergence=True`（实跑已踩：band="核心" 但 actual_weight=0.6%） |
| **软状态熵高** | `tv_distance` 大但 `entropy_delta≈0` = 信念整体平移；`entropy_delta>0` = 越来越拿不准——两者含义不同，须分开 |
| **argmax 不变但质量漂移** | `argmax_from==argmax_to` 但 `tv_distance>0`（如 S2:0.6→0.8 的"半只脚进 S3"）——argmax 相同不表示没变 |

---

## 8. 实跑（浪潮信息）暴露的问题如何被 diff 捕获

把上一轮实跑发现的坑写进设计，diff 引擎必须能暴露它们：

| 实跑问题 | diff 中的对应机制 |
|---------|------------------|
| `factor_snapshot.state="S0"` 与顶层 `argmax_state="S3"` 矛盾 | diff 只比较 `StateBelief`（权威），并校验 `factor_snapshot.state` 是否与之一致（不一致记 warning） |
| `position_band="核心"` 但 `actual_weight=0.6%` | `PositionDiff.band_weight_divergence` |
| `e_revision=1.0` 饱和丢失幅度 | L1 字段差比较**原始值**（`eps_fy1_revision_1m` 的 Δ），不比较被 clip 的方向分 |
| 18 项 missing（sw_daily 无权限等） | `missing_appeared/disappeared` + `degraded_flip` 跟踪数据源可用性变化 |
| F 只看增长不看现金质量 | `FactorDelta.fields` 里 `operating_cashflow` 的负 delta 与 `net_profit_yoy` 的正 delta 并存 → `consistency=divergent`（因子内部背离） |

---

## 9. 消费方（diff 的下游，防 dead-end）

| 消费方 | 用 diff 的什么 | 效果 |
|--------|---------------|------|
| **ExitEngine**（§37） | `exit_conditions_met` + `StateShift.kind=downgrade` + `PositionDiff` | 五层退出检查：Thesis/Evidence/Revision/State/Price-Risk |
| **决策日志**（`DecisionJournalEntry`） | `thesis_delta` + `attribution` | "当时为什么动/不动"，事后复盘可回放 |
| **报告叙事** | `thesis_delta` + `attribution.note` | §35 要求的"为什么变"，替代"当前评分78" |
| **监控触发器**（§36 EVI） | `tv_distance` / `evidence_arrival_rate` 超阈值 | 高信息量变化 → 触发深度研究 |
| **锚点 diff**（§44） | 同一引擎、`prev=entry` | 回答"相对建仓时证据/预期/赔率变了什么" |

---

## 10. 与现有代码对齐（落地改动点）

| 现状 | 改动 |
|------|------|
| `models.py::SnapshotDiff`（薄壳 6 字段） | **升级替换**为 `CompanyStateDiff` + 子模型（`StateShift`/`ConfidenceDelta`/`EVDiff`/`PositionDiff`/`FieldDelta`/`FactorDelta`/`ChangeAttribution`/`ArtifactRef`） |
| 无 diff 引擎 | 新增 `alphabee/midterm/diff.py`：`diff(prev, curr) -> CompanyStateDiff`（纯函数）+ `diff_series()`（velocity/acceleration） |
| 无快照持久化 | 新增 `alphabee/midterm/persistence.py`：`CompanyStateArtifact` append-only 落盘（对齐 `market_regime/persistence.py` 模式），diff 的 `ArtifactRef` 引用它 |
| `classifier._drift` 已存在 | 复用为 `StateShift.drift` 的计算口径 |
| `factors.py::_*_FIELDS` 元组已存在 | 复用为 diff 的字段对齐清单 |

---

## 11. 落地分期

| 期 | 内容 | 依赖 |
|----|------|------|
| D1 | `CompanyStateDiff` 等 typed contracts（升级 `SnapshotDiff`）+ 单测 | 无 |
| D2 | `diff.py` 纯函数引擎（L1–L5 分层差 + 归因 + 边界）+ 单测 | D1 |
| D3 | `persistence.py`（CompanyStateArtifact 落盘）+ `diff_series`（velocity/acceleration） | D2 |
| D4 | 消费方接线：ExitEngine 检查、决策日志、报告叙事投影 | D3 |

---

## 12. LLM 边界标注（全流程）

### 12.1 总原则（延续 MIDTERM_DECISION_MODEL 的 LLM 边界结论）

- diff 的**数值计算核心**（字段差 / TV 距离 / 熵差 / log-odds / EV 差 / 权重差 / 速度）**全部纯规则，禁 LLM**；
- LLM 只站在两个边界：**上游**（财报/研报文本 → `EvidenceEvent`）与**下游**（结构化归因 → 可读叙述）；
- 中间有两处**语义判断必须 LLM**（归因的因果确认、thesis_broken 退出判定），但都要求**规则兜底**：
  LLM 失败时退模板 / 退保守，diff 不中断。

### 12.2 全流程逐步骤分类表

| 步骤 | 计算 | LLM 需求 | 失败降级 |
|------|------|---------|---------|
| 0 校验 / 1 首帧 / 11 missing 差集 | 规则 | 🚫 禁 | — |
| 2 L1 字段差 / 3 L2 评分差 | 规则 | 🚫 禁（数值幻觉高危区） | — |
| 4 L3 状态漂移（TV/熵/drift） | 规则 | 🚫 禁 | — |
| 5 L3' 置信度差 | 规则 | 🚫 禁；但其 `evidence_ids` 的原料在上游需 LLM | — |
| 6 L4 赔率差 | 规则 | 🚫 禁 | — |
| 7 L5 仓位差 | 规则（drivers 数值分解） | 🔶 增强：叙述 | 无叙述也可 |
| 8 证据差集 | 规则（集合运算） | 🔶 上游 LLM 必需（EvidenceEvent 生成） | `evidence=[]` |
| 9 归因 | 规则（候选+排序） | 🔴 **LLM 必需**（因果确认+note） | 模板 note |
| 10 退出检查 | 规则（数值类 stop） | 🔴 **LLM 必需**（thesis_broken 语义） | 保守→转研究 |
| thesis_delta | 规则（投影） | 🔶 增强：润色 | 模板句 |
| velocity/acceleration | 规则 | 🚫 禁 | — |

### 12.3 LLM 调用点清单（typed 契约 + 降级）

| # | 调用点 | 输入 | 输出（typed） | 必需性 | 降级 |
|---|--------|------|--------------|--------|------|
| ① | EvidenceEvent 抽取（上游） | 窗口内财报/研报/公告文本 | `list[EvidenceEvent]` | **LLM 必需**（否则 Confidence 轴空转） | `evidence=[]`，conf 差 None |
| ② | 归因因果确认 + note | 规则产出的候选归因 | `ChangeAttribution.note` + `thesis_delta` | **LLM 必需** | 模板 note |
| ③ | thesis_broken 退出判定 | 新证据 + 假设 H + invalidation | `exit_conditions_met` 追加项 | **LLM 必需** | 保守：转 ResearchTask 不直接退出 |
| ④ | 仓位/背离的语义叙述 | `PositionDiff` + `consistency=divergent` 的因子 | 可读解释 + 研究问题（"市场知道什么我不知道"，§20） | LLM 增强 | 无 |
| ⑤ | 锚点 diff 定性总结（§44） | 锚点 diff 的结构化 delta | "相对建仓时 thesis 是否仍成立" 的定性判断 | LLM 增强 | 无 |

### 12.4 LLM 禁区（红线，违反即破坏可回测性）

- 任何数值 Δ 计算（字段差 / TV / 熵 / log-odds / EV / 权重 / 速度）；
- `appeared / disappeared / unchanged` 分类；
- `drivers` 数值分解与 `band_weight_divergence` 判定；
- 退出条件的数值阈值判断。

---

## 附：关键设计决策速查

1. **分层求差**：L1 数据 → L2 评分 → L3 状态/置信 → L4 赔率 → L5 仓位，五层正交，不压平。
2. **软状态漂移**：`tv_distance` + `mass_delta` + `entropy_delta`，argmax 只是投影之一。
3. **归因链**：`evidence → factor → decision`，先结构化后叙事；`thesis_delta` 是投影不是散文。
4. **两种基准**：连续帧（t-1）与锚点帧（entry）共用同一引擎（§44）。
5. **首帧=基线登记**：全部 `appeared`，不产归因。
6. **单帧存原始量**：velocity/acceleration 由 diff 序列派生，不落单帧。
7. **diff 是出口的守门员**：`exit_conditions_met` + `band_weight_divergence` + `degraded_flip` 是 diff 必须主动暴露的异常。
8. **LLM 只在边界**：数值核心全规则；LLM 三处必需（证据抽取①/归因②/thesis_broken③）+ 两处增强（叙述④/锚点总结⑤），全部带规则兜底（§12）。
