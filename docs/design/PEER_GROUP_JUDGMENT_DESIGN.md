# 对标组候选判定设计（C 结构化维度 + D 独立 judge + E 分类学特征）

> 目标条目：把 `alphabee/company_track/` 对标组的候选判定从「单次 LLM 生成 + 单分自评 + 中文关键词否决」升级为「**召回（Recall）→ 独立判定（Judge）→ 确定性闸门（Gate）**」三段式：C（结构化匹配维度）+ D（独立 judge 调用）+ E（申万分类学特征/召回池），并建立可复现的**标注集回归**以标定权重与阈值。
> 关联条目：`docs/roadmap/REPORT_QUALITY_FIX_ROADMAP.md` §11 P2-①（peer 基准补齐 + 在线兜底）；`docs/roadmap/COMPANY_TRACK_ROADMAP.md`（Phase C 对标组构建、Phase D 对标组基准）；`docs/design/SEMANTIC_JUDGMENT_CONTRACT_DESIGN.md`（"关键词表 → 结构化契约 + 受控 judge"的同构先例）。
> 状态：📝 **设计待评审（未实施）**；其中**评测 harness 已实施**（`scripts/peer_group_eval.py` + 标注集 + 单测），下文 §6 含首次真实回归结果。所有"实测"均附复现命令。

---

## 1. 背景与问题

### 1.1 现状（盘点，2026-10）

对标组在线构建链路（`resolve_company_track` 存储未命中时）：

```
本地财报「管理层讨论与分析」(peer_report.fetch_local_report_fragments)
        │
        ├─ infer_peer_candidates : 单次 LLM「推断同环节 A 股」→ 逐条给 reason + overlap
        │        └─ _REASON_MISMATCH 关键词否决（中文措辞枚举）
        └─（兜底）select_peer_candidates : 申万成分股闭集内 LLM 择优
        │
        └─ build_peer_group : 阈值/下限闸门 → tushare 存在性校验（含按名回查陈旧码）→ 持久化
                             → notes 明细 + no_peers 终态
```

### 1.2 三个已确诊缺陷

| 编号 | 缺陷 | 证据 | 后果 |
|---|---|---|---|
| **D-1 关键词不可移植** | `peer_extract._REASON_MISMATCH` 用中文措辞枚举（`下游偏/以碳钢为主/而非/部分重叠…`）判"理由自曝差异" | 见 `peer_extract.py`；表述变体与跨行业专属词（医药"仿制 vs 创新"、软件"订阅 vs 授权"）无法覆盖 | 漏判/误判，需持续打地鼠打补丁 |
| **D-2 自评不校准 + 自利** | 同一调用里"提议候选"与"给候选打分"耦合，`overlap` 绝对分漂移 | 实测 002318 盛德鑫泰一次 0.55、一次留；阈值 0.5/0.6 间抖动 | 阈值不稳，fail 在阈值附近随机 |
| **D-3 无确定性环节先验/召回池** | 候选仅由 LLM 生成；无"同环节"确定信号，也无外部召回 | 联网东财研报摘要不含竞对（实测 5 篇无"广达/纬创/华勤"），一度使 LLM 抽取恒空 | 真对标可能漏（召回受生成器上限约束） |

### 1.3 C/D/E 的概念定位

- **C = 判据长什么样**：结构化匹配维度（product / customer / material_tech / business_model）。
- **D = 谁来判**：独立 LLM 调用（生成者写、评审者判），去自利、可批量相对排序。
- **E = 候选从哪来 + 确定性环节先验**：申万 L1/L2/L3 分类（`all_stocks.csv` 快照 / `index_member_all`）。

> C/D/E **不在同一根轴**：D 治"错"（判据偏差），E 治"漏"（召回/先验）。因此本设计是 **C+D+E 组合**，而非三选一。

---

## 2. 总体架构

