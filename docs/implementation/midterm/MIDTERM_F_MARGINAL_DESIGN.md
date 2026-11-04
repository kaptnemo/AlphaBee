# 中期决策层 F 因子「边际改善」建模设计（F-Marginal）

> **状态：设计文档（2026-11-01 定稿，尚未实施）**。前置：`docs/implementation/midterm/MIDTERM_STATE_EVIDENCE_V2_DESIGN.md`（状态证据 V2）、`alphabee/midterm/score_engine.py::fundamental_breakdown`（L1 可审计分解，2026-11-01 已落地）。
> **立项依据**：2026-11-01 用户帧（`600760.SH`，`F=−1`）复核：F 的三个输入（`revenue_yoy=-34.19` / `net_profit_yoy=-26.0` / `eps_growth_yoy=-28.0`）全是**报告期累计同比**（三季报 = 前三季度累计），而同一份报告里 **Q3 单季营收 +23.29%、单季净利 +137**——F 完全看不到这个拐点。叠加 `±30pp` 入口饱和与 `[-1,1]` 出口饱和，F 在恶化区退化成二值。
> **本文档只做设计与契约，不含实现**。实施切分见 §9。
> 关联 skill：`alphabee-schema-steward`（§3 派生字段治理）、`alphabee-pipeline-contract-steward`（§6 契约传播）。

---

## 0. 一页摘要

**问题**：F 量的是「过去 9 个月的平均增速（水平）」，缺的是「最近一季相对趋势的变化量（边际）」。再加一个水平项（TTM、环比）解决不了，必须引入**加速度 + 质地对偶**。

**方案**（三层）：

```text
L  = 现行三项累计 YoY 的饱和均值                          （水平，不变）
Δ_rev = q_revenue_yoy    − revenue_yoy                   （单季比累计快多少 pp）
Δ_np  = q_net_profit_yoy  − net_profit_yoy
M_raw = mean(saturate(Δ_rev, 25), saturate(Δ_np, 25))     （加速度 ∈[-1,1]）
Q     = clip(1 + gross_margin_pp / 4, 0.2, 1.0)          （质：单季毛利率同比变化 pp）
M     = M_raw × Q

F = saturate(0.7·L + 0.3·M + OCF惩罚 + beat + miss + 细分, 1)   # 有单季数据（P1）
F = saturate(L       + OCF惩罚 + beat + miss + 细分, 1)         # 无单季数据（现行，逐值不变）
```

