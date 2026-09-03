"""BacktestInput: what it refuses to be built from, and how it identifies itself."""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.backtest.config import ExecutionConfig, SlippageConfig
from app.domain.backtest.input import BacktestInput, InvalidBacktestInputError
from app.domain.market.models import CandleInterval, CandleStatus
from app.domain.market.session import MarketSessionCalendar
from app.domain.strategy.params import OrbParams
from tests.backtest.conftest import (
    INFY_TOKEN,
    SESSION_OPEN,
    make_candle,
    make_input,
    make_instrument,
    make_series,
    placeholder_schedule,
)


class TestValidConstruction:
    def test_a_well_formed_input_is_accepted(self, backtest_input: BacktestInput) -> None:
        assert len(backtest_input.candles_5m) == 12
        assert len(backtest_input.candles_1m) == 60
        assert backtest_input.instrument.tradingsymbol == "RELIANCE"

    def test_input_is_frozen(self, backtest_input: BacktestInput) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            backtest_input.candles_5m = ()  # type: ignore[misc]

    def test_sessions_are_derived_in_order(self, backtest_input: BacktestInput) -> None:
        assert backtest_input.sessions == (date(2026, 8, 21),)

    def test_input_holds_no_repository_clock_or_provider(self) -> None:
        """Determinism is structural: there is nothing external to reach."""
        names = {f.name for f in dataclasses.fields(BacktestInput)}
        forbidden = {"repository", "clock", "provider", "settings", "session_factory", "path"}
        assert names & forbidden == set()
        assert names == {
            "instrument",
            "candles_5m",
            "candles_1m",
            "calendar",
            "strategy_params",
            "cost_schedule",
            "slippage_config",
            "execution_config",
        }


class TestRejectsBadSeries:
    def test_rejects_empty_signal_series(self) -> None:
        with pytest.raises(InvalidBacktestInputError, match="candles_5m is empty"):
            make_input(candles_5m=())

    def test_rejects_empty_resolution_series(self) -> None:
        with pytest.raises(InvalidBacktestInputError, match="candles_1m is empty"):
            make_input(candles_1m=())

    def test_rejects_an_in_progress_candle(self) -> None:
        bars = list(make_series(3, CandleInterval.M5))
        bars[1] = make_candle(
            SESSION_OPEN + CandleInterval.M5.delta,
            CandleInterval.M5,
            status=CandleStatus.IN_PROGRESS,
        )
        with pytest.raises(InvalidBacktestInputError, match="only completed bars"):
            make_input(candles_5m=tuple(bars))

    def test_rejects_non_ascending_candles(self) -> None:
        bars = make_series(3, CandleInterval.M5)
        reordered = (bars[0], bars[2], bars[1])
        with pytest.raises(InvalidBacktestInputError, match="is not ascending"):
            make_input(candles_5m=reordered)

    def test_rejects_duplicate_buckets(self) -> None:
        bars = make_series(3, CandleInterval.M5)
        with pytest.raises(InvalidBacktestInputError, match="duplicate bucket"):
            make_input(candles_5m=(bars[0], bars[1], bars[1]))

    def test_rejects_mixed_instruments(self) -> None:
        bars = list(make_series(3, CandleInterval.M5))
        bars[2] = make_candle(
            SESSION_OPEN + CandleInterval.M5.delta * 2,
            CandleInterval.M5,
            token=INFY_TOKEN,
        )
        with pytest.raises(InvalidBacktestInputError, match="one input holds exactly one"):
            make_input(candles_5m=tuple(bars))

    def test_rejects_candles_for_a_different_instrument_entirely(self) -> None:
        with pytest.raises(InvalidBacktestInputError, match="belongs to instrument"):
            make_input(instrument=make_instrument(token=INFY_TOKEN, tradingsymbol="INFY"))

    def test_rejects_mismatched_interval_on_the_signal_series(self) -> None:
        with pytest.raises(InvalidBacktestInputError, match="has interval 15m, expected 5m"):
            make_input(candles_5m=make_series(3, CandleInterval.M15))

    def test_rejects_mismatched_interval_on_the_resolution_series(self) -> None:
        with pytest.raises(InvalidBacktestInputError, match="has interval 5m, expected 1m"):
            make_input(candles_1m=make_series(3, CandleInterval.M5))

    def test_rejects_misaligned_bar_boundaries(self) -> None:
        """A 5m bar must start on a 5m epoch boundary, as Phase 1 buckets do."""
        offset = make_candle(SESSION_OPEN + timedelta(minutes=2), CandleInterval.M5)
        with pytest.raises(InvalidBacktestInputError, match="not aligned to a 5m boundary"):
            make_input(candles_5m=(offset,))

    def test_rejects_a_naive_timestamp_that_bypassed_candle_validation(self) -> None:
        """Defence in depth.

        ``Candle`` already raises on a naive timestamp, so the only way to reach
        this guard is to force the value past that constructor - which is
        exactly what a future refactor of Candle could accidentally do.
        """
        bars = list(make_series(2, CandleInterval.M5))
        object.__setattr__(bars[1], "start_at", datetime(2026, 8, 21, 3, 50))
        with pytest.raises(InvalidBacktestInputError, match="naive timestamp"):
            make_input(candles_5m=tuple(bars))

    def test_rejects_a_list_instead_of_a_tuple(self) -> None:
        with pytest.raises(InvalidBacktestInputError, match="must be a tuple"):
            make_input(candles_5m=list(make_series(3, CandleInterval.M5)))  # type: ignore[arg-type]


