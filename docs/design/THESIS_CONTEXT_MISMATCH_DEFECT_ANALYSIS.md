# Thesis「语境不适配」批量出现缺陷分析

> 关联条目：[`SEMANTIC_JUDGMENT_CONTRACT_DESIGN.md`](SEMANTIC_JUDGMENT_CONTRACT_DESIGN.md)（判定语义契约）、[`THESIS_CONFIDENCE_DAMPING_DESIGN.md`](THESIS_CONFIDENCE_DAMPING_DESIGN.md)（置信度乘性阻尼，已实施）、[`REPORT_QUALITY_FIX_ROADMAP.md`](../roadmap/REPORT_QUALITY_FIX_ROADMAP.md)（"大存大贷" rejected 传导断裂，同源缺陷）、[`RULE_MARKET_ADAPTATION_DESIGN.md`](RULE_MARKET_ADAPTATION_DESIGN.md)（Phase 1 语境注入只加注释不改分数）、[`PEER_GROUP_JUDGMENT_DESIGN.md`](PEER_GROUP_JUDGMENT_DESIGN.md)（同行基准）、[`DEVIATION_CONTROL_FRAMEWORK.md`](DEVIATION_CONTROL_FRAMEWORK.md) §8（加权边审计）。
> 状态：✅ **已实施（F1–F5）**。修复方案与实施记录见 [`THESIS_CONTEXT_MISMATCH_FIX_PLAN.md`](../implementation/thesis/THESIS_CONTEXT_MISMATCH_FIX_PLAN.md)；变更登记见 [`../roadmap/ROADMAP.md`](../roadmap/ROADMAP.md) 行为变更登记表（2026-10-17）。
> 证据：真实 run **603986.SH 兆易创新**，`data/task_records/603986.SH/2026-10-17/task-6e9476f66ac8.json`（2026-10-17，`llm_review=True`）。文中所有"实测"结论均附复现命令（附录 A）。

---

## 1. 背景与问题

### 1.1 缺陷现象（603986.SH 一轮，可直接核对）

本轮 `review_overall_status=blocked`。thesis_review 产出 **3 条 blocking + 3 条 warning 的"语境不适配"**，覆盖 8 个维度中的 6 个：

| 维度 | judgment（score / conf） | L1 状态 | 审查发现（节选） |
|---|---|---|---|
| 竞争壁垒 | negative（**-1.0** / 0.11） | contested | **blocking**。判断为 negative，但 counter_evidence 显示"竞争优势弱化假设被证伪"，且存储分部增速远超可比大市值同行、主业份额未弱化；negative 主要来自框架层冲突而非直接竞争数据 |
| 盈利质量 | strong_negative（**-1.0** / 0.43） | contested | **blocking**。过度强调非经常性损益和勾稽异常，但 counter_evidence 显示主业扣非净利率达 42.1%、扣非增速 796.9%，应收/现金流健康，三表勾稽断裂可由非现金公允价值变动解释 |
| 成长质量 | strong_negative（**-0.85** / 0.11） | contested | **blocking**。context_notes 明确指出成长期公司波动应折减，但 strong_negative 未充分反映 counter_evidence 中存储业务量价齐升、份额扩张的正面事实，过度负面 |
| 资本配置效率 | neutral（0.05 / 0.14） | qualified | warning。行业信息不足，缺少同行业可比基准 |
| 经营稳定性 | negative（-0.312 / 0.11） | qualified | warning。negative 主要基于财务勾稽异常，但 counter_evidence 已归因于非现金因素，可能过重 |
| 估值合理性 | neutral（0.0 / 0.04） | qualified | warning。估值判断 neutral 与证据 negative 不符，估值基准矛盾、行业比较数据缺失 |

同一 run 的**旁证**（附录 A 提取，直接印证第 2 节根因链）：

- `assumption_invalidated`：**12 条已证伪假设仍处于登记簿**（"供给缺口逆转…""非经常性收益虽大但主业扣非盈利同样强劲且可持续…""产能瓶颈…"）。
- `amplification_direction_conflict`（§8 审计）：加权边 `insight->thesis` 方向不一致——**insight 方向=正面（净分 2） vs 证据方向=负面（合并均分 -0.149）**。
- `verified_conflict` 6 条框架层冲突全部 severity≥high（其中"市场份额增长 vs 对标组增速落后"直接指向竞争壁垒）。

