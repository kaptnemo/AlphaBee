# 维度置信度归零修复设计（D-2 实施落地方案）

> 关联条目：[`SEMANTIC_JUDGMENT_CONTRACT_DESIGN.md`](SEMANTIC_JUDGMENT_CONTRACT_DESIGN.md) §1.1 **D-2** 与 §6（问题诊断与方案比较，本设计是该 §6 的实施落地方案，**O-3 裁定以本文 §3.1 为准**）；[`ROADMAP.md`](../roadmap/ROADMAP.md) **P1-4**（2026-10-04，已实施一半：罚分不再 +0.1 提升置信度，但"绝对减法折减"与"文案分流"明文未动）；[`DEVIATION_CONTROL_FRAMEWORK.md`](DEVIATION_CONTROL_FRAMEWORK.md) §7.2 / §16（降级阻尼 ×0.85 一档封顶、"保守化螺旋"反模式）；[`INSIGHT_DEGRADATION_DESIGN.md`](INSIGHT_DEGRADATION_DESIGN.md)（相邻，不在本设计范围）。
> 状态：✅ **已实施**。文中所有"实测"数据均附复现命令（附录 A），语料范围逐字披露（F-4 纪律）。
> 证据更新：2026-10-16 补充 **300760.SZ 二次复现**（`task-623f034dc596`）：7 个维度 `0.0` + 1 个 `0.12`，`overall_score=0.2` = 单维得分（`alpha_arena.log:13910`）——与 002916 一轮（7 个 `0.0` + 1 个 `0.03`）同模式，确认 D-2 是**确定性缺陷而非偶发**。

---

## 1. 背景与问题

### 1.1 两次复现证据（真实 run，可直接核对）

| 项 | 002916.SZ 深南电路（2026-10-05） | 300760.SZ 迈瑞医疗（2026-10-16） |
|---|---|---|
| task record | `data/task_records/002916.SZ/2026-10-05/task-7fe88386b437.json` | `data/task_records/300760.SZ/2026-10-16/task-623f034dc596.json` |
| 最终维度置信度 | 7 个 `0.0` + `credit_risk` `0.03` | 7 个 `0.0` + `operational_stability` `0.12` |
| 信号层 | — | 22 条信号：触发 4（2 medium + 2 low）、`none` 15、`missing_fact` 3；异常触发 0 |
| 冲突层 | — | 11 组冲突、26 条已核验假设、6 条已证伪假设 |
| insight | — | 降级产出（tier=1 `lenient_rescue`，`degraded=true`） |
| 下游后果 | 7/8 `insufficient`、8 条 blocking、`overall_score=-0.362`=单维 | 7/8 `insufficient`、10 条 blocking、`overall_score=0.2`=单维（`alpha_arena.log:13910`）、`needs_revision` |
| 审查文案 | — | 7 个维度被报「该维度无信号覆盖，判断不可靠」，但各维度 evidence 实为 4/9/3/14/10/12/1 条 |

复现命令见附录 A.1–A.2。两轮模式的共同点是：**维度 evidence 非空（甚至 14 条），confidence 却是 0**——审查端误诊为"无信号覆盖"，整体结论退化为单一维度决定。

### 1.2 根因链（四层，按因果顺序）