class TestResolutionCoverage:
    def test_rejects_a_session_with_no_resolution_bars(self) -> None:
        """A 5m session without 1m bars would silently degrade every exit."""
        next_day = SESSION_OPEN + timedelta(days=1)
        five_minute = make_series(3, CandleInterval.M5) + make_series(
            3, CandleInterval.M5, start_at=next_day
        )
        one_minute = make_series(15, CandleInterval.M1)
        with pytest.raises(InvalidBacktestInputError, match="but no 1m bars"):
            make_input(candles_5m=five_minute, candles_1m=one_minute)

    def test_accepts_when_every_session_is_covered(self) -> None:
        next_day = SESSION_OPEN + timedelta(days=1)
        five_minute = make_series(3, CandleInterval.M5) + make_series(
            3, CandleInterval.M5, start_at=next_day
        )
        one_minute = make_series(15, CandleInterval.M1) + make_series(
            15, CandleInterval.M1, start_at=next_day
        )
        assert len(make_input(candles_5m=five_minute, candles_1m=one_minute).sessions) == 2

    def test_extra_resolution_sessions_are_allowed(self) -> None:
        """More 1m history than 5m is harmless; the reverse is not."""
        next_day = SESSION_OPEN + timedelta(days=1)
        one_minute = make_series(15, CandleInterval.M1) + make_series(
            15, CandleInterval.M1, start_at=next_day
        )
        assert make_input(candles_1m=one_minute).sessions == (date(2026, 8, 21),)

    def test_error_names_the_missing_sessions(self) -> None:
        next_day = SESSION_OPEN + timedelta(days=1)
        five_minute = make_series(3, CandleInterval.M5) + make_series(
            3, CandleInterval.M5, start_at=next_day
        )
        with pytest.raises(InvalidBacktestInputError, match="2026-08-22"):
            make_input(candles_5m=five_minute, candles_1m=make_series(15, CandleInterval.M1))


