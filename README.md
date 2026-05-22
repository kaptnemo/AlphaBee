# 🐝 AlphaBee

**AlphaBee** 是一个面向 A 股研究场景的多代理投资分析系统，基于 LangGraph 和 DeepAgents 构建。系统将个股的基本面、行情、风险、行业等多个维度的分析拆解为独立的专项代理，并通过结构化的 Harness 执行层对分析过程进行编排、评审与评估。

[![Python](https://img.shields.io/badge/Python-3.13-blue.svg)](https://www.python.org/)
[![Poetry](https://img.shields.io/badge/dependency-Poetry-cyan.svg)](https://python-poetry.org/)
[![LangGraph](https://img.shields.io/badge/LangGraph-1.2-purple.svg)](https://langchain-ai.github.io/langgraph/)
[![DeepAgents](https://img.shields.io/badge/DeepAgents-0.6-indigo.svg)](https://pypi.org/project/deepagents/)
[![LangChain](https://img.shields.io/badge/LangChain--OpenAI-1.2-yellow.svg)](https://python.langchain.com/)
[![Tushare](https://img.shields.io/badge/Tushare-1.4-red.svg)](https://tushare.pro/)
[![AkShare](https://img.shields.io/badge/AkShare-1.18-brightgreen.svg)](https://akshare.akfamily.xyz/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)
---

## ✨ 核心能力

- **多维度并发分析**：基本面、行情、风险、交叉分析、行业分析五个子代理并发执行
- **结构化执行 Harness**：以 `Run / Step / Artifact / Observation / Decision / Issue` 对象承载分析过程，支持计划（plan）→ 报告（report）→ 评审（critic）→ 评估（evaluate）完整链路
- **持续跟踪工作流**：基于自定义观察框架对目标股票进行定期快照与变化报告
- **多源数据接入**：Tushare、AkShare、Baostock、Tavily Web Search，通过统一 collector 层管理
- **Web Search 隔离**：中间件层强制区分结构化数据与搜索内容，防止价格/财务数据来自搜索引擎
- **交互式 CLI**：支持单次查询与多轮对话两种使用模式

---

## 🏗️ 架构概览

```
main.py (CLI)
  └─ Orchestrator
       ├─ FundamentalAgent   ← 财务与基本面分析
       ├─ MarketAgent        ← 行情、资金面、市场表现
       ├─ RiskAgent          ← 风险识别与不确定性提示
       ├─ CrossAnalysisAgent ← 汇总产物 → Harness Runtime
       └─ IndustryAgent      ← 行业景气度与板块表现

Harness Runtime (LangGraph)
  └─ planner → reporter → critic → evaluator

Workflow
  └─ FrameworkMonitor       ← 定期快照与跟踪报告
```

### 子代理职责

| 代理 | 数据来源 | 输出 |
|---|---|---|
| `FundamentalAgent` | Tushare 财务报告 | 多期财务摘要 |
| `MarketAgent` | Tushare / AkShare 行情 | 行情、估值、资金流 |
| `RiskAgent` | 综合基本面 + 新闻 + 搜索 | 风险点与不确定性列表 |
| `CrossAnalysisAgent` | 前三个代理产物 | Harness 执行结果 + 评估报告 |
| `IndustryAgent` | 板块行情、估值历史 | 行业景气度与横向对比 |

### Harness 执行模型

`CrossAnalysisAgent` 进入 Harness 后的执行步骤：

1. 并发调用 `FundamentalAgent / MarketAgent / RiskAgent`
2. 成功结果封装为 `Artifact`，失败封装为 `Issue`
3. 将 `artifacts / issues / decisions` 传入 LangGraph runtime
4. `planner` 制定交叉分析计划
5. `reporter` 生成结构化分析报告
6. `critic` 对报告进行交叉一致性评审
7. `evaluator` 输出评估报告，评估维度包括：schema 完整性、artifact 覆盖度、数值一致性、证据引用、freshness 等

---

## 📁 目录结构

```text
alphabee/
  agents/        # 多代理定义（Orchestrator + 5 个子代理）
  collectors/    # 数据采集辅助层（Tushare / AkShare / Baostock）
  config/        # 配置读取（支持 ${ENV_VAR} 占位符）
  core/          # 核心 schema 与状态模型
  harness/       # LangGraph Harness runtime 与 prompts
  middleware/    # Web Search 隔离、消息限制等中间件
  tools/         # 结构化工具（基本面、行情、新闻、搜索）
  workflow/      # 监控工作流（FrameworkMonitor）
main.py          # CLI 入口
config.yaml      # 运行配置（LLM、数据源、搜索）
outputs/         # 运行输出（报告 + 快照）
logs/            # 结构化日志（structlog + 日志轮转）
```

---

## 🚀 安装

**环境要求**：Python `>=3.13`，[Poetry](https://python-poetry.org/)

```bash
git clone https://github.com/your-org/alphabee.git
cd alphabee
poetry install
```

---

## ⚙️ 配置

项目读取根目录下的 `config.yaml`，支持通过环境变量注入敏感值：

```yaml
llm:
  api_key: "${LLM_API_KEY}"
  base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
  model: "qwen3-plus"

web_search:
  tavily:
    api_key: "${TAVILY_API_KEY:}"
```

| 配置项 | 说明 | 是否必须 |
|---|---|---|
| `LLM_API_KEY` | 大模型 API 密钥 | 必须 |
| `TAVILY_API_KEY` | Tavily 搜索 API 密钥 | 可选 |
| Tushare Token | Tushare 数据权限 Token | 可选（部分接口需要） |

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

`get_fundamentals` 和 `get_market_data` 内置 TTL 缓存与并发去重，减少多代理并发时的重复调用。

---

## 📌 开发状态

AlphaBee 目前处于活跃开发阶段，可运行的核心功能包括：

- ✅ CLI（单次查询 + 多轮对话）
- ✅ 五代理并发分析流程
- ✅ Harness 执行与评估链路
- ✅ 持续跟踪工作流
- ⚙️ Memory 模块（开发中）
- ⚙️ MCP Server（开发中）
- ⚙️ 评估回归测试体系（开发中）

---

## 📄 License

[MIT](./LICENSE)
