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
The log records what the strategy said; the trades record what execution did.

.. rubric:: What is deliberately not here

No price, slippage or charge arithmetic, no sizing formula, no P&L and no
metric. No clock: ``generated_at`` is supplied by the caller. No randomness. All
run state lives in local variables of :func:`run_backtest`, so two calls cannot
share anything.

Data that cannot establish a fill raises :class:`UnexecutableBarError` out of
the run. Session quarantine is not implemented yet, so ``quarantined_sessions``
is reported as zero.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
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
from app.domain.backtest.models import Fill, RunManifest, SignalRecord
from app.domain.backtest.portfolio import Portfolio
from app.domain.backtest.result import BacktestResult
from app.domain.market.models import Candle
from app.domain.strategy.contract import Signal, Strategy, StrategyContext
from app.domain.strategy.orb import OrbStrategy

__all__ = ["ENGINE_VERSION", "run_backtest"]

#: Recorded in every manifest. Bump when the sequencing changes.
ENGINE_VERSION = "2.6.3"


def _by_session(candles: Sequence[Candle]) -> dict[date, tuple[Candle, ...]]:
    return {
        day: tuple(bars) for day, bars in groupby(candles, key=lambda c: to_ist(c.start_at).date())
    }


def run_backtest(
    backtest_input: BacktestInput,
    *,
    starting_capital: Decimal,
    generated_at: datetime,
    strategy: Strategy | None = None,
) -> BacktestResult:
    """Run ``backtest_input`` session by session, bar by bar, and measure it."""
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

    for day, bars in sessions_5m.items():
        bounds = data.calendar.session_bounds(bars[0].start_at)
        if bounds is None:
            raise ValueError(f"{day} has signal bars but is not a trading day on the calendar")
        context = StrategyContext(
            instrument=data.instrument,
            calendar=data.calendar,
            session_open=bounds[0],
            session_close=bounds[1],
            prior_atr=data.prior_atr(day),
        )
        minutes = sessions_1m.get(day, ())

        pending: Signal | None = None
        held: tuple[ExecutionIntent, Fill] | None = None

        for index, bar in enumerate(bars):
            if pending is not None:
                entry_bar_start = pending.signal_bar_start + interval
                next_bar = bar if bar.start_at == entry_bar_start else None
                # Sized from the price the entry actually fills at, which is not
                # known until the bar opens: probe the fill for one share, size
                # from its price, then fill the real quantity.
                probe = resolve_entry_fill(
                    ExecutionIntent(pending, 1, entry_bar_start),
                    next_bar,
                    tick_size=tick_size,
                    slippage=slippage,
                )
                if probe.fill is not None:
                    quantity = portfolio.size_for(
                        probe.fill.price, lot_size=data.instrument.lot_size
                    )
                    intent = ExecutionIntent(pending, quantity, entry_bar_start)
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

            signal = active.on_bar(bars[: index + 1], context)
            if signal is None:
                continue
            signal_log.append(
                SignalRecord(signal=signal, accepted=True, decision_reason=signal.reason)
            )
            if held is None:
                # Entered at the next bar's open. A signal on the session's last
                # bar has no next bar, and ``pending`` dies with the session.
                pending = signal

        if held is not None:
            raise ValueError(
                f"a position opened on {day} was still held when its bars ran out; the session "
                "has no bar at the hard exit, so the trade cannot be closed honestly"
            )

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
    )
