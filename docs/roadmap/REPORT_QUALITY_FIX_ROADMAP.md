# AlphaBee 报告质量修复 ROADMAP（详细设计）

> 版本：v2（实现级设计，替代 v1 概览稿）
> 诊断对象：002130.SZ 20260630 运行结果（25 个系统问题，7 条 `report_rewrite_needed` high）
> 状态：待评审后进入实现

---

## 0. 文档说明与决策默认值

本文是 v1 概览稿的**实现级展开**：每个 P0 项都给出精确代码定位、before/after 代码、契约传播表、测试用例与验收标准。v1 遗留的三个待拍板决策，本文按**推荐值**写死（表 0-1），评审时如需改，只影响对应小节，不波及其它。

### 表 0-1 关键决策默认值

| # | 决策点 | 本文采用值 | 可替换为 |
|---|--------|-----------|----------|
| D1 | P0-① 税率基线语义 | 默认走公司历史 ETR；`use_statutory` 降级为"仅附注法定参考"；保留 `statutory_as_baseline` 显式开关（默认 false） | 显式开启制度基线 |
| D2 | P0-③ rejected 证据链接填充 | LLM 优先 + 关键词确定性兜底 + 未命中不扣分（保守） | 仅 LLM / 仅确定性 |
| D3 | P0-③ rejected 扣分幅度 | 一期完全归零（disputed 贡献=0）；二期引入 `residual_weight=0.1` | 一期即上 residual |

### 修复原则（约束所有设计）

1. **不重排管道**：`run_thesis` 已在 `verify_hypotheses` 之后（`alphabee/orchestrator/agent.py:312-323`）。问题在数据回写，不在顺序。
2. **外科手术式**：P0 只修契约/配置 bug；P1/P2 才是能力扩展。
3. **契约传播优先**：先改 contract → 上游生产者 → 下游消费者 → 补测试。
4. **向后兼容**：新增字段全部带默认值，旧数据可 `model_validate`。

---

## 1. 诊断摘要

上层（Conflict → Verification → Insight → Report）已能主动纠正底层伪风险；但底层（Anomaly → Signal → Thesis → Gate）存在 5 处**传导链断裂**，导致"伪风险无法在下游被消除 + 质量门控红灯仍强制交付"。

```
AnomalyEngine（baseline 设计制造伪异常：P0-①②）
        │  fact_values
        ▼
SignalEngine（anomaly_pattern → 信号，level=high）
        │  thesis_impact
        ▼
ThesisEngine（信号聚合 → dimension score/judgment）
        │  问题：rejected 假设不回改分数（P0-③）
        ▼
review_thesis（发现矛盾但只降 status，不改 judgment）
        │  问题：Decision 无证据引用（P0-④）
        ▼
review_report（度量口径错误 → 假报不一致，无意义重写）（P0-⑤）
        │
        ▼
finalize（红灯照常交付）
```

---

## 2. 修复总览

| ID | 修复项 | 根因定位 | 类型 | 依赖 | 里程碑 |
|----|--------|----------|------|------|--------|
| P0-① | 税率基线从法定 25% 改为公司历史 ETR | `rules.yaml:117-135` + `engine.py:202-208` | 配置+引擎 | 无 | M1 |
| P0-② | codir 合成 baseline 不再伪装"历史基线" + 公司事件降级 | `engine.py:333-346` | 引擎+呈现 | 无 | M2 |
| P0-③ | rejected 假设回写 dimension 分数（含双重计数修复） | `engine.py:393-400` + `schemas.py` + `explore_conflicts/prompts.py` | 引擎+契约 | 无 | M2 |
| P0-④ | Decision 补 `based_on` 证据引用 | `verification.py:219` + `agent.py:144` | 契约 | 无 | M1 |
| P0-⑤ | `cross_source_consistency` 剥离已结算冲突 | `gates.py:207-210` | 度量 | 无 | M1 |
| P1-① | `effective_score = score×confidence` + 强档降级 | `models.py:251` + `engine.py` | 评分 | 无 | M3 |
| P1-② | 证伪条件类型化（confirm/disconfirm/support/escalate） | `contracts.py:134` | 契约 | P0-③ | M3 |
| P1-③ | task_records → 认知状态（Hypothesis + WatchCondition） | `task_records/` | 架构 | P1-② | M4 |
| P2-① | peer 基准补齐 + 在线兜底 | `peer_group_build.py` + `resolve_company_track.py:89` | 数据 | 无 | M4 |

