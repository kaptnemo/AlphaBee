# MIDTERM S1-S4 整体设计

它解决的不是“股价跌多少买、涨多少卖”，而是：

[
\boxed{
预期差发现
\rightarrow
证据验证
\rightarrow
共识扩散
\rightarrow
预期透支
\rightarrow
退出
}
]

并通过 **Evidence / Expectation / Price / Valuation / Risk** 的持续变化决定状态迁移和仓位。

下面我给出一个完整版本。

---

# 一、整个体系最核心的思想

传统交易体系经常围绕：

```text
突破120日线 → 买
上涨5% → 加仓
跌8% → 止损
跌破20周线 → 卖
```

这套方法的问题是：

> **价格决定了几乎所有行为。**

而我们现在这套体系应该是：

[
\boxed{
Investment\ Decision
====================

f(F,E,T,V,C,R,M)
}
]

其中：

| 因子 | 含义                        |
| -- | ------------------------- |
| F  | Fundamental，基本面及其变化       |
| E  | Expectation，市场预期及Revision |
| T  | Trend，价格/相对强度趋势           |
| V  | Valuation，估值与赔率           |
| C  | Crowding，拥挤程度             |
| R  | Risk，个股/产业风险              |
| M  | Market，市场环境               |

但还有一个比 Score 更重要的变量：

[
\boxed{State}
]

所以不能简单：

> 78分买入，65分卖出。

因为：

**两个78分的股票可能处于完全不同的生命周期。**

---

# 二、S1-S4究竟代表什么？

我建议最终正式定义为：

| State  | 名称                     | 核心问题                  |
| ------ | ---------------------- | --------------------- |
| S0     | Discovery              | 是否存在值得研究的预期差？         |
| **S1** | Expectation Gap        | **市场可能错了吗？**          |
| **S2** | Evidence Confirmation  | **我的判断正在被事实验证吗？**     |
| **S3** | Consensus Expansion    | **市场是否正在持续认可这个逻辑？**   |
| **S4** | Expectation Saturation | **好消息是不是已经Price In？** |
| SX     | Broken / Exit          | **Thesis是否已经失效？**     |

完整生命周期：

[
S0\rightarrow S1\rightarrow S2\rightarrow S3\rightarrow S4\rightarrow SX
]

但注意：

> **它不是只能向右移动。**

完全允许：

[
S3\rightarrow S2
]

甚至：

[
S3\rightarrow SX
]

因此这不是“持仓时间阶段”。

而是：

[
\boxed{Investment\ State}
]

---

# 三、S1：Expectation Gap——预期差形成

这是整个投资过程最有赔率、同时也是最容易判断错误的阶段。

核心问题：

> **市场是不是低估/误解了某些未来变化？**

例如：

* 盈利即将见底；
* 行业供需即将反转；
* 新产品可能带来第二曲线；
* 市场过度悲观；
* 一次性事件掩盖真实盈利能力；
* 估值已经充分反映坏消息；
* 新业务尚未进入Consensus。

这个阶段经常出现：

[
Price\ Weak
]

[
Consensus\ Weak
]

但：

[
LeadingEvidence\uparrow
]

因此：

[
ExpectationGap>0
]

---

# 四、S1最重要的不是“便宜”

这是一个非常重要的原则。

不能因为：

[
PE低
]

或者：

[
PriceDrawdown=-50%
]

就定义为S1。

真正的S1应该满足：

[
\boxed{
LowExpectation
+
PotentialPositiveChange
+
AsymmetricPayoff
}
]

也就是：

> 市场预期很低，但你发现了一些市场可能还没有充分Price In的正向变化。

否则：

[
LowPE + BadBusiness
]

只是：

> Value Trap。

---

# 五、S1应该建立Hypothesis，而不是结论

例如：

```text
H1：国内业务将在Q3恢复
H2：2027 EPS已经接近下修底部
H3：反腐影响正在消退
H4：当前估值已经充分Price In悲观预期
```

每个假设需要：

