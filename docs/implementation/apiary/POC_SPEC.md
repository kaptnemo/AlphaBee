# Apiary POC 实施规范：利润/毛利率变化的可持续性

> 状态：实施规范（待评审，未实施）
> 定位：Apiary 架构的窄范围概念验证（POC）。只回答"双路径 + 能力沉淀是否比现有系统更有帮助"，不建设全量能力工厂。
> 关联：`docs/design/APIARY_RESEARCH_OS_DESIGN.md` §8.1（MVA）、§9.4（A/B/C/D）、§13.4（修订后的最小验证）。
> 纪律：本文含文件清单、接口签名、测试矩阵、验收标准与回滚；POC 代码**不得改动生产主图**。

---

## 0. 结论先行

- **范围**：一个研究主题、3 个确定性能力、1 个 Scout、1 个方法卡、6 个案例、A/B/C/D 四组评估。
- **周期**：6–8 周（单开发者 + Agent；本仓 DoD 含测试/变异/冻结，慢于普通原型）。
- **主验收**：验收案例上，D/C 组的论证质量优于 A 组，事实错误不增加，总成本可接受。
- **副验收**：Scout 能发现 pipeline 盲区；方法卡在 held-out 案例上可复用；3 个能力被 ≥2 个案例使用。
- **非指标**：能力数量、问题数量、报告长度、请求收敛速度。
- **失败即停**：无质量改善 / 总成本 > 3× 基线 / Scout 无盲区 / 数据不可识别。

---

## 1. 研究主题

### 1.1 主问题

> 公司近期的利润/毛利率变化，是**可持续的**，还是**一次性/节奏性的**？

### 1.2 子问题

1. 收入、毛利率、费用各贡献多少？
2. 分部结构变化能否解释毛利率变化？
3. 一次性损益/非经常性损益占比多少？
4. 现金转换是否支持利润（OCF/净利、应收、存货）？
5. 同业同口径对比是否支持"公司特有"？
6. 下一期什么数据能判别？证伪条件是什么？

### 1.3 决策用途

- 改变或确认"持续性"判断；
- 形成观察清单与证伪条件；
- **不**直接给买卖建议或目标价。

### 1.4 非目标

- 不做完整估值/仓位；
- 不做全行业覆盖；
- 不做全量快照 / bitemporal；
- 不做 Apiary 全自动（五角色自动化不在 POC 内）；
- 不做 C/D 类黄金帧、多智能体辩论、planner。

---

## 2. 案例集

### 2.1 选择原则

- **6 个案例**：3 个开发（dev）+ 3 个验收（acceptance）。
- 覆盖 2–3 个行业，避免单一行业偏差。
- 优先数据可得；至少 1 个有已知口径问题、1 个毛利率稳定、1 个毛利率波动。
- **验收案例不参与 prompt/能力迭代**；W1 冻结后不得替换（除非数据不可用，需记录并重跑全部 arm）。

### 2.2 候选案例

| case | 标的 | 行业 | 用途 | 选择理由 | 数据状态 |
|---|---|---|---|---|---|
| C1 | 600760.SH 中航沈飞 | 军工 | dev | 已有 81KB 报告 + 1 帧；毛利率争议明确 | 待 W1 审计 |
| C2 | 002916.SZ 深南电路 | PCB | dev | ROADMAP 有真实交叉核对记录；peer 口径问题典型 | 待 W1 审计 |
| C3 | 300760.SZ 迈瑞医疗 | 医疗器械 | dev | 有 thesis_versions；毛利率稳定对照 | 待 W1 审计 |
| C4 | 601899.SH 紫金矿业 | 有色 | acceptance | 周期股，利润波动大 | 待 W1 审计 |
| C5 | 603259.SH 药明康德 | CXO | acceptance | 一次性/非经常性典型 | 待 W1 审计 |
| C6 | 002517.SZ 恺英网络 | 游戏 | acceptance | 已有 task records / 中期样例 | 待 W1 审计 |

**候选替换池**（W1 数据审计不通过时使用）：`600183.SH 生益科技`、`002415.SZ 海康威视`、`002436.SZ 兴森科技`。

> 最终案例表在 W1 结束前冻结，写入 `data/research/poc/cases/INDEX.md`；之后不得因结果不理想而替换。

