# 状态-H 背离检测器设计（中期决策层留痕）

> **状态：设计文档（2026-10 完成，尚未实施）**。立项来源：601899.SH 真实案例——中期决策总结中 `argmax=S1`（预期差形成，多头早期信号：试探 5-10%）与 `P(H|E)=0.95` 的 H 文本（"11.3x PE 不是错杀，而是市场对周期顶部盈利的合理折价；真正的多头逻辑必须等 2027 年"——看空当期）形成**叙事背离**，且仓位数学把高置信当"力度缩放"（`position.py` `stock_weight = base × 期望仓位带 × confidence`），使"更确信地试探买入"与"H 说当前不该买"方向相反。本设计**不改变状态/置信度/仓位的任何输出**，只把这类背离**显式留痕**（Issue + 偏离账本 + 报告披露），与 `policy_exposure` 同款"只留痕不进主权重"。
> 关联文档：`docs/design/A_SHARE_FRAMEWORK_REVIEW.md`（§2.2 F 质量缺陷、§7 Q3）、`docs/implementation/framework/P0_A_SHARE_FIX_IMPLEMENTATION_DESIGN.md`（P0-6 consensus_missing 同款发射模式）、`alphabee/agents/thesis/engine.py` `conflict_direction`（文本判向先例：标记表 + 否定窗口 + 分句）、`docs/midterm/MIDTERM_DECISION_MODEL.md`（三轴正交 §6）。
> 实施时：新 category 登记走 `alphabee-pipeline-contract-steward`（`CLASS_BY_CATEGORY` 覆盖守卫）；行为零变更（纯新增观察器），但按 ROADMAP 变更登记表登记新 category。

---

## 1. 问题定义与设计定位

### 1.1 背离的精确形态（601899 案例）

| 面 | 内容 | 方向 |
|---|---|---|
| 认知状态 | `argmax=S1`（预期差形成 = 市场低预期 + 未定价正向变化，动作=试探 5-10%） | **多头**（当期） |
| 核心假设 H | "折价是周期顶部盈利的合理定价，真正多头逻辑必须等 2027" | **看空**（当期） |
| 后验 | `P(H|E)=0.95`（clamp 上限） | 对看空 H 的高度确认 |

机制成因（上轮诊断结论，三条）：① 分类器只看 F 的**量**不看**质**（+51.9% headline → `f_up` 满分，价格驱动成分无通道打折）；② `confidence` 在仓位乘法里是**力度缩放**而非方向参与（假设"H 确认 = 顺状态方向下注"，本例 H 逆状态方向）；③ 状态机与贝叶斯正交，无互相约束的边。

### 1.2 本设计的定位（明确边界）

- **观察器，不是修正器**：不改状态、不改置信度、不改仓位——三者输出的逐字节不变由回归钉保证（§6）；
- **留痕三通道**：主链 Issue（进报告"系统问题"段）+ 偏离账本（`CLASS_BY_CATEGORY` 登记后自动入 `deviation_events`）+ 审计可查；
- **为什么不动仓位数学**：confidence 与 H 方向的语义对齐是另一类改动（涉及 bayes/position 契约面与行为变更），本设计先把背离**暴露出来**，修复方案待观察数据积累后另立项（§9 顺延 RC-FIX-CONFIDENCE-DIRECTION）。

---

## 2. 检测器契约（纯规则、禁 LLM、fail-open）

### 2.1 新模块 `alphabee/midterm/state_thesis_guard.py`

