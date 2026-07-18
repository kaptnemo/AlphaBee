# AlphaBee Engineering Roadmap

## 目标

本文档从软件工程角度分析 AlphaBee 当前代码架构中的不合理之处，并给出改造建议与实施路线图。

业务能力路线图单独保存在：

```text
ROADMAP.md
```

本文档聚焦：

- 模块边界
- 依赖方向
- 可测试性
- 可维护性
- 数据与 schema 治理
- 编排层复杂度
- 工程质量与交付能力

---

## 当前工程架构概览

当前 AlphaBee 大致包含以下几条并行线：

```text
main.py
  → alphabee/orchestrator/
      → collectors
      → analysis engines
      → conflict / verification agents
      → thesis / review
      → report

alphabee/agents/
  → facts
  → derived_facts
  → signal
  → anomaly
  → thesis
  → explore_conflicts
  → verify_hypotheses

alphabee/collectors/
  → tushare / akshare / baostock / eastmoney / local

alphabee/adapters/
  → source field mapping

alphabee/data_fetch/
  → failure recording / auto-fix task system

alphabee/harness/
  → older structured runtime / compressor

alphabee/agents_legacy/
  → legacy DeepAgents pipeline

alphabee/tools/
  → older tool layer / legacy business tools

alphabee/workflow/
  → framework monitor path
```

这说明项目已经积累了很多能力，但也出现了明显的架构漂移：新旧管线并存、编排层变厚、数据层和业务层边界不稳定。

---

## 核心架构问题

## 1. 多条 pipeline 并存，主路径不够清晰

当前仓库里同时存在：

- `alphabee/orchestrator/`：当前主路径
- `alphabee/agents_legacy/`：旧 DeepAgents pipeline
- `alphabee/harness/`：旧/复用型 planner-reporter-critic-evaluator runtime
- `alphabee/tools/`：旧工具层
- `alphabee/workflow/framework_monitor.py`：独立监控工作流

问题：

- 新贡献者很难判断哪个路径是 runtime 主路径。
- README、CLAUDE.md、代码注释中的架构描述可能不同步。
- 一些模块仍然有价值，但没有清晰标记为 active / legacy / experimental。

建议：

```text
alphabee/
  runtime/              # 当前唯一主运行路径
  domain/               # 纯业务模型和规则
  data/                 # 数据访问与 provider
  adapters/             # 外部字段 → canonical
  agents/               # LLM agents
  apps/                 # CLI / web / admin
  legacy/               # 明确迁入旧实现
  experiments/          # 未接入原型
```

短期不一定要大搬家，但必须先在文档和 package 层标清：

- active
- legacy
- experimental
- deprecated

---

## 2. `orchestrator/analyzers.py` 过厚，职责混杂

`alphabee/orchestrator/analyzers.py` 约 900+ 行，承担了：

- company context 构建
- DerivedFacts 调用
- SignalEngine 调用
- AnomalyEngine 调用
- conflict prompt 生成
- conflict agent 调用
- hypothesis verification 调用
- thesis 调用
- data_fetch gap recording

问题：

- 单文件承担太多阶段职责。
- 单元测试难写，只能 monkeypatch 大量内部函数。
- 业务阶段之间依赖隐式共享 `state` dict。
- 后续加入 InsightAgent / BusinessModelClassifier 会继续膨胀。

建议拆分为：

```text
alphabee/orchestrator/
  graph.py
  nodes/
    collect.py
    analyze.py
    anomaly.py
    signal.py
    conflicts.py
    verification.py
    thesis.py
    insight.py
    report.py
  services/
    company_context.py
    gap_recorder.py
    payload_builders.py
```

每个 node 文件只暴露一个 LangGraph node 函数，复杂逻辑下沉到 service。

当前已完成第一步拆分：

```text
alphabee/orchestrator/
  analyzers.py                # 兼容 facade
  nodes/
    analyze.py
    conflicts.py
    verification.py
    thesis.py
  services/
    company_context.py
    gap_recorder.py
    payload_builders.py
```

后续仍可继续把 `review_thesis` / `report gates` 也并入 `nodes/`，进一步收敛顶层编排文件。

---

## 3. 编排层直接 import 具体工具，依赖倒置不足

例如编排层直接调用：

```python
get_company_profile()
get_industry_fact()
get_financial_facts_model()
get_market_facts_model()
```

问题：

- 编排层知道太多数据获取细节。
- 测试时只能 monkeypatch 具体函数。
- 未来引入缓存、批量拉取、离线回放时会比较痛。

建议引入服务接口：

```python
class FactService:
    def get_financial_facts(symbol) -> FinancialFacts: ...
    def get_market_facts(symbol) -> MarketFacts: ...
    def get_company_context(symbol) -> CompanyContext: ...
```