**数据不需要新接口**：`get_financial_fact(periods=8)` 已返回 8 期 `income[]` / `fina[]`（[financial_fact.py:23](alphabee/agents/facts/tools/financial_fact.py#L23)），而 `build_fundamental_factor`（[factors.py:346](alphabee/midterm/factors.py#L346)）当前只取最新一期。单季值 = 累计差分，单季同比 = 与去年同期比。

**分两阶段**：P0 **影子模式**（算出 `M / Q / F_shadow` 写进 `score_breakdown`，F 不变）→ 影子期验收 → P1 **开权重**（`f_marginal_mode=on`）。

**同帧效果**（`600760`，单季毛利率 9.63% vs 去年约 13%）：

| 口径 | F | 状态 |
|---|---|---|
| 现行（无单季） | **−0.733** | S5 0.890，确信退出，顶背离 0.846 |
| **P1（含质量门 Q=0.20）** | **−0.393** | S5 0.453，`uncertain=True`，顶背离 0.346 |
| 反例（忽略质量门 Q=1） | −0.153 | **S0 0.229**、S5 0.146、顶背离**消失** |

第三行就是必须防的失败模式：一个低基数季度把「证伪」抹成「没信号」。

---

## 1. 背景与问题

### 1.1 现状：F 的口径

[score_engine.py:167](alphabee/midterm/score_engine.py#L167) `fundamental_breakdown`（L1 起为唯一计算入口）：

```text
growth = mean( saturate(revenue_yoy,  30),
               saturate(net_profit_yoy, 30),
               saturate(eps_growth_yoy, 30) )
raw = growth + OCF惩罚(≤0) + beat(+0.2) + miss(−0.3) + 细分(+0.2)
F   = saturate(raw, 1)
```

三个输入全部来自 `fina_indicator` 的**累计口径**字段（`or_yoy` / `netprofit_yoy` / `basic_eps_yoy`，见 [financial.yaml:584](alphabee/schemas/financial.yaml#L584)、[financial.yaml:598](alphabee/schemas/financial.yaml#L598)）。schema 已注明「三季报 = 前三季度累计同比」。

由此产生三个结构性缺陷（与 2026-11-01 复核结论一致）：

1. **滞后**：累计同比是 3 个季度（或 4 个）的加权平均，最近一季的拐点被前几季拖平；
2. **双饱和**：入口 `±30pp` 截断 + 出口 `[-1,1]` 截断，`−34%` 与 `−100%` 等价；
3. **无质地**：`gross_margin` 在快照里只展示、不参与 F；「增收不增利」不可见。

### 1.2 本帧事实（600760，2026-11-01）

| 项 | 累计（9M） | 单季（Q3） |
|---|---|---|
| 营收 YoY | −34.19% | **+23.29%** |
| 归母净利 YoY | −26.0% | **+137%** |
| 毛利率 | 10.07% | **9.63%**（同比约 −3.4pp，参考 H 的 13% 中枢） |

**量增、利增、质降**。这正是「边际改善」与「改善质量」必须分开建模的原因。

### 1.3 为什么不能靠现有手段解决

| 手段 | 为什么不行 |
|---|---|
| 调 `_FUNDAMENTAL_YOY_SCALE`（30→40） | 只改饱和点，输入仍是累计水平；本帧 F 仍会饱和 |
| 换成 TTM YoY | TTM 是 12 个月窗口的**水平**（换窗口不换性质），仍看不见 Q3 拐点 |
| 用环比 QoQ | 军工/交付型行业 Q4 集中确认，环比季节性畸高，噪声远大于信号 |
| 直接给单季大权重 | 一次低基数季度就能翻状态（§0 第三行：F=−0.153、顶背离消失） |
| 做成第七因子 | S0–S5 的 caps/温度/EV/仓位全按六因子设计，重排成本远高于收益；F 的语义本就是「边际方向」 |

---

## 2. 目标与非目标

### 2.1 目标

| 编号 | 目标 | 可测口径 |
|---|---|---|
| **G1** | F 对**单季拐点**敏感，但对**单季噪声**不敏感 | 校准门 1/3/4（§7.1） |
| **G2** | 改善必须**质地成立**（量、利、毛利率同向） | 质量门 Q；门 2 反例必红 |
| **G3** | **无单季数据 ⇒ 与现行逐值相同** | 门 5 + golden 逐字节测试 |
| **G4** | 单季数据可**从现有 8 期数据还原**，不新增外部接口 | §3.1 输入清单；离线冒烟 |
| **G5** | 可**影子运行**：先观测不生效 | `f_marginal_mode="shadow"` 下 F 不变 |
| **G6** | 全程**可审计**：原始单季值、M、Q、F_shadow 落盘 | `score_breakdown` 新键 + 报告行 |
| **G7** | 不改变 v1/v2 的**状态机语义**（只改 F 的输入） | 状态公式与 caps 零改动 |

### 2.2 非目标

| 编号 | 非目标 | 理由 |
|---|---|---|
| N1 | 不改 `score_engine` 的 E/T/V/C/R 与 OCF/beat/miss/细分 口径 | 与边际改善正交；L1 刚把它们稳定下来 |
| N2 | 不改 `IndicatorFact`/`fina_indicator` 的既有 canonical 字段语义 | 只新增派生字段，不改口径 |
| N3 | 不动状态证据公式（V2 `classifier_v2`）与 caps/温度 | F 只是它的输入之一 |
| N4 | 不做"连续 N 季改善"的持续性判定（v1 不做） | 需要历史帧序列，列为 §10 的后续项 |
| N5 | 不改 `position` / `EV` 数学 | 输入变、公式不变 |
| N6 | 不做行业/标的自适应权重 | 参数全局统一，先积累语料 |

---

## 3. 数据层：单季还原

### 3.1 输入（现成，零新接口）

`build_fundamental_factor(fin_data, segments)` 的 `fin_data` 即 `get_financial_fact(symbol)` 的返回（默认 `periods=8`）：

```text
{
  "ref_dates": ["20260930", "20260630", ...],   # 8 期，最新在前（不依赖顺序，见 3.2）
  "income": [{"period": "20260930", "revenue": ..., "net_profit_attr_p": ..., ...}, ...],
  "fina":   [{"period": "20260930", "revenue_yoy": ..., "net_profit_yoy": ...,
              "gross_margin": ..., ...}, ...],
  ...
}
```

主要用：`income[].operating_revenue`（营业收入，累计；缺失回落 `income[].revenue`=营业总收入，
并记 `marginal_revenue_basis`）、`income[].net_profit_attr_p`（归母净利，累计），
以及 `fina[].gross_margin`（累计毛利率，PERCENT）。

**期数要求**：目标期 `t`、同年前一期 `t_prev`、去年同季 `t-4`、去年同季的前一期 `t_prev-4`。
`periods=8` 对 Q1/Q2/Q3/年报全部覆盖（Q1 不需要 `t_prev`；年报 `t_prev` 为同年 9M）。

### 3.2 恒等式与边界

```text
单季值(t) = 累计(t) − 累计(t_prev)      # t_prev = 同一财年的上一报告期
单季值(t) = 累计(t)                     # t 为 Q1（0331）
单季同比  = 单季值(t) / 单季值(t-4) − 1  # t-4 = 去年同一报告期
单季毛利  = 累计营收(t)×累计毛利率(t) − 累计营收(t_prev)×累计毛利率(t_prev)
单季毛利率 = 单季毛利 / 单季营收
毛利率同比变化(pp) = 单季毛利率(t) − 单季毛利率(t-4)
```

| 边界 | 处理 |
|---|---|
| **Q1** | 单季 = 累计（`t_prev` 不存在，年份边界）；**数据层仍产出 q_\***，但评分层判定「加速度不可识别」⇒ `marginal=None`、`on` 与 `off` 逐值相同（见 §4.3 与 `MIDTERM_F_DATA_GOVERNANCE_DESIGN.md` §6） |
| **年报（1231）** | `t_prev` = 同年 0930（不是 t−1 期） |
| **跨年** | `t_prev` 必须由**报告期字符串**算（0630→0331、0930→0630、1231→0930），不能取数组下标 |
| **数组顺序** | 一律先按 `period` 降序排序再去重（Tushare 顺序不可依赖） |
| **期数不足** | 任一必需期缺失 ⇒ 对应派生字段 `None`（不插值、不用 TTM 顶替） |
| **上年同期 ≤ 0**（亏损/转盈） | 单季净利同比 `None`（百分比无意义）；改用**营收腿**；两条腿都无 ⇒ 整体降级 |
| **分母过小** | `|单季值(t-4)| < 1% × 当期**单季营收**`（P1-1 起；旧口径用累计营收）⇒ 视为基数不稳，该腿 `None` |
| **数据异常** | 单季毛利率 ∉ [−50, 100] ⇒ `None`（不静默夹取） |

### 3.3 产出字段（`FundamentalFactor` 新增 4 个，additive）

```python
q_revenue_yoy: float | None = None      # PERCENT 单季营收同比（多期还原派生）
q_net_profit_yoy: float | None = None   # PERCENT 单季归母净利同比（同上）
q_gross_margin: float | None = None     # PERCENT 单季毛利率
q_gross_margin_pp: float | None = None  # PERCENT 单季毛利率同比变化（pp，带符号）
```

命名带 `q_` 前缀，docstring 写明「**多期还原派生字段，非 canonical 单期事实**」——依据
`alphabee-schema-steward`：这是计算产物而非数据源事实，所以不进 `schemas/financial.yaml`
的 canonical 列表，也不进 `_FUNDAMENTAL_FIELDS` 缺失登记（缺失由 `score_breakdown` 的
`f_marginal_source` 表达，见 §4.4）。

### 3.4 纯函数契约（`factors.py`）

```python
_Q_PREV = {"0630": "0331", "0930": "0630", "1231": "0930"}   # 同年上一报告期

def _normalize_period(value: Any) -> str | None:
    """YYYYMMDD（或 YYYY-MM-DD）→ 'YYYYMMDD'；非法 → None。"""

def _prev_year(period: str) -> str:
    """'20260930' → '20250930'。"""

def _prev_quarter(period: str) -> str | None:
    """同年上一报告期；Q1（0331）→ None。"""

def _series(records: Any, field: str) -> dict[str, float]:
    """[{period, field}] → {period: value}；按 period 降序排序后去重（首条生效）。"""

def _single_quarter(series: Mapping[str, float], period: str) -> float | None:
    """单季值：Q1 取累计，其余取 累计(t) − 累计(t_prev)；缺任一 → None。"""

def derive_marginal(fin_data: dict[str, Any] | None) -> dict[str, float | None]:
    """产出 4 个 q_* 字段；任一不可算 → None（不插值）。纯函数、无 IO、无缓存副作用。"""
```

`build_fundamental_factor` 末尾统一赋值：

```python
marginal = derive_marginal(data)
factor = FundamentalFactor(..., **{k: marginal[k] for k in _Q_MARGINAL_FIELDS})
```

### 3.5 营收口径与近似（P1-4 后必须披露）

- **口径统一**：`derive_marginal` 优先用 canonical `operating_revenue`（Tushare `income.revenue`，
  营业收入）还原单季营收；`q_revenue_yoy` 与 `q_gross_margin` 都用这一条序列，与 L 的
  `revenue_yoy`（`or_yoy`，营业收入同比）及 `grossprofit_margin` 的分母同口径。实际口径写入
  `FundamentalFactor.marginal_revenue_basis`（`operating_revenue` / `total_revenue`）。
- **缺失回落**：无 `operating_revenue` 时才回落 canonical `revenue`（营业总收入），此时单季毛利的
  分子（毛利）与分母（营业总收入）不同源，**对营业外收入占比大的公司有偏差**（主业公司可忽略）；
  该近似（风险项 R2）**仅在回落路径存在**，并不是默认路径的固有近似。
- **精确解法（P1.5，仍为可选、不默认做）**：在 [financial_fact.py:47](alphabee/agents/facts/tools/financial_fact.py#L47) 的
  `fields` 追加 `oper_cost`，新增 canonical `operating_cost` 与 mapping，单季毛利率改为
  `(单季营业收入 − 单季营业成本)/单季营业收入`。本设计**不默认**做（多一次数据面改动，先影子复核偏差）。

---

## 4. 评分层：F 公式

### 4.1 定义与参数

```python
# score_engine.py 新增常量（结构性示意，需校准；不进 config）
F_MARGINAL_WEIGHT = 0.30       # 边际项权重（水平项 = 1 − 0.30）
F_ACCEL_SCALE = 25.0           # PERCENT 加速度饱和点（+25pp → +1）
F_MARGIN_DELTA_SCALE = 4.0     # PERCENT 毛利率变化尺度（-4pp → 质量门触底）
F_MARGIN_QUALITY_FLOOR = 0.20  # 质量门下限（不允许归零：仍有量/利证据）

def _marginal(f: FundamentalFactor) -> tuple[float, float, float] | None:
    """→ (M_raw, Q, M)；任一脚缺失 → None。"""
```

```text
Δ_rev = q_revenue_yoy    − revenue_yoy
Δ_np  = q_net_profit_yoy − net_profit_yoy
M_raw = mean( saturate(Δ_rev, F_ACCEL_SCALE), saturate(Δ_np, F_ACCEL_SCALE) )   # 可单腿
Q     = clip(1 + q_gross_margin_pp / F_MARGIN_DELTA_SCALE, FLOOR, 1.0)
M     = M_raw × Q
```

`Δ` 的口径说明：**加速度**而非「单季水平」——`q_yoy − 累计_yoy` 直接度量"最近一季比过去三季快/慢多少"，
与 L 的水平信息正交；若直接用 `q_yoy` 水平，会与 L 重复计分（一家常年 +30% 的公司会被算两次）。

### 4.2 参数选择的理由

| 参数 | 取值 | 理由 |
|---|---|---|
| `F_MARGINAL_WEIGHT` | 0.30 | 水平项抗季节性与单期噪声，必须主导；边际只做修正。上限 0.4（影子期若翻转率 > 10% 则下调） |
| `F_ACCEL_SCALE` | 25pp | 「一季比趋势快 25pp」已达到可信拐点量级；再大用饱和避免低基数放大 |
| `F_MARGIN_DELTA_SCALE` | 4pp | 毛利率同比 −4pp 对制造业已是实质恶化；−2pp 给 0.5 信用 |
| `F_MARGIN_QUALITY_FLOOR` | 0.20 | 不归零：量/利加速本身仍是信息，只是不配拿满信用 |

### 4.3 降级路径（G3 的实现）

```python
if marginal is None:                 # 无单季数据
    f_growth = growth                # 现行：L 原样
else:
    f_growth = (1 - W) * growth + W * M
score = f_growth + ocf + beat + miss + segment
F = _saturate(score, 1.0)
```

**关键性质**：`marginal is None` 时新代码路径与现行**逐值相同** ⇒
① golden 的合成输入（无多期数据）保持绿；② 次新股/数据缺失标的零风险；③ 灰度可按标的分批。

### 4.4 叠加与去重

| 项 | 规则 |
|---|---|
| OCF 惩罚（−0.5） | 不参与边际混合，直接加在 `score` 上（L1 口径不变） |
| beat（+0.2） | **与边际项去重**：仅当 `marginal_mode="on"` 且 `marginal is not None and Q < 1.0` 时 beat 折半（`beat *= 0.5`）——避免"超预期"与"加速"对同一改善双重奖励。**shadow 模式不折半**（否则 F 会变，违反 G7） |
| miss（−0.3） | 不去重（惩罚不对称，信息量更大） |
| 细分（+0.2） | 不参与（结构性维度，与时间维度正交） |

### 4.5 边界情形

| 情形 | 行为 |
|---|---|
| 亏损转盈（上年同期净利 ≤ 0） | `q_net_profit_yoy=None`；只用营收腿；两腿都无 ⇒ 降级 |
| 单季营收缺失/≤ 0 | **营收腿与净利腿均 `None`**（仅 `q_gross_margin` 可能已写入；分母无意义，两腿都不可算） |
| 年报期 | `t_prev` = 同年 0930；与 Q1 的"单季=累计"规则互斥，不得混用 |
| 数据重述 | 以最新 `ann_date` / `update_flag` 为准（`_dedup` 排序后取首条，见治理设计 §5）；重述后的历史期不回溯（本帧一次性） |
| 停牌/无新报告期 | 沿用快照 `as_of_date` 的最新报告期；不向前找更早的（避免跨期拼接） |

### 4.6 数学性质（测试钉住）

1. **有界**：`M ∈ [-1,1]`，`F ∈ [-1,1]`。
2. **连续**：`M` 对 `Δ` 连续（分段线性）；`Q` 对 `margin_pp` 连续。
3. **单调**：`Δ_rev`↑ 或 `Δ_np`↑ ⇒ `M` 不降；`margin_pp`↑ ⇒ `Q` 不降。**注意**：`F` 对 `revenue_yoy` **不再严格单调递增**——`revenue_yoy`↑ 会同时抬高 L 与压低 Δ_rev（分母/基准效应），这是"水平 vs 加速度"的固有张力，需在测试里按「固定其余项、只动单季值」的口径断言，而不是对累计值断言。
4. **降级恒等**：`marginal is None ⇒ F == 现行公式`（逐值，`abs=0`）。

---

## 5. 阶段化：影子 → 开权重

### 5.1 模式开关

config 新增（[config/__init__.py:350](alphabee/config/__init__.py#L350) 区）：

```yaml
midterm:
  f_marginal_mode: shadow      # off = 不算（严格复现旧口径）；shadow = 算但不进 F（缺省）；
                               # on     = 进 F（P1，门禁通过后显式开启）
```

- **纯函数纪律**：`score_engine` 不读 config。`decision_model.evaluate` 解析一次
  （`resolve_f_marginal_mode()`，fail-open 回 `"shadow"`），作为 keyword 参数
  `compress_scores(snapshot, marginal_mode=...)` 与 `fundamental_breakdown(f, e, marginal_mode=...)` 传入。
- 缺省 `shadow`：**F 逐值不变**，只多算几个分解键（数据层已取，无额外 IO）。
- `off`：连派生都不做（`score_breakdown` 不含边际键），用于严格复现与故障隔离。

### 5.2 影子期观测（≥ 50 帧真实语料）

| 指标 | 口径 | 判读 |
|---|---|---|
| S1 `ΔF` 分布 | `\|F_shadow − F\|` 的**中位数用 ex-Q1**（`median_abs_delta_ex_q1`）；P90 仍为全帧（`abs_delta_f_p90`，无 ex-Q1 版本） | 中位 \|ΔF\|（ex-Q1）≤ 0.10 |
| S2 状态翻转率 | `argmax(F_shadow) ≠ argmax(F)` 的帧占比 | ≤ 10% |
| S3 危险翻转 | 现行判 `S5` 而 `F_shadow` 判到 `{S1,S2,S3}` 的帧 | ≤ 3%（尤其盯顶背离消失） |
| S4 加速度覆盖率 | **ex-Q1** 的 `f_marginal_source > 0` 帧占比（`coverage_ex_q1`） | ≥ 85%（数据可用性的现实检验） |
| S5–S8 | OCF 命中率 / beat-miss 同期率 / 扣非差异 / 分部分布（治理设计 §15.1，additive 观测键） | 只观测，不单独构成开权门 |
| 人工抽检 | 每个翻转帧对照财报原文 | 翻转必须有原文依据 |

- **ex-Q1 口径（P0-3）**：Q1 帧的单季=累计、`Δ` 结构性为 0，不参与加速度质量门；`n_q1_frames` 单独计数，`coverage` / `abs_delta_f_median` / `abs_delta_f_p90` 等全帧字段保留作对照，`g8_pass` 判读用 `coverage_ex_q1` 与 `median_abs_delta_ex_q1`。实现与 CLI 文本同步（`scripts/f_marginal_shadow.py` 以 ex-Q1 为主、全帧值并列打印）。

**语料来源（两条链路，读取侧合并、写入侧不合并）**：

- `data/midterm/state/<symbol>.jsonl`：`tracking/scheduler.py` 的跟踪帧（带 `stale_after` / `exit_conditions`，同标的同日幂等）。
- `TaskRecord.midterm`（`data/task_records/**/*.json`）：一次性查询 run 的帧，经 `alphabee/task_records/shadow.py::load_task_record_frames` 适配；`midterm is None` 的旧记录只计数不抛。
- **不把查询帧写进 state 目录**：那会与 `symbol:as_of_date` 幂等冲突（同日吞掉跟踪帧）、污染 tracking 的 `prev` 连续性（prior/thesis/evidence）并让 `stale_after=None` 静默绕过 preflight 陈旧阻断。
- 样本单位 = **symbol-day**：`dedupe_frames` 默认按 `(symbol, as_of_date)` 去重；`--source both` 时 state 帧排在前面（优先保留字段更全者）。

### 5.3 开权重条件

同时满足才把默认改为 `on`：门 1–7（§7.1）全绿 **且** S1–S3 达标 **且** 影子期 ≥ 50 帧。

### 5.4 回滚

`f_marginal_mode: shadow` → `off` 一行配置；无需改代码。若需彻底回退，revert PR-4（代码保留影子路径）。

---

## 6. 契约传播

### 6.1 契约变化（old → new）

```yaml
contract_change:
  producer:
    - alphabee/midterm/factors.py:346            (build_fundamental_factor + derive_marginal)
    - alphabee/midterm/score_engine.py:167       (fundamental_breakdown 增边际键与 mode)
  old_shape:
    FundamentalFactor: { ..., score, score_breakdown }
    score_breakdown:   { growth_mean, ocf_penalty, beat_bonus, raw, f, ... }
  new_shape:
    FundamentalFactor: { ..., q_revenue_yoy, q_net_profit_yoy, q_gross_margin, q_gross_margin_pp }
    score_breakdown:   { ..., marginal_raw, marginal_quality, marginal, f_shadow,
                             f_shadow_delta, f_marginal_source }
    CompanyStateArtifact: { ..., f_marginal_mode }
  changed_fields:
    - { name: q_revenue_yoy,      change: add, default: null }
    - { name: q_net_profit_yoy,   change: add, default: null }
    - { name: q_gross_margin,     change: add, default: null }
    - { name: q_gross_margin_pp,  change: add, default: null }
    - { name: f_marginal_mode,    change: add, default: "off" }   # artifact 级 provenance
  breaking: false
```

### 6.2 `models.py`

- `FundamentalFactor` 末尾追加 §3.3 的 4 个字段（`None` 显式缺失，不登记进 `missing_facts`）。
- `CompanyStateArtifact` 追加 `f_marginal_mode: str = "off"`（新增帧写解析后的实际值；旧帧回落 `"off"`，
  语义为"旧帧未记录/未启用"）。`schema_version` 不升（additive 默认值，沿用 V2 先例）。

### 6.3 `score_engine.py`

```python
def fundamental_breakdown(
    f: FundamentalFactor,
    e: ExpectationFactor,
    *,
    marginal_mode: str = "off",       # off | shadow | on
) -> dict[str, float] | None: ...

def _fundamental_trend(f, e, *, marginal_mode: str = "off") -> float | None: ...
def compress_scores(snapshot: FactorSnapshot, *, marginal_mode: str = "off") -> VariableScores: ...
```

**兼容性**：`marginal_mode` 缺省 `"off"` ⇒ 所有既有调用点（含单测直接调 `compress_scores(snap)`）
行为逐值不变。

### 6.4 `decision_model.py`

- `resolve_f_marginal_mode()`（fail-open，三档语义分开）：
  - **配置缺段/缺字段** → 走 `MidtermClassifierSettings` 的默认值 `"shadow"`（算但不生效，正是要积累的语料）；
  - **读配置抛异常 / 取值非法** → warn + `"off"`（最保守：连派生都不做，避免在配置坏掉时往 artifact 写新键）；
  - **显式参数**（`evaluate(f_marginal_mode=...)`）优先于配置，与 `classifier_formula` 同构。
- `evaluate`：`resolved_mode = resolve_f_marginal_mode(f_marginal_mode)`；`compress_scores(snapshot, marginal_mode=resolved_mode)`；
  `_writeback_f_breakdown(snapshot, marginal_mode=resolved_mode)`（把 `f_shadow` 等键写进 `score_breakdown`）；
  artifact 写 `f_marginal_mode=resolved_mode`。
- `get_decision`：透传 `f_marginal_mode`（keyword-only）。

### 6.5 消费面

| 消费者 | 改动 | 说明 |
|---|---|---|
| `midterm_reporter._render_f_breakdown` | 追加一行「F 边际（影子）」 | 仅当 `score_breakdown` 含 `marginal` 键；含单季原始值、M_raw、Q、F_shadow 与 ΔF |
| `apps/cli/renderer.py` | **不改** | 已有 `顺序/公式/不确定/顶背离` 标签；F 边际在总结正文 |
| `task_records` | **不改** | 记录 `report_raw` 与渲染文本，无需新字段 |
| `tracking/scheduler.py` | **不改** | `FactorSnapshot` 随 artifact 落盘，字段自动携带 |
| `orchestrator/contracts.coerce_midterm_decision` | **不改** | `model_validate` 自动接受新字段 |

**dead-end 检查**：4 个 `q_*` 字段由 `fundamental_breakdown`（P1）与报告行（P0）消费；
`f_marginal_mode` 由报告元信息行与跟踪帧消费；`marginal*` 键由报告消费。无 dead-end。

### 6.6 序列化 / golden

- `schema_version` 不变；旧 JSONL `model_validate` 回落默认值。
- **golden 口径**（[test_v1_artifact_golden.py](tests/midterm/test_v1_artifact_golden.py)）：`_canonical` 的剔除集合
  追加 ① `FundamentalFactor` 的 4 个新字段；② `f_marginal_mode`。二者与 `score_breakdown` 同属
  "additive 诊断字段"，剔除后仍保持**数值零回归**，其存在性与确定性由新用例单独钉住。

---

## 7. 校准与验收

### 7.1 校准门（写死进测试；P1 开权重必须全绿）

| 门 | 断言 | 反例/杀伤 |
|---|---|---|
| **G-1 600760** | `marginal_mode="on"` 下 `F ∈ [−0.55, −0.30]`，`argmax ∈ {S5}`，且 `adverse_divergence is not None` | 去掉质量门 ⇒ F −0.15、adverse None ⇒ 本门必红 |
| **G-2 无数据恒等** | 无单季数据的帧：`on` 模式 F 与 `off` 模式**逐值相等**（`abs=0`） | — |
| **G-3 加速 + 优质** | `L=+0.3`、`Δ_rev=Δ_np=+30pp`、`margin_pp=+2` ⇒ `F > 0.45` | 权重为 0 ⇒ 红 |
| **G-4 高位减速** | `L=+1.0`、`Δ_rev=Δ_np=−40pp` ⇒ `F < 0.55` | 去掉边际项 ⇒ F=1.0 ⇒ 红 |
| **G-5 亏损转盈** | 上年同期净利 ≤ 0 的帧：`marginal` 只用营收腿或降级，`|M|` 不得由净利百分比产生 | 直接算百分比 ⇒ 红 |
| **G-6 单调/有界** | §4.6 的 1/2/3/4 条 | — |
| **G-7 影子不生效** | `shadow` 模式下对全测试帧 `F_shadow` 有值但 `F` 与 `off` **逐值相等** | — |
| **G-8 语料门槛** | 真实帧 ≥ 50 帧；`median|ΔF|`（**ex-Q1**，`median_abs_delta_ex_q1`）≤ 0.10；翻转率 ≤ 10%；危险翻转（`S5`→多头）≤ 3%；覆盖率（**ex-Q1**，`coverage_ex_q1`）≥ 85% | 未达标不得开 `on`；Q1 帧不计入 S1/S4 分母（P0-3） |

### 7.2 参数冻结

`F_MARGINAL_WEIGHT` / `F_ACCEL_SCALE` / `F_MARGIN_DELTA_SCALE` / `F_MARGIN_QUALITY_FLOOR`
在 P2 影子期后冻结，冻结值写入常量区注释（含语料规模与达标指标），并由测试以
±0.05 容差复核聚合门（沿用 V2 §7.3 的"参数钉"做法）。

---

## 8. 测试矩阵

### 8.1 数据层（`tests/midterm/test_marginal_derivation.py`）

| 编号 | 用例 | 断言 |
|---|---|---|
| **D1** | Q3 正常路径 | 单季营收 = 9M − H1；单季同比 = 单季/去年 Q3 − 1 |
| **D2** | Q1 路径 | 单季 = 累计（不调用 t_prev） |
| **D3** | 年报路径 | 单季 = FY − 同年 9M；同比用去年 FY − 去年 9M |
| **D4** | 数组乱序 | 输入倒序/乱序结果一致（先排序） |
| **D5** | 期数不足 | 缺 t−4 ⇒ 同比 None；缺 t_prev ⇒ 单季值 None；不插值 |
| **D6** | 上年同期 ≤ 0 | 净利同比 None；营收腿仍可用 |
| **D7** | 分母过小 | `|t−4| < 1% 累计` ⇒ 该腿 None |
| **D8** | 异常毛利率 | 单季毛利率 ∉ [−50,100] ⇒ None |
| **D9** | 全空 `fin_data` | 4 字段全 None，不抛 |

### 8.2 评分层（`tests/midterm/test_f_marginal_scoring.py`）

| 编号 | 用例 | 断言 |
|---|---|---|
| **S1** | 600760 三档 | 现行 −0.733；`on` ≈ −0.393；`Q=1` 对照 ≈ −0.153（§0 表） |
| **S2** | 降级恒等 | `marginal None` ⇒ F 与 `marginal_mode="off"` 逐值相等 |
| **S3** | 影子不生效 | `shadow` ⇒ F 不变、`f_shadow` 有值、`f_shadow_delta` 记录差值 |
| **S4** | beat 去重 | `on` + `Q<1` + beat 生效 ⇒ beat 折半；`shadow` 下 beat 与 F 均不变 |
| **S5** | 边界 | 亏损转盈、年报、Q1（复用 D 组输入） |
| **S6** | 有界/连续 | §4.6 |
| **S7** | 变异杀伤 | ① 去掉质量门 ⇒ G-1 红；② 权重置 0 ⇒ G-3 红；③ 用 `q_yoy` 水平替代 `Δ` ⇒ G-3 重复计分（断言与 L 的相关系数过高则红） |

### 8.3 回归/接线

| 文件 | 断言 |
|---|---|
| `test_v1_artifact_golden.py` | 剔除 4 字段 + `f_marginal_mode` 后仍逐字节等于 golden；新字段定值（None/`"shadow"`） |
| `test_midterm_reporter.py` | `shadow` 帧出现「F 边际（影子）」行；`off` 帧不出现；旧帧不出现 |
| `test_decision_model.py` | artifact `f_marginal_mode` 等于解析值；`off` 时 `score_breakdown` 无边际键 |
| `test_factors.py` | `build_fundamental_factor` 透传 4 字段（用合成多期数据） |

---

## 9. 实施切分与回滚

| PR | 内容 | 行为变化 | 回滚 |
|---|---|---|---|
| **PR-1** | §3 数据层（`derive_marginal` + 4 字段）+ D 组测试 | **无**（字段新增，None；F 不变） | revert |
| **PR-2** | §4/§5 影子层（`marginal_mode` 参数、breakdown 键、config、reporter 行、golden 剔除）+ S 组测试 | **无**（缺省 `shadow`：F 逐值不变） | `f_marginal_mode: off` |
| **PR-3** | 影子期观测脚本/统计 + 门 G-8 证据 + 参数冻结 | 无 | — |
| **PR-4** | 开权重（`on`）+ golden 重基线（记录 ΔF 证据） | **有**（F 变，v1/v2 共用） | 配置改回 `shadow` |

**PR-4 的前置**：§7.1 全绿 + 影子期 ≥ 50 帧 + ROADMAP 登记。**未达标不得开 `on`**。

---

## 10. 风险与替代方案

### 10.1 风险登记

| 编号 | 风险 | 缓解 |
|---|---|---|
| **R1** | 低基数伪拐点（去年 Q3 极小） | D7 分母门槛 + 亏损转盈走绝对额/降权 + 门 G-5 |
| **R2** | 「营业总收入 vs 营业收入」口径近似导致毛利率失真 | **P1-4 后收敛**：仅在 `operating_revenue` 缺失、回落总营收时存在（§3.5）；影子期抽检回落路径偏差；必要时补 `oper_cost`（P1.5） |
| **R3** | 单季数据噪声推高翻转率 | 权重 0.3 上限 + 25pp 饱和 + 影子期 S2/S3 门槛 |
| **R4** | 与 beat 双重奖励 | §4.4 折半规则 |
| **R5** | 历史帧口径断裂（on 后与旧帧不可比） | `f_marginal_mode` provenance + 跟踪帧 `mismatch` 口径可复用 |
| **R6** | 期数/重述导致同标的期间不一致 | `_dedup` 按 `ann_date` 最新；报告期显式落盘（`period` 不丢） |
| **R7** | 参数过拟合 | 参数冻结 + 聚合门 ±0.05 + 影子期独立样本 |

### 10.2 已评估并拒绝

| 替代 | 拒绝理由 |
|---|---|
| 直接把 `q_*_yoy` 作为 F 的新输入（不加加速度） | 与 L 重复计分（常年高增长公司被算两次） |
| 用 `net_profit_ttm_yoy`（已有字段）替代单季 | TTM 是 12 个月窗口的**水平**，仍看不见单季拐点 |
| 用 QoQ 环比 | 交付型行业季节性畸高 |
| 只加净利单季、不加毛利率门 | 本帧会翻成 S0 并把顶背离抹掉（§0 第三行） |
| 把边际改善做成第七因子 | 状态机/EV/仓位全按六因子设计，重排成本高、收益低 |
| 一次性开 `on`，不做影子 | 无法在真实语料上量化翻转率与误报，风险不可控 |

---

## 11. 文档与 ROADMAP 登记

| 文件 | 动作 |
|---|---|
| `docs/implementation/midterm/MIDTERM_F_MARGINAL_DESIGN.md` | 本文件（新增） |
| `docs/midterm/MIDTERM_DECISION_MODEL.md` | F 因子小节补「水平 + 加速度 + 质量门」口径与降级路径 |
| `docs/roadmap/ROADMAP.md` | 逐 PR 登记；PR-4 单独登记"行为变更（F 口径切换）+ 回滚" |
| `docs/guide/CONFIGURATION.md` | 补 `midterm.f_marginal_mode` 三值与缺省 |
| `config.yaml.example` | `midterm` 段补 `f_marginal_mode: shadow` 与注释 |

---

## 附录 A 参数速查

```python
F_MARGINAL_WEIGHT      = 0.30
F_ACCEL_SCALE          = 25.0   # pp
F_MARGIN_DELTA_SCALE   = 4.0    # pp
F_MARGIN_QUALITY_FLOOR = 0.20
# 新增 config
midterm.f_marginal_mode = "shadow"   # off | shadow | on
```

## 附录 B 600760 复算（P1 口径）

```text
L      = mean(sat(-34.19), sat(-26.0), sat(-28.0)) = mean(-1, -0.8667, -0.9333) = -0.9333
Δ_rev  = 23.29 - (-34.19) = +57.48 pp → sat(+57.48/25) = +1.0
Δ_np   = 137.0 - (-26.0)  = +163.0 pp → sat(+163/25)  = +1.0
M_raw  = 1.0
Q      = clip(1 + (-3.4)/4, 0.2, 1.0) = 0.20
M      = 0.20
score  = 0.7×(-0.9333) + 0.3×0.20 + 0 (OCF=+10.26亿) + 0.2 (beat) = -0.3933
F      = -0.3933
状态   = S5（P 0.453，uncertain=True，顶背离 0.346）
对照   = 现行 -0.7333（S5 0.890、确信退出、顶背离 0.846）
```

## 附录 C 术语

| 词 | 含义 |
|---|---|
| 累计同比 | 报告期累计值相对去年同期的增速（三季报 = 前三季度累计） |
| 单季同比 `q_*_yoy` | 同一报告期（Q1/Q2/Q3/Q4）单季值相对去年同季的增速 |
| 加速度 `Δ` | 单季同比 − 累计同比（pp）：最近一季比过去几季快/慢多少 |
| 质量门 `Q` | 由单季毛利率同比变化映射的折减系数 ∈[0.2, 1.0] |
| 影子模式 | 计算 `M/Q/F_shadow` 并落盘，但 **F 仍用现行公式** |
