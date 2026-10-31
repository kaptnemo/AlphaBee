# 中期决策层 T→E→F 分类器变体设计（可切换对比）

> **状态：设计文档（2026-10 完成；前置 P0 已全部落地，TEF 本身尚未实施）**。为响应 `docs/design/A_SHARE_FRAMEWORK_REVIEW.md` §2.1（信息顺序假设倒置）而立项：**保留现有 E→T→F 分类器原样不动**，新增一套**完全独立**的 T→E→F 分类器，两套通过配置/参数切换，用于同一数据上的 A/B 对比。本文档给出可直接开工的代码粒度：文件清单、类/函数签名、接线点（含既有代码锚点）、切换机制、序列化与兼容策略、配置项、测试矩阵、PR 切分与回滚。
> **对齐状态（2026-10-18 核实）**：本设计的全部前置依赖（`docs/implementation/framework/P0_A_SHARE_FIX_IMPLEMENTATION_DESIGN.md` 六项 P0）已按 PR-A（`bb7c06b`）/ PR-B（`cae758a`）/ PR-C（`494b084`）落地并完成 ROADMAP 变更登记（登记表第 81–83 行）。本文档已完成对齐更新：§1.1 立项依据按 P0-6 修复后的 E 重述、新增 §1.3 前置落地状态、§3.2 补充 E 构成口径、§4.1/§4.5 锚点行号刷新、§9 对比实验基线重定义。**TEF 实施按 §8.1 的 PR-1/PR-2 不变。**
> 关联文档：`docs/midterm/MIDTERM_DECISION_MODEL.md`（§4.2 状态映射表、§4.3 E>T>F 顶部顺序、§4.4 共振/背离——本文档的语义基线）、`docs/design/A_SHARE_FRAMEWORK_REVIEW.md`（§2.1 立项依据）、`docs/implementation/framework/P0_A_SHARE_FIX_IMPLEMENTATION_DESIGN.md`（已实施的前置设计）、`docs/design/RESEARCH_CONTINUUM_DESIGN.md`（§15 代码级设计范式）。
> 实施时契约改动必须走 `alphabee-pipeline-contract-steward` / `alphabee-schema-steward` skill 流程；本设计已按 steward 要求预先完成下游消费面追踪（§4、§6）。
> **修订指针（2026-10-31）**：本文档 §3.2 的逐状态状态证据公式是 **TEF 历史设计（`formula=v1`，已冻结）**。该公式的形式缺陷已由「状态证据公式 V2」修订，实现见 `alphabee/midterm/classifier_v2.py`（`v2` 为显式 opt-in，缺省仍为 `v1`；顺序哲学与本文档 §3.1 一致，只改「证据 → 归一化 → softmax」的形式：双侧中性带 / 双击证伪通路 / 上限归一化 + 温度 / 有向不确定 + 结构化背离）。设计见 `docs/implementation/midterm/MIDTERM_STATE_EVIDENCE_V2_DESIGN.md`（§4/§5），本文档**保留原文不改**（历史设计）。

---

## 1. 背景与目标

### 1.1 立项依据（评审 §2.1）

`MIDTERM_DECISION_MODEL.md` §4.3 明文规定顶部判定顺序 **E → T → F**："盈利预测先停止上修（E 转平），再股价 RS 下降（T 弱），最后财报才真正变坏（F 坏）"。该顺序隐含"存在活跃且及时无偏的一致预期流"（美股式机构信息流）。

A 股现实相反：研报覆盖稀疏且滞后于行情，题材/政策驱动下**股价先行、预期跟随、财报最后确认**是常态，真实信号顺序更接近 **T → E → F**。按 E>T>F 的优先级，S3→S4 判定会在"主题退潮、股价已破位"时仍等待 E 转平，系统性慢半拍。

> **P0-6 已落地后的重述（2026-10-18）**：评审的"E 不可靠"批评已在 P0 PR-C 部分修复——`evidence_rules` 与 `score_engine` 均加 coverage<3 护栏（`_MIN_CONSENSUS_COVERAGE=3`，`score_engine.py:51`），E 方向分锚重调为 **0.6×预告锚 + 0.4×研报修正**（`score_engine.py:197/225-233`）。但护栏修复的是 E 的**可靠性**，修不了 E 的**领先性**：即便研报修正不再产生假 strong，研报整体仍滞后于行情（"行情确认"而非"预期领先"），题材/政策驱动下价格抢跑的结构性事实不变。因此 **"E 先导 vs T 先导"的顺序问题在修复后的 E 上依然成立**——本设计的问题域不变，只是对比实验的基线从"未修复的 E"变为"修复后的 E"（§9.0）。

### 1.2 目标与非目标

