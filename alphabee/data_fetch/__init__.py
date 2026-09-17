"""Data fetch failure recording and auto-fix management.

Provides:
- ``record_failure``: capture a data fetch failure event and upsert an aggregated issue
- ``init_db``: initialise the SQLite database and create tables
- ``scan_and_create_tasks``: scan open issues and generate fix tasks
- ``record_event`` / ``mark_resolved`` / ``list_events`` / ``summarize_symbol`` / ``purge_before``:
  偏离账本读写（deviation-control framework §5.2 / §14.1-C，F0b）
- Models: ``DataFetchEvent``, ``DataFetchIssue``, ``DataFixTask``, ``DeviationEvent`` (SQLAlchemy ORM)
"""

from alphabee.data_fetch.database import get_session, init_db, reset_db
from alphabee.data_fetch.deviation_store import (
    list_events,
    mark_resolved,
    purge_before,
    record_event,
    summarize_symbol,
)
from alphabee.data_fetch.fix_executor import build_agent_prompt, prepare_fix, verify_and_submit
from alphabee.data_fetch.models import (
    DataFetchEvent,
    DataFetchIssue,
    DataFixTask,
    DeviationEvent,
    ErrorSeverity,
    ErrorType,
    IssueStatus,
    TaskStatus,
)
from alphabee.data_fetch.recorder import record_failure
from alphabee.data_fetch.scanner import (
    get_open_tasks,
    mark_issue_fixed,
    mark_task,
    scan_and_create_tasks,
)

__all__ = [
    "record_failure",
    "record_event",
    "mark_resolved",
    "list_events",
    "summarize_symbol",
    "purge_before",
    "init_db",
    "reset_db",
    "get_session",
    "scan_and_create_tasks",
    "get_open_tasks",
    "mark_task",
    "mark_issue_fixed",
    "build_agent_prompt",
    "prepare_fix",
    "verify_and_submit",
    "DataFetchEvent",
    "DataFetchIssue",
    "DataFixTask",
    "DeviationEvent",
    "ErrorSeverity",
    "ErrorType",
    "IssueStatus",
    "TaskStatus",
]