```
Recall ─────────── Judge ───────────── Gate ──────────────────────────── 持久化
候选池+E特征        独立LLM             确定性合成overlap
                                    （权重唯一处）+阈值/下限
  │                   │                    │                               │
  ├ 人工candidates     ├ verdict             ├ drop: reject                 ├ PeerGroup
  ├ 本地财报business   │  direct/adjacent/   ├ drop: product<floor          │  codes/international
  ├ 同行业闭集(E)      │  reject            ├ drop: customer<floor         │  reason_map / scores?
  └ E: same_l3/l2      ├ dims(product/      └ drop: overlap<min_overlap     │  notes(剔除明细)
     残差桶→L2降级     │  customer/                                             │  no_peers 终态
                       │  material_tech/
                       │  business_model)
                       └ reason(先匹配点后不匹配点)
```

---

## 3. 详细设计

### 3.1 Recall（候选来源 + E 特征）

候选来源优先级（沿用并扩展 `build_peer_group`）：
1. `candidates`（人工/结构化，最高）；
2. `business_description`（本地财报章节 → LLM 生成）；
3. `universe_codes`（同行业闭集 → LLM 择优）；
4. `fragments`（研报/业绩会，历史路径保留）。

**E 的两个角色**：
- **召回池**：business_description 路径额外并入"同 L3 成分"（残差桶则用 L2），保证真对标进入候选；
- **特征**：对每个候选计算 `same_l3` / `same_l2`（静态快照），并给 target 标 `taxonomy_reliable`。

**残差桶自适应**：
```
taxonomy_reliable = not ( L3_name.startswith("其他")  or  L3成分数 < RESIDUAL_L3_MIN )
```
不可信时：召回用 L2、特征降权、artifact/notes 标注"分类兜底，未经业务核验"。

### 3.2 Judge（D：独立调用，批量）

新增独立函数，输入 target 业务描述 + segments + 候选列表（含 E 特征），**一次批量**判定。逐候选输出：

```json
{
  "code": "002463.SZ",
  "verdict": "direct | adjacent | reject",
  "dims": {"product":0.0, "customer":0.0, "material_tech":0.0, "business_model":0.0},
  "reason": "先列匹配点/不匹配点，再给维度分"
}
```

Prompt 硬约束（跨行业通用，不含行业专属词）：
- **先写匹配点/不匹配点再打分**（reasoning-first，抑制自利）；
- `verdict` 三级语义明确；
- 不要求 LLM 做采纳决定（决定权交 Gate）；
- 注入 E 特征并声明：**同 L3 是强证据但不能替代业务判断**（L3 不同但产品/客户高度重叠仍可 `direct`，如武进不锈之于久立特材；同 L3 但材料/终端不同应 `reject`，如金洲管道）。

> **判模型（已决，§8 决策 1）**：Judge 与生成器**同模型同组件**（`agent.peer_group`）——省一次模型配置与成本；同源模型的**相关误差**风险由「结构化维度 + reasoning-first + held-out 标定」缓解，不引入第二模型。

### 3.3 Gate（C：确定性）

**权重唯一处**（常量/配置）：
```
weights = {product: 0.40, customer: 0.30, business_model: 0.20, material_tech: 0.10}
overlap = Σ w_i · dim_i
```
剔除规则（按序，命中即 drop，落 notes）：
```
1. verdict == "reject"                       → drop("judge reject")
2. product < product_floor(0.30)             → drop("产品重叠不足")
   customer < customer_floor(0.20)           → drop("客户重叠不足")
3. overlap < min_overlap(0.50)               → drop("overlap x < y")
4. E 特征仅作提示，不硬闸（避免误杀武进不锈）
```
保留者写 `reason_map[code]=reason`，并写 `scores[code]=overlap` 与 `match_dims[code]=dims`（**确定持久化**，§8 决策 2）。

**消费侧最小数量闸（已决，§8 决策 4）**：Gate 产出 `codes` 后，若 `len(codes) < min_peers(2)`，**不注入 `peer_*`**，回退 industry 基线，并记 issue/notes（说明"对标组不足、中位数不可比"）。理由：**1 只候选时中位数 = 该股本身**，作为相对基准无意义；期望空组（`expect_empty`）同样走此路。

### 3.4 数据契约

