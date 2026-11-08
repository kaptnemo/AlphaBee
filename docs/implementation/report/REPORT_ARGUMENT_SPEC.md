# 报告论证规范（REPORT_ARGUMENT_SPEC）

> 状态：v0（W1-Q0 固化，2026-11-08）。
> 来源：`docs/implementation/framework/REPORT_CLAIM_FIRST_AND_PARAMS_GOVERNANCE_DESIGN.md` §1.5 / §3.1。
> 定位：**可执行判据**，不是写作风格建议——报告评审、Q2 门禁（G1–G4）与 memo_lite prompt 都逐条对照本页。
> 适用范围：报告主文（一页结论 / 核心主张 / 情景与触发 / 我们可能错在哪里 / 观察清单 / 关键指标）；附录只受“不与主文重复论证”约束。

## 1. 实质主张（material claim）的三条判据

一句话同时满足以下三条，计为 **1 条实质主张**：

1. **含方向/判断词**：回升、下移、低估、高估、成立、不成立、领先、落后、不可持续……（纯描述句不算）。
2. **含数字、比较或时间窗**（至少一项）：数字 / 同比环比 / 与对标组比较 / “2026Q4 前”这类可判定的时间窗。
3. **不是输入字段的同义复述**：把“毛利率 10.07%”改写成“毛利率承压”不算新主张；主张必须表达出可被反驳的判断。

用途：统计核心主张数量（Q1-lite 建议 2–3 条；Q2 上限 5 条），判断主文“有没有观点”。

## 2. 论证六要素（Toulmin 落位）

| 要素 | 报告中的位置 | 硬要求 |
|---|---|---|
| Claim | 每条主张的首句 | 可反驳；不含“可能/或许”搪塞 |
| Grounds | 依据段 | ≥1 条带数字与口径的证据；或显式标 `inferred` |
| Warrant | 推理段 | 说明“依据为何支持主张”的机制，不得只写“综上” |
| Rebuttal | 反证段 | 至少一条最强反证，并说明为什么没推翻主张 |
| Qualifier | 限定段 | 置信度（高/中/低）+ 适用时间窗 |
| Falsification | 失效条件 | 四字段齐全：指标 / 阈值 / 时间窗 / 数据来源 |

## 3. 长度预算

| 单元 | 预算 | 说明 |
|---|---:|---|
| 一页结论 | ≤800 字 | 1 主张 + 3 证据 + 2 反证 + 1 失效条件 |
| 核心主张 | 3 条 × ≤500 字 | 每条六要素齐全；Q2 上限 5 条 |
| 情景与触发 | ≤1,200 字 | base/bull/bear 各 ≤300 字 + 触发条件 |
| 我们可能错在哪里 | ≤1,200 字 | 集中回应反证，不写模板风险词 |
| 观察清单 | ≤1,000 字 | 表格 ≤6 行：看什么 / 何时变 / 什么情况下错 / 来源 |
| 关键指标 | ≤800 字 | 6–8 行表格 + 一段“支撑/挑战” |
| **主文合计** | **≤10,000 字** | 硬上限（Q2 门禁 G4） |
| 附录 | 不限 | 必须与主文不重复论证；主文用“见附录 X”引用 |

字符数按 Python `len()` 计（中文 1 字符），与基线脚本同一口径；主文 = 上述六节之和（不含免责声明与附录）。

## 4. 好坏对照（600760 真实内容改写）

**合格示例（六要素齐全）**：

> **主张**：中航沈飞当前的核心矛盾不是收入下滑，而是毛利率中枢从 13.7% 下移到 10% 附近；若 2026Q4 单季毛利率不能回到 12% 以上，“交付节拍错配”的解释不成立。
> **依据**：前三季毛利率 10.07%（同比 −1.76pp）；Q3 单季收入 +23.29% 而毛利率仅 9.63%；7 家对标中 5 家毛利率同比上升。
> **推理**：若是固定成本摊薄，收入放量应带来毛利率回升；量增而毛利降，指向定价或型号结构位移。
> **反证**：Q3 单季归母 +144%、扣非 +40.7%，2026 年零存货计提——恶化尚未兑现。
> **限定**：中等置信，适用于 2026Q4–2027Q1。
> **失效条件**：2026 年报 Q4 单季毛利率 ≥12%，且合同负债回补至 44 亿元以上；来源：2026 年报 + 资产负债表。

**不合格示例（归档报告里的等价写法）**：

> 中航沈飞的毛利率近期有所承压，收入同比下滑，估值处于较高水平，同时订单交付节奏存在不确定性，建议关注后续经营变化。

不合格原因（逐条对照）：

- 没有可反驳的方向判断：“有所承压 / 存在不确定性”无法判定真假，也没有阈值；
- 数字、口径、时间窗全缺：“近期 / 后续 / 较高”均不可观测；
- 六要素 0/6：无依据、无推理、无反证、无限定、无失效条件；
- 形容词堆砌 + 模板风险词，读者无法据此检验，也无法逐条打分。

## 5. 反模式清单（评审逐条对照）

| # | 反模式 | 判定 | 修法 |
|---|---|---|---|
| 1 | 形容词堆砌 | “显著 / 明显 / 大幅”无数字锚点 | 换数字 + 口径 + 对比基准 |
| 2 | 信号堆砌 | 主文逐条罗列信号 / 异常 / 冲突 / 维度 | 只引用支撑当前主张的 2–4 条，其余移附录 |
| 3 | 无源断言 | 数字无财报期 / 行情日 / 来源 | 补口径与来源；补不了标 `inferred` 或删除 |
| 4 | 单源支撑 | 仅一条证据支撑强结论 | 补独立第二源，否则降 Qualifier |
| 5 | 无 Rebuttal | 只列支持证据 | 至少给一条最强反证，并解释为何没推翻主张 |
| 6 | 不可证伪 | “若基本面恶化则降级” | 指标 + 阈值 + 时间窗 + 数据来源四字段齐全 |
| 7 | 因果跳跃 | 相关即因果、缺机制 | 补 Warrant：依据为何支持主张 |
| 8 | 概率误用 | 把 softmax 权重 / 模型分数说成概率 | 标注“模型权重，非频率概率”或删除 |

