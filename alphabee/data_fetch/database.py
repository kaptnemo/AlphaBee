"""SQLite database connection and session management."""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

_DEFAULT_DB_PATH = Path(__file__).parent.parent.parent / "data" / "fetch_events.db"


def _db_url() -> str:
    path = os.environ.get("DATA_FETCH_DB_PATH", str(_DEFAULT_DB_PATH))
    return f"sqlite:///{path}"


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(_db_url(), echo=False)
    return _engine


def get_session() -> Session:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(), expire_on_commit=False
        )
    return _session_factory()


def init_db() -> None:
    """Create all tables if they do not exist."""
    from alphabee.data_fetch.models import Base

    Base.metadata.create_all(get_engine())


def reset_db() -> None:
    """Reset module-level state — useful for testing."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