**目标**：
1. 现有 E>T>F 分类器**逻辑零改动**（唯一变化是 `ClassifierResult` 承载位置迁移 + re-export 兼容，见 §4.1）；
2. 新增 T→E→F 分类器，**完全独立**（不 import、不共享 ETF 模块的任何可变逻辑）；
3. 两套**可切换**（配置 + 显式参数），切换不改编排图、不改下游消费契约；
4. 帧级 **provenance**：每帧持久化记录"由哪套分类器产出"，跨序对比可审计、可隔离。

**非目标（防范围膨胀，逐条登记）**：
- 不改 `score_engine` / `bayes` / `evidence_rules` / `position` 任何逻辑；
- **不做每标的自动选序**（"哪种顺序适合哪类标的"是后续研究问题，本设计只提供可对比的双实现；对比实验指南见 §9）；
- 主链 CLI（`main.py`）不新增 flag——主链跟随配置；对比便利性由 tracking CLI 提供（§5.2）；
- 不新增第三种顺序、不回填历史帧（历史帧归属 `etf` 是历史事实）；
- 不升 `CompanyStateArtifact.schema_version`（新增字段为 additive，见 §6.1）。

### 1.3 前置依赖的落地状态（2026-10-18 核实）

P0 六项已全部落地（ROADMAP 登记表第 81–83 行），其中直接影响本设计的有五项：

| P0 项 | 落地内容（提交） | 对本设计的影响 |
|---|---|---|
| P0-1 | 证据增量更新：`bayes.update_confidence_delta` + `evaluate(prev_evidence_log=...)`（`bb7c06b`） | 对比实验的帧序列后验不再系统性爬升；§9 基线干净化。**同时改变了 §4.5 的 `evaluate` 现状锚点**（`prev_evidence_log` 已在 `decision_model.py:337`，TEF 的 `classifier_order` 参数追加在其后） |
| P0-2 | retraction 事件 + `EvidenceEvent.supersedes`（`models.py:501`）+ `TriggerKind.RESTATEMENT` | 对比实验中的证据链可区分"作废/新证"；与 TEF 无直接耦合（分类器只消费 VariableScores） |
| P0-3 | `trading_calendar.py` / `board_limits.py` / `exit_conditions.py`；`diff(trading_days=..., suspended_days=...)`（`cae758a`） | TEF 把 T 当先导门控，其失真源（停牌日假稳定、跌停边界）已在口径层修正——对比实验的 T 输入更干净；`diff` 签名已变（`diff.py:720`），TEF 设计无需再动 diff |
| P0-4 | `price_move_mode/limit_ratio` 制度归一 + `report_calendar.py` + `TriggerKind.REPORT_CALENDAR` | 对比实验的告警/触发标注更完整（封板边界不再漏判）；与分类器无直接耦合 |
| P0-6 | coverage 护栏 + E 锚重调（0.6 预告锚 + 0.4 研报） | **TEF 对比问题的基线重定义**：§1.1 已重述；§9.0 明确"对比必须在修复后的 E 上进行" |

P0-5（政策入口 + 涨停情绪）只影响 `market_regime` 的 M 因子与 Issue 层，与分类器顺序对比正交，不改变本设计任何条款。

**结论**：本设计**无需调整任何公式/签名/切换机制条款**（§3–§7 原样有效），需要更新的是：§4.1/§4.5 的现状锚点行号、§5.1 配置段的现状注记、§9 的实验基线口径，以及 §8.3 的登记清单。

---

## 2. 设计原则

1. **两套零共享可变逻辑**：TEF 模块只 import `alphabee.midterm.models`（纯契约类型）。共享的只有 models 中的类型定义，与两套各自复制的中性常量（阈值、状态集合、迁移合法性集合）——复制是**刻意为之**（对比隔离），由 parity 测试钉住中性常量相等（§7.2）。
2. **输出契约不变**：两套 `classify_state` 签名与返回类型**完全相同**（`ClassifierResult`，迁入 models 后的单一类型）⇒ 下游 `decision_model` 零感知、artifact 结构不变。
3. **切换面最小**：配置段 + `evaluate`/`get_decision` 关键字参数 + tracking CLI flag，三处；fail-open 到 `etf`（仓库既有配置纪律）。
4. **provenance 显式化**：`CompanyStateArtifact.classifier_order` 字段（additive，默认 `"etf"`），帧落盘可见、可审计（§6）。

---

## 3. TEF 语义设计（核心）

### 3.1 顶部判定顺序定义

T→E→F：对 S3→S4（充分定价/降级）的判定——

