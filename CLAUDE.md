# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
poetry install

# Run the CLI (single query)
poetry run python main.py "帮我分析一下宁德时代的投资价值"

# Interactive chat mode
poetry run python main.py --chat

# Disable terminal colors (for log redirection)
poetry run python main.py --no-color "分析一下比亚迪"

# Run all tests
poetry run pytest

# Run a single test file
poetry run pytest tests/agents/derived_facts/test_accounts_receivable_yoy.py

# Run tests with integration markers only
poetry run pytest -m integration
```

Tests use `asyncio_mode = "auto"` (configured in pyproject.toml), so async test functions don't need explicit decorators.

## Architecture

AlphaBee is a multi-agent investment analysis system for the A-share market, built on **LangGraph** + **DeepAgents**. There are two parallel tracks:

### Active pipeline — `alphabee/orchestrator/`

`main.py` uses the Harness-driven orchestrator at `alphabee/orchestrator/agent.py`, which compiles a LangGraph `StateGraph` with this flow:

```
START → collect_facts → run_harness → supplement_if_needed ⇄ run_harness → finalize_message → END
```

- **`collect_facts`** (`orchestrator/collectors.py`): Runs FactCollectorAgent + DerivedFactEngine + SignalEngine sequentially, wraps results as `Artifact`/`Observation` objects.
- **`run_harness`**: Delegates to `HarnessRuntime` (planner → reporter ⇄ critic → evaluator).
- **`supplement_if_needed`**: Detects high/critical `missing_data`/`data_gap` issues from harness critique and re-invokes FactCollectorAgent (max 1 round). On successful supplement, wipes harness-internal state and re-runs harness from scratch.
- **`finalize_message`**: Merges all artifacts into a JSON `AIMessage` for streaming output.

### Legacy agents — `alphabee/agents_legacy/`

The legacy orchestrator (`agents_legacy/orchestrator/agent.py`) is a DeepAgents-based root agent with `CompiledSubAgent`s (FundamentalAgent, MarketAgent, RiskAgent, CrossAnalysisAgent). The CrossAnalysisAgent (`agents_legacy/cross/agent.py`) was the original Harness integration pattern — the new orchestrator replicated its design but swapped in next-gen agents.

### New-Gen layered pipeline — `alphabee/agents/`

Under active development, replacing the legacy subagents:

1. **FactCollectorAgent** (`agents/facts/`) — 8 domain-specific tools (company_profile, financial_fact, market_fact, operation_fact, industry_fact, competition_fact, expectation_fact, risk_fact) + web_search. Returns AlphaBee canonical field names via the adapter layer. Built with `deepagents.create_deep_agent`.
2. **DerivedFactAgent** (`agents/derived_facts/`) — 21 YAML-defined rules across 7 dimensions (profitability, growth, solvency, efficiency, valuation, cashflow, risk). Rules declare `required_facts` (canonical fields) and/or `required_derived_facts` (upstream rules), forming a DAG resolved via **topological sort**. Formulas use a safe AST evaluator — only arithmetic and comparison operators are whitelisted.
3. **SignalAgent** (`agents/signal/`) — Takes derived facts and generates structured signals with risk levels (prototype, 3 rules).
4. **FactAnalysisAgent** (`agents/fact_analysis/`) — Placeholder.

### Harness Runtime — `alphabee/harness/`

A reusable LangGraph execution harness providing:

- **planner** → **reporter** ⇄ **critic** (iterative, up to 3 rounds) → **evaluator**
- All nodes output **only** structured objects (`Artifact`, `Decision`, `Issue`) — no free text. The model is prompted with JSON output instructions and responses are parsed/coerced through `ThinkingNodeOutput`.
- The `critic` triggers a reporter rewrite when it detects evidence gaps, conflicts, time mismatches, or numeric inconsistencies (controlled by `REWRITE_TRIGGER_CATEGORIES` and keyword matching).
- `HarnessStateCompressor` provides role-aware context slicing (3 tiers: full content for the producing node, summaries for most nodes, claim→evidence maps for review/evaluation nodes). Two-stage pipeline: rule-based deterministic slicing first, then LLM summarization if still over threshold.
- Supports custom prompts per instance (`reporter_prompt`, `critic_prompt`, `evaluator_prompt`).

### Core data model — `alphabee/core/schemas.py`

Pydantic models that carry through the entire pipeline:

- **`Run`** / **`Step`**: Execution lifecycle (status, timestamps, goal, context).
- **`Artifact`**: Any output (data, plan, report, critique, evaluation) with `type`, `value`, `producer_step`, and `role_group` (auto-inferred from type string via `_ARTIFACT_TYPE_TO_ROLE_GROUP`).
- **`Observation`**: Timestamped facts with freshness classification (`ObservationFreshness`).
- **`Decision`**: Judgments with `maker`, `confidence` (0-1), `rationale`, `evidence_refs` (typed references to artifacts/observations/decisions — with LLM coercion for common invalid values).
- **`Issue`**: Problems with `severity`, `category`, `scope` (which pipeline stage produced it: planning/data/report/review/evaluation), `status`.

### Schema Registry + Adapters — `alphabee/schemas/` + `alphabee/adapters/`

- `schemas/INDEX.yaml` defines 7 domains with 125+ canonical field names — the single source of truth.
- `adapters/tushare/` and `adapters/akshare/` contain YAML mapping files translating source API field names → canonical names, decoupling agents from data sources.

### Configuration — `config.yaml` + `alphabee/config/`

- `config.yaml` at project root supports `${ENV_VAR}` and `${ENV_VAR:default}` placeholders.
- `alphabee.config.get_settings()` returns a Pydantic `Settings` object.
- All LLM instances are created through `alphabee.utils.create_chat_model(component_name)` which reads the shared LLM config and applies component-specific kwargs. Model components (`harness.planner`, `harness.reporter`, `agent.facts`, etc.) get dedicated `ChatOpenAI` instances cached via `@lru_cache`.

### Middleware

- **`web_search_guard`**: Three-level enforcement — (1) pre-call keyword block prevents web_search for price/valuation/financial terms, (2) post-call disclaimer warns results are qualitative only, (3) post-call numeric scan injects verification instructions if numbers are detected.
- **`check_message_limit`**: Truncates conversation history to prevent context overflow.

## Key patterns

- **Factory functions**: Each agent is created via a factory (e.g., `fact_collector_agent_factory()`), NOT a module-level constant. This prevents state leakage and ensures fresh middleware/config per invocation.
- **Subagents as CompiledSubAgent**: Subagents wrap factory-created runnables in `CompiledSubAgent(name=..., description=..., runnable=...)` registered on the parent `create_deep_agent`.
- **YAML-driven rules**: Derived fact and signal rules live in `agents/derived_facts/rules/*.yaml` and `agents/signal/rules/*.yaml`. Each rule declares dependencies declaratively; the engine handles topological ordering and safe formula evaluation.
- **`@lru_cache` for expensive construction**: `HarnessRuntime`, model instances, and compressor models use `@lru_cache` to amortize construction cost.
- **State reset on supplement**: When supplement collects new data, the orchestrator clears `decisions`/`observations`/harness-internal `steps` and re-runs the full harness — DerivedFact/Signal engines are deterministic and re-execute automatically with fresh inputs.
- **Streaming with subgraphs**: `main.py` streams with `subgraphs=True` to capture events from both parent and child graphs. Namespace tuples are parsed (`_parse_namespace`) to display the agent hierarchy in the CLI.