```text
Probability
Supporting Evidence
Contradicting Evidence
Expected Evidence
Invalidation Condition
Deadline
```

也就是说：

[
S1=HypothesisDriven
]

而不是：

> “我觉得这个公司很好。”

---

# 六、S1仓位：研究仓

因为：

[
Confidence
]

仍然比较低。

所以S1不应该重仓。

如果定义：

[
EquityExposure=60%
]

单股在权益仓中的研究仓：

[
5%-10%
]

对应总资产：

[
3%-6%
]

更合理。

注意：

> 这是框架示意，不应该成为固定数字。

真正仓位以后应该由：

[
Position=f(State,Confidence,Risk,Portfolio)
]

决定。

---

# 七、S1最重要的止损：Thesis Stop + Time Stop

S1最不应该做的是：

> 跌了就补仓。

因为你的Thesis还没有验证。

如果核心假设：

[
H_1
]

被证伪：

[
P(H_1)\downarrow
]

应该退出。

甚至：

[
Price=-2%
]

也可以退出。

反过来，即使：

[
Price=+5%
]

但证据没出现，也不能自动进入S2。

---

S1还有非常重要的：

[
\boxed{TimeStop}
]

比如：

> 预计Q3出现盈利拐点。

结果：

```text
Q3没出现
↓
Q4仍然没出现
↓
EPS继续下修
```

即使股票没跌：

> 也应该退出。

因为资本存在：

[
OpportunityCost
]

---

# 八、S1价格只是一种“信息信号”

如果突然：

[
Price=-10%
]

不要自动理解：

> 便宜10%，应该加仓。

而应该问：

[
\boxed{
Market\ Knows\ Something\ I\ Don't?
}
]

尤其：

* 个股跌、行业不跌；
* 放量下跌；
* RS明显恶化；
* 同行业公司没有同步下跌。

应该自动触发：

[
ResearchTask
]

所以：

[
Price\rightarrow InformationSignal
]

而不是：

[
Price\rightarrow AutomaticTrade
]

---

# 九、S2：Evidence Confirmation——证据确认

这是我认为整个体系里：

[
\boxed{\text{最重要的开仓/加仓阶段}}
]

S1解决：

> 我可能发现了预期差。

S2解决：

> **现实开始证明我是对的。**

例如：

[
RevenueInflection
]

[
MarginInflection
]

[
IndustryData\uparrow
]

[
EPSRevision\uparrow
]

[
NewProductSuccess
]

开始出现。

---

# 十、为什么 S1 Late → S2 Early 是最理想的开仓区域？

整个生命周期：

```text
S1 Early
赔率高 / 胜率低
       ↓
S1 Late
       ↓
S2 Early   ← 最优区域
       ↓
S2 Late
       ↓
S3
胜率高 / 赔率下降
       ↓
S4
赔率明显下降
```

所以：

[
\boxed{
OptimalEntry
\approx
S1Late\rightarrow S2Early
}
]

这是：

[
Probability\uparrow
]

但：

[
ExpectationGap
]

仍然较大的阶段。

本质上是：

[
\boxed{
Evidence\ Confirmed
\quad but\quad
Consensus\ Not\ Fully\ Formed
}
]

---

# 十一、S2不是“业绩增长”

这点必须特别强调。

比如：

[
Profit+50%
]

不代表：

[
S2
]

因为市场可能原来预期：

[
+80%
]

真正需要：

[
Actual>Expectation
]

或者：

[
FutureExpectation\uparrow
]

所以：

[
\boxed{
S2核心不是Growth，而是PositiveEvidenceChange
}
]

---

# 十二、S2最重要的变量之一：Earnings Revision

例如：

[
EPS_{2027}:
3.0
\rightarrow3.2
\rightarrow3.5
\rightarrow3.8
]

说明：

[
Expectation\uparrow
]

这通常比：

> 去年利润同比+100%

更重要。

因此：

[
\boxed{
RevisionMomentum
}
]

应该成为AlphaBee核心因子。

至少包括：

[
Revision_{1M}
]

[
Revision_{3M}
]

[
RevisionBreadth
]

