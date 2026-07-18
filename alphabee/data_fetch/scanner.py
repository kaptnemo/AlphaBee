"""Scanner — scans open ``DataFetchIssue`` records and creates actionable ``DataFixTask``s.

Usage::

    from alphabee.data_fetch.scanner import scan_and_create_tasks
    tasks = scan_and_create_tasks(max_tasks=10)
    for task in tasks:
        print(task.prompt_context)
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime

from alphabee.data_fetch.database import get_session, init_db
from alphabee.data_fetch.models import (
    DataFetchEvent,
    DataFetchIssue,
    DataFixTask,
    IssueStatus,
    TaskStatus,
    VerificationStatus,
)
from alphabee.data_fetch.strategies import recommend_fix

# issues with these statuses are eligible for fix task creation
_SCANNABLE_STATUSES = (IssueStatus.NEW, IssueStatus.ACTIVE)


def scan_and_create_tasks(max_tasks: int = 10) -> Sequence[DataFixTask]:
    """Scan for open issues and create fix tasks for them.

    Issues that already have a pending/running fix task are skipped.

    Args:
        max_tasks: Maximum number of new tasks to create per scan.

    Returns:
        Created ``DataFixTask`` instances.
    """
    init_db()
    session = get_session()
    new_tasks: list[DataFixTask] = []

    try:
        open_issues = (
            session.query(DataFetchIssue)
            .filter(DataFetchIssue.status.in_(_SCANNABLE_STATUSES))
            .order_by(DataFetchIssue.occurrence_count.desc())
            .limit(max_tasks * 2)
            .all()
        )

        for issue in open_issues:
            if len(new_tasks) >= max_tasks:
                break

            # Skip if already has a pending/running task
            existing = (
                session.query(DataFixTask)
                .filter(
                    DataFixTask.issue_id == issue.issue_id,
                    DataFixTask.status.in_((TaskStatus.PENDING, TaskStatus.RUNNING)),
                )
                .first()
            )
            if existing is not None:
                continue

            # Build rich context for the fix agent
            sample_event = (
                (session.query(DataFetchEvent).filter(DataFetchEvent.event_id == issue.sample_event_id).first())
                if issue.sample_event_id
                else None
            )

            plan = recommend_fix(
                provider=issue.provider,
                api_name=issue.api_name,
                error_type=issue.error_type.value,
                error_message=(sample_event.error_message if sample_event else None),
            )

            prompt_context = _build_prompt_context(issue, sample_event, plan)

            task = DataFixTask(
                issue_id=issue.issue_id,
                status=TaskStatus.PENDING,
                prompt_context=prompt_context,
                patch_target=", ".join(plan.relevant_paths[:5]),
                created_at=datetime.now(),
                updated_at=datetime.now(),
            )
            session.add(task)
            new_tasks.append(task)

            issue.status = IssueStatus.ACTIVE
            issue.fix_strategy = plan.strategy

        session.commit()

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    return new_tasks


def get_open_tasks() -> Sequence[DataFixTask]:
    """Return all pending or running fix tasks."""
    init_db()
    session = get_session()
    try:
        return (
            session.query(DataFixTask)
            .filter(DataFixTask.status.in_((TaskStatus.PENDING, TaskStatus.RUNNING)))
            .order_by(DataFixTask.created_at.desc())
            .all()
        )
    finally:
        session.close()


def mark_task(
    task_id: int,
    status: str,
    result_summary: str = "",
    verification_result: str | None = None,
) -> None:
    """Update a fix task's status and result."""
    init_db()
    session = get_session()
    try:
        task = session.query(DataFixTask).filter(DataFixTask.task_id == task_id).first()
        if task is None:
            return
        try:
            task.status = TaskStatus(status)
        except ValueError:
            task.status = TaskStatus.FAILED
        task.result_summary = result_summary
        if verification_result is not None:
            task.verification_result = verification_result
        task.updated_at = datetime.now()
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def mark_issue_fixed(issue_id: int, resolution_note: str = "", verification_status: str = "passed") -> None:
    """Mark an issue as fixed with resolution notes."""
    init_db()
    session = get_session()
    try:
        issue = session.query(DataFetchIssue).filter(DataFetchIssue.issue_id == issue_id).first()
        if issue is None:
            return
        issue.status = IssueStatus.FIXED
        issue.resolution_note = resolution_note
        try:
            issue.verification_status = VerificationStatus(verification_status)
        except ValueError:
            issue.verification_status = VerificationStatus.PASSED

        # Also close pending tasks for this issue
        session.query(DataFixTask).filter(
            DataFixTask.issue_id == issue_id,
            DataFixTask.status.in_((TaskStatus.PENDING, TaskStatus.RUNNING)),
        ).update(
            {DataFixTask.status: TaskStatus.DONE},
            synchronize_session="fetch",
        )

        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ── prompt context builder ─────────────────────────────────────────────


def _build_prompt_context(
    issue: DataFetchIssue,
    sample_event: DataFetchEvent | None,
    plan,
) -> str:
    """Build a structured prompt context for the fix agent."""

    event_lines: list[str] = []
    if sample_event:
        event_lines.append(f"- Symbol: {sample_event.symbol or 'N/A'}")
        event_lines.append(f"- Error: {sample_event.error_message or 'N/A'}")
        if sample_event.request_payload:
            event_lines.append(f"- Request: {json.dumps(sample_event.request_payload, ensure_ascii=False)}")
        if sample_event.missing_fields:
            event_lines.append(f"- Missing fields: {', '.join(sample_event.missing_fields)}")

    parts = [
        f"## Issue: {issue.title}",
        f"Provider: {issue.provider}",
        f"API: {issue.api_name}",
        f"Error type: {issue.error_type.value}",
        f"Occurrence count: {issue.occurrence_count}",
        f"First seen: {issue.first_seen_at.isoformat() if issue.first_seen_at else 'N/A'}",
        f"Last seen: {issue.last_seen_at.isoformat() if issue.last_seen_at else 'N/A'}",
        f"Fingerprint: {issue.fingerprint}",
        "",
        "## Sample Event",
        *event_lines,
        "",
        "## Relevant Code",
        *[f"- {p}" for p in plan.relevant_paths],
        "",
        "## Recommended Fix Strategy",
        f"Strategy: {plan.strategy.value}",
        "",
        "## Recommended Actions",
        *[f"{i + 1}. {a}" for i, a in enumerate(plan.recommended_actions)],
        "",
        "## Agent Instructions",
        plan.agent_instruction,
    ]
    return "\n".join(parts)
