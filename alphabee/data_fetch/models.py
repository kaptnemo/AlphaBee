"""SQLAlchemy ORM models for data fetch failure tracking.

Three tables:
- ``data_fetch_events``  — raw failure events (append-only)
- ``data_fetch_issues``   — deduplicated, aggregated issue tickets
- ``data_fix_tasks``      — actionable fix tasks for agents
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ── enumerations ───────────────────────────────────────────────────────


class ErrorSeverity(enum.StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ErrorType(enum.StrEnum):
    PERMISSION = "permission"
    MISSING_FIELD = "missing_field"
    TIMEOUT = "timeout"
    PARSE_ERROR = "parse_error"
    NETWORK = "network"
    RATE_LIMIT = "rate_limit"
    EMPTY_RESPONSE = "empty_response"
    UNKNOWN = "unknown"


class IssueStatus(enum.StrEnum):
    NEW = "new"
    ACTIVE = "active"
    INVESTIGATING = "investigating"
    FIXED = "fixed"
    WONT_FIX = "wont_fix"
    IGNORED = "ignored"


class TaskStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class VerificationStatus(enum.StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class FixStrategy(enum.StrEnum):
    SWITCH_SOURCE = "switch_source"
    ADD_FIELD = "add_field"
    FIX_INTERFACE = "fix_interface"
    FIX_CRAWLER = "fix_crawler"
    FALLBACK = "fallback"


# ── ORM models ─────────────────────────────────────────────────────────


class DataFetchEvent(Base):
    __tablename__ = "data_fetch_events"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    api_name: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(32))
    error_type: Mapped[ErrorType] = mapped_column(Enum(ErrorType), nullable=False, index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    missing_fields: Mapped[list[str] | None] = mapped_column(JSON)
    request_payload: Mapped[dict | None] = mapped_column(JSON)
    response_snippet: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[ErrorSeverity] = mapped_column(Enum(ErrorSeverity), nullable=False, default=ErrorSeverity.MEDIUM)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    session_id: Mapped[str | None] = mapped_column(String(64))
    task_id: Mapped[str | None] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    issue: Mapped[DataFetchIssue | None] = relationship(
        "DataFetchIssue",
        back_populates="sample_event",
        foreign_keys="DataFetchIssue.sample_event_id",
    )

    def __repr__(self) -> str:
        return (
            f"<DataFetchEvent id={self.event_id} "
            f"provider={self.provider} api={self.api_name} "
            f"error={self.error_type.value}>"
        )


class DataFetchIssue(Base):
    __tablename__ = "data_fetch_issues"

    issue_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fingerprint: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[IssueStatus] = mapped_column(Enum(IssueStatus), nullable=False, default=IssueStatus.NEW, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    api_name: Mapped[str] = mapped_column(String(128), nullable=False)
    error_type: Mapped[ErrorType] = mapped_column(Enum(ErrorType), nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    sample_event_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("data_fetch_events.event_id"))
    owner_agent: Mapped[str | None] = mapped_column(String(64))
    fix_strategy: Mapped[FixStrategy | None] = mapped_column(Enum(FixStrategy))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    verification_status: Mapped[VerificationStatus] = mapped_column(
        Enum(VerificationStatus), nullable=False, default=VerificationStatus.PENDING
    )

    sample_event: Mapped[DataFetchEvent | None] = relationship(
        "DataFetchEvent",
        back_populates="issue",
        foreign_keys=[sample_event_id],
    )
    fix_tasks: Mapped[list[DataFixTask]] = relationship("DataFixTask", back_populates="issue")

    def __repr__(self) -> str:
        return (
            f"<DataFetchIssue id={self.issue_id} "
            f"'{self.title}' count={self.occurrence_count} "
            f"status={self.status.value}>"
        )


class DataFixTask(Base):
    __tablename__ = "data_fix_tasks"

    task_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    issue_id: Mapped[int] = mapped_column(Integer, ForeignKey("data_fetch_issues.issue_id"), nullable=False, index=True)
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus), nullable=False, default=TaskStatus.PENDING)
    prompt_context: Mapped[str | None] = mapped_column(Text)
    patch_target: Mapped[str | None] = mapped_column(String(256))
    result_summary: Mapped[str | None] = mapped_column(Text)
    verification_result: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)

    issue: Mapped[DataFetchIssue] = relationship("DataFetchIssue", back_populates="fix_tasks")

    def __repr__(self) -> str:
        return f"<DataFixTask id={self.task_id} issue={self.issue_id} status={self.status.value}>"