[
RevisionAcceleration
]

---

# 十三、S2仓位：正常仓

如果S1：

[
ResearchPosition
]

那么：

[
S1\rightarrow S2
]

意味着：

> 证据已经提高Confidence。

于是：

[
ResearchPosition\rightarrow NormalPosition
]

例如权益仓内部：

[
10%-15%
]

而不是简单：

> 股票涨5%就加仓。

真正的加仓原因应该是：

[
\boxed{
EvidenceStrength\uparrow
}
]

---

# 十四、S2的止损：Evidence Stop + Revision Stop

因为你加仓的原因是：

[
Evidence\uparrow
]

所以减仓自然应该来自：

[
Evidence\downarrow
]

例如原来：

[
Volume\uparrow
]

[
Margin\uparrow
]

[
EPSRevision\uparrow
]

后来：

[
Volume\uparrow
]

但：

[
Margin\downarrow
]

同时：

[
EPSRevision\downarrow
]

那么：

[
S2\rightarrow S1
]

仓位：

[
NormalPosition\rightarrow ResearchPosition
]

---

# 十五、这里形成一个非常重要的机制：State Downgrade

整个系统不能只有：

[
Buy/Hold/Sell
]

而应该：

[
\boxed{
StateTransition
\rightarrow
PositionTransition
}
]

例如：

```text
S1 → S2
研究仓 → 正常仓

S2 → S3
正常仓 → 核心仓

S3 → S2
核心仓 → 正常仓

S2 → S1
正常仓 → 研究仓

S1 → Broken
研究仓 → 0
```

这比单纯：

> 买 / 卖

更符合真实投资。

---

# 十六、S3：Consensus Expansion——共识扩散

到了S3：

> Thesis基本已经得到验证。

典型状态：

[
F\uparrow
]

[
E\uparrow
]

[
T\uparrow
]

也就是：

[
\boxed{
Fundamental
+
Expectation
+
Price
}
]

形成共振。

这个阶段往往就是：

> 大牛股最好拿、但越来越难买的阶段。

---

# 十七、S3赚的是什么钱？

已经不是早期预期差：

[
Mispricing
]

而更多是：

[
ConsensusExpansion
]

也就是：

> 越来越多人发现这个逻辑。

卖方上调：

[
TargetPrice\uparrow
]

机构：

[
Position\uparrow
]

EPS：

[
Revision\uparrow
]

股价：

[
RS\uparrow
]

于是形成：

[
PositiveFeedbackLoop
]

---

# 十八、S3最大的任务不是寻找买点，而是“拿住”

这一点非常重要。

真正的大趋势：

[
+100%
]

过程中可能出现：

[
-8%
]

[
-12%
]

甚至：

[
-20%
]

如果：

[
F\uparrow
]

[
E\uparrow
]

行业景气：

[
\uparrow
]

那么：

[
PriceCorrection
]

很多时候只是：

> Noise。

所以：

[
\boxed{
S3是四个阶段中PriceTolerance最高的阶段
}
]

不能使用：

> -5%机械止损。

---

# 十九、S3应该使用“分级减仓”

健康状态：

[
F\uparrow+E\uparrow+T\uparrow
]

第一阶段：

[
E:\uparrow\rightarrow Flat
]

但F、T仍强。

→ **继续持有。**

第二阶段：

[
E\downarrow
]

同时：

[
RS\downarrow
]

→ **减仓20%-30%。**

第三阶段：

[
E\downarrow+TrendBreak
]

→

[
S3\rightarrow S2/S4
]

降到正常仓。

第四阶段：

[
FundamentalThesisBroken
]

→ Exit。

---

# 二十、为什么S3特别要警惕“业绩很好但股价不涨”？

因为股票市场交易未来。

很多顶部的顺序是：

[
\boxed{
E\rightarrow T\rightarrow F
}
]

即：

### 第一阶段

盈利预测停止上修。

[
Revision\uparrow\rightarrow Flat
]

### 第二阶段

股价RS下降。

### 第三阶段

趋势破坏。

