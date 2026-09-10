"""Performance metrics against one hand-worked fixture.

.. rubric:: The fixture

Four trades on 500,000 of capital, each 100 shares entered at 1000.00 and held
for thirty minutes, with no costs so the net P&L is exactly the round number
named:

    trade 1   +1000     equity 501,000
    trade 2    -500      equity 500,500
    trade 3   +1500     equity 502,000
    trade 4   -1000     equity 501,000

Everything asserted below was derived from those four numbers by hand:

    expectancy      (1000 - 500 + 1500 - 1000) / 4      = 250
    profit factor   2500 / 1500                          = 1.6667
    max drawdown    peak 502,000 -> 501,000              = 1000
    stdev           deviations 750, -750, 1250, -1250
                    squares sum 4,250,000, / 3           = 1,416,666.67
                    sqrt                                 = 1190.238071
    standard error  1190.238071 / sqrt(4)                = 595.119036
    t statistic     250 / 595.119036                     = 0.4201
    95% interval    250 +/- 1.96 * 595.119036            = (-916.43, 1416.43)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.backtest.cash import build_equity_curve
from app.domain.backtest.metrics import evaluate_performance
from app.domain.backtest.models import (
    AmbiguityResolution,
    EquityPoint,
    Fill,
    FillReason,
    OrderSide,
    Trade,
)
from app.domain.strategy.contract import SignalDirection

CAPITAL = Decimal("500000")
START = datetime(2026, 8, 21, 3, 45, tzinfo=UTC)
QUANTITY = 100
ENTRY_PRICE = Decimal("1000.00")
HOLD = timedelta(minutes=30)


def trade(
    net: str,
    *,
    hours_in: int = 1,
    direction: SignalDirection = SignalDirection.LONG,
    costs: str = "0",
    ambiguity: AmbiguityResolution = AmbiguityResolution.UNAMBIGUOUS,
) -> Trade:
    """A trade netting exactly ``net`` on 100 shares, ``hours_in`` after the start."""
    entered = START + timedelta(hours=hours_in)
    exited = entered + HOLD
    move = Decimal(net) / Decimal(QUANTITY)
    entry_side = OrderSide.BUY if direction.is_long else OrderSide.SELL
    exit_price = ENTRY_PRICE + move if direction.is_long else ENTRY_PRICE - move

    def fill(side: OrderSide, reason: FillReason, price: Decimal, at: datetime) -> Fill:
        return Fill(
            side=side,
            reason=reason,
            quantity=QUANTITY,
            price=price,
            reference_price=price,
            slippage_per_unit=Decimal("0"),
            costs=Decimal(costs),
            occurred_at=at,
            bar_start=at,
        )

    entry = fill(entry_side, FillReason.ENTRY, ENTRY_PRICE, entered)
    exit_fill = fill(entry_side.opposite, FillReason.TARGET, exit_price, exited)
    gross = Decimal(net)
    total_costs = Decimal(costs) * 2

    return Trade(
        instrument_token=738561,
        direction=direction,
        entry=entry,
        exit=exit_fill,
        gross_pnl=gross,
        costs=total_costs,
        net_pnl=gross - total_costs,
        exit_reason=FillReason.TARGET,
        ambiguity=ambiguity,
    )


FIXTURE = (
    trade("1000", hours_in=1),
    trade("-500", hours_in=2),
    trade("1500", hours_in=3),
    trade("-1000", hours_in=4),
)


def report(*trades: Trade, quarantined: int = 0):  # noqa: ANN201
    curve = build_equity_curve(trades, starting_capital=CAPITAL, start_at=START)
    return evaluate_performance(trades, curve, quarantined_sessions=quarantined)


class TestTradeMetrics:
    def test_counts_and_win_rate(self) -> None:
        m = report(*FIXTURE).trades

        assert m.trade_count == 4
        assert (m.win_count, m.loss_count, m.scratch_count) == (2, 2, 0)
        assert m.win_rate == Decimal("0.5")

    def test_averages_are_signed(self) -> None:
        """A loss is reported negative. Magnitudes read tidily and invite
        exactly one mistake: adding them to the wins."""
        m = report(*FIXTURE).trades

        assert m.average_win == Decimal("1250")
        assert m.average_loss == Decimal("-750")
        assert m.largest_win == Decimal("1500")
        assert m.largest_loss == Decimal("-1000")

    def test_known_expectancy(self) -> None:
        assert report(*FIXTURE).trades.expectancy_inr == Decimal("250")

    def test_known_profit_factor(self) -> None:
        """2500 of gross profit against 1500 of gross loss."""
        factor = report(*FIXTURE).trades.profit_factor
        assert factor is not None
        assert factor.quantize(Decimal("0.0001")) == Decimal("1.6667")

    def test_average_holding_time(self) -> None:
        assert report(*FIXTURE).trades.average_holding_time == HOLD

    def test_long_short_breakdown(self) -> None:
        mixed = (trade("1000"), trade("-400", hours_in=2, direction=SignalDirection.SHORT))
        m = report(*mixed).trades

        assert (m.long_count, m.short_count) == (1, 1)
        assert m.long_net_pnl == Decimal("1000")
        assert m.short_net_pnl == Decimal("-400")

    def test_expectancy_in_r_is_undefined_until_r_is_recorded(self) -> None:
        """No trade carries an r_multiple yet - the project has not decided
        whether R is measured on the gross or the net move, and this module
        will not decide it by averaging whatever happens to be there."""
        assert report(*FIXTURE).trades.expectancy_r is None


class TestPortfolioMetrics:
    def test_pnl_aggregation_separates_gross_from_costs(self) -> None:
        costed = (trade("1000", costs="25"), trade("-500", hours_in=2, costs="25"))
        m = report(*costed).portfolio

        assert m.gross_pnl == Decimal("500")
        assert m.total_costs == Decimal("100")
        assert m.net_pnl == Decimal("400")

    def test_total_return(self) -> None:
        """1000 net on 500,000 of capital."""
        assert report(*FIXTURE).portfolio.total_return == Decimal("0.002")

    def test_known_drawdown(self) -> None:
        """Equity peaks at 502,000 after trade 3 and falls to 501,000 after
        trade 4 - a fall of 1000, never recovered because the run ends there.

        The underwater period runs from the peak to the end of the curve, and
        those are the exits of trades 3 and 4 one hour apart. It is not the
        holding time: a drawdown is measured between equity points, not inside
        the trade that caused it.
        """
        m = report(*FIXTURE).portfolio

        assert m.max_drawdown == Decimal("1000")
        assert m.max_drawdown_pct is not None
        assert m.max_drawdown_pct.quantize(Decimal("0.000001")) == Decimal("0.001992")
        assert m.max_drawdown_duration == timedelta(hours=1)
        assert m.max_drawdown_recovered is False

    def test_a_recovered_drawdown_is_marked_as_such(self) -> None:
        recovering = (trade("-1000"), trade("2000", hours_in=2))
        m = report(*recovering).portfolio

        assert m.max_drawdown == Decimal("1000")
        assert m.max_drawdown_recovered is True

    def test_exposure_and_time_in_market(self) -> None:
        """Four thirty-minute holds is two hours held. The run spans from the
        opening balance to the last exit, and the denominator is calendar time -
        which is why the raw timedelta is reported alongside."""
        m = report(*FIXTURE).portfolio

        assert m.time_in_market == timedelta(hours=2)
        assert m.exposure is not None
        assert m.exposure < Decimal("1")

    def test_trades_per_active_day(self) -> None:
        """All four close on the same session, so four per active day."""
        m = report(*FIXTURE).portfolio
        assert m.active_days == 1
        assert m.trades_per_active_day == Decimal("4")


class TestRiskMetrics:
    def test_sharpe_is_zero_when_the_mean_return_is_zero(self) -> None:
        """Up 10% then down 10% of the larger balance: +10 on 100 and -11 on
        110 are +0.1 and -0.1, so the mean is exactly zero and so is Sharpe."""
        curve = (
            EquityPoint(
                at=START, cash=Decimal("100"), position_value=Decimal("0"), equity=Decimal("100")
            ),
            EquityPoint(
                at=START + HOLD,
                cash=Decimal("110"),
                position_value=Decimal("0"),
                equity=Decimal("110"),
            ),
            EquityPoint(
                at=START + HOLD * 2,
                cash=Decimal("99"),
                position_value=Decimal("0"),
                equity=Decimal("99"),
            ),
        )
        risk = evaluate_performance((), curve).risk

        assert risk.mean_return == Decimal("0")
        assert risk.sharpe_per_trade == Decimal("0")
        assert risk.sortino_per_trade == Decimal("0")

    def test_a_positive_expectancy_gives_a_positive_sharpe(self) -> None:
        risk = report(*FIXTURE).risk
        assert risk.sharpe_per_trade is not None
        assert risk.sharpe_per_trade > 0

    def test_return_over_max_drawdown(self) -> None:
        """1000 of net profit against 1000 of drawdown."""
        assert report(*FIXTURE).risk.return_over_max_drawdown == Decimal("1")


class TestStatisticalMetrics:
    def test_known_standard_error_and_t_statistic(self) -> None:
        stats = report(*FIXTURE).statistics

        assert stats.sample_size == 4
        assert stats.degrees_of_freedom == 3
        assert stats.mean_trade_pnl == Decimal("250")
        assert stats.trade_pnl_stdev is not None
        assert stats.trade_pnl_stdev.quantize(Decimal("0.01")) == Decimal("1190.24")
        assert stats.standard_error is not None
        assert stats.standard_error.quantize(Decimal("0.01")) == Decimal("595.12")
        assert stats.t_statistic is not None
        assert stats.t_statistic.quantize(Decimal("0.0001")) == Decimal("0.4201")

    def test_known_confidence_interval(self) -> None:
        """250 +/- 1.96 * 595.12. It straddles zero, which is the honest verdict
        on four trades: this edge is not distinguishable from luck."""
        interval = report(*FIXTURE).statistics.confidence_interval_95
        assert interval is not None
        low, high = interval

        assert low.quantize(Decimal("0.01")) == Decimal("-916.43")
        assert high.quantize(Decimal("0.01")) == Decimal("1416.43")
        assert low < 0 < high


class TestUndefinedIsNotZero:
    """A ratio with an empty denominator has no value, and zero is a value."""

    def test_no_trades(self) -> None:
        empty = report()

        assert empty.trades.trade_count == 0
        assert empty.trades.win_rate is None
        assert empty.trades.expectancy_inr is None
        assert empty.trades.profit_factor is None
        assert empty.trades.average_holding_time is None
        assert empty.portfolio.net_pnl == Decimal("0")
        assert empty.portfolio.max_drawdown == Decimal("0")
        assert empty.portfolio.trades_per_active_day is None
        assert empty.risk.sharpe_per_trade is None
        assert empty.statistics.standard_error is None
        assert empty.statistics.confidence_interval_95 is None

    def test_one_trade_has_no_spread_to_measure(self) -> None:
        """One observation has no spread - not a spread of zero - so everything
        that divides by one is undefined rather than infinite."""
        single = report(trade("1000"))

        assert single.trades.trade_count == 1
        assert single.trades.expectancy_inr == Decimal("1000")
        assert single.statistics.trade_pnl_stdev is None
        assert single.statistics.standard_error is None
        assert single.statistics.t_statistic is None
        assert single.statistics.degrees_of_freedom is None
        assert single.risk.sharpe_per_trade is None

    def test_all_winners_have_no_profit_factor(self) -> None:
        """Dividing by zero gross loss is undefined, not infinite, and not a
        very large number either."""
        winners = report(trade("1000"), trade("500", hours_in=2))

        assert winners.trades.win_rate == Decimal("1")
        assert winners.trades.profit_factor is None
        assert winners.trades.average_loss is None
        assert winners.trades.largest_loss is None
        assert winners.risk.sortino_per_trade is None
        assert winners.portfolio.max_drawdown == Decimal("0")
        assert winners.portfolio.max_drawdown_pct is None
        assert winners.risk.return_over_max_drawdown is None

    def test_all_losers(self) -> None:
        losers = report(trade("-1000"), trade("-500", hours_in=2))

        assert losers.trades.win_rate == Decimal("0")
        assert losers.trades.profit_factor == Decimal("0")
        assert losers.trades.average_win is None
        assert losers.trades.expectancy_inr == Decimal("-750")
        assert losers.portfolio.max_drawdown == Decimal("1500")

    def test_zero_variance_leaves_no_sharpe(self) -> None:
        """Two identical 10% gains: the returns have no spread, so there is
        nothing to divide the mean by."""
        curve = (
            EquityPoint(
                at=START, cash=Decimal("100"), position_value=Decimal("0"), equity=Decimal("100")
            ),
            EquityPoint(
                at=START + HOLD,
                cash=Decimal("110"),
                position_value=Decimal("0"),
                equity=Decimal("110"),
            ),
            EquityPoint(
                at=START + HOLD * 2,
                cash=Decimal("121"),
                position_value=Decimal("0"),
                equity=Decimal("121"),
            ),
        )
        risk = evaluate_performance((), curve).risk

        assert risk.return_stdev == Decimal("0")
        assert risk.sharpe_per_trade is None
        assert risk.sortino_per_trade is None

    def test_zero_exposure_and_zero_span(self) -> None:
        """A curve with only its opening balance spans no time at all."""
        empty = report()
        assert empty.portfolio.time_in_market == timedelta()
        assert empty.portfolio.exposure is None


class TestDataQualityCounts:
    def test_ambiguous_and_fallback_exits_are_counted(self) -> None:
        """A run whose exits were mostly guessed is weaker evidence than one
        resolved from real 1-minute data, however good its Sharpe looks."""
        mixed = (
            trade("1000"),
            trade("-500", hours_in=2, ambiguity=AmbiguityResolution.RESOLVED_BY_1M),
            trade("1500", hours_in=3, ambiguity=AmbiguityResolution.PESSIMISTIC_FALLBACK),
        )
        result = report(*mixed)

        assert result.ambiguous_exit_count == 2
        assert result.pessimistic_fallback_count == 1

    def test_quarantined_sessions_are_carried_from_the_engine(self) -> None:
        """Skipped sessions leave no trades, so nothing in the two inputs could
        reveal they existed."""
        assert report(*FIXTURE, quarantined=3).quarantined_sessions == 3

    def test_a_negative_quarantine_count_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be negative"):
            report(*FIXTURE, quarantined=-1)


class TestArithmeticIsExact:
    def test_every_money_metric_is_decimal(self) -> None:
        result = report(*FIXTURE)

        for value in (
            result.trades.expectancy_inr,
            result.trades.average_win,
            result.portfolio.net_pnl,
            result.portfolio.max_drawdown,
            result.statistics.mean_trade_pnl,
        ):
            assert isinstance(value, Decimal)
            assert not isinstance(value, float)

    def test_an_empty_curve_is_rejected(self) -> None:
        """The curve always carries at least the opening balance, so an empty
        one means it was never built."""
        with pytest.raises(ValueError, match="equity curve is empty"):
            evaluate_performance((), ())