### 1.2 缺陷的生产点与分级机制（先厘清"语境不适配"从哪来）

1. **生产点**：`ThesisReviewer` Layer 2 LLM 审查。`_merge_llm` 中 `context_appropriate=false` → 无条件追加 `"语境不适配：" + context_rationale`（[`reviewer.py:368-369`](../../alphabee/agents/thesis/reviewer.py#L368-L369)）。只有 `llm_review=True` 才运行这一层。
2. **分级与"语境"无关**：`_build_review` 按 Layer 1 确定性状态分流——`contested/insufficient` → blocking，其余 → warning（[`reviewer.py:394-399`](../../alphabee/agents/thesis/reviewer.py#L394-L399)）。因此同一条 LLM 文案落在 blocking 还是 warning，完全由 L1 的状态决定。
3. **"语境不适配"是 LLM 层唯一对外可见的通道**：`signals_consistent=false` 只写 `conflict_description`、不进 issues（[`reviewer.py:359-366`](../../alphabee/agents/thesis/reviewer.py#L359-L366)）；`missing_checks` 只进 `missing_evidence`。LLM 发现"判断与证据不符"时，只有塞进 `context_rationale` 才能被下游看见——所有 LLM 层的不满都汇聚到这一个桶里（[估值合理性] 那条"证据 negative 与判断 neutral 不符"的文案文不对题，正是该通道挤压的直接表现）。

---

## 2. 根因链（七层，按因果顺序）

### R1（核心架构缺陷）：判定与语境是两条永不交汇的通道

维度判断只由**分数通道**决定：`score = mean(level_score × impact_direction)`（信号 + 异常投影），再叠加已验证冲突的罚分（[`engine.py:789`](../../alphabee/agents/thesis/engine.py#L789)）。而 `counter_evidence` / `context_notes` / `missing_evidence` 是**纯文本附加字段，从不回流到分数**：

- `_apply_insight` 注入洞察反证时从不碰 score（[`engine.py:968-1024`](../../alphabee/agents/thesis/engine.py#L968-L1024)）；
- `_refresh_dimensions` 最终从 score×confidence 重算 judgment，context_notes 只拼进 interpretation 文案（[`engine.py:1026-1043`](../../alphabee/agents/thesis/engine.py#L1026-L1043)）。

后果：审查 LLM 看到的每个维度都是"标签说 negative + 反证说被证伪 + 备注说已折减"的**内部矛盾组合**（`to_dict` 把三个字段并列输出，[`models.py:199-201`](../../alphabee/agents/thesis/models.py#L199-L201)）。逐条报语境不适配几乎必然，而不是偶发。

### R2：rejected 假设"精确扣除"依赖关键词精确匹配，框架层冲突扣不动分

被证伪（rejected）假设要回改分数，唯一路径是 `_remove_disputed_contributions`，其触发依赖 `_infer_disputed_evidence` 在假设文本里**精确子串匹配** pattern_id / signal_id（[`engine.py:831-859`](../../alphabee/agents/thesis/engine.py#L831-L859)）。匹配不到 → 分数原地不动，只有 counter_evidence 文本。

**实测（附录 A.2）**：本轮 3 条真实已证伪假设的解释文本（"供给缺口逆转…""非经常性收益虽大但主业扣非盈利同样强劲…""产能瓶颈…"）全部返回空集——框架层冲突（LLM 主题式假设）本来就不含 `anomaly_pattern:*` / signal_id 子串。于是 12 条已证伪假设全部只写反证、不改分。这正是"大存大贷"缺陷（[`REPORT_QUALITY_FIX_ROADMAP.md:255`](../roadmap/REPORT_QUALITY_FIX_ROADMAP.md#L255)）在框架层冲突上的同构复发。

### R3：冲突方向分类器对"证伪式措辞"失灵，反而给 moat 扣了分

`conflict_direction` 只按标记词典判向，`unknown` 保守回落**既有负贡献语义**（[`engine.py:250-287`](../../alphabee/agents/thesis/engine.py#L250-L287)）。**实测（附录 A.2）**：

| 真实假设 explanation | 分类结果 | 实际影响 |
|---|---|---|
| "产品结构差异：…公司主业内生份额仍在提升，**非竞争优势弱化**。" | `unknown`（良性、恶性词表均无命中） | 保守按负贡献处理 → 竞争壁垒被罚分（high+partial → 0.55×0.7≈0.385） |
| "非经常性收益为证券投资公允价值浮盈…**被高利润增速掩盖**。" | `benign`（"非经常"命中良性词） | 方向判反：确认"掩盖"的假设反而不扣分 |
| "正常经营变化：…业务结构剧变**而非操纵**，数据自洽性可解释。" | `benign` | 正确 |

竞争壁垒 score=-1.0 的直接来源就是第一条：**把"竞争优势没有弱化"的证伪式措辞判成 unknown → 罚分**，同时"竞争优势弱化假设被证伪"的反证只以文本存在（R2）。措辞本身决定分数方向，与事实方向无关。

### R4：语境折减系数不足以改变档位，note 与标签自相矛盾

`_apply_company_context` 对成长期公司的 growth_quality 等负分维度乘 0.85，并追加"已按生命周期做折减"的 note（[`engine.py:914-921`](../../alphabee/agents/thesis/engine.py#L914-L921)）。但档位阈值是 `score ≤ -0.5 → strong_negative`（[`models.py:39-45`](../../alphabee/agents/thesis/models.py#L39-L45)），且 `score_to_judgment` 只在 score **恰为 ±1.0 且 confidence<0.3** 时才降档（[`models.py:257-270`](../../alphabee/agents/thesis/models.py#L257-L270)）。

**实测（附录 A.3）**：本 run `company_lifecycle=growth`，growth_quality score=-0.85 与 `-1.0×0.85` 完全吻合，且 `score_to_judgment(-0.85, 0.11)` → **strong_negative**。折减做了、留痕了，但对外可读的结论（judgment）纹丝不动——审查 LLM 看到的正是 [成长质量] 那条："note 说应折减，标签仍 strong_negative"。同理，竞争壁垒 score=-1.0 只因 confidence 0.11 触发极端降档才显示 negative，**score 与 judgment 的可见矛盾**（-1.0 却非 strong）也是审查 LLM 的怀疑面。

### R5：审查层单向挑剔、无修复闭环

- REVIEWER_SYSTEM_PROMPT 第 3 问只有极薄引导（"给定行业/生命周期信息，当前判断是否需要校准？如缺少行业信息，标注'行业信息不足'"）（[`prompts.py:186-187`](../../alphabee/agents/thesis/prompts.py#L186-L187)）。[资本配置效率] 的"行业信息不足，缺少同行业可比基准"就是这条 prompt 诱导的标准输出。
- 硬约束"不能修改 thesis 的判断值"（[`prompts.py:209-213`](../../alphabee/agents/thesis/prompts.py#L209-L213)），且图结构是 `review_thesis → generate_report`（[`agent.py:498-505`](../../alphabee/orchestrator/agent.py#L498-L505)）——thesis 从不重写，报告 gate 只重写报告。每条语境不适配都原样进入 `blocking_issues` 与 orchestrator `Issue`（[`agent.py:299-313`](../../alphabee/orchestrator/agent.py#L299-L313)），无去重、无收敛。
- 审查输入是完整 thesis JSON（含 counter_evidence/context_notes）+ 逐条信号详情 + CompanyContext（[`reviewer.py:315-341`](../../alphabee/agents/thesis/reviewer.py#L315-L341)）。LLM 天然会把"标签 vs 反证"的对立报出来——它在做它被要求做的事，但结构上没有给它任何消化矛盾的出口。

### R6：审查端拿到的语境输入本身单薄、缺可比基准

`build_company_context` 的行业语境只有：申万行业名 + "行业PE/PB"一行摘要（[`company_context.py:75-87`](../../alphabee/orchestrator/services/company_context.py#L75-L87)），商业模式用毛利/研发费用启发式分类（[`company_context.py:111-120`](../../alphabee/orchestrator/services/company_context.py#L111-L120)）。而 COMPANY_TRACK 产出的 `peer_group` / `peer_benchmarks`（真正的同行可比数据，本 run 已有 5 只对标股）只挂在 `ThesisArtifact.industry_context`（[`thesis.py:151-163`](../../alphabee/orchestrator/nodes/thesis.py#L151-L163)），**review_thesis 节点重建 CompanyContext 时未传给 reviewer**（[`agent.py:174-179`](../../alphabee/orchestrator/agent.py#L174-L179)）。所以 [资本配置效率] 缺同行基准、[估值合理性] 行业比较数据缺失是**结构性必然**。

### R7：两层引擎的"语境视野"天然不一致（系统性分歧源）

Signal/Anomaly 规则引擎只能看到**报表数字与勾稽关系**（非经常损益、应收增速、三表配平），Insight LLM 能看到分部结构、扣非口径、公允价值变动、同行对比。后者生成的 counter_evidence 与前者算出的 judgment 系统性反向。本轮 §8 放大审计的实证：`insight 方向=正面（净分 2） vs 证据方向=负面（均分 -0.149）`——两层画像的分歧不是本 run 特有，而是"规则引擎打分 + LLM 写反证 + LLM 再审查"三段式设计的固有张力：**反证写出来就是为了表达与分数不同的观点，但结构上没有给它任何权重表达渠道（R1）**。

---

## 3. 为什么是"大量"——乘法效应

```
一条 insight 反证 → 无差别广播到全部维度（×8）        ── engine.py:996-1003
每个维度被 Layer 2 LLM 独立审查，prompt 还诱导报"行业信息不足"
LLM 的不满只能走"语境不适配"一条通道                    ── §1.2 第 3 点
审查无修复闭环 → 每条都原样进入 blocking/warning，无去重  ── R5
```

- **单点事实 N 倍放大**：`_apply_insight` 明文注释 "Rather than guessing which dimension each counter-evidence item belongs to, we attach all of them to every dimension"（[`engine.py:991-1003`](../../alphabee/agents/thesis/engine.py#L991-L1003)）。[竞争壁垒] 的"存储分部增速远超可比同行"与 [成长质量] 的"存储业务量价齐升"极可能是**同一条 insight 反证的两次命中**。
- **一条事实、两条通道、互相打脸**：被证伪假设同时产生 counter_evidence 文本（不进分）和 confidence 阻尼（只降置信度不改方向），而罚分来自另一个"方向判反"的假设（R3）。同一主题在 score、confidence、counter_evidence 三处留下互相矛盾的三笔账。
- **分级放大观感**：blocking/warning 由 L1 状态决定（§1.2 第 2 点），同一类 LLM 文案被打散到两个列表，视觉上"到处都是"。

## 4. 影响面与边界条件

- **受影响对象**：`review_thesis` 的 blocking/warning 列表（→ 报告醒目区、orchestrator HIGH/MEDIUM Issue、`overall_status=blocked` 触发报告 gate 提示）。分析结论方向本身（thesis.judgment）**不被修改**，因此缺陷表现为"审查结论比 thesis 更负面、且负面理由与反证矛盾"。
- **不触发的边界**：`llm_review=False` 时无"语境不适配"文案（但 R1-R4 的判定-语境脱节仍然存在，改由 §8 放大审计的 D3 偏离兜底捕捉——本轮 `amplification_direction_conflict` 即为该兜底的产物）；无 insight 时无广播（R3 的广播乘数消失）；无框架层冲突时 R2/R3 不触发。
- **与既有修复的关系**：`THESIS_CONFIDENCE_DAMPING_DESIGN` 修的"置信度归零"是 confidence 通道的同类病（绝对扣减 → 乘性阻尼），**score 通道的"反证不回分"从未修**；`task_records/distiller.py::suggest_calibration` 已把"语境不适配"当作高频问题持续蒸馏（[`analyzer.py:210-216`](../../alphabee/task_records/analyzer.py#L210-L216)），说明该缺陷是跨 run 的系统性问题而非本 run 特有。

---

## 5. 修复方向（按杠杆排序，待评审）

1. **给反证一条回分的通道**（治本，对应 R1/R2）：rejected 假设无法精确匹配 pattern/signal 时，对其 `related_dimensions` 做一次确定性的档位回落或分数阻尼（与 confidence 的乘性阻尼同构），消除"证伪成立、负分照旧"。
2. **insight 反证按维度相关性分配**（消除最大乘数，对应 R3 广播）：由 insight 层输出每条反证的 `related_dimensions`，或按关键词映射，取代全量广播。
3. **审查结论闭环**（对应 R5）：`context_appropriate=false` / contested 的维度进入确定性的"语境重校"步骤（如 strong_negative → negative），而不是只报 issue；`signals_consistent=false` 同步进入 issues，结束"语境不适配"单通道挤压。
4. **语境输入补全**（对应 R6）：review_thesis 节点把 `peer_group` / `peer_benchmarks` 传入 reviewer，并为估值/资本配置维度加"有可比基准时强制引用"的审查规则。
5. **方向分类器补词**（对应 R3 的直接修）："非竞争优势弱化"类证伪式措辞、"掩盖"类确认式措辞进入对应词表，或对 unknown 的中性表述不再一律按负贡献回落。

---

## 附录 A：复现命令与实测证据

### A.1 提取本 run 的审查结论

```bash
python3 - <<'EOF'
import json
d = json.load(open('data/task_records/603986.SH/2026-10-17/task-6e9476f66ac8.json'))
print('overall:', d['overall_judgment'], '| review:', d['review_overall_status'])
print('company:', d['company_industry'], d['company_lifecycle'], d['company_market_cap'], d['company_track_label'])
for i in d['issues']:
    if i.get('category') in ('thesis_gap', 'assumption_invalidated', 'amplification_direction_conflict'):
        print(f"[{i['severity']}] {i['category']}: {i['message'][:180]}")
EOF
```

### A.2 冲突方向分类与证伪假设精确扣除（R2/R3 实测）

```bash
poetry run python - <<'EOF'
from alphabee.agents.thesis.engine import conflict_direction, ThesisEngine
print(conflict_direction("产品结构差异：对标组收入增速高或含并购/低基数/不同子产品结构，公司主业内生份额（相对申万行业）仍在提升，非竞争优势弱化。"))   # -> unknown（保守负贡献 → moat 被罚分）
print(conflict_direction("非经常性收益为证券投资公允价值浮盈，标的高度集中且存在回撤风险，主业扣非盈利质量被高利润增速掩盖。"))                       # -> benign（方向判反）
print(conflict_direction("正常经营变化：周期复苏导致收入/存货/应收等科目剧烈波动，勾稽关系偏离是业务结构剧变而非操纵，数据自洽性可解释。"))             # -> benign（正确）
eng = ThesisEngine()
for t in ["供给缺口逆转：大厂回流利基市场或AI需求放缓，量价齐升逻辑见顶。",
          "非经常性收益虽大但主业扣非盈利同样强劲且可持续。",
          "产能瓶颈：代工产能分配不足或存货快速消化反映备货不足。"]:
    p, s = eng._infer_disputed_evidence({"explanation": t, "summary": ""}, {}, {"pattern_matches": []})
    print(t[:16], '->', p or '∅', s or '∅', '（空 = 分数不动）')
EOF
```

### A.3 档位折减与降档规则（R4 实测）

```bash
poetry run python - <<'EOF'
from alphabee.agents.thesis.models import score_to_judgment, JUDGMENT_THRESHOLDS
print(JUDGMENT_THRESHOLDS)                                   # [(0.5,'strong_positive'),(0.2,'positive'),(-0.2,'neutral'),(-0.5,'negative'),(-1.1,'strong_negative')]
print(score_to_judgment(-0.85, 0.11))   # growth_quality：-1.0×0.85 后仍是 strong_negative
print(score_to_judgment(-1.0, 0.43))    # earnings_quality：strong_negative
print(score_to_judgment(-1.0, 0.11))    # competitive_moat：仅因 conf<0.3 且 score=-1.0 才降为 negative
EOF
```