---

## 3. P0-① 税率基线：`use_statutory` 从"统计基线"改为"解释参考"

### 3.1 问题

`effective_tax_rate` 规则（`alphabee/agents/anomaly/rules.yaml:117-135`）声明 `use_statutory: true, statutory_rate: 0.25`。`AnomalyEngine._evaluate_rule`（`engine.py:202-208`）据此：

```python
baseline_mean = rule.statutory_rate            # 0.25
baseline_std  = rule.statutory_rate * 0.05     # 0.0125
```

本期 ETR=0.0866 → `z = (0.0866 - 0.25) / 0.0125 = -13.07` → high。任何 15% 优惠税率公司（高新企业）都会触发，与自身历史无关。**宁德时代、002130.SZ 两次都中招，证明是 recurring 设计缺陷。**

### 3.2 目标行为

1. 税率异常的 z-score 基于**公司自身历史 ETR**（与其它 ratio 规则一致）。
2. 法定税率仅作**解释性参考**（"显著低于法定 25%"），不参与 z-score。
3. 历史样本 < 2 期时跳过规则（`return None`），不用法定税率硬凑基线。

### 3.3 契约变化

**文件 1：`alphabee/agents/anomaly/models.py`（契约所有者）**

```python
@dataclass
class CrossRule:
    ...
    use_statutory: bool = False          # 语义改为："附注法定参考，供解释用"
    statutory_rate: float = 0.0
    statutory_as_baseline: bool = False  # 新增：默认 False；显式开启才用制度基线

@dataclass
class MetricAnomaly:
    ...
    reference_rate: float | None = None  # 新增：法定参考利率（仅解释用）
```

**文件 2：`rules.yaml`（配置，无 schema 变化）**

```yaml
- id: effective_tax_rate
  ...
  use_statutory: true          # 保留：表示附注法定参考
  statutory_rate: 0.25
  # 不新增 statutory_as_baseline（默认 false → 走历史基线）
```

### 3.4 实现设计

**`engine.py:201-216` 改后：**

```python
if rule.use_statutory and rule.statutory_as_baseline:
    baseline_mean, baseline_std = rule.statutory_rate, rule.statutory_rate * 0.05
    baseline_mode = "statutory"
else:
    baseline_mean, baseline_std = self._compute_baseline(history)
reference_rate = rule.statutory_rate if rule.use_statutory else None
```

`MetricAnomaly` 构造处补 `reference_rate=reference_rate`。

### 3.5 契约传播表

| 字段 | 生产者 | 消费者 | 影响 |
|------|--------|--------|------|
| `CrossRule.statutory_as_baseline` | `rules.yaml` → `CrossRule.from_dict`（`models.py:33`） | `engine.py` | 新增，默认 false 无破坏 |
| `MetricAnomaly.reference_rate` | `engine.py` | `reporter.py`、`payload_builders.py`、报告渲染 | 新增，可选 |

### 3.6 测试

| 用例 | 输入 | 期望 |
|------|------|------|
| 历史 ETR 稳定 | 4 期 ETR=[0.09,0.10,0.11,0.12]，本期 0.10 | level=none（\|z\|<2） |
| 历史样本不足 | history < 2 | `_evaluate_rule` 返回 `None` |
| 显式制度基线回退 | `statutory_as_baseline=true` | 保持旧行为（z 用 0.25±0.0125） |

### 3.7 验收标准

- [ ] 002130.SZ 重跑后 `effective_tax_rate.level != high`
- [ ] 报告 anomaly 章节对 `reference_rate` 非空规则追加"法定参考 25%，公司适用 15% 优惠税率时属正常"

### 3.8 风险与回滚

- 风险：部分公司历史 ETR 波动大，可能产生新误报。缓解：`threshold_sigma` 保持 2.0。
- 回滚：`rules.yaml` 加 `statutory_as_baseline: true` 即恢复旧行为（配置级）。