### 2.3 数据截止与防泄露

每个案例记录：

```text
case_id
symbol
as_of                   # 决策时点（研究截止时点），不是财报报告期
data_cutoff             # 允许使用的最新数据时点
data_snapshot_id        # 本次冻结的输入包（不可变）
capability_release      # 实际执行的能力版本
manifest_hash           # 本次看到的能力清单
research_config_version
model_and_prompt_version
reconstructed           # true = 无真实快照，尽力重建
```

- 输入冻结到 `data/research/poc/cases/<case>/<as_of>/`（facts / artifacts / report / source refs）。
- POC 阶段不做完整 bitemporal；`reconstructed=true` 的案例只做"尽力点时"，不得用于严格校准结论。
- 禁止使用 `data_cutoff` 之后的数据；LLM 的历史知识泄露只能记录、不能完全消除。
- 所有 arm 使用同一份冻结输入、同一 `capability_release` 与 `manifest_hash`。
- `as_of` 必须落实"当时已公开"；仅固定财报报告期不能避免未来信息泄漏。

### 2.4 开发/验收隔离

| 集合 | 用途 | 纪律 |
|---|---|---|
| dev（C1–C3） | 调 prompt、能力、brief 模板 | 可反复跑 |
| acceptance（C4–C6） | 最终评估 | W6 才首次运行；不参与调参；结果不改 rubric |

---

## 3. 三个能力

> 三个能力都必须是**确定性函数**；LLM 不得参与数值计算。每个能力卡带 `applicability / min_data / identification / output_nature`。

### 3.1 `margin.segment_decompose`

**目的**：把毛利额/毛利率变化分解为收入、分部结构、分部毛利率与残差。

**输入**：

```text
income: 两期营业收入、毛利/毛利率
segments: 两期分部 revenue / cost / gross_margin / share（可选）
period: 报告期
```

**输出**：

```text
revenue_effect
segment_mix_effect
segment_margin_effect
residual
identification: exact | partial | not_identifiable
output_nature: deterministic | estimate | cannot_answer
reasons: list[str]
```

**算法（POC 最小版）**：

1. 无分部数据时，只做合并口径：
   `ΔGP = (Rev1 − Rev0) × GM0 + Rev1 × (GM1 − GM0)`
   输出 `revenue_effect` 与 `margin_effect`，`identification=partial`。
2. 有同报告期分部 revenue + gross_margin 时（`w` = 分部收入占比，效果以毛利率 pp 表示）：
   - `mix = Σ (w1_i − w0_i) × GM0_i`
   - `margin = Σ w1_i × (GM1_i − GM0_i)`
   - `residual = (GM1 − GM0) − mix − margin`（口径差异/四舍五入）
   - `identification=partial`（仍不能分离销量/单价）。
3. 分部期间与财报期间不一致、缺 cost 且缺 gross_margin、或分部覆盖不足 → `not_identifiable`，写明缺什么。

**单位纪律**：合并口径的 effect 用 **CNY**；分部口径的 effect 用**毛利率 pp**；两者不得混用，输出必须带 `unit` 字段。

**明确不做**：`volume_effect / price_effect`。没有销量、单价、单位成本时，这两个不可识别。

**测试**：

- 合成：收入与毛利率同向/反向；分部 mix 变化；零收入；缺分部。
- 真实：600760.SH 的 9M2025→9M2026；预期 `-8.3 亿 / -2.4 亿` 量级（以实际数据为准）。
- 变异：符号翻转、缺数据仍输出、期间错配未拒绝 → 必红。

**验收**：3 个案例跑通；缺数据时必须返回 `not_identifiable`；所有数字可从输入复算。

### 3.2 `data.caliber_check`

**目的**：在写 claim 之前，先检查事实的口径、时点、修订、单位、派生/直报、跨源冲突。

**输入**：facts bundle + 每条 fact 的 `source / ann_date / update_flag / period / unit / derived`。

**检查项**：