### 最后

财报才真正看到增长下降。

所以：

> **财报往往是滞后指标。**

这也是为什么：

[
GoodEarnings + WeakPrice
]

本身就是重要的：

[
Conflict
]

AlphaBee应该自动研究。

---

# 二十一、S4：Expectation Saturation——预期充分定价

S4不意味着：

> 公司变差了。

这是最容易误解的地方。

它真正意味着：

[
\boxed{
CompanyStillGood
\quad but\quad
RiskRewardBad
}
]

例如：

[
Revenue+50%
]

[
Profit+70%
]

都很好。

但是市场已经预期：

[
Profit+80%
]

于是：

[
ExpectationGap\leq0
]

这就是S4。

---

# 二十二、S4最典型的几个特征

可能包括：

### ① Consensus极高

所有人都知道它很好。

### ② Revision开始减速

[
\uparrow\uparrow
\rightarrow
\uparrow
\rightarrow
Flat
]

### ③ Valuation扩张

[
PE\uparrow
]

### ④ Crowding上升

### ⑤ 股价对利好钝化

例如：

> 利润+80%，股价不涨。

这是非常重要的：

[
PriceResponseToGoodNews\downarrow
]

### ⑥ 对小利空异常敏感

这往往意味着：

> 边际买家已经不足。

---

# 二十三、S4不是马上清仓

因为：

> 泡沫可以继续扩大。

所以不能：

[
S4Detected\rightarrow SellAll
]

更合理的是：

[
CorePosition\rightarrow ReducedPosition
]

然后：

> 让趋势决定剩余仓位什么时候退出。

因此S4主要赚：

[
ResidualMomentum
]

而不是：

[
FundamentalAlpha
]

---

# 二十四、S4止损逻辑明显变成 Price Sensitive

S3：

[
PriceTolerance=High
]

S4：

[
PriceTolerance\downarrow
]

因为：

[
ExpectedUpside\downarrow
]

而：

[
DrawdownRisk\uparrow
]

因此应该逐渐使用：

[
TrailingStop
]

例如从阶段高点：

[
-10%\sim-15%
]

结合：

* RS破坏；
* 中期趋势破坏；
* Revision下修；
* Valuation压缩；

分批退出。

具体阈值以后必须回测。

---

# 二十五、为什么同样跌10%，S2/S3/S4意义不同？

这是整个State Machine最漂亮的地方之一。

假设：

[
Price=-10%
]

### S1

可能意味着：

> 市场掌握了你不知道的坏消息。

→ **高度警惕。**

### S2

需要判断：

> Evidence是否恶化？

→ **中等警惕。**

### S3

如果：

[
F,E
]

仍然强：

> 可能只是正常波动。

→ **容忍。**

### S4

可能意味着：

> 共识开始瓦解。

→ **高度警惕。**

所以：

[
\boxed{
SamePriceMove
+
DifferentState
==============

DifferentMeaning
}
]

这也是为什么我越来越不建议用统一：

[
-8% StopLoss
]

---

# 二十六、四个阶段完整对比

|             | S1          | S2                | S3             | S4              |
| ----------- | ----------- | ----------------- | -------------- | --------------- |
| 核心          | 预期差         | 证据确认              | 共识扩散           | 预期透支            |
| 市场认知        | 低           | 开始改变              | 高              | 极高              |
| Fundamental | 尚未充分确认      | 改善                | 强              | 通常仍强            |
| Revision    | 底部/转折       | **上修**            | **持续上修**       | 放缓/转负           |
| Trend       | 弱/筑底        | 改善                | **强**          | 高位/转弱           |
| Valuation   | 通常低         | 合理                | 扩张             | 偏高              |
| Crowding    | 低           | 中低                | 上升             | 高               |
| 赔率          | **最高**      | **高**             | 中              | 低               |
| 胜率          | 低           | **快速提高**          | **最高**         | 开始下降            |
| 最佳动作        | 试探          | **开仓/加仓**         | **持有**         | 减仓/退出           |
| Price容忍     | 低           | 中                 | **高**          | 低               |
| 核心止损        | Thesis/Time | Evidence/Revision | Revision/Trend | Trend/Valuation |