1. 先看 `t_relative_strength` 是否**破坏**（T 弱）；
2. 再看 `e_revision` 是否**减速/转负**（E 停上修）；
3. 最后才确认 `f_fundamental_trend`（财报滞后，最后确认）。

与 ETF 的差异**只落在顺序决定性位置上**：S2 门控、S4 门控、S5 构成、背离 case ②。其余全部与 ETF 逐值一致（不变式清单见 §3.4）。

### 3.2 逐状态公式对照表

方向分符号沿用 classifier 现有约定：`_up(x)`/`_down(x)`/`_not_up(x)`（缺失→0/0.5 中性）。

> **E 构成口径（P0-6 落地后，2026-10-18）**：下表中 `e`/`e_up`/`e_down`/`e_not_up` 均指 `VariableScores.e_revision`，其**构成已变**——P0 PR-C 起为 0.6×预告锚（`_forecast_revision_anchor`，`score_engine.py:197`）+ 0.4×研报修正（`_revision` 原逻辑），且 coverage<3 时研报侧归零（`score_engine.py:231`）。本设计的门控公式**只消费 `e_revision` 的最终值，与构成无关**，故公式逐字不变；对比实验解释差异时应知 E 已含预告锚成分。

| 状态 | ETF（现状，`classifier.py:181-194`） | TEF（新） | 差异点 | 依据 |
|---|---|---|---|---|
| S0 研究候选 | `_S0_BASELINE`（0.30） | **相同** | 无 | 顺序无关 |
| S1 预期差 | `e_not_up * (f_up + 0.5*t_down + 0.5*v_up)` | **相同** | 无 | "市场低预期"是预期差的定义，与顺序无关 |
| S2 证据确认 | `e_up * (f_up + _not_up(t))` | `t_up * (f_up + _not_up(e))` | **门控 E→T**，未共振项 T→E | T 先行的世界里"证据确认"由价格先确认 |
| S3 共识扩散 | `3.0 * min(f_up, e_up, t_up)` | **相同** | 无 | 三共振无顺序 |
| S4 充分定价 | `e_not_up * (t_down + v_down + c_down)` | `t_down * (e_not_up + v_down + c_down)` | **门控 E→T**（§3.1 的落地） | T 破位是充分定价判定的先导信号 |
| S5 退出 | `e_down + 0.5 * f_down` | `t_down * (e_down + 0.5 * f_down)` | **加 T 门控**（价格破位先导） | 退出先看价格是否已破位 |

**S5 门控的取舍（在 TEF 模块 docstring 逐字披露）**：TEF S5 要求价格确认（`t_down>0`），代价是"非价格证伪（造假/政策突变，价格尚未反应）"时退出**更慢**；收益是主题退潮式破位退出**更早**、且大幅抑制"E 单因子噪声触发 S5"的假退出。这是 T→E→F 哲学的忠实表达，对比实验应把"S5 时滞 vs 假退出率"作为核心观察项（§9.3）。

### 3.3 一致性 / 背离（§4.4 对照）

| 分支 | ETF（`classifier.py:98-140`） | TEF | 差异 |
|---|---|---|---|
| resonant | F↑E↑T↑ | 相同 | 无 |
| divergent ① | F 强 + E/T 弱（"业绩好但股价不涨"） | 相同 | 无（顺序无关） |
| divergent ② | **E↑ + T↓**（"业绩上修 vs 股价走弱"，ETF 顶部顺序的第二步信号） | **T↑ + E↓**（"价格先行转强 vs 预期未跟上"——A 股题材行情的常态背离） | **镜像翻转** |
| E↑T↓ 在 TEF | — | **不再具名背离**，判 `independent` | T 破位在 TEF 由 S4 路径直接承接（T 弱本身就是降级先导），不再需要"背离"语义二次表达 |

`_divergence_reason` 文案同步镜像；`_research_tasks` 的 divergent 研究任务结构与 id（`research-divergent`）不变，`unknown` 文案随具名背离变化（下游 `explore_conflicts` 消费契约不变）。

### 3.4 不变式清单（两套必须相同，parity 测试钉住）

- S0/S1/S3 公式逐值相等；
- 阈值常量：`_SOFTMAX_TEMPERATURE=0.5`、`_ENTROPY_UNCERTAIN=1.0`、`_UP_T=0.3`、`_DOWN_T=-0.3`、`_ACTIVE_T=0.3`、`_DRIFT_EPS=1e-3`、`_S0_BASELINE=0.30`；
- `_STATES`、`_FORWARD`、`_BACKWARD`、`_REOPEN` 迁移合法性集合；
- `_softmax` / `_entropy` / `_clip01` / `_up` / `_down` / `_not_up` 纯函数语义；
- `_drift`、`_transition`（kind 判定）、`_active_factors`、`_research_tasks`（结构，文案除外）。

