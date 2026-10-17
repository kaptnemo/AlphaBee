# Thesis「语境不适配」修复设计（F1–F5 实施落地方案）

> 关联条目：[`THESIS_CONTEXT_MISMATCH_DEFECT_ANALYSIS.md`](THESIS_CONTEXT_MISMATCH_DEFECT_ANALYSIS.md)（缺陷分析与根因链 R1–R7，本文是其 §5 五条修复方向的落地方案）；[`THESIS_CONFIDENCE_DAMPING_DESIGN.md`](THESIS_CONFIDENCE_DAMPING_DESIGN.md)（置信度乘性阻尼同构模式：一档封顶、显式常量、日志留痕）；[`REPORT_QUALITY_FIX_ROADMAP.md`](../roadmap/REPORT_QUALITY_FIX_ROADMAP.md)（"大存大贷" rejected 传导断裂，F1 的同源修复）；[`SEMANTIC_JUDGMENT_CONTRACT_DESIGN.md`](SEMANTIC_JUDGMENT_CONTRACT_DESIGN.md)（判定语义契约，F1/F3 均不得破坏）；[`DEVIATION_CONTROL_FRAMEWORK.md`](DEVIATION_CONTROL_FRAMEWORK.md) §7.2/§16（一档封顶、保守化螺旋反模式）。
> 状态：✅ **已实施**。五个修复项已按 §6.1 顺序（F5→F1→F2→F4→F3）分别实现、评审并合入；变更登记见 [`../roadmap/ROADMAP.md`](../roadmap/ROADMAP.md) 行为变更登记表（2026-10-17）。
> 证据：603986.SH 兆易创新 `task-6e9476f66ac8`（2026-10-17，3 blocking + 3 warning 语境不适配），缺陷文档 §1.1 已还原全部现场。

---

## 1. 背景与问题

缺陷文档已定位七层根因。本文按杠杆顺序落地其 §5 五条修复方向，编号对应：

| 修复项 | 对应根因 | 修复内容（一句话） |
|---|---|---|
| **F1** | R1 + R2 | rejected 假设未溯源时，维度分数做一次 ×0.5 折减（一档封顶），消除"证伪成立、负分照旧" |
| **F2** | R3（广播乘数） | insight 反证按 `related_dimensions` 定向注入，取代全维广播 |
| **F3** | R5 | 审查层闭环：`signals_consistent=false` 进入 issues；强档判断在"LLM 标志 + 反证存在"双条件确认下确定性降一档，并传导到报告 |
| **F4** | R6 | 把 COMPANY_TRACK 的 peer_group / peer_benchmarks 传入 reviewer（Layer 1 规则 + LLM 输入） |
| **F5** | R3（分类器） | 方向分类器补词：掩盖/弱化 入恶性词表，仍在提升/未弱化/未见弱化 入良性词表 |

五个修复项**互不依赖**；F1+F5 组合后 603986 一轮的三条 blocking 应全部闭合（§6.3 重放核对）。

---

## 2. 设计目标与非目标

### 2.1 目标

- **G1 反证有回分通道**：rejected 假设无法精确溯源时，其相关维度分数不再原地不动（F1）。
- **G2 反证不再错位**：带维度标注的 insight 反证只进入相关维度；旧产物无标注时保持全维广播（兼容，F2）。
- **G3 审查结论闭环**：LLM 层的不一致发现进入 issues；满足结构性双条件的强档判断确定性降档，且报告端可见（F3）。
- **G4 语境输入补全**：估值/资本配置/信用维度在无同行基准时如实提示；有基准时 LLM 审查必须引用（F4）。
- **G5 方向分类贴近事实**：603986 暴露的"证伪式措辞判 unknown→罚分""确认掩盖判 benign→漏罚"两类错分被修正（F5）。

### 2.2 非目标

- ❌ 不改 judgment 的分数映射表与 `score_to_judgment` 强档降级规则本身（F1 只改分数输入，不改档位函数）。
- ❌ 不把 `unknown` 的保守回落语义改为"不计负票"（P0-2R3 captain 裁定的安全侧语义不动，F5 只补词）。
- ❌ 不修改 thesis 的判定值字段语义（F3 的 `recalibrated_judgment` 是 verdict 上的**附加字段**，`original_judgment` 保持原值）。
- ❌ 不改报告 gate 的判定逻辑（F3 只增报告 prompt 呈现指令，不改变 gate 阈值）。
- ❌ 不引入新 ArtifactType / OrchestratorState 字段（编排契约纪律：所有载荷变更都落在既有 artifact 的 additive 字段上）。
- ❌ 不重做 COMPANY_TRACK 的时效性治理（`company_track_stale` 已由 resolve_company_track 节点负责）。