---

# 二十七、因此开仓吸引力不是线性的

不是：

[
S1>S2>S3>S4
]

也不是：

[
S4>S3>S2>S1
]

而大概是一条钟形曲线：

```text
Entry
Attractiveness

          ▲
         / \
        /   \
       /     \
      /       \
_____/         \_____
S1   S2        S3   S4
```

最高点大致：

[
\boxed{S1Late\rightarrow S2Early}
]

因为这里：

[
Odds
]

还比较高，

而：

[
Probability
]

已经开始明显提高。

---

# 二十八、仓位也不应该机械跟State单调增加

初步可以理解：

[
S1 < S2 < S3
]

但不能简单：

[
S4=0
]

更合理：

```text
S1    ██
S2    █████
S3    ███████
S4    ███
Exit
```

也就是：

> **仓位随Confidence上升，但随着Risk/Reward恶化主动下降。**

可以抽象成：

[
\boxed{
Position_i
==========

BaseRiskBudget
\times
StateMultiplier
\times
Confidence
\times
RiskAdjustment
\times
PortfolioAdjustment
}
]

这才适合真正实现进AlphaBee。

---

# 二十九、Confidence和State必须分开

这是以后设计数据结构时非常重要的一点。

例如：

```text
state = S2
confidence = 0.82
```

和：

```text
state = S2
confidence = 0.55
```

完全不是一回事。

State描述：

> **投资生命周期在哪里。**

Confidence描述：

> **我们对这个状态判断有多确定。**

所以：

[
State\neq Score
]

---

# 三十、还有一个维度：State Velocity

这个我建议正式加入AlphaBee。

不仅记录：

[
State_t
]

还应该记录：

[
\Delta State
]

甚至：

[
StateVelocity
]

例如：

### A

```text
S1 → S2
用了6个月
```

### B

```text
S1 → S2 → S3
用了6周
```

B显然意味着：

[
EvidenceArrivalRate
]

非常高。

所以：

[
StateVelocity\uparrow
]

本身就是重要信号。

---

# 三十一、还需要一个“State Acceleration”

例如：

```text
Revision ↑
Revenue ↑
Margin ↑
RS ↑
```

同时加速。

可以定义：

[
StateMomentum
]

这就是我们分析工业富联时提到的：

[
S3Late\rightarrow S3Acceleration
]

也就是说：

> 状态不一定只能继续走向S4。

如果突然出现重大新产品周期：

[
ExpectationGap
]

重新打开。

完全可能：

[
S3Late\rightarrow S3Acceleration
]

甚至形成新的：

[
S1'
]

即：

> 第二增长曲线带来的新预期差周期。

---

# 三十二、所以现实中的生命周期不是一条直线

真正可能是：

```text
              ┌──── S3 Acceleration ────┐
              │                          │
S0 → S1 → S2 → S3 → S4 → Exit
      ↑    ↑    ↓
      │    └────┘
      └─────────
```

一家公司可以经历：

[
Cycle_1
]

然后因为：

> 新产品、新市场、新商业模式

进入：

[
Cycle_2
]

所以State必须和：

[
Thesis
]

绑定。

不能简单：

```text
company.state = S3
```

更合理：

```text
Thesis A = S4
Thesis B = S1
Company Aggregate State = S3
```

这个以后做AlphaBee会非常重要。

---

# 三十三、Sector State 和 Company State 也必须分开

沃尔核材就是非常好的例子。

AI高速铜缆产业：

[
SectorState\approx S3
]

但沃尔公司：

[
CompanyState\approx S2
]

因为：

[
RevenueStory
]

已经验证，

但：

[
ProfitStory
]

尚未完全验证。

所以：

[
\boxed{
SectorState\neq CompanyState
}
]

同样：

[
MarketState
]

也应该独立存在。

最终：

[
InvestmentState
===============

f(
MarketState,
SectorState,
CompanyState,
ThesisState
)
]

