"""The backtest engine: sequencing, and nothing else.

Every decision in a run already has an owner. The strategy decides whether a
bar signals, the execution resolvers decide whether and at what price a bar
fills, the portfolio moves cash and positions, and the metrics measure the
result. This module decides only **in what order they are asked**, and hands
each of them exactly what it may see at that moment.

.. rubric:: The order, per 5-minute bar

1.  **Enter** a pending signal at this bar's open - but only if this bar is the
    exact next bucket after the signal bar. If that bucket is missing from the
    data, the entry resolver is told there is no execution bar and reports
    ``NO_EXECUTION_BAR``; a later bar is never substituted, because its open is
    a price the signal could not have been acted on at.
2.  **Exit** on this bar - the stop or the target, same-bar collisions resolved
    from this bar's own 1-minute bars - and, failing both, the hard exit.
3.  **Decide** on this bar's close. The strategy is handed the session prefix
    ending with this bar and nothing later. A signal that fires while flat
    becomes the intent step 1 acts on at the next bar.

Every signal is written to the signal log, including one that fires while a
position is already held or on the session's last bar and so is never traded.
Each entry attempt stamps its outcome on the record's ``execution_status``
(``FILLED`` or ``NO_EXECUTION_BAR``); a signal that arrived while a position was
held keeps ``None``. The log records what the strategy said and what became of
it; the trades record only what actually executed.

.. rubric:: What is deliberately not here

No price, slippage or charge arithmetic, no sizing formula, no P&L and no
metric. No clock: ``generated_at`` is supplied by the caller. No randomness. All
run state lives in local variables of :func:`run_backtest`, so two calls cannot
share anything.

.. rubric:: Sessions

Before a session's bars are replayed, its opening window must be fully covered.
A session without one - a special session that starts later in the day, or
minutes missing from the open - cannot form an opening range, so it is recorded
as ``UNTRADABLE_NO_OPENING_RANGE`` and skipped: the strategy is never asked, no
signal can exist and no bar is filled in. Later sessions are replayed normally.
Every calendar trading day of the run gets exactly one :class:`SessionRecord`,
including ``NO_DATA`` for a trading day with no bars at all.

Data that cannot establish a fill raises :class:`UnexecutableBarError` out of
the run. Session quarantine is not implemented yet, so ``quarantined_sessions``
is reported as zero. ``BacktestInput`` carries no recorded feed gaps either, so
the resolvers' ``INSIDE_DATA_GAP`` check is never exercised by a run; both
arrive with historical data.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import groupby

from app.core.time import to_ist
from app.domain.backtest.execution import (
    ExecutionIntent,
    resolve_entry_fill,
    resolve_exit_fill,
    resolve_hard_exit_fill,
)
from app.domain.backtest.input import BacktestInput
from app.domain.backtest.models import (
    Fill,
    RunManifest,
    SessionRecord,
    SessionStatus,
    SignalRecord,
)
from app.domain.backtest.portfolio import Portfolio
from app.domain.backtest.result import BacktestResult
from app.domain.indicators import IndicatorError, opening_range
from app.domain.market.models import Candle
from app.domain.strategy.contract import Signal, Strategy, StrategyContext
from app.domain.strategy.orb import OrbStrategy

__all__ = ["ENGINE_VERSION", "SignalGate", "run_backtest"]

#: Recorded in every manifest. Bump when the sequencing changes.
ENGINE_VERSION = "2.7.0"

#: Decides whether a strategy signal may be traded. Handed the signal, the session
#: prefix that produced it and the context, it returns that signal's record. It may
#: only subtract: it cannot create a signal, and one that returns a record for any
#: other signal, or one already carrying an execution status, raises.
SignalGate = Callable[[Signal, Sequence[Candle], StrategyContext], SignalRecord]


def _by_session(candles: Sequence[Candle]) -> dict[date, tuple[Candle, ...]]:
    return {
        day: tuple(bars) for day, bars in groupby(candles, key=lambda c: to_ist(c.start_at).date())
    }


def _has_opening_range(bars: Sequence[Candle], day: date, data: BacktestInput) -> bool:
    """Whether ``day``'s bars fully cover its opening window.

    Asks the same :func:`opening_range` the strategy uses, so "untradable" means
    exactly "the strategy could not have formed a range", with no second rule.
    """
    try:
        opening_range(
            bars,
            day,
            data.calendar,
            opening_range_minutes=data.strategy_params.opening_range_minutes,
        )
    except IndicatorError:
        return False
    return True


def run_backtest(
    backtest_input: BacktestInput,
    *,
    starting_capital: Decimal,
    generated_at: datetime,
    strategy: Strategy | None = None,
    signal_gate: SignalGate | None = None,
) -> BacktestResult:
    """Run ``backtest_input`` session by session, bar by bar, and measure it.

    Without ``signal_gate`` every signal is accepted by the strategy, which is the
    strategy-only path. With one, rejected signals are logged and never entered.
    """
    data = backtest_input
    active: Strategy | OrbStrategy = (
        strategy if strategy is not None else OrbStrategy(data.strategy_params)
    )
    tick_size = data.instrument.tick_size
    costs = data.cost_schedule
    slippage = data.slippage_config
    interval = data.strategy_params.signal_interval.delta

    sessions_5m = _by_session(data.candles_5m)
    sessions_1m = _by_session(data.candles_1m)

    portfolio = Portfolio.funded(
        starting_capital,
        started_at=data.candles_5m[0].start_at,
        fixed_notional=data.strategy_params.fixed_notional_inr,
    )
    signal_log: list[SignalRecord] = []
    session_log: list[SessionRecord] = []

    for day, bars in sessions_5m.items():
        bounds = data.calendar.session_bounds(bars[0].start_at)
        if bounds is None:
            raise ValueError(f"{day} has signal bars but is not a trading day on the calendar")
        if not _has_opening_range(bars, day, data):
            session_log.append(SessionRecord(day, SessionStatus.UNTRADABLE_NO_OPENING_RANGE))
            continue
        signals_before, trades_before = len(signal_log), len(portfolio.trades)
        context = StrategyContext(
            instrument=data.instrument,
            calendar=data.calendar,
            session_open=bounds[0],
            session_close=bounds[1],
            prior_atr=data.prior_atr(day),
            round_trip_friction=data.round_trip_friction,
        )
        minutes = sessions_1m.get(day, ())

        #: Index into ``signal_log`` of the signal awaiting its entry bar.
        pending: int | None = None
        held: tuple[ExecutionIntent, Fill] | None = None

        for index, bar in enumerate(bars):
            if pending is not None:
                signal = signal_log[pending].signal
                entry_bar_start = signal.signal_bar_start + interval
                next_bar = bar if bar.start_at == entry_bar_start else None
                # Sized from the price the entry actually fills at, which is not
                # known until the bar opens: probe the fill for one share, size
                # from its price, then fill the real quantity.
                probe = resolve_entry_fill(
                    ExecutionIntent(signal, 1, entry_bar_start),
                    next_bar,
                    tick_size=tick_size,
                    slippage=slippage,
                )
                signal_log[pending] = replace(signal_log[pending], execution_status=probe.status)
                if probe.fill is not None:
                    quantity = portfolio.size_for(
                        probe.fill.price, lot_size=data.instrument.lot_size
                    )
                    intent = ExecutionIntent(signal, quantity, entry_bar_start)
                    entry = resolve_entry_fill(
                        intent,
                        next_bar,
                        tick_size=tick_size,
                        slippage=slippage,
                        cost_schedule=costs,
                    ).fill
                    assert entry is not None
                    portfolio = portfolio.enter(intent, entry)
                    held = (intent, entry)
                pending = None

            if held is not None:
                intent, entry = held
                resolution = resolve_exit_fill(
                    intent,
                    entry,
                    bar,
                    tick_size=tick_size,
                    execution=data.execution_config,
                    slippage=slippage,
                    minute_bars=tuple(
                        m for m in minutes if bar.start_at <= m.start_at < bar.end_at
                    ),
                    cost_schedule=costs,
                )
                exit_fill = resolution.fill or resolve_hard_exit_fill(
                    intent,
                    bar,
                    hard_exit_time=data.strategy_params.hard_exit_time,
                    tick_size=tick_size,
                    slippage=slippage,
                    cost_schedule=costs,
                )
                if exit_fill is not None:
                    portfolio = portfolio.close(exit_fill, ambiguity=resolution.ambiguity)
                    held = None

            seen = bars[: index + 1]
            decided = active.on_bar(seen, context)
            if decided is None:
                continue
            if signal_gate is None:
                record = SignalRecord(signal=decided, accepted=True, decision_reason=decided.reason)
            else:
                record = signal_gate(decided, seen, context)
                if record.signal != decided or record.execution_status is not None:
                    raise ValueError(
                        "a signal gate may only accept or reject the signal it was handed; it "
                        "returned a record for a different signal or one already executed"
                    )
            signal_log.append(record)
            if held is None and record.accepted:
                pending = len(signal_log) - 1  # entered at the next bar's open

        if pending is not None:
            # Signalled on the session's last bar: there is no next bar.
            record = signal_log[pending]
            outcome = resolve_entry_fill(
                ExecutionIntent(record.signal, 1, record.signal.signal_bar_start + interval),
                None,
                tick_size=tick_size,
                slippage=slippage,
            )
            signal_log[pending] = replace(record, execution_status=outcome.status)

        if held is not None:
            raise ValueError(
                f"a position opened on {day} was still held when its bars ran out; the session "
                "has no bar at the hard exit, so the trade cannot be closed honestly"
            )

        signals, trades = len(signal_log) - signals_before, len(portfolio.trades) - trades_before
        status = (
            SessionStatus.TRADED
            if trades
            else SessionStatus.SIGNALLED
            if signals
            else SessionStatus.NO_SIGNAL
        )
        session_log.append(SessionRecord(day, status, signal_count=signals, trade_count=trades))

    if sessions_5m:
        first, last = min(sessions_5m), max(sessions_5m)
        span = (first + timedelta(days=offset) for offset in range((last - first).days + 1))
        session_log.extend(
            SessionRecord(day, SessionStatus.NO_DATA)
            for day in span
            if day not in sessions_5m and data.calendar.is_trading_day(day)
        )
        session_log.sort(key=lambda record: record.session)

    manifest = RunManifest(
        input_fingerprint=data.fingerprint(),
        strategy_name=active.name,
        strategy_version=active.version,
        engine_version=ENGINE_VERSION,
        generated_at=generated_at,
    )
    return BacktestResult.measured(
        manifest,
        trades=portfolio.trades,
        equity_curve=portfolio.equity_curve,
        signal_log=signal_log,
        session_log=session_log,
    )