### 3.5 已评估并拒绝的替代设计

| 替代方案 | 拒绝理由 |
|---|---|
| S5 改为加法式 `t_down + e_down + 0.5*f_down` | 排序信号被稀释：E 单独下修仍会触发 S5，变体与 ETF 不可区分，失去对比价值 |
| S2/S4 门控用加权系数（如 `0.5*e_up + 0.5*t_up`）而非翻转 | 两套边界模糊，A/B 差异不可解释 |
| 在 `classifier.py` 内加 `order` 参数分支 | 违反"完全分开"要求，破坏 ETF 模块的纯净性 |
| 每标的自动选序 | 属后续研究问题，v1 先交付可对比双实现（§1.2 非目标） |

---

## 4. 代码结构设计

### 4.1 文件清单

| 文件 | 动作 | 内容 |
|---|---|---|
| `alphabee/midterm/classifier_tef.py` | **新增** | TEF 分类器（完全自包含，§4.3） |
| `alphabee/midterm/classifier_dispatch.py` | **新增** | 配置感知调度（§4.4） |
| `alphabee/midterm/models.py` | 修改 | `ClassifierResult` 从 classifier.py **迁入**（字段不变）；`CompanyStateArtifact` 追加 `classifier_order: str = "etf"`（§6.1）。注：P0 已在本文件追加 `EvidenceEvent.supersedes`（`models.py:501`）、`CompanyStateDiff.trading_days/suspended_days/superseded_evidence`（`models.py:717-730`），本设计的 additive 追加是同一既定模式 |
| `alphabee/midterm/classifier.py` | 修改（逻辑零改动） | `ClassifierResult` 改为从 models re-export（`classify_state` 主体在 `classifier.py:299`）；docstring 顶部加"顺序变体见 classifier_tef / 切换见 classifier_dispatch"指引 |
| `alphabee/midterm/decision_model.py` | 修改 | import 换 dispatch 版；`evaluate`/`get_decision` 加 `classifier_order` 参数并写入 artifact（§4.5）。注：`prev_evidence_log` 已在（`decision_model.py:337/444`，P0 PR-A），`classifier_order` 追加其后，参数顺序 = `prev_evidence_log` 之后 |
| `alphabee/midterm/__init__.py` | 修改 | 导出面扩展（§4.6；现状导出行 `__init__.py:16/163/166`） |
| `alphabee/config/__init__.py` | 修改 | 新增 `MidtermClassifierSettings` + `Settings.midterm` 段（§5.1）。注：P0 PR-B 已在 `DeviationTrackingSettings`（`config/__init__.py:111`）追加 `price_move_mode/price_move_ratio`（`157/166`），本设计的新增段同模式、互不冲突 |
| `config.yaml` / `config.yaml.example` | 修改 | 新增 `midterm:` 段（含注释） |
| `alphabee/tracking/scheduler.py` | 修改 | `reconcile`/`run_once`/`run_watchlist` 透传 order + CLI `--classifier`；`TrackingReport` 追加 provenance 两字段（§5.2、§6.2）。注：P0 已在该文件追加 `calendar_source/trading_days`（`scheduler.py:167-168`）、`prev_evidence_log` 接线（`scheduler.py:449-454`）、`diff(trading_days=...)` 接线（`scheduler.py:468-480`），本设计的追加同模式 |
| `tests/midterm/test_classifier_tef.py` | **新增** | TEF 单测（§7.1） |
| `tests/midterm/test_classifier_parity.py` | **新增** | parity 与差异方向钉（§7.2） |
| `tests/midterm/test_classifier_dispatch.py` | **新增** | 调度/fail-open/接线钉（§7.3） |
| `docs/midterm/MIDTERM_DECISION_MODEL.md` | 修改 | §4.3 后新增 §4.6「顺序变体（T→E→F）与切换」指引段 |
| `docs/roadmap/ROADMAP.md` | 修改 | 变更登记行（§8） |

### 4.2 不动清单（显式声明）

- `classifier.py` 的 `_state_evidence` / `_triad_consistency` / `_divergence_reason` / `_transition` / `classify_state` 主体**一行不改**；
- `diff.py` **不改**——它 import 的是 classifier 的中性私有常量（`_STATES/_FORWARD/_BACKWARD/_REOPEN/_drift`，`diff.py:34`），这些语义与顺序无关，继续从 classifier 导入；TEF 模块自备同值副本（parity 钉相等）。跨序差分不阻断、不归因重算，mismatch 只在调度层标注（§6.2）。注：P0 PR-B 已改过 `diff()`（`diff.py:720` 追加 `trading_days/suspended_days`），与本设计无关且已落地，TEF 实施时无需再动 diff；
- `score_engine.py` / `bayes.py` / `evidence_rules.py` / `position.py` 不改（P0 对前三者的既有改动已落地，不在 TEF 范围内）；
- 编排图、`OrchestratorState`、`ArtifactType`、`NODE_ORDER` 全部不动。