```python
"""状态-H 语义背离检测（设计 STATE_THESIS_MISMATCH_DETECTOR_DESIGN §2）。

纯观察器：只读 ``StateBelief``（argmax/entropy）+ H 文本 + 后验，产出
``MismatchVerdict``；不改任何决策输出。禁 LLM、无网络、无 IO。

- 状态方向用**动作语义**映射（S1/S2/S3=多头、S4/S5=空头、S0=中性），与
  ``bayes._STATE_BULL_DIRECTION``（S1=0.0 中性锚）**刻意不同**：背离是
  "状态说试探 vs H 说别买"的用户可见冲突，用动作语义；差异在 docstring 披露。
- H 方向用确定性文本分类（分句 + 否定窗口 + 转折后置权重，先例 =
  ``agents/thesis/engine.py::conflict_direction``），**中性/判不清不触发**（保守）。

from alphabee.midterm.models import StateBelief

STATE_SIDE: dict[str, str] = {"S0": "neutral", "S1": "long", "S2": "long",
                              "S3": "long", "S4": "short", "S5": "short"}
_CONF_THRESHOLD = 0.7    # 后验低于此值：背离不成立（结构性示意，应回测）
_ENTROPY_THRESHOLD = 1.0 # 熵高于此值：状态归类不确定，不触发（与 classifier._ENTROPY_UNCERTAIN 同值，parity 钉）
_HIGH_CONF = 0.9         # 后验 ≥ 此值 → HIGH，否则 MEDIUM

def thesis_side(thesis: str) -> str: ...      # "bullish" / "bearish" / "neutral"
def check_state_thesis_mismatch(
    state: StateBelief | None,
    thesis: str,
    confidence: float | None,
) -> MismatchVerdict: ...
```

### 2.2 `MismatchVerdict` 与触发条件

```python
@dataclass(frozen=True)
class MismatchVerdict:
    mismatch: bool = False
    state_side: str = ""    # long / short / neutral
    thesis_side: str = ""   # bullish / bearish / neutral
    reason: str = ""        # 一句话成因（入 Issue message）
```

触发条件（**全满足**才 `mismatch=True`，任一不满足 → False，保守）：

1. `state` 非空且 `argmax_state in {S1..S5}`（S0 无动作方向，不触发）；
2. `thesis` 非空且 `thesis_side(thesis) in {bullish, bearish}`（neutral/判不清不触发）；
3. `state_side` 与 `thesis_side` **相反**（long↔bearish、short↔bullish）；
4. `confidence` 非 None 且 `confidence >= _CONF_THRESHOLD`（置信不足 = 背离不成立）；
5. `state.entropy <= _ENTROPY_THRESHOLD`（熵高 = 状态归类不确定，背离不 crisp）。

**严重度**：`confidence >= _HIGH_CONF` → `IssueSeverity.HIGH`；否则 `MEDIUM`。

---

## 3. H 语义方向判定（确定性文本分类）

### 3.1 分句与否定窗口（先例：`agents/thesis/engine.py::conflict_direction`）

1. **分句**：按 `，。；、！？` 与转折词（`但/而/然而/不过/而是/而是因为`）切分，逐句独立判向；
2. **否定窗口**：句内否定词（`不/非/并非/未/尚未/缺乏/难以/无`）与看多/看空标记共现 ⇒ 该句不按标记原方向计（"**不是**错杀" = 否定+看多标记 ⇒ 不判 bull；"**并非**低估" 同理）；
3. **转折后置权重**：「不是 X，**而是** Y」框架中，转折引导句权重 ×2（Y 为主导方向）——601899 H 的"不是错杀，**而是**……合理折价"由后置句主导判 bearish；
4. **聚合**：逐句打分（bull 句 +1 / bear 句 −1 / neutral 0，后置句 ±2），总分 >0 ⇒ bullish、<0 ⇒ bearish、=0 ⇒ neutral。

### 3.2 标记表（模块常量，结构性示意）

```python
_BEARISH_MARKERS: tuple[str, ...] = (
    "周期顶部", "见顶", "合理折价", "已定价", "充分定价", "price in", "透支",
    "打折", "需要等", "必须等", "被高估", "不可持续", "均值回归", "承压", "谨慎",
    "证伪", "拐点向下",
)
_BULLISH_MARKERS: tuple[str, ...] = (
    "错杀", "被错杀", "低估", "安全边际", "未定价", "重估", "超预期", "量价齐升",
    "接棒", "增长确定性", "预期差", "戴维斯双击",
)
```

### 3.3 残余风险披露（模块 docstring 逐字登记）

文本启发式固有残余（与 `conflict_direction` 同款披露）：管理口径措辞可能使方向误判；缓解 = **中性不触发**（fail-open 保守）+ 否定窗口 + 后置权重；LLM 增强属顺延（§9 RC-GUARD-LLM）。601899 H 原文作为回归钉（§6）。

