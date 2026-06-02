# 成长质量与收入结构分析参考

## 触发场景

当用户提问包含以下意图时，使用本参考：

- 成长质量好不好 / 增长可不可持续
- 增收不增利 / 利润跑输收入
- 收入增长有没有含金量
- 扩张效率怎么样
- 是重资产还是轻资产增长
- 公司是在靠提效增长还是靠堆资产增长

---

## 核心规则组合

| 规则 | 回答什么问题 |
|------|------------|
| `profit_leverage` | 利润增速是否高于收入增速（盈利杠杆是否释放）？ |
| `market_share_change` | 收入增长是靠抢市场还是行业整体推动？ |
| `asset_turnover` | 资产利用效率是否在提升？ |
| `capex_intensity` | 增长需要持续大量资本投入吗？ |
| `inventory_pressure` | 收入增长是否伴随存货积压（是否真实卖出）？ |
| `receivable_pressure` | 收入增长是否伴随应收账款异常扩张（是否真实回款）？ |

---

## 成长质量四象限

结合 `profit_leverage` 与 `capex_intensity` 判断增长模式：

| | 资本支出低（light/moderate） | 资本支出高（heavy） |
|---|---|---|
| **利润杠杆释放**（leveraged） | 🟢 **高质量内生增长**：轻资产 + 利润弹性大，是最理想的成长模式（软件、品牌消费） | 🟡 **扩张型重资产增长**：重资产投入带动利润释放，需验证ROI，通常见于制造业景气期 |
| **利润被稀释**（diluted） | 🟡 **投入期增长**：收入增长但利润被费用吃掉，可能是市场投入期（需判断是否战略性亏损）| 🔴 **低效重资产扩张**：大量资本投入但利润不见改善，警惕无效扩张或产能过剩 |

---

## 收入真实性验证

高质量的收入增长应同时满足：

| 条件 | 检验规则 | 预期信号 |
|------|---------|---------|
| 利润增速不低于收入 | `profit_leverage` ≥ matched | 不发生增收不增利 |
| 应收账款不异常扩张 | `receivable_pressure` = low/medium | 回款质量有保障 |
| 存货不异常积压 | `inventory_pressure` = normal/depleting | 产品真实销售出去 |
| 资产使用效率改善 | `asset_turnover` = high 或 趋势提升 | 规模扩张伴随效率提升 |

若上述四个条件均满足，收入增长可信度高；若 `receivable_pressure` 和 `inventory_pressure` 同时出现黄/红信号，需怀疑收入质量。

---

## 典型异常模式识别

### 模式1：虚胖型增长
- `revenue_yoy` 高（如 30%+）
- `receivable_pressure` = high（应收账款快速积累）
- `cashflow_quality` = warning（现金流跟不上利润）
- 解释：收入增长可能来自激进的信用销售，真实回款存疑，警惕后续大额坏账。

### 模式2：通道型增长（低质量扩张）
- `revenue_yoy` 高
- `profit_leverage` = diluted（利润增速 << 收入增速）
- `capex_intensity` = heavy
- 解释：公司在靠大量资本投入和规模扩张推高收入，但单位盈利在下降，ROE 可能同步下行，增长的经济价值存疑。

### 模式3：去库存驱动的虚假改善
- `revenue_yoy` 看起来改善
- `inventory_pressure` = depleting（存货大幅消化）
- `gross_margin_trend` = contracting（毛利率收窄）
- 解释：公司可能在以低价清理库存推高收入，并非需求真实改善，毛利率下滑印证了这一点。

### 模式4：高质量复利增长（最优）
- `revenue_yoy` 稳定增长（15%-30%）
- `profit_leverage` = leveraged
- `capex_intensity` = light 或 moderate
- `receivable_pressure` = low
- `market_share_change` = gaining
- 解释：公司在抢市场份额的同时，利润弹性强于收入，轻资产模式现金流充裕，属于高质量复利增长。

---

## 解释原则

- **不要只说"增速高"**，要说明增速高的质量如何（利润跑赢还是跑输，现金流有没有跟上）
- **增收不增利**时，要进一步区分：是费用前置投入（战略性，可接受）还是竞争导致的利润侵蚀（危险）
- **capex_intensity** 高本身不是坏事，关键看资本回报（结合 `roe_level` 和 `asset_turnover` 判断ROI）
- 单季度数据存在季节性，建议用滚动12个月（TTM）口径进行趋势判断

---

## 行业差异注意事项

- **互联网 / SaaS**：`capex_intensity` 天然很低，但研发费用（计入费用）是真实"资本"，需单独关注
- **制造业 / 能源**：`capex_intensity` 高是行业特性，应与同行对比，而非与绝对阈值比较
- **零售 / 快消**：`inventory_pressure` 是最重要的早期信号，存货积压是需求走弱的领先指标
- **工程建设**：`receivable_pressure` 天然偏高，应重点关注同比趋势变化而非绝对水位