---

## 4. P0-② codir 合成 baseline：不再伪装"历史基线" + 公司事件降级

### 4.1 问题

codir 规则（`high_cash_high_debt`）在 `engine.py:333-346` 返回：

```python
baseline_mean = rule.threshold_sigma * 2   # 4.0
baseline_std  = 1.0                        # 合成值
current_value = z_a + z_b                  # 两字段 z 之和
```

报告渲染成"本期值 50.43 vs 历史基线均值 4.0 ± 标准差 1.0"，让读者（和 LLM 报告生成器）误以为存在真实历史基线。实际 `4.0±1.0` 只是显示约定，真实信号是现金 z 与有息负债 z 各自飙升（H 股募资导致 regime change）。

### 4.2 目标行为

1. codir 的 `baseline_mean/std` 标注为"合成"而非"历史基线"。
2. 报告展示"综合偏离度 = 现金 z + 负债 z"及各自 z。
3. 识别公司事件（募资/再融资），对受影响异常降级。

### 4.3 契约变化

**`models.py`：**

```python
@dataclass
class MetricAnomaly:
    ...
    baseline_kind: str = "statistical"          # "statistical" | "synthetic_codir" | "statutory"
    component_z: dict[str, float] | None = None # codir 时 {"cash": z_a, "interest_bearing_debt": z_b}
    regime_change: bool = False                 # 是否命中公司事件过滤
```

### 4.4 实现设计

**引擎（`engine.py` codir 分支）：**

```python
return MetricAnomaly(
    ...,
    baseline_kind="synthetic_codir",
    component_z={rule.metric_a: z_a, rule.metric_b: z_b},
    ...
)
```

**公司事件检测（新增方法 `_detect_corporate_event`）：**

```python
def _detect_corporate_event(self, snapshots) -> bool:
    """本期 financing_cashflow 单季正向 z > threshold 且 cash 同期正向 z > threshold。"""
    try:
        fc_series = self._extract_field_series("financing_cashflow", snapshots, {})
        cash_series = self._extract_field_series("cash", snapshots, {})
        # 复用 _build_history_values + _compute_baseline 计算各自 z
        ...
        return z_fc > 2.0 and z_cash > 2.0
    except Exception:
        return False   # 数据缺失静默返回，不阻断
```

命中时对 `high_cash_high_debt` 设 `regime_change=True` 并 `level` 降一级（high→medium→low→none，最小到 low）。

**报告（`reporter.py` / `REPORT_GENERATOR_PROMPT`）：**

- `baseline_kind=="synthetic_codir"` → 改写为"综合偏离度 50.43（现金 z=…，负债 z=…）"。
- `regime_change=True` → 附加"⚠ 本期疑似公司事件（如再融资/募资），异常可能无经济风险含义"。

### 4.5 契约传播表

| 字段 | 生产者 | 消费者 |
|------|--------|--------|
| `baseline_kind` | `engine.py` | 报告渲染、`payload_builders.py` |
| `component_z` | `engine.py` | 报告渲染 |
| `regime_change` | `engine.py` | 报告渲染、signal 层（可选降级） |

### 4.6 测试

| 用例 | 期望 |
|------|------|
| codir 命中 | `baseline_kind=="synthetic_codir"` 且 `component_z` 含两键 |
| fc 与 cash 同时飙升 | `regime_change=True`、level 降一级 |
| fc 数据缺失 | `_detect_corporate_event` 返回 False，不阻断 |

### 4.7 验收标准

- [ ] 002130.SZ 重跑后 `high_cash_high_debt` 带 `regime_change=True` 或降级
- [ ] 报告不再出现"历史基线 4.0±1.0"字样

---

## 5. P0-③ rejected 假设回写 dimension 分数（核心）

### 5.1 问题

`_apply_conflict_analysis` 的 rejected 分支（`engine.py:393-400`）只 append `dim.counter_evidence` 字符串，**从不回改触发该维度的信号贡献**。导致"大存大贷"假设已证伪，`credit_risk` 仍被压成 `strong_negative=-1.0`。

