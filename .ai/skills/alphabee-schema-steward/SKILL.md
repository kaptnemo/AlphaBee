---
name: alphabee-schema-steward
description: 当 AlphaBee 开发过程中涉及内部字段字典、Canonical Schema、Tushare/AkShare/Baostock 等数据源字段映射、facts 收集、adapter、derived facts、signals 或投资分析字段治理时使用本 Skill。它帮助持续维护 AlphaBee 的内部标准字段、数据源映射关系、命名规范、单位规范、血缘追踪和下游影响，避免业务逻辑直接依赖外部数据源字段名。
---

# AlphaBee 字段治理助手

## 目标

在 AlphaBee 的功能开发过程中，持续维护一套稳定、统一、可追踪的内部字段体系。

AlphaBee 的下游模块不应该直接依赖 Tushare、AkShare、Baostock 或其他外部数据源的字段名。

必须始终遵循：

```text
外部数据源字段
→ 数据源 Adapter / Mapping
→ AlphaBee 内部标准字段
→ Direct Facts / Derived Facts / Signals / Thesis
```

## 什么时候使用本 Skill

当任务涉及以下内容时，使用本 Skill：

- 新增、修改或删除 AlphaBee 内部字段
- 新增 Tushare、AkShare、Baostock 等数据源字段映射
- 编写或审查 facts 收集逻辑
- 编写或审查 adapter / normalizer
- 编写 derived fact 规则
- 编写 signal 或 thesis 相关逻辑
- 发现代码里直接使用外部数据源字段名
- 对齐不同数据源之间的字段语义、单位、频率、口径
- 设计财务、行情、公司、行业、估值、预期等字段结构

## 核心原则

### 1. 下游只认 AlphaBee 内部字段

允许：

```python
facts["financial"]["operating_cashflow"]
facts["financial"]["net_profit"]
facts["market"]["close_price"]
```

避免：

```python
row["n_cashflow_act"]
row["经营活动产生的现金流量净额"]
row["netOperateCashFlow"]
```

外部字段名只能出现在：

```text
adapters/
mapping files
source-specific fetcher
```

不能泄漏到：

```text
derived_facts/
signals/
thesis/
agents/
report writer
```

### 2. 内部字段是 AlphaBee 的系统语言

Tushare、AkShare、Baostock、Wind、东方财富等只是输入方言。

AlphaBee 自己的 canonical schema 才是系统内部语言。

### 3. 字段治理优先于快速拼接

不要为了快速完成一个功能，在下游代码中临时写：

```python
row.get("n_cashflow_act")
```

应该先完成字段映射：

```yaml
n_cashflow_act:
  canonical: operating_cashflow
```

然后下游统一使用：

```python
facts["financial"]["operating_cashflow"]
```

## 推荐目录结构

```text
alphabee/
  schemas/
    canonical_fields.yaml
    financial.yaml
    market.yaml
    company.yaml
    industry.yaml
    valuation.yaml
    expectation.yaml

  adapters/
    tushare/
      financial_mapping.yaml
      market_mapping.yaml
      company_mapping.yaml
      industry_mapping.yaml

    akshare/
      financial_mapping.yaml
      market_mapping.yaml
      company_mapping.yaml
      industry_mapping.yaml

    baostock/
      financial_mapping.yaml
      market_mapping.yaml
      company_mapping.yaml

  derived_facts/
    rules/

  signals/

  thesis/
```

## Canonical Field 定义格式

每个 AlphaBee 内部字段建议使用以下格式：

```yaml
operating_cashflow:
  category: financial
  type: float
  unit: CNY
  frequency:
    - quarterly
    - annual
  description: 经营活动产生的现金流量净额
  aliases:
    zh:
      - 经营现金流
      - 经营活动现金流净额
    en:
      - operating cash flow
      - cash flow from operations
  required_by:
    derived_facts:
      - cashflow_quality
    signals:
      - profit_quality
  source_mappings:
    tushare:
      - n_cashflow_act
    akshare:
      - 经营活动产生的现金流量净额
    baostock:
      - netOperateCashFlow
  notes: 用于衡量净利润是否有经营现金流支持。
```

