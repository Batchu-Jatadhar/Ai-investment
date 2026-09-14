"""Replay stored historical candles through a PAPER session, one completed bar at a time.

The runner adds no trading logic. It:

1.  refuses any trading mode but PAPER, before touching the repository;
2.  loads the range with :func:`load_backtest_input`, which reads through the
    repository port, validates order, completeness and provenance, and applies
    the warmup/ATR rules - so missing warmup, empty ranges, invalid ranges and
    non-chronological data all fail there with :class:`HistoricalDataError`;
3.  builds a :class:`PaperSession` (strategy -> risk -> AI filter -> paper
    execution) and, by default, registers it as the active session the
    dashboard reads;
4.  feeds each trading session's signal bars in order, with the session's prior
    ATR from :meth:`BacktestInput.prior_atr` and the bar's own minute bars.

.. rubric:: Gaps and session ends

Missing signal bars are reported in :attr:`PaperReplayResult.missing_bars` and
never filled in. An entry whose next bar is missing expires in the paper adapter
instead of filling on a later bar, exactly as in the backtest.

At the end of each session's data, a still-working entry order is cancelled - it
has no bar left to execute on - and recorded. A position still open raises
:class:`PaperReplayError`: with no bar at the hard exit there is no price to
close it at, and inventing one is not an option. The backtest engine refuses the
same case.

No clock, no broker, no network: prices come only from stored completed bars.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from itertools import groupby

from app.config.settings import TradingMode
from app.core.time import to_ist
from app.domain.backtest.config import CostSchedule, ExecutionConfig, SlippageConfig
from app.domain.market.models import Candle, Instrument
from app.domain.market.ports import MarketDataRepository
from app.domain.market.session import MarketSessionCalendar
from app.domain.risk.sizing import RiskConfig
from app.domain.strategy.params import OrbParams
from app.services.ai_filter import AiAnalyst
from app.services.backtest_data import DEFAULT_PAGE_SIZE, load_backtest_input
from app.services.paper_session import Evaluation, PaperSession, set_active_paper_session

__all__ = ["PaperReplayError", "PaperReplayModeError", "PaperReplayResult", "replay_paper_session"]


class PaperReplayError(ValueError):
    """The stored data cannot be replayed honestly."""


class PaperReplayModeError(RuntimeError):
    """A replay was requested for a mode other than PAPER. Nothing was read or run."""


@dataclass(frozen=True, slots=True)
class PaperReplayResult:
    session: PaperSession
    evaluations: tuple[Evaluation, ...]
    trading_sessions: tuple[date, ...]
    #: Signal-bar starts absent between a session's open and its last stored bar.
    missing_bars: tuple[datetime, ...]
    #: Calendar trading days in the window with no stored bars at all.
    sessions_without_data: tuple[date, ...]
    #: Entry orders cancelled because their session's data ended before their bar.
    cancelled_at_session_end: tuple[str, ...]


def replay_paper_session(
    repository: MarketDataRepository,
    instrument: Instrument,
    *,
    trading_mode: TradingMode,
    warmup_start: datetime,
    start: datetime,
    end: datetime,
    starting_capital: Decimal,
    risk_config: RiskConfig,
    cost_schedule: CostSchedule,
    params: OrbParams | None = None,
    slippage: SlippageConfig | None = None,
    execution: ExecutionConfig | None = None,
    calendar: MarketSessionCalendar | None = None,
    analyst: AiAnalyst | None = None,
    register: bool = True,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> PaperReplayResult:
    """Replay ``[start, end)`` (IST midnights) after a ``[warmup_start, start)`` warmup."""
    if trading_mode is not TradingMode.PAPER:
        raise PaperReplayModeError(
            f"paper replay runs in PAPER mode only, not {trading_mode.value.upper()}; "
            "no live or broker execution path exists"
        )

    params = params or OrbParams()
    slippage = slippage or SlippageConfig()
    execution = execution or ExecutionConfig()
    calendar = calendar or MarketSessionCalendar.nse_equity()
    loaded = load_backtest_input(
        repository,
        instrument,
        warmup_start=warmup_start,
        start=start,
        end=end,
        strategy_params=params,
        cost_schedule=cost_schedule,
        slippage_config=slippage,
        execution_config=execution,
        calendar=calendar,
        page_size=page_size,
    )
    data = loaded.backtest_input

    session = PaperSession(
        instrument=instrument,
        starting_capital=starting_capital,
        risk_config=risk_config,
        cost_schedule=cost_schedule,
        params=params,
        calendar=calendar,
        execution=execution,
        slippage=slippage,
        analyst=analyst,
    )
    if register:
        set_active_paper_session(session)

    interval = params.signal_interval.delta
    by_day = _by_day(data.candles_5m)
    minutes_by_day = _by_day(data.candles_1m)
    evaluations: list[Evaluation] = []
    missing: list[datetime] = []
    cancelled: list[str] = []

    for day in loaded.trading_sessions:
        bars = by_day[day]
        bounds = calendar.session_bounds(bars[0].start_at)
        if bounds is None:
            raise PaperReplayError(f"{day} has stored bars but is not a trading day")
        expected = bounds[0]
        prior_atr = data.prior_atr(day)
        minutes = minutes_by_day.get(day, ())

        for bar in bars:
            if bar.start_at < expected:
                raise PaperReplayError(
                    f"bar at {bar.start_at.isoformat()} is out of order or off the "
                    f"{params.signal_interval.value} grid"
                )
            while expected < bar.start_at:
                missing.append(expected)
                expected += interval
            expected = bar.start_at + interval
            evaluations.append(
                session.on_bar(
                    bar,
                    prior_atr=prior_atr,
                    minute_bars=tuple(
                        m for m in minutes if bar.start_at <= m.start_at < bar.end_at
                    ),
                )
            )

        working = session.paper.working_order
        if working is not None:
            session.paper.cancel(working.client_order_id)
            cancelled.append(working.client_order_id)
        if session.paper.position is not None:
            raise PaperReplayError(
                f"a paper position was still open when {day}'s stored bars ran out before the "
                "hard exit; there is no bar to close it at, so the replay stops rather than "
                "invent a price"
            )

    return PaperReplayResult(
        session=session,
        evaluations=tuple(evaluations),
        trading_sessions=loaded.trading_sessions,
        missing_bars=tuple(missing),
        sessions_without_data=loaded.sessions_without_data,
        cancelled_at_session_end=tuple(cancelled),
    )


def _by_day(candles: Sequence[Candle]) -> dict[date, tuple[Candle, ...]]:
    return {
        day: tuple(group)
        for day, group in groupby(candles, key=lambda c: to_ist(c.start_at).date())
    }
