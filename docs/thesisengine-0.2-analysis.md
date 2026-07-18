# ThesisEngine 0.2：显式消费 anomaly/conflict 的设计解读

这里对应的是 **`ROADMAP.md` 的 0.2**，不是 README。它的核心意思是：

> **ThesisEngine 不能只看 `signal_results` 做机械聚合，而要把 anomaly / conflict / verification / company_context 这些“解释层证据”显式纳入论点生成。**

---

## 1. 当前现状

当前 `ThesisEngine.run()` 的签名还是：

```python
ThesisEngine.run(
    symbol,
    period,
    signal_results,
)
```

它现在做的事情很纯：

1. 遍历每条 signal
2. 读取 `level`
3. 读取 `thesis_impact`
4. 把信号按 thesis 维度分组
5. 用 `level_score × impact_direction` 求平均
6. 得出每个维度的 `judgment / score / evidence`

也就是说，**当前 ThesisEngine 本质上是一个“信号聚合器”**。

它知道：

- 哪条 signal 是 high / medium / low
- 这条 signal 对 `financial_quality` 还是 `earnings_quality` 有负面影响

但它**不知道**：

- 这个 signal 背后是不是一个已经验证过的高严重度冲突
- 这个异常是不是“虚增收入”这种更强的二阶模式
- 某个假设是不是已经被验证为 rejected / unknown
- 这个公司本来就是重资产、项目制、应收天然偏高，还是这种表现真的异常

所以它容易得出这种结果：

```text
earnings_quality = negative
financial_quality = neutral
credit_risk = slightly_negative
```

但得不出真正像分析师的话：

```text
公司的核心矛盾不是“有几个负面信号”，
而是“收入和利润仍在增长，但回款与勾稽关系已经明显恶化，
且这种恶化已经形成可验证冲突，因此增长质量要打折”。
```

---

## 2. 0.2 想解决的是什么问题

ROADMAP 里这段：

```python
ThesisEngine.run(
    symbol,
    period,
    signal_results,
    anomaly_report=None,
    conflict_analysis=None,
    verification_results=None,
    company_context=None,
)
```

本质是在说：

**thesis 不能只由 signal 决定，还要由“被解释过的证据”决定。**

也就是把 ThesisEngine 从：

```text
signal aggregator
```

升级成：

```text
evidence-aware judgment engine
```

---

## 3. 四类新增输入各自解决什么

### 3.1 `anomaly_report`

作用：把**异常模式本身**直接变成 thesis 证据，而不只是 signal 的上游原料。

比如现在已经做了一部分：

- anomaly 先进入 signal
- signal 再影响 thesis

但这还是一层间接映射。

0.2 想要的是更进一步：

```text
anomaly_pattern = inflated_revenue
```

不只是触发一个 `signal=negative`，而是直接成为 thesis evidence：

```json
{
  "source": "anomaly_pattern:inflated_revenue",
  "dimension": "earnings_quality",
  "stance": "negative",
  "reason": "收入增长没有被现金流和回款质量支撑"
}
```

这样 thesis 里就能直接出现更强的证据表述，而不是只有抽象的 signal id。

### 3.2 `conflict_analysis`

作用：把**已识别的背离/矛盾**直接作用到 thesis。

冲突和 signal 的区别是：

- signal 更像“局部风险提示”
- conflict 更像“这些事实之间出现了结构性矛盾”

例如：

```text
净利润增长
但经营现金流下降
同时应收账款天数拉长
```

signal 可能分别给出：

- revenue_quality_risk
- cashflow_quality_risk

但 conflict 会把它们组合成一句更强的话：

```text
利润表显示增长，但现金流与营运资本并未验证这种增长。
```

这类冲突一旦是 `high/critical`，就不该只作为旁注，而应该**下调相关 thesis 维度**。

ROADMAP 里这句很关键：

> 已验证 high/critical conflict 可以下调相关维度

意思就是：

- 如果 conflict 已经够强
- 它应该成为 thesis score 的直接负项
- 而不是只存在 `conflict_data` artifact 里，最后没人真正消费

### 3.3 `verification_results`

作用：把“假设被证实 / 被否定 / 暂时未知”的结果写进 thesis。

这部分最像分析过程中的“证据状态管理”。

ROADMAP 里提了两个重要目标：

#### `rejected hypotheses` 作为反向证据进入 thesis

比如系统原先怀疑：

```text
是不是因为行业景气下行导致毛利率下降？
```

后续验证发现：

- 同行业毛利率并没恶化
- 只有这家公司恶化

那这个假设就被 `rejected`。

这时 thesis 不该只保留原来负面信号，而应该增加一个更强的判断：

```text
“行业原因”这个解释不成立，因此公司自身经营问题的解释权重上升。
```