### 5.2 隐藏 bug：双重计数

同一"大存大贷"模式以两条路径各扣一次 `credit_risk`：

| 路径 | 触发点 | source_id | 贡献 |
|------|--------|-----------|------|
| 信号路径 | `anomaly_pattern_high_cash_high_debt.yaml:38-42`（`thesis_impact.credit_risk.high=negative`） | `anomaly_pattern_high_cash_high_debt`（source_type="signal"） | `1.0 × -1.0 = -1.0` |
| 异常直连 | `engine.py:_consume_anomaly_report`（`level_scale=0.45`） | `anomaly_pattern:high_cash_high_debt`（source_type="anomaly"） | `1.0 × 0.45 × -1.0 = -0.45` |

rejected 回写必须**同时删除两条**。

### 5.3 目标行为

- rejected 假设指向的异常模式，其相关维度**移除该模式全部负向贡献**（两条路径），重算 `judgment`。
- 其余未被证伪的负面信号保留，维度不是整体归零，而是精确扣除被证伪的那一块。

### 5.4 契约变化（跨模块）

**文件 1：`alphabee/agents/schemas.py`（冲突假设契约，需确认假设模型类名）**

给 hypothesis 增加可选争议证据字段：

```python
disputed_pattern_ids: list[str] = Field(default_factory=list)  # 如 ["high_cash_high_debt"]
disputed_signal_ids: list[str] = Field(default_factory=list)   # 如 ["anomaly_pattern_high_cash_high_debt"]
```

**文件 2：`alphabee/agents/explore_conflicts/prompts.py`（生产者）**

`EXPLORE_CONFLICTS_PROMPT` 增加指令：对每个假设给出 `disputed_pattern_ids`/`disputed_signal_ids`（候选 id 列表由上游注入 prompt）。

### 5.5 实现设计

**Step 1 — 保留有效贡献（`engine.py:148-167` 重构）**

当前 `score = sum(effective_scores) / len(effective_scores)` 计算完即丢弃。改为：

```python
dim_effective: dict[str, list[tuple[str, str, float]]] = {}  # dim_key -> [(signal_id, source_type, eff_score)]
for dim_key, contribs in dim_contributions.items():
    eff = [(e.signal_id, e.source_type, ls * d) for (ls, d, e) in contribs]
    dim_effective[dim_key] = eff
    score = sum(v for _, _, v in eff) / len(eff) if eff else 0.0
```

> 注：`EvidenceItem` 已携带 `signal_id` + `source_type`，无需扩展 `_append_contribution` 元组（修正 v1 描述）。

**Step 2 — 假设 ↔ 证据链接（两级填充）**

1. LLM 优先：`explore_conflicts` 输出 `disputed_*`。
2. 确定性兜底（`engine.py` 新增 `_infer_disputed_evidence`）：对 rejected 假设的 `explanation` 做关键词匹配——与 `ANOMALY_PATTERNS` 的 `name`、`signal_results` 的 `signal_id` 模糊匹配，命中则填 `disputed_*`。

**Step 3 — rejected 扣分（`engine.py:_apply_conflict_analysis` rejected 分支改后）**

```python
elif status == "rejected":
    disputed_signal = set(hypothesis.get("disputed_signal_ids") or [])
    disputed_pattern = set(hypothesis.get("disputed_pattern_ids") or [])
    for dim_id in dim_ids:
        dim = dimensions.get(dim_id)
        eff = dim_effective.get(dim_id, [])
        kept = [
            (sid, stype, v) for (sid, stype, v) in eff
            if not (
                (stype == "signal" and sid in disputed_signal)
                or (stype == "anomaly" and any(sid == f"anomaly_pattern:{p}" for p in disputed_pattern))
            )
        ]
        if kept != eff:
            dim_effective[dim_id] = kept
            dim.score = sum(v for _, _, v in kept) / len(kept) if kept else 0.0
            dim.evidence = [
                e for e in dim.evidence
                if not (
                    (e.source_type == "signal" and e.signal_id in disputed_signal)
                    or (e.source_type == "anomaly" and any(e.signal_id == f"anomaly_pattern:{p}" for p in disputed_pattern))
                )
            ]
        dim.counter_evidence.append(message)
        dim.confidence = max(0.0, dim.confidence - 0.05)
```

