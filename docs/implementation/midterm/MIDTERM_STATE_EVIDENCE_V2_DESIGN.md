# 中期决策层状态证据公式 V2 设计（State Evidence V2）

> **状态：设计文档（2026-10-30 定稿，尚未实施）**。前置：`docs/implementation/midterm/MIDTERM_CLASSIFIER_TEF_DESIGN.md`（TEF 变体，已实施）、`docs/midterm/MIDTERM_DECISION_MODEL.md`（§4.2 状态映射 / §4.3 顶部顺序 / §4.4 共振背离语义基线）。
> **立项依据**：2026-10-30 对 `600760.SH` 一帧（`as_of=2026-10-30`，方向分 `F=-1 / E=-0.5169 / T=+1 / V=+0.6015 / C=-0.1902 / R=-0.0874`）的逐位复算 + 5^6 全网格枚举。复算结果：该帧在 `tef` 下得到 **S2 概率质量 0.5264**，而 S2 的证据 `1.0` 全部来自 `_not_up(e)=1.0`（E 不是正的），F 的贡献为 0；**同一帧在「T≥0」半平面内 S4/S5 恒为 0、数学不可达**。
> **本文档只做公式与契约设计，不含实现**。实施切分见 §10。
> 关联 skill：`alphabee-pipeline-contract-steward`（§6 契约传播）、`alphabee-schema-steward`（§6.1 字段治理）。

---

## 0. 一页摘要

四个缺陷 → 四个机制，一一对应，逐条可测：

| 编号 | 缺陷（现状实测） | 机制 | 验收不变量 |
|---|---|---|---|
| **D1** | `_not_up` 单边：E 从 `+0.05` 掉到 `-1.0`，S2 证据 `0.95 → 1.00`（"恶化 = 确认"） | **M1 双侧中性带** `neutral_band(x,w)` | I1 单调性 |
| **D2** | 乘法硬门：`T ≥ 0 ⇒ S4=S5=0`，关键格（F≤−0.5,E≤−0.3,T≥0.3）**1000/1000 判为 S2/S1** | **M2 双击证伪通路** `max(价格路径, F×E 证伪路径)` | I2 可达性 / I3 关键格 |
| **D3** | 证据上限不齐：S3 上限 3.0 vs S5 上限 1.5 ⇒ 完美多头 P=0.9706/熵 0.168，完美空头 P=0.6722/熵 0.920 | **M3 上限归一化 + 温度重标定** | I4 对称性 / I5 标定 |
| **D4** | 可观测性缺口：`uncertain` / `consistency` / 背离文案**不进 artifact**，报告只印 argmax+熵+概率质量 | **M4 有向不确定 + 结构化背离 + 渲染** | §8.4 接线 |

**改后同帧结果**（`600760`，`tef`）：`argmax=S5`，`P=0.795`，看空质量（S4+S5）= `0.819`，`熵=0.800 nats`（≤ 1.0 ⇒ 允许清仓分支），并产出 `AdverseDivergence(severity=0.76)`。关键格 1000 组：`S5 = 100%`。六状态全部可达。

**默认行为零变化**：新增 `midterm.formula_revision`，缺省 `v1`（路由到现有 `classifier.py` / `classifier_tef.py`，一行不改）。`v2` 为显式 opt-in，验收通过后再切默认。

---

## 1. 背景与问题

### 1.1 触发帧（可复现）

`600760.SH`，`as_of=2026-10-30`，财报口径 `20260930`：

```text
方向分: F=-1.0  E=-0.5169  T=+1.0  V=+0.6015  C=-0.1902  R=-0.08741
v1-tef: 分布 {S0:.1298, S1:.1300, S2:.5264, S3:.0712, S4:.0712, S5:.0712}  熵 1.4327
v1-etf: 分布 {S0:.1235, S1:.1237, S2:.0678, S3:.0678, S4:.0992, S5:.5181}  熵 1.4516
```

`v1-tef` 的 S2 证据为 `1.0`，分解为 `t_up × (f_up + _not_up(e)) = 1.0 × (0 + 1.0)`：F 贡献 0，E 贡献满额。且该分布对 F/E 的**幅度完全不敏感**：

| F | E | S2 证据 | P(S2) |
|---|---|---|---|
| −1.00 | −1.0000 | 1.0000 | 0.5264 |
| −0.60 | −0.5169 | 1.0000 | 0.5264 |
| −0.05 | −0.0500 | 1.0000 | 0.5264 |

### 1.2 缺陷清单

**D1（家族共有，`classifier.py` 与 `classifier_tef.py` 同构）**：`_not_up(x) = 1 − max(0, x)` 是**单边**算子，语义应为"尚未上修"（接近中性），实现成了"只要不是正的就给满分"。后果：

- S2 的证据随 E 恶化而**上升**（`E=+0.30 → S2ev=0.70；E=+0.05 → 0.95；E≤0 → 1.00`）；
- ETF 侧镜像：`S2 = e_up × (f_up + _not_up(t))`，T 从 `+0.30` 掉到 `−1.0`，`P(S2) 0.41 → 0.56`。

这是**符号方向错误**，不是调参问题。

**D2（TEF 特有）**：`t_down = clip01(−T)`，因此 `T ≥ 0 ⇒ S4 = S5 = 0 ⇒` 永远不可能成为 argmax（`S0 = 0.30` 恒大于 0）。状态机在"股价未破位"时只剩 4 个出口，且加仓侧只有 S2。全网格（5^6=15625，取值 `[−1,−0.5,0,0.5,1]`）实测：

| 口径 | 关键格（F≤−0.5,E≤−0.3,T≥0.3，1000 组） | 全网格 S5 占比 | 全网格 S0 占比 |
|---|---|---|---|
| `v1-tef` | S2 **90%** / S1 10% / S4+S5 **0%** | 1.2% | 24.0% |
| `v1-etf` | S5 **74%** / S4 26% | 14.9% | 4.7% |

**这不是"退出慢半拍"（TEF 设计 §3.2 披露的时滞口径），而是"退出在数学上不可达"。** 披露口径低估了严重程度。

**D3（家族共有，TEF 加重）**：各状态可达上限不同（S3=3.0、S2=2.0、S5=1.5、S0≡0.30），softmax 内建未文档化的状态先验：

| 输入 | 证据 | argmax | P | 熵 |
|---|---|---|---|---|
| 完美多头 F=E=T=+1 | S3=3.0 | S3 | **0.9706** | **0.168** |
| 完美空头 F=E=T=−1 + 贵 + 拥挤 | S4=2.0, S5=1.5 | S4 | **0.6722** | **0.920** |

后果 A：多头可达 97%、空头最多 67%，"状态确信度"在两方向不是同一个量。
后果 B（跨层耦合）：`_ENTROPY_UNCERTAIN=1.0` 对空头更易触发，而 `position.py:175` 的硬清零条件是 `raEV<0 且 entropy≤1.0` ⇒ **清仓路径在空头帧上系统性被削弱**。网格实测 `uncertain` 率：`tef 90.8%` / `etf 88.4%`，`tef` 中位熵 `1.591`（= 88.8%·ln6）。

