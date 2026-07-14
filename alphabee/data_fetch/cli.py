"""CLI for data fetch failure management.

Usage::

    poetry run python -m alphabee.data_fetch                        # show summary
    poetry run python -m alphabee.data_fetch issues                 # list open issues
    poetry run python -m alphabee.data_fetch scan [max_tasks]       # scan + create fix tasks
    poetry run python -m alphabee.data_fetch auto-fix [max_tasks]   # scan + Claude agent + submit
    poetry run python -m alphabee.data_fetch tasks                  # list pending fix tasks
    poetry run python -m alphabee.data_fetch show <issue_id>        # show issue details
    poetry run python -m alphabee.data_fetch fix <issue_id> [--note "..."]  # mark fixed
    poetry run python -m alphabee.data_fetch run-fix <task_id>      # run Claude agent, then verify + submit
    poetry run python -m alphabee.data_fetch verify <task_id>       # test, commit, push, create/update MR
    poetry run python -m alphabee.data_fetch prompt <task_id>       # show agent prompt
    poetry run python -m alphabee.data_fetch stats                  # show statistics
"""

from __future__ import annotations

import sys
from datetime import datetime

from alphabee.data_fetch.database import get_session, init_db
from alphabee.data_fetch.models import (
    DataFetchEvent,
    DataFetchIssue,
    DataFixTask,
    IssueStatus,
    TaskStatus,
)


def _print_issues(status_filter: tuple[str, ...] | None = None) -> None:
    session = get_session()
    try:
        q = session.query(DataFetchIssue).order_by(
            DataFetchIssue.occurrence_count.desc()
        )
        if status_filter:
            q = q.filter(DataFetchIssue.status.in_(status_filter))
        issues = q.limit(50).all()

        if not issues:
            print("No issues found.")
            return

        print(f"{'ID':<6} {'Count':<7} {'Status':<14} {'Provider':<12} {'Title'}")
        print("-" * 100)
        for issue in issues:
            print(
                f"{issue.issue_id:<6} "
                f"{issue.occurrence_count:<7} "
                f"{issue.status.value:<14} "
                f"{issue.provider:<12} "
                f"{issue.title}"
            )
    finally:
        session.close()


def _print_tasks() -> None:
    session = get_session()
    try:
        tasks = (
            session.query(DataFixTask)
            .order_by(DataFixTask.created_at.desc())
            .limit(50)
            .all()
        )

        if not tasks:
            print("No fix tasks found.")
            return

        print(f"{'Task ID':<8} {'Issue':<8} {'Status':<10} {'Target'}")
        print("-" * 100)
        for task in tasks:
            print(
                f"{task.task_id:<8} "
                f"{task.issue_id:<8} "
                f"{task.status.value:<10} "
                f"{task.patch_target or 'N/A'}"
            )
    finally:
        session.close()


def _print_stats() -> None:
    session = get_session()
    try:
        total_events = session.query(DataFetchEvent).count()
        total_issues = session.query(DataFetchIssue).count()
        open_issues = (
            session.query(DataFetchIssue)
            .filter(DataFetchIssue.status.in_(("new", "active")))
            .count()
        )
        fixed_issues = (
            session.query(DataFetchIssue)
            .filter(DataFetchIssue.status == IssueStatus.FIXED)
            .count()
        )
        pending_tasks = (
            session.query(DataFixTask)
            .filter(DataFixTask.status.in_(("pending", "running")))
            .count()
        )

        print("Data Fetch Failure Statistics")
        print("=" * 40)
        print(f"  Total failure events: {total_events}")
        print(f"  Total issues:         {total_issues}")
        print(f"  Open (new/active):    {open_issues}")
        print(f"  Fixed:                {fixed_issues}")
        print(f"  Pending fix tasks:    {pending_tasks}")
    finally:
        session.close()


def _show_issue(issue_id: int) -> None:
    session = get_session()
    try:
        issue = (
            session.query(DataFetchIssue)
            .filter(DataFetchIssue.issue_id == issue_id)
            .first()
        )
        if issue is None:
            print(f"Issue {issue_id} not found.")
            return

        print(f"Issue #{issue.issue_id}: {issue.title}")
        print(f"  Status:       {issue.status.value}")
        print(f"  Provider:     {issue.provider}")
        print(f"  API:          {issue.api_name}")
        print(f"  Error type:   {issue.error_type.value}")
        print(f"  Occurrences:  {issue.occurrence_count}")
        print(f"  First seen:   {issue.first_seen_at}")
        print(f"  Last seen:    {issue.last_seen_at}")
        print(f"  Fingerprint:  {issue.fingerprint}")
        print(f"  Fix strategy: {issue.fix_strategy.value if issue.fix_strategy else 'N/A'}")

        if issue.sample_event_id:
            event = (
                session.query(DataFetchEvent)
                .filter(DataFetchEvent.event_id == issue.sample_event_id)
                .first()
            )
            if event:
                print(f"\n  Sample Event:")
                print(f"    Symbol:    {event.symbol or 'N/A'}")
                print(f"    Error:     {event.error_message or 'N/A'}")
                print(f"    Severity:  {event.severity.value}")

        tasks = (
            session.query(DataFixTask)
            .filter(DataFixTask.issue_id == issue_id)
            .all()
        )
        if tasks:
            print(f"\n  Fix Tasks ({len(tasks)}):")
            for t in tasks:
                print(f"    Task #{t.task_id}: {t.status.value}")
                if t.prompt_context:
                    print(f"\n{t.prompt_context}")
    finally:
        session.close()


