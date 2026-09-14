"""ORB v3: session-bar ATR, the reachability filter and the friction filter.

.. rubric:: Hand calculation

Sixteen weekday sessions from 2026-08-03 each hold the full 360 minutes from
09:15 to 15:15 IST. In each, the first minute carries the whole range and every
other minute sits flat at 1000.00, and every close is 1000.00, so each session
bar's true range is its high minus low:

    session 0        1001.00 / 999.00    no previous close, not counted
    sessions 1-7     1004.00 / 996.00    TR  8.00 each
    sessions 8-14    1006.00 / 994.00    TR 12.00 each
    session 15       1012.00 / 988.00    TR 24.00

    seed    (7 x 8.00 + 7 x 12.00) / 14            = 10.00
    Wilder  (10.00 x 13 + 24.00) / 14              = 11.00

So the seventeenth session is handed a session ATR of exactly 11.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest

from app.core.time import IST
from app.domain.backtest.config import NSE_INTRADAY_EQUITY
from app.domain.backtest.costs import estimate_round_trip_friction, leg_charges
from app.domain.backtest.engine import run_backtest
from app.domain.backtest.input import BacktestInput
from app.domain.backtest.models import OrderSide
from app.domain.indicators import (
    IndicatorError,
    SessionBar,
    session_average_true_range,
    session_bars,
)
from app.domain.market.aggregation import aggregate_minutes
from app.domain.market.models import Candle, CandleInterval
from app.domain.market.session import MarketSessionCalendar
from app.domain.strategy.contract import SignalDirection, StrategyContext
from app.domain.strategy.orb import OrbReason, OrbStrategy
from app.domain.strategy.params import ORB_V2, ORB_V3, OrbParams
from tests.backtest.conftest import SESSION_OPEN, make_candle, make_input, make_instrument
from tests.backtest.test_golden_backtest import golden_input
from tests.backtest.test_orb_v2 import BASELINE as V2_BASELINE

M1, M5 = CandleInterval.M1, CandleInterval.M5
CAL = MarketSessionCalendar.nse_equity()
HARD_EXIT = time(15, 15)

#: Fingerprints taken before v3 existed. Neither may move.
GOLDEN_V1_FINGERPRINT = "d6916c9ab779eafa35f5a49c3e7fe2d9da68ccd1b871f1739d57cdc049753eeb"
V2_BASELINE_FINGERPRINT = "26903c5887443458044d85ba7f9d81dc8c89bbda4e1daec692eeabed6d819ea1"

RANGES = (
    ("1001.00", "999.00"),
    *((("1004.00", "996.00"),) * 7),
    *((("1006.00", "994.00"),) * 7),
    ("1012.00", "988.00"),
)
VIOLENT = ("1500.00", "500.00")


def weekdays(start: date, count: int) -> list[date]:
    days, day = [], start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


DAYS = weekdays(date(2026, 8, 3), 18)


def minute(at: datetime, high: str = "1000.00", low: str = "1000.00", close: str = "1000.00"):  # noqa: ANN201
    return make_candle(at, M1, open_="1000.00", high=high, low=low, close=close, volume=100)


def session_minutes(
    day: date, high: str, low: str, *, until: time = HARD_EXIT, after: tuple[str, str] | None = None
) -> list[Candle]:
    """Full coverage from 09:15 to ``until``; the first minute carries the range."""
    opens = datetime.combine(day, time(9, 15), IST).astimezone(UTC)
    count = int(
        (datetime.combine(day, until) - datetime.combine(day, time(9, 15))).total_seconds() // 60
    )
    bars = [minute(opens, high, low)] + [minute(opens + M1.delta * i) for i in range(1, count)]
    if after is not None:  # the 15:15-15:30 quarter-hour, which v3 must ignore
        bars += [minute(opens + M1.delta * i, *after) for i in range(count, count + 15)]
    return bars


def history(sessions: int = 16, **kwargs: object) -> list[Candle]:
    return [
        m
        for day, (h, lo) in zip(DAYS, RANGES[:sessions], strict=False)
        for m in session_minutes(day, h, lo, **kwargs)
    ]  # type: ignore[arg-type]


def build(minutes: list[Candle], params: OrbParams = ORB_V3) -> BacktestInput:
    minutes = sorted(minutes, key=lambda m: m.start_at)
    return make_input(
        candles_5m=aggregate_minutes(minutes, M5).candles,
        candles_1m=tuple(minutes),
        strategy_params=params,
    )


TODAY = DAYS[16]
TODAY_MINUTES = session_minutes(TODAY, "1003.00", "997.00", until=time(10, 0))
BASELINE = build(history() + TODAY_MINUTES)


class TestSessionBars:
    def test_a_bar_is_the_high_low_and_close_of_open_to_the_hard_exit(self) -> None:
        day = DAYS[0]
        opens = datetime.combine(day, time(9, 15), IST).astimezone(UTC)
        bars = [minute(opens + M1.delta * i) for i in range(360)]
        bars[45] = minute(opens + M1.delta * 45, high="1010.00")  # 10:00
        bars[285] = minute(opens + M1.delta * 285, low="990.00")  # 14:00
        bars[359] = minute(opens + M1.delta * 359, close="1005.00")  # 15:14
        late = [minute(opens + M1.delta * i, "2000.00", "1.00") for i in range(360, 375)]

        (bar,) = session_bars(bars + late, CAL, window_end=HARD_EXIT)
        assert bar == SessionBar(day, Decimal("1010.00"), Decimal("990.00"), Decimal("1005.00"))
        assert session_bars(bars, CAL, window_end=HARD_EXIT) == (bar,)  # 15:15-ending data

    def test_a_session_missing_any_minute_of_the_window_gets_no_bar(self) -> None:
        full = session_minutes(DAYS[0], "1001.00", "999.00")
        assert len(session_bars(full, CAL, window_end=HARD_EXIT)) == 1
        missing = full[:200] + full[201:]
        assert session_bars(missing, CAL, window_end=HARD_EXIT) == ()
        short = session_minutes(DAYS[0], "1001.00", "999.00", until=time(15, 0))
        assert session_bars(short, CAL, window_end=HARD_EXIT) == ()

    def test_sessions_come_back_in_order_one_bar_each(self) -> None:
        bars = session_bars(history(3), CAL, window_end=HARD_EXIT)
        assert [b.session for b in bars] == DAYS[:3]


class TestSessionAtr:
    def test_hand_calculated_prior_session_atr(self) -> None:
        assert BASELINE.prior_atr(TODAY) == Decimal("11")
        prior = session_bars(tuple(history()), CAL, window_end=HARD_EXIT)
        assert BASELINE.prior_atr(TODAY) == session_average_true_range(prior)

    def test_fewer_than_fifteen_prior_session_bars_is_none(self) -> None:
        assert build(history(14) + TODAY_MINUTES).prior_atr(DAYS[14]) is None
        assert build(history(15) + TODAY_MINUTES).prior_atr(TODAY) == Decimal("10")

    def test_an_incomplete_prior_session_is_left_out_not_filled(self) -> None:
        last = DAYS[15]
        gap = datetime.combine(last, time(12, 0), IST).astimezone(UTC)
        holed = [m for m in history() if m.start_at != gap]
        assert build(holed + TODAY_MINUTES).prior_atr(TODAY) == Decimal("10")

    def test_session_atr_rejects_too_few_or_unordered_bars(self) -> None:
        bars = session_bars(history(), CAL, window_end=HARD_EXIT)
        with pytest.raises(IndicatorError, match="needs at least 15"):
            session_average_true_range(bars[:14])
        with pytest.raises(IndicatorError, match="ascending"):
            session_average_true_range(tuple(reversed(bars)))


class TestNoLeakage:
    def test_todays_bars_cannot_affect_the_atr(self) -> None:
        wild = session_minutes(TODAY, *VIOLENT, until=time(10, 0))
        assert build(history() + wild).prior_atr(TODAY) == Decimal("11")

    def test_future_sessions_cannot_affect_the_atr(self) -> None:
        tomorrow = session_minutes(DAYS[17], *VIOLENT)
        assert build(history() + TODAY_MINUTES + tomorrow).prior_atr(TODAY) == Decimal("11")

    def test_minutes_after_the_hard_exit_cannot_affect_the_atr(self) -> None:
        assert build(history(after=VIOLENT) + TODAY_MINUTES).prior_atr(TODAY) == Decimal("11")


def context(
    prior_atr: Decimal | None, friction: Callable[[Decimal], Decimal | None] | None
) -> StrategyContext:
    bounds = CAL.session_bounds(SESSION_OPEN)
    assert bounds is not None
    return StrategyContext(
        instrument=make_instrument(),
        calendar=CAL,
        session_open=bounds[0],
        session_close=bounds[1],
        prior_atr=prior_atr,
        round_trip_friction=friction,
    )


def session(breakout_close: str = "1006.00", high: str = "1005.00", low: str = "995.00"):  # noqa: ANN201
    """Opening range 09:15-09:30 of width high - low, then a breakout bar."""
    opening = [
        make_candle(
            SESSION_OPEN + M5.delta * i, open_="1000.00", high=high, low=low, close="1000.00"
        )
        for i in range(3)
    ]
    close = Decimal(breakout_close)
    breakout = make_candle(
        SESSION_OPEN + M5.delta * 3,
        open_="1000.00",
        high=str(max(close, Decimal("1000.00"))),
        low=str(min(close, Decimal("1000.00"))),
        close=breakout_close,
    )
    return (*opening, breakout)


def fixed(value: str | None) -> Callable[[Decimal], Decimal | None]:
    return lambda _price: None if value is None else Decimal(value)


class TestReachabilityFilter:
    """Width 10 with a 2R target needs a session ATR of at least 30."""

    def test_the_boundary_passes_and_a_hair_less_is_too_wide(self) -> None:
        v3 = OrbStrategy(ORB_V3)
        assert (
            v3.evaluate(session(), context(Decimal("30"), fixed("1.00"))).reason
            is OrbReason.LONG_BREAKOUT
        )
        assert (
            v3.evaluate(session(), context(Decimal("29.99"), fixed("0"))).reason
            is OrbReason.RANGE_TOO_WIDE
        )

    def test_no_session_atr_declines(self) -> None:
        decision = OrbStrategy(ORB_V3).evaluate(session(), context(None, fixed("0")))
        assert decision.reason is OrbReason.ATR_UNAVAILABLE

    def test_the_four_tick_floor_still_applies_first(self) -> None:
        narrow = session(
            breakout_close="1000.20", high="1000.10", low="999.95"
        )  # width 0.15 < 0.20
        decision = OrbStrategy(ORB_V3).evaluate(narrow, context(Decimal("100"), fixed("0")))
        assert decision.reason is OrbReason.RANGE_TOO_NARROW


class TestFrictionFilter:
    """Width 10 with max_friction_r 0.10 allows friction up to 1.00 per share."""

    def test_the_boundary_passes_and_a_paisa_more_is_too_high(self) -> None:
        v3 = OrbStrategy(ORB_V3)
        assert (
            v3.evaluate(session(), context(Decimal("40"), fixed("1.00"))).reason
            is OrbReason.LONG_BREAKOUT
        )
        assert (
            v3.evaluate(session(), context(Decimal("40"), fixed("1.01"))).reason
            is OrbReason.FRICTION_TOO_HIGH
        )

    @pytest.mark.parametrize("friction", [None, fixed(None)], ids=["no-estimator", "no-estimate"])
    def test_an_unavailable_estimate_declines(self, friction: object) -> None:
        decision = OrbStrategy(ORB_V3).evaluate(session(), context(Decimal("40"), friction))  # type: ignore[arg-type]
        assert decision.reason is OrbReason.FRICTION_UNAVAILABLE

    @pytest.mark.parametrize(
        ("breakout", "boundary", "direction"),
        [
            ("1006.00", Decimal("1005.00"), SignalDirection.LONG),
            ("994.00", Decimal("995.00"), SignalDirection.SHORT),
        ],
    )
    def test_friction_is_priced_at_the_breakout_boundary(
        self, breakout: str, boundary: Decimal, direction: SignalDirection
    ) -> None:
        asked: list[Decimal] = []

        def record(price: Decimal) -> Decimal:
            asked.append(price)
            return Decimal("0.50")

        decision = OrbStrategy(ORB_V3).evaluate(session(breakout), context(Decimal("40"), record))
        assert decision.signal is not None and decision.signal.direction is direction
        assert asked == [boundary]


class TestEarlierVersionsIgnoreV3Rules:
    def test_v1_and_v2_keep_their_width_multiple_and_ignore_friction(self) -> None:
        """Width 10 against ATR 7: inside 1.5x (v1/v2 trade) but beyond ATR/3 (v3 rejects)."""
        ctx = context(Decimal("7"), None)
        assert OrbStrategy().evaluate(session(), ctx).reason is OrbReason.LONG_BREAKOUT
        assert OrbStrategy(ORB_V2).evaluate(session(), ctx).reason is OrbReason.LONG_BREAKOUT
        assert OrbStrategy(ORB_V3).evaluate(session(), ctx).reason is OrbReason.RANGE_TOO_WIDE


class TestFrictionEstimate:
    def test_statutory_charges_on_both_legs_per_share_plus_two_slippage_legs(self) -> None:
        estimate = estimate_round_trip_friction(
            Decimal("1000.00"),
            schedule=NSE_INTRADAY_EQUITY,
            notional=Decimal("100000"),
            lot_size=1,
            tick_size=Decimal("0.05"),
            adverse_ticks=1,
        )
        turnover = Decimal("100000.00")  # 100 shares at 1000
        charges = (
            leg_charges(NSE_INTRADAY_EQUITY, side=OrderSide.BUY, turnover=turnover).total
            + leg_charges(NSE_INTRADAY_EQUITY, side=OrderSide.SELL, turnover=turnover).total
        )
        assert estimate == charges / 100 + Decimal("0.10")
        assert Decimal("0.90") < estimate < Decimal("1.00")  # about Rs 82.6 per round trip

    def test_no_estimate_when_the_notional_cannot_buy_one_lot(self) -> None:
        assert (
            estimate_round_trip_friction(
                Decimal("150000"),
                schedule=NSE_INTRADAY_EQUITY,
                notional=Decimal("100000"),
                lot_size=1,
                tick_size=Decimal("0.05"),
                adverse_ticks=1,
            )
            is None
        )

    def test_the_input_estimates_with_its_own_costs_notional_and_slippage(self) -> None:
        data = BASELINE
        assert data.round_trip_friction(Decimal("1000.00")) == estimate_round_trip_friction(
            Decimal("1000.00"),
            schedule=data.cost_schedule,
            notional=data.strategy_params.fixed_notional_inr,
            lot_size=data.instrument.lot_size,
            tick_size=data.instrument.tick_size,
            adverse_ticks=data.slippage_config.adverse_ticks,
        )


class TestIdentity:
    def test_v1_and_v2_fingerprints_are_unchanged(self) -> None:
        assert golden_input().fingerprint() == GOLDEN_V1_FINGERPRINT
        assert V2_BASELINE.fingerprint() == V2_BASELINE_FINGERPRINT

    def test_exact_v3_rule_identity(self) -> None:
        assert ORB_V3.hypothesis_version == "3"
        assert ORB_V3.canonical() == {
            **OrbParams().canonical(),
            "session_atr": "true",
            "max_friction_r": "0.1",
        }
        payload = BASELINE.canonical_payload()
        assert payload["prior_atr"] == {
            "method": "wilder",
            "period": "14",
            "source": (
                "session bars from complete 1m coverage of session open to hard_exit_time, "
                "strictly before the session"
            ),
        }
        assert payload["range_filter"] == {
            "reachability": "opening range width x (1 + target_r_multiple) <= prior session ATR",
            "friction": (
                "statutory charges on a buy and a sell leg at fixed_notional_inr and the "
                "opening-range boundary price, per share, plus 2 x adverse_ticks x tick_size, "
                "<= max_friction_r x opening range width"
            ),
        }
        assert (
            "range_filter" not in build(history() + TODAY_MINUTES, OrbParams()).canonical_payload()
        )
        assert "range_filter" not in build(history() + TODAY_MINUTES, ORB_V2).canonical_payload()

    def test_three_versions_over_the_same_bars_are_three_runs_and_v3_is_stable(self) -> None:
        minutes = history() + TODAY_MINUTES
        fingerprints = {build(minutes, p).fingerprint() for p in (OrbParams(), ORB_V2, ORB_V3)}
        assert len(fingerprints) == 3
        assert build(minutes).fingerprint() == BASELINE.fingerprint()

    def test_the_manifest_names_version_3(self) -> None:
        result = run_backtest(
            BASELINE,
            starting_capital=Decimal("500000"),
            generated_at=datetime(2026, 9, 1, tzinfo=UTC),
        )
        assert (result.manifest.strategy_name, result.manifest.strategy_version) == ("orb", "3")
        assert result.manifest.input_fingerprint == BASELINE.fingerprint()
        assert OrbStrategy(ORB_V3).version == "3"

    @pytest.mark.parametrize(
        ("kwargs", "error", "match"),
        [
            ({"session_atr": True}, TypeError, "needs max_friction_r"),
            ({"max_friction_r": Decimal("0.1")}, ValueError, "needs session_atr=True"),
            ({"session_atr": True, "max_friction_r": Decimal("0")}, ValueError, r"in \(0, 1\)"),
            ({"session_atr": True, "max_friction_r": Decimal("1")}, ValueError, r"in \(0, 1\)"),
            (
                {
                    "session_atr": True,
                    "max_friction_r": Decimal("0.1"),
                    "atr_interval": CandleInterval.M15,
                },
                ValueError,
                "cannot be combined",
            ),
        ],
    )
    def test_inconsistent_v3_parameters_are_refused(
        self, kwargs: dict[str, object], error: type[Exception], match: str
    ) -> None:
        with pytest.raises(error, match=match):
            OrbParams(**kwargs)  # type: ignore[arg-type]
