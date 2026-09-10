"""Metrics attached to a run's result.

Reuses the hand-built fixture from the metrics tests - four trades netting
+1000, -500, +1500, -1000 on 500,000 - so the figures asserted here are the
same ones already worked out by hand there, and this file only checks that they
survive the journey into the result.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, timedelta
from decimal import Decimal

import pytest

from app.domain.backtest.cash import build_equity_curve
from app.domain.backtest.metrics import evaluate_performance
from app.domain.backtest.models import EquityPoint, RunManifest, SignalRecord, Trade
from app.domain.backtest.result import BacktestResult
from app.domain.strategy.contract import Signal, SignalDirection
from tests.backtest.test_metrics import CAPITAL, FIXTURE, START, trade

DIGEST = "a" * 64


def manifest() -> RunManifest:
    return RunManifest(
        input_fingerprint=DIGEST,
        strategy_name="orb",
        strategy_version="1",
        engine_version="2.5",
        generated_at=START,
    )


def curve_for(trades: tuple[Trade, ...]) -> tuple[EquityPoint, ...]:
    return build_equity_curve(trades, starting_capital=CAPITAL, start_at=START)


def measured(trades: tuple[Trade, ...] = FIXTURE, **kw: object) -> BacktestResult:
    return BacktestResult.measured(
        manifest(),
        trades=trades,
        equity_curve=curve_for(trades),
        **kw,  # type: ignore[arg-type]
    )


def signal_record() -> SignalRecord:
    return SignalRecord(
        signal=Signal(
            instrument_token=738561,
            direction=SignalDirection.LONG,
            stop_price=Decimal("1390.00"),
            target_r_multiple=Decimal("2"),
            signal_bar_start=START,
            reason="LONG_ORB_BREAKOUT",
        ),
        accepted=True,
        decision_reason="taken",
    )


class TestTheResultCarriesItsMetrics:
    def test_measuring_attaches_a_report(self) -> None:
        result = measured()

        assert result.performance is not None
        assert result.performance.trades.trade_count == 4
        assert len(result.trades) == 4
        assert len(result.equity_curve) == 5

    def test_the_headline_figures_agree(self) -> None:
        """Gross, costs and net stay three separate numbers all the way up."""
        portfolio = measured().performance.portfolio  # type: ignore[union-attr]

        assert portfolio.gross_pnl == Decimal("1000")
        assert portfolio.total_costs == Decimal("0")
        assert portfolio.net_pnl == Decimal("1000")
        assert portfolio.net_pnl == portfolio.gross_pnl - portfolio.total_costs

    def test_costed_trades_keep_gross_and_net_apart(self) -> None:
        costed = (trade("1000", costs="25"), trade("-500", hours_in=2, costs="25"))
        portfolio = measured(costed).performance.portfolio  # type: ignore[union-attr]

        assert portfolio.gross_pnl == Decimal("500")
        assert portfolio.total_costs == Decimal("100")
        assert portfolio.net_pnl == Decimal("400")

    def test_every_metric_group_is_present(self) -> None:
        report = measured().performance
        assert report is not None

        assert report.trades.expectancy_inr == Decimal("250")
        assert report.portfolio.max_drawdown == Decimal("1000")
        assert report.risk.sharpe_per_trade is not None
        assert report.statistics.t_statistic is not None
        assert report.statistics.sample_size == 4

    def test_the_signal_log_rides_along(self) -> None:
        result = BacktestResult.measured(
            manifest(),
            trades=FIXTURE,
            equity_curve=curve_for(FIXTURE),
            signal_log=(signal_record(),),
        )
        assert len(result.signal_log) == 1

    def test_quarantined_sessions_reach_the_report(self) -> None:
        result = measured(quarantined_sessions=2)
        assert result.performance is not None
        assert result.performance.quarantined_sessions == 2


class TestMetricsMustDescribeThisRun:
    """The one mistake this shape invites."""

    def test_a_report_from_other_trades_is_refused(self) -> None:
        elsewhere = evaluate_performance(FIXTURE, curve_for(FIXTURE))

        with pytest.raises(ValueError, match="measured from a different run"):
            BacktestResult(
                manifest=manifest(),
                trades=FIXTURE[:2],
                equity_curve=curve_for(FIXTURE),
                performance=elsewhere,
            )

    def test_a_report_from_another_curve_is_refused(self) -> None:
        shorter = FIXTURE[:2]

        with pytest.raises(ValueError, match="different equity curve"):
            BacktestResult(
                manifest=manifest(),
                trades=shorter,
                equity_curve=curve_for(shorter),
                performance=evaluate_performance(shorter, curve_for(FIXTURE)),
            )


class TestEmptyResultsAreExplicit:
    def test_an_unmeasured_run_says_so(self) -> None:
        """None means *not measured*, which is not the same as measured and
        empty - "we found nothing" and "we did not look" are different
        findings."""
        result = BacktestResult(manifest=manifest())

        assert result.performance is None
        assert result.trades == ()
        assert result.canonical()["measured"] == "false"

    def test_a_measured_run_with_no_trades_is_a_real_report(self) -> None:
        """A zero-trade run still has an opening balance to measure, and its
        report is full of honest Nones rather than absent."""
        result = BacktestResult.measured(manifest(), equity_curve=curve_for(()))

        assert result.performance is not None
        assert result.performance.trades.trade_count == 0
        assert result.performance.trades.win_rate is None
        assert result.performance.portfolio.net_pnl == Decimal("0")
        assert result.canonical()["measured"] == "true"
        assert result.canonical()["win_rate"] == ""

    def test_measuring_an_empty_curve_is_refused(self) -> None:
        """A curve always carries the opening balance, so an empty one means it
        was never built."""
        with pytest.raises(ValueError, match="equity curve is empty"):
            BacktestResult.measured(manifest(), equity_curve=())


class TestDeterministicSerialization:
    def test_the_same_run_renders_identically(self) -> None:
        assert measured().canonical() == measured().canonical()

    def test_keys_are_sorted_and_values_are_strings(self) -> None:
        rendered = measured().canonical()

        assert list(rendered) == sorted(rendered)
        assert all(isinstance(value, str) for value in rendered.values())

    def test_the_headline_numbers_render_exactly(self) -> None:
        rendered = measured().canonical()

        assert rendered["gross_pnl"] == "1000"
        assert rendered["net_pnl"] == "1000"
        assert rendered["total_costs"] == "0"
        assert rendered["max_drawdown"] == "1000"
        assert rendered["expectancy_inr"] == "250"
        assert rendered["win_rate"] == "0.5"
        assert rendered["trade_count"] == "4"
        assert rendered["input_fingerprint"] == DIGEST

    def test_an_undefined_metric_renders_empty_not_zero(self) -> None:
        """The distinction this whole layer exists to preserve. A run with no
        losing trade has no profit factor, and rendering it as 0 would report a
        strategy that never profited."""
        winners = (trade("1000"), trade("500", hours_in=2))
        rendered = measured(winners).canonical()

        assert rendered["profit_factor"] == ""
        assert rendered["win_rate"] == "1"

    def test_the_summary_does_not_re_encode_the_run(self) -> None:
        """The trades and the curve are the run. Re-encoding them here would be
        a second representation of the same facts."""
        rendered = measured().canonical()

        assert "trades" not in rendered
        assert "equity_curve" not in rendered
        assert rendered["trade_count"] == "4"


class TestMetricsNeverTouchMarketData:
    def test_measuring_needs_only_trades_and_an_equity_curve(self) -> None:
        import inspect

        parameters = set(inspect.signature(evaluate_performance).parameters)
        assert parameters == {"trades", "equity_curve", "quarantined_sessions"}

    def test_the_metrics_module_imports_no_market_data(self) -> None:
        """Candles are the engine's business. A metric that could reach one
        could be computed from something other than what was traded."""
        import ast
        import pathlib

        import app.domain.backtest.metrics as metrics_module

        source = pathlib.Path(metrics_module.__file__).read_text(encoding="utf-8")
        imported = {
            node.module
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom) and node.module
        }

        assert not any("market" in module for module in imported)
        assert "app.domain.backtest.execution" not in imported
        assert "app.domain.strategy.orb" not in imported

    def test_a_result_can_be_measured_without_any_bars(self) -> None:
        """The whole fixture is built from trades and an equity curve; no
        candle exists anywhere in this file."""
        assert measured().performance is not None


class TestResultShape:
    def test_it_is_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            measured().performance = None  # type: ignore[misc]

    def test_the_curve_must_be_ordered(self) -> None:
        earlier = EquityPoint(at=START, cash=CAPITAL, position_value=Decimal("0"), equity=CAPITAL)
        later = dataclasses.replace(earlier, at=START + timedelta(hours=1))

        with pytest.raises(ValueError, match="ordered in time"):
            BacktestResult(manifest=manifest(), equity_curve=(later, earlier))

    def test_utc_fixture_assumption(self) -> None:
        assert START.tzinfo is UTC