字段定义至少包含：

```text
category
type
unit
frequency
description
source_mappings
```

## Mapping 文件格式

每个数据源单独维护 mapping。

示例：Tushare 财务字段映射

```yaml
source: tushare
domain: financial

fields:
  n_cashflow_act:
    canonical: operating_cashflow
    unit: CNY
    transform: none
    description: 经营活动产生的现金流量净额

  n_income:
    canonical: net_profit
    unit: CNY
    transform: none
    description: 净利润

  revenue:
    canonical: revenue
    unit: CNY
    transform: none
    description: 营业收入
```

示例：AkShare 财务字段映射

```yaml
source: akshare
domain: financial

fields:
  经营活动产生的现金流量净额:
    canonical: operating_cashflow
    unit: CNY
    transform: none
    description: 经营活动产生的现金流量净额

  净利润:
    canonical: net_profit
    unit: CNY
    transform: none
    description: 净利润

  营业总收入:
    canonical: revenue
    unit: CNY
    transform: none
    description: 营业总收入
```

示例：Baostock 财务字段映射

```yaml
source: baostock
domain: financial

fields:
  netOperateCashFlow:
    canonical: operating_cashflow
    unit: CNY
    transform: none
    description: 经营活动现金流净额

  netProfit:
    canonical: net_profit
    unit: CNY
    transform: none
    description: 净利润

  totalRevenue:
    canonical: revenue
    unit: CNY
    transform: none
    description: 营业总收入
```

## 字段新增流程

当开发中需要新增字段时，按以下流程处理：

1. 先检查是否已有语义相同或高度相似的 canonical field。
2. 如果已有字段，只新增数据源 mapping，不新增内部字段。
3. 如果没有字段，新增一个 AlphaBee canonical field。
4. 字段名使用稳定英文 snake_case。
5. 补充 category、type、unit、frequency、description、aliases、source_mappings。
6. 更新相关数据源 mapping 文件。
7. 检查 derived facts 是否需要把该字段加入 `required_facts`。
8. 检查 signals / thesis / agents 是否受影响。
9. 确保下游代码只使用 canonical field。
10. 输出 schema 更新摘要和下游影响。

## 字段命名规范

使用英文 snake_case。

推荐：

```yaml
operating_cashflow
net_profit
revenue
accounts_receivable
inventory
gross_margin
close_price
pe_ttm
pb_lf
market_cap
industry_name
```

避免：

```yaml
n_cashflow_act
经营现金流
cashflow
netProfit
净利润
closePrice
```

## 单位规范

必须明确单位。

常见单位：

```yaml
CNY: 人民币元
CNY_10K: 人民币万元
CNY_100M: 人民币亿元
PERCENT: 百分比，例如 12.5 表示 12.5%
RATIO: 比率，例如 0.125 表示 12.5%
SHARE: 股
VOLUME: 成交量
DATE: 日期
```

如果外部数据源单位不同，必须在 adapter 层完成转换。

例如：

```yaml
reg_capital:
  canonical: registered_capital
  source_unit: CNY_10K
  canonical_unit: CNY
  transform: multiply_by_10000
```

下游不要处理单位换算。

## 频率与期间规范

字段需要明确适用频率：

```yaml
frequency:
  - daily
  - weekly
  - monthly
  - quarterly
  - annual
```

财务字段必须带期间信息：

```yaml
period: 2024Q4
report_type: annual
```

行情字段必须带交易日期：

```yaml
trade_date: 2026-06-03
```

## 缺失值处理

Adapter 输出时必须区分：

```yaml
null: 原始数据缺失或无法取得
0: 明确为零
```

不要把缺失值转换成 0。

推荐输出：

```yaml
value: null
missing_reason: source_field_missing
```