**① 初始定义：覆盖度比例，分母固定为信号总数（天花板低）。**
[`engine.py:390/454`](../../alphabee/agents/thesis/engine.py#L454)：`confidence = min(1.0, len(contribs) / max(1, total_signals))`，`total_signals = len(signal_results)`（22）。19 条可评分信号（15 条 `none` 也计入，`SIGNAL_LEVEL_TO_SCORE["none"]=0.2`）被 8 个维度分摊，单维度初始覆盖度仅 4.5%~27% 量级；`missing_fact`（3 条）在 [`engine.py:404-406`](../../alphabee/agents/thesis/engine.py#L404) 被跳过。**此时任何扣减都足以致命。**

**② 决定性原因：冲突折减是绝对减法，与覆盖度比例不同量纲，且按「每条假设 × 每个相关维度」重复执行、与 severity 无关。**
[`engine.py:691/717/727`](../../alphabee/agents/thesis/engine.py#L691) 三条路径：

```python
dim.confidence = max(0.0, dim.confidence - 0.05)   # verified/partial 且有 gaps
dim.confidence = max(0.0, dim.confidence - 0.05)   # rejected
dim.confidence = max(0.0, dim.confidence - 0.1)    # unknown/pending
```

一个初始 13.6%（3/22）的维度，一次 −0.1 去掉大半、两次归零；对 4.5%（1/22）的维度一次 −0.05 即击穿。300760 一轮 32 条假设 × 各自 `related_dimensions` 的重复执行，把 7 个维度全部压到 `max(0.0, …)` 的地板。**severity 的差异已经由 score 侧的 `_CONFLICT_PENALTY`（critical 0.8 / high 0.55 / …）表达，confidence 侧再叠加与 severity 无关的绝对扣减是双重惩罚。**

**③ 放大器：insight 降级阻尼 ×0.85（设计内保留，不修）。**
本 run insight 为 tier=1 降级产出 ⇒ [`engine.py:991-998`](../../alphabee/agents/thesis/engine.py#L991) `factor = min(0.92, DAMPING_FACTOR=0.85) = 0.85`，全体维度再乘 0.85。这层本身是 §7.2 规则 2 的**有意设计**（消费降级产物必须保守化），本设计不动它；但它与 ② 叠加时，把"本可幸存的薄覆盖维度"再砍 15%。可用记录逐位验证它真实生效：`operational_stability` 初始 `3/22=0.13636…`，无冲突命中，`0.13636…×0.85=0.1159…` → `round(·,3)=0.116` → `to_dict round(·,2)=0.12`，与 record 中的 `0.12` 完全吻合（附录 A.3）。

**④ 下游三重放大：0 置信度被当成「无信号」逐级传导。**
- [`engine.py:530`](../../alphabee/agents/thesis/engine.py#L530) `_compute_overall` 只取 `confidence > 0` 的维度求均值 ⇒ 本 run 只有 `operational_stability`（0.2）参与 ⇒ `overall_score=0.2, judgment=positive`——**一个 12% 置信度的维度决定整份 thesis 方向**。
- [`reviewer.py:137-140`](../../alphabee/agents/thesis/reviewer.py#L137) Rule 1 对 `confidence == 0` 一律报「该维度无信号覆盖，判断不可靠」。真相是"冲突重罚导致置信度归零"——evidence 非空（后补的 verified-conflict 证据在 confidence 计算**之后**才 append 进 evidence，[`engine.py:752-762`](../../alphabee/agents/thesis/engine.py#L752)，永不提升置信度）。该误导性诊断已被 SEMANTIC 文档 §6.3 第 3 条预告。
- [`models.py:265-266`](../../alphabee/agents/thesis/models.py#L265) `score_to_judgment` 在 `confidence < 0.3 且 score = ±1.0` 时强档降级（所以「score=-1.0 却显示 negative」）。

### 1.3 为什么已修的 P1-4 不够

P1-4（2026-10-04）只删掉了 [`engine.py`](../../alphabee/agents/thesis/engine.py) `_apply_verified_conflict` 里的 `confidence + 0.1`（"惩罚越重越自信"），并在 ROADMAP 第 64 行明文登记「其余 confidence 语义点全部未动（覆盖度 / 折减 / insight 乘数 / effective_score）」。**② 的绝对减法与 ④ 的文案分流从未被修**，且当时没有任何测试钉住 −0.05/−0.1 的数值效果（§6.4 影响面核查），故 300760 一轮完整复现。

---

## 2. 设计目标与非目标

### 2.1 目标

- **G1 不归零不变量**：`len(contribs) > 0 ⇒ confidence > 0`（任意冲突条数下成立）。
- **G2 折减语义可解释**：confidence 折减表达「该维度经历过修订性更新」，不表达「证据消失」；severity 的差异只由 score 侧表达，避免双重惩罚。
- **G3 审查文案如实**：`confidence==0` 且 evidence 非空时报「置信度归零」，只有 evidence 为空才报「无信号覆盖」。
- **G4 overall 不再被单维决定**：通过 G1 达成（有效维度数 ≥2）；按 confidence 加权是另一语义变更，不进本设计（§9 O-2）。

### 2.2 非目标

- ❌ 不改 confidence 的覆盖度语义本身（B3 拆 `coverage`/`reliability` 字段不做，§9 O-1）。
- ❌ 不做 `_compute_overall` 的 confidence 加权（§9 O-2）。
- ❌ 不改 insight 阻尼系数、触发条件与「一档封顶」口径（③ 保留原样）。
- ❌ 不动 SEMANTIC 文档的其余范围（D-3 `unknown` 语义止损、coverage 度量体系、judge 路线）——那些仍属该文档待评审部分。
- ❌ 不引入任何新 ArtifactType / OrchestratorState 字段 / Pydantic 契约变更（编排契约：engine 保持确定性引擎，reviewer 保持 L1 确定性）。

---

## 3. 方案设计（B1 落地）

### 3.1 核心决策：冲突折减改「乘性 ×0.85、一档封顶」（裁定 SEMANTIC O-3 = B1）

三处绝对减法统一改为：**每条被冲突修订的维度，在 `_apply_conflict_analysis` 尾部最多乘一次 `DAMPING_FACTOR`（0.85）**，与命中次数、假设条数、severity 无关。

选型理由（对照 SEMANTIC §6.2 表）：

| 方案 | 结论 |
|---|---|
| **B1（乘性 ×0.85，一档封顶）** | ✅ **采用**。① 覆盖度是比例，折减必须是乘法才量纲一致；② 复用 [`degradation.py:44`](../../alphabee/orchestrator/services/degradation.py#L44) `DAMPING_FACTOR` 与 §7.2「一档封顶、与个数无关」的既有语义，不引入新魔法常量（C-5 纪律）；③ `0.85^k > 0`（k 有限）⇒ G1 自动成立 |
| B2（保底 `FLOOR=1/total_signals`） | ❌ 不采用。引入新常量且语义含糊（"保底可见"不等于"有证据"），G1 可由 B1 免费获得 |
| B3（拆 coverage / reliability） | ❌ 本轮不做。语义最正确但改动面大（4 处消费 + reviewer 文案 + 报告载荷），登记为 §9 O-1 |

**status 档位不再区分**（原 −0.1 vs −0.05 的相对差异是伪精度：对 4.5% 覆盖度的维度，−0.05 已击穿；且 unknown 的语义止损属 SEMANTIC D-3 的独立范围）。若未来确要区分，乘性下用两档 0.85/0.92、仍一档封顶（§9 O-3），**本轮不做**。

### 3.2 补丁草图 — `engine.py::_apply_conflict_analysis`

```python
# 函数内新增局部 import（与 _apply_insight 同款写法，避免 agents→orchestrator 模块级依赖）
from alphabee.orchestrator.services.degradation import DAMPING_FACTOR

damp_flags: set[str] = set()   # 本次冲突结算中需要折减置信度的维度（只登记，不在此处折减）

# ── 循环内三处改动（原 691 / 717 / 727）──
# (a) verified/partial 且有 gaps：保留 missing_evidence.extend(gaps[:3])，删除
#     `dim.confidence = max(0.0, dim.confidence - 0.05)`，改为 `damp_flags.add(dim_id)`
# (b) rejected：保留 counter_evidence / _remove_disputed_contributions 精确扣除，
#     删除 `dim.confidence = max(0.0, dim.confidence - 0.05)`，改为 `damp_flags.add(dim_id)`
# (c) unknown/pending：保留 missing_evidence.extend(missing[:3])，删除
#     `dim.confidence = max(0.0, dim.confidence - 0.1)`，改为 `damp_flags.add(dim_id)`

# ── 循环后、去重块之后新增 ──
for dim_id in damp_flags:
    dim = dimensions.get(dim_id)
    if dim is None or dim.confidence <= 0:
        continue  # 无贡献的维度（覆盖度=0）保持 0：阻尼只作用于"有证据但被修订过"的维度
    dim.confidence = round(dim.confidence * DAMPING_FACTOR, 3)  # 一档封顶：与命中次数无关
```

行为等价性声明：**score 路径零改动**（`_apply_verified_conflict` 的罚分、`_remove_disputed_contributions` 的精确扣除、`counter_evidence`/`missing_evidence` 追加全部原样），P1-4 钉住的"罚分不升置信度"不受影响。

### 3.3 补丁草图 — `reviewer.py` Rule 1 文案分流

```python
# ── Rule 1: zero confidence ──
if not evidence:
    issues.append("该维度无信号覆盖，判断不可靠")
    status = "insufficient"
    suggested_action = "needs_more_data"
elif confidence == 0:
    # B1 修复后新 run 不会再走到本分支（len(contribs)>0 ⇒ confidence>0）；
    # 保留仅为重放旧 artifact 时给出如实诊断（证据存在但置信度已被旧逻辑归零）。
    issues.append(f"该维度置信度被冲突折减归零，方向判断不具参考性（证据 {len(evidence)} 条）")
    status = "insufficient"
    suggested_action = "needs_more_data"
```

状态判定（insufficient）与 suggested_action 不变——只改文案，保证旧 record 重放的行为等价性（这是 SEMANTIC §6.3 第 3 条的落地）。

### 3.4 可观测性（最小面，P1 可选）

P1 在折减循环内追加一条确定性日志（不新增 schema；`_apply_conflict_analysis` 不在 `run()` 的作用域内，无 `symbol` 可用，只记维度级信息）：

```python
logger.info("thesis_confidence_damped", dim_id=dim_id, hits=len(hits_per_dim[dim_id]), damped=dim.confidence)
```

（命中数由 `damp_flags` 改为 `dict[str, int]` 计数即可，仍是纯确定性结构）。task record 侧**不新增字段**——重放回归靠测试钉，不靠 record 扩列（record 目前未落 `thesis_impact`/`related_dimensions`，见 §6.3 诚实披露）。

---

## 4. 改动清单（inScope / outOfScope）

- **inScope**：`alphabee/agents/thesis/engine.py`（`_apply_conflict_analysis` 三处减法 → 标志登记 + 尾部乘性折减 + 局部 import）、`alphabee/agents/thesis/reviewer.py`（Rule 1 文案分流）、新增 `tests/agents/thesis/test_confidence_damping.py`、扩展 `tests/agents/thesis/test_conflict_direction_r3.py`（002916 engine 级重放断言）、`docs/roadmap/ROADMAP.md`（行为变更登记行，实施时）、本文档 + SEMANTIC 文档交叉引用。
- **outOfScope**：`SIGNAL_LEVEL_TO_SCORE` / `_CONFLICT_PENALTY` / `JUDGMENT_THRESHOLDS`（K4 产品口径）、`_apply_insight` 的阻尼（③）、`_compute_overall` 加权、task record 模型、所有 prompts。

---

## 5. 分阶段实施

### 阶段 P0 —— 修复 + 回归钉（单 commit，可回滚）

1. `engine.py` 按 §3.2 落地乘性折减（一档封顶）。
2. `reviewer.py` 按 §3.3 文案分流。
3. 新增 `test_confidence_damping.py`（§6.1 全表）+ 002916 重放扩展（§6.2）+ 300760 半重建 fixture（§6.3，诚实标注推断项）。
4. ROADMAP 增加行为变更登记行（纪律：`docs/design/SEMANTIC_JUDGMENT_CONTRACT_DESIGN.md` §5.2 的"ROADMAP 行 + 判别力测试 + 回滚说明"）；SEMANTIC 文档的交叉引用（状态行 / D-2 行 / §6 头部指针）**已随本设计成文完成**，实施时只需把本文档状态改为「已实施」。
5. `pytest` / `ruff` / `mypy` 门禁全绿。

### 阶段 P1 —— 可观测性（可选，独立 commit）

§3.4 的 `thesis_confidence_damped` 日志；用 300760 新 run 验证日志量与 `insufficient` 下降幅度，人工抽检 1–2 条。

### 阶段 P2 ——（不排期，登记为开放问题）

B3 拆字段（O-1）、`_compute_overall` 加权（O-2）——各自另立设计，不得并入本 commit。

---

## 6. 测试计划

### 6.1 新文件 `tests/agents/thesis/test_confidence_damping.py`

| 用例 | 断言 |
|---|---|
| 单次命中为乘性 | `conf=0.5`，1 条 rejected 假设命中 ⇒ `0.425`（=0.5×0.85） |
| 一档封顶 | 同维度被 5 条假设命中 ⇒ 仍 `0.425`（≠ 0.5×0.85⁵） |
| 三种 status 同口径 | verified+gap / rejected / unknown 三条路径各命中一次 ⇒ 各自只乘一次 0.85 |
| 不归零不变量（G1） | 任意冲突条数 × 任意假设数下，`contribs>0` 的维度 `confidence > 0` |
| 零覆盖保持零 | 无贡献维度（覆盖度 0）被命中 ⇒ 仍 `0.0`（不抬升） |
| score 路径不受影响（回归 P1-4/P0-2） | 罚分、精确扣除、`counter_evidence`/`missing_evidence` 行为与修复前一致；罚分**不**升置信度 |
| reviewer 文案分流 | evidence 空 ⇒ 旧文案；`confidence==0` 且 evidence 非空 ⇒ 新文案（旧 artifact 兼容分支） |

### 6.2 engine 级 002916 真实语料重放（扩展 `test_conflict_direction_r3.py`）

复用既有 `_REAL_002916_CONFLICTS`（五冲突真实 explanation，[`test_conflict_direction_r3.py:161`](../../tests/agents/thesis/test_conflict_direction_r3.py#L161)），在 `test_engine_002916_five_conflicts_projection` 旁新增断言：修复后受冲突修订的维度 `confidence > 0`（修复前该场景归零/0.03），且 `dim.score` 与修复前逐位一致（score 路径零改动）。

### 6.3 300760 半重建 fixture（诚实披露口径）

task record 未落 `thesis_impact` 与冲突的 `related_dimensions`（`recorder._extract_signals` 只存 id/level/interpretation），**无法从 record 全量重放 engine 输入**。半重建口径：
- 22 条信号的 `level` 全部来自 record（附录 A.4）；`thesis_impact` 按 report `dimension_analysis` 的 evidence 清单手工映射（表见附录 A.5，**标注"重建"**）；
- 冲突侧用 record 的 6 条 `verified_conflict` issue 摘要重建 severity/状态；`related_dimensions` 按 report 冲突→维度叙述推断（**标注"推断"**）。
- 断言因此采用**不变量而非精确值**：重建输入的每个有贡献维度 `confidence > 0`；`operational_stability` 保持 `0.12`（无冲突命中 ⇒ 不得被改动）；有效维度数 ≥ 2；`overall_score` 不再等于单维得分。若推断的 `related_dimensions` 与真实值偏差导致精确断言不稳，一律退回不变量断言（在测试 docstring 里写明原因）。

### 6.4 现有测试影响面核查（2026-10-16 实测）

已逐文件核查 `tests/agents/thesis/`：**没有任何用例钉住 −0.05/−0.1 的数值效果**。`test_confidence_penalty.py`（11 例）钉的是"罚分不升置信度"（与本改动正交，必须保持全绿）；`test_explicit_context_consumption.py` 只断言 evidence/counter_evidence/missing_evidence 存在性（不受影响）；`test_effective_score.py` 用直接构造的 confidence（不受影响）。这正是缺陷两次漏网的测试缺口，由 §6.1 补齐。

### 6.5 判别力（变异必红）

| 变异 | 注入方式 | 必红断言 |
|---|---|---|
| M1 | 把乘性改回 `max(0.0, conf - 0.1)` | §6.1 乘性/不归零断言 |
| M2 | 去掉一档封顶（每条假设乘一次） | §6.1 封顶断言 |
| M3 | 删掉 reviewer 文案分流 | §6.1 文案断言 |
| M4 | 误改 score 路径（如删罚分） | §6.2 002916 分数逐位一致 + `test_confidence_penalty.py` |

---

## 7. 兼容性、风险与回滚

| 风险 | 缓解 | 回滚 |
|---|---|---|
| 置信度整体抬高 ⇒ `insufficient` 减少 ⇒ review 变宽松 | score 惩罚路径零改动，结论方向不受影响；报告仍展示 `effective_score`（=score×confidence），低覆盖度的弱判断依然"净得分低" | 单 commit 回滚（`git revert`），行为变更登记行同步删除 |
| 乘性折减削弱"被多重冲突打穿"的警示 | 一档封顶外，多次命中仍体现在 `missing_evidence`/`counter_evidence` 与 score 罚分上；§3.4 日志给出命中数可审计 | 同左 |
| 300760 半重建 fixture 与真实输入有偏差 ⇒ 断言失真 | 断言用不变量（§6.3）；真实行为验收靠修复后**新 run**（§8 验收 4） | 修 fixture 映射表 |
| 旧 artifact 重放（修复前 record） | reviewer 文案分流保留 `confidence==0 && evidence` 分支，状态判定不变 | 同左 |

---

## 8. 验收标准

1. **300760 半重建重放**：有贡献维度 `confidence > 0`（修复前 7 个为 0）；`operational_stability` 仍为 `0.12`；有效维度数 ≥ 2；`overall_score` 不再等于单维得分。
2. **002916 engine 重放**：`credit_risk` 等被修订维度 `confidence > 0`；`insufficient` 维度由 7 → ≤1；分数与修复前逐位一致。
3. **判别力**：M1–M4 变异必红（§6.5）。
4. **真实 run 验证**：修复后对 300760 重新跑一轮（或任一有 ≥2 组冲突的标的），确认报告阻断项中「该维度无信号覆盖」只出现在真正无证据的维度上，且 `thesis_generated` 日志的 `overall_score` 不再等于某个单维得分。
5. **门禁**：`pytest` / `ruff` / `mypy` 零回归（含既有 `test_confidence_penalty.py` 11 例全绿）。
6. **登记**：ROADMAP 行为变更行 + 本文档状态改「已实施」+ SEMANTIC 文档交叉引用完成。

---

## 9. 开放问题

| 编号 | 问题 | 备选 | 归属 |
|---|---|---|---|
| O-1 | 拆 `coverage` / `reliability` 两字段（B3） | 后续另立设计（改动面含 4 消费点 + reviewer + 报告载荷） | SEMANTIC §6.2 |
| O-2 | `_compute_overall` 是否按 confidence 加权 | (a) 维持等权（当前）；(b) score×confidence 加权平均 | 本设计 P2 |
| O-3 | 折减是否按 status 分两档 | (a) 统一 ×0.85（本轮采用）；(b) unknown ×0.85、rejected/gap ×0.92 | 本设计 P2 |
| O-4 | 是否把折减命中数落入 task record（扩列） | (a) 只落日志（本轮）；(b) record 加 `confidence_hits` 字段 | §3.4 |

---

## 附录 A：复现命令（全部可一键执行）

### A.1 300760 record 快照（7×0.0 + 1×0.12）

```bash
python - <<'PY'
import json
d = json.load(open("data/task_records/300760.SZ/2026-10-16/task-623f034dc596.json"))
print([(x["dim_id"], x["confidence"], x["score"]) for x in d["thesis_dimensions"]])
print("overall:", d["overall_judgment"], "| review:", d["review_overall_status"])
# 期望：7 个 (…, 0.0, …) + ('operational_stability', 0.12, 0.2)；overall=positive；review=needs_revision
PY
grep -n '"event": "thesis_generated"' logs/alpha_arena.log | grep 300760   # 期望 overall_score=0.2（单维）
```

### A.2 002916 record 快照（7×0.0 + 1×0.03）

```bash
python - <<'PY'
import json
d = json.load(open("data/task_records/002916.SZ/2026-10-05/task-7fe88386b437.json"))
print([(x["dim_id"], x["confidence"], x["score"]) for x in d["thesis_dimensions"]])
# 期望：7 个 0.0 + ('credit_risk', 0.03, -0.362)
PY
```

### A.3 算术逐位验证（③ 的 ×0.85 真实生效）

```bash
python - <<'PY'
conf = 3/22                    # operational_stability：3 条贡献 / 22 条信号
print("initial:", conf)                      # 0.1363636…
print("after insight x0.85:", round(conf*0.85, 3))   # 0.116（engine round 3）
print("to_dict round 2:", round(round(conf*0.85, 3), 2))  # 0.12 == record 值
PY
```

### A.4 300760 信号层分布（19 可评分 / 3 missing_fact / 0 异常）

```bash
python - <<'PY'
import json
d = json.load(open("data/task_records/300760.SZ/2026-10-16/task-623f034dc596.json"))
from collections import Counter
c = Counter(s["level"] for s in d["signal_results"])
print(c, "| anomaly_triggered:", d["anomaly_triggered_count"])
# 期望：{'none':15,'low':2,'medium':2,'missing_fact':3}；anomaly_triggered=0
PY
```

### A.5 300760 信号→维度 `thesis_impact` 重建映射（供 §6.3 fixture；来源 = report `dimension_analysis` evidence 清单，**重建口径**）

| 维度 | 信号路径证据（level/impact，report 原文） | 贡献数 |
|---|---|---|
| capital_efficiency | cashflow_quality_risk(low/neutral)、expansion_risk(medium/neutral)、debt_risk(none/neutral)、profitability_quality_risk(medium/neutral) | 4 |
| competitive_moat | moat_erosion_risk(low/neutral)、revenue_quality_risk(none/positive)（冲突 f2/f5 为后补，不计） | 2 |
| credit_risk | expansion_risk(medium/slightly_negative)、debt_risk(none/positive)、anomaly_pattern_high_cash_high_debt(none/neutral) | 3 |
| earnings_quality | 5 条「未命中异常模式」（neutral） | 5 |
| financial_quality | expansion_risk(medium/slightly_negative)、anomaly_cluster_risk、debt_risk(none/positive)、3 条 neutral 异常模式 | 6 |
| growth_quality | report 明示 growth_quality_risk / revenue_quality_risk **未列入** evidence；主要靠冲突后补 | ≤1（以 fixture 实测为准） |
| operational_stability | cross_validation_break(none/positive)、anomaly_cluster_risk(none/positive)、debt_risk(none/positive) | 3 |
| valuation_fit | valuation_risk(none/positive) | 1 |

> 口径披露（F-4）：本表分子来自 report 文本，`thesis_impact` 原始字段未落 task record，故标"重建"；任何断言如与该口径冲突，以 §6.3 的不变量断言为准。
