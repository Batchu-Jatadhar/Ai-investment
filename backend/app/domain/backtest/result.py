"""The aggregate output of one backtest run.

One level above the value objects in ``models.py``, because it holds a
:class:`~app.domain.backtest.metrics.PerformanceReport` and the metrics that
produce one are computed *from* those value objects. Putting the aggregate here
keeps that dependency a line rather than a cycle.

Still a value object: it computes nothing itself, and the one place metrics are
attached to a run is :meth:`BacktestResult.measured`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.core.canonical import canonical_decimal
from app.domain.backtest.metrics import PerformanceReport, evaluate_performance
from app.domain.backtest.models import (
    EquityPoint,
    ExecutionStatus,
    RunManifest,
    SignalRecord,
    Trade,
)

__all__ = ["BacktestResult"]


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """The complete output of one backtest run.

    A value object that computes nothing itself. ``performance`` is measured by
    :func:`~app.domain.backtest.metrics.evaluate_performance` and attached by
    :meth:`measured`, which is the only place the two are wired together - so a
    result cannot carry metrics describing some other run's trades.

    ``performance`` is ``None`` when a run was never measured. That is not the
    same as a run with nothing in it: a zero-trade run still has an equity curve
    carrying its opening balance, and measuring it yields a real report full of
    honest ``None`` values. ``None`` here means *not measured*, and the two are
    kept apart because "we found nothing" and "we did not look" are different
    findings.
    """

    manifest: RunManifest
    signal_log: tuple[SignalRecord, ...] = ()
    trades: tuple[Trade, ...] = ()
    equity_curve: tuple[EquityPoint, ...] = ()
    performance: PerformanceReport | None = None

    def __post_init__(self) -> None:
        for name in ("signal_log", "trades", "equity_curve"):
            if not isinstance(getattr(self, name), tuple):
                raise TypeError(
                    f"{name} must be a tuple; a result is immutable so that it cannot be "
                    "edited after the run that produced it"
                )
        previous: datetime | None = None
        for point in self.equity_curve:
            if previous is not None and point.at < previous:
                raise ValueError(
                    f"equity_curve must be ordered in time; {point.at.isoformat()} follows "
                    f"{previous.isoformat()}"
                )
            previous = point.at
        if self.performance is not None:
            self._check_performance_describes_this_run()

    def _check_performance_describes_this_run(self) -> None:
        """Refuse metrics that were measured from something else.

        Cheap, and it catches the one mistake this shape invites: attaching a
        report built from a different trade list, which would make every headline
        figure in the result describe a run that never happened.
        """
        assert self.performance is not None
        measured = self.performance
        if measured.trades.trade_count != len(self.trades):
            raise ValueError(
                f"performance describes {measured.trades.trade_count} trade(s) but this result "
                f"holds {len(self.trades)}; the metrics were measured from a different run"
            )
        if measured.portfolio.equity_curve != self.equity_curve:
            raise ValueError(
                "performance was measured from a different equity curve than this result holds"
            )

    @classmethod
    def measured(
        cls,
        manifest: RunManifest,
        *,
        trades: Sequence[Trade] = (),
        equity_curve: Sequence[EquityPoint],
        signal_log: Sequence[SignalRecord] = (),
        quarantined_sessions: int = 0,
    ) -> BacktestResult:
        """Build a result and measure it in one step.

        The only wiring between a run's raw output and its metrics. Going
        through here is what guarantees the two describe each other.
        """
        trades = tuple(trades)
        equity_curve = tuple(equity_curve)
        return cls(
            manifest=manifest,
            signal_log=tuple(signal_log),
            trades=trades,
            equity_curve=equity_curve,
            performance=evaluate_performance(
                trades, equity_curve, quarantined_sessions=quarantined_sessions
            ),
        )

    def canonical(self) -> dict[str, str]:
        """Deterministic headline rendering, for a report or a stored summary.

        Follows the convention the configuration objects already use: sorted
        keys, decimals through :func:`canonical_decimal`, and an undefined metric
        rendered as an empty string rather than as ``0`` - the distinction this
        whole layer exists to preserve.

        Deliberately a summary and not a serialization of the run. The trades and
        the curve are the run; re-encoding them here would be a second
        representation of the same facts.
        """

        def render(value: Decimal | None) -> str:
            return canonical_decimal(value) if value is not None else ""

        def signals_with(status: ExecutionStatus) -> str:
            return str(sum(1 for r in self.signal_log if r.execution_status is status))

        summary = {
            "accepted_signal_count": str(sum(1 for r in self.signal_log if r.accepted)),
            "engine_version": self.manifest.engine_version,
            "executed_signal_count": signals_with(ExecutionStatus.FILLED),
            "no_execution_bar_signal_count": signals_with(ExecutionStatus.NO_EXECUTION_BAR),
            "input_fingerprint": self.manifest.input_fingerprint,
            "measured": str(self.performance is not None).lower(),
            "signal_count": str(len(self.signal_log)),
            "strategy_name": self.manifest.strategy_name,
            "strategy_version": self.manifest.strategy_version,
            "trade_count": str(len(self.trades)),
        }
        if self.performance is not None:
            portfolio = self.performance.portfolio
            summary.update(
                {
                    "ambiguous_exits": str(self.performance.ambiguous_exit_count),
                    "expectancy_inr": render(self.performance.trades.expectancy_inr),
                    "gross_pnl": canonical_decimal(portfolio.gross_pnl),
                    "max_drawdown": canonical_decimal(portfolio.max_drawdown),
                    "net_pnl": canonical_decimal(portfolio.net_pnl),
                    "pessimistic_fallbacks": str(self.performance.pessimistic_fallback_count),
                    "profit_factor": render(self.performance.trades.profit_factor),
                    "quarantined_sessions": str(self.performance.quarantined_sessions),
                    "sharpe_per_trade": render(self.performance.risk.sharpe_per_trade),
                    "t_statistic": render(self.performance.statistics.t_statistic),
                    "total_costs": canonical_decimal(portfolio.total_costs),
                    "total_return": render(portfolio.total_return),
                    "win_rate": render(self.performance.trades.win_rate),
                }
            )
        return dict(sorted(summary.items()))
