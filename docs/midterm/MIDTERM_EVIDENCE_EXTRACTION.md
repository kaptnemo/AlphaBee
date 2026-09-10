# MIDTERM Evidence 抽取设计（LLM 边界 ①）

> 非结构化文本（财报/业绩预告/研报/公告/新闻）→ `EvidenceEvent[]` → 喂 `bayes.update_confidence`，
> 点亮 `get_decision` 的 Confidence 轴（三轴正交决策中唯一空转的输入）。
>
> 关联文档：
> - `docs/midterm/MIDTERM_S1_S4.md` §5（S1=HypothesisDriven）、§11（S2 核心是 EvidenceChange）、§36（EVI）
> - `docs/midterm/MIDTERM_DECISION_MODEL.md` §6（三轴正交：State×Confidence×EV）
> - `docs/midterm/MIDTERM_STATE_DIFF_DESIGN.md` §12.3-①（本设计对应的 LLM 必需点）
> - `alphabee/midterm/models.py::EvidenceEvent`（输出契约，已存在）
> - `alphabee/midterm/bayes.py::update_confidence`（消费方：log-odds 更新机制）
>
> 本文档是 `evidence_extractor` 的实现依据；当前状态：`evidence` 参数无任何代码生成
> `EvidenceEvent`，实跑中 `evidence=[]` → `thesis_confidence=0` → Confidence 轴空转。

---

## 0. 定位与目标

**目标**：把"发生了什么"（非结构化文本）翻译成"这对我的假设意味着什么"（结构化证据），
让贝叶斯引擎有米下锅。

**边界**（延续 LLM 边界原则）：
- 抽取是**确定性核心的上游**，只产 typed 结构，不碰分数/状态/仓位计算；
- **异步 enrich + 失败降级**：LLM 挂掉 → `evidence=[]` → 模型照常出 `state_prior` 保守版。

---

## 1. 为什么必须 LLM（规则做不到的三件事）

| 规则做不到 | 例子 |
|-----------|------|
| **切分事件**：把叙述文本切成离散证据 | "Q2 收入恢复，但海外需求偏弱，毛利率连两季改善" → 3 条事件 |
| **语义方向判定** | "海外需求偏弱"=refuting，"需求恢复中"=confirming——关键词匹配必漏必错 |
| **相对 thesis 的因果判断**：同一事实对不同 H 方向不同 | "公司回购"对 H1（估值低估）是 confirming，对 H2（主业增速）是 neutral |

---

## 2. 输入：什么文本值得抽（EVI 分级，§36）

不是所有文本都抽。按 Expected Value of Information 分级：

| 级别 | 事件类型 | 来源（复用仓库已有能力） |
|------|---------|------------------------|
| **高 EVI（必抽）** | 财报、业绩预告/快报、盈利预测修正、评级调整、新产品/涨价、大客户 capex、监管变化 | `get_financial_fact`/`get_expectation_fact`（结构化）、`financial_report/links.py`（巨潮财报/东财研报链接）、`research_reports` agent（PDF+OCR） |
| 中 EVI（抽摘要） | 研报观点、机构调研、行业数据 | 东财研报列表（`EastmoneyHelper`）、`web_search` |
| 低 EVI（不抽） | 行业论坛、一般新闻 | — |

**原则**：高 EVI 里"结构化数据优先"——业绩预告/快报的数值（`forecast`/`express` 表）直接由
**规则**转 `EvidenceEvent`（见 §6），只有**定性文本**才走 LLM。LLM 只补规则拿不到的部分。

---

## 3. 输出契约：`EvidenceEvent` 字段填充规则（防幻觉）

`EvidenceEvent` 已存在于 `models.py`，本设计规定每个字段的填充纪律：

```python
class EvidenceEvent(BaseModel):
    id: str                   # 事件签名哈希：date+kind+主体+数值（去重键，§7）
    date: str                 # 事件发生日（非抽取日）
    kind: str                 # 受控词表：fundamental / expectation / trend / crowding / thesis / price
    description: str          # 一句话事实（客观陈述，不含方向判断）
    effect_on_thesis: str     # confirming / refuting / neutral（相对 H，Stage B 产出）
    confidence_delta: float   # 证据强度——只允许离散等级（§6），禁止 LLM 自由出连续值
    source_refs: list[str]    # 原文引用（quote + URL）——铁律：无引用不出数
```

**防幻觉铁律**：
1. `description` 必须能在 `source_refs` 的原文里找到对应句（quote 支撑）；
2. 涉及数值时数值必须可溯源，否则该数值置 `None`；
3. `confidence_delta` 只接受离散等级映射值（§6），LLM 输出的连续值一律拒绝。

---

## 4. 两阶段抽取设计（推荐）

