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

.. rubric:: The target

:func:`resolve_target_fill` models the other exit, and models it differently on
purpose. The target is **not** a resting limit order. It is fired by the engine
as an IOC once it sees the price trade through the level, which is the accepted
cost of OCO Design B: with only one protective order resting at the broker, the
target has to be triggered rather than waited on, and a bar that merely reaches
the level does not fill.

That is where ``target_requires_through_ticks`` comes in. The bar must trade
through the target by at least that many ticks before the engine is credited
with having reacted.

The two exits are pessimistic in opposite directions, and both deliberately:

*   A **stop** that gaps fills at the worse price the bar opened at, because
    that is what the market offered.
*   A **target** that gaps fills at the target level and no better, because
    crediting the whole favourable gap would assume an engine reaction faster
    than the one being modelled.

Neither exit is ever credited with the good half of a surprise.

.. rubric:: When execution cannot be established

Two different things can stop a fill happening, and conflating them is how a
backtest quietly reports a number it did not earn.

*   **The market did not do it.** The stop was not touched, the target was not
    traded through, the session ended before the entry bar existed. These are
    ordinary results. They are returned - ``None`` for an exit that survived,
    :attr:`ExecutionStatus.NO_EXECUTION_BAR` for a signal with nowhere to fill.

*   **The data cannot say.** A bar with no volume, a flat bar with no trades,
    a bar overlapping a recorded :class:`~app.domain.market.ports.DataGap`.
    Here the honest answer is not "no fill" - it is "this bar cannot answer the
    question", and the two are opposite. Reporting silence would let a run
    trade straight through a hole in its own data and show a clean equity curve
    for it. These raise :class:`UnexecutableBarError`, because deciding what to
    do about a hole - quarantine the session, skip the instrument, abandon the
    run - is the engine's call and cannot be made inside a fill resolver.

The rule in one line: *the market saying no is a result, the data being unable
to say is an error.*

.. rubric:: When both exits are inside one bar

A 5-minute bar that reached the stop *and* traded through the target says
nothing about which happened first, and the difference is the whole trade: one
outcome is -1R, the other is +2R. :func:`resolve_exit_fill` decides it in two
tiers, and records which tier decided.

*   **Tier 1 - the minute bars.** The five completed 1-minute bars inside that
    5-minute window are walked in order, and whichever level is reached first
    wins. They are used *only to order the two events*; the fill itself is the
    one the 5-minute bar already produced, so this resolves the ambiguity
    without changing the execution model.
*   **Tier 2 - assume the stop.** When the minute bars are missing, incomplete
    or themselves unusable, the stop is taken. Never the target. An engine that
    guessed favourably here would turn its worst data into its best results,
    which is the most expensive way a backtest can lie.

Both tiers are recorded as an
:class:`~app.domain.backtest.models.AmbiguityResolution`, because a run where
most exits fell back to the assumption is weaker than one where most were
resolved from real data, and the report has to be able to say so.

A minute in which *both* levels are reached is ambiguous at 1-minute resolution
too. It takes the stop, and it records ``PESSIMISTIC_FALLBACK`` rather than
``RESOLVED_BY_1M`` - the minute data was read but did not decide, and the label
names what actually decided.

.. rubric:: The hard exit

:func:`resolve_hard_exit_fill` flattens whatever is still open at the hypothesis'
cutoff, 15:15 IST. It lives here and not in the strategy on purpose: the
strategy proposes entries and the levels that invalidate them, and a
"hard exit signal" would be a strategy inventing an exit it has no business
owning. Being flat before the closing auction is a property of how this engine
trades, not of what the ORB believes about price.

The cutoff is read from the bar's own timestamp, never from a clock. That is
what makes a run over 2019 data produce the same answer in 2026.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from enum import StrEnum

from app.core.time import ensure_utc, to_ist
from app.domain.backtest.config import ExecutionConfig, SlippageConfig
from app.domain.backtest.models import AmbiguityResolution, Fill, FillReason, OrderSide
from app.domain.market.models import Candle, CandleInterval, CandleStatus
from app.domain.market.ports import DataGap
from app.domain.strategy.contract import Signal, SignalDirection

