"""The paper execution adapter.

Implements :class:`~app.domain.execution.ports.ExecutionPort` by replaying the
backtest's own fill resolvers against completed bars fed to :meth:`on_bar`. It
has no broker, no client, no socket and no clock: it cannot send an order
because it holds nothing an order could be sent through. Tests assert that.

.. rubric:: Assumptions, all inherited rather than invented

*   **Latency** is ``ExecutionConfig.entry_timing``: a request made on bar N's
    close executes at bar N+1's open. That holds for entries and for
    :meth:`flatten` alike. If bar N+1 is missing, the entry EXPIRES; a later bar
    is never substituted.
*   **Slippage** is ``SlippageConfig``, adverse on every leg.
*   **Stop, target and hard exit** are ``resolve_exit_fill`` and
    ``resolve_hard_exit_fill``, so a paper trade and a backtest trade over the
    same bars are the same trade.
*   **Costs** are the ``CostSchedule``.

One instrument, one working order or position at a time, as in the backtest.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, time
from decimal import Decimal

from app.domain.backtest.cash import CashLedger
from app.domain.backtest.config import CostSchedule, ExecutionConfig, SlippageConfig
from app.domain.backtest.costs import leg_charges
from app.domain.backtest.execution import (
    ExecutionIntent,
    resolve_entry_fill,
    resolve_exit_fill,
    resolve_hard_exit_fill,
)
from app.domain.backtest.models import AmbiguityResolution, ExecutionStatus, Fill, FillReason, Trade
from app.domain.backtest.position import Position, PositionBook
from app.domain.execution.ports import InvalidTransitionError, OrderRecord, OrderStatus
from app.domain.market.models import Candle, CandleInterval, CandleStatus, Instrument
from app.domain.risk.sizing import RiskDecision
from app.domain.strategy.contract import Signal

__all__ = ["PaperExecutionAdapter"]


class PaperExecutionAdapter:
    """Simulated execution for one instrument. Deterministic: same calls, same fills."""

    def __init__(
        self,
        *,
        instrument: Instrument,
        starting_capital: Decimal,
        signal_interval: CandleInterval,
        hard_exit_time: time,
        execution: ExecutionConfig,
        slippage: SlippageConfig,
        cost_schedule: CostSchedule,
    ) -> None:
        self._instrument = instrument
        self._interval = signal_interval
        self._hard_exit_time = hard_exit_time
        self._execution = execution
        self._slippage = slippage
        self._costs = cost_schedule
        self._ledger = CashLedger.funded(starting_capital)
        self._book = PositionBook()
        self._intent: ExecutionIntent | None = None  # of the held position
        self._trades: list[Trade] = []
        self._orders: dict[str, OrderRecord] = {}
        self._working: str | None = None  # client_order_id awaiting its entry bar
        self._flatten_pending = False
        self._last_bar_start: datetime | None = None

    # ------------------------------------------------------------------ views

    @property
    def position(self) -> Position | None:
        return self._book.position

    @property
    def trades(self) -> tuple[Trade, ...]:
        return tuple(self._trades)

    @property
    def cash(self) -> Decimal:
        return self._ledger.cash

    def order(self, client_order_id: str) -> OrderRecord:
        try:
            return self._orders[client_order_id]
        except KeyError:
            raise InvalidTransitionError(f"no order {client_order_id!r} was submitted") from None

    # --------------------------------------------------------------- commands

    def submit(self, client_order_id: str, signal: Signal, decision: RiskDecision) -> OrderRecord:
        """Accept or reject an entry. Resubmitting the identical request returns the
        original record; reusing the id for a different request raises."""
        existing = self._orders.get(client_order_id)
        if existing is not None:
            if existing.signal == signal and existing.quantity == decision.quantity:
                return existing
            raise InvalidTransitionError(
                f"client_order_id {client_order_id!r} was already used for a different request"
            )

        reasons = [r.code.value for r in decision.rejections]
        if signal.instrument_token != self._instrument.instrument_token:
            reasons.append("instrument_mismatch")
        if self._working is not None or not self._book.is_flat or self._flatten_pending:
            reasons.append("order_or_position_active")
        entry_bar_start = signal.signal_bar_start + self._interval.delta
        if self._last_bar_start is not None and entry_bar_start <= self._last_bar_start:
            reasons.append("entry_bar_already_passed")

        record = OrderRecord(
            client_order_id=client_order_id,
            signal=signal,
            quantity=decision.quantity,
            status=OrderStatus.REJECTED if reasons else OrderStatus.ACCEPTED,
            reasons=tuple(reasons),
        )
        self._orders[client_order_id] = record
        if not reasons:
            self._working = client_order_id
        return record

    def cancel(self, client_order_id: str) -> OrderRecord:
        record = self.order(client_order_id)
        return self._transition(record, OrderStatus.CANCELLED)

    def flatten(self) -> None:
        """Cancel the working entry now; exit any held position at the next bar's open."""
        if self._flatten_pending:
            raise InvalidTransitionError("a flatten is already pending")
        if self._working is None and self._book.is_flat:
            raise InvalidTransitionError("nothing to flatten: no working order and no position")
        if self._working is not None:
            self.cancel(self._working)
        if not self._book.is_flat:
            self._flatten_pending = True

    def on_bar(self, bar: Candle, minute_bars: Sequence[Candle] = ()) -> tuple[Fill, ...]:
        """Advance the simulation by one completed bar and return the fills it produced."""
        self._require_next_bar(bar)
        fills: list[Fill] = []

        record = self._orders[self._working] if self._working is not None else None
        # A bar before the entry bar leaves the order working; one after it expires it.
        entry_bar_start = (
            record.signal.signal_bar_start + self._interval.delta if record is not None else None
        )
        if record is not None and entry_bar_start is not None and bar.start_at >= entry_bar_start:
            intent = ExecutionIntent(record.signal, record.quantity, entry_bar_start)
            outcome = resolve_entry_fill(
                intent,
                bar if bar.start_at == intent.entry_bar_start else None,
                tick_size=self._instrument.tick_size,
                slippage=self._slippage,
                cost_schedule=self._costs,
            )
            if outcome.status is ExecutionStatus.NO_EXECUTION_BAR:
                self._transition(record, OrderStatus.EXPIRED)
            elif outcome.fill is not None:
                self._transition(record, OrderStatus.FILLED, fill=outcome.fill)
                self._book = self._book.enter(intent, outcome.fill)
                self._ledger = self._ledger.after_entry(outcome.fill)
                self._intent = intent
                fills.append(outcome.fill)

        if self._flatten_pending:
            fills.append(self._close(self._flatten_fill(bar)))
            self._flatten_pending = False
        elif self._intent is not None:
            intent = self._intent
            held = self._book.position
            assert held is not None
            resolution = resolve_exit_fill(
                intent,
                held.entry,
                bar,
                tick_size=self._instrument.tick_size,
                execution=self._execution,
                slippage=self._slippage,
                minute_bars=minute_bars,
                cost_schedule=self._costs,
            )
            exit_fill = resolution.fill or resolve_hard_exit_fill(
                intent,
                bar,
                hard_exit_time=self._hard_exit_time,
                tick_size=self._instrument.tick_size,
                slippage=self._slippage,
                cost_schedule=self._costs,
            )
            if exit_fill is not None:
                fills.append(self._close(exit_fill, resolution.ambiguity))

        return tuple(fills)

    # ---------------------------------------------------------------- helpers

    def _transition(
        self, record: OrderRecord, status: OrderStatus, *, fill: Fill | None = None
    ) -> OrderRecord:
        if record.status is not OrderStatus.ACCEPTED:
            raise InvalidTransitionError(
                f"order {record.client_order_id!r} is {record.status.value} and cannot become "
                f"{status.value}"
            )
        updated = replace(record, status=status, fill=fill)
        self._orders[record.client_order_id] = updated
        self._working = None
        return updated

    def _close(
        self,
        exit_fill: Fill,
        ambiguity: AmbiguityResolution = AmbiguityResolution.UNAMBIGUOUS,
    ) -> Fill:
        book, trade = self._book.close(exit_fill, ambiguity=ambiguity)
        self._book = book
        self._ledger = self._ledger.after_exit(trade)
        self._trades.append(trade)
        self._intent = None
        return exit_fill

    def _flatten_fill(self, bar: Candle) -> Fill:
        """Exit at this bar's open, adverse slippage, charged like any other leg."""
        assert self._intent is not None
        side = self._intent.exit_side
        adverse = self._slippage.adverse_ticks * self._instrument.tick_size
        price = bar.open - adverse if self._intent.direction.is_long else bar.open + adverse
        quantity = self._intent.quantity
        return Fill(
            side=side,
            reason=FillReason.FLATTEN,
            quantity=quantity,
            price=price,
            reference_price=bar.open,
            slippage_per_unit=adverse,
            costs=leg_charges(self._costs, side=side, turnover=price * quantity).total,
            occurred_at=bar.start_at,
            bar_start=bar.start_at,
        )

    def _require_next_bar(self, bar: Candle) -> None:
        if bar.status is not CandleStatus.COMPLETED:
            raise InvalidTransitionError(f"bar at {bar.start_at.isoformat()} is not completed")
        if bar.instrument_token != self._instrument.instrument_token:
            raise InvalidTransitionError(
                f"bar is for instrument {bar.instrument_token}, not "
                f"{self._instrument.instrument_token}"
            )
        if bar.interval is not self._interval:
            raise InvalidTransitionError(
                f"bar interval {bar.interval.value} is not {self._interval.value}"
            )
        if self._last_bar_start is not None and bar.start_at <= self._last_bar_start:
            raise InvalidTransitionError(
                f"bar at {bar.start_at.isoformat()} does not follow "
                f"{self._last_bar_start.isoformat()}; bars must arrive in order, once"
            )
        self._last_bar_start = bar.start_at