最后 `_refresh_dimensions`（`engine.py:565`）重算 `judgment`，实现 judgment 跟随扣分更新。

> 镜像参考：现有 `_apply_verified_conflict`（`engine.py:416-447`）已经是"事后直接调 score"的先例，本设计与其同构（一个加负分、一个扣分）。

**Step 4 — 残留风险（二期，D3）**

一期 disputed 贡献归零；二期引入 `residual_weight=0.1`，保留极小残余负向，避免"一证伪就清零"。

### 5.6 契约传播表

| 字段 | 生产者 | 消费者 |
|------|--------|--------|
| `disputed_pattern_ids`/`disputed_signal_ids` | `explore_conflicts`（LLM）+ `engine._infer_disputed_evidence`（兜底） | `engine._apply_conflict_analysis` |
| `dim_effective`（内部） | `engine.run` | `engine._apply_conflict_analysis` |

### 5.7 测试

| 用例 | 期望 |
|------|------|
| high_cash_high_debt → credit_risk，假设 rejected | credit_risk score 从 -1.0 回升到 neutral/negative 区间，judgment 不再 strong_negative |
| 双重计数验证 | rejected 后同时移除 `signal:anomaly_pattern_high_cash_high_debt` 与 `anomaly_pattern:high_cash_high_debt` 两条贡献 |
| `disputed_*` 为空走兜底 | 关键词命中后行为与显式一致 |
| `disputed_*` 未命中 | 不扣分，保持现状（保守） |

### 5.8 验收标准

- [ ] 002130.SZ 重跑后 `credit_risk.judgment != strong_negative`
- [ ] `review_thesis` 不再为 credit_risk 报"thesis 判断 strong_negative 但存在较强反向信号"

### 5.9 风险与回滚

- 风险：错误扣分（误删未证伪信号）。缓解：未命中不扣分；关键词兜底要求精确匹配。
- 风险：`score` 重算破坏 `_compute_overall`。缓解：`_refresh_dimensions` 之后才 `_compute_overall`（`engine.py:197-200`，当前顺序已满足）。
- 回滚：此改动在 `engine.py` 内局部，git revert 单文件即可。

---

## 6. P0-④ Decision 补 `based_on` 证据引用

### 6.1 问题

`evidence_coverage=0.0`、`grounding_score=0.0`、`build_evidence_map` 返回空。根因：全流水线 `Decision` 不带 `based_on`/`evidence_refs`：

- `verification.py:219`（rejected Decision）只有 `rationale` + `confidence`。
- `agent.py:144`（review_thesis 维度 Decision）同样无引用。
- 唯一带 `based_on` 的是 `gates.py:435`，但它在 `compute_report_metrics` 之后创建，不影响度量。

### 6.2 目标行为

- 每个 Decision 都能追溯到其消费的 artifact。
- `compute_report_metrics` 的 `evidence_coverage > 0`、`grounding_score > 0`、`build_evidence_map` 非空。

### 6.3 实现设计

**新增 helper（`collectors.py` 或就近）**：现有 `_find_artifact`（返回 value）与 `find_artifact_model`（返回 payload）都**不返回 artifact id**，需补一个：

```python
def _find_artifact_id(artifacts: list[Artifact], artifact_type: str) -> str | None:
    for a in reversed(artifacts):
        if a.type == artifact_type:
            return a.id
    return None
```

**`verification.py` rejected Decision 改后：**

```python
conflicts_result_artifact_id = _find_artifact_id(state.get("artifacts", []), ArtifactType.CONFLICTS_RESULT)
...
settled_decisions.append(
    Decision(
        ...,
        based_on=[conflicts_result_artifact_id] if conflicts_result_artifact_id else [],
    )
)
```

**`agent.py` review_thesis 维度 Decision 改后：**

