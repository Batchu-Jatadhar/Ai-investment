"""The execution seam.

Where a strategy's answer stops and the simulator's begins. **Nothing here
fills, prices, or slips anything** - the behaviour arrives in Phase 2.4, and the
configuration it will read (:class:`SlippageConfig`, :class:`ExecutionConfig`)
and the record it will produce (:class:`Fill`) already exist. What was missing
was the thing in between: a statement of what the simulator has been asked to
do.

That statement is :class:`ExecutionIntent`, and its job is to make the approved
model's first rule unrepresentable to break. A signal is produced on the close
of bar N and entered at the open of bar N+1, because nobody can trade on a
price that has already printed. Expressed as a type, "the entry bar starts
strictly after the signal bar" is checked once at construction rather than
being an assumption every future code path has to remember.

.. rubric:: The protective stop

:func:`resolve_stop_fill` is the first piece of actual execution here. The stop
is modelled as a **resting protective order**: it sits at the exchange, so it
fills on a touch rather than needing the bar to trade through, and a bar that
gaps past it fills at that bar's opening price rather than at a level nobody
was willing to trade at.

Note the asymmetry with the breakout rule one layer up, which is deliberate and
not a mistake: a breakout needs a *close strictly beyond* the level, while a
stop triggers on a mere touch. They model different things. A breakout is an
inference about intent from where the bar settled; a stop is an order already
sitting in the book, and the book does not wait for a close.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.core.time import ensure_utc
from app.domain.backtest.config import SlippageConfig
from app.domain.backtest.models import Fill, FillReason, OrderSide
from app.domain.market.models import Candle, CandleStatus
from app.domain.strategy.contract import Signal, SignalDirection

__all__ = ["ExecutionIntent", "resolve_stop_fill"]


@dataclass(frozen=True, slots=True)
class ExecutionIntent:
    """An accepted signal, sized, awaiting execution on a named bar.

    ``quantity`` is **supplied, never derived here**. Sizing is the risk
    engine's decision in Phase 3, and in Phase 2 it comes from the fixed
    notional placeholder. This type records the number it was handed so a fill
    can be attributed to it; it has no opinion about what the number should be,
    and no access to the account state that would let it form one.

    ``entry_bar_start`` names the bar the entry executes on - bar N+1 for a
    signal produced on bar N. It is a bar identity rather than a price: what
    that bar opened at is the simulator's to discover, and putting a price here
    would recreate exactly the leak the strategy contract was shaped to prevent.

    Carries no order identifier, no broker reference, no account and no venue.
    A backtest has no orders, only assumptions about how one would have filled,
    and a type that could hold a broker's order id would invite a live-trading
    path to grow through the simulator.
    """

    signal: Signal
    quantity: int
    entry_bar_start: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "entry_bar_start", ensure_utc(self.entry_bar_start))
        if self.quantity <= 0:
            raise ValueError(
                f"quantity must be positive, got {self.quantity}; an intent to trade nothing "
                "is not an intent"
            )
        if self.entry_bar_start <= self.signal.signal_bar_start:
            raise ValueError(
                f"entry_bar_start ({self.entry_bar_start.isoformat()}) must be strictly after "
                f"the signal bar ({self.signal.signal_bar_start.isoformat()}); a signal "
                "produced on a bar's close cannot be filled on that same bar, because that "
                "bar's prices have already printed"
            )

    @property
    def direction(self) -> SignalDirection:
        return self.signal.direction

    @property
    def instrument_token(self) -> int:
        return self.signal.instrument_token

    @property
    def entry_side(self) -> OrderSide:
        """BUY for a long, SELL for a short.

        Derived rather than stored: the side and the direction cannot disagree
        if there is only one of them.
        """
        return OrderSide.entry_for(self.direction)

    @property
    def exit_side(self) -> OrderSide:
        return self.entry_side.opposite


def resolve_stop_fill(
    intent: ExecutionIntent,
    bar: Candle,
    *,
    tick_size: Decimal,
    slippage: SlippageConfig,
) -> Fill | None:
    """The protective stop's fill on ``bar``, or ``None`` if it survived.

    ``bar`` is one bar of the open position's life, at or after the entry bar.
    Only that bar is read - the function is handed no series and no index, so
    it cannot consult what happened afterwards even in principle. Whether the
    stop had already filled on an earlier bar is the caller's sequencing
    problem, and giving this function the surrounding bars is what would let
    that leak.

    **A touch is enough.** For a long the stop triggers when ``low`` reaches it,
    for a short when ``high`` does, and reaching it exactly counts. The order is
    already resting in the book, so a bar that dips through the level and
    recovers still took the position out - the recovery is only visible with
    hindsight the position did not have. This is the opposite of the breakout
    rule, which needs a close strictly beyond the level, and the difference is
    the point: one is an inference from where a bar settled, the other is an
    order that was already sitting there.

    **A gap fills at the opening price.** If the bar starts already beyond the
    stop, there was no trade at the stop level to be had, so the fill is the
    open. Pretending otherwise would credit the run a price nobody offered,
    which is the single most flattering error a backtest can make.

    Slippage is applied on top in both cases, always adverse - a long exits
    lower, a short exits higher. Applying it to a gap fill as well is the
    pessimistic reading: the open is where the bar started, not necessarily
    where a market order leaving that instant would have been filled.

    ``costs`` is ``0`` here. The statutory Indian charges are Phase 2.5 and must
    be verified against named sources before they are applied; a fill carrying
    an invented cost would be worse than one that visibly carries none.
    """
    if bar.status is not CandleStatus.COMPLETED:
        raise ValueError(
            f"the bar at {bar.start_at.isoformat()} is {bar.status.value}; a fill cannot be "
            "resolved against a bar whose high and low can still move"
        )
    if bar.start_at < intent.entry_bar_start:
        raise ValueError(
            f"the bar at {bar.start_at.isoformat()} precedes the entry bar "
            f"({intent.entry_bar_start.isoformat()}); a protective stop cannot fill before "
            "the position it protects exists"
        )
    if tick_size <= 0:
        raise ValueError(f"tick_size must be positive, got {tick_size}")

    stop = intent.signal.stop_price
    is_long = intent.direction.is_long

    gapped = bar.open <= stop if is_long else bar.open >= stop
    touched = bar.low <= stop if is_long else bar.high >= stop
    if not touched:
        return None

    reference = bar.open if gapped else stop
    adverse = slippage.adverse_ticks * tick_size
    price = reference - adverse if is_long else reference + adverse

    return Fill(
        side=intent.exit_side,
        reason=FillReason.STOP,
        quantity=intent.quantity,
        price=price,
        reference_price=reference,
        slippage_per_unit=adverse,
        costs=Decimal(0),
        # A gap fill happened at the opening print, which is a time we know. A
        # touch happened somewhere inside the bar, and all we can honestly say
        # is that it had happened by the close. Intrabar timing is what the
        # 1-minute resolution phase is for; nothing here invents it.
        occurred_at=bar.start_at if gapped else bar.end_at,
        bar_start=bar.start_at,
    )
