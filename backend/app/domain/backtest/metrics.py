"""Performance metrics, computed from closed trades and the equity curve.

Pure arithmetic over two inputs and nothing else. No candles, no strategy, no
execution rules, no broker, no AI. Given a trade log and a curve it can say what
a run did; it cannot say how the run happened, and it does not need to.

.. rubric:: Undefined is not zero

Most of these metrics are ratios, and a ratio with an empty denominator has no
value. A run with no losing trades has no profit factor; a run with identical
returns has no Sharpe; a run that never drew down has no return-over-drawdown.
Every such field is ``None`` rather than ``0``, because zero is an answer and
these have none. Reporting ``0.0`` for a Sharpe that could not be computed is
how a strategy with two trades ends up looking merely mediocre rather than
unmeasured.

.. rubric:: The return series, stated exactly

The equity curve samples **only when the book is flat** - one point at the start
and one per closed trade. So the return series is **per trade, not per calendar
period**: return *i* is the change in equity across trade *i* as a fraction of
the equity before it.

*   **Frequency: per trade.** Not daily, not hourly. Trades are irregularly
    spaced in time and the series carries no notion of duration.
*   **Annualization: none.** ``sharpe_per_trade`` is named for its convention so
    it cannot be misread as annual. Annualizing would mean multiplying by the
    square root of trades per year, which assumes the sample's trade frequency
    continues - an extrapolation from a few dozen observations that this module
    refuses to make silently. A caller who wants an annual figure should do it
    deliberately, knowing what they are assuming.
*   **Risk-free rate: zero.** A position held for minutes accrues no meaningful
    risk-free return, so subtracting one would be noise dressed as rigour.
*   **Flat trades: kept.** A scratch trade is a real observation with a return
    of zero. It lowers the mean and the variance, and dropping it would flatter
    the result.

.. rubric:: What the drawdown is, and is not

``max_drawdown`` is measured on that same curve, so it is a **realized,
trade-to-trade** drawdown. It is **not** intra-trade mark-to-market drawdown:
a trade that ran 3R against the position before closing at +2R contributes
nothing to it, because the curve never saw the excursion.

That is a real limitation and it makes this number optimistic against a
tick-level equity curve. It is inherited deliberately rather than papered over -
measuring the excursion needs a valuation rule for open positions, which this
project has not chosen, and inventing one inside a metrics module would be the
worst possible place to make that decision.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from app.domain.backtest.models import AmbiguityResolution, EquityPoint, Trade
from app.domain.strategy.contract import SignalDirection

__all__ = [
    "PerformanceReport",
    "PortfolioMetrics",
    "RiskMetrics",
    "StatisticalMetrics",
    "TradeMetrics",
    "evaluate_performance",
]

#: Ratios and roots run here rather than in whatever context the caller has
#: installed, so an unrelated module cannot change a reported figure.
_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)

#: Normal-approximation multiplier for a two-sided 95% interval. See
#: :class:`StatisticalMetrics` for why this is not a t critical value.
_Z_95 = Decimal("1.96")

_ZERO = Decimal(0)


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    with localcontext(_CONTEXT):
        return sum(values, _ZERO) / Decimal(len(values))


def _sample_stdev(values: Sequence[Decimal]) -> Decimal | None:
    """Sample standard deviation, or ``None`` for fewer than two observations.

    One observation has no spread to measure - not a spread of zero.
    """
    if len(values) < 2:
        return None
    average = _mean(values)
    assert average is not None
    with localcontext(_CONTEXT):
        squares = sum(((value - average) ** 2 for value in values), _ZERO)
        return (squares / Decimal(len(values) - 1)).sqrt()


def _divide(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    """Divide, or ``None`` when the denominator is zero."""
    if denominator == 0:
        return None
    with localcontext(_CONTEXT):
        return numerator / denominator


@dataclass(frozen=True, slots=True)
class TradeMetrics:
    """What the individual trades did.

    ``average_loss`` and ``largest_loss`` are reported **signed**, so a loss is
    negative. Reporting magnitudes reads more tidily and invites exactly one
    mistake: adding them to the wins.

    ``expectancy_r`` is ``None`` whenever no trade carries an ``r_multiple``,
    which is currently every trade - the project has not yet decided whether R
    is measured on the gross or the net move, and this module will not decide it
    by averaging whatever happens to be there.
    """

    trade_count: int
    win_count: int
    loss_count: int
    scratch_count: int
    win_rate: Decimal | None
    average_win: Decimal | None
    average_loss: Decimal | None
    expectancy_inr: Decimal | None
    expectancy_r: Decimal | None
    profit_factor: Decimal | None
    largest_win: Decimal | None
    largest_loss: Decimal | None
    average_holding_time: timedelta | None
    long_count: int
    short_count: int
    long_net_pnl: Decimal
    short_net_pnl: Decimal


@dataclass(frozen=True, slots=True)
class PortfolioMetrics:
    """What the account did.

    ``max_drawdown`` is realized and trade-to-trade - see the module docstring
    for what that excludes. ``max_drawdown_duration`` runs from the peak to the
    moment equity recovered it, or to the end of the curve when it never did,
    which ``max_drawdown_recovered`` distinguishes.

    ``exposure`` divides time held by the **calendar** span of the run, so an
    intraday strategy will read low: the denominator includes every night and
    weekend the market was shut. ``time_in_market`` is the unambiguous figure.
    """

    gross_pnl: Decimal
    total_costs: Decimal
    net_pnl: Decimal
    starting_capital: Decimal
    ending_equity: Decimal
    total_return: Decimal | None
    max_drawdown: Decimal
    max_drawdown_pct: Decimal | None
    max_drawdown_duration: timedelta | None
    max_drawdown_recovered: bool
    time_in_market: timedelta
    exposure: Decimal | None
    trades_per_active_day: Decimal | None
    active_days: int
    equity_curve: tuple[EquityPoint, ...]


@dataclass(frozen=True, slots=True)
class RiskMetrics:
    """Return per unit of risk, per trade.

    Every field here is ``None`` when it cannot be computed: fewer than two
    trades leaves no spread to divide by, identical returns leave a zero one,
    and a run with no losing trade has no downside deviation.
    """

    mean_return: Decimal | None
    return_stdev: Decimal | None
    downside_deviation: Decimal | None
    sharpe_per_trade: Decimal | None
    sortino_per_trade: Decimal | None
    return_over_max_drawdown: Decimal | None


@dataclass(frozen=True, slots=True)
class StatisticalMetrics:
    """Is the average trade distinguishable from zero?

    The interval uses the **normal approximation** (1.96), not a t critical
    value, because a t table is not worth embedding and interpolating badly
    would be worse than being explicit. For fewer than about thirty trades the
    true interval is **wider** than the one reported here - at ten trades the
    critical value is 2.26 rather than 1.96, some fifteen percent - so a
    marginal result on a small sample should be read as not significant.

    ``t_statistic`` is reported alongside precisely so a reader can compare it
    against a real table for their own degrees of freedom, which is
    ``trade_count - 1``.
    """

    sample_size: int
    mean_trade_pnl: Decimal | None
    trade_pnl_stdev: Decimal | None
    standard_error: Decimal | None
    confidence_interval_95: tuple[Decimal, Decimal] | None
    t_statistic: Decimal | None
    degrees_of_freedom: int | None


@dataclass(frozen=True, slots=True)
class PerformanceReport:
    """Everything measurable about one run, and how trustworthy it is.

    ``ambiguous_exit_count`` and ``pessimistic_fallback_count`` are data-quality
    figures rather than performance ones, and they belong beside the returns:
    a run whose exits were mostly decided by the pessimistic assumption is
    weaker evidence than one resolved from real 1-minute data, however good its
    Sharpe looks. ``quarantined_sessions`` is supplied by the engine, which is
    the only layer that knows a session was skipped.
    """

    trades: TradeMetrics
    portfolio: PortfolioMetrics
    risk: RiskMetrics
    statistics: StatisticalMetrics
    ambiguous_exit_count: int
    pessimistic_fallback_count: int
    quarantined_sessions: int


def _trade_metrics(trades: Sequence[Trade]) -> TradeMetrics:
    nets = [trade.net_pnl for trade in trades]
    wins = [net for net in nets if net > 0]
    losses = [net for net in nets if net < 0]
    r_multiples = [t.r_multiple for t in trades if t.r_multiple is not None]

    gross_profit = sum(wins, _ZERO)
    gross_loss = -sum(losses, _ZERO)

    longs = [t for t in trades if t.direction is SignalDirection.LONG]
    shorts = [t for t in trades if t.direction is SignalDirection.SHORT]

    holding = [t.exit.occurred_at - t.entry.occurred_at for t in trades]
    average_holding = sum(holding, timedelta()) / len(holding) if holding else None

    return TradeMetrics(
        trade_count=len(trades),
        win_count=len(wins),
        loss_count=len(losses),
        scratch_count=len(nets) - len(wins) - len(losses),
        win_rate=_divide(Decimal(len(wins)), Decimal(len(nets))),
        average_win=_mean(wins),
        average_loss=_mean(losses),
        expectancy_inr=_mean(nets),
        expectancy_r=_mean(r_multiples),
        profit_factor=_divide(gross_profit, gross_loss),
        largest_win=max(wins) if wins else None,
        largest_loss=min(losses) if losses else None,
        average_holding_time=average_holding,
        long_count=len(longs),
        short_count=len(shorts),
        long_net_pnl=sum((t.net_pnl for t in longs), _ZERO),
        short_net_pnl=sum((t.net_pnl for t in shorts), _ZERO),
    )


def _drawdown(
    curve: Sequence[EquityPoint],
) -> tuple[Decimal, Decimal | None, timedelta | None, bool]:
    """Largest realized peak-to-trough fall, and how long it lasted."""
    if not curve:
        return _ZERO, None, None, True

    peak = curve[0].equity
    peak_at = curve[0].at
    worst = _ZERO
    worst_peak = peak
    worst_peak_at = peak_at
    worst_trough_index = 0

    for index, point in enumerate(curve):
        if point.equity > peak:
            peak = point.equity
            peak_at = point.at
        fall = peak - point.equity
        if fall > worst:
            worst = fall
            worst_peak = peak
            worst_peak_at = peak_at
            worst_trough_index = index

    if worst == 0:
        return _ZERO, None, None, True

    recovered_at = None
    for point in curve[worst_trough_index:]:
        if point.equity >= worst_peak:
            recovered_at = point.at
            break

    duration = (recovered_at or curve[-1].at) - worst_peak_at
    return worst, _divide(worst, worst_peak), duration, recovered_at is not None


def _portfolio_metrics(trades: Sequence[Trade], curve: Sequence[EquityPoint]) -> PortfolioMetrics:
    if not curve:
        raise ValueError(
            "the equity curve is empty; it always carries at least the run's opening "
            "balance, so an empty one means the curve was never built"
        )

    starting_capital = curve[0].equity
    ending_equity = curve[-1].equity
    worst, worst_pct, duration, recovered = _drawdown(curve)

    held = sum((t.exit.occurred_at - t.entry.occurred_at for t in trades), timedelta())
    span = curve[-1].at - curve[0].at
    exposure = (
        _divide(Decimal(int(held.total_seconds())), Decimal(int(span.total_seconds())))
        if span
        else None
    )

    active_days = len({t.exit.occurred_at.date() for t in trades})

    return PortfolioMetrics(
        gross_pnl=sum((t.gross_pnl for t in trades), _ZERO),
        total_costs=sum((t.costs for t in trades), _ZERO),
        net_pnl=sum((t.net_pnl for t in trades), _ZERO),
        starting_capital=starting_capital,
        ending_equity=ending_equity,
        total_return=_divide(ending_equity - starting_capital, starting_capital),
        max_drawdown=worst,
        max_drawdown_pct=worst_pct,
        max_drawdown_duration=duration,
        max_drawdown_recovered=recovered,
        time_in_market=held,
        exposure=exposure,
        trades_per_active_day=_divide(Decimal(len(trades)), Decimal(active_days)),
        active_days=active_days,
        equity_curve=tuple(curve),
    )


def _returns(curve: Sequence[EquityPoint]) -> list[Decimal]:
    """One return per closed trade, as a fraction of the equity before it."""
    series: list[Decimal] = []
    for previous, point in zip(curve, curve[1:], strict=False):
        step = _divide(point.equity - previous.equity, previous.equity)
        if step is not None:
            series.append(step)
    return series


def _risk_metrics(curve: Sequence[EquityPoint], net_pnl: Decimal, worst: Decimal) -> RiskMetrics:
    series = _returns(curve)
    mean_return = _mean(series)
    stdev = _sample_stdev(series)

    downside = [value for value in series if value < 0]
    downside_deviation = None
    if len(series) >= 2 and downside:
        with localcontext(_CONTEXT):
            squares = sum((value**2 for value in downside), _ZERO)
            downside_deviation = (squares / Decimal(len(series) - 1)).sqrt()

    return RiskMetrics(
        mean_return=mean_return,
        return_stdev=stdev,
        downside_deviation=downside_deviation,
        sharpe_per_trade=(
            _divide(mean_return, stdev) if mean_return is not None and stdev else None
        ),
        sortino_per_trade=(
            _divide(mean_return, downside_deviation)
            if mean_return is not None and downside_deviation
            else None
        ),
        return_over_max_drawdown=_divide(net_pnl, worst),
    )


def _statistical_metrics(trades: Sequence[Trade]) -> StatisticalMetrics:
    nets = [trade.net_pnl for trade in trades]
    mean_pnl = _mean(nets)
    stdev = _sample_stdev(nets)

    standard_error = None
    if stdev is not None:
        with localcontext(_CONTEXT):
            standard_error = stdev / Decimal(len(nets)).sqrt()

    interval = None
    t_statistic = None
    if mean_pnl is not None and standard_error is not None:
        with localcontext(_CONTEXT):
            margin = _Z_95 * standard_error
            interval = (mean_pnl - margin, mean_pnl + margin)
        t_statistic = _divide(mean_pnl, standard_error)

    return StatisticalMetrics(
        sample_size=len(nets),
        mean_trade_pnl=mean_pnl,
        trade_pnl_stdev=stdev,
        standard_error=standard_error,
        confidence_interval_95=interval,
        t_statistic=t_statistic,
        degrees_of_freedom=len(nets) - 1 if len(nets) >= 2 else None,
    )


def evaluate_performance(
    trades: Sequence[Trade],
    equity_curve: Sequence[EquityPoint],
    *,
    quarantined_sessions: int = 0,
) -> PerformanceReport:
    """Measure one run from its closed trades and its equity curve.

    ``quarantined_sessions`` is supplied by the engine: sessions skipped for
    unusable data leave no trades behind, so nothing in these two inputs could
    reveal that they existed, and a run that silently dropped a third of its
    days would otherwise look complete.
    """
    if quarantined_sessions < 0:
        raise ValueError(f"quarantined_sessions must not be negative, got {quarantined_sessions}")

    portfolio = _portfolio_metrics(trades, equity_curve)

    return PerformanceReport(
        trades=_trade_metrics(trades),
        portfolio=portfolio,
        risk=_risk_metrics(equity_curve, portfolio.net_pnl, portfolio.max_drawdown),
        statistics=_statistical_metrics(trades),
        ambiguous_exit_count=sum(
            1 for t in trades if t.ambiguity is not AmbiguityResolution.UNAMBIGUOUS
        ),
        pessimistic_fallback_count=sum(
            1 for t in trades if t.ambiguity is AmbiguityResolution.PESSIMISTIC_FALLBACK
        ),
        quarantined_sessions=quarantined_sessions,
    )