```python
thesis_artifact_id = _find_artifact_id(artifacts, ArtifactType.THESIS_ANALYSIS)
signal_artifact_id = _find_artifact_id(artifacts, ArtifactType.SIGNAL_ANALYSIS)
...
Decision(
    ...,
    based_on=[x for x in (thesis_artifact_id, signal_artifact_id) if x],
)
```

**校验（`gates.py` 前置，非阻塞）：** `review_report` 前若所有 Decision 的 `based_on`+`evidence_refs` 为空，产出 DATA-scope warning，提示证据链未闭环。

### 6.4 测试

| 用例 | 期望 |
|------|------|
| 完整 pipeline 跑一次 | `evidence_coverage > 0` 且 `grounding_score > 0` |
| `build_evidence_map(state)` | 返回非空列表 |

### 6.5 验收标准

- [ ] 002130.SZ 重跑后 `evidence_coverage` 由 0.0 变为正数

---

## 7. P0-⑤ `cross_source_consistency` 口径

### 7.1 问题

`gates.py:207-210`：

```python
cross_source_consistency = not any(
    category in issue_categories
    for category in {"cross_source_conflict", "verified_conflict", "time_mismatch", "thesis_conflict"}
)
```

`verified_conflict`（`verification.py:206-214`）是**已正确结算的冲突披露项**，`thesis_conflict`（`agent.py:212-224`）是**已识别的论点矛盾**。把它们算进"不一致"，导致任何做了实质冲突验证的报告都必然 false，再叠加 `_deterministic_assessment`（`gates.py:296`）触发无意义重写。

### 7.2 设计

```python
UNRESOLVED_INCONSISTENCY = {"cross_source_conflict", "time_mismatch", "numeric_inconsistency", "conflict"}
SETTLED_CONFLICT = {"verified_conflict", "thesis_conflict"}

cross_source_consistency = not any(c in issue_categories for c in UNRESOLVED_INCONSISTENCY)
```

`SETTLED_CONFLICT` 改由 `issue_handling`（`gates.py:82-93` 已检查 `disclosed_issue_ids`）承接披露检查。

同步更新 `_deterministic_assessment`（`gates.py:296`）阻断文案：仅当 `UNRESOLVED_INCONSISTENCY` 非空才报"存在跨来源/跨维度冲突"。

### 7.3 测试

| 用例 | 期望 |
|------|------|
| 含 verified_conflict + thesis_conflict，不含 cross_source_conflict | `cross_source_consistency == True` |
| 含 cross_source_conflict | `False` |

### 7.4 验收标准

- [ ] 002130.SZ 重跑后 `cross_source_consistency == True`（其冲突均已被 verified/rejected/partial 结算）

---

## 8. P1-① `effective_score` + 强档降级

### 8.1 问题

`score_to_judgment`（`models.py:251`）只认 `score`，`confidence` 独立计算（`engine.py:163-166`），出现"strong_negative -1.0 @ confidence 10%"的自相矛盾表达。

### 8.2 设计（MVP）

`ThesisDimension` 增加：

```python
effective_score: float = 0.0   # score × confidence
```

`to_dict` 输出 `effective_score`。`score_to_judgment` 增加可选参数：

```python
def score_to_judgment(score: float, confidence: float = 1.0) -> str:
    if confidence < 0.3 and score in (-1.0, 1.0):
        return "negative" if score < 0 else "positive"  # 强档但极低置信 → 普通档
    ...  # 原逻辑
```

报告维度分析同时展示 `judgment / score / confidence / effective_score`。

### 8.3 后续（二期）

拆为 `Direction / Magnitude / Confidence / Decision Weight` 四元组，聚合用 `direction × magnitude × confidence × evidence_quality × materiality`。本 ROADMAP 仅落 MVP。

---

## 9. P1-② 证伪条件类型化

### 9.1 问题

`InsightArtifact.what_would_change_my_mind: list[str]`（`contracts.py:134`）是无类型字符串，无法区分"证伪/确认/支持/风险升级"。报告里"OCF/NP≥1 且 gap<10pp → 证明主动备货"是过度武断——它只能证伪"被动积压"，不能确认"主动备货"。

### 9.2 设计

