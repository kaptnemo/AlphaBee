# AlphaBee 中期投资决策模型设计

> `Investment Decision = f(F, E, T, V, C, R, M)`
>
> 关联文档：
> - `docs/midterm/MIDTERM_S1_S4.md` — 因子框架、S1–S4 状态机、决策/仓位原始定义
> - `docs/midterm/MIDTERM_FACTOR_DATA_DESIGN.md` — 七因子数据获取设计
> - `docs/midterm/MIDTERM_INVESTMENT_ROADMAP.md` — 落地路线图（7.5/7.6 typed contracts）
> - `alphabee/midterm/models.py` — 本模型对应的 typed contracts（已实现）
>
> 本文档固化「七因子如何合成为投资决策」的领域模型，是 `classifier.py` / `score_engine.py` /
> `bayes.py` / `position.py` 四个引擎的实现依据。

---

## 0. 一句话定位

> **因子决定 State（在哪个生命周期）和 EV（赔率多少），证据决定 Confidence（多确定），
> 三者合成个股决策，再乘以市场与组合约束得到最终仓位。**

7 个因子只直接进入「压缩」和「状态/赔率」两层，且以**模式**（pattern）而非**求和**（sum）的方式决定 State。

---

## 1. 为什么不能线性加权

`score = w₁F + w₂E + … + w₇M` 被文档明确否定（§1）：

> 两个 78 分的股票可能处于完全不同的生命周期，所以不能"78 分买入、65 分卖出"。

线性加权会抹掉三件正交的事：

| 被抹掉的量 | 含义 | 文档出处 |
|-----------|------|---------|
| **State** | 投资生命周期在哪里（S0–S5） | §29：State ≠ Score |
| **Confidence** | 对这个状态判断有多确定（P(H\|E)） | §29：Confidence ≠ State |
| **RiskReward** | 赔率/期望值（EV / RiskAdjustedEV） | §42 |

因此决策模型必须先分离出 State，再谈分数。开仓吸引力也不是线性单调（§27），而是一条钟形曲线，
峰值在 `S1 Late → S2 Early`（赔率仍高 + 胜率快速上升的交点）。

---

## 2. 总架构：四层流水线 + 三种正交量

```text
        F  E  T  V  C  R  M                     Evidence（证据日志）
             │                                        │
   ┌─────────┴───────────┐                 ┌──────────┴───────────┐
   │ Layer 1  VariableScores │               │ Layer 1'  bayes      │
   │  （因子 → 方向分压缩）    │               │  （证据 → P(H|E) 后验）│
   └─────────┬───────────┘                 └──────────┬───────────┘
             │                                        │
   ┌─────────┴───────────────┐                       │
   │ Layer 2  classifier      │                       │
   │   因子模式 → State(位置)  │                       │
   │   V/E → ExpectedValue(赔率)│                      │
   └─────────┬───────────────┘                       │
             │                                       │
        State │   EV   │  Confidence ─────────────────┘
              │        │        │
   ┌──────────┴────────┴────────┴──────────────────────────┐
   │ Layer 3  Decision = State(动作类型) × Confidence(力度)  │
   │                    × RiskAdjustedEV(赔率门槛)           │
   └───────────────────────┬───────────────────────────────┘
                           │
   ┌───────────────────────┴───────────────────────────────┐
   │ Layer 4  Position = 个股决策 × MarketExposure(M)         │
   │                     × Portfolio调整(相关性/集中度)         │
   └─────────────────────────────────────────────────────────┘
```

**三种正交量**贯穿全文，永不合并：

| 量 | 类型 | 决定什么 | 输入 |
|----|------|---------|------|
| State | 分类量（S0–S5） | 动作**类型**（试探/加仓/持有/减仓/清仓） | 因子模式 |
| Confidence | 连续量（0–1） | 动作**力度**（后验越高越敢下注） | 证据贝叶斯更新 |
| RiskAdjustedEV | 连续量（RATIO） | 赔率**门槛**（赔率不足则不做/减仓） | V + 情景预测 |

---

## 3. Layer 1：FactorSnapshot → VariableScores（压缩）

### 3.1 输入：FactorSnapshot（原始值，已实现）

`midterm/models.py` 的 `FactorSnapshot` 承载七因子原始 canonical 值（F 的 `revenue_yoy/roe`、E 的
`eps_fy1_revision_1m`、T 的 `rs_stock_market_20d`、V 的 `pe_ttm_5y_percentile`、C 的 `hot_rank`、
R 的 `pledge_ratio/audit`、M 的 `market_score/regime`）。**这是唯一事实来源，方向分只是其"视图"。**