| code | 检查 | 严重度 |
|---|---|---|
| `period_mismatch` | 财报期 / 行情期 / 分部期不一致 | high |
| `stale` | `as_of` 超过 `stale_after` | medium |
| `revised` | 同报告期存在更新修订（`update_flag` 变化） | medium |
| `derived_vs_reported` | 派生值与直报值冲突 | high |
| `unit_mismatch` | 元/万元/亿元/百分比混用 | high |
| `cross_source_conflict` | 两个来源同指标不一致 | high |
| `missing_source` | 关键数字无来源 | medium |

**输出**：`list[CaliberFinding]`（code / severity / message / evidence_ref / suggested_action）。

**纪律**：纯确定性；不调用 LLM；不"修复"数据，只报告。

**测试**：

- 已知历史缺陷做 fixture：市值万元/亿元、应收同比口径、折旧/收入 0.0、peer 数据 stale。
- 修订：同报告期 `update_flag` 变化 → `revised`。
- 期间错配：财报 20260930 + 行情 20261030 混用 → `period_mismatch`。

**验收**：能在 600760 报告里复现至少 3 个已知口径问题；误报可解释。

### 3.3 `peer.same_caliber_compare`

**目的**：同口径 peer 对比，明确 stale / 缺数 / 单成分偏差。

**输入**：target facts、peer group、同报告期、peer benchmarks。

**输出**：表格（target / peer / median / diff）、`stale`、`coverage`、`peer_confidence`、`missing`。

**纪律**：

- 同期间、同单位、同指标；
- 不复用 `peer_revenue_yoy=-2.18` 这类已被验证为口径缺陷的值；
- 单成分/高极差时降级置信度；
- 不做 LLM 算术。

**测试**：

- 002916.SZ 已知 peer 组；
- stale peer（as_of 早于 target）；
- 单成分主导 → 置信度降级；
- 缺 peer → 返回 `coverage=0`，不编造。

**验收**：3 个案例出表；每个数字可追溯；stale/缺数显式。

---

## 4. 方法卡

### 4.1 `method.profit_sustainability`

```yaml
id: method.profit_sustainability
name: 利润/毛利率变化的可持续性判断
applicability: 有至少两期财务数据；收入确认或产品结构可能造成节奏性波动
inputs: [income_statement, segment_data, cashflow, peer_benchmarks]
procedure:
  - 同口径确认（期间/单位/修订）
  - 收入/毛利率/费用分解（调用 margin.segment_decompose）
  - 分部结构与一次性项检查
  - 现金转换检查（OCF/净利、应收、存货）
  - 同业同口径对比（调用 peer.same_caliber_compare）
  - 下一期判别条件与证伪条件
failure_modes:
  - 把季节性当趋势
  - 用累计口径掩盖单季变化
  - 用同业共性解释公司特有变化
  - 用一次性收益/损失解释可持续性
  - 用未来数据回看（lookahead）
validation: 在 held-out 案例上比较"可持续/不可持续/待定"判断与事后一期数据
cases: []
status: draft
```

### 4.2 方法卡输出

```text
SustainabilityVerdict
  direction: sustainable | one_off | cyclical | cannot_judge
  confidence: low | medium | high
  evidence_refs[]
  strongest_counter
  falsifier: {observable, threshold, time_window, source}
  watchlist[]
```

### 4.3 验证方式

- 在 acceptance 案例上，用"下一期实际数据"做事后检验；
- 记录判断正确/错误/无法判断；
- 不做统计显著性宣称；n=3 只做案例对照；
- 方法卡不通过单测替代；必须有案例验证记录。

---

## 5. Scout

**归属**：Scout 在 **AlphaBee 运行时**执行；Apiary 不拥有 Scout 执行环境，只接收问题、盲区与数据缺口。POC 阶段 Scout 代码在 AlphaBee 仓，Apiary 的评估脚本通过 API/CLI 调用。

### 5.1 执行协议

```text
1. 盲读：只读原始数据 + 标的背景 + 主题；不读 pipeline artifacts
2. 落稿：写 ScoutMemo（问题、初步解释、反证、证据需求、数据缺口）
3. 对账：读 pipeline brief/artifacts，做针对性挑战
4. 产出：盲区、分歧、下一轮问题、数据缺口
```

### 5.2 输入/输出

**输入**：case snapshot（原始数据）、主题、研究 taxonomy、时间/预算上限。

**输出**：

