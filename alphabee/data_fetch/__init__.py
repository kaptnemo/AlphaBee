"""Data fetch failure recording and auto-fix management.

Provides:
- ``record_failure``: capture a data fetch failure event and upsert an aggregated issue
- ``init_db``: initialise the SQLite database and create tables
- ``scan_and_create_tasks``: scan open issues and generate fix tasks
- Models: ``DataFetchEvent``, ``DataFetchIssue``, ``DataFixTask`` (SQLAlchemy ORM)
"""

from alphabee.data_fetch.database import get_session, init_db, reset_db
from alphabee.data_fetch.models import (
    DataFetchEvent,
    DataFetchIssue,
    DataFixTask,
    ErrorSeverity,
    ErrorType,
    IssueStatus,
    TaskStatus,
)
from alphabee.data_fetch.recorder import record_failure
from alphabee.data_fetch.fix_executor import build_agent_prompt, prepare_fix, prepare_and_run_fix, verify_and_submit
from alphabee.data_fetch.scanner import (
    get_open_tasks,
    mark_issue_fixed,
    mark_task,
    scan_and_create_tasks,
)

__all__ = [
    "record_failure",
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
    "ErrorSeverity",
    "ErrorType",
    "IssueStatus",
    "TaskStatus",
]
