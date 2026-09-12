# AlphaBee 洞察力注入设计：中期决策层从「机械执行规则」到「证据条件化」

> 版本：v1（设计稿，待评审）
> 触发案例：沃尔核材（002130.SZ）中期决策 —— 手动分析判 S2，AlphaBee 判 S4 并输出「清仓」
> 核心问题：如何让 AlphaBee 拥有「洞察力」（看到结构性突变、区分实质与噪音），而不是机械执行规则

---

## 0. 结论先行

**AlphaBee 已经有洞察力，但它被丢弃在「洞察层 → 中期决策层」的边界上。**

上层 `synthesize_insights` 产出的 `InsightArtifact` 已经包含真正的洞察：

- `supporting_evidence` / `counter_evidence`（带 weight 的正反证据，含"高速通信线+35.44%""448G试产""产能到货"）
- `materiality_rank`（关键变量重要性排序，含"汇兑损益是 critical、商誉是 high"）
- `bull_case / base_case / bear_case`（带**前提条件、触发因素、传导路径**的情景推演）
- `business_model_context`（商业模式语境）

但 `resolve_midterm_decision` 节点（`alphabee/orchestrator/nodes/midterm.py:194-207`）只把两样东西传给中期决策模型：

```python
hypothesis  = _resolve_hypothesis(insight, thesis)  # 只取 insight.core_view（一句话）
prior       = _prior_confidence(insight, thesis)     # 只取 insight.confidence（low/medium/high → 0.3/0.5/0.7）
window_texts= _window_texts(artifacts)               # 只取 verified conflict explanations
```

**上面四个字段（supporting/counter/materiality/bull_bear_case）全部被丢弃。** 而中期决策层 `alphabee/midterm/` 是一个纯确定性的「因子→状态→EV→仓位」引擎，它只接收：

1. 7 个因子方向分（F/E/T/V/C/R/M）
2. 一句话 thesis（作为 H 假设）
3. 一个标量置信度（prior）

于是"洞察"被压缩成了一个标量，剩下的计算全部由**硬编码查表 + 结构式公式**驱动。这就是"机械执行规则"的精确来源。

---

## 1. 机械性的五个代码级根因

### 根因 1：情景概率是硬编码状态表，与具体证据无关

`alphabee/midterm/bayes.py:38-53` 定义了一张状态锚定表：

```python
_STATE_BULL = {"S0":0.33, "S1":0.30, "S2":0.45, "S3":0.55, "S4":0.35, "S5":0.10}
_STATE_BEAR = {"S0":0.33, "S1":0.30, "S2":0.20, "S3":0.15, "S4":0.32, "S5":0.60}
```

`scenario_probability`（`bayes.py:183-248`）只由 `state` + `confidence` + `_STATE_BULL_DIRECTION` 驱动：

```
沃尔核材：state=S4, confidence=1.0, direction=-1.0（S4 看空）
c = 2×1.0 − 1.0 = 1.0
p_bear = clip(0.32 − 1.0×0.35×(−1.0)) = clip(0.67) = 0.67   ← 报告里的 67%
p_bull = clip(0.35 + 1.0×0.35×(−1.0)) = clip(0.0)  = 0.02   ← 报告里的 2%
```

**bear=67%、bull=2% 完全由 `(S4, 1.0, -1)` 这张表算出**，与"448G 试产""产能到货""高速通信线+35.44%"这些具体证据**零关系**。证据只通过一个标量 `confidence` 间接影响极化幅度，从不改变方向、也不改变情景本身。

### 根因 2：`thesis_confidence` 饱和到 1.0

`bayes.py:129-153` 的 `update_confidence` 是 log-odds 累加后 sigmoid：

```python
lo = _logit(prior or 0.5)
for event in events:
    lo += _event_logodds(event)
return _sigmoid(lo)
```

沃尔核材的 H 假设本身已经写着"高风险信号大多是假警报"，而证据适配器把 `verified→confirming`（`evidence_adapter.py:43-48`），于是验证结论大多 confirm H → log-odds 一路累加 → sigmoid 饱和到 **1.0**。`confidence=1.0` 又触发根因 1 的**全极化**（c=1.0），把 bear 推到 67%。

**问题**：置信度没有上限校准，也没有让"反证"（refuting）足够强力地拉回。refuting 证据虽然存在（"存货增速远超营收""税前利润下滑低税率掩盖"），但其强度（weak/medium/strong → 0.1/0.3/0.5）不足以抵消多条 confirming 的累积。

