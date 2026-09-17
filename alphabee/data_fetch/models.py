"""SQLAlchemy ORM models for data fetch failure tracking.

Five tables:
- ``data_fetch_events``  — raw failure events (append-only)
- ``data_fetch_issues``   — deduplicated, aggregated issue tickets
- ``data_fix_tasks``      — actionable fix tasks for agents
- ``deviation_events``    — 偏离账本（deviation-control framework §5.2）
- ``deviation_metrics``   — 每 run 偏离指标（deviation-control framework §11.1，F5）
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
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
    request_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
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


class DeviationEvent(Base):
    """偏离账本行（deviation-control framework §5.2 / §14.1-C）。

    与 ``DataFetchEvent`` 共享同一 ``Base`` / ``init_db()`` / ``DATA_FETCH_DB_PATH``，
    独立表名 ``deviation_events``：新增表只 ``create_all``，不改既有表结构，
    回滚只需 drop 本表。

    身份是 ``fingerprint``（§5.2 指纹去重）：同一偏离（分类 + 类目 + 归一化 message +
    symbol + 检测节点）在后续 run 复发时**不新增行**，而是 ``occurrence_count += 1``
    并刷新 ``last_seen_at``，从而支持"复发率"统计。
    """

    __tablename__ = "deviation_events"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    symbol: Mapped[str | None] = mapped_column(String(32), index=True)
    step_id: Mapped[str | None] = mapped_column(String(64))  # 产生地（related_step）
    detected_at_step: Mapped[str | None] = mapped_column(String(64))  # §6 检测器所在节点
    deviation_class: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    message: Mapped[str | None] = mapped_column(Text)
    recovery_action: Mapped[str | None] = mapped_column(String(64))
    recovery_cost: Mapped[int | None] = mapped_column(Integer)
    amplified_by: Mapped[list[str] | None] = mapped_column(JSON)
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return (
            f"<DeviationEvent id={self.event_id} class={self.deviation_class} "
            f"category={self.category} count={self.occurrence_count} "
            f"resolved={self.resolved}>"
        )


class DeviationMetric(Base):
    """每 run 偏离指标行（deviation-control framework §11.1 / §14.5-B，F5）。

    与 ``DeviationEvent`` 共享同一 ``Base`` / ``init_db()`` / ``DATA_FETCH_DB_PATH``，
    独立表名 ``deviation_metrics``：**只新增表**，既有表结构一字不动（回滚 = drop 本表，
    或把 ``deviation.ledger.enabled`` 关掉 ⇒ 零写入）。

    身份是 ``run_id``（唯一）：run 尾部的度量 sink 重放时按 ``run_id`` **upsert**，
    不会为同一 run 堆出多行（与账本的指纹去重同一思路，但度量是"每 run 一行"而非累计计数）。

    八列数值口径见 ``alphabee/orchestrator/services/telemetry.py`` 的模块 docstring；
    ``None`` = 分母缺失（§14.5-B：缺失分母不得静默回退 0）。
    """

    __tablename__ = "deviation_metrics"

    metric_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    detection_rate: Mapped[float | None] = mapped_column(Float)
    mean_detection_latency: Mapped[float | None] = mapped_column(Float)
    recovery_rate: Mapped[float | None] = mapped_column(Float)
    recovery_half_life: Mapped[float | None] = mapped_column(Float)
    amplification_overturn_rate: Mapped[float | None] = mapped_column(Float)
    on_track_curve: Mapped[list[float] | None] = mapped_column(JSON)
    budget_consumption: Mapped[float | None] = mapped_column(Float)
    silent_degradation_rate: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)

    def __repr__(self) -> str:
        return (
            f"<DeviationMetric run={self.run_id} detection_rate={self.detection_rate} "
            f"recovery_rate={self.recovery_rate}>"
        )