---

## 3. 方案设计

### 3.1 F1 — rejected 未溯源 → 分数 ×0.5 折减（一档封顶）

#### 3.1.1 选型

| 方案 | 结论 |
|---|---|
| **A：分数 ×0.5，一档封顶** | ✅ **采用**。×0.5 保证任何 strong_*（\|score\|≥0.5）折减后必降一档（-1.0→-0.5 恰落在 negative 边界）；方向不变；与置信度阻尼的"一档封顶"语义同构，不引入新量纲 |
| B：×0.85（与置信度阻尼同系数） | ❌ 不采用。缺陷文档 R4 实测：-1.0×0.85=-0.85 仍是 strong_negative，修复目标（标签与反证的可见矛盾）不闭合 |
| C：硬钳位 \|score\|≤0.49 | ❌ 不采用。对轻度负面（-0.6）过度拉平（-0.49 vs A 的 -0.3），且破坏 score 的连续量纲 |
| D：整维归零（score=0.0） | ❌ 不采用。"证伪≠维度无问题"——被证伪的只是一条假设，其余贡献仍有效（P0-③ 的精确扣除哲学） |

系数显式化：模块常量 `_REJECTED_SCORE_DAMP = 0.5`，出处注释引用本设计（§8.2 规则 1：权重值必须显式，不得埋在逻辑里）。

#### 3.1.2 补丁草图 — `agents/thesis/engine.py`

```python
# 模块级常量（与 _CONFLICT_PENALTY 并列，出处注释引用本设计 §3.1）：
_REJECTED_SCORE_DAMP = 0.5

# ── _apply_conflict_analysis 内：签名不变，新增局部登记集合 ──
untraced_rejected: set[str] = set()   # rejected 且精确溯源失败的维度（只登记，循环后统一折减）

# rejected 分支（原 engine.py:705-730 区域）：
elif status == "rejected":
    message = summary or hypothesis.get("explanation", "")
    disputed_pattern, disputed_signal = self._resolve_disputed_evidence(...)
    for dim_id in dim_ids:
        dim = dimensions.get(dim_id)
        if dim is None:
            continue
        if message:
            dim.counter_evidence.append(message)
        removed = False
        if disputed_pattern or disputed_signal:
            removed = self._remove_disputed_contributions(
                dim=dim, dim_effective=dim_effective,
                disputed_pattern=disputed_pattern, disputed_signal=disputed_signal,
            )
        if not removed:
            # 缺陷文档 R2：框架层假设不含 pattern/signal id → 精确扣除必然落空。
            # 原实现"分数原地不动"使"证伪成立、负分照旧"（603986 三条 blocking 的根）。
            untraced_rejected.add(dim_id)
        damp_flags[dim_id] = damp_flags.get(dim_id, 0) + 1   # 置信度阻尼照旧

# ── _remove_disputed_contributions：返回值 None → bool（True=已精确扣除）──
# 未命中早退（现 engine.py:886-888 的 `if kept == eff: return`）改为 `return False`；
# 函数末尾显式 `return True`（已扣除并重算分数）。

# ── 循环尾部（置信度阻尼循环之后新增，与 damp_flags 块并列）──
for dim_id in untraced_rejected:
    dim = dimensions.get(dim_id)
    if dim is None or dim.score == 0:
        continue   # 中性维度折减是无操作，跳过（与置信度阻尼"零覆盖不抬升"对称）
    dim.score = round(dim.score * _REJECTED_SCORE_DAMP, 3)
    dim.context_notes.append(
        f"被证伪假设未溯源到具体贡献，维度分数已按 ×{_REJECTED_SCORE_DAMP:g} 折减（一档封顶）。"
    )
    logger.info("thesis_score_damped", dim_id=dim_id, damped_score=dim.score)
```

#### 3.1.3 行为等价性声明