def _get_mr_url_for_branch(branch: str) -> str | None:
    """Get MR creation URL for a branch."""
    try:
        from alphabee.data_fetch.fix_executor import _build_mr_url as _mr
        from alphabee.data_fetch.fix_executor import _gather_context as _gc
        # Hmm, this requires a task_id but we just want the URL
        # Use dummy context just for the URL
        import subprocess
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True,
        )
        remote = result.stdout.strip()
        if "github.com:" in remote:
            owner_repo = remote.split("github.com:")[1].replace(".git", "")
        elif "github.com/" in remote:
            owner_repo = remote.split("github.com/")[1].replace(".git", "")
        else:
            return None
        return f"https://github.com/{owner_repo}/compare/main...{branch}?expand=1"
    except Exception:
        return None


def main() -> None:
    init_db()
    args = sys.argv[1:]

    if not args:
        _print_stats()
        print()
        print("Open issues:")
        _print_issues(status_filter=("new", "active"))
        return

    cmd = args[0].lower()

    if cmd == "issues":
        _print_issues()

    elif cmd == "tasks":
        _print_tasks()

    elif cmd == "stats":
        _print_stats()

    elif cmd == "scan":
        from alphabee.data_fetch.scanner import scan_and_create_tasks

        max_tasks = int(args[1]) if len(args) > 1 else 10
        tasks = scan_and_create_tasks(max_tasks=max_tasks)
        if tasks:
            print(f"Created {len(tasks)} fix task(s):")
            for t in tasks:
                print(f"  Task #{t.task_id} → issue #{t.issue_id}: {t.patch_target}")
                print(f"  {t.prompt_context[:200]}...")
                print()
        else:
            print("No new fix tasks created (all open issues already have pending tasks).")

    elif cmd in ("auto-fix", "fix-all"):
        import asyncio

        from alphabee.data_fetch.fix_executor import prepare_and_run_fix
        from alphabee.data_fetch.scanner import get_open_tasks, scan_and_create_tasks

        max_tasks = int(args[1]) if len(args) > 1 else 10
        created_tasks = scan_and_create_tasks(max_tasks=max_tasks)
        tasks = list(get_open_tasks())
        if not tasks:
            print("No open fix tasks found.")
            return

        if created_tasks:
            print(f"Created {len(created_tasks)} new fix task(s).")
        print(f"Running {len(tasks)} open fix task(s) end-to-end...")
        for task in tasks:
            print(f"\nTask #{task.task_id} → issue #{task.issue_id}")
            result = asyncio.run(prepare_and_run_fix(task.task_id))
            if result.success:
                print(f"SUCCESS: {result.message}")
                if result.branch:
                    print(f"Branch: {result.branch}")
                if result.mr_url:
                    print(f"MR: {result.mr_url}")
            else:
                print(f"FAILED: {result.message}")

    elif cmd == "show":
        if len(args) < 2:
            print("Usage: show <issue_id>")
            return
        _show_issue(int(args[1]))

    elif cmd == "fix":
        if len(args) < 2:
            print("Usage: fix <issue_id> [--note <resolution>]")
            return
        from alphabee.data_fetch.scanner import mark_issue_fixed

        issue_id = int(args[1])
        note = ""
        if "--note" in args:
            idx = args.index("--note")
            if idx + 1 < len(args):
                note = args[idx + 1]
        mark_issue_fixed(issue_id, resolution_note=note)
        print(f"Issue #{issue_id} marked as fixed.")

    elif cmd in ("run-fix", "fix-task"):
        if len(args) < 2:
            print("Usage: run-fix <task_id>")
            print("  Invokes Claude Agent SDK, then verifies and submits the fix.")
            return
        import asyncio
        from alphabee.data_fetch.fix_executor import prepare_and_run_fix

        task_id = int(args[1])
        result = asyncio.run(prepare_and_run_fix(task_id))
        if result.success:
            print(f"SUCCESS: {result.message}")
            if result.branch:
                print(f"Branch: {result.branch}")
            if result.mr_url:
                print(f"MR: {result.mr_url}")
        else:
            print(f"Error: {result.message}")

    elif cmd == "verify":
        if len(args) < 2:
            print("Usage: verify <task_id>")
            print("  Runs tests, commits, pushes, and creates MR. Must be on the fix branch.")
            return
        from alphabee.data_fetch.fix_executor import verify_and_submit

        task_id = int(args[1])
        result = verify_and_submit(task_id)
        if result.success:
            print(f"SUCCESS: {result.message}")
            if result.mr_url:
                print(f"Create MR: {result.mr_url}")
        else:
            print(f"FAILED: {result.message}")

    elif cmd in ("prompt", "show-prompt"):
        if len(args) < 2:
            print("Usage: prompt <task_id>")
            return
        from alphabee.data_fetch.fix_executor import build_agent_prompt

        task_id = int(args[1])
        print(build_agent_prompt(task_id))

    else:
        print(f"Unknown command: {cmd}")
        print("Available: issues | tasks | stats | scan | auto-fix | show <id> | fix <id> | run-fix <task_id> | verify <task_id> | prompt <task_id>")


if __name__ == "__main__":
    main()
