"""Data fetch failure recording and auto-fix management.

Provides:
- ``record_failure``: capture a data fetch failure event and upsert an aggregated issue
- ``init_db``: initialise the SQLite database and create tables
- Models: ``DataFetchEvent``, ``DataFetchIssue``, ``DataFixTask`` (SQLAlchemy ORM)
"""

from alphabee.data_fetch.database import get_session, init_db
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

__all__ = [
    "record_failure",
    "init_db",
    "get_session",
    "DataFetchEvent",
    "DataFetchIssue",
    "DataFixTask",
    "ErrorSeverity",
    "ErrorType",
    "IssueStatus",
    "TaskStatus",
]