### 4.3 新增模块签名：`classifier_tef.py`

```python
"""Layer 2a 分类器变体：T→E→F 顶部判定顺序（design MIDTERM_CLASSIFIER_TEF_DESIGN §3）。

与 alphabee.midterm.classifier（E→T→F 基准）完全独立：本模块只 import
alphabee.midterm.models，不 import classifier；阈值常量与中性纯函数为**刻意复制**
（对比隔离），由 tests/midterm/test_classifier_parity.py 钉住与 ETF 的中性口径相等。

顺序差异落点（§3.2/§3.3）：
- S2 门控 E→T、S4 门控 E→T、S5 加 T 门控；
- 背离 case ② 镜像为 T↑+E↓；E↑+T↓ 不再具名背离（T 破位由 S4 路径承接）。

已披露取舍（§3.2 S5）：非价格证伪（造假/政策突变）时退出更慢，换取主题退潮式
破位退出更早、假退出率更低。
"""

# 阈值常量（与 classifier 同值，parity 钉）：_SOFTMAX_TEMPERATURE / _ENTROPY_UNCERTAIN /
# _UP_T / _DOWN_T / _ACTIVE_T / _DRIFT_EPS / _S0_BASELINE / _STATES / _FORWARD / _BACKWARD / _REOPEN

def classify_state(scores: VariableScores, *, previous: StateBelief | None = None) -> ClassifierResult:
    """与 classifier.classify_state 同签名、同返回类型；状态证据公式按 §3.2 TEF 列。"""
```

内部函数：`_state_evidence`（TEF 公式）、`_triad_consistency` / `_divergence_reason`（§3.3 镜像）、`_transition` / `_drift` / `_research_tasks` / `_active_factors`（与 ETF 同构，文案按 §3.3）。返回的 `ClassifierResult` 结构与 ETF 完全一致。

### 4.4 新增模块签名：`classifier_dispatch.py`

```python
"""配置感知分类器调度（TEF design §4.4）。

只做两件事：resolve 顺序字符串（fail-open）+ 路由到对应模块。
纪律（与 tracking.triggers.thresholds_from_settings 同构）：
- 配置在**调用时**读取（不 import 期读），缺失/异常/非法值 → warn + "etf"，绝不抛；
- 模块属性延迟 import（monkeypatch 可证伪"真的路由到目标模块"）。

from alphabee.midterm.models import ClassifierResult, StateBelief, VariableScores

ORDER_ETF = "etf"
ORDER_TEF = "tef"
KNOWN_ORDERS = (ORDER_ETF, ORDER_TEF)

def resolve_classifier_order(order: str | None) -> str:
    """order 非 None 且合法 → 原样；order=None → 读 settings.midterm.classifier_order
    （缺失/异常/非法 → "etf" + warning，fail-open）。"""

def classify_state(scores: VariableScores, *, previous: StateBelief | None = None,
                   order: str | None = None) -> ClassifierResult:
    """resolve 后路由：tef → classifier_tef.classify_state；其余 → classifier.classify_state。
    order 传非 None 时以显式值为准（同样 fail-open 到 etf）。"""
```

**解析去重约定**：`evaluate` 需要把**解析后的 order 值**写入 artifact provenance，因此它先调 `resolve_classifier_order` 一次拿到确定值，再把该确定值传给 `classify_state(order=确定值)`——dispatch 内部对合法值直接放行，不存在双重解析分叉。

### 4.5 接线点

| 锚点（现状，2026-10-18 核实） | 改动 |
|---|---|
| `decision_model.py:33` `from alphabee.midterm.classifier import classify_state` | 改为 `from alphabee.midterm.classifier_dispatch import classify_state, resolve_classifier_order` |
| `decision_model.py:368` `cls = classify_state(scores)` | 改为先 `resolved = resolve_classifier_order(classifier_order)`，再 `cls = classify_state(scores, classifier_order=resolved)`；artifact 构造处写入 `classifier_order=resolved` |
| `evaluate(...)`（`decision_model.py:328`；`prev_evidence_log` 已在 `:337`） | 签名在 `prev_evidence_log` 之后追加 `classifier_order: str | None = None`（keyword-only）；`None` = 跟随配置 |
| `get_decision(...)`（`decision_model.py:436`；`prev_evidence_log` 已在 `:444`） | 签名追加同参数并透传 `evaluate` |
| 主链 `nodes/midterm.py:330` `get_decision(...)` | **不改**——主链跟随配置（§5.1）；该文件 P0 PR-C 已加 `consensus_missing` Issue 发射（`nodes/midterm.py`），与 TEF 无耦合 |
| `tracking/scheduler.py:449` `decision_model.evaluate(...)`（`prev_evidence_log` 已在 `:454`） | 透传 `classifier_order`（来源：CLI `--classifier`，缺省 None → 配置） |
| `midterm/__init__.py:16` | 见 §4.6 |