```text
ScoutMemo
  questions[]        # 10–15 条，带 decision_impact
  claims[]           # 初步解释 + 证据需求
  counter_evidence[]
  data_gaps[]
  blind_spots[]
  confidence
  used_artifacts[]   # 盲读阶段必须为空
```

### 5.3 策略与预算

- POC 只做一个策略：**Top-down + Contrarian**（先立假设，再找反证）。
- 预算：≤15 个问题、≤2 轮、≤20 分钟、token 上限由配置；超限返回部分结果。
- 独立性：同模型同数据会产生相关错误；用不同 prompt/策略降低相关性，记录 `model_id/prompt_hash`。
- 禁止：盲读阶段读 pipeline artifacts；写生产数据；调用未授权工具。

### 5.4 测试

- 盲读阶段用 monkeypatch/spy 验证**没有**读 `pipeline_artifacts`；
- 对账阶段验证**读了**；
- 超预算返回部分结果，不抛；
- 输出 schema 校验。

---

## 6. Claim 账本与对账

### 6.1 最小 Claim schema

```text
claim_id
text
claim_type            # fact / causal / comparative / predictive / judgment
materiality
evidence_refs[]
counter_refs[]
rival_explanations[]  # causal/comparative/predictive 至少 2；fact 不强制
warrant
qualifier
falsification[]       # observable / threshold / time_window / source / gray_zone
status
confidence
source_arm            # A/B/C/D，评估时用于剥离
```

### 6.2 每案例要求

- 3–5 条 claim，其中至少 1 条核心 claim、1 条最强反证；
- 每条 material claim 有 evidence_ref 或显式 `inferred`；
- 核心 claim 有 falsification（四字段齐全）。

### 6.3 对账输出

```text
Reconciliation
  claim_diff[]        # agree / disagree / partial + 理由
  blind_spots[]       # pipeline 没查的维度/证据
  data_conflicts[]    # 口径/来源冲突
  open_questions[]
  next_questions[]    # 按信息量排序
```

不自动合并；高影响分歧由人工裁决。

---

## 7. Brief 模板

所有 arm 使用**同一个** brief 模板与渲染器，避免"报告 vs 简报"不可比。

```text
【研究简报】<symbol> <name>
问题：<question>
答案（置信度）：<direction + confidence>
当前深度：L1–L6；下一层需要：<data>

Claim 1..3–5：
  主张 / 依据 / 推理 / 反证 / 限定 / 失效条件

最强反证：
数据 caveat：
覆盖地图：已查 / 部分 / 未查 / 数据不足 / 不重要
下一轮 3 个问题：
观察清单：
```

- 主文目标 1,500–2,500 字；长证据进附录（POC 可只存 JSON，不渲染）。
- 数字必须来自能力或 facts；LLM 不得自算。
- 所有 arm 用同一 `render_brief()`；差异只来自证据/claim。

---

## 8. A/B/C/D 评估

### 8.1 四个 arm

| arm | 配置 | 隔离的变量 |
|---|---|---|
| **A** | 现有 pipeline 的产物 → 同一 brief 模板 | 基线 |
| **B** | pipeline + 同等预算的额外研究（同模型、更多检索/时间，**无 Scout、无新能力**） | 单纯增加算力 |
| **C** | pipeline + 独立 Scout（盲读→对账） | 双路径组织 |
| **D** | C + 3 个新能力 | 能力沉淀 |

**控制**：同一数据截止、同一模型、同一总预算（token + 时间）、同一 brief 渲染器、同一评分者。

**编排归属**：Apiary 的 `run_arm.py` 通过 AlphaBee API/CLI 发起运行；brief 由 AlphaBee 的 `render_brief()` 统一渲染；评估记录写 Apiary。

### 8.2 盲评 rubric

| 维度 | 1 分 | 3 分 | 5 分 |
|---|---|---|---|
| 证据覆盖 | 关键主张无证据 | 有直接证据但覆盖不全 | 多源/直接证据充分 |
| 引用支持 | 引用与主张无关 | 部分相关 | 精准支持 |
| 最强反证 | 无/弱反方 | 提及但未回应 | 明确回应并限定 |
| 竞争解释 | 无替代解释 | 有但无判别证据 | 有判别性证据 |
| 可证伪性 | 空泛/不可观测 | 有条件但不可判定 | 可观测+阈值+窗口+来源 |
| 内部一致 | 明显矛盾 | 有矛盾但已解释 | 完全一致 |
| 校准/限定 | 过度自信/绝对化 | 基本合理 | 与证据强度匹配 |
| 可行动性 | 无观察项 | 有但不可执行 | 看什么/何时变/什么情况下错 |