### 根因 3：证据只映射 verification，丢弃 insight 的结构化证据

证据事件有两个来源（`decision_model.py:329-379` 的 `collect_evidence`）：

1. 数值类规则 `build_numeric_evidence`（预期 facts + 一致预期）
2. 定性 Stage A/B（`window_texts` → `extract_facts` → `judge_facts`）

而 `window_texts` 只包含 **verified conflict explanations**（`midterm.py:141-162`），`evidence_adapter.py` 也只映射 **VerificationResultItem**。

**`insight.supporting_evidence` / `counter_evidence`（带 weight 的正反证据）从未进入证据日志。** 这就是"高速通信线+35.44%"这类结构性洞察在中期决策里彻底消失的原因。

### 根因 4：EV 收益是结构式公式，与公司具体下行无关

`decision_model.py:49-52, 173-175`：

```python
_BULL_SCALE = 40.0; _BEAR_SCALE = 30.0; _BEAR_BASE = 10.0
bull_val = (1.0 - percentile) * _BULL_SCALE
bear_val = -(percentile * _BEAR_SCALE + _BEAR_BASE)   # ← 沃尔核材 bear -27.66% 的来源
```

bear 的下行幅度只由**估值分位**决定，与"存货去化失败 / 商誉减值 / 汇兑持续"这些 insight 层识别出的真实下行路径**无关**。一个存货风险公司和一个商誉风险公司，只要估值分位相同，bear 收益就一样。

### 根因 5：拥挤度 / 市场 regime 一刀切，且 EV 门槛是硬否决

**拥挤度**（`score_engine.py:185-205`）：

```python
if c.turnover_rate_percentile is not None:
    contribs.append(1.0 - 2.0 * c.turnover_rate_percentile)  # 分位 0.8 → -0.6
```

换手率历史分位 0.7977 → 直接 -0.595。**没有区分"启动初期的高换手"和"高位派发的拥挤"**——成长股在催化剂驱动的启动期，换手率天然偏高，这不等于"overheated"。

**市场 regime**（`position.py:179-182`）：`actual_weight = portfolio_exposure × stock_weight`，熊市 `PositionAdvice=[0, 0.2]` → 暴露被压到 0-0.2，叠加 EV 门槛。

**EV 硬否决**（`position.py:165-167`）：

```python
if risk_adjusted_ev is not None and risk_adjusted_ev < _EV_THRESHOLD:  # 1.0
    stock_weight = 0.0   # ← 沃尔核材 RiskAdjustedEV=-0.6581 → 直接清仓
```

软状态机制（`position.py:144-146` 的 `expected_weight = Σ P(S_k)·weight_band(S_k)`）其实**已经**能通过高熵自动压低仓位，但这条 `RiskAdjustedEV < 1.0 → 压 0` 的硬规则把它覆盖了——高熵的"不确定"没有传导到"不清仓"。

---

## 2. 关键发现：洞察已经存在，但被丢弃在边界

对照一下"洞察层产出了什么" vs "中期决策层收到了什么"：

| InsightArtifact 字段 | 内容示例（沃尔核材） | 是否传给 midterm |
|----------------------|---------------------|------------------|
| `core_view` | 一句话核心观点 | ✅（作为 thesis H） |
| `confidence` | low/medium/high | ✅（→ prior 0.3/0.5/0.7） |
| `supporting_evidence` | 高速通信线+35.44%、448G试产、产能到货 | ❌ **丢弃** |
| `counter_evidence` | 增收不增利、商誉、资产周转低 | ❌ **丢弃** |
| `materiality_rank` | 汇兑 critical / 商誉 high / 现金流 high | ❌ **丢弃** |
| `bull_case / bear_case` | 带前提条件 + 触发因素 + 传导路径 | ❌ **丢弃** |
| `business_model_context` | 成熟医疗设备 / 出海汇率敏感 | ❌ **丢弃** |

**结论**：不是"AlphaBee 没有洞察力"，而是"洞察层的洞察没有接进决策层"。这是一个**接线问题**，不是**能力问题**。

---

## 3. 设计原则

