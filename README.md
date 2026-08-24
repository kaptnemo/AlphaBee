# 🐝 AlphaBee

**AlphaBee** 是一个面向 A 股市场的多智能体投资分析系统。基于 LangGraph + DeepAgents 构建，将个股分析拆解为 **事实采集 → 衍生指标 → 信号检测 → 异常发现 → 冲突探索 → 假设验证 → 论点生成 → 报告输出** 的分层流水线。

[![Python](https://img.shields.io/badge/Python-3.13+-blue.svg)](https://www.python.org/)
[![Poetry](https://img.shields.io/badge/dependency-Poetry-cyan.svg)](https://python-poetry.org/)
[![LangGraph](https://img.shields.io/badge/LangGraph-1.2-purple.svg)](https://langchain-ai.github.io/langgraph/)
[![DeepAgents](https://img.shields.io/badge/DeepAgents-0.6-indigo.svg)](https://pypi.org/project/deepagents/)
[![Tushare](https://img.shields.io/badge/Tushare-1.4-red.svg)](https://tushare.pro/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)

---

## 核心能力

- **分层可独立运行的流水线**：事实采集(8 工具) → 衍生指标(21 条) → 信号(20 条) → 勾稽异常(10 条) → 冲突探索 → 证据验证 → 洞察综合 → 论点(8 维度) → 报告审查，每层可独立测试
- **财务造假侦查**：《手把手教你读财报》框架 —— 10 条勾稽关系 z-score 检测 + 9 个异常模式（虚增收入、大存大贷、折旧调节等），基于近 4 期历史基线(μ±σ)区分偶然波动与真实异常，每条异常附带附注排查路径
- **LLM 证据验证**：跨维度冲突探索（盈利 vs 现金流等 5 大模式）+ `web_search` / Tushare / 东方财富研报 / **本地财报检索** 工具驱动的假设验证，坚持"唯证据论"
- **本地财报检索**：已解析的公司公告/财报 markdown 按章节拆分，`query_financial_report` 用受限 deep agent 核验公司一手披露内容
- **YAML 驱动的规则引擎**：指标/信号/异常规则均为声明式 YAML，拓扑排序依赖解析 + 安全 AST 公式求值
- **统一字段适配层**：Tushare/AkShare 原始字段经 Adapter + Schema Registry 映射为规范字段（7 大领域、125 字段），业务逻辑不依赖数据源字段名
- **任务记录与规则自蒸馏**：每次运行自动保存 TaskRecord → 确定性统计分析 → LLM 蒸馏建议（新信号/行业校准/阈值调整）
- **可观测性**：Langfuse 全链路追踪 + structlog 结构化日志

## 快速开始

推荐环境：`conda` + Python `3.13` + [Poetry](https://python-poetry.org/)。

```bash
git clone https://github.com/captainemo/AlphaBee.git
cd AlphaBee

conda create -n alphabee python=3.13 -y
conda activate alphabee
python -m pip install --upgrade pip poetry
poetry install

cp .env.example .env
cp config.yaml.example config.yaml
```

最小配置：`LLM_API_KEY`（必填）、`TUSHARE_TOKEN`（建议填写）、`TAVILY_API_KEY`（可选）。

```bash
poetry run python main.py --help
poetry run python main.py "帮我分析一下宁德时代"
```

### 常用命令

```bash
# 单次分析 / 多轮对话
poetry run python main.py "帮我分析一下比亚迪"
poetry run python main.py --chat

# 启用 LLM 增强层与审查（--enhance / --llm-review）
poetry run python main.py --enhance --llm-review "分析比亚迪"

# 基于预定义监控框架持续评估特定标的
poetry run python main.py --monitor-framework monitor_framework.md --symbol 300760.SZ

# 任务记录与分析（每次运行自动保存到 data/task_records/）
poetry run python main.py --task-stats
poetry run python main.py --distill            # LLM 规则蒸馏建议
poetry run python main.py --task-history 600519.SH
```

## 文档

细节内容单独维护在 [docs/](docs/) 文档站（`docsify`）：

| 文档 | 内容 |
|------|------|
| [架构与流水线详解](docs/ARCHITECTURE.md) | 节点说明、指标/信号/异常规则、多期趋势、报告结构 |
| [使用指南](docs/USAGE.md) | 全部 CLI 命令、多轮对话、框架监控、任务记录 |
| [配置说明](docs/CONFIGURATION.md) | `config.yaml` 与全部环境变量 |
| [开发者环境](docs/DEVELOPMENT.md) | 开发命令、AI skills 约定、目录结构、后续工作 |
| [概念速查](docs/CONCEPTS.md) | Fact / DerivedFact / Signal / Thesis 等术语 |

## 测试

```bash
poetry run pytest            # 全部测试
poetry run pytest -m integration   # 仅集成测试
```

## License

[MIT](./LICENSE)
