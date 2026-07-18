---
name: derived-facts
description: 将原始财务与市场事实转化为结构化衍生信号的分析技能。基于17条预定义规则（覆盖盈利能力、成长质量、偿债能力、运营效率、估值匹配、股东回报、风险七个维度），对股票进行多维度量化诊断，输出有明确档位判断和业务解释的衍生事实。
version: 1.0.0
---

# derived-facts

把原始财报数字与市场行情，转化为"有结论"的衍生分析信号。

---

## What this skill is for

这个 skill 的核心职责是：**用规则把数据变成结论**。

原始数据（净利润、营收、PE、负债率……）本身不能直接回答"公司财务健不健康"这类问题。这个 skill 通过预定义规则，把原始字段计算成有档位判断（excellent / warning / risky 等）的衍生指标，并附上可读的业务解释。

典型使用场景：

- 判断一家公司的盈利质量是否扎实
- 评估财务结构的安全边际
- 衡量成长是否伴随效率提升
- 判断当前估值是否与基本面匹配
- 识别收入结构或存货方面的潜在风险
- 综合给出"多维度信号灯"诊断

---

## 17条规则索引

### 盈利能力
| 规则 | 公式 | 核心问题 |
|------|------|---------|
| `roe_level` | net_profit / avg_shareholders_equity | 股东权益回报率是否足够？ |
| `gross_margin_trend` | gross_margin_current − gross_margin_prev | 毛利率在扩张还是收窄？ |

### 成长质量
| 规则 | 公式 | 核心问题 |
|------|------|---------|
| `profit_leverage` | net_profit_yoy − revenue_yoy | 利润增速是否显著高于收入增速？ |
| `market_share_change` | revenue_yoy − industry_revenue_yoy | 公司是在抢市场份额还是丢份额？ |

### 偿债能力
| 规则 | 公式 | 核心问题 |
|------|------|---------|
| `debt_ratio` | total_liabilities / total_assets | 整体杠杆水平是否合理？ |
| `interest_coverage` | EBIT / interest_expense | 利息能否轻松覆盖？ |
| `current_ratio` | current_assets / current_liabilities | 短期偿债能力是否充足？ |

### 运营效率
| 规则 | 公式 | 核心问题 |
|------|------|---------|
| `inventory_pressure` | inventory_yoy − revenue_yoy | 存货是否在积压？ |
| `asset_turnover` | revenue / total_assets | 资产利用效率如何？ |
| `capex_intensity` | capex / revenue | 是轻资产还是重资产模式？ |

### 估值匹配
| 规则 | 公式 | 核心问题 |
|------|------|---------|
| `peg_ratio` | pe_ttm / net_profit_yoy | 成长溢价是否合理？ |
| `pb_roe_match` | pb_ratio / (roe × 100) | 按盈利能力衡量估值是否偏贵？ |
| `valuation_compression` | pe_ttm / pe_ttm_5y_avg | 当前估值是否被历史压缩？ |

### 股东回报 / 现金质量
| 规则 | 公式 | 核心问题 |
|------|------|---------|
| `cashflow_quality` | operating_cashflow / net_profit | 利润有没有现金支撑？ |
| `receivable_pressure` | accounts_receivable / revenue | 应收账款是否过高？ |
| `dividend_coverage` | operating_cashflow / dividends_paid | 分红是否可持续？ |

### 风险
| 规则 | 公式 | 核心问题 |
|------|------|---------|
| `goodwill_risk` | goodwill / shareholders_equity | 商誉减值风险有多大？ |

---

## Intent → 规则映射

根据用户问题类型，优先选择对应规则组合：

### "财务质量 / 利润有没有水分 / 基本面健不健康"
→ 见 `references/financial_quality.md`
核心规则：`cashflow_quality` + `receivable_pressure` + `inventory_pressure` + `gross_margin_trend`

### "成长性怎么样 / 增长是否高质量 / 增收不增利"
→ 见 `references/revenue_decomposition.md`
核心规则：`profit_leverage` + `market_share_change` + `asset_turnover` + `capex_intensity`

### "估值合不合理 / 现在贵不贵 / 是否被低估"
→ 见 `references/market_expectation.md`
核心规则：`peg_ratio` + `pb_roe_match` + `valuation_compression`

### "行业景气 / 市场份额 / 竞争格局"
→ 见 `references/industry_cycle.md`
核心规则：`market_share_change` + `gross_margin_trend` + `roe_level`

### "财务风险 / 债务压力 / 资金链"
核心规则：`debt_ratio` + `interest_coverage` + `current_ratio` + `cashflow_quality`

### "分红能否持续 / 股东回报质量"
核心规则：`dividend_coverage` + `cashflow_quality` + `roe_level`

### "并购风险 / 商誉 / 外延扩张"
核心规则：`goodwill_risk` + `cashflow_quality` + `debt_ratio`

---

## 使用方式：Function Call

直接调用以下两个工具函数，无需手动计算公式或对照阈值。

### 第一步：确认规则与所需字段

```
list_derived_fact_rules()
```

返回所有 17 条规则的名称、描述和 `required_facts` 字段清单。
在准备 `fact_values` 前先调用，确保字段名拼写正确。

---

### 第二步：调用规则计算

```
evaluate_derived_facts(
    rule_names=["cashflow_quality", "receivable_pressure", "inventory_pressure"],
    fact_values={
        "operating_cashflow": 1200,
        "net_profit": 1000,
        "accounts_receivable": 800,
        "revenue": 5000,
        "inventory": 1200,
        "inventory_prev": 900
    }
)
```

引擎自动完成：公式计算 → 阈值匹配 → 业务解释映射，返回 Markdown 格式报告。

**字段缺失时**：该规则被跳过，报告中标注"数据不可用"，其他规则正常计算。
**规则名错误时**：报告中标注"未知规则"，不影响其他规则。

---

### 标准流程

1. **识别问题维度** → 从下方"Intent → 规则映射"选出规则组合
2. **调用 `list_derived_fact_rules()`** → 确认所需字段名称（可选，熟悉后可跳过）
3. **收集字段值** → 从已有事实数据中提取对应字段，组装 `fact_values` 字典
4. **调用 `evaluate_derived_facts()`** → 传入规则名列表和字段字典
5. **综合输出** → 按维度分组展示结果，最后给出整体信号摘要（2-3句话）

---

## 输出格式约定

`evaluate_derived_facts` 已返回格式化的 Markdown 报告，直接使用即可。
综合多条规则时，在报告末尾追加 **总体信号摘要**（2-3句话），例如：

> **总体信号**：现金流质量优秀，利润含金量高；应收账款占比偏高需持续关注；
> 存货增速与营收匹配，无明显积压风险。整体财务质量处于**绿色**区间。

---

## 这个 skill 不做的事

- 不给出"买入 / 卖出 / 持有"的投资建议
- 不预测股价涨跌
- 不替代行业专家对特殊商业模式的判断（金融、房地产等需单独参考行业基准）
- 若所需字段数据缺失，明确标注"数据不可用"，不做估算填充
