"""Failure event recorder — the primary API for capturing data fetch failures.

Usage::

    from alphabee.data_fetch import record_failure

    try:
        result = fetch_some_data(symbol)
    except Exception as exc:
        record_failure(
            provider="tushare",
            api_name="income",
            symbol="600519.SH",
            error_type="timeout",
            error_message=str(exc),
            severity="medium",
        )
"""

from __future__ import annotations

import logging
from datetime import datetime

from alphabee.data_fetch.database import get_session, init_db
from alphabee.data_fetch.fingerprint import compute_fingerprint
from alphabee.data_fetch.models import (
    DataFetchEvent,
    DataFetchIssue,
    ErrorSeverity,
    ErrorType,
    IssueStatus,
)

logger = logging.getLogger(__name__)

_init_done = False


def _ensure_init() -> None:
    global _init_done
    if not _init_done:
        init_db()
        _init_done = True


def record_failure(
    provider: str,
    api_name: str,
    error_type: str = "unknown",
    error_message: str | None = None,
    symbol: str | None = None,
    severity: str = "medium",
    missing_fields: list[str] | None = None,
    request_payload: dict | None = None,
    response_snippet: str | None = None,
    trace_id: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
) -> tuple[DataFetchEvent, DataFetchIssue]:
    """Record a data fetch failure and upsert the aggregated issue.

    Returns the created/updated ``(event, issue)`` pair.

    Args:
        provider: Data source name (``tushare``, ``akshare``, ``eastmoney``, …).
        api_name: Specific API or function name (e.g. ``income``, ``daily``).
        error_type: One of ``permission``, ``missing_field``, ``timeout``,
            ``parse_error``, ``network``, ``rate_limit``, ``empty_response``,
            ``unknown``.
        error_message: Original exception text.
        symbol: Stock code involved (if applicable).
        severity: ``low`` / ``medium`` / ``high``.
        missing_fields: Field names that were absent from the response.
        request_payload: Serialised request parameters (for diagnosis).
        response_snippet: Truncated response body.
        trace_id: Langfuse trace identifier.
        session_id: User / agent session identifier.
        task_id: Task identifier.
    """
    _ensure_init()

    # ── normalise enums ────────────────────────────────────────────────
    try:
        et = ErrorType(error_type)
    except ValueError:
        et = ErrorType.UNKNOWN

    try:
        sev = ErrorSeverity(severity)
    except ValueError:
        sev = ErrorSeverity.MEDIUM

    # ── fingerprint ────────────────────────────────────────────────────
    fp = compute_fingerprint(
        provider=provider.lower(),
        api_name=api_name or "unknown",
        error_type=et.value,
        missing_fields=missing_fields,
        error_prefix=error_message,
    )

    session = get_session()
    try:
        # ── create event ───────────────────────────────────────────────
        event = DataFetchEvent(
            occurred_at=datetime.now(),
            provider=provider.lower(),
            api_name=api_name,
            symbol=symbol,
            error_type=et,
            error_message=error_message,
            missing_fields=missing_fields,
            request_payload=request_payload,
            response_snippet=response_snippet,
            severity=sev,
            trace_id=trace_id,
            session_id=session_id,
            task_id=task_id,
            fingerprint=fp,
        )
        session.add(event)
        session.flush()

        # ── upsert issue ───────────────────────────────────────────────
        issue = session.query(DataFetchIssue).filter(DataFetchIssue.fingerprint == fp).first()
        if issue is None:
            title = _build_title(provider, api_name, et.value, missing_fields)
            issue = DataFetchIssue(
                fingerprint=fp,
                title=title,
                status=IssueStatus.NEW,
                provider=provider.lower(),
                api_name=api_name,
                error_type=et,
                occurrence_count=1,
                first_seen_at=event.occurred_at,
                last_seen_at=event.occurred_at,
                sample_event_id=event.event_id,
            )
            session.add(issue)
        else:
            issue.occurrence_count += 1
            issue.last_seen_at = event.occurred_at
            if issue.status == IssueStatus.FIXED:
                issue.status = IssueStatus.NEW
            issue.sample_event_id = event.event_id

        session.flush()
        session.commit()
        return event, issue

    except Exception:
        session.rollback()
        logger.exception("Failed to record data fetch failure")
        raise


def _build_title(
    provider: str,
    api_name: str,
    error_type: str,
    missing_fields: list[str] | None = None,
) -> str:
    """Generate a human-readable title for a new issue."""
    source = f"{provider}/{api_name}"
    if error_type == "permission":
        return f"[{source}] 权限不足"
    if error_type == "missing_field":
        fields = ", ".join(missing_fields or ["unknown"])
        return f"[{source}] 缺少字段: {fields}"
    if error_type == "timeout":
        return f"[{source}] 接口超时"
    if error_type == "parse_error":
        return f"[{source}] 数据解析失败"
    if error_type == "network":
        return f"[{source}] 网络错误"
    if error_type == "rate_limit":
        return f"[{source}] 触发限流"
    if error_type == "empty_response":
        return f"[{source}] 返回空数据"
    return f"[{source}] 未知错误"