__all__ = [
    "EntryOutcome",
    "ExecutionIntent",
    "ExecutionStatus",
    "ExitResolution",
    "UnexecutableBarError",
    "resolve_entry_fill",
    "resolve_exit_fill",
    "resolve_hard_exit_fill",
    "resolve_stop_fill",
    "resolve_target_fill",
]


class ExecutionStatus(StrEnum):
    """What became of an attempt to execute.

    One enum for both axes on purpose: a run's execution log wants a single
    column it can count, not a status that sometimes lives in a return value
    and sometimes in an exception type.
    """

    FILLED = "filled"
    #: The signal had no bar to be entered on - the session ended first.
    NO_EXECUTION_BAR = "no_execution_bar"
    #: The bar recorded no trades, so no price on it was ever transacted.
    NO_VOLUME = "no_volume"
    #: The bar neither moved nor traded: a placeholder, not a bar.
    NO_RANGE = "no_range"
    #: The bar overlaps a recorded gap in the feed, so its prices are suspect.
    INSIDE_DATA_GAP = "inside_data_gap"


class UnexecutableBarError(ValueError):
    """Raised when a bar cannot establish an execution at all.

    Distinct from "the level was not reached", which is an ordinary result and
    is returned rather than raised. This says the data is unusable, and it
    carries :attr:`status` so a caller can branch on the kind rather than
    matching on message text.
    """

    def __init__(self, status: ExecutionStatus, bar_start: datetime, detail: str) -> None:
        self.status = status
        self.bar_start = bar_start
        super().__init__(f"bar at {bar_start.isoformat()} is unusable ({status.value}): {detail}")


@dataclass(frozen=True, slots=True)
class EntryOutcome:
    """The result of trying to enter, and why it turned out that way.

    Mirrors ``OrbDecision`` one layer up: the fill and the reason travel
    together, so the path where nothing happened cannot be logged without
    saying what happened instead.
    """

    status: ExecutionStatus
    fill: Fill | None

    def __post_init__(self) -> None:
        if (self.status is ExecutionStatus.FILLED) != (self.fill is not None):
            raise ValueError(
                f"status {self.status.value} and "
                f"{'a fill' if self.fill else 'no fill'} disagree; a filled entry must carry "
                "its fill and an unfilled one must not"
            )


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
    gaps: Sequence[DataGap] = (),
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

    _require_executable(bar, intent, gaps)

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


def resolve_target_fill(
    intent: ExecutionIntent,
    entry: Fill,
    bar: Candle,
    *,
    tick_size: Decimal,
    execution: ExecutionConfig,
    slippage: SlippageConfig,
    gaps: Sequence[DataGap] = (),
) -> Fill | None:
    """The target's fill on ``bar``, or ``None`` if it was not reached.

    The target level is resolved here rather than carried on the signal,
    because R is measured from the entry and the entry is not known until the
    fill. ``entry`` supplies it: risk is the distance from the fill to the
    stop, and the target sits ``target_r_multiple`` of that distance the other
    side of the fill. Two trades from the same signal at different fills have
    different targets, which is exactly what an absolute target price on the
    signal could not have expressed.

    **Reaching the level is not enough.** The engine fires an IOC when it sees
    the price trade through, so the bar must exceed the target by
    ``target_requires_through_ticks``. Reaching the trigger exactly counts as
    having traded through by exactly the threshold, and does fill; a graze that
    stops one tick short does not. With the threshold configured to zero the
    trigger is the target itself and a touch fills, which is the resting-limit
    behaviour - the rule follows the configuration rather than being hard-coded
    either way.

    **A favourable gap is not credited.** However far through the bar went, the
    fill is the target level, never the better price the bar opened at. Taking
    the gap would assume the engine reacted faster than the model claims it
    does. This is the mirror of the stop, which *is* filled at the worse price
    a gap opened at - between them, neither exit is ever credited with the good
    half of a surprise.

    Slippage applies on top, adverse as always: a long exits lower, a short
    exits higher. ``costs`` is ``0`` for the reason given on the stop.

    Only ``bar`` is read. Nothing here can see the bars either side of it.
    """
    if entry.reason is not FillReason.ENTRY:
        raise ValueError(
            f"entry must be an ENTRY fill, got {entry.reason.value}; the target is measured "
            "from the price actually paid to open the position"
        )
    if bar.status is not CandleStatus.COMPLETED:
        raise ValueError(
            f"the bar at {bar.start_at.isoformat()} is {bar.status.value}; a fill cannot be "
            "resolved against a bar whose high and low can still move"
        )
    if bar.start_at < intent.entry_bar_start:
        raise ValueError(
            f"the bar at {bar.start_at.isoformat()} precedes the entry bar "
            f"({intent.entry_bar_start.isoformat()}); a target cannot fill before the position "
            "it closes exists"
        )
    if tick_size <= 0:
        raise ValueError(f"tick_size must be positive, got {tick_size}")

    _require_executable(bar, intent, gaps)

    is_long = intent.direction.is_long
    risk = abs(entry.price - intent.signal.stop_price)
    if risk == 0:
        raise ValueError(
            f"the entry filled at the stop ({entry.price}), so risk is zero and no R multiple "
            "describes a target; such a trade should never have been opened"
        )

    reach = intent.signal.target_r_multiple * risk
    target = entry.price + reach if is_long else entry.price - reach
    through = execution.target_requires_through_ticks * tick_size
    trigger = target + through if is_long else target - through

    reached = bar.high >= trigger if is_long else bar.low <= trigger
    if not reached:
        return None

    adverse = slippage.adverse_ticks * tick_size
    price = target - adverse if is_long else target + adverse

    return Fill(
        side=intent.exit_side,
        reason=FillReason.TARGET,
        quantity=intent.quantity,
        price=price,
        reference_price=target,
        slippage_per_unit=adverse,
        costs=Decimal(0),
        # The through-print happened somewhere inside the bar; all that can
        # honestly be said is that it had happened by the close.
        occurred_at=bar.end_at,
        bar_start=bar.start_at,
    )