也就是 **rejected hypothesis 是反证，不是垃圾信息**。

#### `unknown hypotheses` 进入 missing evidence

如果某个关键问题还没验证出来，比如：

```text
应收恶化究竟来自大客户账期拉长，还是渠道压货？
```

那 thesis 应该显式保留“不确定性”，而不是装作已经判断完了。

也就是 thesis 应该能写出：

```text
当前判断偏负面，但关键缺失证据仍包括：
- 应收账龄结构变化
- 前五大客户回款集中度
- 信用政策是否明显放宽
```

这会让 thesis 更像“可证伪观点”，而不是“强行下结论”。

### 3.4 `company_context`

作用：避免 thesis 对相同信号做机械、跨行业失真的解释。

这部分非常重要，因为同样一个异常，在不同商业模式下含义不同。

比如：

- 白酒企业应收大增：通常很异常
- 军工企业应收大增：可能和结算节奏有关
- 软件项目制企业应收大增：可能和验收周期有关
- 医药流通应收重：行业特征，不一定代表造假

所以 thesis 不应该只说：

```text
accounts_receivable risk = negative
```

而应该结合 `company_context` 判断：

```text
该公司属于 project-based / receivable-heavy 模式，
因此应收偏高本身不是最强负面证据；
真正异常的是“应收增速持续快于收入 + 现金流未同步改善”。
```

也就是说，`company_context` 让 thesis 从：

```text
看到异常就扣分
```

变成：

```text
先判断这个异常在该商业模式下是否合理，再决定扣多少分
```

---

## 4. 为什么一定要“显式消费”

“显式消费”的意思不是把这些数据挂在 artifact 里，而是：

- **接口上收进来**
- **逻辑上真参与打分/构造 evidence**
- **输出里能看到它们的影响**

当前系统里，`run_thesis` 节点其实已经拿到了部分信息：

- `anomaly_report`
- `company_context`
- `_build_conflict_data(state)`

但这些目前主要是：

- 被放进 `thesis_analysis` artifact
- 或者给 enhancer 用
- **没有进入 `ThesisEngine.run()` 的核心判断逻辑**

这就叫“有数据，但没被显式消费”。

---

## 5. 0.2 期望的目标状态

如果真正落地 0.2，ThesisEngine 应该至少做三件新增的事。

### 5.1 扩展 evidence 来源

现在 evidence 几乎都长这样：

```json
{
  "signal_id": "cross_validation_break",
  "level": "high",
  "impact": "negative",
  "interpretation": "..."
}
```

后面应该扩展成多来源：

- signal evidence
- anomaly evidence
- conflict evidence
- verification evidence
- missing evidence

也就是 thesis 不再只是一堆 signal 的解释，而是一个更完整的证据篮子。

### 5.2 冲突/验证结果直接改写维度判断

例如：

- `financial_quality` 原本根据 signal 聚合是 `neutral`
- 但有一个 `critical conflict` 已被验证成立
- 那这个维度应被下调到 `negative` 甚至 `strong_negative`

这一步不是“生成更好文案”，而是**改变 judgment 本身**。

### 5.3 输出不只给结论，还给“证据结构”

最终 thesis 更理想的样子不是：

```json
{
  "financial_quality": "negative"
}
```

而是更像：

```json
{
  "financial_quality": {
    "judgment": "negative",
    "supporting_evidence": [...],
    "counter_evidence": [...],
    "missing_evidence": [...],
    "confidence": 0.62
  }
}
```

这也是为什么 0.2 和后面的 Phase 1 / Phase 3 是连着的：

- 0.2 先让 ThesisEngine 真吃到 anomaly/conflict/verification
- 后面才能自然过渡到 InsightAgent 和 claim-evidence graph

---

## 6. 用一句话概括 0.2

**0.2 的本质不是“给 ThesisEngine 多传几个参数”，而是把 thesis 从“信号平均器”升级成“基于异常、冲突、验证结果和公司背景的证据判断器”。**

---

## 7. 对照当前代码，可以这样理解“已实现到哪一步”

### 已有

- anomaly 已经能先进入 signal
- 部分二阶 anomaly pattern 已经能直接映射到 thesis 维度
- `run_thesis` 节点已经能拿到 `anomaly_data / conflict_data / company_context`

### 还没真正完成 0.2 的地方

- `ThesisEngine.run()` 签名还没扩
- `conflict_analysis` 还没直接改写 thesis 分数
- `verification_results` 还没进入 counter-evidence / missing-evidence
- `company_context` 还没参与维度加权逻辑，只是在外围 artifact/enhancer 层

所以现在更准确地说是：

> **0.1 已经有可用版本，0.2 还只完成了外围数据准备，核心消费逻辑尚未真正进入 ThesisEngine。**
