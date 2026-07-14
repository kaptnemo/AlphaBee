"""Fix executor — uses Claude Agent SDK to auto-fix data fetch failures.

Workflow::

    # Step 1: prepare and let the agent fix
    poetry run alphabee-fetch run-fix 3

    # The agent will:
    #   1. Read the task context from SQLite
    #   2. Create a git branch
    #   3. Read relevant source files
    #   4. Make code edits
    #   5. Run tests, commit, push, and create/update the MR

    # Step 2 (optional): re-run verification and submission
    poetry run alphabee-fetch verify 3
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from alphabee.data_fetch.database import get_session, init_db
from alphabee.data_fetch.models import (
    DataFetchEvent,
    DataFetchIssue,
    DataFixTask,
    TaskStatus,
)

_PROJECT_ROOT = Path(__file__).parent.parent.parent


# ── data structures ────────────────────────────────────────────────────


@dataclass
class FixResult:
    success: bool
    message: str
    mr_url: str | None = None
    branch: str | None = None


# ── public API ─────────────────────────────────────────────────────────


def build_agent_prompt(task_id: int) -> str:
    """Build a comprehensive agent prompt from a fix task."""
    ctx = _load_task(task_id)
    if ctx is None:
        return f"Task #{task_id} not found."
    return _build_prompt(
        issue=ctx["issue"],
        sample_event=ctx["sample_event"],
        task_context=ctx["task_context"],
        fix_branch=ctx["fix_branch"],
        task_id=task_id,
    )


async def prepare_and_run_fix(task_id: int) -> FixResult:
    """Create git branch and invoke Claude Agent SDK to fix the issue.

    This is an async function that streams agent progress to stdout.
    """
    from claude_agent_sdk import ClaudeAgentOptions, query

    ctx = _load_task(task_id)
    if ctx is None:
        return FixResult(success=False, message=f"Task #{task_id} not found")

    issue = ctx["issue"]
    fix_branch = ctx["fix_branch"]

    # ── create git branch ─────────────────────────────────────────────
    result = _create_branch(fix_branch)
    if not result.success:
        return result

    # ── mark task as running ──────────────────────────────────────────
    _update_task_status(task_id, TaskStatus.RUNNING)

    # ── build agent prompt ────────────────────────────────────────────
    prompt = _build_prompt(
        issue=issue,
        sample_event=ctx["sample_event"],
        task_context=ctx["task_context"],
        fix_branch=fix_branch,
        task_id=task_id,
    )

    # ── invoke Claude agent ───────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"Claude Agent is fixing: {issue['title']}")
    print(f"Branch: {fix_branch}")
    print(f"{'='*60}\n")

    try:
        full_output: list[str] = []
        async for msg in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                allowed_tools=["Read", "Edit", "Bash", "Grep", "Glob"],
                permission_mode="acceptEdits",
                cwd=str(_PROJECT_ROOT),
            ),
        ):
            text = _render_message(msg)
            if text:
                full_output.append(text)

        combined = "\n".join(full_output)

        # ── check for CANNOT_FIX marker ────────────────────────────────
        if "CANNOT_FIX" in combined:
            _update_task_status(task_id, TaskStatus.FAILED)
            reason = combined.split("CANNOT_FIX:", 1)[1].split("\n")[0].strip() if "CANNOT_FIX:" in combined else "unknown"
            print(f"\n{'='*60}")
            print(f"Agent cannot fix this issue: {reason}")
            print(f"Task #{task_id} marked as FAILED.")
            print(f"{'='*60}")
            return FixResult(
                success=False,
                message=f"CANNOT_FIX: {reason}",
                branch=fix_branch,
            )

        verify_result = verify_and_submit(task_id)
        if verify_result.success:
            print(f"\n{'='*60}")
            print("Agent fix completed and submitted.")
            if verify_result.mr_url:
                print(f"MR: {verify_result.mr_url}")
            print(f"{'='*60}")
            return verify_result

        return verify_result

    except Exception as exc:
        _update_task_status(task_id, TaskStatus.FAILED)
        return FixResult(
            success=False,
            message=f"Agent failed: {exc}",
            branch=fix_branch,
        )


def verify_and_submit(task_id: int) -> FixResult:
    """Run tests, commit changes, push, and mark issue as fixed.

    Must be called on the fix branch after the agent has made changes.
    """
    ctx = _load_task(task_id)
    if ctx is None:
        return FixResult(success=False, message=f"Task #{task_id} not found")

    issue = ctx["issue"]
    issue_id = issue["issue_id"]
    fix_branch = ctx["fix_branch"]
    task_status = ctx.get("task_status")

    if task_status == TaskStatus.DONE.value:
        return FixResult(
            success=True,
            message="Task already verified.",
            mr_url=ctx.get("verification_result") or None,
            branch=fix_branch,
        )

    try:
        # 1. Check branch
        current = _run_git("branch", "--show-current").strip()
        if current != fix_branch:
            message = f"Expected branch '{fix_branch}', currently on '{current}'."
            _update_task_status(task_id, TaskStatus.FAILED)
            _update_task_verification_result(task_id, message)
            return FixResult(success=False, message=message)

        # 2. Check for changes
        status = _run_git("status", "--porcelain")
        base_branch = _get_default_base_branch()
        ahead_count = 0
        try:
            ahead_count = int(
                _run_git("rev-list", "--count", f"{base_branch}..HEAD").strip() or "0"
            )
        except RuntimeError:
            ahead_count = 0

        if not status.strip() and ahead_count == 0:
            message = "No changes detected. The agent may not have made edits."
            _update_task_status(task_id, TaskStatus.FAILED)
            _update_task_verification_result(task_id, message)
            return FixResult(success=False, message=message)

        # 3. Run tests
        test_result = _run_tests()
        if not test_result.success:
            _update_task_status(task_id, TaskStatus.FAILED)
            _update_task_verification_result(task_id, test_result.message)
            return test_result

        # 4. Stage, commit, push
        title = issue["title"]
        provider = issue["provider"]
        api_name = issue["api_name"]
        error_type = issue["error_type"]
        occurrence_count = issue["occurrence_count"]

        if status.strip():
            _run_git("add", "-A")
            _run_git(
                "commit",
                "-m",
                f"fix: {title}\n\n"
                f"Resolves data_fetch issue #{issue_id}\n"
                f"Provider: {provider}\n"
                f"API: {api_name}\n"
                f"Error type: {error_type}\n"
                f"Occurrences: {occurrence_count}\n",
            )
        _run_git("push", "-u", "origin", fix_branch)

        # 5. Create or reuse a pull request
        body = (
            f"Resolves data_fetch issue #{issue_id}\n\n"
            f"- Provider: {provider}\n"
            f"- API: {api_name}\n"
            f"- Error type: {error_type}\n"
            f"- Occurrences: {occurrence_count}\n"
            f"- Task ID: {task_id}\n"
        )
        mr_url = _create_or_get_pull_request(
            branch=fix_branch,
            title=f"fix: {title}",
            body=body,
        )

        # 6. Mark as fixed
        from alphabee.data_fetch.scanner import mark_issue_fixed, mark_task

        mark_task(
            task_id,
            "done",
            f"Fixed via branch {fix_branch}; MR: {mr_url}",
            verification_result=mr_url,
        )
        mark_issue_fixed(
            issue_id,
            resolution_note=f"Fixed via branch {fix_branch}",
            verification_status="passed",
        )

        return FixResult(
            success=True,
            message="Fix verified and submitted.",
            mr_url=mr_url,
            branch=fix_branch,
        )
    except Exception as exc:
        _update_task_status(task_id, TaskStatus.FAILED)
        _update_task_verification_result(task_id, str(exc))
        return FixResult(
            success=False,
            message=f"Verification failed: {exc}",
            branch=fix_branch,
        )


def prepare_fix(task_id: int) -> FixResult:
    """Prepare a fix: create git branch and return agent prompt.

    This is the non-SDK version — just outputs the prompt for manual use.
    """
    ctx = _load_task(task_id)
    if ctx is None:
        return FixResult(success=False, message=f"Task #{task_id} not found")

    fix_branch = ctx["fix_branch"]
    result = _create_branch(fix_branch)
    if not result.success:
        return result

    prompt = _build_prompt(
        issue=ctx["issue"],
        sample_event=ctx["sample_event"],
        task_context=ctx["task_context"],
        fix_branch=fix_branch,
        task_id=task_id,
    )

    _update_task_status(task_id, TaskStatus.RUNNING)

    return FixResult(
        success=True,
        message=prompt,
        branch=fix_branch,
    )


# ── task loading ───────────────────────────────────────────────────────


def _load_task(task_id: int) -> dict | None:
    """Load fix task + issue + sample event from database.

    Returns a dict with keys: task_status, result_summary, verification_result,
    issue, sample_event, task_context, fix_branch.
    """
    init_db()
    session = get_session()
    try:
        task = (
            session.query(DataFixTask)
            .filter(DataFixTask.task_id == task_id)
            .first()
        )
        if task is None:
            return None

        issue = (
            session.query(DataFetchIssue)
            .filter(DataFetchIssue.issue_id == task.issue_id)
            .first()
        )
        if issue is None:
            return None

        sample_event = (
            session.query(DataFetchEvent)
            .filter(DataFetchEvent.event_id == issue.sample_event_id)
            .first()
        ) if issue.sample_event_id else None

        return {
            "task_status": task.status.value,
            "result_summary": task.result_summary,
            "verification_result": task.verification_result,
            "issue": {
                "issue_id": issue.issue_id,
                "title": issue.title,
                "provider": issue.provider,
                "api_name": issue.api_name,
                "error_type": issue.error_type.value,
                "fix_strategy": issue.fix_strategy.value if issue.fix_strategy else "N/A",
                "occurrence_count": issue.occurrence_count,
                "first_seen_at": issue.first_seen_at.isoformat() if issue.first_seen_at else "N/A",
                "last_seen_at": issue.last_seen_at.isoformat() if issue.last_seen_at else "N/A",
                "fingerprint": issue.fingerprint,
            },
            "sample_event": {
                "symbol": sample_event.symbol if sample_event else None,
                "error_message": sample_event.error_message if sample_event else None,
                "severity": sample_event.severity.value if sample_event else "N/A",
                "request_payload": sample_event.request_payload if sample_event else None,
                "missing_fields": sample_event.missing_fields if sample_event else None,
            } if sample_event else None,
            "task_context": task.prompt_context,
            "fix_branch": f"fix/data-fetch-{issue.issue_id}",
        }
    finally:
        session.close()


# ── prompt builder ─────────────────────────────────────────────────────


def _build_prompt(
    issue: dict,
    sample_event: dict | None,
    task_context: str | None,
    fix_branch: str,
    task_id: int,
) -> str:
    """Build a comprehensive agent prompt for the Claude Agent SDK."""
    provider = issue["provider"]
    api_name = issue["api_name"]
    error_type = issue["error_type"]
    title = issue["title"]

    parts = [
        "# Auto-Fix Data Fetch Failure",
        "",
        f"You are fixing a data fetch failure in the AlphaBee project.",
        f"You are on git branch `{fix_branch}`.",
        f"You are working on task #{task_id}.",
        "",
        "## Issue",
        f"- **Title**: {title}",
        f"- **Issue ID**: {issue['issue_id']}",
        f"- **Failed provider**: {provider}",
        f"- **Failed API**: {api_name}",
        f"- **Error type**: {error_type}",
        f"- **Fix strategy**: {issue['fix_strategy']}",
        f"- **Occurrences**: {issue['occurrence_count']}",
        "",
    ]

    if sample_event:
        parts.extend([
            "## Sample Failure",
            f"- **Symbol**: {sample_event.get('symbol') or 'N/A'}",
            f"- **Error**: {sample_event.get('error_message') or 'N/A'}",
            f"- **Severity**: {sample_event.get('severity')}",
        ])
        if sample_event.get("request_payload"):
            parts.append(
                f"- **Request**: {json.dumps(sample_event['request_payload'], ensure_ascii=False)}"
            )
        if sample_event.get("missing_fields"):
            parts.append(
                f"- **Missing fields**: {', '.join(sample_event['missing_fields'])}"
            )
        parts.append("")

    if task_context:
        parts.extend(["## Fix Plan", task_context, ""])

    # ── core: layered fallback strategy ────────────────────────────────
    src = _datasource_guidance(provider, api_name)

    parts.extend([
        "## Data Source Fallback Strategy",
        "",
        "When the current data source cannot provide the required business data, "
        "try alternatives in this **strict priority order**:",
        "",
        "| Priority | Source  | Module |",
        "|----------|---------|--------|",
        "| 1 (highest) | Tushare  | `alphabee/collectors/tushare/helper.py` |",
        "| 2          | AkShare  | `alphabee/collectors/akshare/helper.py` |",
        "| 3          | Baostock | `alphabee/collectors/baostock/helper.py` |",
        "| 4 (lowest) | Eastmoney | `alphabee/collectors/eastmoney/helper.py` |",
        "",
    ])

    if src:
        parts.extend(src)
    else:
        parts.extend([
            "### How to add a fallback",
            "",
            "Create a new provider module under `alphabee/providers/<domain>.py` following",
            "the reference implementation in `alphabee/providers/industry.py`.  Then update",
            "the fact tool to delegate one line to the provider.  Example:",
            "",
            "```python",
            "# 1. Create alphabee/providers/financial.py",
            "from dataclasses import dataclass, field",
            "",
            "@dataclass",
            "class FinancialResult:",
            "    data: list[dict] = field(default_factory=list)",
            "    error: str | None = None",
            "    source: str = \"\"",
            "",
            "def get_income(symbol: str, periods: int = 8) -> FinancialResult:",
            "    result = _try_tushare_income(symbol, periods)",
            "    if result is not None:",
            "        return result",
            "    result = _try_akshare_income(symbol, periods)",
            "    if result is not None:",
            "        return result",
            "    return FinancialResult(source=\"none\", error=\"All sources exhausted\")",
            "",
            "# 2. In the fact tool (e.g. financial_fact.py):",
            "from alphabee.providers.financial import get_income",
            "result = get_income(symbol)",
            "data = result.data",
            "error = result.error",
            "```",
            "",
        ])

    parts.extend([
        "### Degradation: use related available data",
        "",
        "If NO alternative source works, check if related fields can substitute:",
        "- `roe` unavailable → try `roe_ttm` or compute from `net_profit / equity`",
        "- `pe_ttm` unavailable → try `pe` or compute from `market_cap / net_profit`",
        "- `gross_margin` unavailable → compute from `(revenue - cost) / revenue`",
        "- `operating_cashflow` unavailable → check `net_cashflow` as approximation",
        "",
        "### Give up gracefully",
        "",
        "If ALL sources fail and no degradation works:",
        "1. Do NOT make up data or force an incorrect fix.",
        "2. Leave the code as-is (the failure will continue to be recorded).",
        "3. Output a message: `CANNOT_FIX: <reason>`.",
        "4. The issue stays open and will accumulate more events for later analysis.",
        "",
        "## Analysis Methodology — how to decide between switching vs degrading",
        "",
        "Before making changes, apply this decision framework.  Use the concrete ",
        "example of `sw_daily` failure (industry daily行情 data) as a reference.",
        "",
        "### Step 1: Trace downstream consumers",
        "",
        "Read the code that consumes the failing API's output to understand which ",
        "**canonical fields** are required and HOW they are used.",
        "",
        "Example: `sw_daily` → traced downstream:",
        "- `_build_company_context()` (`analyzers.py:181-184`) reads `industry_pe_ttm` / `industry_pb`",
        "- `render()` in the fact tool outputs PE(TTM) / PB columns",
        "- **Conclusion**: PE and PB are HARD requirements, not optional",
        "",
        "### Step 2: Build a capability matrix for candidate alternatives",
        "",
        "For each candidate data source, list which canonical fields it CAN provide:",
        "",
        "| Candidate | close | pct_chg | PE | PB | Time series |",
        "|-----------|-------|---------|----|----|-------------|",
        "| sw_daily (current) | ✓ | ✓ | ✓ | ✓ | 90 days |",
        "| index_daily (Tushare) | ✓ | ✓ | ✗ | ✗ | 90 days |",
        "| board_hist (AkShare) | ✓ | ✓ | ✗ | ✗ | 90 days |",
        "| board_snapshot (AkShare) | ✓ | - | ✓ | ✓ | Snapshot only |",
        "",
        "**Key insight**: NO single alternative source provides all fields.  The best ",
        "solution is often a **combination**: `index_daily` (trend) + AkShare snapshot (PE/PB).",
        "",
        "### Step 3: Evaluate data consistency risk",
        "",
        "- Same classification system? (e.g. both use 申万2021 codes → low risk)",
        "- Different classification? (e.g. 申万 vs 东方财富 industry names → matching risk)",
        "- Need name-based matching? (fuzzy match on industry name → added complexity)",
        "",
        "**Principle**: prefer solutions that reuse the SAME identifier (e.g. SW code) ",
        "across sources to avoid fuzzy matching errors.",
        "",
        "### Step 4: Choose the minimal combination",
        "",
        "The winning solution is the combination of sources that:",
        "1. Covers ALL downstream-required fields",
        "2. Minimizes cross-source matching risk",
        "3. Adds the fewest new dependencies",
        "",
        "Example outcome for sw_daily:",
        "  ✓ Tushare index_daily (close + pct_chg, same SW code)",
        "  ✓ AkShare board_snapshot (PE/PB, by industry name match)",
        "  ✗ Full switch to AkShare board_hist (no PE/PB, different classification)",
        "",
        "## Providers Layer — implement fallback in a clean provider module",
        "",
        "Do NOT scatter try/except blocks in business logic files (fact tools).",
        "Instead, extract the fallback chain into a dedicated provider module ",
        "under `alphabee/providers/`.",
        "",
        "### Pattern",
        "",
        "```",
        "alphabee/providers/",
        "└── <domain>.py          # One file per data domain (industry, financial, market, ...)",
        "```",
        "",
        "Each provider module exposes functions named by data domain (e.g. ",
        "`get_industry_daily()`), with the fallback chain inside, returning a ",
        "consistent dataclass regardless of which source succeeded.",
        "",
        "Reference implementation: `alphabee/providers/industry.py`",
        "",
        "### Provider function signature pattern",
        "",
        "```python",
        "from dataclasses import dataclass, field",
        "",
        "@dataclass",
        "class <Domain>Result:",
        "    daily: list[dict] = field(default_factory=list)",
        "    error: str | None = None",
        "    source: str = \"\"   # which path succeeded",
        "",
        "def get_<domain>_<data>(...) -> <Domain>Result:",
        "    # Priority 1: best source",
        "    result = _try_primary(...)",
        "    if result is not None:",
        "        return result",
        "    ",
        "    # Priority 2: fallback combination",
        "    result = _try_fallback_combo(...)",
        "    if result is not None:",
        "        return result",
        "    ",
        "    return <Domain>Result(source=\"none\", error=\"All sources exhausted\")",
        "```",
        "",
        "### How to integrate in the fact tool",
        "",
        "The fact tool (e.g. `industry_fact.py`) changes from:",
        "",
        "```python",
        "# BEFORE: inline try/except mess",
        "try:",
        "    df = helper.sw_daily(...).data",
        "except Exception as e1:",
        "    try:",
        "        df = helper.index_daily(...).data",
        "    except Exception as e2:",
        "        ...",
        "```",
        "",
        "To:",
        "",
        "```python",
        "# AFTER: clean delegation to provider",
        "from alphabee.providers.<domain> import get_<domain>_<data>",
        "",
        "result = get_<domain>_<data>(required_params)",
        "data = result.daily",
        "error = result.error",
        "```",
        "",
        "### Benefits",
        "- Business logic stays clean — one line of delegation",
        "- Fallback chain is testable independently of business code",
        "- New data sources can be added to the provider without touching tool code",
        "- Consistent error/source tracking for debugging",
        "",
        "## Adapter Layer — unify output to AlphaBee canonical fields",
        "",
        "When switching data sources, you MUST add a field-name adapter so the ",
        "new source's raw field names are translated to AlphaBee canonical names ",
        "before reaching downstream consumers.",
        "",
        "### Architecture",
        "",
        "```",
        "Source API (raw names)",
        "  → Adapter YAML (source_name → canonical_name)",
        "  → Adapter class (loads YAML, applies rename)",
        "  → TuShareResult / AkShareResult (wraps DataFrame)",
        "  → Downstream agents (expect canonical names)",
        "```",
        "",
        "### Existing adapters",
        "",
        "| Source   | Adapter file | Mapping dir |",
        "|----------|-------------|-------------|",
        "| Tushare  | `alphabee/adapters/tushare.py` | `alphabee/adapters/tushare/*.yaml` |",
        "| AkShare  | n/a (applied in tool code) | `alphabee/adapters/akshare/*.yaml` |",
        "",
        "### How to add a mapping for a new source",
        "",
        "1. **Find canonical field names** — read `alphabee/schemas/INDEX.yaml` to see",
        "   which domain the data belongs to, then open the domain schema file",
        "   (e.g. `alphabee/schemas/financial.yaml`) to see all canonical field names.",
        "",
        "2. **Create a mapping YAML** in `alphabee/adapters/<source>/`:",
        "```yaml",
        "  # alphabee/adapters/baostock/financial_mapping.yaml",
        "  query_profit_data:",
        "    roeAvg:         roe",
        "    npMargin:       net_margin",
        "    grossProfit:    gross_profit",
        "    ...",
        "```",
        "   - The top-level key is the API method name.",
        "   - Each mapping is `source_field: canonical_field`.",
        "",
        "3. **Create or update the adapter class** following `adapters/tushare.py`:",
        "```python",
        "  class BaostockAdapter:",
        "      def __init__(self):",
        "          self.config = self._load_yaml_dir('alphabee/adapters/baostock/')",
        "",
        "      def adapt(self, method_name: str, df: DataFrame) -> DataFrame:",
        "          if method_name not in self.config:",
        "              return df",
        "          return df.rename(columns=self.config[method_name], errors='ignore')",
        "```",
        "",
        "4. **Apply the adapter** where the result is constructed (e.g., in the helper's",
        "   result wrapper or the tool function itself). Follow the pattern in",
        "   `TuShareResult.__init__` or `AkShareResult.to_dataframe()`.",
        "",
        "### Key rule",
        "The canonical field names are the **contract** between data sources and agents.",
        "If you add a new data source without an adapter, downstream agents will fail",
        "because they expect canonical names (roe, net_margin, revenue_yoy, ...),",
        "not source-specific names (roeAvg, npMargin, total_revenue, ...).",
        "",
    ])

    parts.extend([
        "## Instructions",
        "",
        f"1. **Trace downstream consumers** — read the code that uses the failing API's",
        f"   output to understand which canonical fields are required.  See `Analysis Methodology` above.",
        f"2. **Build a capability matrix** for candidate alternative sources — list which",
        f"   canonical fields each source can provide.  Prefer combinations that cover ALL fields.",
        f"3. Follow the **Data Source Fallback Strategy** to try alternatives in priority order.",
        f"4. **Implement the fix in a provider module** under `alphabee/providers/<domain>.py`,",
        f"   NOT as inline try/except in the fact tool.  See `Providers Layer` above for the pattern.",
        f"5. If all sources fail, try degradation (use related available fields).",
        f"6. If nothing works, output EXACTLY `CANNOT_FIX: <reason>` as your final message and stop.",
        f"   Your task will be automatically marked as FAILED and remain open for later analysis.",
        f"7. Update the fact tool to delegate to the new provider function (one line of delegation).",
        f"8. After the code change is complete, run the verification command from the repo root:",
        f"   `poetry run alphabee-fetch verify {task_id}`",
        f"   It will run tests, commit, push, and create or update the MR.",
        f"9. Do not stop at code edits; finish only after verification succeeds and the MR URL is available.",
        "",
        "## Key Files",
        f"- Primary source (failed): `alphabee/collectors/{provider}/helper.py`",
        "- Fact tools (where API calls happen): `alphabee/agents/facts/tools/*.py`",
        "- **Providers layer** (where fallback chains live): `alphabee/providers/*.py`",
        "- Reference provider: `alphabee/providers/industry.py`",
        "- Canonical schema: `alphabee/schemas/INDEX.yaml` + domain files",
        "- Adapter mappings: `alphabee/adapters/tushare/*.yaml`, `alphabee/adapters/akshare/*.yaml`",
        "",
        "## Rules",
        "- **Always apply the Analysis Methodology before coding** — do not jump to implementation.",
        "- **Extract fallback logic into `alphabee/providers/`, NOT into business code.**",
        "- Follow existing code conventions and patterns (reference `providers/industry.py`).",
        "- Use existing utilities (TuShareHelper, AkShareHelper, retry wrappers).",
        "- **When switching data sources, ALWAYS create an adapter mapping YAML**",
        "  in `alphabee/adapters/<source>/` to translate raw fields → canonical names.",
        "- Reference `alphabee/schemas/INDEX.yaml` for the authoritative canonical field list.",
        "- Do NOT modify files unrelated to this fix.",
        "- If the error is `permission`, the API token is the root cause — check `config.yaml`.",
        "- If the error is `timeout` or `network`, add retry logic BEFORE trying fallback sources.",
    ])

    return "\n".join(parts)


# ── data source guidance ───────────────────────────────────────────────


def _datasource_guidance(provider: str, api_name: str) -> list[str] | None:
    """Return concrete guidance for switching data sources for common APIs."""

    # Map common Tushare APIs → their data domain → alternative sources
    tushare_alternatives: dict[str, dict] = {
        "income": {
            "domain": "利润表数据",
            "akshare": "ak.stock_financial_abstract_ths() 或 ak.stock_profit_sheet_by_report_em()",
            "baostock": "bs.query_profit_data()",
            "eastmoney": "EastmoneyHelper 年报接口",
        },
        "balancesheet": {
            "domain": "资产负债表数据",
            "akshare": "ak.stock_financial_abstract_ths() 或 ak.stock_balance_sheet_by_report_em()",
            "baostock": "bs.query_balance_data()",
        },
        "cashflow": {
            "domain": "现金流量表数据",
            "akshare": "ak.stock_financial_abstract_ths() 或 ak.stock_cash_flow_sheet_by_report_em()",
            "baostock": "bs.query_cash_flow_data()",
        },
        "fina_indicator": {
            "domain": "财务指标（ROE/ROA/毛利率等）",
            "akshare": "ak.stock_financial_analysis_indicator()",
            "degradation": "从 income + balancesheet 原始数据自行计算",
        },
        "daily": {
            "domain": "日线行情",
            "akshare": "ak.stock_zh_a_hist()",
            "baostock": "bs.query_history_k_data_plus()",
        },
        "daily_basic": {
            "domain": "每日指标（PE/PB/换手率）",
            "akshare": "ak.stock_zh_a_spot_em() 计算 PE/PB",
            "degradation": "用 market_cap + net_profit 计算 PE，market_cap + equity 计算 PB",
        },
        "stock_basic": {
            "domain": "股票基本信息",
            "akshare": "ak.stock_info_a_code_name()",
            "local": "alphabee/static/all_stocks.csv",
        },
        "moneyflow": {
            "domain": "资金流向",
            "akshare": "ak.stock_individual_fund_flow()",
        },
        "forecast": {
            "domain": "业绩预告",
            "akshare": "ak.stock_profit_forecast_em()",
        },
    }

    akshare_alternatives: dict[str, dict] = {
        "stock_news_em": {
            "domain": "个股新闻",
            "tushare": "ts.major_news()",
            "baostock": "bs.query_stock_basic() 获取基本信息",
        },
        "stock_financial_analysis_indicator": {
            "domain": "财务指标",
            "tushare": "ts.fina_indicator()",
            "degradation": "从 income + balancesheet 原始数据自行计算",
        },
    }

    if provider == "tushare":
        alt = tushare_alternatives.get(api_name)
    elif provider == "akshare":
        alt = akshare_alternatives.get(api_name)
    else:
        alt = None

    if alt is None:
        return None

    lines = [
        f"### Current API: {provider}/{api_name}",
        f"**Data domain**: {alt['domain']}",
        "",
        "**Alternative sources in priority order**:",
    ]

    for source in ["tushare", "akshare", "baostock", "eastmoney"]:
        key = source if source != provider else None
        if key and key in alt:
            lines.append(f"- **{source}**: `{alt[key]}`")

    if "local" in alt:
        lines.append(f"- **local**: `{alt['local']}`")

    if "degradation" in alt:
        lines.append(f"- **降级方案**: {alt['degradation']}")

    lines.append("")
    return lines


# ── git operations ─────────────────────────────────────────────────────


def _run_git(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {e.stderr.strip()}"
        ) from e


def _get_git_remote_owner_repo() -> tuple[str, str]:
    remote = _run_git("remote", "get-url", "origin").strip()
    if "github.com:" in remote:
        owner_repo = remote.split("github.com:")[1].removesuffix(".git")
    elif "github.com/" in remote:
        owner_repo = remote.split("github.com/")[1].removesuffix(".git")
    else:
        raise RuntimeError("Origin remote is not a GitHub URL.")

    owner = owner_repo.split("/", 1)[0]
    return owner_repo, owner


def _get_default_base_branch() -> str:
    try:
        ref = _run_git("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD").strip()
        if ref.startswith("origin/"):
            return ref.split("/", 1)[1]
    except RuntimeError:
        pass
    return "main"


def _create_branch(branch_name: str) -> FixResult:
    try:
        _run_git("checkout", "-b", branch_name)
        return FixResult(success=True, message=f"Branch {branch_name} created")
    except RuntimeError as e:
        try:
            _run_git("show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}")
            _run_git("checkout", branch_name)
            return FixResult(success=True, message=f"Switched to {branch_name}")
        except RuntimeError:
            pass
        msg = str(e)
        return FixResult(success=False, message=f"Cannot create branch: {msg}")


def _build_mr_url(branch: str) -> str:
    try:
        owner_repo, _ = _get_git_remote_owner_repo()
        base_branch = _get_default_base_branch()
        return f"https://github.com/{owner_repo}/compare/{base_branch}...{branch}?expand=1"
    except Exception:
        return "https://github.com"


def _create_or_get_pull_request(branch: str, title: str, body: str) -> str:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        return _build_mr_url(branch)

    owner_repo, owner = _get_git_remote_owner_repo()
    base_branch = _get_default_base_branch()

    def _api_request(method: str, path: str, payload: dict | None = None) -> tuple[int, str]:
        data = None
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "AlphaBee-data-fetch",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        headers["Authorization"] = "Bearer " + token
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib_request.Request(
            f"https://api.github.com{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib_request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib_error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", errors="replace")

    head_ref = f"{owner}:{branch}"
    status, existing_body = _api_request(
        "GET",
        f"/repos/{owner_repo}/pulls?head={urllib_parse.quote(head_ref)}&state=open&per_page=1",
    )
    if status == 200:
        existing = json.loads(existing_body or "[]")
        if existing:
            return existing[0]["html_url"]

    status, create_body = _api_request(
        "POST",
        f"/repos/{owner_repo}/pulls",
        payload={
            "title": title,
            "head": branch,
            "base": base_branch,
            "body": body,
            "maintainer_can_modify": True,
        },
    )
    if status in (200, 201):
        created = json.loads(create_body or "{}")
        html_url = created.get("html_url")
        if html_url:
            return html_url
        return _build_mr_url(branch)

    if status == 422:
        status, existing_body = _api_request(
            "GET",
            f"/repos/{owner_repo}/pulls?head={urllib_parse.quote(head_ref)}&state=open&per_page=1",
        )
        if status == 200:
            existing = json.loads(existing_body or "[]")
            if existing:
                return existing[0]["html_url"]

    raise RuntimeError(
        f"Unable to create pull request: {create_body.strip() or 'unknown error'}"
    )


# ── test runner ────────────────────────────────────────────────────────


def _run_tests() -> FixResult:
    try:
        result = subprocess.run(
            ["poetry", "run", "pytest", "-m", "not integration", "-q", "--tb=short"],
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode == 0:
            return FixResult(success=True, message="All tests passed.")
        return FixResult(
            success=False,
            message=f"Tests failed:\n{result.stdout[-2000:]}\n{result.stderr[-1000:]}",
        )
    except subprocess.TimeoutExpired:
        return FixResult(success=False, message="Tests timed out.")
    except FileNotFoundError:
        return FixResult(success=False, message="poetry not found.")


def _update_task_status(task_id: int, status: TaskStatus) -> None:
    session = get_session()
    try:
        task = (
            session.query(DataFixTask)
            .filter(DataFixTask.task_id == task_id)
            .first()
        )
        if task:
            task.status = status
            task.updated_at = datetime.now()
            session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()


def _update_task_verification_result(task_id: int, verification_result: str) -> None:
    session = get_session()
    try:
        task = (
            session.query(DataFixTask)
            .filter(DataFixTask.task_id == task_id)
            .first()
        )
        if task:
            task.verification_result = verification_result
            task.updated_at = datetime.now()
            session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()


# ── message rendering ──────────────────────────────────────────────────


def _render_message(msg) -> str:
    """Render a Claude Agent SDK message to stdout.

    Returns the text that was rendered (for CANNOT_FIX detection).
    """
    tname = type(msg).__name__

    if not hasattr(msg, "content"):
        return ""

    blocks = msg.content if isinstance(msg.content, list) else [msg.content]
    rendered: list[str] = []

    for block in blocks:
        bname = type(block).__name__

        if bname == "TextBlock" and hasattr(block, "text") and block.text:
            print(block.text)
            rendered.append(block.text)

        elif bname == "ThinkingBlock":
            pass

        elif bname == "ToolUseBlock" and hasattr(block, "name"):
            if block.name == "Bash":
                cmd = block.input.get("command", "") if isinstance(block.input, dict) else str(block.input)
                print(f"\n[RUN] {cmd[:120]}")
            elif block.name in ("Read", "Edit"):
                fp = block.input.get("file_path", "") if isinstance(block.input, dict) else ""
                print(f"[{block.name}] {fp}")
            elif block.name in ("Grep", "Glob"):
                pat = block.input.get("pattern", "") if isinstance(block.input, dict) else ""
                print(f"[{block.name}] {pat}")

        elif bname == "ToolResultBlock":
            content = block.content if hasattr(block, "content") else str(block)
            if isinstance(content, str) and len(content) > 500:
                content = content[:250] + "\n...\n" + content[-250:]
            if isinstance(content, str) and content.strip():
                print(f"[result] {content[:300]}")

    return "\n".join(rendered)
