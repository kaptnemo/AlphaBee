# 🐝 AlphaBee

**AlphaBee** 是一个面向 A 股研究场景的多代理投资分析系统，基于 LangGraph 和 DeepAgents 构建。系统将个股研究拆解为 **事实采集 → 派生指标计算 → 信号识别 → 分析评估** 的分层流水线，并通过结构化的 Harness 执行层对分析过程进行编排、评审与质量评估。

[![Python](https://img.shields.io/badge/Python-3.13+-blue.svg)](https://www.python.org/)
[![Poetry](https://img.shields.io/badge/dependency-Poetry-cyan.svg)](https://python-poetry.org/)
[![LangGraph](https://img.shields.io/badge/LangGraph-1.2-purple.svg)](https://langchain-ai.github.io/langgraph/)
[![DeepAgents](https://img.shields.io/badge/DeepAgents-0.6-indigo.svg)](https://pypi.org/project/deepagents/)
[![LangChain](https://img.shields.io/badge/LangChain--OpenAI-1.2-yellow.svg)](https://python.langchain.com/)
[![Tushare](https://img.shields.io/badge/Tushare-1.4-red.svg)](https://tushare.pro/)
[![AkShare](https://img.shields.io/badge/AkShare-1.18-brightgreen.svg)](https://akshare.akfamily.xyz/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)

---

## ✨ 核心能力

- **分层分析流水线**：事实采集 → 派生指标计算 → 信号识别 → Harness 评审，逐层递进
- **YAML 驱动的规则引擎**：21 条派生指标规则覆盖盈利、成长、偿债、运营效率、估值匹配、现金流质量、风险 7 个维度，支持拓扑排序依赖解析与安全 AST 公式求值
- **结构化执行 Harness**：以 `Run / Step / Artifact / Observation / Decision / Issue` 对象承载分析过程，支持计划（plan）→ 报告（report）↔ 评审（critic，可迭代）→ 评估（evaluate）完整链路
- **统一字段适配层**：通过 Adapter + Schema Registry 将 Tushare/AkShare 原始字段名映射为 AlphaBee 规范字段名（7 大领域、125+ 字段），解耦代理与数据源
- **持续跟踪工作流**：基于自定义观察框架对目标股票进行定期快照与变化报告
- **多源数据接入**：Tushare、AkShare、Baostock、Tavily Web Search、DuckDuckGo，通过统一 collector 层管理
- **Web Search 隔离**：中间件层强制区分结构化数据与搜索内容，防止价格/财务数据来自搜索引擎
- **可观测性**：Langfuse 全链路追踪 + structlog 结构化日志
- **交互式 CLI**：支持单次查询与多轮对话两种使用模式

---

## 🏗️ 架构概览

```
main.py (CLI)
  └─ Orchestrator (Legacy)                    ← 当前运行入口
       ├─ FundamentalAgent   ← 财务与基本面分析
       ├─ MarketAgent        ← 行情、资金面、市场表现
       ├─ RiskAgent          ← 风险识别与不确定性提示
       ├─ CrossAnalysisAgent ← 汇总产物 → Harness Runtime
       └─ IndustryAgent      ← 行业景气度与板块表现

新一代分层流水线 (开发中)
  └─ FactCollectorAgent       ← 8 维度客观事实采集
       └─ DerivedFactAgent    ← 21 条规则 · 派生指标计算
            └─ SignalAgent    ← 结构化信号识别 (原型)
                 └─ FactAnalysisAgent ← 综合分析 (占位)

Harness Runtime (LangGraph)
  └─ planner → reporter ↔ critic (迭代) → evaluator

Workflow
  └─ FrameworkMonitor         ← 定期快照与跟踪报告
```

### Legacy 子代理职责（当前运行）

| 代理 | 数据来源 | 输出 |
|---|---|---|
| `FundamentalAgent` | Tushare 财务报告 | 多期财务摘要 |
| `MarketAgent` | Tushare / AkShare 行情 | 行情、估值、资金流 |
| `RiskAgent` | 综合基本面 + 新闻 + 搜索 | 风险点与不确定性列表 |
| `CrossAnalysisAgent` | 前三个代理产物 | Harness 执行结果 + 评估报告 |
| `IndustryAgent` | 板块行情、估值历史 | 行业景气度与横向对比 |

### 新一代代理架构（开发中）

| 层级 | 代理 | 职责 |
|---|---|---|
| 事实采集 | `FactCollectorAgent` | 8 个领域工具 + web_search，只采集客观事实，不做主观判断 |
| 派生指标 | `DerivedFactAgent` | 基于 21 条 YAML 规则进行拓扑排序计算，支持链式依赖 |
| 信号识别 | `SignalAgent` | 基于派生指标触发结构化信号（高/中/低），附带评审问题清单 |
| 综合分析 | `FactAnalysisAgent` | 综合分析层（占位，尚未实现） |

#### FactCollectorAgent 8 维度工具

| 工具 | 领域 | 数据内容 |
|---|---|---|
| `get_company_profile` | 公司概况 | 基本信息、股东结构 |
| `get_financial_fact` | 财务数据 | 多期利润表/资产负债表/现金流量表/财务指标 |
| `get_operation_fact` | 经营数据 | 按产品/地区拆分的营收构成 |
| `get_industry_fact` | 行业数据 | 申万行业分类、指数表现、估值水平 |
| `get_competition_fact` | 竞争格局 | 同业对比（市值、PE、PB、ROE） |
| `get_market_fact` | 市场数据 | 价格、成交量、资金流向、均线 |
| `get_expectation_fact` | 市场预期 | 盈利预测、业绩快报 |
| `get_risk_fact` | 风险数据 | 新闻舆情、质押比例、回购、违规 |

#### 派生指标规则（21 条 / 7 维度）

| 维度 | 规则 |
|---|---|
| 盈利能力 | `roe_level` · `gross_margin_trend` |
| 成长质量 | `revenue_growth` · `profit_leverage` · `market_share_change` |
| 偿债能力 | `debt_ratio` · `interest_coverage` · `current_ratio` |
| 运营效率 | `inventory_pressure` · `asset_turnover` · `capex_intensity` |
| 估值匹配 | `peg_ratio` · `pb_roe_match` · `valuation_compression` |
| 现金流/股东回报 | `cashflow_quality` · `receivable_pressure` · `receivable_growth_gap` · `accounts_receivable_growth` · `accounts_receivable_yoy` · `dividend_coverage` |
| 风险 | `goodwill_risk` |

### Harness 执行模型

`CrossAnalysisAgent` 进入 Harness 后的执行步骤：

1. 并发调用 `FundamentalAgent / MarketAgent / RiskAgent`
2. 成功结果封装为 `Artifact`，失败封装为 `Issue`
3. 将 `artifacts / issues / decisions` 传入 LangGraph runtime
4. `planner` 制定交叉分析计划
5. `reporter` 生成结构化分析报告
6. `critic` 对报告进行交叉一致性评审（可触发 reporter 迭代重写，最多 3 轮）
7. `evaluator` 输出评估报告，评估维度包括：schema 完整性、artifact 覆盖度、数值一致性、证据引用、freshness 等

---

## 📁 目录结构

```text
alphabee/
  agents/            # 新一代分层代理架构
    facts/           #   FactCollectorAgent（8 维度事实采集）
    derived_facts/   #   DerivedFactAgent（21 条规则引擎）
    fact_analysis/   #   FactAnalysisAgent（占位）
    signal/          #   SignalAgent（信号识别原型）
  agents_legacy/     # Legacy 代理架构（当前运行）
    orchestrator/    #   主编排代理
    fundamental/     #   基本面代理
    market/          #   行情代理
    risk/            #   风险代理
    cross/           #   交叉分析代理
    industry/        #   行业代理
  adapters/          # 字段适配层（Tushare/AkShare → 规范字段名）
    tushare/         #   7 个 YAML 映射文件
    akshare/         #   2 个 YAML 映射文件
  collectors/        # 数据采集层（Tushare / AkShare / Baostock / EastMoney）
  config/            # 配置读取（支持 ${ENV_VAR} 占位符）
  core/              # 核心 schema（Run/Step/Artifact/Decision/Issue）与代理基类
  harness/           # LangGraph Harness runtime、prompts、状态压缩
  middleware/        # Web Search 隔离、消息限制等中间件
  schemas/           # 规范字段定义（INDEX.yaml，7 领域 125+ 字段）
  skills/            # 技能提取层（占位）
  tools/             # 结构化工具（基本面、行情、新闻、搜索、Tushare 动态查询）
  utils/             # 工具函数（LLM 客户端、日志配置）
  workflow/          # 监控工作流（FrameworkMonitor）
main.py              # CLI 入口
config.yaml          # 运行配置（LLM、数据源、搜索）
tests/               # 测试套件（派生指标规则单测 + 集成测试）
outputs/             # 运行输出（分析报告 + 监控快照）
logs/                # 结构化日志（structlog + 日志轮转）
```

---

## 🚀 安装

**环境要求**：Python `>=3.13`，[Poetry](https://python-poetry.org/)

```bash
git clone https://github.com/captainemo/AlphaBee.git
cd AlphaBee
poetry install
```

---

## ⚙️ 配置

项目读取根目录下的 `config.yaml`，支持通过环境变量（`.env` 文件）注入敏感值：

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

| 环境变量 | 说明 | 是否必须 |
|---|---|---|
| `LLM_API_KEY` | 大模型 API 密钥（DashScope） | 必须 |
| `TAVILY_API_KEY` | Tavily 搜索 API 密钥 | 可选 |
| `TUSHARE_TOKEN` | Tushare 数据权限 Token | 可选（部分接口需要） |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | Langfuse 可观测性平台密钥 | 可选 |

---

## 💡 快速开始

### 单次分析

```bash
poetry run python main.py "帮我分析一下宁德时代的投资价值"
```

### 多轮对话

```bash
poetry run python main.py --chat
```

会话内置命令：

| 命令 | 说明 |
|---|---|
| `/clear` | 清空上下文 |
| `/exit` | 退出会话 |

### 其他选项

```bash
# 关闭终端颜色输出（适合日志重定向）
poetry run python main.py --no-color "分析一下比亚迪"

# 指定日志目录
poetry run python main.py --log-dir ./logs "分析一下招商银行"
```

### 持续跟踪模式

```bash
poetry run python main.py \
  --monitor-framework ./your-framework.md \
  --symbol 300760 \
  --monitor-periods 8
```

执行流程：

1. 读取观察框架（Markdown 格式）
2. 拉取最新基本面、行情、新闻与补充搜索
3. 生成结构化跟踪报告
4. 将快照写入 `outputs/monitor_snapshots/`，报告写入 `outputs/monitor_reports/`

---

## 🔧 工具层

工具函数位于 `alphabee/tools/`，返回 Pydantic 结构化模型：

| 工具 | 返回类型 | 说明 |
|---|---|---|
| `get_fundamentals` | `Fundamentals` | 多期财务摘要（含 LLM 综合分析） |
| `get_market_data` | `MarketData` | 行情、估值、资金流 |
| `get_stock_news_summary` | `NewsSummary` | 近期新闻摘要 |
| `web_search` | `SearchResult` | Tavily / DuckDuckGo 搜索（受中间件约束） |
| `get_industry_fundamentals` | `IndustryFundamentals` | 行业估值历史与成分股 |
| `tushare_query` | `dict` | 动态 Tushare API 查询（覆盖 30+ 接口） |

`get_fundamentals` 和 `get_market_data` 内置 TTL 缓存与并发去重，减少多代理并发时的重复调用。

新一代 `FactCollectorAgent` 额外提供 8 个领域工具（见上方架构章节），均通过 `TuShareHelper` + `SyncTTLCache` 接入，返回 AlphaBee 规范字段名。

---

## 📊 数据架构

### Schema Registry

`schemas/INDEX.yaml` 定义了 7 大领域共 125+ 规范字段，作为全系统的字段单一来源：

| 领域 | 字段数 | 数据源接口 |
|---|---|---|
| `financial` | 30 | income, balancesheet, cashflow, fina_indicator |
| `market` | 22 | daily, daily_basic, moneyflow |
| `company` | 26 | stock_basic, stock_company, top10_holders |
| `industry` | 12 | index_classify, index_daily, sw_daily |
| `expectation` | 14 | forecast, express |
| `risk` | 16 | pledge_stat, repurchase, stk_rewards |
| `operation` | 5 | fina_mainbz |

### Adapter 适配层

`adapters/` 通过 YAML 映射文件将数据源原始字段名转换为规范字段名，解耦代理逻辑与数据源：

- **Tushare**：7 个映射文件（financial / market / company / industry / expectation / risk / operation）
- **AkShare**：2 个映射文件（industry / risk）

---

## 🧪 测试

```bash
poetry run pytest
```

当前测试覆盖 `DerivedFactAgent` 的规则引擎：

- `test_accounts_receivable_yoy.py` — 应收账款同比规则单元测试
- `test_agent_integration.py` — 派生指标代理集成测试
- `test_e2e_receivable_quality.py` — 应收账款质量端到端测试

---

## 📌 开发状态

AlphaBee 目前处于活跃开发阶段。可运行的核心功能：

- ✅ CLI（单次查询 + 多轮对话）
- ✅ Legacy 五代理并发分析流程
- ✅ Harness 执行与评估链路（含 critic 迭代重写）
- ✅ 持续跟踪工作流（FrameworkMonitor）
- ✅ 派生指标规则引擎（21 条规则 / 7 维度）
- ✅ FactCollectorAgent 8 维度事实采集
- ✅ Schema Registry + Adapter 适配层
- ✅ Langfuse 可观测性集成
- ⚙️ SignalAgent 信号识别（原型，1 条规则）
- ⚙️ FactAnalysisAgent 综合分析（占位）
- ⚙️ Memory 模块（开发中）
- ⚙️ MCP Server（开发中）
- ⚙️ 评估回归测试体系（开发中）

### 架构演进方向

```
当前 (Legacy)：  Orchestrator → 5 SubAgents → Harness
目标 (New)：     FactCollector → DerivedFacts → Signal → Analysis → Harness
```

新一代架构将分析过程拆解为可组合的流水线，每条规则以 YAML 定义、支持链式依赖，代理之间通过规范字段名通信，实现更高的可维护性与可扩展性。

---

## 📄 License

[MIT](./LICENSE)