### 4.6 导出面（`midterm/__init__.py`）

现有导出语义**逐字保留**（`classify_state` 仍指向 ETF 版，既有 import 方语义零变化），新增：

```python
from alphabee.midterm.classifier_tef import classify_state as classify_state_tef
from alphabee.midterm.classifier_dispatch import (
    classify_state as classify_state_dispatch,
    resolve_classifier_order,
)
```

`__all__` 追加 `classify_state_tef` / `classify_state_dispatch` / `resolve_classifier_order`。`ClassifierResult` 导入路径改自 models（对既有 `from alphabee.midterm.classifier import ClassifierResult` 的 re-export 兼容保留）。

---

## 5. 切换机制

### 5.1 配置段（新增 `midterm:`）

`alphabee/config/__init__.py`：

```python
class MidtermClassifierSettings(BaseModel):
    # 决策层分类器顺序变体：etf=E→T→F（基准，缺省）；tef=T→E→F（A 股价格先行变体）
    classifier_order: Literal["etf", "tef"] = "etf"

class Settings(BaseModel):
    ...
    # 中期决策层（缺段 ⇒ 全默认值；classifier_order 缺省 etf，旧 config 行为不变）
    midterm: MidtermClassifierSettings = Field(default_factory=MidtermClassifierSettings)
```

`config.yaml` / `config.yaml.example`：

```yaml
midterm:
  # 分类器顺序变体（TEF design §5.1）：etf = E→T→F（基准）；tef = T→E→F（价格先行变体）
  classifier_order: etf
```

读取纪律与 `deviation.tracking` 相同：运行时读取、fail-open、坏值 warn 回落 `etf`，**绝不抛**。注：`deviation.tracking` 段已于 P0 PR-B 追加 `price_move_mode/price_move_ratio`（`config/__init__.py:157/166`），本设计的 `midterm` 段为**新增独立段**，与既有段无命名冲突；`Settings` 顶层追加位置在 `domain_context` 段之后。

### 5.2 CLI 覆盖（tracking，A/B 对比入口）

`python -m alphabee.tracking` 新增：

```text
--classifier {etf,tef}   分类器顺序变体（缺省 = 读 config midterm.classifier_order）
```

主链 `main.py` **不加**该 flag（非目标，§1.2）。A/B 对比时用 tracking 路径落帧（该路径本就是帧持久化唯一入口），两序各跑一次即可并置观察。

---

## 6. 持久化与兼容策略（steward 边界）

### 6.1 `CompanyStateArtifact.classifier_order`（additive）

- 字段：`classifier_order: str = "etf"`，追加在 `degraded_reason` 之后（字段面只 append，不重排、不重命名）。
- **旧帧兼容**：存量 `data/midterm/state/*.jsonl` 无此字段，`load_artifacts` 读回后回落默认 `"etf"`——**语义正确**（历史帧确由 ETF 产出），不是"猜测"，专测钉住。
- **不升 `schema_version`**：additive 字段向后兼容，与 `TrackingReport` 既往"只 append 字段"纪律一致。
- 写入时机：`evaluate` 内部（§4.5），无论主链还是 tracking 路径，**所有帧都带 provenance**。

### 6.2 跨序差分处理（最小、不阻断）

- `diff.py` 不改（§4.2）。跨序两帧 diff 照常计算，但在 `tracking/scheduler.py::_reconcile_frame` 比较 `prev.classifier_order != curr.classifier_order` 时：
  - `TrackingReport` 追加两字段：`classifier_order: str = ""`（本帧解析值）、`classifier_order_mismatch: bool = False`（prev 非空且 order 不同 → True；旧帧默认 `"etf"` 参与比较）；
  - mismatch=True 时该帧照常落盘与告警（不阻断、不静默），报告字段自证——对比期间的混帧可被事后筛选。
- 操作建议（写入运行指南 §9.2）：A/B 对比用 `--state-dir` 分目录隔离（RC-5 已支撑），mismatch 字段作为隔离失效的兜底证据。

---

## 7. 测试矩阵

### 7.1 TEF 单测（`tests/midterm/test_classifier_tef.py`）