## 血缘追踪

标准化后的 facts 应保留 source metadata。

推荐结构：

```yaml
facts:
  financial:
    operating_cashflow:
      value: 1200000000
      unit: CNY
      period: 2024Q4
      source: tushare
      source_field: n_cashflow_act

    net_profit:
      value: 1000000000
      unit: CNY
      period: 2024Q4
      source: tushare
      source_field: n_income
```

如果为了计算方便，也可以在内部生成 flat values：

```python
fact_values = {
    "operating_cashflow": 1200000000,
    "net_profit": 1000000000,
}
```

但 flat values 不应丢失原始 metadata。

## Adapter 编写规则

Adapter 的职责：

```text
读取外部 raw record
→ 根据 mapping 转换字段名
→ 统一单位
→ 处理缺失值
→ 附带 source metadata
→ 输出 AlphaBee canonical facts
```

Adapter 不负责：

```text
投资判断
Derived Fact 推导
Signal 判断
报告写作
```

## Derived Fact 依赖规则

Derived Fact 规则只能依赖 canonical fields。

推荐：

```yaml
name: cashflow_quality
required_facts:
  - operating_cashflow
  - net_profit
formula: operating_cashflow / net_profit
```

避免：

```yaml
name: cashflow_quality
required_facts:
  - n_cashflow_act
  - n_income
formula: n_cashflow_act / n_income
```

## Code Review 检查清单

审查数据相关改动时，逐项检查：

- 是否新增了外部字段名？
- 外部字段名是否只出现在 adapter / mapping 层？
- 是否已有语义相同的 canonical field？
- 是否新增了重复字段？
- 新字段是否有 category、type、unit、frequency、description？
- 是否补齐 Tushare / AkShare / Baostock 的映射？
- 单位是否统一？
- 缺失值是否被正确保留，而不是误转为 0？
- period / trade_date 是否清晰？
- derived facts 是否只使用 canonical fields？
- source metadata 是否保留？
- 下游模块是否需要同步更新？

## 输出格式

当协助开发或 review 时，优先输出以下结构：

```yaml
schema_updates:
  canonical_fields_added:
    - name:
      category:
      type:
      unit:
      description:

  canonical_fields_modified:
    - name:
      change:

  source_mappings_added:
    - source:
      source_field:
      canonical:
      transform:

  source_mappings_modified:
    - source:
      source_field:
      change:

code_changes_needed:
  - file:
    change:

validation_notes:
  - note:

downstream_impact:
  derived_facts:
  signals:
  agents:
  reports:
```

## 常见场景

### 场景一：新增 Tushare 字段

如果用户说：

```text
我想用 Tushare 的 n_cashflow_act 做现金流质量分析
```

应建议：

```yaml
canonical_field: operating_cashflow
source_mapping:
  source: tushare
  source_field: n_cashflow_act
  canonical: operating_cashflow
required_by:
  - cashflow_quality
```

不要建议下游直接读取 `n_cashflow_act`。

### 场景二：发现多个数据源字段语义相同

例如：

```text
Tushare: n_income
AkShare: 净利润
Baostock: netProfit
```

应统一到：

```yaml
net_profit
```

### 场景三：字段名语义不清

如果字段名过于宽泛，例如：

```yaml
cashflow
profit
growth
```

应建议改为更明确的字段：

```yaml
operating_cashflow
net_profit
revenue_growth_yoy
```

### 场景四：单位不一致

如果一个数据源返回万元，另一个返回元，应在 adapter 层统一到 canonical unit。

不要让 downstream 自己判断单位。

## 最重要的判断

任何数据字段进入 AlphaBee 之前，都要回答四个问题：

```text
1. 这个字段在 AlphaBee 内部叫什么？
2. 这个字段的单位、频率、期间口径是什么？
3. 它来自哪个数据源的哪个原始字段？
4. 哪些 derived facts / signals / agents 会依赖它？
```