1. **确定性核心不改，但要从"先验/查表驱动"改成"证据条件化"**：状态机、EV、仓位仍是确定性纯函数，但它们的输入不再是"7 因子 + 1 标量"，而是"7 因子 + 结构化证据"。
2. **证据不能只压缩成一个标量**：当前 evidence 只影响 `confidence`（标量）。证据的方向、强度、主题（fundamental/expectation/trend/crowding/thesis）应分别影响状态分布、情景概率、EV 幅度。
3. **洞察层的四个字段是现成的证据源**：`supporting_evidence`/`counter_evidence`（weight → 强度）、`materiality_rank`（→ 情景收益幅度）、`bull/bear_case`（→ 情景触发条件）。
4. **结构性突变（segment inflection）要作为一等公民**：`company_track` 已有 segments，但 F 因子只看整体。要让"高速通信线+35.44% vs 整体+1.54%"成为可识别的结构性信号。
5. **不确定性要传导到动作**：高熵（1.666）+ argmax 非多数（35.7%）应输出"减仓/观察"而非"清仓"。

---

## 4. 改造方案（按优先级）

### 改造 A（核心）：把 insight 的正反证据注入 EvidenceEvent

**现状**：`midterm.py:194-199` 只取 `core_view` + `confidence`；证据日志只有数值规则 + verified conflicts。

**目标**：`insight.supporting_evidence` / `counter_evidence` 成为证据日志的一等来源。

**契约变化**：`InsightArtifact.supporting_evidence/counter_evidence` 已是 `list[{statement, source, weight}]`（`contracts.py`），其中 `weight ∈ {strong, moderate}`。新增一个适配器，映射到 `EvidenceEvent`：

```python
# alphabee/midterm/insight_evidence_adapter.py（新）
_WEIGHT_TO_STRENGTH = {"strong": Strength.STRONG, "moderate": Strength.MEDIUM}

def adapt_insight_evidence(insight: InsightArtifact, *, date: str) -> list[EvidenceEvent]:
    events = []
    for ev in insight.supporting_evidence:
        events.append(EvidenceEvent(
            kind="thesis",
            effect_on_thesis="confirming",
            confidence_delta=STRENGTH_DELTA[_WEIGHT_TO_STRENGTH[ev.weight]],
            description=ev.statement,   # 高速通信线+35.44% 等
            ...
        ))
    for ev in insight.counter_evidence:
        events.append(EvidenceEvent(..., effect_on_thesis="refuting", ...))
    return events
```

**接线**：`resolve_midterm_decision` 里，把 `adapt_insight_evidence(insight)` 合并进 `collect_evidence` 的结果（或作为独立 evidence 源传入 `get_decision`）。

**验证**：沃尔核材重跑后，`evidence_log` 里应出现"高速通信线+35.44%（confirming）"和"增收不增利（refuting）"两条，而不是只有 verified conflicts。

---

### 改造 B（核心）：证据条件化情景概率，替换硬编码状态表

**现状**：`scenario_probability(state, confidence, has_evidence)` 只用状态表 + confidence 极化。

**目标**：情景概率 = 状态锚点（先验）× 证据净方向（后验修正），而非只由 confidence 极化。

**设计**：给 `scenario_probability` 增加一个由证据日志推导的 `net_tilt`（净方向偏斜）：

```python
# bayes.py 新增
def _evidence_tilt(events: list[EvidenceEvent]) -> float:
    """净证据方向：Σ(confirming·d − refuting·d)，clip 到 [-1,1]。"""
    tilt = 0.0
    for e in events:
        if e.effect_on_thesis == "confirming":
            tilt += e.confidence_delta
        elif e.effect_on_thesis == "refuting":
            tilt -= e.confidence_delta
    return max(-1.0, min(1.0, tilt))

def scenario_probability(state, *, confidence=None, has_evidence=None, events=None):
    ...
    # 原：direction = _STATE_BULL_DIRECTION[state]（硬编码看多/看空）
    # 新：direction = 0.5 * state_direction + 0.5 * evidence_tilt（证据可扭转方向）
    state_dir = _STATE_BULL_DIRECTION[state]
    ev_tilt = _evidence_tilt(events or [])
    direction = 0.5 * state_dir + 0.5 * ev_tilt
    p_bull = _clip_prob(b0 + c * _CONFIDENCE_MAGNITUDE * direction)
    p_bear = _clip_prob(be0 - c * _CONFIDENCE_MAGNITUDE * direction)
```