**事实错误**单独计数：数字错、期间错、单位错、引用不存在、逻辑矛盾。事实错误不进入均分，单独报告。

### 8.3 流程

1. **预注册**：W1 冻结案例、rubric、arm 定义、成功阈值。
2. **盲化**：剥掉 arm 标签，随机顺序；评分者不知道来源。
3. **评分**：每个案例 4 个 arm × 8 维；记录评分时间。
4. **分析**：配对描述统计；报告全部 3 个 acceptance 案例，不只报最好/最差。
5. **不做事后调参**：看到结果后不改 rubric/案例；如需改，重跑全部 arm 并记录。

### 8.4 决策规则（预注册建议）

| 结果 | 决策 |
|---|---|
| D > A 在 ≥2/3 验收案例（主质量维度），事实错误不增加，成本 ≤2× A | **Go**：进入下一阶段（方法库/评估扩展） |
| C > A 但 D ≈ C | **Pivot**：能力沉淀未产生增量；优先方法/Scout |
| C ≈ A 且 D ≈ A | **Stop**：双路径无额外价值 |
| 成本 >3× A 或事实错误增加 | **Stop/Pivot**：先修成本/质量 |

> n=3 不做显著性检验；结论是"是否值得继续"，不是"已证明有效"。

---

## 9. 6–8 周计划

| 周 | 任务 | 交付物 | 验收 |
|---|---|---|---|
| **W1** | 案例/数据审计；冻结案例、as_of、dev/acceptance；跑 dev A 基线；acceptance A 基线只生成并封存 | `cases/INDEX.md`、dev A 基线 | 6 个案例数据可用；A 基线可复现；acceptance 结果不看 |
| **W2** | 能力 2 `caliber_check` + 能力 3 `peer_compare` | 代码 + 单测 | 已知缺陷 fixture 全过；误报可解释 |
| **W3** | 能力 1 `margin.segment_decompose`；方法卡 v0 | 代码 + 单测 + 方法卡 | 缺数据返回 `not_identifiable`；600760 可复算 |
| **W4** | Scout 盲读/对账；claim 账本；reconciliation | Scout memo + claim ledger | 盲读不读 pipeline；对账有盲区 |
| **W5** | Brief 渲染器；dev 案例 A/B/C/D 试跑；修 bug | 4 arm briefs | 同模板、同预算、数字可追溯 |
| **W6** | acceptance 案例 A/B/C/D；盲评 | 评分表 | 盲化成功；事实错误单独记录 |
| **W7** | 分析、go/no-go、文档 | 评估报告 + 决策 memo | 规则明确；不事后改 rubric |
| **W8** | buffer / 修复 / 归档 | 最终交付 | 可回滚；无生产改动 |

**单开发者注意**：W2–W4 是瓶颈；如果数据审计发现 3 个案例不可用，立即缩为 4 个案例（2 dev + 2 acceptance），不延长时间线。

---

## 10. 代码结构与接口

### 10.1 POC 落点（两仓）

POC 阶段**不改生产主图**，按分仓边界放：

```text
# 仓库 A：AlphaBee（运行时 + 正式能力）
alphabee/
  research/
    __init__.py
    capabilities/
      margin_decompose.py
      caliber_check.py
      peer_compare.py
    scout/
      agent.py
      prompts.py
    reconcile.py
    brief.py
scripts/alphabee_poc/
  build_cases.py          # 冻结案例输入（数据归 AlphaBee）
tests/research/
data/research/poc/
  cases/<case>/<as_of>/{meta.json,inputs.json,artifacts/}
  runs/<run_id>/{brief.md,claims.jsonl,scout.json}

# 仓库 B：Apiary（控制面）
poc/
  client.py               # AlphaBee API/CLI 客户端
  run_arm.py              # 编排 A/B/C/D
  run_eval.py             # 盲评/汇总
  backlog/                # 缺口/立项记录
data/
  runs/<run_id>/eval.json # 评估记录（只写 Apiary 侧）

# 共享契约（AlphaBee 发布，Apiary 固定版本）
alphabee-contracts/
  models.py               # Claim / Brief / ScoutMemo / CaseSnapshot / Task / Manifest
  version.py
```

