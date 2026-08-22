"""ORM models.

Phase 0 defines exactly one table.  The full v0.3 schema (market, strategy,
riskdec, execution, portfolio, perf, ops, audit) is NOT created here — tables
arrive in the phase that first writes to them.

``system_event`` is the seed of the ``audit.event`` log described in the
architecture: append-only, one row per notable lifecycle event.  The hash chain
and actor/entity columns are added in the phase that introduces order and mode
transitions; this table already carries the shape they extend.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SystemEvent(Base):
    """Append-only record of application lifecycle events.

    Extension points for later phases (each requires an Alembic migration):
      * ``actor_type`` / ``actor_id``  — who caused the event
      * ``entity_type`` / ``entity_id`` — what it concerned
      * ``prev_hash`` / ``hash``        — tamper-evident chaining
    """

    __tablename__ = "system_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    app_env: Mapped[str] = mapped_column(String(32), nullable=False)
    trading_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    app_version: Mapped[str] = mapped_column(String(32), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<SystemEvent id={self.id} type={self.event_type!r} "
            f"mode={self.trading_mode!r} at={self.occurred_at!r}>"
        )