编排层只依赖 `FactService`，具体 provider / adapter / cache 全部藏在 service 内。

---

## 4. schema 中心不够统一，Pydantic 模型、YAML schema、mapping 并存但未强绑定

当前有：

- `alphabee/agents/facts/models.py`：Pydantic facts model
- `alphabee/schemas/*.yaml`：canonical field registry
- `alphabee/adapters/tushare/*.yaml`
- `alphabee/adapters/akshare/*.yaml`
- derived_facts YAML
- signal YAML
- anomaly YAML

问题：

- Pydantic 字段与 canonical schema 可能漂移。
- signal/derived/anomaly 规则里的字段不一定能被静态校验。
- 出现过 `operating_cash_flow` vs `operating_cashflow` 这类风险。
- Adapter mapping 是否覆盖 required facts，当前没有系统检查。

建议新增 schema validation 工具：

```text
alphabee/schema_registry/
  canonical.py
  validator.py
  dependency_graph.py
```

校验内容：

- Pydantic facts 字段必须存在于 canonical schema
- derived_facts.required_facts 必须存在于 canonical schema 或 derived registry
- signal.required_facts 必须存在于 canonical schema / anomaly facts
- adapters mapping target 必须是 canonical fields
- 单位/频率/period 口径必须完整

并提供命令：

```bash
poetry run python -m alphabee.schema_registry.validate
```

---

## 5. Adapter 层能力偏薄，数据血缘和单位转换不系统

当前 `TuShareAdapter` 主要做 DataFrame rename：

```python
df.rename(columns=adapter_columns)
```

问题：

- 缺少统一的 source metadata。
- 缺少单位转换标准化。
- 缺少缺失字段原因。
- 缺少 mapping 覆盖率检查。
- AkShare / Baostock / Eastmoney adapter 模式不完全统一。

建议 adapter 输出不仅是 DataFrame，而是 canonical record：

```python
CanonicalFact(
    field="operating_cashflow",
    value=...,
    unit="CNY",
    period="2024Q4",
    source="tushare",
    source_field="n_cashflow_act",
    missing_reason=None,
)
```

短期可先保持 DataFrame rename，但增加：

- mapping validation
- unit transform 字段
- missing report
- lineage metadata sidecar

---

## 6. Provider / Collector / Tool 三层边界不清

当前同时有：

- `collectors/*/helper.py`
- `providers/industry.py`
- `agents/facts/tools/*.py`
- `tools/*.py`

问题：

- 哪一层负责 retry？
- 哪一层负责 fallback？
- 哪一层负责 adapter？
- 哪一层负责业务模型组装？
- 旧 `tools/` 和新 `agents/facts/tools/` 容易混用。

建议明确分层：

```text
collector:
  只负责单一外部 API 调用、retry、错误记录、原始结果包装

adapter:
  只负责字段名、单位、缺失、血缘

provider:
  负责多数据源 fallback 和组合

fact_tool:
  负责面向 Agent 的业务语义接口，返回 Pydantic facts

engine:
  只消费 canonical facts，不碰外部 source 字段
```

---

## 7. 大量异常被吞掉，错误可观测性不足

代码中存在多处：

```python
except Exception:
    pass
```

或：

```python
except Exception:
    return fallback
```

问题：

- 运行失败时用户只看到“数据缺失”，不知道根因。
- 自动修复系统有 data_fetch，但很多吞错路径不会记录足够上下文。
- LLM 失败、provider 失败、adapter 失败没有统一错误 taxonomy。

建议：

- 建立统一 `AlphaBeeError` 层级。
- 所有节点输出 structured `Issue`。
- provider/tool 层禁止 silent pass。
- 若必须不中断 pipeline，也要记录：
  - provider
  - api_name
  - symbol
  - error_type
  - missing_fields
  - degradation_path

---

## 8. `main.py` 过厚，CLI、渲染、流式事件处理混在一起

`main.py` 同时承担：

- argparse
- terminal UI
- streaming event parsing
- report rendering
- chat mode
- monitor workflow routing
- task record integration

问题：

- CLI 入口难测试。
- UI 逻辑和 runtime 逻辑混在一起。
- 后续若加 web/admin/API，会重复实现渲染逻辑。

建议拆分：

```text
alphabee/apps/cli/
  main.py
  args.py
  renderer.py
  streaming.py
  chat.py

main.py
  只保留 thin wrapper
```

---

## 9. 测试覆盖集中在局部，主 pipeline 缺少契约测试

当前测试主要覆盖：