**若 W1 两仓初始化成本过高**：允许同一工作区用 `alphabee/` 与 `apiary/` 目录模拟两仓；但 import 边界、写权限、契约包按两仓执行，P1 再物理拆分。

**数据归属**：

- `data/research/poc/cases/` 与正式研究产物在 AlphaBee；
- Apiary 的 `poc/data/` 只放实验/评估记录，不写 AlphaBee 正式数据；
- 两边都不得直接读写对方内部表。

### 10.2 关键接口

```python
# AlphaBee（运行时）
def decompose_margin(income, segments, *, period: str) -> MarginDecomposition: ...
def check_caliber(facts, *, as_of: str) -> list[CaliberFinding]: ...
def compare_peers(target, peers, *, period: str) -> PeerComparison: ...
def run_scout(case: CaseSnapshot, *, blind: bool = True) -> ScoutMemo: ...
def reconcile(brief: Brief, scout: ScoutMemo) -> Reconciliation: ...
def render_brief(claims: list[Claim], *, question: str) -> str: ...

# Apiary（控制面）
def evaluate_run(run: EvalRecord) -> EvalSummary: ...
```

### 10.3 兼容与回滚

- **不改** `alphabee/orchestrator/agent.py`、`NODE_ORDER`、`OrchestratorState`、`gates.py`。
- **不改** `config.yaml` 生产段；POC 配置放 AlphaBee `scripts/alphabee_poc/config.yaml` 或 Apiary `poc/config.yaml`。
- Apiary **不 import** AlphaBee 内部模块；只依赖 `alphabee-contracts` + API/CLI。
- 新增文件只被 POC 脚本引用；生产入口不 import。
- 回滚 = 删除两仓新增目录/包（`alphabee/research/`、`scripts/alphabee_poc/`、`alphabee-contracts/`、`poc/`）；无生产影响。

---

## 11. 测试矩阵

| 层 | 测试 | 必过 |
|---|---|---|
| 能力 | 合成数据：符号/缺数/期间错配/零收入 | ✅ |
| 能力 | 真实 fixture：600760 / 002916 已知口径 | ✅ |
| 能力 | 变异：符号翻转、缺数据仍输出、期间错配未拒绝 | ✅ 必红 |
| Scout | 盲读不读 `pipeline_artifacts`（spy） | ✅ |
| Scout | 对账阶段读到 artifacts | ✅ |
| Scout | 超预算返回部分结果 | ✅ |
| Claim | schema 校验；material claim 无证据 → 拒 | ✅ |
| Brief | 同模板；数字全部可追溯 | ✅ |
| Eval | arm 分配、盲化、评分表 | ✅ |
| 契约 | `alphabee-contracts` 版本兼容；Apiary 不 import AlphaBee 内部模块（AST） | ✅ |
| 数据 | `data_snapshot_id/as_of/manifest_hash` 记录；无 `data_cutoff` 之后数据 | ✅ |
| 边界 | Apiary 停机 → AlphaBee `run_case` 仍可跑；AlphaBee 停机 → Apiary 仍可读写 backlog/既有 eval | ✅ |
| 回归 | 现有 `pytest` 不新增失败 | ✅ |

---

## 12. 验收与 go/no-go

### 12.1 主验收

1. acceptance 案例 3 个，A/B/C/D 全部跑完；
2. D 在 ≥2/3 案例上主质量优于 A；
3. 事实错误不增加；
4. 总成本（时间 + token + 人工）≤2× A；
5. 评分盲化、rubric 预注册、案例未替换。

### 12.2 副验收

- Scout 每案例至少 1 个 pipeline 未覆盖的盲区；
- 3 个能力至少被 2 个案例使用；
- 方法卡有 3 个案例的验证记录；
- 所有数字可追溯到 facts/能力，无 LLM 自算。

### 12.3 停止标准