**关键变化**：`direction` 不再由状态硬编码，而是"状态先验方向"与"证据净方向"各占一半。沃尔核材若 `supporting_evidence`（高速通信线、产能、订单）足够强，`ev_tilt > 0` 会**部分抵消** S4 的看空方向，bear 概率不会被动推到 67%。

**验证**：构造一个"状态 S4 但证据强 confirming"的用例，断言 bear < 0.5（不再被状态表锁死）。

---

### 改造 C：置信度校准（防饱和 + 反证加权 + 熵传导）

**现状**：`update_confidence` 无上限，sigmoid 饱和到 1.0；`position.py` 的 EV 硬否决覆盖了高熵的保守化。

**设计**（三处小改）：

1. **置信度上限**：`update_confidence` 返回值夹紧到 `[0.05, 0.95]`（`_CONFIDENCE_CLAMP`），避免 1.0 全极化。理由：没有证据能让人 100% 确信一个未证实的 thesis。
2. **反证加权**：`_event_logodds` 对 refuting 用更大的灵敏度（如 `refuting` 的 log-odds 放大 1.2×），因为"反证"在贝叶斯里应该比"佐证"更有信息量（不对称）。
3. **熵传导到仓位**：`build_position` 里，把 `_EV_THRESHOLD` 硬否决改成"软阈值"——`RiskAdjustedEV < 1.0` 时不直接压 0，而是 `stock_weight *= 0.3`（减仓而非清仓），仅当 `RiskAdjustedEV < 0` 且 `entropy > _ENTROPY_UNCERTAIN` 时才压 0。这样高熵的"不确定"真正降低动作力度，而不是被硬规则覆盖。

**验证**：沃尔核材重跑后，`thesis_confidence` 应 ≤ 0.95（不再 1.0）；`position_band` 应是"减仓/观察"而非"清仓"。

---

### 改造 D：EV 收益 materiality 化

**现状**：`bear_val = -(percentile×30+10)`，只由估值分位决定。

**目标**：bear/bull 收益幅度由 `insight.materiality_rank` + `bull_case/bear_case` 的触发因素决定。

**设计**：把 `materiality_rank` 里的 critical/high 变量映射为情景收益的**幅度修正项**：

```python
# decision_model.py
def _estimate_expected_value(..., insight_materiality=None):
    base_bear = -(percentile * _BEAR_SCALE + _BEAR_BASE)
    # materiality 修正：critical 风险项越多，bear 下行越深（但这是"公司特定"的，不再是通用公式）
    materiality_penalty = 0.0
    for item in (insight_materiality or []):
        if item.get("importance") == "critical" and item.get("variable") in _DOWNSIDE_VARS:
            materiality_penalty += 5.0   # 每个 critical 下行变量加深 bear 5%
    bear_val = base_bear - materiality_penalty
```

`_DOWNSIDE_VARS` 从 `materiality_rank` 的变量名识别（如"存货去化与减值""商誉减值""汇兑持续"）。这样 bear 幅度**部分来自公司的真实下行路径**，而非纯估值分位公式。

**验证**：一个"商誉+存货双 critical"公司与一个"无 critical 下行变量"公司，bear 收益应不同。

---

### 改造 E：结构性洞察（segment inflection）+ 语境归一化

**现状**：F 因子只看整体 `revenue_yoy/net_profit_yoy/eps_growth_yoy`（`score_engine.py:117-137`），`company_track.segments` 未进 F。

**目标**：识别"整体增速 1.54% 但细分业务 +35.44%"的结构性突变。

**设计**：

1. **segment divergence 进 F**：`FactorSnapshot.fundamental` 新增 `segment_fastest_yoy` / `segment_slowest_yoy`（来自 `company_track` 的 segments）。当"最快细分增速 >> 整体增速"时，F 方向分不再只看整体，而是把"结构性亮点"作为正向修正：

```python
if f.segment_fastest_yoy and f.revenue_yoy:
    divergence = f.segment_fastest_yoy - f.revenue_yoy   # +35.44% - +17.94% = +17.5pp
    if divergence > 15:   # 细分业务显著跑赢整体 → 结构性突变信号
        growth += 0.2     # F 方向分正向修正（结构性亮点）
```

2. **拥挤度语境归一化**（`score_engine.py:_crowding`）：当 E 因子（分析师上修）或 segment divergence 显著为正时，高换手率分位应部分豁免——"催化剂驱动的启动期换手" ≠ "高位派发的拥挤"：

