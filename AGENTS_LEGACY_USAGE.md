# agents_legacy Usage Inventory

## 结论

当前 active 主流程 **没有运行时依赖** `alphabee/agents_legacy` 中的方法、类或变量。

active 主入口是：

```text
main.py
→ alphabee.orchestrator.agent.alphabee_agent
→ alphabee/orchestrator/*
→ alphabee/agents/*
```

全仓库搜索结果显示，`alphabee/agents_legacy` 的引用只出现在：

1. 文档说明：
   - `README.md`
   - `CLAUDE.md`
   - `ENGINEERING_ROADMAP.md`
2. 一个注释：
   - `alphabee/orchestrator/state.py`
3. `alphabee/agents_legacy` 包内部自引用。

因此，直接删除 `alphabee/agents_legacy/` 不应破坏当前 active runtime，但删除前建议同步清理文档和注释。

---

## Active 主流程未使用的 legacy symbols

以下 legacy 方法/变量目前只在 `alphabee/agents_legacy` 包内部使用，不被 active orchestrator 调用。

### `alphabee/agents_legacy/orchestrator/agent.py`

| Symbol | 类型 | 状态 |
|---|---|---|
| `alphabee_agent` | DeepAgents root agent | legacy only |

说明：

- 旧版 DeepAgents 根代理。
- 注册了 `FundamentalAgent`、`MarketAgent`、`RiskAgent`、`CrossAnalysisAgent`。
- 当前 `main.py` 没有引用它；当前入口引用的是 `alphabee.orchestrator.agent.alphabee_agent`。

### `alphabee/agents_legacy/fundamental/agent.py`

| Symbol | 类型 | 状态 |
|---|---|---|
| `fundamental_agent_factory(resultType, example="")` | factory function | legacy only |

说明：

- 旧基本面分析子代理。
- 使用 `query_tushare`、`web_search`、`web_search_guard`、`check_message_limit`。
- 当前 active pipeline 已由 `alphabee/agents/facts/`、`DerivedFactsEngine`、`SignalEngine` 和 `ThesisEngine` 替代。

### `alphabee/agents_legacy/market/agent.py`

| Symbol | 类型 | 状态 |
|---|---|---|
| `market_agent_factory(resultType, example="")` | factory function | legacy only |

说明：

- 旧行情分析子代理。
- 使用 `query_tushare` 和 DeepAgents skill。
- 当前 active pipeline 中市场结构化数据来自 `alphabee.agents.facts.tools.market_fact.get_market_facts_model`。

### `alphabee/agents_legacy/risk/agent.py`

| Symbol | 类型 | 状态 |
|---|---|---|
| `risk_agent_factory(resultType, example="")` | factory function | legacy only |

说明：

- 旧风险分析子代理。
- 使用 `web_search`、`get_market_data`、`get_fundamentals`、`get_stock_news_summary`。
- 当前 active pipeline 的风险表达主要来自 signal/anomaly/conflict/thesis review。

### `alphabee/agents_legacy/industry/agent.py`

| Symbol | 类型 | 状态 |
|---|---|---|
| `industry_agent_factory(resultType, example="")` | factory function | legacy only |

说明：

- 旧行业分析子代理。
- 使用 `get_industry_fundamentals`。
- 在 legacy orchestrator 中的注册代码已被注释。
- active pipeline 当前使用 `alphabee.agents.facts.tools.industry_fact.get_industry_fact` 构建 company context。

### `alphabee/agents_legacy/cross/agent.py`

| Symbol | 类型 | 状态 |
|---|---|---|
| `CrossAnalysisState` | TypedDict | legacy only / conceptual reference |
| `collect_subagent_artifacts` | LangGraph node | legacy only |
| `run_harness` | LangGraph node | legacy only |
| `supplement_if_needed` | LangGraph node | legacy only |
| `finalize_message` | LangGraph node | legacy only |
| `cross_agent` | compiled LangGraph | legacy only |

说明：

- 这是旧版综合分析链路。
- 当前 `alphabee/orchestrator/state.py` 注释写着 `OrchestratorState` mirrors `CrossAnalysisState`，只是概念来源，不是 runtime import。
- 当前 active orchestrator 已有自己的 graph：`collect_raw_facts → run_analysis_engines → explore_conflicts → verify_hypotheses → run_thesis → review_thesis → generate_report → finalize_message`。

---

## 删除前需要清理的非代码引用

### 1. `alphabee/orchestrator/state.py`

当前注释：

```python
Mirrors CrossAnalysisState from agents_legacy/cross/agent.py
```

建议改成：

```python
Defines the active orchestrator state shared by the LangGraph pipeline.
```

### 2. `CLAUDE.md`

包含 legacy architecture 说明。删除 legacy 后应更新：

- 移除或压缩 `Legacy agents — alphabee/agents_legacy/` 小节。
- 保留一行历史说明即可，例如：

```text
The previous DeepAgents-based pipeline has been removed; the active runtime is alphabee/orchestrator.
```

### 3. `README.md`

如果 README 里仍提到 `agents_legacy/`，删除 legacy 后应同步移除或改为历史说明。

### 4. `ENGINEERING_ROADMAP.md`

工程路线图中将 `agents_legacy/` 标记为 legacy。删除后应更新为 “removed legacy pipeline”。

---

## active 主流程对应替代关系

| Legacy capability | Legacy symbol | Active replacement |
|---|---|---|
| 根代理编排 | `agents_legacy.orchestrator.agent.alphabee_agent` | `alphabee.orchestrator.agent.alphabee_agent` |
| 基本面分析 | `fundamental_agent_factory` | `collect_raw_facts` + `get_financial_facts_model` + `DerivedFactsEngine` |
| 行情分析 | `market_agent_factory` | `get_market_facts_model` + market facts canonical model |
| 风险分析 | `risk_agent_factory` | `SignalEngine` + `AnomalyEngine` + `explore_conflicts` |
| 综合交叉分析 | `cross_agent` | `explore_conflicts` + `verify_hypotheses` + `run_thesis` + `review_thesis` |
| 行业分析 | `industry_agent_factory` | `get_industry_fact` + company context / future BusinessModelContext |

---

## 建议删除步骤

### Step 1：确认无运行时 import

运行：

```bash
rg "agents_legacy|alphabee\\.agents_legacy" .
```

预期只剩文档或注释。

### Step 2：清理文档和注释

删除或更新：

- `alphabee/orchestrator/state.py` 中的 legacy 注释
- `README.md`
- `CLAUDE.md`
- `ENGINEERING_ROADMAP.md`

### Step 3：删除目录

```bash
rm -rf alphabee/agents_legacy
```

### Step 4：运行验证

```bash
poetry run pytest tests/orchestrator tests/agents tests/data_fetch -q
python -m py_compile main.py alphabee/orchestrator/agent.py alphabee/orchestrator/analyzers.py
```

### Step 5：更新架构文档

删除后，文档中应明确：

```text
Active runtime: alphabee/orchestrator
Removed runtime: alphabee/agents_legacy
```

---

## 风险评估

删除 `alphabee/agents_legacy/` 的主要风险不是 runtime break，而是：

1. 文档仍引用旧架构，造成认知混乱。
2. 某些旧 prompt 中可能还有可复用业务表达，删除前如需保留可迁移到 active prompt。
3. `cross/agent.py` 中旧 harness 编排模式仍有参考价值，但 active orchestrator 已经重写了对应能力。

如果要保留历史参考，建议不要保留在 package 内，而是迁出到：

```text
docs/archive/legacy_agents.md
```