```python
class FalsificationCondition(BaseModel):
    condition: str = ""
    kind: str = "disconfirm"   # confirm | disconfirm | support | escalate
    direction: str = ""        # 成立时对当前观点的影响方向

class InsightArtifact(...):
    what_would_change_my_mind: list[FalsificationCondition] = Field(default_factory=list)
```

兼容：旧 `list[str]` 经 coerce 成 `kind="disconfirm"`。

报告按 kind 分组渲染，明确写"此条件**削弱**当前观点，而非证明相反观点"。

---

## 10. P1-③ task_records → 认知状态系统

### 10.1 现状

`alphabee/task_records/` 已有 `TaskRecord`（持久化 signal/anomaly/dimension/review/report/peer）及 `analyzer.py`（高频高 z 规则统计）、`distiller.py`（阈值调整建议）。目前是"事后日志"，缺"可查询研究状态"。

### 10.2 设计（分期）

- **一期**：新增 `HypothesisRecord` 与 `WatchCondition` 模型，字段对齐 P1-②（condition/kind/direction）+ `status`（open/confirmed/disconfirmed/escalated）+ `evidence_snapshot`。
- **二期**：`UpdateService`——输入下期 `TaskRecord`，逐条评估 `WatchCondition`，更新 `status` 与置信度，产出"假设状态迁移报告"。
- **三期**：持久化从"单文件/日志"升级为可查询存储（SQLite/JSONL 索引），支持 `symbol + hypothesis + date` 检索。

独立 Sprint，不在 M1-M3 内。

---

## 11. P2-① peer 基准补齐 + 在线兜底

### 11.1 问题

`data/peer_groups/` 只有 `601138.SH.json`、`603986.SH.json`，缺 `002130.SZ.json`。`resolve_company_track`（`resolve_company_track.py:89`）只读不建 → `[low] peer_group_missing`。

### 11.2 设计

- 离线：为 002130.SZ 跑 `build_peer_group`（`alphabee/company_track/peer_group_build.py`），落盘 `data/peer_groups/002130.SZ.json`。
- 在线兜底：`PeerGroupStore().load()` 未命中时尝试 `build_peer_group(...)`，失败才走 `peer_group_missing` 降级。
- 批量回填脚本（`scripts/`）为历史标的补配置。

---

## 12. 全局验证策略

### 12.1 黄金回归集

把 002130.SZ、宁德时代做成快照测试，跑完 pipeline 断言：

```text
effective_tax_rate.level != high
cross_source_consistency == True
evidence_coverage > 0
credit_risk.judgment != strong_negative
```

### 12.2 契约回归

所有新增字段走 `model_validate` 旧数据兼容测试。

### 12.3 误报率基线

用 `task_records/analyzer.py` 统计修复前后各规则 high 触发率，量化 false positive 下降。

---

## 13. 里程碑与依赖

| 里程碑 | 内容 | 依赖 | 建议窗口 |
|--------|------|------|----------|
| M1 | P0-①⑤④ | 无 | 第 1 周 |
| M2 | P0-②③ | 无（P0-③ 独立） | 第 2 周 |
| M3 | P1-①② | P0-③ | 第 3 周 |
| M4 | P1-③ + P2-① | P1-② | 第 4 周起 |

> P0-③ 是唯一跨三模块（`schemas.py` / `explore_conflicts/prompts.py` / `engine.py`）的改动，建议先评审其 `disputed_*` 契约再动手。

---

## 14. 风险与回滚总表

| ID | 主要风险 | 缓解 | 回滚方式 |
|----|----------|------|----------|
| P0-① | 历史 ETR 波动新误报 | threshold 2.0 + reference_rate 保留 | 配置级 `statutory_as_baseline: true` |
| P0-② | `_detect_corporate_event` 误判 | 数据缺失静默 False；仅降一级 | 单文件 revert |
| P0-③ | 错误扣分 | 未命中不扣分；精确匹配兜底 | `engine.py` 单文件 revert |
| P0-④ | 引入 None id | `based_on` 过滤空值 | 单文件 revert |
| P0-⑤ | 把真实不一致漏报 | `UNRESOLVED_INCONSISTENCY` 仍覆盖 cross_source/time/numeric | 单文件 revert |
