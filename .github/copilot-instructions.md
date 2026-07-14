# AlphaBee Copilot Instructions

## Commands

- Install dependencies: `poetry install`
- Show CLI options: `poetry run python main.py --help`
- Run a one-shot analysis: `poetry run python main.py "帮我分析一下宁德时代的投资价值"`
- Start chat mode: `poetry run python main.py --chat`
- Run the monitoring workflow: `poetry run python main.py --monitor-framework path/to/framework.md --symbol 300750 --monitor-periods 8`

There is currently no repository-defined lint command, test command, or `tests/` suite in this repo, so there is no single-test command to use yet.

## High-level architecture

- `main.py` is the only entrypoint. It handles CLI parsing, pretty terminal rendering, chat history, and the optional monitor workflow.
- Normal query flow is `main.py` → `alphabee.agents.orchestrator.agent.alphabee_agent` → subagents → tools → collector helpers → external data providers / LLM.
- The orchestrator is a DeepAgents graph that routes to five compiled subagents:
  - `FundamentalAgent` for multi-period company fundamentals
  - `MarketAgent` for latest quote / valuation / money flow
  - `RiskAgent` for combined risk review using fundamentals, market data, news, and optional web search
  - `CrossAnalysisAgent` for cross-checking the outputs of the fundamental, market, and risk agents
  - `IndustryAgent` for sector-level performance, valuation history, and constituents
- Most business logic lives in `alphabee/tools/`, not in the agent files. Agents are mostly prompt + tool wiring.
- The tool layer returns structured Pydantic models. `get_fundamentals` and `get_industry_fundamentals` also call the configured LLM to synthesize JSON summaries on top of raw market data.
- Provider access is wrapped in `alphabee/collectors/*/helper.py`. Use these helpers instead of calling Tushare or AkShare directly so you keep the existing retry, result-wrapper, and export behavior.
- The separate workflow path is `alphabee/workflow/framework_monitor.py`. It gathers fundamentals, market data, news, and web search results, asks the LLM for a structured monitoring report, then writes snapshots and reports under `data/<symbol>/monitor_snapshots/` and `data/<symbol>/monitor_reports/`.

## Key conventions

- Configuration is loaded from the repository root `config.yaml` through `alphabee.config.settings`. The loader expands `${ENV_VAR}` and `${ENV_VAR:default}` placeholders, so keep secrets and environment-specific values there instead of hardcoding them.
- Preserve the quantitative vs qualitative boundary around `web_search`. `alphabee.middleware.web_search_guard` explicitly blocks using search for prices, valuation, financials, or sector performance, and injects verification instructions when numeric data appears in search output. Structured tools are the source of truth for numbers.
- Normalize stock symbols to Tushare format (`600519.SH`, `000001.SZ`, etc.). Both `fundamentals.py` and `market_data.py` have local `_normalize_ts_code` helpers, and downstream code expects normalized A-share symbols.
- Keep tool outputs structured. The prompts expect JSON responses, and the tool functions expose strongly shaped Pydantic models such as `Fundamentals`, `MarketData`, and `IndustryFundamentals`.
- The implemented system lives in `agents/`, `tools/`, `collectors/`, `config/`, `middleware/`, and `workflow/`. Directories like `apps/`, `memory/`, and `mcp_server/` exist but are not meaningfully wired into the current runtime, so inspect before assuming they are active extension points.
- Logging uses `structlog` with a daily rotating JSON log file in `logs/alpha_arena.log`. `main.py` suppresses most console logging so the streamed terminal UI stays readable.
- Conversation length is capped by `alphabee.middleware.common.check_message_limit`; long-lived chat changes should account for the current 50-message cutoff.