def _require_executable(bar: Candle, intent: ExecutionIntent, gaps: Sequence[DataGap]) -> None:
    """Refuse a bar that cannot establish any execution.

    Order matters: a bar that neither moved nor traded is described as
    :attr:`ExecutionStatus.NO_RANGE`, the more specific fact, rather than as
    merely volumeless.

    A flat bar that *did* trade is left alone. One price printed and it is a
    real one - an illiquid instrument that traded once in five minutes is thin,
    not corrupt, and rejecting it would discard data the market actually made.
    """
    if bar.high == bar.low and bar.volume == 0:
        raise UnexecutableBarError(
            ExecutionStatus.NO_RANGE,
            bar.start_at,
            "it neither moved nor traded, so it is a placeholder rather than a bar and no "
            "price on it was ever transacted",
        )
    if bar.volume == 0:
        raise UnexecutableBarError(
            ExecutionStatus.NO_VOLUME,
            bar.start_at,
            f"it spans {bar.low}-{bar.high} but recorded no trades, so any fill taken from it "
            "would be a price nobody paid",
        )
    for gap in gaps:
        if gap.instrument_tokens and intent.instrument_token not in gap.instrument_tokens:
            continue
        if bar.start_at < gap.ended_at and bar.end_at > gap.started_at:
            raise UnexecutableBarError(
                ExecutionStatus.INSIDE_DATA_GAP,
                bar.start_at,
                f"it overlaps a recorded gap from {gap.started_at.isoformat()} to "
                f"{gap.ended_at.isoformat()} ({gap.reason}); the feed was not delivering, so "
                "the bar's extremes are whatever happened to arrive rather than what traded",
            )