单阶段"边抽事实边判方向"容易混入主观偏见。拆两阶段：

```text
Stage A（客观·事实抽取）
  文本 → FactEvent(kind, date, description, numbers, quotes, source_refs)
        ↑ 只问"发生了什么"，不问"好不好"

Stage B（主观·方向判定）
  FactEvent + Thesis H → EvidenceEvent(effect_on_thesis, confidence_delta)
        ↑ 相对 H 判断 confirming/refuting + 强度等级
```

**为什么两阶段**：
- `FactEvent` 可复用——thesis 变了只重跑 Stage B，不必重抽（`FactEvent` 入库缓存）；
- 数值交叉验证集中在 Stage A（客观层做，不受方向偏见污染）；
- Stage B 输入更小（事实列表而非全文），推理更便宜、更稳。

> Phase 1 简化：单 thesis 场景可合并为单阶段（prompt 强制"先引原文 → 再给方向 → 最后给等级"的顺序）；Phase 2 拆两阶段 + FactEvent 缓存。

### 4.1 Stage A：FactEvent typed contract（新增）

```python
class FactEvent(BaseModel):
    """客观事实事件（无方向判定，可跨 thesis 复用，入库缓存）。"""
    id: str                           # 事件签名哈希（date+kind+主体+数值）
    date: str                         # 事件发生日 YYYY-MM-DD
    kind: str                         # 受控词表（同 EvidenceEvent.kind）
    description: str                  # 客观事实陈述
    numbers: dict[str, float | None] = {}   # canonical 数值（如 {"revenue_yoy": 5.2}）
    quotes: list[str] = []            # 原文引用（description 的支撑句）
    source_refs: list[str] = []       # 来源 URL
    source_type: str = ""             # financial_report / forecast / research_report / announcement / news
```

### 4.2 Stage A prompt 要点

```text
系统提示（要点）：
- 你是客观事实抽取器，只报告"发生了什么"，禁止评价好坏、禁止预测。
- 每个事件必须：附原文引用句（quotes）、事件发生日、受控 kind。
- 数值只抄原文出现的数字；原文没有的数值不得补全（置 None）。
- 不相关文本输出空列表，不得编造。
输出：严格 JSON（FactEvent[]）。
```

### 4.3 Stage B：方向判定（新增 EvidenceJudgment）

```python
class EvidenceJudgment(BaseModel):
    """Stage B 输出：相对 thesis 的方向与强度（→ 组装 EvidenceEvent）。"""
    fact_id: str
    effect_on_thesis: str      # confirming / refuting / neutral
    strength: str              # weak / medium / strong（离散等级，§6）
    reasoning: str             # 相对 H 的推理（可审计，写入 note/description）
```

```text
系统提示（要点）：
- 给定 Thesis H（含 invalidation 条件），判定每个事实事件对 H 是证实/证伪/无关。
- 强度只允许 weak/medium/strong 三档，禁止输出数字。
- reasoning 必须引用事实的原文（防幻觉）。
- Thesis 为空时：数值类事件按符号定方向，定性事件一律 neutral（显式标记）。
```

---

## 5. 数值防幻觉三道闸（复用 `web_search_guard` 的教训）

1. **原文引用闸**：抽取的数值必须附 quote（`source_refs`）；quote 在原文中找不到 → 丢弃该数值；
2. **结构化交叉验证闸**：从财报文本抽的 `revenue` 必须与 `get_financial_fact` 的结构化值 ≈ 一致，
   不一致**以结构化数据为准**并记 warning；
3. **冲突降级闸**：数值冲突/缺失 → 事件保留方向但数值字段置 `None`，绝不静默回退。

---

## 6. `confidence_delta` 标定策略（最难字段）

`bayes.py` 的似然比 `log((1+d)/(1-d))` 在 d>0.7 后爆炸（d=0.9 → LR=19），
单条证据不该有此统治力。**按证据类型分两路，全部离散化**：

| 证据类型 | 标定方式 | 等级 → 数值 |
|---------|---------|------------|
| **数值类**（beat/miss、revision 幅度） | **规则算**：偏离幅度 → 等级；方向由数值符号定 | weak=0.1 / medium=0.3 / strong=0.5 |
| **定性类**（新产品、监管、管理层） | **LLM 给离散等级**（weak/medium/strong） | 上限 0.7，禁止连续值 |

数值类示例规则（示意，需回测）：

```text
盈利超出预告上限幅度：<5% → weak；5–15% → medium；>15% → strong
revision_1m 幅度：|Δ| < 3% → weak；3–10% → medium；>10% → strong（方向=符号）
```

---

## 7. 去重与独立性（贝叶斯累加的两个陷阱）

