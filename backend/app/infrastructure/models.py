"""ORM models.

Tables arrive in the phase that first writes to them; the full v0.3 schema is
not created up front.

Phase 0 — ``system_event``: append-only lifecycle log, the seed of the
``audit.event`` log in the architecture.

Phase 1 — market data: ``instrument``, ``market_tick``, ``candle``,
``connection_event``, ``data_gap``, ``data_quality_event``. Together these
answer the question the architecture requires of this phase: *what market data
did we receive for instrument X during window Y, and was any of it suspect?*

Prices are ``Numeric``, never float. Every timestamp column is timezone-aware
and stores UTC; conversion to IST happens only in session logic and the UI.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Prices: 20 digits, 6 decimals. Currency quotes carry four decimals and equity
# two; six leaves headroom without inviting float-style rounding surprises.
PRICE = Numeric(20, 6)

# SQLite only autoincrements a column declared exactly INTEGER PRIMARY KEY, so a
# BIGINT surrogate key silently fails to generate ids there. The variant keeps
# 64-bit keys on PostgreSQL while staying insertable on SQLite.
BIGINT_PK = BigInteger().with_variant(Integer, "sqlite")


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


# --------------------------------------------------------------------------- #
# Phase 1 - market data
# --------------------------------------------------------------------------- #


class InstrumentRecord(Base):
    """The broker's instrument master, refreshed daily.

    ``instrument_token`` is the streaming subscription key but is **not** stable
    identity - exchanges reuse tokens for derivatives after expiry. The stable
    key, and the unique constraint, is ``(exchange, tradingsymbol)``.

    ``retrieved_at`` is what makes staleness detectable: the dump is generated
    once a day, and acting on yesterday's lot sizes is a real hazard.
    """

    __tablename__ = "instrument"
    __table_args__ = (
        UniqueConstraint("exchange", "tradingsymbol", name="uq_instrument_symbol"),
        Index("ix_instrument_token", "instrument_token"),
        Index("ix_instrument_segment", "segment"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instrument_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    exchange_token: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tradingsymbol: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    segment: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    instrument_type: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    tick_size: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=Decimal("0"))
    lot_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    strike: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    last_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    is_index: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class MarketTickRecord(Base):
    """A normalized tick.

    Stored to answer "what did we receive for instrument X during window Y" and
    to make replay possible. Retention is a policy decision, not an accident:
    ticks are pruned by age, while completed candles are kept.
    """

    __tablename__ = "market_tick"
    __table_args__ = (
        Index("ix_market_tick_token_time", "instrument_token", "event_time"),
        Index("ix_market_tick_received", "received_at"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    instrument_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tradingsymbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exchange: Mapped[str | None] = mapped_column(String(16), nullable=True)
    mode: Mapped[str] = mapped_column(String(8), nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exchange_timestamp: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_price: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    last_quantity: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    average_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    total_buy_quantity: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    total_sell_quantity: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    open_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    high_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    low_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    close_price: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    open_interest: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    depth: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    is_index: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")


class CandleRecord(Base):
    """An OHLCV bar.

    Only COMPLETED bars are persisted. An IN_PROGRESS bar is a live view that
    still changes; writing it would let a later reader mistake it for settled
    history, which is exactly the repainting the architecture forbids.

    The unique constraint on ``(instrument_token, interval, start_at)`` makes
    re-ingestion idempotent.
    """

    __tablename__ = "candle"
    __table_args__ = (
        UniqueConstraint("instrument_token", "interval", "start_at", name="uq_candle_bucket"),
        Index("ix_candle_token_interval_start", "instrument_token", "interval", "start_at"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    instrument_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tradingsymbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exchange: Mapped[str | None] = mapped_column(String(16), nullable=True)
    interval: Mapped[str] = mapped_column(String(8), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    high: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    low: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    close: Mapped[Decimal] = mapped_column(PRICE, nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tick_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    last_update_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ConnectionEventRecord(Base):
    """Provider lifecycle events: auth, connect, subscribe, disconnect."""

    __tablename__ = "connection_event"
    __table_args__ = (Index("ix_connection_event_time", "occurred_at"),)

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class DataGapRecord(Base):
    """A period during which the stream was not delivering.

    Recorded explicitly so nothing later mistakes a reconnect for continuous
    data. A bar overlapping a gap is suspect by construction.
    """

    __tablename__ = "data_gap"
    __table_args__ = (Index("ix_data_gap_window", "started_at", "ended_at"),)

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_seconds: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    instrument_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class DataQualityEventRecord(Base):
    """A rejected or suspicious tick. Nothing is discarded silently."""

    __tablename__ = "data_quality_event"
    __table_args__ = (
        Index("ix_data_quality_time", "occurred_at"),
        Index("ix_data_quality_issue_token", "issue", "instrument_token"),
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    issue: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    instrument_token: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