- `PeerGroup`（`peer_group_store.py`）：
  - 已有：`symbol/codes/international/source/name/updated_at/notes/reason_map`；
  - 已加：`no_peers: bool`（LLM 有效响应且无保留 ⇒ 终态，避免每次重跑 LLM）；
  - **确定新增（§8 决策 2）**：`scores: dict[str, float]`（code→overlap）、`match_dims: dict[str, dict[str, float]]`（code→结构化维度）；均为 append 带默认，旧文件向后兼容。
- `notes`：质量闸剔除明细 `质量闸剔除 {code} {name}（{drop}）：{reason}`。
- **不改** `fact_values` 的 `peer_*` 键契约。
- **对标组置信度（已决，§8 决策 5）**：`CompanyTrackArtifact.review_notes` 增加一行置信标注，并在报告层展示。置信度由确定性信号合成（见 §3.7）。

### 3.5 配置（已按 §8 决策定项）

```
company_track.peer_quality:
  enabled: true
  min_overlap: <held-out 标定>          # 初始 0.5 为占位，须回归重标
  weights: {product:0.40, customer:0.30, business_model:0.20, material_tech:0.10}
  product_floor: 0.30
  customer_floor: 0.20
  residual_l3_min_constituents: 15
  judge_enabled: true                   # 与生成同模型（无独立模型项）
  judge_batch_size: 20
  min_peers: 2                          # < 2 只不启用 peer_*，回退 industry
  confidence:                           # 对标组置信度分档阈值
    low: 0.4
    medium: 0.7
```

### 3.6 失败/降级（fail-open）

- **Judge 失败**：不静默采纳生成分；回退生成分（若有）并记 `judge_degraded`，**不置 `no_peers`**（可重试）。
- **无 E 数据**：`same_l3=None`，judge 忽略该特征。
- **全空**：`no_peers = 生成器与 judge 均有效响应且无保留`。
- **对标组不足**（`len(codes) < min_peers`）：不注入 `peer_*`，回退 industry，记 issue。
- 任何异常不抛，节点继续，最终回退 industry 基线。

### 3.7 对标组置信度（已决，§8 决策 5）

确定性合成（不额外调 LLM），分三档（low / medium / high）：

```
confidence_score = w1 · taxonomy_reliable          # E：target L3 是否可信（非残差桶、成分足）
                 + w2 · judge_direct_ratio          # D：保留项中 verdict=direct 的占比
                 + w3 · mean_overlap                # C：保留项 overlap 均值
档位：< low → "低"；< medium → "中"；否则 "高"（低置信时报告显式提示基准参考性弱）
```
展示位置：`CompanyTrackArtifact.review_notes`（一行）+ 报告 `ReportCompanyTrackPayload`（对标组章节）。

### 3.6 失败/降级（fail-open）

- **Judge 失败**：不静默采纳生成分；回退生成分（若有）并记 `judge_degraded`，**不置 `no_peers`**（可重试）。
- **无 E 数据**：`same_l3=None`，judge 忽略该特征。
- **全空**：`no_peers = 生成器与 judge 均有效响应且无保留`。
- 任何异常不抛，节点继续，最终回退 industry 基线。

---

## 4. 与既有实现的差异（删除/替换清单）

| 位置 | 现状 | 改造 |
|---|---|---|
| `peer_extract._REASON_MISMATCH` | 中文关键词正则 | **删除**，由 `verdict`/`dims` 结构化承载 |
| `infer_peer_candidates` | 单次调用出候选+overlap | 拆为 **generator（Recall）** + **judge（独立）**；generator 出 `dims+overlap`，judge 出 `verdict+dims` |
| `build_peer_group` | 直接用生成 `overlap` 阈值 | Gate 改为结构化维度合成 + floors + verdict |
| E | 无 | `same_l3/l2` 特征 + 召回池 + 残差桶→L2 降级 |

---

## 5. 评测 harness（已实施）

`scripts/peer_group_eval.py`：在**人工标注集**上横向比较策略，离线扫参。