**D4（可观测性）**：`ClassifierResult.uncertain` / `Consistency` / `_divergence_reason` 都不进 `CompanyStateArtifact`（[models.py:544](alphabee/midterm/models.py#L544)），因此 `_render_state`（[midterm_reporter.py:221](alphabee/orchestrator/nodes/midterm_reporter.py#L221)）只能印 `argmax / 熵 / 分布 / 概率质量`。读者看到 "S2 · 0.5264" 时无法知道分类器内部已判 `uncertain=True` 且给出了具名背离。

### 1.3 为什么不能靠调参解决

D1/D2 是**结构性的**：`_not_up` 的单边性与 `t_down` 的乘法零化，对任何温度/阈值取值都不成立——把温度调到让 S2 低于 0.5，只会让所有状态都低于 0.5（分布整体摊平），不会让 S5 从 0 变成非 0。D3 的上限不齐是公式几何，也不能由温度修正（温度是全局缩放，上限差异是状态间差异）。故必须改**形式**，且只能改"顺序哲学"以外的部分。

---

## 2. 目标与非目标

### 2.1 目标

| 编号 | 目标 | 可测口径 |
|---|---|---|
| **G1** | 证据对因子恶化**单调**：F/E 向不利方向移动时，多头侧状态（S0–S3）证据不得上升，空头侧（S4/S5）不得下降 | 属性测试 I1（扫 `F,E ∈ [−1,0]` 各 21 格 × 5 组其他因子） |
| **G2** | **每个状态都可达**，且 S5 在 `T>0` 半平面可被"F/E 双击证伪"触发 | 可达性测试 I2：六状态各存在输入使 `P≥0.65`；`F=E=−1,T=+1 ⇒ S5` |
| **G3** | **幅度敏感**：`F=−1` 与 `F=−0.5`（其余相同）必须产生不同分布 | 敏感性测试 I3：`max_s |ΔP| ≥ 0.02` |
| **G4** | 证据与分布**可比较**：每状态证据归一化到 `[0,1]`；熵可跨状态、跨变体比较 | 上限测试 I4：归一化后每状态可达 1.0；多空极端帧 `P_top` 差 ≤ 0.10 |
| **G5** | **关键格不反转**：`F≤−0.5 ∧ E≤−0.3 ∧ T≥0.3` 的网格点，看空侧（S4+S5）质量 ≥ 0.5 | 关键格测试 I3b |
| **G6** | **可观测**：`uncertain` / 一致性 / 背离文案 / 反证严重度进入 artifact、报告、CLI、跟踪帧、任务记录 | 接线测试 §8.4（producer→consumer 全覆盖） |
| **G7** | **零回归**：`formula_revision=v1` 的**数值**与今天逐值一致（含 ETF）；artifact 仅多出 8 个 additive 字段 | 回归测试 §8.3（两段式 golden） |

### 2.2 非目标

| 编号 | 非目标 | 理由 |
|---|---|---|
| N1 | 不改 T→E→F / E→T→F **顺序哲学**与迁移合法性集合 | 顺序本身有 A 股依据（TEF 设计 §1.1），问题在形式 |
| N2 | 不改 `score_engine` / `bayes` / `evidence_rules` / `insight_evidence_adapter` | 方向分口径与后验数学不在本次问题域 |
| N3 | 不改 `position.py` 公式 | v2 的熵已是可比量，现有阈值经验证可判别（§6.5）；若校准不符再走 §11.2 R3 |
| N4 | 不做"每标的自动选序" | 沿用 TEF 设计 §1.2 非目标 |
| N5 | 不改 M 因子 / `market_regime` / 编排图 / `NODE_ORDER` / `ArtifactType` | 与状态证据正交 |
| N6 | 不升 `CompanyStateArtifact.schema_version` | 全部为 additive 字段 + 默认值（沿用 TEF 设计 §6.1 先例） |
| N7 | 不引入 LLM / 网络 / 随机性 | 证据层必须是确定性纯函数 |

---

## 3. 总体设计

### 3.1 分层

```text
score_engine.compress_scores            （不改）
   → VariableScores
        ├── v1: classifier.py / classifier_tef.py     （不改，权威 v1 实现）
        └── v2: classifier_v2.py                      （新增，本次交付）
                 evidence → normalize → softmax → belief / consistency / adverse
   → decision_model.evaluate              （改：formula 路由 + 字段写入 + direction）
   → CompanyStateArtifact                 （改：新增 8 个 additive 字段）
   → position / reporter / cli / tracking / task_records   （改：渲染与消费，不改公式）
```

### 3.2 为什么新增 v2 模块，而不是就地改 v1

1. **A/B 与历史可复现**：v1 是现有 A/B 基线（TEF 设计 §9.0），一旦改动，历史帧与对比实验全部失效。
2. **零回归可证**：v1 模块一行不改 ⇒ 「默认配置行为不变」是可证的（golden JSON），不需要"看起来一样"。
3. **对比隔离**：v1/v2 差异与 etf/tef 差异解耦，形成 2×2 矩阵（§3.3），否则无法归因。
4. **失败隔离**：v2 有 bug 时 `formula_revision=v1` 即回滚，无需 revert 代码。

代价：v1 与 v2 是两份实现（~200 行重复）。**治理约定**：v1 冻结，不再加功能；v2 是唯一演进分支（写入模块 docstring）。

### 3.3 2×2 矩阵

| | `etf`（E→T→F） | `tef`（T→E→F） |
|---|---|---|
| **v1** | `classifier.py`（现状） | `classifier_tef.py`（现状） |
| **v2** | `classifier_v2.py(order="etf")` | `classifier_v2.py(order="tef")` |

v2 内部用同一个 `order` 参数表达两种顺序的**门控落点**（见 §4.3），其余机制（M1/M2/M3）两序共享 —— 因为 D1/D3 是家族缺陷，D2 的 "双击证伪通路" 是两条路径取 `max`，与顺序无关。

---

## 4. 证据层（V2 公式）

### 4.1 机制与缺陷的映射

| 机制 | 解决 | 落点 |
|---|---|---|
| **M1 双侧中性带** `neutral_band(x,w)` | D1：把"不是正的"换成"接近中性"，两侧都衰减 | S1 的 E 项、S2 的 E 项（tef）/ T 项（etf） |
| **M2 双击证伪通路** | D2：退出不再被 `t_down` 独占 | S5 = `max(价格路径, F×E 证伪路径)` |
| **M3 上限归一化 + 温度重标定** | D3：状态间上限可比、熵可比较 | 每个状态的证据 `÷ cap`，`cap` = 该状态可达上限 |
| **M4 有向不确定 + 结构化背离** | D4：可观测、可消费 | `uncertain` 改由 `P_top` 判定；新增 `AdverseDivergence` |

### 4.2 算子定义

```python
def clip01(x: float) -> float: ...
def up(x: float | None) -> float:          # 正部；缺失 → 0（不制造虚假证据）
def dn(x: float | None) -> float:          # 负部；缺失 → 0
def neutral_band(x, w) -> float:           # 双侧中性带：|x|≥w → 0；x=0 → 1；缺失 → 0.5
def not_yet(x, w) -> float:                # 单侧"尚未上修"：1 − max(0,x)/w；缺失 → 0.5
def severe(x) -> float:                    # 仅负侧严重度：x≥0 → 0；|x|≤LO → 0；|x|≥HI → 1；线性
```

**为什么 `not_yet` 仍保留单边**：它只用在 S4 的"预期停止上修"语义上（E 转负完全满足"停上修"）。若把它也改成双侧，S4 会在 E 崩盘时反而下降——那是语义反转。**S1/S2 的"尚未共振"必须用 `neutral_band`**，这正是 D1 的修复点。

**缺失值口径与 v1 对齐**：`up/dn` 缺失 → 0；`neutral_band` / `not_yet` 缺失 → **0.5**（与 v1 `_not_up(None)=0.5` 同口径：给半分"信息不足"的信用，既不判死也不满额确认）；`severe` 缺失 → 0（不制造证伪证据）。

> 这条对 TEF 的目标标的群体很关键：低覆盖标的的 `e_revision` 常为 `None`（`coverage<3` 护栏）。若把缺失当 0，S2 会退化为 `t_up × f_up`，价格先行逻辑对低覆盖标的直接失效；取 0.5 才能保持"价格先确认、预期待补"的原意。缺失本身仍通过熵与 `degraded`/`missing_facts` 显式暴露，不改"缺失不得静默变 0"的纪律。

### 4.3 逐状态公式

记 `f,e,t,v,c` 为方向分；`f_up=up(f)`、`e_dn=dn(e)` …；`ne=neutral_band(e,W_E)`、`nt=neutral_band(t,W_T)`、`nf=neutral_band(f,W_F)`；`sf=severe(f)`、`se=severe(e)`。

```text
# ── S0 研究候选（含"无信号"与"信号互相矛盾"两种情形，故带底噪） ──
neutrality = nf * ne * nt                                  # ∈[0,1]
S0 = S0_BASELINE * (S0_FLOOR + (1 - S0_FLOOR) * neutrality)

# ── S1 预期差形成：预期接近中性 + F 未恶化 + 价格弱/估值便宜 ──
S1 = ne * (f_up + 0.5 * t_dn + 0.5 * v_up) * (1 - sf)

# ── S2 证据确认（价格先行）：门控随 order 翻转 ──
S2_tef = t_up * (f_up + ne)            # T 先确认，预期尚未共振
S2_etf = e_up * (f_up + nt)            # E 先确认，价格尚未共振

# ── S3 共识扩散：三共振（min 天然是幅度渐变，保持 v1） ──
S3 = 3.0 * min(f_up, e_up, t_up)

# ── S4 充分定价 / 减仓：门控随 order 翻转 ──
S4_tef = t_dn * (not_yet(e, W_E) + v_dn + c_dn)
S4_etf = not_yet(e, W_E) * (t_dn + v_dn + c_dn)

# ── S5 退出：两条独立路径取 max（价格破位确认 OR F/E 双击证伪） ──
S5 = max(t_dn * (e_dn + 0.5 * f_dn), BREAK_LAMBDA * se * sf)
```

**每条的设计理由（评审用）**

| 项 | 理由 |
|---|---|
| `S0` 底噪 `S0_FLOOR` | 去掉恒定 0.30 的"永远竞争者"（v1-tef 全网格 S0 占 24%），但保留"矛盾信号 ⇒ 研究候选"的语义；纯中性时 `S0` 归一化为 1.0 |
| `S1 × (1−sf)` | F 严重恶化时，"便宜 + 价格弱"不能叫预期差；避免 600760 帧里 V=+0.60 单因子把 S1 抬到 0.30 |
| `S2` 用 `ne` 而非 `not_yet(e)` | D1 修复：E 越负，S2 越低；`E=−1` 时 `S2=0` |
| `S2` 用 `t_up` 作门控（tef） | 保留 TEF 顺序哲学：价格先确认 |
| `S5` 的第二条路径 | D2 修复：**价格不破位也能退出**，但要求 F 与 E **同时**实质恶化（双击），且有 `SEVERE_LO` 死区 ⇒ 不会把"轻微下修"当证伪 |
| `S5` 用 `max` 而非加法 | 避免双重计数；两条路径各自单调（加法会让"价格破位 + 双击"两条同时满足时证据被重复放大） |

**双路径的语义边界（重要）**：困境反转与证伪的区别在于**预期是否已被砍**。

| 输入 | 含义 | v2 结果 |
|---|---|---|
| `F=−1, E=−0.2, T=+1` | 基本面已崩、预期尚未下修（反转候选） | `S2=0.365, S0=0.314, S5≈0.08` ⇒ `uncertain=True`，不退出、不加仓 |
| `F=−1, E=−0.52, T=+1` | 基本面崩 + 预期被打（双击） | `S5=0.795`，`AdverseDivergence=0.76` |
| `F=−1, E=−1, T=+1` | 完全证伪、价格仍强（顶背离典型） | `S5=0.923` |

### 4.4 证据上限与归一化

```python
EVIDENCE_CAPS = {"S0": 0.30, "S1": 2.0, "S2": 2.0, "S3": 3.0, "S4": 3.0, "S5": 2.0}
# S5 cap = max(1.5, BREAK_LAMBDA)，导入期断言相等（防参数改动后 cap 漂移）
norm[s] = clip01(evidence[s] / EVIDENCE_CAPS[s])        # ∈ [0,1]，且每状态可达 1.0
```

上限即"该状态公式在 `[−1,1]^6` 上的可达最大值"——这是结构性常量，**不参与调参**（测试 I4 断言每状态存在输入使其归一化为 1.0）。

### 4.5 分布与温度

```python
logits[s] = norm[s] / TEMPERATURE
P[s]      = softmax(logits)[s]          # 均匀先验（等价于在 logits 上加常数，可省）
H_nats    = -Σ P[s]·ln P[s]
H_norm    = H_nats / ln(6)              # ∈ [0,1]
```

**语义声明（必须进 docstring 与报告文案）**：`P` 是"归一化模式证据的 softmax 权重"，**不是频率概率**，也不可当作置信度区间；`H_norm` 是分布离散度。`P_top = max(P)` 用于 `uncertain` 判定（§5.3）。

均匀先验 + `S0` 底噪保证"任何输入都有可达分布"（不会出现全零 → 均分 1/6 的假高熵）。

### 4.6 参数表

| 参数 | 默认 | 可调 | 含义 / 校准门 |
|---|---|---|---|
| `S0_BASELINE` | 0.30 | 否 | 沿用 v1 常量（parity 钉） |
| `S0_FLOOR` | 0.30 | 是 | S0 归一化下限；校准门：无信号帧 `P(S0) ≥ 0.85` |
| `W_E` | 0.60 | 是 | E 中性带半宽（0.60 ≈ 一次"中性偏空"的 E） |
| `W_T` | 0.80 | 是 | T 中性带半宽（etf 的 S2 用） |
| `W_F` | 1.00 | 是 | F 中性带半宽（仅 S0 neutrality） |
| `SEVERE_LO` | 0.25 | 是 | 证伪死区下界（`|x| ≤ 0.25` 不计证伪） |
| `SEVERE_HI` | 0.60 | 是 | 证伪饱和上界 |
| `BREAK_LAMBDA` | 2.0 | 是 | 证伪通路权重；校准门：关键格 S5 ≥ 90% 且"仅 F 崩"帧 S5 ≤ 0.2 |
| `TEMPERATURE` | 0.22 | 是 | 归一化证据上的 softmax 温度；校准门：完美多空 `P_top` 差 ≤ 0.10 |
| `UNCERTAIN_P_TOP` | 0.55 | 是 | `uncertain` 判定阈值；校准门：真实语料 uncertain ∈ [20%,40%] |

参数集中在 `classifier_v2.py` 顶部常量区（沿用 v1/v1-TEF 风格），**不暴露给 config**（沿用 `§6.3 结构性示意阈值应回测` 纪律）；`TEMPERATURE` 与 `UNCERTAIN_P_TOP` 的最终值由 §7.3 校准冻结，并由 §8.1 的聚合区间测试钉住。

### 4.7 数学性质（实现必须满足，测试钉住）

1. **单调性（D1 修复的形式证明）**：对 `F` 或 `E` 沿负方向移动，`S0/S1/S2/S3` 证据不增、`S4/S5` 不降。
   逐项看：`S0` 依赖 `|f|,|e|` 的 `neutral_band`（单减）；`S1` 含 `ne` 与 `(1−sf)`（单减），`f_up=0`；`S2_tef` 含 `ne`（单减）；`S3` 含 `min(·,0)=0`（恒定）；`S5` 含 `e_dn`/`se`/`sf`（单增）；`S4` 含 `not_yet`（E≤0 时恒 1）。∎
2. **有界**：`norm ∈ [0,1]`；`P ∈ (0,1)`；`H_norm ∈ [0,1]`。
3. **可达**：六状态各有输入使 `norm=1.0` 且 `P_top ≥ 0.65`（§8.1 I2）。
4. **证伪路径与顺序解耦**：`S5` 的第二项不含 `t`，故 `T` 的符号不改变其可达性（D2 修复的形式证明）。

---

## 5. 分类层

### 5.1 模块与签名

**新增** `alphabee/midterm/classifier_v2.py`（自包含；只 import `alphabee.midterm.models`；不 import `classifier` / `classifier_tef` —— 沿用 TEF 设计 §2 原则 1 的隔离纪律）。

```python
"""Layer 2a' 分类器 V2：修正状态证据的形式缺陷（design MIDTERM_STATE_EVIDENCE_V2_DESIGN）。

与 v1 的差异（顺序哲学不变）：
- S1/S2 用双侧 neutral_band 取代单边 _not_up（修 D1）；
- S5 = max(价格路径, F×E 双击证伪路径)，退出不再被 t_down 独占（修 D2）；
- 证据按状态上限归一化后 softmax，熵可跨状态比较（修 D3）；
- uncertain 由 P_top 判定 + 结构化 AdverseDivergence（修 D4）。

治理：本模块是唯一演进分支；v1（classifier.py / classifier_tef.py）冻结。
"""

FORMULA_REVISION = "v2"
EVIDENCE_CAPS: dict[str, float] = {...}
DEFAULT_PARAMS = EvidenceParams(...)   # 见 §4.6

@dataclass(frozen=True)
class EvidenceParams:
    s0_baseline: float = 0.30
    s0_floor: float = 0.30
    w_e: float = 0.60
    w_t: float = 0.80
    w_f: float = 1.00
    severe_lo: float = 0.25
    severe_hi: float = 0.60
    break_lambda: float = 2.0
    temperature: float = 0.22
    uncertain_p_top: float = 0.55

def state_evidence(scores: VariableScores, *, order: str, params: EvidenceParams = DEFAULT_PARAMS) -> dict[str, float]
def normalized_evidence(evidence: dict[str, float]) -> dict[str, float]
def distribution(norm: dict[str, float], *, params: EvidenceParams = ...) -> dict[str, float]
def entropy_nats(dist: dict[str, float]) -> float
def classify_state(scores: VariableScores, *, order: str, previous: StateBelief | None = None,
                   params: EvidenceParams = DEFAULT_PARAMS) -> ClassifierResult
```

`order` 取值 `"etf"|"tef"`（非法值由 dispatch 已 fail-open，模块内**不重复兜底**，`raise ValueError` 便于测试）。

### 5.2 背离分类学（含新增 adverse 格）

优先级从高到低（命中即返回）：

| 优先级 | 条件 | `Consistency` | `divergence_reason` |
|---|---|---|---|
| 1 | `sf>0 ∧ se>0 ∧ t_up>0`（**新增**：价格仍强但 F/E 同时实质恶化） | `divergent` | `基本面与预期同时实质恶化而价格仍强（顶背离）` |
| 2 | `f_up ∧ ¬e_up ∧ (e_dn∨t_dn) ∧ ¬t_up` | `divergent` | `业绩好但股价不涨：F 强而 E/T 弱`（沿用） |
| 3 | 顺序型：`tef` → `t_up∧e_dn`；`etf` → `e_up∧t_dn` | `divergent` | `价格先行转强 vs 预期未跟上` / `业绩上修 vs 股价相对走弱` |
| 4 | `f_up∧e_up∧t_up` | `resonant` | — |
| 5 | 其余 | `independent` | — |

**为什么优先级 1 最高**：它是否定"加仓"的证据强度最大（`S5=0.923` 帧也是它），必须在报告与 `next_evidence_to_watch` 里排在最前。**为什么不做成新枚举值**：`Consistency` 是三值枚举，新增值会让所有 `== resonant/divergent` 的下游分支静默走 else；结构性信息放在独立字段（§5.4）。

> 注：`sf=severe(f)`、`se=severe(e)` 为 §4.2 的负侧严重度；`sf>0` 即「F 已跌破 `SEVERE_LO` 死区」而不仅是「F<0」。该处用严重度而非符号，是为了不把「单季轻微负增长」判成顶背离。

### 5.3 `uncertain` 的新定义（D4-A）

```python
P_top = max(P.values())
uncertain = (P_top < params.uncertain_p_top) or (consistency == "divergent" and adverse is None)
```

- "无状态主导" → 不确定（触发 `research-uncertain`）；
- "有背离但**不**是顶背离"（价格先行 / 业绩好股价不涨）→ 不确定（触发 `research-divergent`）；
- **顶背离不算"不确定"，而是"确定的坏消息"** —— 它应产出 `research-adverse`（高优先级）并允许减仓，不应因"不确定"而被压成观望。这是 D3 跨层耦合（不确定 ⇒ 不能清仓）的正确解法：把"不确定"限定为语义上的不可判，而不是熵的高低。

### 5.4 结果载体（additive）

`ClassifierResult`（[models.py:798](alphabee/midterm/models.py#L798)）新增五字段（全部 additive；v1 也填 `factor_consistency` / `divergence_reason`，因为 v1 内部本来就算出了它们、只是丢掉了）：

```python
factor_consistency: str = ""                          # Consistency 值；v1 也填
divergence_reason: str = ""                           # 具名背离文案；v1 也填
entropy_normalized: float | None = None               # H/ln6；v1 为 None
adverse_divergence: AdverseDivergence | None = None   # v1 为 None
state_evidence: dict[str, float] = field(default_factory=dict)  # 归一化证据；v1 为空
```

**为什么 `factor_consistency` / `divergence_reason` 由 classifier 出而不是 decision_model 重算**：一致性是分类器的判决（`_triad_consistency`），在 `decision_model` 里重算等于两份真相（`alphabee-schema-steward`：单一来源）。v1 填这两个字段只是把已有中间结果透出，**不改变任何数值行为**。

`research_tasks` 新增 `research-adverse`（id 固定），`unknown` 为背离描述；既有 `research-divergent` / `research-uncertain` 的 id 与结构**不变**（下游 `next_evidence_to_watch` 契约不变）。

`_transition` / `_drift` / `_active_factors` 与 v1 逐值同构（迁移合法性集合、`_ACTIVE_T`、`_DRIFT_EPS` 复用同值常量，由 parity 测试钉）。

---

## 6. 消费层（契约传播）

### 6.1 契约变化（old shape → new shape）

```yaml
contract_change:
  producer:
    - alphabee/midterm/classifier_v2.py          (新增)
    - alphabee/midterm/decision_model.py:328     (evaluate：formula 路由 + 字段写入)
  old_shape:
    CompanyStateArtifact: { ..., classifier_order: str }
    ClassifierResult:     { state, transition, factor_deltas, uncertain, research_tasks, rationale }
  new_shape:
    CompanyStateArtifact: { ..., classifier_order: str, classifier_formula: str,
                            entropy_normalized: float|None, uncertain: bool,
                            factor_consistency: str, divergence_reason: str,
                            adverse_divergence: AdverseDivergence|None,
                            state_evidence: dict[str,float], factor_direction: float|None }
    ClassifierResult:     { ..., factor_consistency: str, divergence_reason: str,
                            entropy_normalized: float|None, adverse_divergence: AdverseDivergence|None,
                            state_evidence: dict[str,float] }
  changed_fields:
    - { name: classifier_formula,   change: add, default: "v1" }
    - { name: entropy_normalized,   change: add, default: null }
    - { name: uncertain,            change: add, default: false }
    - { name: factor_consistency,   change: add, default: "" }
    - { name: divergence_reason,    change: add, default: "" }
    - { name: adverse_divergence,   change: add, default: null }
    - { name: state_evidence,       change: add, default: {} }
    - { name: factor_direction,     change: add, default: null }
  breaking: false    # 全部 additive + 默认值；旧帧 load 后回落默认（沿用 TEF §6.1 先例）
```

### 6.2 `models.py`

新增模型（放在 `ClassifierResult` 之前）：

```python
class AdverseDivergence(BaseModel):
    """顶背离（结构性反证）：价格仍强，但基本面与一致预期同时实质恶化。

    与 `Consistency.DIVERGENT` 互补：后者描述"因子不同向"，本模型描述
    "不同向的方向是不利的"，并提供可排序的严重度，供报告 / 研究任务 / 记录消费。
    """
    kind: str = "fundamental_adverse_vs_price_strength"
    severity: float = 0.0            # ∈[0,1] = fundamental_severity × expectation_severity × price_strength
    fundamental_severity: float = 0.0
    expectation_severity: float = 0.0
    price_strength: float = 0.0
    description: str = ""
```

`CompanyStateArtifact` 追加 §6.1 的 8 个字段（带默认值，同一行内加注释说明口径）。

### 6.3 `decision_model.evaluate`（[decision_model.py:328](alphabee/midterm/decision_model.py#L328)）

1. 签名追加 `classifier_formula: str | None = None`（keyword-only，紧跟现有 `classifier_order`）。
2. `resolved_formula = resolve_formula_revision(classifier_formula)`；`cls = classify_state(scores, order=resolved_order, formula=resolved_formula)`。
3. 写入 artifact：`classifier_formula` / `entropy_normalized` / `uncertain` / `factor_consistency` / `divergence_reason` / `adverse_divergence` / `state_evidence` / `factor_direction`。
   - 除 `classifier_formula` / `factor_direction` 外，其余六项都直接从 `ClassifierResult` 取（§5.4 已补齐），`decision_model` **不重算**一致性/背离，避免两份真相。
   - `uncertain` 直接取 `cls.uncertain`：**v1 与 v2 都有该字段**，只是判据不同（v1 = 熵/背离；v2 = `P_top`/背离，§5.3）；v1 的取值与今天逐值相同。
   - `state_evidence` 仅 v2 非空（归一化证据），v1 为 `{}`。
4. `factor_direction`：新增 `_factor_direction(scores, *, formula)`：
   - `v1`：现状六因子均值（**逐字不变**）。
   - `v2`：`0.7 × mean(F,E) + 0.3 × mean(F,E,T)`，缺失因子按可用子集求均值（全缺 ⇒ `None`）。
     **理由**：`scenario_probability` 算的是 **thesis 的情景概率**，其驱动变量是 F/E；T 是时序修正；V/C/R 是赔率/拥挤/风险，不是 thesis 驱动。v1 的等权均值让 `T=+1` 与 `V=+0.60` 把 `F=−1`、`E=−0.52` 抵消成 `−0.032`（实测），这是类别错误。
   - 只在 `formula=="v2"` 时使用 v2 口径，v1 路径零变化。
5. `position` 调用**不改**（§6.5）。

### 6.4 `classifier_dispatch.py`（[classifier_dispatch.py:24](alphabee/midterm/classifier_dispatch.py#L24)）

```python
FORMULA_V1 = "v1"; FORMULA_V2 = "v2"; KNOWN_FORMULAS = (FORMULA_V1, FORMULA_V2)

def resolve_formula_revision(formula: str | None) -> str:
    """显式合法值优先；None → settings.midterm.formula_revision；异常/非法 → warn + "v1"。"""

def classify_state(scores, *, previous=None, order=None, formula=None) -> ClassifierResult:
    resolved_order = resolve_classifier_order(order)
    if resolve_formula_revision(formula) == FORMULA_V2:
        from alphabee.midterm import classifier_v2
        return classifier_v2.classify_state(scores, order=resolved_order, previous=previous)
    # v1 路径：路由到原模块，数值逐值一致（模块属性路由，monkeypatch 可证伪）
    ...
```

**fail-open 纪律沿用**：配置在调用时读取、异常不抛、非法值 warn + 回落 `v1`。

**导出面**：`midterm/__init__.py` 追加 `resolve_formula_revision` / `FORMULA_V1` / `FORMULA_V2`（与现有 `resolve_classifier_order` 同区，[:183](alphabee/midterm/__init__.py#L183)），保证 `decision_model` 只 import dispatch 一处。

### 6.5 `position.py` —— 不改公式（附证据）

v2 的归一化同时修复了 D3-B 的跨层耦合：熵变成可比量后，现有阈值 `_ENTROPY_UNCERTAIN=1.0`（nats）恢复判别力。实测：

| 帧 | v2 熵 | `entropy ≤ 1.0` | 硬清零分支 | 判定是否合理 |
|---|---|---|---|---|
| `600760`（S5=0.795） | **0.800** | 是 | 允许（`raEV<0` 时压 0） | ✅ 应允许 |
| 困境反转候选（P_top=0.365） | **1.540** | 否 | 只减仓 ×0.3 | ✅ 不该清仓 |
| 完美多头 | 0.597 | 是 | 允许 | ✅ |
| 完美空头 | 0.726 | 是 | 允许 | ✅ |

`v1` 同帧为 `1.433 > 1.0` ⇒ 清仓被阻塞（这正是现状缺陷的可观测后果之一）。故 **`position.py` 一行不改**，用 §8.5 的验收帧把这条判别力钉住；若真实语料校准发现错配，再走 §11.2 R3（新增可选 `entropy_threshold` 参数，默认 None = 现状）。

### 6.6 `midterm_reporter.py`

1. `_render_state`（[:221](alphabee/orchestrator/nodes/midterm_reporter.py#L221)）扩写：

```text
── 认知状态（软状态分布） ──
  argmax: S5    熵: 0.800 nats（归一化 0.447）    状态确信度: 0.795
  分布: S0=0.097 S1=0.025 S2=0.034 S3=0.025 S4=0.025 S5=0.795
  归一化证据: S0=0.30 S1=0.00 S2=0.07 S3=0.00 S4=0.00 S5=0.76   ← 仅 v2（state_evidence）
  口径: formula=v2 / order=tef；P 为归一化证据的 softmax 权重，非频率概率
  一致性: divergent（基本面与预期同时实质恶化而价格仍强（顶背离））
  不确定: False（P_top=0.795 ≥ 0.55）
  漂移: ...
```

2. 新增 `_render_adverse_divergence`（插在 `_render_state` 之后）：仅当 `adverse_divergence is not None` 输出，含 `severity` 与三分解、以及"不作为加仓证据"的显式提示。
3. `_render_variable_scores`（[:265](alphabee/orchestrator/nodes/midterm_reporter.py#L265)）追加一行：

```text
  情景方向 factor_direction: -0.583（v2 = 0.7×mean(F,E) + 0.3×mean(F,E,T)；v1 为六因子均值）
```

4. `_render_meta`（[:397](alphabee/orchestrator/nodes/midterm_reporter.py#L397)）追加 `formula=...`。
5. **v1 帧渲染保持现状**：`formula="v1"` 或 `state_evidence` 为空时，不输出 v2 专属行（golden 测试钉）。

### 6.7 `apps/cli/renderer.py`（[:592](alphabee/apps/cli/renderer.py#L592)）

`resolve_midterm_decision` 分支追加 `formula` 与 `不确定`/`顶背离` 标签（只读渲染，不新增计算）。

### 6.8 `tracking/scheduler.py`

- `TrackingReport` 追加 `classifier_formula: str = ""`、`classifier_formula_mismatch: bool = False`（[scheduler.py:171](alphabee/tracking/scheduler.py#L171) 同区）。
- `_reconcile_frame` 的 mismatch 判定同构扩展（`classifier_order` 与 `classifier_formula` 各判一次；只标注、不阻断）。
- `reconcile(...)` / `run_once(...)` 透传 `classifier_formula`。

### 6.9 `task_records`

- `RunRecord`（[models.py:106](alphabee/task_records/models.py#L106) 区）追加：`midterm_formula: str = ""`、`midterm_uncertain: bool = False`、`midterm_adverse_severity: float = 0.0`。
- `recorder.py`（[:99](alphabee/task_records/recorder.py#L99) 区）从 artifact 取值填充。
- `apps/cli/task_report.py`（[:85](alphabee/apps/cli/task_report.py#L85)）渲染为 `中期决策: 状态 S5 · formula v2 · 不确定 否 · 顶背离 0.76`。

### 6.10 明确不消费 / 不修改

| 消费者 | 判定 | 理由 |
|---|---|---|
| `orchestrator/services/payload_builders.py` | 不改 | 当前不消费 midterm（grep 无引用）；总结只进 artifacts/日志 |
| `orchestrator/contracts.py::coerce_midterm_decision` | 不改 | `model_validate` 自动接受新字段；旧 dict 回落默认 |
| 报告正文（`generate_report`） | 不改 | `MIDTERM_DECISION_SUMMARY` 不进报告（现有设计） |
| `midterm/diff.py` | 不改 | 只消费 `StateBelief`/`classifier_order`；新字段不进 diff 语义 |
| `midterm/exit_conditions.py` / `evidence_*` / `score_engine` / `bayes` | 不改 | 与本次问题域正交 |
| `orchestrator/agent.py` / `node_contracts.py` / `NODE_ORDER` | 不改 | 无新节点、无新 artifact 类型 |

**dead-end 检查**：`classifier_formula` / `adverse_divergence` / `uncertain` / `entropy_normalized` 均至少有一个真实消费者（reporter + task_records + scheduler）；`state_evidence` 由 reporter 元信息行消费；`factor_direction` 由 reporter 消费。无 dead-end。

### 6.11 序列化与版本

- `schema_version` 保持 `"1"`（additive 字段 + 默认值；`model_validate` 对旧 JSONL 自动回落，沿用 TEF 设计 §6.1）。
- JSONL 落盘（`midterm/persistence.py`）无需改动；旧帧重新落盘时自动带新字段。
- 跨变体混帧由 `classifier_formula` 自证，`TrackingReport.classifier_formula_mismatch` 兜底筛选。

---

## 7. 配置、切换与校准

### 7.1 config

```yaml
midterm:
  classifier_order: tef          # 现状，不变
  formula_revision: v1           # 新增；v1 = 现状（缺省），v2 = 新公式
```

`alphabee/config/__init__.py::MidtermClassifierSettings`（[:349](alphabee/config/__init__.py#L349) 区）追加 `formula_revision: Literal["v1","v2"] = "v1"`。**缺省 v1 ⇒ 所有旧 config 行为不变。**

### 7.2 显式参数与 CLI

- `evaluate(..., classifier_formula=...)` / `get_decision(..., classifier_formula=...)`（透传）。
- tracking CLI 追加 `--formula {v1,v2}`（与现有 `--classifier` 同处，[scheduler.py:868](alphabee/tracking/scheduler.py#L868)；缺省 None → 跟随配置）。
- `resolve_*` 双方都 fail-open；显式值优先于配置（与 `classifier_order` 完全同构）。

### 7.3 校准规程（唯一允许调参的通道）

**语料**：`data/midterm/state/*.jsonl` 与各 `--state-dir` 目录下的真实帧，抽取 `variable_scores` + `classifier_order`，离线重算 v2 分布（不写盘）。

**步骤（非循环：先定形状，再定阈值）**：
1. 固定 `EVIDENCE_CAPS`（结构性，不调）与 `SEVERE_LO/HI`、`BREAK_LAMBDA`。
2. **定 `TEMPERATURE`**（只由形状约束决定，不看 `uncertain` 率）：
   - (a) 对称性：完美多头 `P_top` − 完美空头 `P_top` ≤ 0.10；
   - (b) 可判别性：`600760` 实帧 `P_top ≥ 0.75`；
   - (c) 分散度：真实语料 `H_norm` 中位数 ≤ 0.75。
   在 `{0.18,0.20,0.22,0.24,0.26}` 中取满足 (a)(b)(c) 且 (b) 裕度最大者。实测 `0.22` 满足：差 0.077、`600760 P_top=0.795`。
3. **定 `UNCERTAIN_P_TOP`**：取真实语料 `P_top` 分布的 (1−目标率) 分位数，目标率 30%（区间 [20%,40%]）。**这一步在 `TEMPERATURE` 冻结之后做**，否则两者互相抵消、任何组合都能凑出目标率。
4. 复核 `BREAK_LAMBDA`：关键格 S5 ≥ 90%，且「仅 F 崩（E≥−0.2）」帧 S5 ≤ 0.2（两约束夹出区间，取区间中点）。
5. 冻结参数写入模块常量；把上述聚合约束写成测试（§8.1 I5），容差 ±0.05。

**纪律**：网格枚举只用于不变量与可达性，**不用于调参**（均匀网格不代表真实分布，§1.2 的 90.8% 已说明这一点）。**语料不足时的降级**：真实帧 < 50 帧时，允许只跑步骤 2/4 的形状约束，`UNCERTAIN_P_TOP` 保留初值 0.55 并在 ROADMAP 登记"待校准"，不得用网格凑数。

### 7.4 A/B 实验协议（承接 TEF 设计 §9.3）

同 `as_of` 回放四个配置：

```bash
for f in v1 v2; do for o in etf tef; do
  poetry run python -m alphabee.tracking --symbol 600760.SH --once \
    --classifier $o --formula $f --state-dir data/midterm/ab_${f}_${o}
done; done
```

观测项（在 TEF §9.3 四项之外新增）：

| 编号 | 指标 | 通过口径 |
|---|---|---|
| A1 | **可达性**：`T>0` 帧中出现过 S5 的帧数占比 | v2 ≥ 5%，v1-tef = 0% |
| A2 | **关键格反转率**：`F≤−0.5 ∧ E≤−0.3 ∧ T≥0.3` 帧被判到 `{S4,S5}` 的比例 | v2 ≥ 90% |
| A3 | **假退出率**：v2 判 S5 后 20 交易日内未继续下跌（事件研究） | 不显著高于 v1-etf |
| A4 | **退出时滞**：真实破位事件日的帧差 | v2 ≤ v1-etf |
| A5 | `uncertain` 率 / 状态分布 / 熵分布 | 落在 §7.3 区间 |
| A6 | 单调性违例数 | 0 |

结论口径：以事件研究为准，不以单一分数为准（沿用 TEF §9.3 第 4 条）。

---

## 8. 测试矩阵

### 8.1 不变量（`tests/midterm/test_classifier_v2.py`）

| 编号 | 测试 | 断言 |
|---|---|---|
| **I1** | `test_adverse_monotonicity` | 扫 `F,E ∈ {−1,−0.95,…,0}` × 5 组 `(t,v,c)`；多头证据不增、空头不降 |
| **I2** | `test_reachability_all_six_states` | 六状态各有输入 `P_top ≥ 0.65`；`F=E=−1,T=+1 ⇒ S5`（D2 反例） |
| **I3** | `test_magnitude_sensitivity` | `F=−1` vs `F=−0.5`：`max|ΔP| ≥ 0.02`（专杀"幅度不敏感"回归） |
| **I3b** | `test_critical_cell_not_inverted` | 网格 1000 组：看空侧质量 ≥ 0.5 的比例 ≥ 0.90 |
| **I4** | `test_caps_are_achievable` | 每状态存在输入使 `norm[s] == 1.0`；`S5.cap == max(1.5, BREAK_LAMBDA)` |
| **I5** | `test_calibration_aggregates` | 完美多空 `P_top` 差 ≤ 0.10；无信号帧 `P(S0) ≥ 0.85`；`H_norm ∈ [0,1]` |
| **I6** | `test_turnaround_vs_falsification` | 三行：仅 F 崩 ⇒ S5 ≤ 0.2；F+E 双击 ⇒ S5 ≥ 0.7；无信号 ⇒ S0 |
| **I7** | `test_adverse_divergence_object` | 600760 帧 `severity ≈ 0.76`，`kind`/三分量正确；非顶背离帧为 None |
| **I8** | `test_uncertain_semantics` | `P_top<0.55 ⇒ uncertain`；顶背离 ⇒ **not** uncertain；价格先行背离 ⇒ uncertain |
| **I9** | `test_no_zero_evidence_uniform` | 任意网格点 `max(evidence) > 0`（防全零 ⇒ 假 1/6 均分） |

### 8.2 变异杀伤（`tests/midterm/test_classifier_v2_parity.py`）

每个变异必须让测试变红（沿用 v1 parity 的"变异杀伤"风格）：

| 变异 | 必红测试 |
|---|---|
| S2 的 `neutral_band` 换回 `not_up` | I1、I3 |
| 删掉 S5 的证伪路径 | I2、I3b |
| 去掉归一化（直接 softmax 原始证据） | I4、I5 |
| `S0` 去掉底噪 | I9 |
| `factor_direction` v2 换回六因子均值 | 场景方向测试（§8.4） |

### 8.3 零回归（关键）

- `tests/midterm/test_classifier_parity.py` **原样全绿**（v1 模块未改）。
- 新增 `test_v1_artifact_golden`（两段式，避免"additive 字段"与"逐字节相等"自相矛盾）：
  1. **数值零回归**：固定输入 + `formula=None` ⇒ `evaluate()` 产出的 JSON **剔除 8 个新增字段后**与改动前冻结的 golden 逐字节相等；
  2. **新增字段定值**：`classifier_formula=="v1"`、`entropy_normalized is None`、`adverse_divergence is None`、`state_evidence=={}`、`factor_direction` 等于改动前的六因子均值；`uncertain` / `factor_consistency` / `divergence_reason` 等于 v1 分类器内部已算出的值（这三个字段 v1 也填，故对 v1 用户也是可观测性提升，但**不改变任何数值**）。
- 新增 `test_dispatch_v1_routes_to_legacy`：monkeypatch 两个旧模块的 `classify_state`，断言 v1 路径确实调用它们（证伪"偷偷走了 v2"）。

### 8.4 接线（producer → consumer）

| 测试文件 | 断言 |
|---|---|
| `tests/midterm/test_decision_model.py` | artifact 8 个新字段写入正确；v2 时 `factor_direction` 为 v2 口径 |
| `tests/orchestrator/test_midterm_reporter.py` | v2 帧出现「归一化熵 / 一致性 / 不确定 / 对立证据」；v1 帧**不出现**；adverse 缺失时无该节 |
| `tests/tracking/test_status.py` | `classifier_formula_mismatch` 标注；旧帧回落 `"v1"` |
| `tests/task_records/` | 三个新字段落库 + 渲染 |
| `tests/apps/cli/`（若有 renderer 测试） | formula / 顶背离标签 |

### 8.5 验收帧清单（固定输入，写进测试）

| 帧 | 输入 `(F,E,T,V,C)` | 期望 |
|---|---|---|
| **600760 实帧** | `(−1, −0.5169, +1, +0.6015, −0.1902)` | `S5`，`P=0.795`，`bear_mass=0.819`，`H=0.800`，`adverse.severity≈0.76` |
| 完美多头 | `(1,1,1,−1,1)` | `S3`，`P=0.850` |
| 完美空头 | `(−1,−1,−1,−1,1)` | `S5`，`P=0.773`，`bear_mass=0.943` |
| 无信号 | `(0.05,−0.05,0.1,0,0)` | `S0`，`P=0.892` |
| 困境反转候选 | `(−1,−0.2,+1,+0.3,0)` | `S2` 为主但 `P_top=0.365 ⇒ uncertain`，`S5≤0.2` |
| 价格强 + 双击 | `(−1,−1,+1,0,0)` | `S5`，`P=0.923`（D2 核心反例：`T=+1` 仍可退出） |

---

## 9. 验收标准

1. **不变量全绿**：§8.1 I1–I9 + §8.2 五个变异全部变红。
2. **零回归**：§8.3 三条全绿；`poetry run pytest tests/midterm tests/orchestrator/test_midterm_reporter.py tests/tracking tests/task_records` 全绿。
3. **验收帧**：§8.5 六行的 `P` / `bear_mass` / `H` 落在 ±0.02 内。
4. **关键格**：网格 1000 组，看空侧 ≥ 90%（实测 v2：`S5=100%`）。
5. **可达性**：六状态全可达（每状态 `P_top ≥ 0.65`）；`T=+1` 且 `F=E=−1` ⇒ `S5`。
6. **契约完整**：§6 的 8 个 artifact 字段各有 ≥1 真实消费者；`coerce_midterm_decision` 能还原旧帧与新帧。
7. **校准**：§7.3 四个聚合区间通过；参数冻结。
8. **文档**：§12 清单完成；ROADMAP 变更登记。

---

## 10. 实施切分与回滚

| PR | 内容 | 行为变化 | 回滚 |
|---|---|---|---|
| **PR-1** | `classifier_v2.py` + §8.1/8.2 测试 | **无**（未接线） | revert |
| **PR-2** | `models.py` 字段 + `dispatch` formula 路由 + `decision_model` 写入 + `config` 段（缺省 v1）+ §8.3 golden | **无**（默认 v1） | `formula_revision: v1` |
| **PR-3** | 消费面：reporter / CLI / scheduler / task_records + §8.4 | 无（v1 帧不新增行） | 同上 |
| **PR-4** | §7.3 校准 + 参数冻结 + §8.1 I5 聚合测试 | 无（仍 v1） | 同上 |
| **PR-5** | A/B（§7.4）+ 文档 + ROADMAP + 默认值切换评估 | **切默认前必须过 §9 全量** | 一行配置 |

**回滚语义**：`formula_revision: v1` ⇒ 路由回 v1 模块，artifact 只多几个默认值字段，报告/仓位/跟踪与今天一致（golden 钉住）。

---

## 11. 风险与替代方案

### 11.1 已评估并拒绝

| 替代 | 拒绝理由 |
|---|---|
| 只调 `TEMPERATURE` / 阈值 | 修不了 D1（单调性）与 D2（可达性）：S5 的证据恒为 0，温度再怎么调也是 0 |
| 把 `_not_up` **全局**改成双侧 band（含 S4 的"停上修"） | S4 语义反转（E 崩时 S4 反而降），会把"充分定价"和"证伪"搅在一起 |
| 改为加法式 `S5 = t_dn + e_dn + 0.5f_dn` | TEF 设计 §3.5 已拒（两变体不可辨）；v2 用 `max(价格路径, 证伪路径)` 同时保可达与可辨 |
| 直接改 v1 模块 | 破坏 A/B 基线与历史帧可复现性；零回归不可证 |
| 给 `S2` 加"F 恶化硬否决" | 会杀死"困境反转"（F 崩 + 预期未砍 + 价格强正是反转早期）；正确做法是**区分**"F 崩"与"F/E 双击"（§4.3 双路径） |
| 新增 `Consistency` 枚举值表示顶背离 | 三值枚举的下游 `==` 分支会静默走 else；改为独立字段（§5.2） |
| 把 `adverse` 塞进 `research_tasks` 就完事 | D4 的教训正是"内部有信号、外部看不见"；必须进 artifact + 报告 |
| 每标的自动选序 / 自动选 formula | 沿用 TEF §1.2 非目标；先积累 A/B 数据 |

### 11.2 风险登记

| 编号 | 风险 | 缓解 |
|---|---|---|
| **R1** | 读者把 softmax 权重当概率 | 报告/文档三处显式声明（§4.5、§6.6）；`P_top` 命名"状态确信度"保留但附口径 |
| **R2** | S5 更易触发 ⇒ 假退出上升 | `SEVERE_LO` 死区 + `BREAK_LAMBDA` 校准门 + A/B 的 A3/A4 事件研究 |
| **R3** | v2 熵与 `_ENTROPY_UNCERTAIN=1.0` 错配 | §6.5 用四帧预验；若真实语料不符，PR-4 追加可选 `build_position(..., entropy_threshold=None)`（默认 None = 现状），不改默认 |
| **R4** | v1/v2 双实现漂移 | 常量与纯函数由 `test_classifier_v2_parity.py` 钉同值；v1 冻结写入 docstring；禁止在 v1 加功能 |
| **R5** | artifact 字段膨胀 | 8 个字段全部可选且各有一消费者；`state_evidence` 仅 v2 非空 |
| **R6** | 历史帧混比污染 A/B | `classifier_formula` provenance + `classifier_formula_mismatch` 标注 |
| **R7** | 参数在网格上过拟合 | §7.3 纪律：网格只做不变量，调参只在真实语料 |

---

## 12. 文档与 ROADMAP 登记

| 文件 | 动作 |
|---|---|
| `docs/implementation/midterm/MIDTERM_STATE_EVIDENCE_V2_DESIGN.md` | 本文件（新增） |
| `docs/midterm/MIDTERM_DECISION_MODEL.md` | §4.2 补 v2 公式表交叉引用；§4.4 补 adverse 格 |
| `docs/implementation/midterm/MIDTERM_CLASSIFIER_TEF_DESIGN.md` | 顶部加"§3.2 公式已由 V2 修订，见 V2 设计"的指针（**不改原文**，保留历史设计） |
| `docs/roadmap/ROADMAP.md` | 变更登记：新增 `classifier_formula` / `AdverseDivergence` / `formula_revision` 三项；记录 `_not_up` 单边缺陷与 S5 不可达的修复 |
| `config.yaml.example` | `midterm` 段补 `formula_revision: v1` 与注释 |

---

## 附录 A 参数速查

```python
S0_BASELINE      = 0.30      # 沿用 v1
S0_FLOOR         = 0.30
W_E              = 0.60
W_T              = 0.80
W_F              = 1.00
SEVERE_LO        = 0.25
SEVERE_HI        = 0.60
BREAK_LAMBDA     = 2.0
TEMPERATURE      = 0.22
UNCERTAIN_P_TOP  = 0.55
EVIDENCE_CAPS    = {"S0":0.30,"S1":2.0,"S2":2.0,"S3":3.0,"S4":3.0,"S5":2.0}
```

## 附录 B 复算脚本（验收用，不改仓库状态）

```bash
poetry run python - <<'PY'
from alphabee.midterm.models import VariableScores
from alphabee.midterm import classifier_v2 as v2

s = VariableScores(f_fundamental_trend=-1.0, e_revision=-0.5169, t_relative_strength=1.0,
                   v_valuation_percentile=0.6015, c_crowding=-0.1902, r_risk=-0.08741)
r = v2.classify_state(s, order="tef")
print({k: round(x, 4) for k, x in r.state.distribution.items()})
print("argmax", r.state.argmax_state, "H", round(r.state.entropy, 3),
      "Hn", round(r.entropy_normalized, 3), "uncertain", r.uncertain)
print("adverse", r.adverse_divergence, "reason", r.divergence_reason)
PY
# 期望：S5≈0.795，H≈0.800，Hn≈0.447，uncertain=False，
#       adverse.severity≈0.76，reason=基本面与预期同时实质恶化而价格仍强（顶背离）
```

## 附录 C 术语

| 词 | 含义 |
|---|---|
| 归一化证据 `norm[s]` | `evidence[s] / cap[s]`，∈[0,1]，状态间可比 |
| 状态确信度 `P_top` | `max(P)`，argmax 状态的分布权重，**不是**后验 `P(H|E)`、**不是**频率概率 |
| 看空质量 `bear_mass` | `P(S4)+P(S5)`，用于报告与人工判读 |
| 双击证伪 | F（已实现基本面）与 E（前瞻预期）同时实质恶化（`|·| ≥ SEVERE_LO`），与价格方向无关 |
| 顶背离 | 价格仍强（`T>0`）而 F/E 同时实质恶化；`AdverseDivergence.severity = sf×se×t_up` |