---

## 4. 发射点与接线

### 4.1 主链发射（`orchestrator/nodes/midterm.py`）

在 `consensus_missing` 发射块（`nodes/midterm.py:349-361` 同款模式）之后追加：

```python
# 状态-H 背离检测（STATE_THESIS_MISMATCH_DETECTOR_DESIGN §4）：纯观察器只留痕
from alphabee.midterm.state_thesis_guard import check_state_thesis_mismatch

verdict = check_state_thesis_mismatch(decision.state, hypothesis, decision.thesis_confidence)
if verdict.mismatch:
    severity = IssueSeverity.HIGH if decision.thesis_confidence >= _HIGH_CONF else IssueSeverity.MEDIUM
    new_issues.append(
        record_deviation(
            DeviationClass.D3_ARGUMENT,
            severity,
            f"认知状态（argmax={decision.state.argmax_state}，动作侧={verdict.state_side}）与核心假设 H 的"
            f"语义方向（{verdict.thesis_side}）背离，且后验 {decision.thesis_confidence:.2f} 高于阈值：{verdict.reason}",
            detected_at_step=step.id,
            category="state_thesis_mismatch",
            scope=IssueScope.PLANNING,
        )
    )
```

- `severity` 判定收敛进 `MismatchVerdict` 更好：加 `severity: str = "medium"` 字段（检测器内判定，节点零逻辑）——实施时按此落地；
- **决策输出零改动**：不碰 `decision`、不碰 `new_artifacts`。

### 4.2 偏离分类登记（`orchestrator/services/deviation.py::CLASS_BY_CATEGORY`）

```python
"state_thesis_mismatch": DeviationClass.D3_ARGUMENT,
```

理由：状态叙事（因子模式）与论点叙事（H）的方向冲突属**论证层**内部矛盾（D3），不是数据缺（D1）也不是状态漂移（D4）——与既有 `tracking_contradiction_forced`（D3）同族。**覆盖守卫测试期望表同步 +1**。

### 4.3 跟踪路径（顺延 RC-GUARD-TRACKING）

v1 **不接** `tracking/scheduler`：跟踪帧的 H 多为继承文本，同一条背离会随帧重复产 Issue，造成告警刷屏（与 P0 评审"STALE_EXPIRED 每周必发淹没真事件"同款噪声风险）。待主链数据积累观察命中率后再评估（顺延登记 §9）。

### 4.4 报告披露路径

Issue 走既有"系统问题"段（`render_final_report` 的 issues 段已就位）——**零新增渲染代码**。已知缺陷登记：用户输出与调试输出分层未做（评审 §6 0.6），本 Issue 会与 parse_error 同段呈现，不在此设计范围内解决。

---

## 5. 兼容与影响

| 面 | 影响 | 处置 |
|---|---|---|
| 决策输出 | **零变化**（观察器只读不写；回归钉保证状态/置信度/仓位逐字节不变） | §6 回归 |
| Issue 流 | 仅命中时新增一条 `state_thesis_mismatch` | 纯新增，不触发时行为逐字不变 |
| 偏离账本 | 新 category 自动入 `deviation_events`（指纹去重/upsert 既有机制） | `CLASS_BY_CATEGORY` 登记 |
| 既有测试 | `test_deviation_service.py` 覆盖守卫期望表 +1 | 同步 |
| classifier/bayes/position | **零改动** | 不动清单 |

---

## 6. 测试矩阵