- 标注集：`tests/fixtures/peer_group_eval/labels.yaml`（冻结候选池 + 逐候选 keep/drop）；
- 单测：`tests/company_track/test_peer_group_eval.py`（纯逻辑，不调 LLM）；
- 策略：`B_single / C_dims / C_D_judge / C_E_taxo / C_D_E`；
- **record/replay**：LLM 产物落 `data/peer_eval_cache/<symbol>.json`（gitignored）；`--record` 才调 LLM，默认读缓存；**扫参永不调 LLM**；
- 两层指标：`taxonomy_recall`（E 召回上限，离线可算）+ Gate 后 P/R/F1；
- `--sweep` 扫 `min_overlap/product_floor/customer_floor`；`--repeat` 测稳定性；输出 `outputs/peer_group_eval.md`。

### 5.1 首次真实回归结果（2026-10-09，4 case，`agent.peer_group`=deepseek-flash）

复现：`poetry run python scripts/peer_group_eval.py --record --sweep`（首次）；之后 `--sweep` 读缓存。

| 策略 | precision | recall | f1 |
|---|---|---|---|
| B_single | 0.756 | 1.000 | 0.847 |
| C_dims | 0.789 | 1.000 | 0.870 |
| C_D_judge（默认阈值） | 0.938 | 0.688 | 0.688 |
| C_E_taxo | 0.789 | 1.000 | 0.870 |
| C_D_E（默认阈值） | 0.938 | 0.688 | 0.688 |
| **C_D_E（扫参后）** | — | — | **0.889**（`min_overlap=0.45, product_floor=0.2, customer_floor=0.1`） |

分类学召回（E）：

| case | same_l3 recall | same_l2 recall |
|---|---|---|
| 002916.SZ 深南电路 | 1.00 | 1.00 |
| 603986.SH 兆易创新 | 1.00 | 1.00 |
| 002318.SZ 久立特材 | 0.50 | 0.50 |
| 301029.SZ 怡合达 | 0.00 | 1.00 |

**结论**：
1. C ≥ B 且可删关键词表（D-1 解）；
2. D 显著提精度（治假阳：剔上游生益、下游封测、碳钢/洁净错配），但默认阈值过严 → 需放宽（D-2 解的方向）；
3. E 的 **L3 召回不可靠**（002318=0.5、301029 L3=0、L2=1）⇒ **E 只能作特征/召回池，绝不硬闸**（D-3 解）。

### 5.2 当前结果的局限（**尚不足以一次性整体实施**）

- **样本仅 4 例，扫参 in-sample**：0.889 是在这 4 例上调出，无 held-out，过拟合风险高；4 例间 P 差 0.03 属噪声。
- **标签边界争议**：`300963 中洲特材`、`002478 常宝` 等"边界 keep/drop"需独立分析师确认。
- **prompt 双维护**：harness 内 generator/judge prompt 是复制，需改为**调用生产函数**再评估。
- **稳定性未测**：`--repeat` 尚未跑，未能证明 D 降抖动。
- **跨行业覆盖不足**：缺银行/消费/医药/周期/真·空组。

---

## 6. 实施路径（分批，带门禁）

> 原则：**先补验证，再分批上**；每步用 harness 回归；判定口径变更须按 ROADMAP「行为变更登记」流程登记。

### Step 0（前置，不改判定行为）
- 标注集扩到 **~15–20**，覆盖六类业态（同质 L3 / 分类冲突 / 残差桶 / 境内外混合 / 无对标空组 / 跨行业），留 **held-out**（约 70/30）。
- `--record --repeat 3` 出稳定性（保留集 Jaccard），确认 D 降抖动。
- harness 改为**import 生产函数**（`infer_peer_candidates` + 新 judge），消除 prompt 双维护。
- **DoD**：held-out 上策略序稳定；稳定性可量化。

### Step 1（P0 / C，可立即落地，风险低）
- 结构化维度替换 `_REASON_MISMATCH`；确定性 Gate（权重/阈值先用常量）。
- `PeerGroup` 加 `scores` / `match_dims`（append 带默认，向后兼容）。
- **消费侧最小数量闸**：`len(codes) < min_peers(2)` ⇒ 不注入 `peer_*`、回退 industry 并记 issue。
- 更新单测：删关键词相关用例，加维度解析/闸门/最小数量/判别力变异。
- **DoD**：held-out 上 C ≥ B；关键词表删除；min_peers 生效；行为变更登记完成。