### 3.2 输出：VariableScores（方向分，已实现 typed contract）

```python
class VariableScores(BaseModel):
    m: dict                        # 复用 MarketScore 摘要（RegimeSnapshot）
    f_fundamental_trend: float|None  # dF/dt 边际变化方向（improving→正）
    e_revision: float|None           # 上修(+) / 下修(-)
    t_relative_strength: float|None  # 相对强度方向分
    v_valuation_percentile: float|None # 估值分位（低分位 = 赔率高）
    c_crowding: float|None           # 拥挤度方向分
    r_risk: float|None               # 三层风险合成方向分
```

### 3.3 压缩规则（score_engine 职责）

每个因子**独立**压成一个 `[-1, 1]` 或 `[0, 100]` 方向分，**不跨因子求和**：

| 因子 | 方向分来源 | 符号约定 |
|------|-----------|---------|
| F | `gross_margin_trend` + `revenue_yoy` + `net_profit_yoy` 的边际方向 | 改善→正 |
| E | `eps_fy1_revision_1m/3m` + `revision_breadth` | 上修→正（**核心因子**） |
| T | `rs_stock_market_20d/60d` + 均线结构 | 相对走强→正 |
| V | `pe_ttm_5y_percentile` / `pb_5y_percentile` | 低分位→正（赔率高） |
| C | `holder_count_change`（户数↓→筹码集中→负）+ `hot_rank` + `turnover_rate_percentile` | 越拥挤→越负 |
| R | 质押 + 杠杆 + 商誉 + 审计意见 三层合成 | 风险升→越负 |
| M | 直接复用 `market_score` | 0–100 |

**约束**：压缩后 `FactorSnapshot` 原始值必须保留——classifier 读方向一致性（共振/背离）、EV 读估值分位与 EPS 绝对值，都需要原始量。

---

## 4. Layer 2a：因子模式 → State（classifier，非求和）

State 由**因子模式**决定，classifier 输出 `StateTransition`（含 `legal` + `trigger_factors`）。

### 4.1 状态定义（`CognitiveState` 已实现）

```text
S0 研究候选 → S1 预期差(试探) → S2 证据确认(加仓) → S3 共识扩散(持有)
→ S4 充分定价(减仓) → S5 退出
```

### 4.2 因子模式 → 状态映射表

| State | 因子模式（§16/§19/§20/§26） | 语义 |
|-------|------------------------------|------|
| **S1** | Price弱 + Consensus弱 + LeadingEvidence↑ | 预期差形成：市场低预期 + 你发现未定价的正向变化 |
| **S2** | F 改善 + E 上修（证据开始验证） | 现实开始证明你对 |
| **S3** | F↑ + E↑ + T↑ 共振 | 基本面+预期+价格三共振 |
| **S4** | E 停上修/转负 + T 弱 + V 扩张 + C↑ | 好消息 Price In，边际买家不足 |
| **S5** | Thesis 证伪 / E 持续下修 | 退出（thesis_broken / alpha_exhausted） |

### 4.3 关键：顶部判定的顺序是 E → T → F（§20）

顶部顺序是 `E → T → F`：**盈利预测先停止上修（E 转平），再股价 RS 下降（T 弱），最后财报才真正变坏（F 坏）**。
因此 classifier 对 `S3 → S4` 的判定**不能等 F 的财报拐点**（财报是滞后指标），必须优先看：

1. `e_revision` 是否 `↑↑→↑→flat→↓`（revision 减速/转负）；
2. `t_relative_strength` 是否破坏；
3. 最后才确认 `f_fundamental_trend`。

所以 classifier 规则优先级：**E > T > F**（对 downgrade 判定尤其如此）。

### 4.4 共振与背离（§16 vs §20）

- `F↑ + E↑ + T↑` 同向 → `resonant`（S3 健康，容忍回撤）；
- `业绩好但股价不涨`（F 强、E/T 弱）→ `divergent`，本身就是 `Conflict`，应触发研究（§20）。

classifier 需对每个 `FactorDelta` 标注 `consistency`（resonant / divergent / independent），
背离是 S3→S4 与 `EmergencyRiskStop` 的前置信号。

### 4.5 状态迁移的合法性（§41）