class TestFingerprint:
    def test_is_a_sha256_hex_digest(self, backtest_input: BacktestInput) -> None:
        fingerprint = backtest_input.fingerprint()
        assert len(fingerprint) == 64
        assert set(fingerprint) <= set("0123456789abcdef")

    def test_is_stable_across_repeated_calls(self, backtest_input: BacktestInput) -> None:
        first = backtest_input.fingerprint()
        for _ in range(5):
            assert backtest_input.fingerprint() == first

    def test_two_equal_inputs_fingerprint_identically(self) -> None:
        assert make_input().fingerprint() == make_input().fingerprint()

    def test_does_not_use_builtin_hash(self, backtest_input: BacktestInput) -> None:
        """`hash()` is salted per process, so it cannot identify a run.

        A subprocess with a different PYTHONHASHSEED must produce the same
        digest; that is the whole point of the guarantee.
        """
        import os
        import subprocess
        import sys

        script = "from tests.backtest.conftest import make_input;print(make_input().fingerprint())"
        digests = set()
        for seed in ("0", "12345"):
            env = {**os.environ, "PYTHONHASHSEED": seed}
            result = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                check=True,
                env=env,
            )
            digests.add(result.stdout.strip())
        assert len(digests) == 1
        assert digests == {backtest_input.fingerprint()}

    def test_changes_when_a_close_price_changes(self) -> None:
        bars = list(make_series(3, CandleInterval.M5))
        bars[1] = make_candle(
            SESSION_OPEN + CandleInterval.M5.delta, CandleInterval.M5, close="1403.00"
        )
        assert (
            make_input(candles_5m=tuple(bars)).fingerprint()
            != make_input(candles_5m=make_series(3, CandleInterval.M5)).fingerprint()
        )

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("open_", "1401.00"),
            ("high", "1450.00"),
            ("low", "1300.00"),
            ("close", "1499.00"),
        ],
    )
    def test_changes_when_any_ohlc_value_changes(self, field: str, value: str) -> None:
        baseline = make_input(candles_5m=make_series(1, CandleInterval.M5)).fingerprint()
        altered = make_candle(SESSION_OPEN, CandleInterval.M5, **{field: value})
        assert make_input(candles_5m=(altered,)).fingerprint() != baseline

    def test_changes_when_volume_changes(self) -> None:
        baseline = make_input(candles_5m=make_series(1, CandleInterval.M5)).fingerprint()
        altered = make_candle(SESSION_OPEN, CandleInterval.M5, volume=999_999)
        assert make_input(candles_5m=(altered,)).fingerprint() != baseline

    def test_changes_when_the_series_length_changes(self) -> None:
        a = make_input(candles_5m=make_series(3, CandleInterval.M5)).fingerprint()
        b = make_input(candles_5m=make_series(4, CandleInterval.M5)).fingerprint()
        assert a != b

    def test_changes_when_strategy_parameters_change(self) -> None:
        baseline = make_input().fingerprint()
        params = OrbParams(target_r_multiple=Decimal("3"))
        altered = make_input(strategy_params=params).fingerprint()
        assert altered != baseline

    def test_changes_when_slippage_config_changes(self) -> None:
        baseline = make_input().fingerprint()
        altered = make_input(slippage_config=SlippageConfig(adverse_ticks=3)).fingerprint()
        assert altered != baseline

    def test_changes_when_execution_config_changes(self) -> None:
        baseline = make_input().fingerprint()
        altered = make_input(
            execution_config=ExecutionConfig(target_requires_through_ticks=2)
        ).fingerprint()
        assert altered != baseline

    def test_changes_when_the_cost_schedule_changes(self) -> None:
        baseline = make_input().fingerprint()
        other = dataclasses.replace(placeholder_schedule(), version="1-verified")
        assert make_input(cost_schedule=other).fingerprint() != baseline

    def test_changes_when_the_instrument_changes(self) -> None:
        baseline = make_input().fingerprint()
        altered = make_input(instrument=make_instrument(tick_size="0.10")).fingerprint()
        assert altered != baseline

    def test_changes_when_the_calendar_holidays_change(self) -> None:
        baseline = make_input().fingerprint()
        altered = make_input(
            calendar=MarketSessionCalendar.nse_equity(holidays=[date(2026, 8, 15)])
        ).fingerprint()
        assert altered != baseline

    def test_unchanged_by_logically_equal_decimal_spellings(self) -> None:
        a = make_input(strategy_params=OrbParams(target_r_multiple=Decimal("2.0")))
        b = make_input(strategy_params=OrbParams(target_r_multiple=Decimal("2.00")))
        assert a.fingerprint() == b.fingerprint()

    def test_unchanged_by_provenance_metadata(self) -> None:
        """The same bar re-ingested from another source is the same bar."""
        import dataclasses as dc

        baseline = make_series(2, CandleInterval.M5)
        reingested = tuple(
            dc.replace(bar, source="zerodha", tick_count=bar.tick_count + 7) for bar in baseline
        )
        assert (
            make_input(candles_5m=reingested).fingerprint()
            == make_input(candles_5m=baseline).fingerprint()
        )

    def test_no_wall_clock_value_reaches_the_payload(self, backtest_input: BacktestInput) -> None:
        payload = backtest_input.canonical_payload()
        flattened = str(payload)
        assert "generated_at" not in flattened
        assert "retrieved_at" not in flattened

    def test_canonical_payload_is_json_serialisable_and_sorted(
        self, backtest_input: BacktestInput
    ) -> None:
        import json

        payload = backtest_input.canonical_payload()
        first = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        second = json.dumps(
            backtest_input.canonical_payload(), sort_keys=True, separators=(",", ":")
        )
        assert first == second
        assert list(payload) == sorted(payload)


def test_session_open_is_the_ist_market_open() -> None:
    """Guards the fixture: 03:45 UTC is 09:15 IST, the NSE equity open."""
    from app.core.time import to_ist

    local = to_ist(SESSION_OPEN)
    assert (local.hour, local.minute) == (9, 15)
    assert SESSION_OPEN.tzinfo is UTC