| 文件 | 用例 | 断言 |
|---|---|---|
| `tests/midterm/test_state_thesis_guard.py`（新） | 601899 回归钉 | H 原文（"不是错杀，而是……合理折价；……必须等 2027 年……打折看待"）⇒ `thesis_side="bearish"`；S1 + conf 0.95 + 低熵 ⇒ `mismatch=True`、`severity="high"` |
| 同上 | 典型 bull H | "低估值错杀、预期差未定价" ⇒ bullish；S1 + bull H ⇒ 不触发（同向） |
| 同上 | 否定窗口 | "并非低估" ⇒ 不判 bull；"不是错杀" ⇒ 不判 bull |
| 同上 | 转折后置权重 | "不是错杀，而是合理折价" ⇒ bearish（后置句主导） |
| 同上 | 中性/空 | 混合 H 打平 ⇒ neutral ⇒ 不触发；H 为空 ⇒ 不触发 |
| 同上 | 阈值保守 | conf 0.69 ⇒ 不触发；entropy 1.2 ⇒ 不触发；S0 ⇒ 不触发；S4+bearish H ⇒ 不触发（同向） |
| 同上 | 常量 parity | `_ENTROPY_THRESHOLD == classifier._ENTROPY_UNCERTAIN`（自备同值钉） |
| `tests/orchestrator/test_midterm_payload.py` 或节点测试（增补） | 发射接线 | monkeypatch 检测器 ⇒ Issue 的 category/scope（PLANNING）/severity 正确、`related_step` 正确；决策 artifact 逐字段不变（观察器零写入钉） |
| `tests/orchestrator/test_deviation_service.py`（增补） | 分类登记 | `CLASS_BY_CATEGORY["state_thesis_mismatch"] == D3_ARGUMENT`（覆盖守卫 +1） |

**变异杀伤**：删否定窗口 ⇒ 601899 钉必红；删后置权重 ⇒ 分句钉必红；把 S1 映射改 neutral ⇒ 触发钉必红；severity 阈值改反 ⇒ high/low 钉必红。
**全量回归**：`pytest tests -q -m "not integration"` 离线口径；`tests/midterm`/`tests/orchestrator` 全绿。

---

## 7. PR 切分与回滚

- **单 PR**：guard 模块 + `MismatchVerdict` + 主链发射 + category 登记 + 测试矩阵全量；
- **回滚**：删除 `nodes/midterm.py` 的调用块（guard 模块可留，无副作用）；`CLASS_BY_CATEGORY` 登记行删除并回退覆盖守卫期望表。

---

## 8. 验收标准

1. **601899 案例重放**：同输入（S1 + 0.95 + H 原文）⇒ `mismatch=True`、severity=high、reason 含两侧方向；
2. **保守钉**：同向 / 置信不足 / 熵高 / S0 / neutral H 全部不触发；
3. **纯观察器**：接线前后 `decision` artifact（state/thesis_confidence/position）逐字段相等（零写入钉）；
4. **登记**：`CLASS_BY_CATEGORY` 覆盖守卫 +1 全绿；ROADMAP 变更登记行（新 category `state_thesis_mismatch` → D3_ARGUMENT、`IssueScope.PLANNING` 使用）；
5. **全量门禁**：离线口径通过，零既有用例回归。

---

## 9. 非目标与顺延登记

| 登记号 | 内容 | 理由 |
|---|---|---|
| RC-FIX-CONFIDENCE-DIRECTION | confidence 缩放与 H 方向的对齐修复（仓位数学层） | 涉及 bayes/position 契约面与行为变更，需观察数据积累后另立项（§1.2） |
| RC-GUARD-TRACKING | 跟踪路径接背离检测 | 继承 H 随帧重复报会产生告警噪声，待命中率观察（§4.3） |
| RC-GUARD-LLM | H 方向判定的 LLM 增强 | 检测器在决策层，LLM 引入延迟/成本/不确定性；关键词分类命中率不足时再评估 |
| — | 状态/置信度/仓位任何修正动作 | 本设计是观察器，修正类改动一律另立项 |

---

## 10. 文档登记要求

1. `ROADMAP.md` 变更登记表一行（新 category + scope=PLANNING 使用；行为面 = 仅命中时新增 Issue）；
2. 模块 docstring 披露：动作语义映射与 `bayes._STATE_BULL_DIRECTION` 的差异、文本启发式残余（§3.3）、阈值出处（结构性示意）；
3. `docs/design/A_SHARE_FRAMEWORK_REVIEW.md` §7 待拍板问题处可追加本设计引用（观察器先行、修正类顺延 RC-FIX-CONFIDENCE-DIRECTION）。