- derived_facts 部分规则
- facts tools 部分工具
- data_fetch
- 少量 orchestrator/anomaly signal 测试

缺口：

- orchestrator graph contract
- report payload contract
- schema/mapping validation
- signal rules 全量 smoke test
- thesis/reviewer 多维度一致性
- provider fallback 行为
- adapter mapping 覆盖
- LLM output JSON parse fallback

建议建立测试金字塔：

```text
unit:
  formula eval / rules / adapters / models

contract:
  each node input/output schema
  each artifact payload shape

integration:
  provider fallback with mocked raw data
  full orchestrator with fixture facts, no external API

golden:
  selected symbols with frozen fixture facts
  expected report skeleton / insight skeleton
```

---

## 10. 缺少“离线回放”机制

当前大量能力依赖外部数据源和 LLM，导致：

- 测试慢
- 不稳定
- 难复现
- 难 debug

建议建立 fixture replay：

```text
data/fixtures/
  600519.SH/
    financial_facts.json
    market_facts.json
    industry_facts.json
    expected_artifacts.json
```

并提供：

```bash
poetry run python -m alphabee.replay 600519.SH
```

目标：不用 Tushare、不用 LLM，也能跑完整 deterministic pipeline。

---

## 11. Legacy / experimental 目录需要治理

当前存在：

- `agents_legacy/`
- `harness/`
- `apps/`
- `memory/`
- `mcp_server/`
- `workflow/`
- `tools/`

部分目录可能未接入主 runtime。

建议：

1. 给每个顶层目录加 `README.md` 或模块 docstring 标记状态。
2. 将不再活跃的模块迁入 `alphabee/legacy/`。
3. 对 experimental 模块增加 feature flag 或明确入口。
4. README 只描述 active runtime，避免混淆。

---

## 12. 工程工具链不完整

当前 `pyproject.toml` 只定义了 pytest 配置，没有统一 lint/type/format 命令。

建议引入：

- `ruff`：lint + format
- `mypy` 或 `pyright`：类型检查
- `pytest-cov`：覆盖率
- `pre-commit`：提交前检查

建议命令：

```bash
poetry run ruff check .
poetry run ruff format .
poetry run pyright alphabee
poetry run pytest -m "not integration"
```

---

# Engineering Roadmap

## Phase E0：架构事实盘点与边界标注

目标：先降低认知成本，不做大规模重构。

任务：

1. 标记 active / legacy / experimental 模块。
2. 更新 README 架构图，使其与当前 runtime 一致。
3. 给 `agents_legacy/`、`harness/`、`tools/`、`workflow/` 增加状态说明。
4. 输出一张当前主链路图：

```text
main.py
→ orchestrator graph
→ collect
→ derived/anomaly/signal
→ conflict/verification
→ thesis/review
→ report
```

验收：

- 新贡献者 10 分钟内能判断主路径在哪里。
- 文档不再混用 legacy pipeline 和 active pipeline。

---

## Phase E1：拆分 orchestrator 巨型模块

目标：提升可测试性与演进速度。

任务：

1. 创建 `alphabee/orchestrator/nodes/`。
2. 将 `analyzers.py` 拆为：

```text
nodes/analyze.py
nodes/conflicts.py
nodes/verification.py
nodes/thesis.py
services/company_context.py
services/gap_recorder.py
```

3. `agent.py` 只负责 graph assembly。
4. 每个 node 增加最小 contract test。

验收：

- 单个 orchestrator node 文件不超过 250 行。
- 每个 node 有独立输入/输出测试。
- 新增 InsightAgent 不需要继续修改 900 行大文件。

---

## Phase E2：建立 Pipeline Contract 和 Artifact Schema

目标：让节点之间传递的数据有强契约。

任务：

1. 为主要 artifact 建立 Pydantic model：

```text
FactCollectionArtifact
DerivedFactsArtifact
SignalAnalysisArtifact
AnomalyReportArtifact
ConflictAnalysisArtifact
VerificationArtifact
ThesisArtifact
InsightArtifact
ReportArtifact
```

2. `OrchestratorState` 中尽量减少裸 `dict`。
3. report payload 使用 typed builder。
4. 添加 contract tests。

验收：

- report payload 改动能被测试捕获。
- artifact shape 不再依赖人工记忆。

---

## Phase E3：Schema Registry 与字段依赖校验

目标：解决字段漂移和规则依赖不可见问题。

任务：

1. 新增 schema validation 命令。
2. 校验 Pydantic model 字段与 canonical schema。
3. 校验 derived/signal/anomaly YAML 依赖字段。
4. 校验 adapter mapping target。
5. 生成 dependency graph：

```text
source_field → canonical_field → derived_fact → signal → thesis_dimension
```