| 用例 | 断言 |
|---|---|
| S2 门控翻转 | `e_up=0, t_up=1, f_up=1` ⇒ TEF S2 证据 > 0，ETF 同输入 S2 = 0 |
| S4 门控翻转 | `t_down=0, e_not_up=1, v_down=1, c_down=1` ⇒ TEF S4 = 0（价格未破位不判充分定价），ETF 同输入 S4 > 0 |
| S4 先导触发 | `t_down=1, e_not_up=0.5, v_down=1` ⇒ TEF S4 > 0（不等 E 转平） |
| S5 门控 | `e_down=1, f_down=1, t_down=0` ⇒ TEF S5 = 0（无价格确认不退出），ETF > 0 |
| S5 破位触发 | `t_down=1, e_down=0.5` ⇒ TEF S5 > 0 |
| 背离镜像 | `t_up=1, e_dn=1` ⇒ TEF `DIVERGENT` + 具名 reason「价格先行转强 vs 预期未跟上」；`e_up=1, t_dn=1` ⇒ TEF `INDEPENDENT`（E↑T↓ 不再具名背离） |
| 不变式 | S1/S3 与 ETF 逐值相等；迁移合法性集合、`_STATES` 与 ETF 相等 |

### 7.2 parity 与差异方向钉（`tests/midterm/test_classifier_parity.py`）

- **中性口径相等钉**：网格扫描合成 `VariableScores`（含缺失/边界），断言两套在 S0/S1/S3 概率、`entropy`、`argmax` 相同；
- **差异方向钉**（不钉具体数值，钉差异存在性与方向）：构造顺序决定性输入，断言 S2/S4/S5 的 TEF/ETF 差异方向符合 §3.2；
- **迁移口径钉**：同一 `previous` + 同一 scores 序列，两套 `transition.legal`/`kind` 一致；
- **变异杀伤**：把 TEF S4 门控改回 E（等价 ETF）⇒ 差异方向钉必红；把 TEF S5 改成加法式 ⇒ S5 门控钉必红；改 TEF 背离 case ② 为 E↑T↓ ⇒ 背离镜像钉必红。

### 7.3 调度与接线钉（`tests/midterm/test_classifier_dispatch.py`）

| 用例 | 断言 |
|---|---|
| fail-open | settings 无 `midterm` 段 / 坏值（`"xxx"`）⇒ resolve 返回 `"etf"` 且不抛、warning 记录 |
| 显式优先级 | `order="tef"` 覆盖配置；`order="etf"` 同理 |
| 真路由（monkeypatch 可证伪） | monkeypatch `classifier_tef.classify_state` 为哨兵 ⇒ dispatch(order="tef") 命中哨兵（模块属性调用，非 import 期绑定） |
| 接线 | `decision_model.evaluate(..., classifier_order="tef")` ⇒ 返回 artifact `classifier_order=="tef"`；`get_decision` 透传生效 |
| 持久化 | 旧帧（无字段）load ⇒ `"etf"`；新帧含 `"tef"` roundtrip 不丢；`scheduler --classifier tef` 落盘帧带 `"tef"`；跨序相邻帧 ⇒ `TrackingReport.classifier_order_mismatch is True` |

### 7.4 回归

- `tests/midterm` 全绿——ETF 路径字节级行为不变（classifier.py 逻辑零改动 + dispatch 默认 `etf` 的强保证）；P0 三批新增用例（证据增量/日历/退出条件/护栏）一并全绿；
- `tests/tracking` 全绿（scheduler 只 append 字段/参数，`TrackingReport` 旧构造位零变化；P0 新增的 trigger/日历用例不回退）；
- 契约不变量（`test_pipeline_contracts`）、`NODE_ORDER`（恒 16）不变化；
- 全量门禁沿用离线口径（`pytest tests -q -m "not integration"`，见 ROADMAP 工程实践登记第 10 条）。

---

## 8. 分期、回滚与文档登记

### 8.1 PR 切分

- **PR-1（核心变体）**：models `ClassifierResult` 迁移 + `classifier_tef.py` + `classifier_dispatch.py` + config 段 + `decision_model`/`__init__` 接线 + 测试 7.1/7.2/7.3；
- **PR-2（provenance 与对比便利）**：`CompanyStateArtifact.classifier_order` 字段 + scheduler 透传/`--classifier` + `TrackingReport` 两字段 + 持久化测试。

### 8.2 回滚

- PR-1：删除 `classifier_tef.py`/`classifier_dispatch.py`，`decision_model` import 复原（`classifier.py` 的 re-export shim 可留，无害）；
- PR-2：artifact 字段与 report 字段均为 additive（可留）；删 CLI flag 即回 CLI 面。