def resolve_entry_fill(
    intent: ExecutionIntent,
    next_bar: Candle | None,
    *,
    tick_size: Decimal,
    slippage: SlippageConfig,
    gaps: Sequence[DataGap] = (),
) -> EntryOutcome:
    """Enter at ``next_bar``'s opening price, or report why not.

    ``next_bar`` is the bar named by :attr:`ExecutionIntent.entry_bar_start`,
    or ``None`` when there is not one - a signal on the session's last bar has
    nowhere to be filled, and that is an ordinary end-of-day outcome rather
    than an error. It is reported as
    :attr:`ExecutionStatus.NO_EXECUTION_BAR` and nothing is fabricated.

    Slippage is adverse: a long pays up, a short sells down.
    """
    if next_bar is None:
        return EntryOutcome(ExecutionStatus.NO_EXECUTION_BAR, None)

    if next_bar.status is not CandleStatus.COMPLETED:
        raise ValueError(
            f"the bar at {next_bar.start_at.isoformat()} is {next_bar.status.value}; an entry "
            "cannot be resolved against a bar that is still forming"
        )
    if next_bar.start_at != intent.entry_bar_start:
        raise ValueError(
            f"next_bar starts at {next_bar.start_at.isoformat()} but the intent names "
            f"{intent.entry_bar_start.isoformat()}; entering on any other bar would be "
            "executing at a price the signal could not have been acted on at"
        )
    if tick_size <= 0:
        raise ValueError(f"tick_size must be positive, got {tick_size}")

    _require_executable(next_bar, intent, gaps)

    adverse = slippage.adverse_ticks * tick_size
    reference = next_bar.open
    price = reference + adverse if intent.entry_side is OrderSide.BUY else reference - adverse

    return EntryOutcome(
        ExecutionStatus.FILLED,
        Fill(
            side=intent.entry_side,
            reason=FillReason.ENTRY,
            quantity=intent.quantity,
            price=price,
            reference_price=reference,
            slippage_per_unit=adverse,
            costs=Decimal(0),
            occurred_at=next_bar.start_at,
            bar_start=next_bar.start_at,
        ),
    )


@dataclass(frozen=True, slots=True)
class ExitResolution:
    """How a bar's exit was decided, and by which tier.

    ``ambiguity`` is ``UNAMBIGUOUS`` whenever only one level was reached, or
    none - there was nothing to resolve. It is only ``RESOLVED_BY_1M`` or
    ``PESSIMISTIC_FALLBACK`` when both were reached inside the same bar.
    """

    fill: Fill | None
    ambiguity: AmbiguityResolution = AmbiguityResolution.UNAMBIGUOUS

    def __post_init__(self) -> None:
        if self.fill is None and self.ambiguity is not AmbiguityResolution.UNAMBIGUOUS:
            raise ValueError(
                f"no exit filled, so there was nothing to resolve, but the resolution is "
                f"recorded as {self.ambiguity.value}"
            )


def _minutes_inside(bar: Candle, minute_bars: Sequence[Candle]) -> tuple[Candle, ...]:
    """The completed 1-minute bars covering ``bar``, or ``()`` if not all are there.

    Anything outside ``bar``'s own window is discarded before the count, so a
    caller may hand over a whole session without a later minute ever reaching
    the decision. Partial coverage is treated as no coverage: the missing
    minute is exactly the one that might have held the answer, and filling the
    hole with the surrounding minutes would be inventing the ordering rather
    than reading it.
    """
    expected = bar.interval.seconds // 60
    inside = sorted(
        (
            minute
            for minute in minute_bars
            if minute.interval is CandleInterval.M1
            and minute.status is CandleStatus.COMPLETED
            and bar.start_at <= minute.start_at
            and minute.end_at <= bar.end_at
        ),
        key=lambda minute: minute.start_at,
    )
    if len(inside) != expected:
        return ()
    for index, minute in enumerate(inside):
        if minute.start_at != bar.start_at + CandleInterval.M1.delta * index:
            return ()
    return tuple(inside)