---

# 三十四、整个系统最重要的数据不是Report，而是Snapshot

每次重要事件后保存：

```yaml
date: 2026-08-24

state: S2
confidence: 0.72

fundamental:
  score: 4.1
  direction: improving

expectation:
  revision_1m: +4.2%
  revision_3m: +9.7%
  direction: improving

valuation:
  percentile: 32

trend:
  rs_20d: positive
  rs_60d: positive

theses:
  - id: H1
    probability: 0.75

  - id: H2
    probability: 0.61

key_unknowns:
  - Q3 margin
  - new product revenue

invalidation:
  - EPS revision turns negative
  - margin falls below X
```

然后真正有价值的是：

[
\boxed{
Snapshot_t-Snapshot_{t-1}
}
]

---

# 三十五、每次变化都应该回答“为什么”

例如：

```text
State:
S1 → S2

原因：

+ Q2收入恢复
+ 毛利率连续两个季度改善
+ 2027 EPS上修8%
+ 行业数据转正

负面：
- 海外需求仍然偏弱

Confidence:
58% → 72%
```

这比：

> “当前评分78，建议买入。”

有价值得多。

---

# 三十六、AlphaBee应该主动识别High Information Value Event

不是所有新闻都值得重新研究。

比如：

> 公司参加某行业论坛。

Information Value可能很低。

但是：

* 财报；
* 业绩预告；
* 新产品销售数据；
* 产品涨价；
* 竞争对手退出；
* 行业价格变化；
* 大客户Capex；
* EPS Revision；
* 监管变化；

可能直接改变：

[
State
]

所以应该估计：

[
\boxed{
EVI=
ExpectedValueOfInformation
}
]

EVI高：

→ 自动深度研究。

EVI低：

→ 只存Evidence。

这能极大减少Agent Token和搜索成本。

---

# 三十七、完整的 Exit Engine

最后不要把退出系统叫：

> Stop Loss。

更合理的是：

[
\boxed{ExitEngine}
]

包含五层：

### 1. Thesis Stop

核心假设证伪。

### 2. Evidence Stop

关键证据持续恶化。

### 3. Revision Stop

未来盈利预期持续下修。

### 4. State Stop

[
S3\rightarrow S2
]

等状态退化。

### 5. Price/Risk Stop

极端价格行为、趋势破坏或未知风险。

另外还有独立：

[
EmergencyRiskStop
]

处理：

* 财务造假；
* 审计异常；
* 重大监管；
* 黑天鹅；
* 核心管理层重大问题等。

---

# 三十八、Portfolio Risk必须独立于Company State

即使：

```text
股票A S2正常
股票B S3正常
股票C S2正常
```

也可能：

[
PortfolioRisk=TooHigh
]

因为它们可能全部暴露于：

[
AI\ Capex
]

或者：

[
SmallCapGrowth
]

或者：

[
LithiumCycle
]

因此最终仓位不是：

[
Position=f(StockState)
]

而是：

[
\boxed{
Position_i
==========

f(
StockState,
Confidence,
ExpectedReturn,
Risk,
Correlation,
PortfolioExposure,
MarketState
)
}
]

---

# 三十九、这样回头看我们分析过的股票

按照当前讨论中的状态，可以形成非常清晰的地图：

| 股票   | State                | 当前最重要的问题            |
| ---- | -------------------- | ------------------- |
| 迈瑞医疗 | **S1 Late→S2**       | 盈利和国内业务是否真正反转       |
| 沃尔核材 | **S2 Mid/Late**      | 高速铜缆能否转化成整体利润       |
| 恺英网络 | **S2 Late→S3 Early** | 2027增长是否具有持续性       |
| 鹏辉能源 | **S2 Late→S3 Early** | 单Wh利润和储能景气能否维持      |
| 工业富联 | **S3 Late→S4 Early** | 高Consensus下还能否继续超预期 |
| 兆易创新 | **S3 Late→S4 Early** | 超级存储景气是不是接近Peak     |

这张表其实已经比：

