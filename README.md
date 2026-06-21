# 🐝 AlphaBee

**AlphaBee** 是一个面向 A 股市场的财报质量体检与勾稽关系异常检测系统。基于 LangGraph + DeepAgents 构建，将个股分析拆解为 **事实采集 → 衍生指标 → 信号检测 → 异常发现 → 论点生成 → 报告输出** 的分层确定性流水线，辅以可选的 LLM 增强层进行语境化解读。

[![Python](https://img.shields.io/badge/Python-3.13+-blue.svg)](https://www.python.org/)
[![Poetry](https://img.shields.io/badge/dependency-Poetry-cyan.svg)](https://python-poetry.org/)
[![LangGraph](https://img.shields.io/badge/LangGraph-1.2-purple.svg)](https://langchain-ai.github.io/langgraph/)
[![DeepAgents](https://img.shields.io/badge/DeepAgents-0.6-indigo.svg)](https://pypi.org/project/deepagents/)
[![Tushare](https://img.shields.io/badge/Tushare-1.4-red.svg)](https://tushare.pro/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)

---

## 核心能力

- **分层确定性流水线**：事实 → 衍生指标(21 条) → 信号(9 条) → 勾稽关系异常(10+8) → 论点 → 审查 → 报告，每层可独立运行和测试
- **《手把手教你读财报》框架**：10 条一阶勾稽关系 z-score 检测 + 8 个二阶异常模式（虚增收入、存货异常、大存大贷、折旧调节等），每条异常附带附注排查路径
- **多期趋势基线**：AnomalyEngine 基于近 4 期历史基线 (μ±σ) 检测指标偏离，区分偶然波动与真实异常
- **行业语境校准**：从 Tushare 提取权威行业分类，审查层对金融/医药/半导体等高杠杆或高研发行业做阈值调整
- **YAML 驱动的规则引擎**：所有衍生指标、信号、勾稽关系规则均为声明式 YAML，支持拓扑排序依赖解析与安全 AST 公式求值
- **统一字段适配层**：Adapter + Schema Registry 将 Tushare/AkShare 原始字段映射为 AlphaBee 规范字段名（7 大领域、125+ 字段）
- **可观测性**：Langfuse 全链路追踪 + structlog 结构化日志
- **交互式 CLI**：单次查询 / 多轮对话，支持 --enhance / --llm-review 可选增强

---

## 架构概览

```
main.py (CLI)
  └─ Orchestrator (StateGraph, 确定性)
       ├─ collect_facts           ← 事实采集 + 结构化建模
       │   ├─ FactCollectorAgent  (LLM, 8 工具)
       │   └─ FinancialFacts / MarketFacts  (Pydantic)
       │
       ├─ DerivedFacts            (确定性引擎, 21 条 YAML)
       │   └─ 拓扑排序 DAG → safe_eval AST 求值
       │
       ├─ SignalEngine            (确定性引擎, 9 条 YAML)
       │   └─ 严重度顺序求值 → thesis_impact 映射
       │
       ├─ AnomalyEngine ★         (确定性引擎, 10 + 8 YAML)
       │   ├─ 一阶: z-score 基线偏离检测
       │   └─ 二阶: 多异常模式匹配 → 根因假设
       │
       ├─ ThesisEngine            (确定性加权评分)
       │   └─ 3 维度 × N 信号 → 综合判断
       │
       ├─ review_thesis           (确定性 + 可选 LLM)
       │   └─ 证据充分性 / 信号一致性 / 语境适配
       │
       └─ generate_report → finalize
           (单次 LLM, 结构化 → Markdown)
```

### 流水线各层规则统计

| 层级 | 引擎 | 规则数 | 类型 |
|------|------|--------|------|
| 派生指标 | DerivedFacts | 21 | 盈利/成长/偿债/效率/估值/现金流/风险 |
| 信号检测 | SignalEngine | 9 | 收入质量/现金流/债务/盈利/增长/扩张/估值/异常聚集/勾稽断裂 |
| 勾稽关系 | AnomalyEngine | 10 + 8 | z-score 检测 + 模式匹配 |
| 投资论点 | ThesisEngine | 3 | 财务质量/盈利质量/信用风险 |

---

## 快速开始

**环境要求**：Python `>=3.13`，[Poetry](https://python-poetry.org/)

```bash
git clone https://github.com/captainemo/AlphaBee.git
cd AlphaBee
poetry install
```

### 基础使用

```bash
# 单次分析
poetry run python main.py "帮我分析一下宁德时代"

# 多轮对话
poetry run python main.py --chat

# 启用 LLM 增强层（跨信号模式 + 行业语境化）
poetry run python main.py --enhance "分析 600519.SH"

# 全开：增强层 + LLM 审查
poetry run python main.py --enhance --llm-review "分析比亚迪"

# 关闭颜色输出
poetry run python main.py --no-color "分析 000858.SZ"
```

### 多轮对话命令

| 命令 | 说明 |
|------|------|
| 直接输入问题 | 继续追问 |
| `/clear` | 清空上下文 |
| `/exit` | 退出会话 |

---

## 配置

项目根目录 `config.yaml`，支持 `${ENV_VAR}` 和 `${ENV_VAR:default}` 占位符：

```yaml
llm:
  api_key: "${LLM_API_KEY}"
  base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
  model: "qwen3.6-plus"

web_search:
  tavily:
    api_key: "${TAVILY_API_KEY:}"
  ddgs:
    region: "cn-zh"

tushare:
  api_key: "${TUSHARE_TOKEN:}"
```

| 环境变量 | 说明 | 必须 |
|----------|------|------|
| `LLM_API_KEY` | 大模型 API 密钥 | ✅ |
| `TUSHARE_TOKEN` | Tushare 数据 Token | ✅（大部分） |
| `TAVILY_API_KEY` | Tavily 搜索 API 密钥 | 可选 |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | Langfuse 可观测性 | 可选 |

---

## 流水线详解

### 1. 事实采集 (FactCollector)

`FactCollectorAgent` 通过 8 个领域工具采集客观数据，输出 Pydantic 结构化模型：

| 工具 | 采集内容 |
|------|---------|
| `get_company_profile` | 公司基本信息、股东结构 |
| `get_financial_fact` | 多期利润表/资产负债表/现金流量表/财务比率（最多 20 期） |
| `get_operation_fact` | 主营业务构成（产品/地区拆分） |
| `get_industry_fact` | 申万行业分类、行业指数PE/PB |
| `get_competition_fact` | 同行竞争对手关键指标对比 |
| `get_market_fact` | 行情、PE/PB、资金流向、均线 |
| `get_expectation_fact` | 业绩预告、业绩快报 |
| `get_risk_fact` | 新闻舆情、股权质押、回购 |

输出模型 `FinancialFacts`（41 字段 × 多期）和 `MarketFacts` 通过 `.to_fact_values()` 展平为 `dict[str, float]`，供下游引擎消费。

### 2. 衍生指标 (DerivedFacts)

21 条 YAML 规则，按利润表→资产负债表→现金流量表的依赖关系做拓扑排序：

```
盈利能力：  roe_level, gross_margin_trend
成长质量：  revenue_growth, profit_leverage, market_share_change
偿债能力：  debt_ratio, interest_coverage, current_ratio
运营效率：  inventory_pressure, asset_turnover, capex_intensity
估值匹配：  peg_ratio, pb_roe_match, valuation_compression
现金流：    cashflow_quality, receivable_pressure, receivable_growth_gap,
            accounts_receivable_growth, accounts_receivable_yoy, dividend_coverage
风险：      goodwill_risk
```

公式使用安全 AST 求值——仅算术和比较运算符：
```yaml
# 例: cashflow_quality.yaml
formula: "operating_cashflow / net_profit"
thresholds:
  excellent: "value >= 1.0"
  normal:    "0.8 <= value < 1.0"
  warning:   "value < 0.8"
```

### 3. 信号检测 (SignalEngine)

9 条 YAML 规则，基于衍生指标 + 原始事实做严重度分级触发：

| 信号 | 触发逻辑 | 覆盖维度 |
|------|---------|---------|
| `revenue_quality_risk` | 应收/营收增速差值 | 财务质量 + 盈利质量 |
| `cashflow_quality_risk` | 经营现金流/净利润 < 阈值 | 财务质量 + 盈利质量 |
| `debt_risk` | 负债率 > 阈值且流动比率低 | 财务质量 + 信用风险 |
| `profitability_quality_risk` | ROE + 毛利率趋势 | 财务质量 + 盈利质量 |
| `growth_quality_risk` | 应收/营收差距 + 利润杠杆 | 财务质量 + 盈利质量 |
| `expansion_risk` | 商誉 + 负债率 + 资本支出 | 财务质量 + 信用风险 |
| `valuation_risk` | PEG + 估值压缩 + PB-ROE 匹配 | 财务质量 |
| `anomaly_cluster_risk` | 2+ 个异常模式触发 | 财务质量 |
| `cross_validation_break` | 最强异常 \|z\| > 2.5σ | 财务质量 + 盈利质量 |

每条信号携带 `thesis_impact`（影响方向+维度）和 `critic_questions`（拷问清单），直接驱动下游论点引擎。

### 4. 勾稽关系异常检测 (AnomalyEngine) ★

基于《手把手教你读财报》框架，两步检测：

**一阶：10 条勾稽关系 z-score 检查**

每条规则取近 4 期历史基线（μ±σ），检测本期是否显著偏离：

| 勾稽关系 | 检测内容 |
|---------|---------|
| 应收/营收背离 | 应收账款增速是否远超营收增速 |
| 存货/营收背离 | 存货增速是否远超营收增速 |
| 现金流/利润背离 | 经营现金流是否远低于净利润 |
| 毛利/费用背离 | 毛利率与费用率是否同向变化 |
| 折旧/固定资产背离 | 折旧率是否异常下降 |
| 税费/利润背离 | 有效税率是否异常低于法定税率 |
| 薪酬/员工背离 | 人均薪酬是否异常波动 |
| 利息/有息负债背离 | 隐含借款利率是否异常 |
| 大存大贷 | 货币资金和有息负债是否同时偏高 |
| 经营/投资现金流错配 | 自由现金流缺口是否持续扩大 |

**二阶：8 个异常模式匹配**

多异常同时触发时，匹配预定义模式：

| 模式 | 触发条件 | 严重度 |
|------|---------|--------|
| 虚增收入嫌疑 | 应收↑ + 现金流失常 | 🔴 high |
| 利润含金量下降 | 现金流失常 | 🔴 high |
| 大存大贷 | 存贷双高 | 🔴 high |
| 存货异常 | 存货↑ | 🔴 high |
| 折旧调节利润 | 折旧率↓ | 🟡 medium |
| 费用资本化 | 费用↓ + 现金流↓ | 🟡 medium |
| 税费不匹配 | 有效税率↓ | 🟡 medium |
| 成本挤压 | 毛利率↓ | 🟡 medium |
| 运营效率提升 | 应收↓ + 现金流↑ | 🟢 正面 |

每个异常模式和每条触发规则都附带 **财报附注排查路径**（如"核对应收账款附注账龄结构表"），在报告中直接呈现。

### 5. 论点生成与审查 (ThesisEngine + Reviewer)

- **ThesisEngine**：加权平均各信号的 thesis_impact → 3 维度评分 + 综合判断
- **CriticEngine**：从信号层/维度层/系统层汇总质疑追问清单，按严重度去重排序
- **ThesisReviewer**：两层审查。Layer 1（确定性）检查零置信度/单证据/信号冲突/行业校准。Layer 2（可选 LLM）做定性评估
- **ThesisEnhancer**（可选 `--enhance`）：LLM 做跨信号模式识别、行业语境化、用户意图适配

---

## 多期趋势分析

AnomalyEngine 的基线计算是系统多期能力的核心。每条勾稽关系规则取近 4 期历史（不含当期）计算均值和标准差：

```
baseline = avg(metric[t-4], metric[t-3], metric[t-2], metric[t-1])
z_score  = (current - baseline) / std
```

这使得系统能够区分"绝对值差但符合公司历史模式"和"本期突然偏离"——前者不是异常，后者才是。

`FinancialSnapshot` 模型支持最多 20 期快照，`to_fact_values()` 输出 11 个 `_prev` 后缀字段供环比分析。

---

## 报告结构

生成的报告包含以下章节：

| 章节 | 内容 |
|------|------|
| 核心发现 | 2-3 句总体判断 |
| 核心指标 | 5-8 个最重要的衍生指标及解读 |
| 风险信号 | 9 条信号逐条评估（high→medium→low） |
| 勾稽关系异常检测 | 触发异常 + z-score + 排查路径 + 匹配模式 |
| 投资论点 | 3 维度判断/评分/置信度/证据链 |
| 审查发现 | blocking issues + warning issues |
| 主要风险 | thesis.primary_risks + review 阻断项 |

---

## 测试

```bash
poetry run pytest                          # 全部测试
poetry run pytest -m integration           # 仅集成测试
poetry run pytest tests/agents/derived_facts/test_accounts_receivable_yoy.py
```

当前覆盖 DerivedFacts 规则引擎单元测试和应收账款质量端到端测试。

---

## 目录结构

```
alphabee/
  agents/
    facts/              FactCollectorAgent — 8 工具 + Pydantic 数据模型
    derived_facts/      DerivedFacts — 21 条 YAML + 拓扑排序引擎
    signal/             SignalEngine — 9 条 YAML + 严重度求值
    thesis/             ThesisEngine + Critic + Enhancer + Reviewer
    anomaly/            AnomalyEngine — 10 勾稽关系 + 8 模式 + z-score
    fact_analysis/      综合分析（占位）
  agents_legacy/        Legacy DeepAgents 架构（不再使用）
  orchestrator/         StateGraph 主编排 + 报告生成
  adapters/             Tushare/AkShare 字段映射 (YAML)
  collectors/           数据采集层 (Tushare/AkShare/Baostock)
  config/               配置读取
  core/                 核心 schema (Run/Step/Artifact/Decision/Issue)
  harness/              Harness Runtime (历史资产，当前未使用)
  middleware/           Web Search 隔离 / 消息限制
  schemas/              规范字段定义 (INDEX.yaml, 125+ 字段)
  tools/                通用工具 (web_search, symbol 提取)
  utils/                LLM 客户端 / 日志
  workflow/             监控工作流 (FrameworkMonitor)
main.py                 CLI 入口
config.yaml             运行配置
tests/                  测试套件
```

---

## 附录：概念速查

### 事实 (Fact)
客观的、可量化的 A 股数据。以 Pydantic 模型 (`FinancialFacts`, `MarketFacts`) 承载，通过 `.to_fact_values()` 展平为 `dict[str, float]` 供下游消费。

### 衍生指标 (DerivedFact)
从事实通过确定性公式计算的二级指标（如 ROE、PEG、现金流质量）。21 条 YAML 规则，支持链式依赖（DAG）。失败的上下游规则自动标记为 `blocked`。

### 信号 (Signal)
从衍生指标 + 事实触发的分级风险标识（high / medium / low / none）。每条信号携带 `thesis_impact`（对投资论点的贡献方向与权重）。当前 9 条规则。

### 勾稽关系 (Cross-Valuation)
三表之间的联动检查——两个指标在正常经营中应有稳定关系，本期偏离历史基线即为异常。10 条规则 + 8 个模式匹配。基于《手把手教你读财报》框架设计。

### 论点 (Thesis)
从多条信号加权聚合的三维度投资质量判断：财务质量、盈利质量、信用风险。由确定性引擎计算，可选 LLM 增强层做语境化解读。