### 8.3 文档登记要求（实施时）

- `ROADMAP.md` 变更登记表追加一行（含：新增 `midterm.classifier_order` 配置项默认值登记、行为变更面 = 仅显式切 `tef` 时、缺省行为零变化）；
- `MIDTERM_DECISION_MODEL.md` §4.3 后新增 §4.6「顺序变体（T→E→F）与切换」指引段（指向本文档）；
- `docs/design/A_SHARE_FRAMEWORK_REVIEW.md` §2.1 追加「已立项」标注；
- 本对齐更新（2026-10-18）与 P0 三提交的对应关系已在本设计头部状态行与 §1.3 登记，无需额外 ROADMAP 行。

---

## 9. A/B 对比实验运行指南（交付后的用法）

### 9.0 基线定义（P0 落地后重定义，2026-10-18）

对比实验的**唯一基线 = P0 六项落地后的代码口径**：

- 置信度走增量语义（P0-1）——帧序列后验不再系统性爬升，对比中观察到的 S3→S4 时滞差异**可归因于分类器顺序本身**，而非后验漂移；
- E 为修复后口径（P0-6）：0.6×预告锚 + 0.4×研报修正、coverage<3 护栏生效；
- T 的机械失真已在口径层修正（P0-3/4）：`trading_days` 落帧、封板边界触发不再漏判、`price_risk_stop/delist_warning` 退出条件生产点已存在；
- **历史存量帧（P0 落地前落盘）不参与对比**——其置信度/证据口径不同，混比会污染结论。对比目录一律用 `--state-dir` 新开。

### 9.1 目标

回答一个问题：**在修复后的 E 与干净化的 T 上，哪个顺序更早命中真实拐点、且假信号更少**——而非"哪个更保守/更激进"。

### 9.2 操作流程

```bash
# 两序隔离落帧（RC-5 的 --state-dir）
poetry run python -m alphabee.tracking --symbol 600519.SH --once --classifier etf --state-dir data/midterm/ab_etf
poetry run python -m alphabee.tracking --symbol 600519.SH --once --classifier tef --state-dir data/midterm/ab_tef
# 同 as_of 回放多期后，对比两目录帧序列（classifier_order 字段可自证归属；
# 若误入同目录，TrackingReport.classifier_order_mismatch=True 兜底可筛选）
```

### 9.3 观察指标（实验设计，非本设计实现范围）

1. **S3→S4 时滞**：主题退潮样本（政策退坡/题材证伪）中，两序各自从"高点帧"到 S4 的帧数差；
2. **S5 时滞 vs 假退出率**：真实破位样本的退出时点对比；非价格证伪样本（造假/问询）中 TEF 的退出滞后幅度；
3. **divergent 命中率**：TEF 的「T↑E↓ 背离」在题材行情中的命中频率与后续走势方向；
4. 结论口径以"事件研究"为准（对照真实公告/拐点日期），**不**以任何单一指标分数为准；
5. **归因纯净性**：观察期若出现 `RESTATEMENT`（P0-2）/`REPORT_CALENDAR`（P0-4）触发，应在其事件窗上核对两序反应——这些是 P0 落地后才有的触发标注，可用于"事件驱动对比"而非仅按帧数对比。

---

## 10. 验收标准

1. **ETF 零回归**：`classifier.py` 逻辑面零改动；`tests/midterm` 全量绿；默认配置下 `classify_state_dispatch` 与 `classifier.classify_state` 输出逐字段相等（parity 钉）；
2. **TEF 独立可辨**：S2/S4/S5 门控与背离镜像按 §3.2/§3.3 公式成立（差异方向钉），且模块不 import `classifier`（AST/导入面专测）；
3. **切换三通道**：配置 `midterm.classifier_order`、`evaluate(classifier_order=...)` 显式参数、tracking CLI `--classifier`，全部 fail-open（坏值 → `etf` + warn，绝不抛）；
4. **provenance 完整**：主链与 tracking 产出的每一帧 `CompanyStateArtifact` 均带解析后的 `classifier_order`；旧帧回落 `"etf"`；跨序混帧在 `TrackingReport` 显式 `mismatch`；
5. **下游零感知**：`MIDTERM_DECISION` artifact 消费方（`midterm_decision_reporter` / `payload_builders` / `contracts.coerce_midterm_decision`）不改一行且测试全绿；
6. **文档登记**：§8.3 四项全部完成；
7. **与 P0 落地面的兼容**：`evaluate` 新增参数与既有 `prev_evidence_log`（P0 PR-A）共存；`TrackingReport` 新增字段与既有 `calendar_source/trading_days`（P0 PR-B）共存——两者均为 additive 追加，旧构造位零变化（专测钉）。