- D ≈ A 或 C ≈ A；
- 成本 >3× A；
- 事实错误增加；
- 数据不可识别导致 3 个案例无法分解；
- Scout 盲区为 0。

### 12.4 决策

- **Go**：进入下一阶段（方法库 + 评估扩展 + 能力毕业试点）。
- **Pivot**：D ≈ C，但 C > A → 先补方法与评估，不扩能力。
- **Stop**：双路径无额外价值 → 回到 pipeline + 手动研究，保留方法卡/能力卡作为文档资产。

### 12.5 边界健康检查（分仓验收项）

- **Apiary 停机**：AlphaBee 仍能跑 pipeline + Scout + reconciliation + brief。POC 实测：停掉 Apiary 的 `run_arm.py`/`run_eval.py`，AlphaBee 的 `run_case` 正常完成。
- **AlphaBee 停机**：Apiary 仍能读/写 backlog、整理规格、查看既有 eval；新的研究执行等待。
- 若任一方向不成立，说明 Scout / reconciliation / brief 被放错仓，需回滚边界，而不是加兼容层。

---

## 13. 风险与缓解

| 风险 | 说明 | 缓解 |
|---|---|---|
| 数据不可识别 | 缺分部成本/销量/单价 | 能力声明 `not_identifiable`；不强行分解 |
| LLM 非确定 | 同输入不同输出 | 记录 model/prompt；必要时多次采样/冻结 |
| 同源相关错误 | Scout 与 pipeline 同模型同数据 | 盲读协议 + 不同策略 prompt；不把"一致"当独立验证 |
| 样本太小 | 6 个案例 | 只做案例对照，不做显著性；结论限于"是否继续" |
| 评估偏差 | 评分者知道 arm | 盲化 + 随机顺序 + 预注册 rubric |
| 范围蔓延 | 想加更多能力/主题 | 冻结 3 能力/1 主题；新需求进 backlog，不进 POC |
| 人工时间 | triage/评分 | 限制 2 小时/周；评分只做 acceptance |
| 历史泄露 | 用未来数据/LLM 知识 | 冻结 as_of；记录 `reconstructed`；不用 strict 校准 |

---

## 14. 开放决策（W1 前需拍板）

1. **最终案例**：C1–C6 是否都可用？替换池用哪几个？
2. **第二评分者**：是否有第二人？没有则单人盲评 + 确定性检查，明确披露。
3. **预算上限**：每案例 Scout token/时间；A/B/C/D 总预算。
4. **模型**：所有 arm 同模型；是否允许 Scout 用不同 temperature/策略 prompt？
5. **A 基线**：用现有报告直接 brief 化，还是重跑 pipeline？建议直接 brief 化，减少变量。
6. **历史案例**：`reconstructed` 是否允许进入 acceptance？建议只做 dev。
7. **两仓初始化**：W1 直接建两个仓库，还是先用同一工作区的 `alphabee/` + `apiary/` 目录模拟（按两仓边界执行），P1 再物理拆分？
8. **契约包发布**：`alphabee-contracts` 在 W1 就打成可安装包，还是先用同仓子包 + 版本号？
9. **部署**：初期同机 / 同 Compose；是否允许 Apiary 与 AlphaBee 共用 PostgreSQL / 对象存储？

---

## 15. 交付物清单

**AlphaBee 仓（运行时 / 正式能力）**

- [ ] `alphabee/research/`：3 能力 + Scout + reconcile + brief
- [ ] 3 张能力卡（YAML）+ 实现 + 单测
- [ ] 1 张方法卡（YAML）+ 3 个案例验证记录
- [ ] 6 个案例快照（`data/research/poc/cases/`）
- [ ] Scout memos（6 份）+ claim ledgers
- [ ] A/B/C/D briefs（6 × 4，同一渲染器）
- [ ] `alphabee-contracts`（版本化公共契约）

**Apiary 仓（控制面）**

- [ ] `poc/client.py` / `run_arm.py` / `run_eval.py`
- [ ] backlog / 评估记录
- [ ] 盲评表 + 评估报告
- [ ] go/no-go memo

**共同**

- [ ] 本文档（POC_SPEC）
- [ ] 回滚说明（删除两仓新增目录/包即可）