`State` 不是买卖指令。`classifier` 只描述"生命周期在哪里 + 是否合法迁移"：

- 合法：`S1→S2→S3→S4→S5` 及反向降级（`S3→S2`、`S2→S1`）；
- 非法跳级（如 `S1→S3` 直接跳）→ `legal=False`，需人工/证据补齐；
- `S3 Late → S3 Acceleration → S1'`（第二增长曲线重开预期差，§31–§32）→ `kind="reopen"`。

---

## 5. Layer 2b：V/E → ExpectedValue（赔率，§42）

### 5.1 模型（`ExpectedValue`/`ScenarioOutcome` 已实现）

```python
class ExpectedValue(BaseModel):
    scenarios: list[ScenarioOutcome]   # bull / base / bear
    ev: float|None                     # EV = Σ P·R
    risk: float|None                   # 风险度量（下行波动/回撤）
    risk_adjusted_ev: float|None       # EV / risk
    probability_source: str            # bayes_posterior / state_prior / manual
```

```python
class ScenarioOutcome(BaseModel):
    scenario: str                      # bull / base / bear
    probability: float                 # P，0-1
    expected_return: float|None        # R，PERCENT 该情景预期收益
    earnings_contribution: float|None  # 盈利变化贡献
    valuation_contribution: float|None # 估值变化贡献
    max_drawdown: float|None           # 该情景最大回撤
```

### 5.2 状态机与 EV 的关系（§42 核心）

**状态机不替代 EV，而是帮 EV 估计概率**：

```text
Evidence → BeliefUpdate → ScenarioProbability(P_bull/base/bear) → ExpectedReturn → Position
```

`P_bull/P_base/P_bear` 由 `Confidence`（贝叶斯后验）+ `State`（生命周期阶段）联合驱动：

| State | P_bull 特征 | 赔率特征 | 钟形曲线位置 |
|-------|------------|---------|-------------|
| S1 | 低（未验证） | **高** | 左坡 |
| S2 | **快速上升** | 高 | 峰值区 |
| S3 | **最高** | 中 | 右坡 |
| S4 | 开始下降 | 低 | 右尾 |

三情景收益 `R` 的分解（`earnings_contribution` + `valuation_contribution`）让 EV 可解释：
是"盈利增长驱动"还是"估值扩张驱动"——S4 的 EV 主要由 `valuation_contribution` 贡献时，是典型的"剩余动量"而非"基本面 alpha"（§23）。

---

## 6. Layer 3：三轴正交决策（State × Confidence × RiskAdjustedEV）

### 6.1 为什么 `if state == S2: buy()` 是错的（§41）

State 只决定**动作类型**，不决定**动作力度**。两个 S2 股票：

| | 股票 A | 股票 B |
|--|--------|--------|
| State | S2 | S2 |
| Confidence | 0.82 | 0.55 |
| PE | 15X | 50X |

**仓位必须不同**。故决策是 State / Confidence / RiskAdjustedEV 三轴的联合函数。

### 6.2 三轴 → 动作类型 × 力度 × 门槛

| 轴 | 决定 | 输出 |
|----|------|------|
| State | 动作类型 | `position_band`：试探 / 加仓 / 核心 / 减仓 / 清仓 |
| Confidence | 动作力度 | 仓位带内的实际权重缩放 |
| RiskAdjustedEV | 赔率门槛 | EV 不足（如 < 阈值）→ 压到 0 或减仓 |

### 6.3 动作类型 → 仓位带（§6/§13/§16，示意，非固定数字）

| State | 动作 | 权益仓内参考带 | 核心止损 |
|-------|------|--------------|---------|
| S1 | 试探 | 5–10% | Thesis / Time Stop |
| S2 | 加仓 | 10–15% | Evidence / Revision Stop |
| S3 | 持有（核心） | 15–20% | Revision / Trend Stop |
| S4 | 减仓 | 降至试探/退出 | Trend / Valuation Stop |
| S5 | 清仓 | 0 | — |

> 阈值须回测，这里只做结构示意（§6 原话："这是框架示意，不应该成为固定数字"）。

---

## 7. Layer 4：组合层 Position（§28/§38）

### 7.1 公式

```text
Position_i = BaseRiskBudget
             × StateMultiplier        # State → 仓位带（Layer 3 输出）
             × Confidence             # 后验置信
             × RiskAdjustment         # r_risk 三层风险合成
             × PortfolioAdjustment    # 相关性 / 集中度 / 风格暴露
             × MarketExposure         # M 的 PositionAdvice（市场仓位上下限）
```