> “六只股票谁分数最高”

有意义很多。

---

# 四十、如果从“新资金”角度看

真正应该优先搜索：

[
\boxed{
S1Late\rightarrow S2Early
}
]

其次：

[
S2
]

然后：

[
S3Early
]

而对于：

[
S3Late/S4
]

除非出现：

> 新的Expectation Gap，

否则不应该因为：

> “公司基本面特别好”

就追进去。

这也是：

[
\boxed{
GoodCompany\neq GoodInvestment
}
]

在State Machine中的表达。

---

# 四十一、但这里必须增加一个重要修正：State不是买卖指令

这个一定要写进AlphaBee。

不能：

```python
if state == S2:
    buy()
```

因为：

[
State
]

只是描述投资生命周期。

最终决策应该是：

[
Decision
========

f(
State,
StateVelocity,
Confidence,
Valuation,
RiskReward,
Portfolio,
Market
)
]

例如两个都是S2：

### A

[
PE=15X
]

[
Confidence=80%
]

### B

[
PE=50X
]

[
Confidence=55%
]

当然不能给一样的仓位。

---

# 四十二、真正的决策核心应该是 Expected Value

最终还是：

[
EV
==

P_{bull}R_{bull}
+
P_{base}R_{base}
+
P_{bear}R_{bear}
]

再除以风险：

[
RiskAdjustedEV
]

State Machine最大的作用不是替代EV。

而是帮助判断：

[
P_{bull}
]

[
P_{base}
]

[
P_{bear}
]

如何随证据变化。

也就是：

[
Evidence
\rightarrow
BeliefUpdate
\rightarrow
ScenarioProbability
\rightarrow
ExpectedReturn
\rightarrow
Position
]

这才是最终成熟版本。

---

# 四十三、所以整个 AlphaBee 的核心闭环已经可以正式写成

[
\boxed{
Discover
\rightarrow
Hypothesis
\rightarrow
Evidence
\rightarrow
Belief
\rightarrow
State
\rightarrow
Valuation
\rightarrow
Decision
\rightarrow
Position
\rightarrow
Monitor
\rightarrow
Update
}
]

展开：

```text
发现异常/机会
        ↓
建立投资假设
        ↓
定义关键未知
        ↓
收集证据
        ↓
更新Hypothesis Probability
        ↓
更新Investment State
        ↓
重新计算Scenario / Valuation
        ↓
判断Risk / Reward
        ↓
调整Position
        ↓
等待High-EVI Event
        ↓
重新研究
        ↓
循环
```

这就是我认为现在 S1-S4 最完整的版本。

---

# 四十四、最终把整个体系浓缩成四句话

### S1

[
\boxed{\text{我可能发现市场错了，但我还没有足够证据。}}
]

**小仓试错，Thesis/Time Stop。**

### S2

[
\boxed{\text{现实开始证明我是对的，但市场还没有完全相信。}}
]

**这是最重要的开仓/加仓区域。**

### S3

[
\boxed{\text{市场开始和我站在一起，趋势和盈利形成共振。}}
]

**减少交易，允许波动，让利润奔跑。**

### S4

[
\boxed{\text{大家都知道它很好，现在的问题是还有多少没有Price In。}}
]

**停止恋战，从追求收益转向保护收益。**

最终：

[
\boxed{
S1:\ Think
\rightarrow
S2:\ Verify
\rightarrow
S3:\ Hold
\rightarrow
S4:\ Harvest
}
]

而贯穿整个过程的纪律是：

[
\boxed{
不是因为价格上涨证明自己正确，
也不是因为价格下跌证明自己错误；
而是不断问：
“相对于我建立仓位时，证据、预期和赔率究竟发生了什么变化？”
}
]

我认为到这里，这套东西已经足够成为 **AlphaBee 投资决策层的核心领域模型**，而不只是一个选股指标。下一步真正值得做的也不是继续增加 S5/S6，而是把 **`Thesis → Evidence → Belief Update → State Transition → Position Action`** 这条链的数据结构和状态迁移规则正式固化下来。