## 6. 基线口径与复现

### 6.1 八条口径（设计 §1.1 固化）

1. `total_chars` = `len(text)`（中文按 1 字符计），基于 UTF-8 文本。
2. `body13_chars` = 13 个 `## ` 章节正文之和；不含标题行、头部元信息与章节边界空行（保留正文内部段落空行）。
3. `narrative_chars` = executive_summary / investment_viewpoint / scenario_analysis / key_metrics / falsification_conditions / risks / disclaimer 七节之和。
4. `detail_chars` = signal_analysis / anomaly_detection / conflict_analysis / dimension_analysis / company_track / review_findings 六节之和。
5. `issue_chars` = `## 系统问题` 起至中期决策块之前；`midterm_chars` = 中期决策块（`═`/`=` 分隔行到 EOF，含分隔行前换行）。
6. `numeric_tokens` = 正则 `\d+(?:\.\d+)?%?` 的匹配数；`numeric_per_kchar` = 每千字符 token 数。
7. `detail_ratio` = `detail_chars / body13_chars`；`nonarg_ratio` = `(issue_chars + midterm_chars) / total_chars`；分母为 0 记 0。
8. `issue_count` / `issue_max_msg_chars`：归档 md 从 `- [severity] category: message` 行单行解析；TaskRecord 口径直接取 `record.issues` 的条数与消息长度。

`--layout memo` 时追加 `main_chars` / `appendix_chars`（主文六节之和 / 附录 A–F 六节之和）；`--layout legacy` 时 `main_chars = body13_chars`、`appendix_chars = 0`。

### 6.2 复现命令

```bash
# 归档 markdown 口径（复现设计 §1.2 的 5 份报告）
poetry run env HOME=/data/freedom/AlphaBee/tmp/pytest_home \
  python scripts/report_baseline.py --reports data/reports --format all --out tmp/report_baseline/

# 任务记录口径（渲染后统计；坏记录跳过并计数）
poetry run env HOME=/data/freedom/AlphaBee/tmp/pytest_home \
  python scripts/report_baseline.py --task-records data/task_records --format csv --out tmp/report_baseline/

# memo 口径（追加 main_chars/appendix_chars）
poetry run env HOME=/data/freedom/AlphaBee/tmp/pytest_home \
  python scripts/report_baseline.py --reports data/reports --layout memo --format all --out tmp/report_baseline/memo/
```

本机 HOME 覆盖是仓库既有测试约定（避免 import 期写只读 HOME）；普通环境直接用 `poetry run python ...` 即可。

### 6.3 基线参考值（2026-11-08，`--reports data/reports`）

| 报告 | total_chars | numeric_tokens | issue_chars | midterm_chars |
|---|---:|---:|---:|---:|
| 300504.SZ 2026-10-30 | 45,590 | 1,614 | 12,467 | — |
| 600760.SH 2026-11-01 | 42,605 | 1,846 | 12,861 | 7,880 |
| 兴森科技 2026-10-17 | 18,694 | 437 | 5,681 | — |
| 海康威视 2026-10-22 | 42,921 | 1,685 | 12,000 | — |
| 生益科技 2026-10-19 | 39,291 | 1,653 | 9,527 | — |

> 与设计 §1.2 的差异：设计表的 13 节正文 / 叙事 / 明细列把 `## ` 标题行与章节边界空行计入（600760 合计 154 字符），本规范按 §1.1 的正文口径剔除，故 `body13_chars` 系统性略小；`total_chars` / `numeric_tokens` / `issue_chars` / `midterm_chars` 与设计表逐字一致。

### 6.4 两种输入口径的差异

- `--reports`：读归档 md 原文，`symbol` / `date` 由文件名推断；无代码的文件名（如 `兴森科技-2026-10-17.md`）以文件名前缀作 `symbol`，输出中注明“非代码”。issue 元信息从系统问题区单行解析。
- `--task-records`：反序列化 TaskRecord 后用 `build_report_markdown` 渲染再统计；标题 / 元信息 / 中期块由记录与当前渲染器重建，`total_chars` 不能与归档 md 直接互比，跨口径只比较占比列。`issue_count` / `issue_max_msg_chars` 直接取 `record.issues`；`report_raw` 为空的记录仍渲染最小骨架并产出一行。
- 归档 md 的 issue 行按 `- [severity] category: message` 解析，`issue_max_msg_chars` 只计 message 部分（不含前缀）；设计 §1.3 的 1,213 / 12,808 是整行长度，两处数值不可直接对照（600760：message 最长 1,166，整行最长 1,213）。

### 6.5 评审用法

- 每次报告评审：先按 §5 反模式清单过一遍，再按 §1 数实质主张、按 §3 对预算、按 §2 检查每条主张六要素是否齐全；
- 主文字数按 §6.1 口径复核（脚本内 `count_main_chars`，与后续 G4 门禁共享同一实现），附录不计入；
- 基线与新布局的对照一律用 `baseline.csv` 的占比列（`detail_ratio`、`nonarg_ratio`），不跨输入口径比较绝对字符数。