验收：

- 类似 `operating_cash_flow` vs `operating_cashflow` 的问题在 CI 中失败。
- 新增规则时自动知道缺哪些字段映射。

---

## Phase E4：统一 Data Provider 架构

目标：把数据获取、fallback、adapter、模型组装分层。

任务：

1. 定义 provider interface。
2. 将 fallback 逻辑集中在 `providers/`。
3. collectors 只做 raw API。
4. adapters 负责 canonical 化。
5. fact tools 只消费 provider 输出。

目标结构：

```text
collectors/raw_api
→ adapters/canonicalize
→ providers/fallback
→ fact_tools/pydantic
→ engines/rules
```

验收：

- 新增 Baostock fallback 不需要改 thesis/signal/report。
- 下游没有外部字段名泄漏。

---

## Phase E5：错误处理与可观测性统一

目标：每个失败都可追踪、可聚合、可自动修复。

任务：

1. 定义 `AlphaBeeError` taxonomy。
2. 替换 silent `except Exception: pass`。
3. 所有 provider/tool/node 失败输出 structured Issue。
4. data_fetch failure record 覆盖 missing field / parse / permission / timeout。
5. Langfuse trace 与 data_fetch issue 建立关联。

验收：

- 用户看到的是“哪个数据源、哪个字段、哪个阶段失败”，不是泛化缺失。
- auto-fix 可以消费更多真实失败。

---

## Phase E6：测试体系升级

目标：让核心 pipeline 可稳定演进。

任务：

1. 添加 offline fixture replay。
2. 添加 full deterministic pipeline test。
3. 添加 all rules smoke test。
4. 添加 adapter mapping coverage test。
5. 添加 report payload golden test。

建议目录：

```text
tests/contracts/
tests/fixtures/
tests/pipeline/
tests/schema_registry/
tests/providers/
```

验收：

- 不依赖外部 API 和 LLM，也能跑主 pipeline。
- 改 rule / schema / adapter 会被对应测试捕获。

---

## Phase E7：CLI / App 层拆分

目标：让 runtime 可被 CLI、Web、API 复用。

任务：

1. 将 `main.py` 拆到 `alphabee/apps/cli/`。
2. 抽出 renderer。
3. 抽出 streaming event parser。
4. 抽出 chat/session manager。
5. 保留根目录 `main.py` 为 thin wrapper。

验收：

- CLI UI 改动不影响 orchestrator。
- 后续 Web/API 不需要复制 CLI 渲染逻辑。

---

## Phase E8：工程工具链与 CI

目标：提升长期维护质量。

任务：

1. 引入 ruff。
2. 引入 pyright 或 mypy。
3. 引入 pytest-cov。
4. 引入 pre-commit。
5. 建立 CI 分层：

```text
quick:
  ruff + unit tests

contract:
  schema validation + contract tests

integration:
  marked external API/LLM tests
```

验收：

- PR 能自动发现格式、类型、schema、contract 问题。

---

# 推荐实施顺序

| 优先级 | 阶段 | 目的 |
|---|---|---|
| P0 | E0 架构盘点与边界标注 | 降低认知成本 |
| P0 | E1 拆分 orchestrator | 为 InsightAgent / 业务迭代减阻 |
| P0 | E3 Schema Registry 校验 | 防止字段漂移 |
| P1 | E2 Pipeline Contract | 提升主链路稳定性 |
| P1 | E4 Data Provider 分层 | 降低数据源扩展成本 |
| P1 | E6 测试体系升级 | 支撑持续重构 |
| P2 | E5 错误与可观测性统一 | 提升自动修复能力 |
| P2 | E7 CLI/App 拆分 | 支持未来产品化 |
| P2 | E8 工程工具链与 CI | 长期质量保障 |

---

# 与业务 Roadmap 的关系

业务 Roadmap 中的重点是：

- InsightAgent
- BusinessModelContext
- Claim-Evidence Graph
- ExpectationFitAgent
- 投资研究备忘录式报告

这些能力都依赖工程侧先完成：

```text
orchestrator 拆分
→ artifact contract
→ schema validation
→ provider 分层
→ deterministic replay tests
```

否则继续叠业务功能会让 `orchestrator/analyzers.py`、schema、provider、report payload 进一步耦合，后续维护成本会快速上升。

建议并行节奏：

```text
Sprint 1:
  E0 + E1 + 业务 Phase 0

Sprint 2:
  E2 + E3 + InsightAgent MVP

Sprint 3:
  E4 + E6 + BusinessModelContext

Sprint 4:
  Claim-Evidence Graph + Report rewrite

Sprint 5:
  ExpectationFitAgent + CI/tooling hardening
```
