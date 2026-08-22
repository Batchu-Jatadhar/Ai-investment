"""Database engine, session management and a real connectivity probe.

Engine-agnostic on purpose: PostgreSQL is the production and CI target, while
local development without Docker can point ``DATABASE_URL`` at SQLite.  Later
phases that need PostgreSQL-specific types must add them in a migration and
mark the corresponding tests ``postgres_only`` rather than silently diverging.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _engine_kwargs(settings: Settings) -> dict[str, Any]:
    if settings.is_sqlite:
        # SQLite is used for local/dev test runs; the API runs requests on a
        # thread pool, hence check_same_thread=False.
        return {"connect_args": {"check_same_thread": False}}
    return {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_pool_max_overflow,
        "pool_pre_ping": True,
    }


def get_engine(settings: Settings | None = None) -> Engine:
    global _engine, _session_factory
    if _engine is None:
        settings = settings or get_settings()
        _engine = create_engine(
            settings.database_url,
            echo=settings.db_echo,
            future=True,
            **_engine_kwargs(settings),
        )
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


def reset_engine() -> None:
    """Dispose the engine and clear cached state. Used by tests."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on failure."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a read session."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


@dataclass(frozen=True)
class DatabaseHealth:
    ok: bool
    backend: str
    latency_ms: float | None = None
    migration_revision: str | None = None
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


def check_database(settings: Settings | None = None) -> DatabaseHealth:
    """Actually exercise the database. No hardcoded 'healthy'.

    Runs ``SELECT 1`` and, when the Alembic bookkeeping table exists, reports
    the applied migration revision so a half-migrated database is visible.
    """
    settings = settings or get_settings()
    backend = settings.database_url.split("://", 1)[0]
    started = time.perf_counter()
    try:
        engine = get_engine(settings)
        with engine.connect() as conn:
            value = conn.execute(text("SELECT 1")).scalar_one()
            if value != 1:
                raise SQLAlchemyError(f"SELECT 1 returned {value!r}")
            revision: str | None = None
            if inspect(conn).has_table("alembic_version"):
                revision = conn.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one_or_none()
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        return DatabaseHealth(
            ok=True,
            backend=backend,
            latency_ms=latency_ms,
            migration_revision=revision,
            details={"migrated": revision is not None},
        )
    except Exception as exc:  # noqa: BLE001 - health probe reports every failure
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        logger.warning(
            "database_health_check_failed",
            extra={"error_type": type(exc).__name__, "backend": backend},
        )
        return DatabaseHealth(
            ok=False,
            backend=backend,
            latency_ms=latency_ms,
            error=f"{type(exc).__name__}: {exc}",
        )