### Step 2（P1 / D）
- 拆独立 batch judge（**同模型**，无独立模型配置项）；judge 失败 fail-open（不置 `no_peers`）。
- **用 held-out 定阈值/权重**（不是当前 4 例）。
- **DoD**：held-out 上 C+D ≥ C，且稳定性 Jaccard 提升。

### Step 3（P2 / E + 置信度）
- `same_l3/l2` 作特征 + 召回池 + 残差桶→L2 降级；**绝不硬闸**。
- **对标组置信度**：按 §3.7 确定性合成三档，写 `review_notes` 并在报告对标组章节展示。
- **DoD**：召回提升（taxonomy_recall 提高）、不放宽精度、置信度分档可复算。

---

## 7. 测试计划

- **单元**：dims 解析/权重合成；四条剔除规则各一；残差判定；`same_l3/l2` 查表；judge 失败降级；陈旧码按名回查。
- **Prompt 契约**：fake model 断言 reasoning-first 与 verdict 语义、输出 schema。
- **节点级**：store 未命中 → Recall+Judge+Gate → 持久化；`no_peers` 跳过重试；notes 明细。
- **判别力变异**：M1 去 product floor；M2 改权重；M3 忽略 residual；M4 judge 失败误置 `no_peers` ⇒ 各必红。
- **标注集回归**：横向 C / C+D / C+E / C+D+E 的 P/R/F1（held-out）；离线扫参；稳定性。
- **配置**：权重/阈值配置化后，缺段回落默认值（未配置者行为不变）。

---

## 8. 决策记录（已决）

| # | 问题 | 决策 | 落点 |
|---|---|---|---|
| 1 | Judge 与生成是否同模型 | **同模型同组件**（`agent.peer_group`）；相关误差由结构化+reasoning-first+held-out 缓解 | §3.2、§3.5 |
| 2 | `scores`/`dims` 是否持久化 | **持久化**：`PeerGroup.scores` + `PeerGroup.match_dims`（append 带默认，旧文件兼容） | §3.3、§3.4 |
| 3 | 权重/阈值来源 | **由 held-out 标注集标定**（初始常量仅为占位，回归后重设） | §3.5、§6 Step 2 |
| 4 | 最小对标数闸 | **`len(codes) < 2` 不启用 `peer_*`**，回退 industry 并记 issue（1 只时中位数=该股本身） | §3.3、§3.6、§6 Step 1 |
| 5 | 报告层是否展示置信度 | **展示**：`review_notes` + 报告对标组章节，按 §3.7 三档合成 | §3.4、§3.7、§6 Step 3 |

> 以上决策不改变"先补验证、再分批上"的总体路径；阈值仍需 Step 0 的 held-out 数据落地后才可冻结。

---

## 9. 附录

### 9.1 复现命令
```bash
# 首次采集（调 LLM 并写缓存）
poetry run python scripts/peer_group_eval.py --record --sweep
# 复算/CI（读缓存，秒级）
poetry run python scripts/peer_group_eval.py --sweep
# 稳定性
poetry run python scripts/peer_group_eval.py --record --repeat 3
# 单测
poetry run pytest tests/company_track/test_peer_group_eval.py -q
```

### 9.2 相关代码（现状）
- `alphabee/company_track/peer_extract.py`：`_REASON_MISMATCH`、`infer_peer_candidates`、`select_peer_candidates`。
- `alphabee/company_track/peer_group_build.py`：来源优先级、`_format_dropped`、A 股校验与按名回查。
- `alphabee/company_track/peer_group_store.py`：`no_peers` 终态。
- `alphabee/company_track/peer_report.py`：本地财报片段。
- `alphabee/company_track/peer_universe.py`：闭集候选/名称映射。
- `alphabee/orchestrator/nodes/resolve_company_track.py`：在线兜底接线。