- **多来源同事件**：财报原文、东财快讯、研报点评都在说同一份业绩 → 按 `id`（date+kind+主体签名）
  去重，`source_refs` 合并；
- **同主题多篇报道**：10 篇文章炒同一利好 → 只算一次，否则 log-odds 累加虚高
  （10 条 d=0.3 的 confirming ≈ 后验 0.97，荒谬）。

---

## 8. 接入 `get_decision`（不破坏确定性核心）

```text
第一遍（确定性核心，同步）:
  get_decision(symbol, evidence=None)     → state_prior 版本（保守）

第二遍（LLM enrich，可异步）:
  evidence = evidence_extractor(symbol, thesis, window_texts)
  get_decision(symbol, evidence=evidence) → bayes_posterior 版本
```

- **LLM 失败 → `evidence=[]`**：模型照常出保守版（`probability_source="state_prior"`），只降级不中断；
- **thesis 为空**（实跑现状）：Stage B 退化为"数值类按符号定方向、定性全 neutral"，显式标记；
- **两遍输出共存**：`CompanyStateArtifact.evidence_log` 留痕，diff（`MIDTERM_STATE_DIFF_DESIGN.md` §5）
  的 `ConfidenceDelta.evidence_ids` 依赖它做归因。

---

## 9. 与仓库已有 LLM 能力的关系（复用 vs 新建）

| 已有能力 | 与 evidence 抽取的关系 |
|---------|----------------------|
| `verify_hypotheses` agent | **最高复用价值**：已在做"假设 × 证据"验证，加 adapter 把其结构化输出映射成 `EvidenceEvent`，不必从零建 |
| `research_reports` agent | Stage A 的输入源（研报 PDF 下载 + OCR 提取正文） |
| `financial_report/links.py` | 财报（巨潮）/研报（东财）链接获取，Stage A 取数入口 |
| `explore_conflicts` | 背离信号（E↑ 但 T↓）→ 触发定向抽取"市场知道什么我不知道"的证据（§20） |
| `web_search_guard` 教训 | 数字必须验证——§5 三道闸的设计依据 |

**建议路径**：Phase 1 先做 `verify_hypotheses → EvidenceEvent` 的 adapter（零新 LLM 链路，
最快点亮 Confidence 轴），Phase 2 再建独立 `evidence_extractor`（两阶段 + 增量）。

---

## 10. 成本控制（三个杠杆）

1. **触发式抽取**：只在高 EVI 事件发生时抽（§2 分级），不是全市场全量；
2. **增量抽取**：只抽窗口内（上次抽取之后）的新文本；
3. **FactEvent 缓存**：Stage A 事实入库，thesis 变化只重跑 Stage B。

---

## 11. LLM 边界与降级汇总

| 环节 | LLM 需求 | 失败降级 |
|------|---------|---------|
| 数值类 EvidenceEvent（预告/快报/revision） | 🚫 纯规则（§6） | — |
| Stage A 事实抽取 | 🔴 必需 | 该文本跳过，不产事件 |
| Stage B 方向判定 | 🔴 必需 | 数值类按符号、定性 neutral |
| confidence_delta 标定 | 🔶 离散等级（禁连续值） | 默认 weak=0.1 |
| 结构化交叉验证 | 🚫 纯规则（§5） | 以结构化数据为准 |

---

## 12. 落地分期

| 期 | 内容 | 依赖 |
|----|------|------|
| E1 | 数值类 EvidenceEvent 规则生成器（forecast/express/revision → evidence）+ 单测 | 无 |
| E2 | `verify_hypotheses → EvidenceEvent` adapter（复用已有 LLM 验证）+ 单测 | E1 |
| E3 | 独立 `evidence_extractor`：Stage A/B 两阶段 prompt + `FactEvent`/`EvidenceJudgment` 契约 + 去重 + 缓存 | E2 |
| E4 | 接入 `decision_model`（异步 enrich + 失败降级）+ 与 diff 归因打通（`ConfidenceDelta.evidence_ids`） | E3 |

---

## 附：关键设计决策速查

1. **两阶段**：Stage A 客观事实（可复用/缓存）→ Stage B 相对 thesis 的方向判定（§4）。
2. **离散等级禁连续值**：`confidence_delta` 只允许 weak/medium/strong（0.1/0.3/0.5，上限 0.7），数值类由规则标定（§6）。
3. **三道闸防数值幻觉**：原文引用 → 结构化交叉验证 → 冲突降级（§5）。
4. **事件签名去重**：`id = hash(date+kind+主体)`，多来源合并、同主题只算一次（§7）。
5. **异步 enrich 不碰核心**：LLM 失败 → `evidence=[]` → state_prior 保守版，只降级不中断（§8）。
6. **结构化优先**：数值类证据规则生成，LLM 只补定性文本（§2/§6）。
