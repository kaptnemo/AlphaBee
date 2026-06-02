# 财务质量分析参考

## 触发场景

当用户提问包含以下意图时，使用本参考：

- 利润质量 / 盈利质量
- 基本面是否健康
- 财务有没有水分 / 会计造假风险
- 现金流质量怎么样
- 应收账款有没有问题
- 存货是否积压
- 毛利率趋势如何

---

## 核心规则组合

| 优先级 | 规则 | 检测什么风险 |
|--------|------|------------|
| ★★★ | `cashflow_quality` | 净利润有没有现金支撑（防范利润虚增） |
| ★★★ | `receivable_pressure` | 应收账款是否过度扩张（防范收入虚增） |
| ★★ | `inventory_pressure` | 存货是否异常堆积（防范跌价减值风险） |
| ★★ | `gross_margin_trend` | 毛利率是否被侵蚀（竞争格局与成本传导）|

---

## 组合诊断逻辑

### 高质量财务（绿灯）
- `cashflow_quality` = excellent（≥ 1.0）
- `receivable_pressure` = low（< 0.15）
- `inventory_pressure` = normal
- `gross_margin_trend` = expanding 或 stable

**解释**：经营现金流充分支持净利润，账款和存货管理健康，盈利质量扎实。

### 潜在水分信号（黄灯）

以下任意组合出现时需重点关注：

1. `cashflow_quality` = warning（< 0.8）+ `receivable_pressure` = medium 或 high
   → 净利润未能转化为现金，且应收账款快速积累。"账面挣钱、口袋没钱"的典型信号，需排查是否存在收入虚增或客户回款恶化。

2. `inventory_pressure` = accumulating + `gross_margin_trend` = contracting
   → 产品卖不出去同时毛利率下滑，可能面临被迫降价去库存，后续利润压力较大。

3. `cashflow_quality` = warning + `inventory_pressure` = accumulating
   → 现金流和库存双重恶化，经营质量明显下降。

### 高风险财务（红灯）

- `cashflow_quality` < 0.5（经营现金流不足净利润50%）
- `receivable_pressure` > 0.30
- `inventory_pressure` = accumulating（库存增速超过收入20pp以上）

三项同时触发时，财务造假或经营急剧恶化的概率显著上升，需要结合审计意见、关联交易、历史数据交叉验证。

---

## 解释原则

- **不要只说"现金流差"**，要说明：净利润有多少比例被经营现金流支持（给出具体数字）
- **不要只说"应收账款高"**，要说明：占营收多少比例，与历史相比是扩大还是收窄
- **存货积压**要说明：库存增速超过收入增速多少个百分点，可能的下游影响是什么
- **毛利率收窄**要说明：收窄幅度（百分点），可能是原材料涨价、产品降价还是结构变化

---

## 行业差异注意事项

- **建筑 / 工程 / 软件**：应收账款天然较高，`receivable_pressure` = medium 为正常，high 才告警
- **零售 / 消费**：库存周转快，`inventory_pressure` 基准应更严格
- **制造业**：毛利率普遍偏低（5%-20%），`gross_margin_trend` 变化的绝对值意义更大
- **金融行业**：以上规则不适用，需使用专项指标（NIM、不良率等）