```python
if c.turnover_rate_percentile is not None:
    crowding_pct = 1.0 - 2.0 * c.turnover_rate_percentile
    if e_revision > 0.3 or segment_divergence > 15:   # 有强催化/结构性亮点时
        crowding_pct *= 0.5   # 拥挤度扣分减半（启动期高换手部分豁免）
    contribs.append(crowding_pct)
```

3. **市场 regime 软约束**：熊市仍压低暴露（保留），但当 `E↑（上修）+ segment divergence` 同时成立时，允许暴露下限上浮（如 [0, 0.2] → [0.1, 0.3]），体现"熊市里的结构性主线"。

**验证**：沃尔核材重跑后，C 因子不再是一票否决的 -0.51，F 因子应反映"高速通信线+35.44%"的结构性亮点。

---

## 5. 架构定位：从「确定性核心 + LLM 注解」到「确定性核心 + 证据条件化」

当前架构的隐含假设是：

```
确定性核心（7因子 → 状态 → EV → 仓位）  ← 机械
     ↑ 只接收一个标量 confidence
LLM 洞察层（core_view + evidence + materiality + scenarios）  ← 有洞察
```

洞察层的产出被降维成"一句话 + 一个标量"喂给确定性核心，于是确定性核心只能机械地查表。

**目标架构**：

```
LLM 洞察层
  ├─ supporting/counter evidence  →  EvidenceEvent（改造A）
  ├─ materiality_rank             →  EV 收益幅度（改造D）
  ├─ bull/bear_case               →  情景触发条件
  └─ business_model_context       →  语境归一化（改造E）

确定性核心（状态 → 情景概率 → EV → 仓位）
  ↑ 接收「结构化证据」，而非「一个标量」
  ├─ 情景概率 = 状态锚点 × 证据净方向（改造B）
  ├─ 置信度 = 校准后的后验（改造C）
  └─ 仓位 = 软状态 × 校准置信度 × 证据化EV × 软约束（改造C/E）
```

**关键转变**：证据从"压缩成一个 confidence 标量"变成"结构化地参与状态、情景、EV、仓位四个计算"。这样确定性核心仍保持可回测、可解释，但它不再机械——因为它的输入携带了洞察。

这与之前讨论的「Validated Evidence Graph」和「Hypothesis 作为研究核心对象」是同一个方向：**让证据成为决策计算的一等公民，而不是 LLM 输出后的一个数字注解。**

---

## 6. 落地顺序与验证

| 优先级 | 改造 | 改动面 | 效果 | 风险 |
|--------|------|--------|------|------|
| P0 | A（证据注入） | 新适配器 + `midterm.py` 接线 | 结构性证据进入决策 | 低，纯新增 |
| P0 | C（置信度校准） | `bayes.py` + `position.py` | 消除 1.0 饱和 + 高熵不再被硬否决 | 低，参数化 |
| P1 | B（证据条件化情景概率） | `bayes.py` | bear 不再被状态表锁死 | 中，需回测 |
| P1 | E（结构性洞察 + 语境归一化） | `score_engine.py` + `factors.py` | 看整体也看结构 | 中，需 segment 数据 |
| P2 | D（EV materiality 化） | `decision_model.py` | bear 幅度反映真实下行 | 中，需 materiality 映射 |

**验证基准**：用沃尔核材 + 迈瑞两例做快照回归，断言：

1. `thesis_confidence ≤ 0.95`（不再 1.0）
2. 沃尔核材 `evidence_log` 含"高速通信线+35.44%"（confirming）与"增收不增利"（refuting）
3. 沃尔核材 `position_band ∈ {观察, 减仓}`（不再是"清仓"）
4. 高熵（>1.0）+ argmax 非多数时，动作力度应自动保守（而非硬清仓）

---

## 7. 一句话总结

> AlphaBee 不缺洞察力——`insight.supporting_evidence / counter_evidence / materiality_rank / bull_bear_case` 里全是洞察。问题在于这些洞察在 `resolve_midterm_decision` 的边界被降维成"一句话 + 一个标量"，剩下的是一个靠硬编码状态表和结构式公式算 EV 的机械引擎。解法不是给确定性核心加 LLM，而是**把结构化证据作为一等输入，条件化状态、情景、EV、仓位四个计算**——让"机械"从"执行规则"变成"执行证据"。