def resolve_exit_fill(
    intent: ExecutionIntent,
    entry: Fill,
    bar: Candle,
    *,
    tick_size: Decimal,
    execution: ExecutionConfig,
    slippage: SlippageConfig,
    minute_bars: Sequence[Candle] = (),
    gaps: Sequence[DataGap] = (),
) -> ExitResolution:
    """The exit taken on ``bar``, resolving a same-bar stop/target collision.

    Returns whichever exit the bar produced. When it produced both, the
    minute bars decide the order if they are all present and usable, and the
    stop is assumed if they are not - never the target.

    ``minute_bars`` may hold any span; only those lying inside ``bar``'s own
    window are consulted, so a later minute cannot reach back and change an
    earlier bar's outcome.
    """
    stop = resolve_stop_fill(intent, bar, tick_size=tick_size, slippage=slippage, gaps=gaps)
    target = resolve_target_fill(
        intent, entry, bar, tick_size=tick_size, execution=execution, slippage=slippage, gaps=gaps
    )

    if stop is None and target is None:
        return ExitResolution(None)
    if target is None:
        return ExitResolution(stop)
    if stop is None:
        return ExitResolution(target)

    minutes = _minutes_inside(bar, minute_bars)
    for minute in minutes:
        try:
            hit_stop = (
                resolve_stop_fill(intent, minute, tick_size=tick_size, slippage=slippage, gaps=gaps)
                is not None
            )
            hit_target = (
                resolve_target_fill(
                    intent,
                    entry,
                    minute,
                    tick_size=tick_size,
                    execution=execution,
                    slippage=slippage,
                    gaps=gaps,
                )
                is not None
            )
        except UnexecutableBarError:
            # A minute that cannot answer is not evidence about the ordering.
            # Treat the window as unresolved rather than skipping the minute,
            # which would silently reorder the events around the hole.
            break

        if hit_stop and hit_target:
            # Ambiguous at this resolution too. The minute data was read but
            # did not decide, so the label names what actually did.
            break
        if hit_stop:
            return ExitResolution(stop, AmbiguityResolution.RESOLVED_BY_1M)
        if hit_target:
            return ExitResolution(target, AmbiguityResolution.RESOLVED_BY_1M)

    return ExitResolution(stop, AmbiguityResolution.PESSIMISTIC_FALLBACK)


def resolve_hard_exit_fill(
    intent: ExecutionIntent,
    bar: Candle,
    *,
    hard_exit_time: time,
    tick_size: Decimal,
    slippage: SlippageConfig,
    gaps: Sequence[DataGap] = (),
) -> Fill | None:
    """Flatten an open position at the cutoff, or ``None`` if it is not due yet.

    ``hard_exit_time`` is an **IST wall-clock time**, naive by design, following
    the same convention as ``OrbParams.hard_exit_time`` and the session window:
    IST for session logic, UTC for storage. It is passed in rather than read
    from the strategy's parameters, so this stays an execution rule that a
    parameter happens to configure rather than a dependency on one strategy.

    The exit is taken on the first bar whose **close** reaches the cutoff - the
    15:10-15:15 bar for a 15:15 deadline - and at that bar's closing price. That
    is the last price actually transacted at or before the deadline, and it is
    always available, whereas waiting for the next bar's opening print would
    need a bar that may not exist on the session's last one.

    Whether the position is still open is the caller's to know. A position
    closed earlier never reaches a bar at the cutoff, because the caller stops
    walking at its first exit.

    The decision comes entirely from ``bar``'s own timestamp. Nothing here reads
    a clock, so the same bars give the same answer whenever the run happens.
    """
    if bar.status is not CandleStatus.COMPLETED:
        raise ValueError(
            f"the bar at {bar.start_at.isoformat()} is {bar.status.value}; a hard exit fills "
            "at a settled closing price, and an in-progress bar has not got one"
        )
    if bar.start_at < intent.entry_bar_start:
        raise ValueError(
            f"the bar at {bar.start_at.isoformat()} precedes the entry bar "
            f"({intent.entry_bar_start.isoformat()}); there is no position to flatten yet"
        )
    if to_ist(bar.start_at).date() != to_ist(intent.entry_bar_start).date():
        raise ValueError(
            f"the bar at {bar.start_at.isoformat()} belongs to a later session than the entry "
            f"({intent.entry_bar_start.isoformat()}); the position should have been flattened "
            "at its own session's cutoff, so surviving into another one is a sequencing bug "
            "rather than an overnight hold this engine models"
        )
    if tick_size <= 0:
        raise ValueError(f"tick_size must be positive, got {tick_size}")

    if to_ist(bar.end_at).time() < hard_exit_time:
        return None

    _require_executable(bar, intent, gaps)

    adverse = slippage.adverse_ticks * tick_size
    reference = bar.close
    price = reference - adverse if intent.direction.is_long else reference + adverse

    return Fill(
        side=intent.exit_side,
        reason=FillReason.TIME_EXIT,
        quantity=intent.quantity,
        price=price,
        reference_price=reference,
        slippage_per_unit=adverse,
        costs=Decimal(0),
        occurred_at=bar.end_at,
        bar_start=bar.start_at,
    )
