"""ORB v2: prior ATR(14) on 15-minute bars, as a distinct hypothesis from v1.

.. rubric:: Hand calculation

Wednesday 2026-08-19 holds 16 complete 15-minute slots of 1-minute bars. In each
slot the first minute carries the whole range and the other fourteen sit flat at
1000.00, and every close is 1000.00, so each 15-minute bar's true range is its
high minus low - the same shape as ``test_prior_atr``:

    slot 0         1001.00 / 999.00    no previous close, not counted
    slots 1-7      1004.00 / 996.00    TR  8.00 each
    slots 8-14     1006.00 / 994.00    TR 12.00 each
    slot 15        1012.00 / 988.00    TR 24.00

    seed    (7 x 8.00 + 7 x 12.00) / 14            = 10.00
    Wilder  (10.00 x 13 + 24.00) / 14              = 11.00

v2 hands Thursday exactly 11. v1, over the same minutes, measures the 48 5-minute
bars instead: a third of them carry a range and two thirds are flat, so its ATR
is a different, smaller number.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.backtest.engine import run_backtest
from app.domain.backtest.input import BacktestInput
from app.domain.indicators import average_true_range
from app.domain.market.aggregation import aggregate_minutes
from app.domain.market.models import Candle, CandleInterval
from app.domain.strategy.orb import OrbStrategy
from app.domain.strategy.params import ORB_V2, OrbParams
from tests.backtest.conftest import make_candle, make_input
from tests.backtest.test_golden_backtest import golden_input

DAY_1 = datetime(2026, 8, 19, 3, 45, tzinfo=UTC)  # 09:15 IST
DAY_2 = DAY_1 + timedelta(days=1)
DAY_3 = DAY_1 + timedelta(days=2)
M1, M5, M15 = CandleInterval.M1, CandleInterval.M5, CandleInterval.M15

#: The golden input's fingerprint before v2 existed. v1 identity must not move.
GOLDEN_V1_FINGERPRINT = "d6916c9ab779eafa35f5a49c3e7fe2d9da68ccd1b871f1739d57cdc049753eeb"

SLOT_RANGES = (
    ("1001.00", "999.00"),
    *((("1004.00", "996.00"),) * 7),
    *((("1006.00", "994.00"),) * 7),
    ("1012.00", "988.00"),
)
VIOLENT = ("1500.00", "500.00")


def slot_minutes(day: datetime, slot: int, high: str, low: str) -> list[Candle]:
    start = day + M15.delta * slot
    first = make_candle(start, M1, open_="1000.00", high=high, low=low, close="1000.00", volume=100)
    flat = [
        make_candle(
            start + M1.delta * i,
            M1,
            open_="1000.00",
            high="1000.00",
            low="1000.00",
            close="1000.00",
            volume=100,
        )
        for i in range(1, 15)
    ]
    return [first, *flat]


def history(slots: int = 16) -> list[Candle]:
    return [
        m for i, (h, lo) in enumerate(SLOT_RANGES[:slots]) for m in slot_minutes(DAY_1, i, h, lo)
    ]


def session(day: datetime, *ranges: tuple[str, str]) -> list[Candle]:
    return [m for i, (h, lo) in enumerate(ranges) for m in slot_minutes(day, i, h, lo)]


DAY_2_QUIET = session(DAY_2, ("1003.00", "997.00"), ("1003.00", "997.00"))


def build(minutes: list[Candle], params: OrbParams = ORB_V2) -> BacktestInput:
    minutes = sorted(minutes, key=lambda m: m.start_at)
    return make_input(
        candles_5m=aggregate_minutes(minutes, M5).candles,
        candles_1m=tuple(minutes),
        strategy_params=params,
    )


BASELINE = build(history() + DAY_2_QUIET)


class TestAtrTimeframe:
    def test_v2_uses_prior_completed_15m_bars(self) -> None:
        assert BASELINE.prior_atr(DAY_2.date()) == Decimal("11")
        assert BASELINE.prior_atr(DAY_1.date()) is None  # nothing before day 1

    def test_v1_still_uses_prior_completed_5m_signal_bars(self) -> None:
        v1 = build(history() + DAY_2_QUIET, OrbParams())
        prior_5m = tuple(c for c in v1.candles_5m if c.start_at < DAY_2)
        assert v1.prior_atr(DAY_2.date()) == average_true_range(prior_5m)
        assert v1.prior_atr(DAY_2.date()) != Decimal("11")

    def test_v2_is_the_same_wilder_atr_over_aggregated_15m_bars(self) -> None:
        prior_15m = aggregate_minutes(tuple(m for m in history()), M15).candles
        assert len(prior_15m) == 16
        assert BASELINE.prior_atr(DAY_2.date()) == average_true_range(prior_15m)


class TestNoLeakage:
    def test_the_current_session_opening_range_cannot_affect_it(self) -> None:
        wild = session(DAY_2, VIOLENT, VIOLENT)
        assert build(history() + wild).prior_atr(DAY_2.date()) == Decimal("11")

    def test_future_sessions_cannot_affect_it(self) -> None:
        tomorrow = session(DAY_3, *([VIOLENT] * 16))
        assert build(history() + DAY_2_QUIET + tomorrow).prior_atr(DAY_2.date()) == Decimal("11")


class TestWarmup:
    def test_fewer_than_fifteen_prior_15m_bars_is_explicitly_none(self) -> None:
        assert build(history(14) + DAY_2_QUIET).prior_atr(DAY_2.date()) is None
        assert build(history(15) + DAY_2_QUIET).prior_atr(DAY_2.date()) == Decimal("10")

    def test_an_incomplete_15m_slot_is_left_out_not_filled(self) -> None:
        """Dropping one minute of the TR-24 slot removes that slot: 15 bars remain
        and the ATR is the seed, 10, rather than a value built on a guessed bar."""
        missing_one = [m for m in history() if m.start_at != DAY_1 + M15.delta * 15 + M1.delta * 7]
        assert build(missing_one + DAY_2_QUIET).prior_atr(DAY_2.date()) == Decimal("10")

    def test_the_same_input_always_gives_the_same_atr(self) -> None:
        again = build(history() + DAY_2_QUIET)
        assert again.prior_atr(DAY_2.date()) == BASELINE.prior_atr(DAY_2.date())
        assert again.fingerprint() == BASELINE.fingerprint()


class TestIdentity:
    def test_v1_fingerprints_are_unchanged(self) -> None:
        assert golden_input().fingerprint() == GOLDEN_V1_FINGERPRINT
        assert "atr_interval" not in OrbParams().canonical()

    def test_v1_and_v2_over_the_same_bars_are_different_runs(self) -> None:
        minutes = history() + DAY_2_QUIET
        v1, v2 = build(minutes, OrbParams()), build(minutes, ORB_V2)
        assert v1.fingerprint() != v2.fingerprint()
        p1, p2 = v1.canonical_payload(), v2.canonical_payload()
        assert p2["strategy_params"] == {**p1["strategy_params"], "atr_interval": "15m"}  # type: ignore[dict-item]
        assert p1["prior_atr"]["source"] == "completed signal bars strictly before the session"  # type: ignore[index]
        assert p2["prior_atr"]["source"] == (  # type: ignore[index]
            "complete 15m bars aggregated from 1m bars strictly before the session"
        )

    def test_manifests_name_the_hypothesis_version(self) -> None:
        at = datetime(2026, 9, 1, tzinfo=UTC)
        minutes = history() + DAY_2_QUIET
        v1 = run_backtest(
            build(minutes, OrbParams()), starting_capital=Decimal("500000"), generated_at=at
        )
        v2 = run_backtest(
            build(minutes, ORB_V2), starting_capital=Decimal("500000"), generated_at=at
        )
        assert (v1.manifest.strategy_name, v1.manifest.strategy_version) == ("orb", "1")
        assert (v2.manifest.strategy_name, v2.manifest.strategy_version) == ("orb", "2")
        assert v1.manifest.input_fingerprint != v2.manifest.input_fingerprint
        assert (OrbStrategy().version, OrbStrategy(ORB_V2).version) == ("1", "2")


class TestHypothesisDefinition:
    def test_v2_differs_from_v1_only_in_the_atr_bars(self) -> None:
        assert dataclasses.replace(ORB_V2, atr_interval=None) == OrbParams()
        assert (OrbParams().atr_bars, ORB_V2.atr_bars) == (M5, M15)
        assert ORB_V2.max_range_atr_multiple == Decimal("1.5")

    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param({"atr_interval": M5}, id="5m-is-v1-spelled-differently"),
            pytest.param({"atr_interval": M1}, id="finer-than-signal"),
            pytest.param(
                {"atr_interval": M15, "signal_interval": M15, "resolution_interval": M5},
                id="15m-over-15m-signal-bars",
            ),
        ],
    )
    def test_other_atr_intervals_are_refused(self, kwargs: dict[str, object]) -> None:
        with pytest.raises(ValueError, match="atr_interval must be None"):
            OrbParams(**kwargs)  # type: ignore[arg-type]