### 7.2 组合风险必须独立于个股 State（§38）

三只个股各自 S2/S3/S2 都正常，但若全暴露于 "AI Capex" 或 "SmallCapGrowth"，**组合风险仍过高**。
因此 `PositionDecision` 分层相乘，不简单相加：

```python
class PositionDecision(BaseModel):
    portfolio_exposure: float|None   # 来自 market_regime PositionAdvice（M）
    stock_weight: float|None         # f(F,E,T,V,C,R) 个股层权重
    actual_weight: float|None        # exposure × weight
    position_band: str               # 试探/加仓/核心/减仓/清仓（映射 State）
    rationale: list[str]
    restricted: bool                 # 单股上限/单次调仓限制是否生效
```

`stock_weight` 由 Layer 3 三轴合成；`portfolio_exposure` 由 M 独立给出；`actual_weight` 是乘积；
`restricted` 由组合集中度/单股上限约束置位。

---

## 8. 状态迁移规则表（classifier 实现依据，汇总）

| 迁移 | 触发 | 动作带变化 |
|------|------|-----------|
| S1 → S2 | E 上修 + F 证据确认（`Evidence Confirmed`） | 研究仓 → 正常仓 |
| S2 → S3 | F↑+E↑+T↑ 共振（`Consensus Expansion`） | 正常仓 → 核心仓 |
| S3 → S2 | E 停上修 + RS 破坏（`State Downgrade`） | 核心仓 → 正常仓 |
| S2 → S1 | E 下修 + Evidence 恶化 | 正常仓 → 研究仓 |
| S1 → S5 | Thesis 证伪 / Time Stop（§7） | 研究仓 → 0 |
| S3 → S4 | E→T→F 顺序反转 + V 扩张 + C↑ | 核心仓 → 减仓 |
| S3 Late → S3 Acceleration → S1' | 新产品周期重开预期差（§31–§32） | 第二曲线 |

---

## 9. 与现有 typed contracts 对齐

| 层 | typed contract（`midterm/models.py`） | 引擎 | 状态 |
|----|--------------------------------------|------|------|
| Layer 1 | `VariableScores` | `score_engine.py` | ❌ 缺 |
| Layer 1' | `EvidenceEvent` / `CompanyStateArtifact` | `bayes.py` | ❌ 缺 |
| Layer 2a | `CognitiveState` / `StateTransition`(待补) | `classifier.py` | ❌ 缺 |
| Layer 2b | `ExpectedValue` / `ScenarioOutcome` | `bayes.py`(概率) + 情景估值 | ❌ 缺 |
| Layer 3 | `CompanyStateArtifact`（承载 State+Confidence+EV） | 决策合成 | ❌ 缺 |
| Layer 4 | `PositionDecision` | `position.py` | ❌ 缺 |

**结论**：决策数据契约已 100% 备好，缺的是四个引擎（`score_engine` / `classifier` / `bayes` / `position`）。

---

## 10. 落地分期

| 期 | 引擎 | 输入 → 输出 | 依赖 |
|----|------|------------|------|
| P1 | `score_engine.py` | FactorSnapshot → VariableScores | 七因子采集接线 |
| P2 | `classifier.py` | VariableScores(模式) → State + StateTransition | score_engine |
| P2 | `bayes.py` | EvidenceEvent 日志 → Confidence（log-odds） | 证据日志 |
| P3 | 情景估值 + EV | State×Confidence → P_bull/base/bear；V+EPS → R | classifier + bayes |
| P3 | `position.py` | 三轴 + M + 组合 → PositionDecision | 全部 |

---

## 附：关键设计决策速查

1. **不线性加权**：State 由因子模式（classifier）决定，非求和（§1/§29）。
2. **三轴正交**：State（类型）· Confidence（力度）· RiskAdjustedEV（门槛）永不合并（§29/§41）。
3. **E > T > F**：顶部判定顺序，避免等财报滞后信号（§20）。
4. **状态机服务 EV**：状态机估计 P，EV 才是最终决策核心（§42）。
5. **组合独立**：Position = 个股决策 × MarketExposure × Portfolio调整，分层相乘（§28/§38）。
6. **State 非指令**：`if state==S2: buy()` 是反模式，动作力度由 Confidence+EV 决定（§41）。
