"""SQL implementation of :class:`~app.domain.market.ports.MarketDataRepository`.

This is the seam the architecture requires: downstream modules ask the
repository, and whether the answer came from the live stream, the database or a
replay source is not their concern.

SQLite stores datetimes without an offset, so every timestamp read back is
re-stamped as UTC. That is safe because the write path only ever stores UTC -
see ``app/core/time.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.logging import get_logger
from app.domain.market.models import (
    Candle,
    CandleInterval,
    CandleStatus,
    DepthLevel,
    Instrument,
    MarketDepth,
    MarketTick,
    TickMode,
)
from app.domain.market.ports import ConnectionEvent, DataGap
from app.domain.market.quality import DataQualityEvent
from app.infrastructure.models import (
    CandleRecord,
    ConnectionEventRecord,
    DataGapRecord,
    DataQualityEventRecord,
    InstrumentRecord,
    MarketTickRecord,
)

logger = get_logger(__name__)

__all__ = ["SqlMarketDataRepository"]

SessionFactory = Callable[[], Session] | sessionmaker


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _depth_to_json(depth: MarketDepth | None) -> dict[str, Any] | None:
    if depth is None:
        return None
    return {
        "bids": [
            {"price": str(level.price), "quantity": level.quantity, "orders": level.orders}
            for level in depth.bids
        ],
        "asks": [
            {"price": str(level.price), "quantity": level.quantity, "orders": level.orders}
            for level in depth.asks
        ],
    }


def _depth_from_json(payload: dict[str, Any] | None) -> MarketDepth | None:
    if not payload:
        return None

    def levels(key: str) -> tuple[DepthLevel, ...]:
        return tuple(
            DepthLevel(
                price=Decimal(str(item["price"])),
                quantity=int(item["quantity"]),
                orders=int(item["orders"]),
            )
            for item in payload.get(key, [])
        )

    return MarketDepth(bids=levels("bids"), asks=levels("asks"))


class SqlMarketDataRepository:
    """Concrete repository over the Phase 1 market-data tables."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    @contextmanager
    def _write(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @contextmanager
    def _read(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # instruments
    # ------------------------------------------------------------------ #

    def replace_instruments(self, instruments: Sequence[Instrument], retrieved_at: datetime) -> int:
        """Replace the instrument master wholesale.

        The broker publishes a complete dump daily; merging would leave expired
        contracts behind forever, so the table is rebuilt in one transaction.
        """
        with self._write() as session:
            session.execute(delete(InstrumentRecord))
            session.add_all(
                [
                    InstrumentRecord(
                        instrument_token=i.instrument_token,
                        exchange_token=i.exchange_token,
                        tradingsymbol=i.tradingsymbol,
                        name=i.name,
                        exchange=i.exchange,
                        segment=i.segment,
                        instrument_type=i.instrument_type,
                        tick_size=i.tick_size,
                        lot_size=i.lot_size,
                        expiry=i.expiry,
                        strike=i.strike,
                        last_price=i.last_price,
                        is_index=i.is_index,
                        source=i.source,
                        retrieved_at=i.retrieved_at or retrieved_at,
                    )
                    for i in instruments
                ]
            )
        logger.info("instrument_master_replaced", extra={"count": len(instruments)})
        return len(instruments)

    def get_instrument_by_token(self, instrument_token: int) -> Instrument | None:
        with self._read() as session:
            row = session.execute(
                select(InstrumentRecord).where(
                    InstrumentRecord.instrument_token == instrument_token
                )
            ).scalar_one_or_none()
            return self._to_instrument(row) if row else None

    def get_instrument_by_symbol(self, exchange: str, tradingsymbol: str) -> Instrument | None:
        with self._read() as session:
            row = session.execute(
                select(InstrumentRecord).where(
                    InstrumentRecord.exchange == exchange.upper(),
                    InstrumentRecord.tradingsymbol == tradingsymbol.upper(),
                )
            ).scalar_one_or_none()
            return self._to_instrument(row) if row else None

    def all_instruments(self, limit: int = 100_000) -> list[Instrument]:
        with self._read() as session:
            rows = session.execute(select(InstrumentRecord).limit(limit)).scalars().all()
            return [self._to_instrument(row) for row in rows]

    def instrument_count(self) -> int:
        with self._read() as session:
            return len(session.execute(select(InstrumentRecord.id)).scalars().all())

    def instruments_retrieved_at(self) -> datetime | None:
        with self._read() as session:
            row = session.execute(
                select(InstrumentRecord.retrieved_at)
                .order_by(InstrumentRecord.retrieved_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            return _aware(row)

    @staticmethod
    def _to_instrument(row: InstrumentRecord) -> Instrument:
        return Instrument(
            instrument_token=row.instrument_token,
            exchange_token=row.exchange_token,
            tradingsymbol=row.tradingsymbol,
            name=row.name,
            exchange=row.exchange,
            segment=row.segment,
            instrument_type=row.instrument_type,
            tick_size=row.tick_size,
            lot_size=row.lot_size,
            expiry=row.expiry,
            strike=row.strike,
            last_price=row.last_price,
            source=row.source,
            retrieved_at=_aware(row.retrieved_at),
        )

    # ------------------------------------------------------------------ #
    # ticks
    # ------------------------------------------------------------------ #

    def save_ticks(self, ticks: Sequence[MarketTick]) -> int:
        if not ticks:
            return 0
        with self._write() as session:
            session.add_all(
                [
                    MarketTickRecord(
                        instrument_token=t.instrument_token,
                        tradingsymbol=t.tradingsymbol,
                        exchange=t.exchange,
                        mode=t.mode.value,
                        event_time=t.event_time,
                        exchange_timestamp=t.exchange_timestamp,
                        received_at=t.received_at,
                        last_price=t.last_price,
                        last_quantity=t.last_quantity,
                        average_price=t.average_price,
                        volume=t.volume,
                        total_buy_quantity=t.total_buy_quantity,
                        total_sell_quantity=t.total_sell_quantity,
                        open_price=t.open_price,
                        high_price=t.high_price,
                        low_price=t.low_price,
                        close_price=t.close_price,
                        open_interest=t.open_interest,
                        depth=_depth_to_json(t.depth),
                        is_index=t.is_index,
                        source=t.source,
                    )
                    for t in ticks
                ]
            )
        return len(ticks)

    def latest_tick(self, instrument_token: int) -> MarketTick | None:
        with self._read() as session:
            row = session.execute(
                select(MarketTickRecord)
                .where(MarketTickRecord.instrument_token == instrument_token)
                .order_by(MarketTickRecord.event_time.desc(), MarketTickRecord.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            return self._to_tick(row) if row else None

    def ticks_in_range(
        self, instrument_token: int, start: datetime, end: datetime, limit: int = 10_000
    ) -> list[MarketTick]:
        with self._read() as session:
            rows = (
                session.execute(
                    select(MarketTickRecord)
                    .where(
                        MarketTickRecord.instrument_token == instrument_token,
                        MarketTickRecord.event_time >= start,
                        MarketTickRecord.event_time < end,
                    )
                    .order_by(MarketTickRecord.event_time, MarketTickRecord.id)
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [self._to_tick(row) for row in rows]

    def prune_ticks_before(self, cutoff: datetime) -> int:
        """Tick retention is a policy, not an accident. Candles are kept."""
        with self._write() as session:
            # DML through Session.execute returns a CursorResult at runtime;
            # SQLAlchemy types it as the wider Result, which has no rowcount.
            statement = delete(MarketTickRecord).where(MarketTickRecord.received_at < cutoff)
            result = cast(CursorResult[Any], session.execute(statement))
            return int(result.rowcount or 0)

    @staticmethod
    def _to_tick(row: MarketTickRecord) -> MarketTick:
        return MarketTick(
            instrument_token=row.instrument_token,
            last_price=row.last_price,
            mode=TickMode(row.mode),
            received_at=_aware(row.received_at),  # type: ignore[arg-type]
            exchange=row.exchange,
            tradingsymbol=row.tradingsymbol,
            exchange_timestamp=_aware(row.exchange_timestamp),
            last_quantity=row.last_quantity,
            average_price=row.average_price,
            volume=row.volume,
            total_buy_quantity=row.total_buy_quantity,
            total_sell_quantity=row.total_sell_quantity,
            open_price=row.open_price,
            high_price=row.high_price,
            low_price=row.low_price,
            close_price=row.close_price,
            open_interest=row.open_interest,
            depth=_depth_from_json(row.depth),
            is_index=row.is_index,
            source=row.source,
        )

    # ------------------------------------------------------------------ #
    # candles
    # ------------------------------------------------------------------ #

    def save_candles(self, candles: Sequence[Candle]) -> int:
        """Persist COMPLETED candles only, idempotently.

        An IN_PROGRESS bar is rejected rather than written: persisting one would
        let a later reader mistake a still-changing bar for settled history.
        """
        completed = [c for c in candles if c.is_completed]
        if not completed:
            return 0
        written = 0
        with self._write() as session:
            for candle in completed:
                existing = session.execute(
                    select(CandleRecord).where(
                        CandleRecord.instrument_token == candle.instrument_token,
                        CandleRecord.interval == candle.interval.value,
                        CandleRecord.start_at == candle.start_at,
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    existing.high = candle.high
                    existing.low = candle.low
                    existing.close = candle.close
                    existing.volume = candle.volume
                    existing.tick_count = candle.tick_count
                    existing.last_update_at = candle.last_update_at
                    continue
                session.add(
                    CandleRecord(
                        instrument_token=candle.instrument_token,
                        tradingsymbol=candle.tradingsymbol,
                        exchange=candle.exchange,
                        interval=candle.interval.value,
                        start_at=candle.start_at,
                        end_at=candle.end_at,
                        open=candle.open,
                        high=candle.high,
                        low=candle.low,
                        close=candle.close,
                        volume=candle.volume,
                        tick_count=candle.tick_count,
                        status=candle.status.value,
                        source=candle.source,
                        last_update_at=candle.last_update_at,
                    )
                )
                written += 1
        return written

    def latest_completed_candle(
        self, instrument_token: int, interval: CandleInterval
    ) -> Candle | None:
        with self._read() as session:
            row = session.execute(
                select(CandleRecord)
                .where(
                    CandleRecord.instrument_token == instrument_token,
                    CandleRecord.interval == interval.value,
                    CandleRecord.status == CandleStatus.COMPLETED.value,
                )
                .order_by(CandleRecord.start_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            return self._to_candle(row) if row else None

    def candles_in_range(
        self,
        instrument_token: int,
        interval: CandleInterval,
        start: datetime,
        end: datetime,
        limit: int = 5_000,
    ) -> list[Candle]:
        with self._read() as session:
            rows = (
                session.execute(
                    select(CandleRecord)
                    .where(
                        CandleRecord.instrument_token == instrument_token,
                        CandleRecord.interval == interval.value,
                        CandleRecord.start_at >= start,
                        CandleRecord.start_at < end,
                    )
                    .order_by(CandleRecord.start_at)
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return [self._to_candle(row) for row in rows]

    def recent_candles(
        self, instrument_token: int, interval: CandleInterval, count: int
    ) -> list[Candle]:
        with self._read() as session:
            rows = (
                session.execute(
                    select(CandleRecord)
                    .where(
                        CandleRecord.instrument_token == instrument_token,
                        CandleRecord.interval == interval.value,
                    )
                    .order_by(CandleRecord.start_at.desc())
                    .limit(count)
                )
                .scalars()
                .all()
            )
            return [self._to_candle(row) for row in reversed(rows)]

    @staticmethod
    def _to_candle(row: CandleRecord) -> Candle:
        return Candle(
            instrument_token=row.instrument_token,
            interval=CandleInterval(row.interval),
            start_at=_aware(row.start_at),  # type: ignore[arg-type]
            end_at=_aware(row.end_at),  # type: ignore[arg-type]
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            volume=row.volume,
            status=CandleStatus(row.status),
            tick_count=row.tick_count,
            source=row.source,
            tradingsymbol=row.tradingsymbol,
            exchange=row.exchange,
            last_update_at=_aware(row.last_update_at),
        )

    # ------------------------------------------------------------------ #
    # operational events
    # ------------------------------------------------------------------ #

    def record_connection_event(self, event: ConnectionEvent) -> None:
        with self._write() as session:
            session.add(
                ConnectionEventRecord(
                    provider=event.provider,
                    event_type=event.event_type.value,
                    state=event.state.value if event.state else None,
                    occurred_at=event.occurred_at,
                    detail=dict(event.detail) or None,
                )
            )

    def record_data_gap(self, gap: DataGap) -> None:
        with self._write() as session:
            session.add(
                DataGapRecord(
                    provider=gap.provider,
                    started_at=gap.started_at,
                    ended_at=gap.ended_at,
                    duration_seconds=Decimal(str(round(gap.duration_seconds, 3))),
                    reason=gap.reason,
                    instrument_count=len(gap.instrument_tokens),
                    detail={"instrument_tokens": list(gap.instrument_tokens[:200])},
                )
            )

    def record_quality_events(self, events: Sequence[DataQualityEvent]) -> int:
        if not events:
            return 0
        with self._write() as session:
            session.add_all(
                [
                    DataQualityEventRecord(
                        issue=event.issue.value,
                        provider=event.provider,
                        instrument_token=event.instrument_token,
                        occurred_at=event.occurred_at,
                        detail=dict(event.detail) or None,
                    )
                    for event in events
                ]
            )
        return len(events)

    def recent_connection_events(self, limit: int = 50) -> list[ConnectionEventRecord]:
        with self._read() as session:
            return list(
                session.execute(
                    select(ConnectionEventRecord)
                    .order_by(ConnectionEventRecord.occurred_at.desc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )

    def data_gap_count(self) -> int:
        with self._read() as session:
            return len(session.execute(select(DataGapRecord.id)).scalars().all())
