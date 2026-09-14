"""A paper trading session: strategy -> risk -> AI filter -> paper execution, one bar at a time.

This composes existing pieces and decides nothing itself. On each completed bar:

1.  the paper adapter resolves fills for the working order or held position;
2.  the strategy evaluates the session so far and records why it did or did not signal;
3.  a signal, when nothing is held or working, is sized by the risk layer against
    current cash, using the signal bar's close as the reference entry (the real
    entry is the next bar's open, known only at the fill);
4.  ``decide_entry`` applies the optional AI filter after risk;
5.  a taken entry is submitted to the paper adapter.

The latest :class:`Evaluation` is kept for the dashboard read model. Nothing here
reads a clock or the network; bars are fed in by whoever runs the session.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from functools import partial

from app.adapters.paper import PaperExecutionAdapter
from app.config.settings import TradingMode
from app.core.time import to_ist
from app.domain.backtest.config import CostSchedule, ExecutionConfig, SlippageConfig
from app.domain.backtest.costs import estimate_round_trip_friction
from app.domain.backtest.models import Fill
from app.domain.execution.ports import OrderRecord
from app.domain.market.models import Candle, Instrument
from app.domain.market.session import MarketSessionCalendar
from app.domain.risk.sizing import RiskConfig, RiskDecision, size_position
from app.domain.strategy.contract import Signal, StrategyContext
from app.domain.strategy.orb import OrbReason, OrbStrategy
from app.domain.strategy.params import OrbParams
from app.services.ai_filter import AiAnalyst, EntryDecision, decide_entry
from app.services.execution import build_execution_port

__all__ = [
    "Evaluation",
    "PaperSession",
    "get_active_paper_session",
    "set_active_paper_session",
]


@dataclass(frozen=True, slots=True)
class Evaluation:
    """What the pipeline concluded on one bar, and why."""

    bar: Candle
    strategy_reason: OrbReason
    fills: tuple[Fill, ...] = ()
    signal: Signal | None = None
    #: Set when a signal was not sized because a position or order was already open.
    skipped_because: str | None = None
    risk: RiskDecision | None = None
    entry: EntryDecision | None = None
    order: OrderRecord | None = None


class PaperSession:
    def __init__(
        self,
        *,
        instrument: Instrument,
        starting_capital: Decimal,
        risk_config: RiskConfig,
        cost_schedule: CostSchedule,
        params: OrbParams | None = None,
        calendar: MarketSessionCalendar | None = None,
        execution: ExecutionConfig | None = None,
        slippage: SlippageConfig | None = None,
        analyst: AiAnalyst | None = None,
    ) -> None:
        self.instrument = instrument
        self.params = params or OrbParams()
        self.calendar = calendar or MarketSessionCalendar.nse_equity()
        self.risk_config = risk_config
        self.analyst = analyst
        self.strategy = OrbStrategy(self.params)
        port = build_execution_port(
            TradingMode.PAPER,
            instrument=instrument,
            starting_capital=starting_capital,
            signal_interval=self.params.signal_interval,
            hard_exit_time=self.params.hard_exit_time,
            execution=execution or ExecutionConfig(),
            slippage=slippage or SlippageConfig(),
            cost_schedule=cost_schedule,
        )
        assert isinstance(port, PaperExecutionAdapter)
        self.paper = port
        self._round_trip_friction = partial(
            estimate_round_trip_friction,
            schedule=cost_schedule,
            notional=self.params.fixed_notional_inr,
            lot_size=instrument.lot_size,
            tick_size=instrument.tick_size,
            adverse_ticks=(slippage or SlippageConfig()).adverse_ticks,
        )
        self.last: Evaluation | None = None
        self._session_day: date | None = None
        self._session_bars: list[Candle] = []

    def on_bar(
        self, bar: Candle, *, prior_atr: Decimal | None, minute_bars: Sequence[Candle] = ()
    ) -> Evaluation:
        """``minute_bars`` resolve a same-bar stop/target collision, as in the backtest."""
        fills = self.paper.on_bar(bar, minute_bars)

        day = to_ist(bar.start_at).date()
        if day != self._session_day:
            self._session_day, self._session_bars = day, []
        self._session_bars.append(bar)
        bars = tuple(self._session_bars)

        bounds = self.calendar.session_bounds(bar.start_at)
        if bounds is None:
            raise ValueError(f"{day} is not a trading day, so no bar can arrive for it")
        context = StrategyContext(
            instrument=self.instrument,
            calendar=self.calendar,
            session_open=bounds[0],
            session_close=bounds[1],
            prior_atr=prior_atr,
            round_trip_friction=self._round_trip_friction,
        )
        decision = self.strategy.evaluate(bars, context)
        evaluation = Evaluation(
            bar=bar, strategy_reason=decision.reason, fills=fills, signal=decision.signal
        )

        signal = decision.signal
        if signal is not None:
            if self.paper.position is not None or self.paper.working_order is not None:
                evaluation = replace(evaluation, skipped_because="position_or_order_open")
            else:
                risk = size_position(
                    self.risk_config,
                    equity=self.paper.cash,
                    available_cash=self.paper.cash,
                    direction=signal.direction,
                    entry_price=bar.close,
                    stop_price=signal.stop_price,
                    instrument=self.instrument,
                )
                entry = decide_entry(
                    signal, risk, session_bars=bars, prior_atr=prior_atr, analyst=self.analyst
                )
                order = None
                if entry.take:
                    order_id = f"{signal.instrument_token}:{signal.signal_bar_start.isoformat()}"
                    order = self.paper.submit(order_id, signal, risk)
                evaluation = replace(evaluation, risk=risk, entry=entry, order=order)

        self.last = evaluation
        return evaluation


_active: PaperSession | None = None


def set_active_paper_session(session: PaperSession | None) -> None:
    """Register the paper session running in *this* process, for the dashboard."""
    global _active
    _active = session


def get_active_paper_session() -> PaperSession | None:
    return _active