- **traced 路径零改动**：`test_rejected_hypothesis_rewrites_credit_risk_score_and_removes_both_paths`、`test_rejected_hypothesis_falls_back_to_keyword_inference` 两钉不变（精确扣除照旧，score=0.0）。
- **置信度路径零改动**：`damp_flags` 逻辑原样；F1 只动 score 通道。
- **verified/unknown 路径零改动**：`_apply_verified_conflict` 罚分、`missing_evidence` 追加原样。
- **唯一有意变更**：`test_rejected_hypothesis_without_match_keeps_score_conservatively`（[`test_rejected_disputed_rewrite.py:108-114`](../../tests/agents/thesis/test_rejected_disputed_rewrite.py#L108)）钉住的"保守不扣分"被推翻——该钉正是缺陷文档 R2 要修复的行为，契约更新见 §5.1。

#### 3.1.4 603986 预期效果（§6.3 重放核对）

- 竞争壁垒 -1.0 → -0.5 → judgment **negative**（原 negative 不变、score 与标签矛盾消除）；
- 盈利质量 -1.0 → -0.5 → **negative**（strong_negative 闭合）；
- 成长质量 -0.85 → -0.425 → **negative**（strong_negative 闭合，与"成长期已折减"的 note 不再打架）。

### 3.2 F2 — insight 反证按维度定向注入

#### 3.2.1 补丁草图 — `agents/insights/models.py`

```python
class EvidenceItem(BaseModel):
    statement: str = Field(...)
    source: str = Field(...)
    weight: Literal["strong", "moderate", "weak"] = Field(default="moderate", ...)
    related_dimensions: list[str] = Field(
        default_factory=list,
        description="该证据反驳/削弱的 thesis 维度 id 列表；无法归因时留空（留空=下游按全维广播兼容）",
    )
```

#### 3.2.2 补丁草图 — `agents/insights/prompts.py`

- JSON 示例的 `counter_evidence` 条目增加 `"related_dimensions": ["earnings_quality"]`；
- 工作方法追加第 7 条："**每条反证标注 related_dimensions**：取值必须是 thesis 维度 id（`financial_quality / earnings_quality / growth_quality / competitive_moat / credit_risk / operational_stability / capital_efficiency / valuation_fit`），表示该反证反驳哪一维的判断；确实无法归因时留空数组。"

#### 3.2.3 补丁草图 — `agents/insights/rescue.py`

```python
# _coerce_evidence_items：透传 related_dimensions（非列表/非法 id 一律丢弃，宽进严出）
dims = [str(d) for d in (item.get("related_dimensions") or []) if isinstance(d, str) and d.strip()]

# _counter_evidence（Tier 2 兜底）：conflict 自带 related_dimensions，直接映射
out.append(EvidenceItem(
    statement=statement,
    source=f"conflict:{theme or 'unknown'}",
    weight="moderate",
    related_dimensions=[str(d) for d in (conflict.get("related_dimensions") or [])],
))
```

#### 3.2.4 补丁草图 — `agents/thesis/engine.py::_apply_insight`

```python
# 原全维广播（engine.py:996-1003）改为定向注入：
for item in counter_evidence_items:
    statement = str(item.get("statement", "") or "")
    if not statement:
        continue
    source = str(item.get("source", "insight") or "insight")
    related = [d for d in (item.get("related_dimensions") or []) if isinstance(d, str) and d in dimensions]
    targets = related or list(dimensions.values())   # 旧产物无字段 → 全维广播（行为兼容）
    text = f"[洞察反证] {statement} (来源: {source})"
    for dim in targets:
        dim.counter_evidence.append(text)
```

#### 3.2.5 契约面

- `orchestrator/contracts.py::InsightArtifact.counter_evidence` 为 `list[dict[str, Any]]` 透传，**无需改动**；`nodes/insights.py:53` 的 `e.model_dump(mode="json")` 自动携带新字段。
- 下游其余消费点（`payload_builders.py:571-577`、midterm 适配器 `insight_evidence_adapter.py`）只读 `statement/source/weight`，**零改动**。

### 3.3 F3 — 审查层闭环：一致性进 issues + 双条件确定性重校

#### 3.3.1 `_merge_llm`：signals_consistent 进入 issues

```python
if not review.get("signals_consistent", True):
    if v.status == "confirmed":
        v.status = "contested"
    v.conflict_description = review.get("consistency_rationale", "")
    if v.status != "contested":
        # 缺陷文档 §1.2-3：一致性发现原本只进 conflict_description、用户不可见，
        # 被挤进"语境不适配"单通道；现在并列可见。
        v.issues.append("信号一致性存疑：" + (review.get("consistency_rationale", "") or "多条信号方向冲突"))
```

#### 3.3.2 `DimensionVerdict` 增 `recalibrated_judgment`（additive）

```python
# models.py DimensionVerdict：
recalibrated_judgment: str = ""   # 审查重校后的档位；空=未重校。original_judgment 保持原值。
# to_dict() 增加 "recalibrated_judgment": v.recalibrated_judgment,
```

#### 3.3.3 确定性重校规则（LLM 提议、结构事实拍板）

`review()` 在 `_merge_llm` 之后（仅 `use_llm=True`）调用：

```python
_STRONG_DOWNGRADE = {"strong_positive": "positive", "strong_negative": "negative"}

def _apply_recalibration(self, verdicts, thesis):
    """双条件确认才降档：① LLM 层标志（语境不适配 / 信号一致性存疑 / L1 contested）；
    ② 维度确有反证文本。只降强档、不改方向、不升档——LLM 只提议，确定性条件拍板。"""
    for dim_id, v in verdicts.items():
        if v.recalibrated_judgment or v.original_judgment not in _STRONG_DOWNGRADE:
            continue
        dim = thesis.dimensions.get(dim_id)
        has_counter = bool(dim and dim.counter_evidence)
        flagged = any("语境不适配" in issue for issue in v.issues) or bool(v.conflict_description) or v.status == "contested"
        if flagged and has_counter:
            v.recalibrated_judgment = _STRONG_DOWNGRADE[v.original_judgment]
            if not v.suggested_action:
                v.suggested_action = "reconsider_with_context"
```

语义安全：`recalibrated_judgment` 只可能出现 `positive/negative`（强档降一档），永不翻转方向、永不触碰非强档维度；`original_judgment/original_score` 保持原值供审计。

#### 3.3.4 下游传导

- `agent.py::review_thesis` 的 Decision rationale 追加：`f" 审查重校={verdict.recalibrated_judgment}"`（非空时）。
- 报告载荷：`payload_builders.py:480-482` 已把 `review_val` 原样透传进 `payload.review`，`to_dict` 携带新字段后**零改动**。
- 报告 prompt（`orchestrator/prompts.py` 约 L95 `review_findings` 处）追加一条指令：
  "若 `review.dimension_verdicts[*].recalibrated_judgment` 非空，投资观点章节的维度结论以**重校档位**表述，并注明'审查重校：原判断 X，因 <issue 文案摘引> 降档'。"
- 报告 gate：**零改动**（非目标 §2.2）。

### 3.4 F4 — 同行基准传入 reviewer

#### 3.4.1 `ThesisReviewer.review` 增可选参数（显式传参，不做探测）

```python
def review(self, thesis, signal_results=None, company_context=None,
           use_llm=False, amplification=None, peer_context: dict[str, Any] | None = None):
```

`_layer1_check(..., peer_context)` 与 `_call_llm(..., peer_context)` 透传；`None` 时行为与现状逐字段一致（既有调用方/测试替身不受影响）。

#### 3.4.2 Layer 1 新 Rule 6（与既有 credit_risk 基准规则同款口径）

```python
# ── Rule 6: 同行基准缺口（缺陷文档 R6）──
if dim_id in ("capital_efficiency", "valuation_fit") and status != "insufficient" and evidence:
    peers = (peer_context or {}).get("peer_benchmarks") or {}
    if not peers:
        issues.append("缺少同行业可比基准（peer_benchmarks），该维度判断未经同行校准")
        if suggested_action not in ("reconsider_with_context", "needs_more_data"):
            suggested_action = "reconsider_with_context"
```

#### 3.4.3 Layer 2 输入与 prompt

```python
# _call_llm 签名与模板：
REVIEWER_USER_TEMPLATE = """...
## 同行基准（可能为空对象）
{peer_context_json}
..."""
peer_context_json = json.dumps(peer_context or {}, ensure_ascii=False, indent=2)
```

`REVIEWER_SYSTEM_PROMPT` 第 3 问补一句："若提供 peer_context（对标组代码与 peer_* 基准摘要），估值/资本配置/信用维度的语境判断**必须引用同行基准**；缺失时标注'行业信息不足，缺少同行业可比基准'。"

#### 3.4.4 `review_thesis` 节点组装（与 amplification 同款显式传递）

```python
from alphabee.company_track.contracts import CompanyTrackArtifact
track = find_artifact_model(artifacts, ArtifactType.COMPANY_TRACK, CompanyTrackArtifact)
peer_context = None
if track is not None:
    peer_context = {
        "track_label": track.track_label,
        "peer_group": list(track.peer_group),
        "peer_benchmarks": dict(track.peer_benchmarks),
    }
review = reviewer.review(..., peer_context=peer_context)
```

### 3.5 F5 — 方向分类器补词（附全量钉语料预演）

#### 3.5.1 词表变更

```python
# engine.py：
_BENIGN_CONFLICT_MARKERS += ("仍在提升", "未弱化", "未见弱化")
_NEGATIVE_CONFLICT_MARKERS += ("掩盖", "弱化")
```

#### 3.5.2 逐词出处披露（t22 F5 纪律：必须逐条标注语料来源）

| 词 | 表 | 语料出处 | 修复效果（603986 实测） |
|---|---|---|---|
| 掩盖 | 恶性 | **真实语料命中**：冲突"非经常性损益掩盖主业盈利质量" explanation"…被高利润增速掩盖。"，修复前误判 benign → 漏罚 | 该 verified 冲突正确扣分 |
| 弱化 | 恶性 | **真实语料命中**：冲突"市场份额增长 vs 对标组增速落后" explanation"非竞争优势弱化"——未被否定时（"竞争优势弱化"）应判 negative | 与否定窗口（"非…弱化"）配合：被否定时不命中 |
| 仍在提升 | 良性 | **真实语料命中**：同上 explanation"…份额仍在提升" | "非竞争优势弱化"+仍在提升 → benign，moat 不再被罚分 |
| 未弱化 / 未见弱化 | 良性 | explanation 语料**零命中**（出现在 verification conclusion"主业内生份额未见弱化"），按 t22 F5 口径作**前瞻拦截**保留 | 防御"份额未弱化"类证伪式措辞 |

#### 3.5.3 预演证据（附录 A）

候选词表补丁（monkeypatch 常量、不改真实代码）重放**全部既有钉**（`test_conflict_direction.py` + `test_conflict_direction_r3.py` 的 parametrize 与单值断言共 69 例）+ 6 条新语料/对抗探针：**75 用例 0 失败**。含关键对抗样本："毛利率仍在提升但收入确认激进" → negative（恶性优先）；"主业份额未弱化" → benign；"毛利率持续弱化" → negative。

#### 3.5.4 与 F1 的联动说明（如实披露）

F5 修正分类后，部分维度（如 603986 的 earnings_quality，其"掩盖"冲突从 benign 变为 negative）**罚分变严**；F1 的 ×0.5 阻尼同时把被证伪假设关联维度的分数折减。两者方向相反、按各自规则独立作用，净效果以 §6.3 全量重放为准——不预先承诺某维度最终档位，只承诺"分类与证伪两条通道都如实"。

---

## 4. 契约变化清单

| 文件 | 变更 | 类型 |
|---|---|---|
| `agents/thesis/engine.py` | 新增 `_REJECTED_SCORE_DAMP`；`_remove_disputed_contributions` 返回 bool；rejected 未溯源登记 + 循环尾折减 + `thesis_score_damped` 日志；`_apply_insight` 定向注入；F5 词表与注释 | 行为 + 注释 |
| `agents/thesis/models.py` | `DimensionVerdict.recalibrated_judgment`（additive，含 to_dict） | schema additive |
| `agents/thesis/reviewer.py` | `review()` 增 `peer_context`；`_layer1_check` Rule 6；`_merge_llm` 一致性进 issues；`_apply_recalibration`；`_call_llm` 模板参数 | 行为 |
| `agents/thesis/prompts.py` | `REVIEWER_USER_TEMPLATE` 增 `{peer_context_json}`；SYSTEM PROMPT 第 3 问补同行基准指令 | prompt |
| `agents/insights/models.py` | `EvidenceItem.related_dimensions`（additive） | schema additive |
| `agents/insights/prompts.py` | JSON 示例 + 第 7 条工作方法 | prompt |
| `agents/insights/rescue.py` | `_coerce_evidence_items` 透传；`_counter_evidence` 映射 conflict.related_dimensions | 行为 |
| `orchestrator/agent.py` | review_thesis 节点：加载 CompanyTrackArtifact 组装 peer_context；Decision rationale 附加重校信息 | 行为 |
| `orchestrator/prompts.py` | 报告 prompt 的 review_findings 指令（重校档位呈现） | prompt |
| `orchestrator/contracts.py` | **零改动**（InsightArtifact dict 透传；ThesisReview 载荷随 models.to_dict 自动扩展） | — |

**不触碰**：`OrchestratorState`、`ArtifactType` 枚举、`node_contracts.py` 的字段契约（均为 additive 载荷；实施时核对 node contract 文案无需变更）、报告 gate 逻辑、midterm 侧消费点。

---

## 5. 测试与回归计划

### 5.1 既有测试的契约更新（唯一两处有意变更）

| 测试 | 变更 |
|---|---|
| `test_rejected_disputed_rewrite.py::test_rejected_hypothesis_without_match_keeps_score_conservatively` | 重命名 `..._damps_score_and_caps_band`：新断言 `score == pytest.approx(-0.5)`、`judgment == "negative"`、counter_evidence 照常入账、context_notes 含折减留痕。旧断言（`strong_negative` + `score < -0.5`）即缺陷本身，**必须推翻** |
| `test_confidence_damping.py::test_damping_emits_audit_log_with_hit_count` / `test_damping_log_counts_per_dim_and_skips_zero_coverage` | 事件列表断言加入 `thesis_score_damped`（两测试的 rejected 假设均未溯源 ⇒ score 0.5→0.25 会触发新日志；事件序为 confidence → score 两条） |

**应保持全绿的既有钉**（F1 不触达）：`test_rejected_disputed_rewrite.py` 另两条（traced 路径）、`test_confidence_damping.py` 的 verified/unknown/300760 重建系列（重建冲突全为 verified ⇒ 无 untraced rejected）、`test_explicit_context_consumption.py`（judgment 断言 `in ("negative","strong_negative")` 在 ×0.5 后仍成立，但回归时需确认其 earnings_quality score 由 -0.875→-0.4375 不破坏其它断言）、`test_conflict_direction*.py` 全量（F5 预演已证 0 失败）、`tests/agents/insights/*`、`tests/orchestrator/*`（peer_context=None 时 F4 行为逐字段一致）。

### 5.2 新增测试

| 文件 | 用例 |
|---|---|
| `tests/agents/thesis/test_rejected_score_damp.py`（新） | ① 未溯源 rejected → score ×0.5、judgment 降档、context_notes 留痕；② 多假设命中一档封顶（×0.5 一次）；③ 正向维度对称折减（0.9→0.45）；④ score==0 跳过；⑤ traced rejected → 无折减（与既有钉同值）；⑥ 日志事件三字段齐全 |
| `tests/agents/thesis/test_insight_counter_evidence_scoping.py`（新） | ① 带 related_dimensions 的反证只进目标维度；② 无字段旧产物全维广播（兼容钉）；③ 非法维度 id 被丢弃不报错；④ 与 `_apply_conflict_analysis` 的 counter_evidence 去重共存 |
| `tests/agents/insights/test_models.py`（扩展） | EvidenceItem.related_dimensions 序列化 roundtrip |
| `tests/agents/insights/test_fallback_insight_driver_profile.py`（扩展） | Tier 2 兜底 counter_evidence 携带 conflict.related_dimensions |
| `tests/agents/thesis/test_recalibration.py`（新） | ① 双条件（LLM 标志 + 反证存在）→ strong 降一档、original 保持；② 无反证 → 不降；③ 非强档 → 不降；④ 不升档不翻向；⑤ to_dict 含 recalibrated_judgment；⑥ signals_consistent=false 且 qualified → issues 含"信号一致性存疑" |
| `tests/agents/thesis/test_reviewer_peer_context.py`（新） | ① peer_benchmarks 缺失 + valuation_fit/capital_efficiency 有证据 → Rule 6 issue + suggested_action；② 有 peer_context → 不报缺基准；③ peer_context=None → 行为与旧版一致；④ `_call_llm` 模板渲染含 peer_context_json（mock LLM 捕获输入） |
| `tests/orchestrator/test_review_thesis_peer_context.py`（新） | review_thesis 节点在有 COMPANY_TRACK artifact 时把 track_label/peer_group/peer_benchmarks 传入 reviewer.review（capture kwargs）；无 track 时传 None |
| `tests/agents/thesis/test_conflict_direction.py`（扩展，带出处注释） | 附录 A 的 6 条新语料/对抗探针入 parametrize |

### 5.3 回归命令与顺序

```bash
poetry run pytest tests/agents/thesis -q          # F1/F3/F4/F5 主战场（既有钉 + 新钉）
poetry run pytest tests/agents/insights -q         # F2
poetry run pytest tests/orchestrator -q            # 节点级 peer_context / 载荷
poetry run pytest -q                                # 全量（含 midterm / gates / task_records）
```

---

## 6. 实施顺序、重放核对与登记

### 6.1 实施顺序（每步独立可合入、可并行评审）

1. **F5**（无契约变化、预演已过）→ 2. **F1**（engine score 通道）→ 3. **F2**（insight 契约）→ 4. **F4**（reviewer 输入）→ 5. **F3**（reviewer 输出 + 报告 prompt）。
   依赖关系：F1 的 603986 重放需 F5 已合入（否则 moat 仍被"unknown 罚分"压 -1.0）；其余相互独立。

### 6.2 全量重放核对（F1+F5 合入后必做）

用 `task-6e9476f66ac8` 的输入重跑 pipeline，逐项核对：

- 竞争壁垒 score -0.5、judgment negative（"非竞争优势弱化"不再罚分 + 若命中未溯源 rejected 再 ×0.5）；
- 盈利质量/成长质量 strong_negative 闭合为 negative；
- 三条 blocking 语境不适配文案不再成立（或至少"标签 vs 反证"矛盾消失）；
- `thesis_score_damped` 日志与 context_notes 留痕可审计；
- 其余 5 维度分数/档位变化逐项记录并评估是否引入新矛盾（若出现，追加本设计补充说明，不回滚）。

### 6.3 登记

- 实施完成后在 [`docs/roadmap/ROADMAP.md`](../roadmap/ROADMAP.md) 登记五项变更（日期 / 编号 / 一行摘要 / 关联本设计与缺陷分析文档）。
- 缺陷分析文档 `THESIS_CONTEXT_MISMATCH_DEFECT_ANALYSIS.md` 头部状态更新为"已实施（F1–F5）"并附本设计链接。

---

## 附录 A：F5 词表补丁全量钉语料预演

**方法**：monkeypatch `engine._BENIGN_CONFLICT_MARKERS` / `_NEGATIVE_CONFLICT_MARKERS`（不修改真实代码），重放 `tests/agents/thesis/test_conflict_direction.py` + `test_conflict_direction_r3.py` 的全部 parametrize 与单值断言，再追加 6 条新语料/对抗探针。

```bash
poetry run python - <<'EOF'
import importlib
from alphabee.agents.thesis import engine as E
E._BENIGN_CONFLICT_MARKERS += ("仍在提升", "未弱化", "未见弱化")
E._NEGATIVE_CONFLICT_MARKERS += ("掩盖", "弱化")
# ... 重放两个测试文件的 parametrize 钉与单值钉（69 例）...
# 新增 6 例：
#   "产品结构差异：…仍在提升，非竞争优势弱化。"              → benign（修复目标 ①）
#   "非经常性收益为证券投资公允价值浮盈…被高利润增速掩盖。"  → negative（修复目标 ②）
#   "正常经营变化：…而非操纵，数据自洽性可解释。"            → benign（对照，不变）
#   "毛利率持续弱化，定价权丧失"                            → negative（对抗：未被否定的弱化）
#   "主业份额未弱化，客户黏性提升"                          → benign（对抗：未弱化）
#   "毛利率仍在提升但收入确认激进"                          → negative（对抗：恶性优先）
EOF
```

**结果：总用例 75，失败 0。**（该预演命令已在本仓库实测执行；实施 F5 后，6 条新用例以带出处注释的 parametrize 形式落入 `test_conflict_direction.py`，既有 69 例保持原样即构成回归钉。）
