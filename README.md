# 🐝 AlphaBee

**AlphaBee** 是一个面向 A 股市场的多智能体投资分析系统。基于 LangGraph + DeepAgents 构建，将个股分析拆解为 **事实采集 → 衍生指标 → 信号检测 → 异常发现 → 冲突探索 → 假设验证 → 论点生成 → 报告输出** 的分层流水线，辅以可选的 LLM 增强层进行语境化解读。

[![Python](https://img.shields.io/badge/Python-3.13+-blue.svg)](https://www.python.org/)
[![Poetry](https://img.shields.io/badge/dependency-Poetry-cyan.svg)](https://python-poetry.org/)
[![LangGraph](https://img.shields.io/badge/LangGraph-1.2-purple.svg)](https://langchain-ai.github.io/langgraph/)
[![DeepAgents](https://img.shields.io/badge/DeepAgents-0.6-indigo.svg)](https://pypi.org/project/deepagents/)
[![Tushare](https://img.shields.io/badge/Tushare-1.4-red.svg)](https://tushare.pro/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)

---

## 核心能力

- **分层流水线**：事实 → 衍生指标(21 条) → 信号(11 条) → 勾稽关系异常(10+9) → 冲突探索(LLM) → 假设验证(LLM+工具) → 论点(8 维度) → 审查 → 报告，每层可独立运行和测试
- **《手把手教你读财报》框架**：10 条一阶勾稽关系 z-score 检测 + 9 个二阶异常模式（虚增收入、存货异常、大存大贷、折旧调节等），每条异常附带附注排查路径
- **多期趋势基线**：AnomalyEngine 基于近 4 期历史基线 (μ±σ) 检测指标偏离，区分偶然波动与真实异常
- **冲突探索与证据验证**：LLM 驱动的跨维度矛盾发现（盈利vs现金流、估值vs基本面等5大模式），生成可验证假设并通过 web_search + Tushare + 东方财富研报工具验证
- **行业语境校准**：从 Tushare 提取权威行业分类，审查层对金融/医药/半导体等高杠杆或高研发行业做阈值调整
- **YAML 驱动的规则引擎**：所有衍生指标、信号、勾稽关系规则均为声明式 YAML，支持拓扑排序依赖解析与安全 AST 公式求值
- **统一字段适配层**：Adapter + Schema Registry 将 Tushare/AkShare 原始字段映射为 AlphaBee 规范字段名（7 大领域、125 字段）
- **任务记录与自蒸馏**：每次运行自动保存 TaskRecord → TaskAnalyzer 确定性统计分析 → RuleDistiller LLM 蒸馏建议（新信号/行业校准/阈值调整）
- **可观测性**：Langfuse 全链路追踪 + structlog 结构化日志
- **交互式 CLI**：单次查询 / 多轮对话 / 任务统计 / 蒸馏报告 / 框架监控，支持 --enhance / --llm-review 可选增强

---

## 架构概览

```
main.py (CLI)
  └─ Orchestrator (StateGraph)
       ├─ collect_raw_facts          ← 事实采集 + 结构化建模
       │   └─ FactCollectorAgent  (LLM, 8 工具)
       │
       ├─ run_analysis_engines       ← 确定性引擎（并行）
       │   ├─ DerivedFacts  (确定性, 21 条 YAML)
       │   ├─ SignalEngine  (确定性, 11 条 YAML)
       │   └─ AnomalyEngine  (确定性, 10 + 9 YAML)
       │
       ├─ explore_conflicts          ← 跨维度矛盾发现 (LLM)
       │   └─ 5 大冲突模式 → 候选假设生成
       │
       ├─ verify_hypotheses          ← 假设证据验证 (LLM + 工具)
       │   └─ web_search / Tushare / 东方财富研报
       │
       ├─ run_thesis                 ← 8 维度加权综合评分
       │
       ├─ review_thesis              ← 证据充分性 / 信号一致性 / 语境适配
       │
       └─ generate_report → finalize_message
           (单次 LLM, 结构化 → Markdown)
```

### 流水线各层规则统计

| 层级 | 引擎 | 规则数 | 维度 |
|------|------|--------|------|
| 派生指标 | DerivedFacts | 21 | 盈利/成长/偿债/效率/估值/现金流/风险 |
| 信号检测 | SignalEngine | 11 | 收入质量/现金流/债务/盈利/成长/扩张/估值/异常聚集/勾稽断裂/壁垒侵蚀/资本效率 |
| 勾稽关系 | AnomalyEngine | 10 + 9 | z-score 检测 + 模式匹配 |
| 投资论点 | ThesisEngine | 8 | 财务质量/盈利质量/信用风险/成长质量/资本效率/竞争壁垒/估值合理/经营稳定 |

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

# 指定日志目录
poetry run python main.py --log-dir ./my_logs "分析宁德时代"
```

### 多轮对话命令

| 命令 | 说明 |
|------|------|
| 直接输入问题 | 继续追问 |
| `/clear` | 清空上下文 |
| `/exit` | 退出会话 |

### 框架监控模式

```bash
# 基于预定义监控框架持续评估特定标的
poetry run python main.py --monitor-framework monitor_framework.md --symbol 300760.SZ

# 指定监控期数
poetry run python main.py --monitor-framework monitor_framework.md --symbol 300760.SZ --monitor-periods 12
```

监控模式读取 Markdown 格式的监控框架文件，对指定标的拉取最新多期财务数据，按框架论点逐条评估并生成结构化监控报告。

### 任务记录与分析

```bash
# 每次运行自动保存记录到 data/task_records/<symbol>/

# 查看统计摘要
poetry run python main.py --task-stats

# 生成规则蒸馏建议报告（需 LLM）
poetry run python main.py --distill

# 查看指定标的的历史运行记录
poetry run python main.py --task-history 600519.SH

# 查看单次运行的完整记录
poetry run python main.py --task-record task-a1b2c3d4e5f6
```

---

## 配置

从 `config.yaml.example` 复制为 `config.yaml`，支持 `${ENV_VAR}` 和 `${ENV_VAR:default}` 占位符：

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

data:
  root_dir: "${DATA_ROOT:data}"
```

| 环境变量 | 说明 | 必须 |
|----------|------|------|
| `LLM_API_KEY` | 大模型 API 密钥 | ✅ |
| `LLM_BASE_URL` | 大模型 API 地址（默认 `https://api.deepseek.com`） | 可选 |
| `LLM_MODEL` | 模型名称（默认 `deepseek-chat`） | 可选 |
| `TUSHARE_TOKEN` | Tushare 数据 Token | ✅（大部分） |
| `TAVILY_API_KEY` | Tavily 搜索 API 密钥 | 可选 |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | Langfuse 可观测性 | 可选 |
| `DATA_ROOT` | 产物根目录（默认 `data/`） | 可选 |

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

11 条 YAML 规则，基于衍生指标 + 原始事实做严重度分级触发：

| 信号 | 触发逻辑 | 覆盖维度 |
|------|---------|---------|
| `revenue_quality_risk` | 应收/营收增速差值 | 财务质量 + 盈利质量 |
| `cashflow_quality_risk` | 经营现金流/净利润 < 阈值 | 财务质量 + 盈利质量 |
| `debt_risk` | 负债率 > 阈值且流动比率低 | 财务质量 + 信用风险 |
| `profitability_quality_risk` | ROE + 毛利率趋势 | 财务质量 + 盈利质量 |
| `growth_quality_risk` | 应收/营收差距 + 利润杠杆 | 财务质量 + 成长质量 |
| `expansion_risk` | 商誉 + 负债率 + 资本支出 | 财务质量 + 信用风险 |
| `valuation_risk` | PEG + 估值压缩 + PB-ROE 匹配 | 财务质量 + 估值合理 |
| `anomaly_cluster_risk` | 2+ 个异常模式触发 | 财务质量 |
| `cross_validation_break` | 最强异常 \|z\| > 2.5σ | 财务质量 + 盈利质量 |
| `moat_erosion_risk` | 毛利率趋势 + ROE + 营收增速 | 竞争壁垒 + 盈利质量 |
| `capital_efficiency_risk` | ROE + 现金流质量 + 资本支出强度 + 分红覆盖 | 资本效率 + 财务质量 |

每条信号携带 `thesis_impact`（影响方向+维度）和 `critic_questions`，直接驱动下游论点引擎。

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

**二阶：9 个异常模式匹配**

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

### 5. 冲突探索 (ExploreConflicts) ★ 新增

LLM 驱动的跨维度矛盾发现引擎，基于上游分析结果检测 **5 大冲突模式**：

| 模式 | 检测内容 |
|------|---------|
| 盈利 vs 现金流背离 | 净利润改善但经营现金流/应收/存货恶化 |
| 估值 vs 基本面背离 | PE/PB 上升但盈利质量/ROE/增长下滑 |
| 行业 vs 公司背离 | 行业信号正面但公司数据走弱 |
| 表间不一致 | 利润表、资产负债表、现金流量表无法交叉验证 |
| 信号方向冲突 | 同维度内信号方向矛盾且强度相近 |

对每个检测到的冲突，生成 **3-5 个候选解释假设**，每个假设附带可验证的预测和验证清单，传递给下游假设验证节点。

### 6. 假设验证 (VerifyHypotheses) ★ 新增

对上一步生成的假设进行证据驱动验证，配备丰富工具：

- **`web_search`**：定性信息检索
- **`query_tushare`**：结构化财务/市场数据
- **东方财富研报工具**（8 个）：研报列表、研报详情、行业研报、PDF 下载等

每个假设归类为 `verified` / `partial` / `rejected` / `unknown` 四种裁决，输出包含支持证据、反驳证据、置信度评分和证据缺口列表。遵循严格的"唯证据论"原则——不做推测。

### 7. 论点生成与审查 (ThesisEngine + Reviewer)

- **ThesisEngine**：8 维度加权综合评分（财务质量、盈利质量、信用风险、成长质量、资本配置效率、竞争壁垒、估值合理性、经营稳定性），每个维度有 5 级分层模板（strong_positive ~ strong_negative）
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
| 风险信号 | 11 条信号逐条评估（high→medium→low） |
| 勾稽关系异常检测 | 触发异常 + z-score + 排查路径 + 匹配模式 |
| 冲突与假设验证 | 检测到的跨维度矛盾 + 假设 + 证据验证结果 |
| 投资论点 | 8 维度判断/评分/置信度/证据链 |
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
    signal/             SignalEngine — 11 条 YAML + 严重度求值
    anomaly/            AnomalyEngine — 10 勾稽关系 + 9 模式 + z-score
    explore_conflicts/  冲突探索 Agent — 5 大矛盾模式 + 候选假设
    verify_hypotheses/  假设验证 Agent — web_search + Tushare + 研报
    thesis/             ThesisEngine + Critic + Enhancer + Reviewer
    fact_analysis/      综合分析（占位）
  agents_legacy/        Legacy DeepAgents 架构（不再使用）
  orchestrator/         StateGraph 主编排 + 报告生成（8 节点）
  task_records/         任务记录采集 / 分析 / 蒸馏
  adapters/             Tushare/AkShare 字段映射 (YAML)
  collectors/           数据采集层 (Tushare/AkShare/Baostock)
  config/               配置读取
  core/                 核心 schema (Run/Step/Artifact/Decision/Issue)
  data_fetch/           数据获取管线 (CLI + scanner + database + fingerprint)
  harness/              Harness Runtime (历史资产，当前未使用)
  middleware/           Web Search 隔离 / 消息限制
  schemas/              规范字段定义 (INDEX.yaml, 125 字段)
  tools/                通用工具 (web_search, symbol 提取)
  utils/                LLM 客户端 / 日志
  workflow/             监控工作流 (FrameworkMonitor)
main.py                 CLI 入口
config.yaml             运行配置
tests/                  测试套件
```

---

## 后续工作

### 1. 完善任务记录与规则自蒸馏

**现状**：`task_records/` 模块已实现基础采集（`TaskRecorder` — 包含完整报告 JSON `report_raw`）、存储（`TaskStore`）、确定性分析（`TaskAnalyzer`）和 LLM 蒸馏建议（`RuleDistiller`）。每次运行自动保存记录到 `data/task_records/<symbol>/`，通过 `--task-stats` / `--distill` 产出统计和蒸馏报告。

**后续**：

- **阶段计时采集**：在 StateGraph 节点间注入 timing hook，使 `StageTiming` 数据自动填充（当前依赖手动传参）
- **信号触发率回归检测**：规则修改后自动对比修改前后的触发率变化，检测规则退化
- **蒸馏闭环自动化**：`--distill` 产出的候选 YAML 增加 diff 对比 + 一键回测功能
- **行业基线自建**：积累 100+ 不同行业标的的运行记录后，自动计算行业 μ±σ 作为 reviewer 的对比基准

### 2. 增加上下文压缩

**现状**：FactCollectorAgent 的 LLM 调用和报告生成 LLM 调用都直接消费原始上下文，没有压缩层。当历史记录积累、FactCollector 输出的 raw_response 变长时，context window 压力递增。

**设计方向**：

- **分层压缩**：对 `fact_collection` artifact 的 raw_response 做结构化摘要提取（保留数值表格，压缩叙述文字）
- **角色感知剪裁**：参考旧 harness 的 node-aware slicing 思路，不同节点接收不同粒度的上下文（如 report 生成需要完整数据，thesis 审查只需摘要）
- **增量注入**：多轮对话中，前一轮的完整 report 压缩为结论 + 关键指标快照后再注入下一轮 context

### 3. 增加记忆力模块——用户投资画像

**目标**：记录用户在多次查询中关注的公司、行业、分析维度偏好，逐步构建用户投资画像，使系统能提供更个性化的分析视角和关注点提醒。

**设计方向**：

- **画像维度**：
  - 行业偏好（用户查询频次最高的申万行业）
  - 风格偏好（价值/成长、大盘/中小盘、高分红/高增长）
  - 风控偏好（对确定性要求高/低、对杠杆容忍度、对估值敏感度）
  - 关注维度权重（财务真实性 vs 成长性 vs 估值合理性，用户更关注哪个）
- **采集方式**：
  - 零侵入：从 `TaskRecord` 的 `query` / `symbol` / `flags` 字段累积，不额外询问用户
  - 维度偏好从 `--enhance` 的使用频率和 reviewer issue 分布推断
- **输出**：
  - 报告中加入"与你投资风格的匹配度"视角
  - 多轮对话中主动提示"你上次关注的 XX 行业/公司有新财报"（如启用 Monitor）
  - `--task-stats` 中增加用户画像卡片
- **存储**：`data/user_profile.json`，定期从 `data/task_records/` 重算更新

---

## 附录：概念速查

### 事实 (Fact)
客观的、可量化的 A 股数据。以 Pydantic 模型 (`FinancialFacts`, `MarketFacts`) 承载，通过 `.to_fact_values()` 展平为 `dict[str, float]` 供下游消费。

### 衍生指标 (DerivedFact)
从事实通过确定性公式计算的二级指标（如 ROE、PEG、现金流质量）。21 条 YAML 规则，支持链式依赖（DAG）。失败的上下游规则自动标记为 `blocked`。

### 信号 (Signal)
从衍生指标 + 事实触发的分级风险标识（high / medium / low / none）。每条信号携带 `thesis_impact`（对投资论点的贡献方向与权重）。当前 11 条规则。

### 勾稽关系 (Cross-Valuation)
三表之间的联动检查——两个指标在正常经营中应有稳定关系，本期偏离历史基线即为异常。10 条规则 + 9 个模式匹配。基于《手把手教你读财报》框架设计。

### 论点 (Thesis)
从多条信号加权聚合的 8 维度投资质量判断：财务质量、盈利质量、信用风险、成长质量、资本配置效率、竞争壁垒、估值合理性、经营稳定性。由确定性引擎计算，可选 LLM 增强层做语境化解读。
