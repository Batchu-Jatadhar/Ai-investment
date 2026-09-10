"""The execution seam: what the simulator is asked to do, before it does it."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.core.time import NaiveDatetimeError
from app.domain.backtest.config import ExecutionConfig, SlippageConfig
from app.domain.backtest.execution import (
    EntryOutcome,
    ExecutionIntent,
    ExecutionStatus,
    ExitResolution,
    UnexecutableBarError,
    resolve_entry_fill,
    resolve_exit_fill,
    resolve_stop_fill,
    resolve_target_fill,
)
from app.domain.backtest.models import AmbiguityResolution, Fill, FillReason, OrderSide
from app.domain.market.models import CandleInterval, CandleStatus
from app.domain.market.ports import DataGap
from app.domain.strategy.contract import Signal, SignalDirection
from tests.backtest.conftest import RELIANCE_TOKEN, SESSION_OPEN, make_candle

#: The signal fires on the 09:15 bar; the entry lands on the 09:20 bar.
NEXT_BAR = SESSION_OPEN + timedelta(minutes=5)


def make_signal(direction: SignalDirection = SignalDirection.LONG) -> Signal:
    return Signal(
        instrument_token=RELIANCE_TOKEN,
        direction=direction,
        stop_price=Decimal("1390.00") if direction.is_long else Decimal("1412.00"),
        target_r_multiple=Decimal("2"),
        signal_bar_start=SESSION_OPEN,
        reason="LONG_ORB_BREAKOUT" if direction.is_long else "SHORT_ORB_BREAKOUT",
    )


def make_intent(**overrides: object) -> ExecutionIntent:
    values: dict[str, object] = {
        "signal": make_signal(),
        "quantity": 70,
        "entry_bar_start": NEXT_BAR,
    }
    values.update(overrides)
    return ExecutionIntent(**values)  # type: ignore[arg-type]


class TestConstruction:
    def test_a_valid_intent_carries_the_signal_and_the_entry_bar(self) -> None:
        intent = make_intent()
        assert intent.quantity == 70
        assert intent.entry_bar_start == NEXT_BAR
        assert intent.instrument_token == RELIANCE_TOKEN
        assert intent.direction is SignalDirection.LONG

    def test_it_is_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            make_intent().quantity = 1  # type: ignore[misc]

    @pytest.mark.parametrize("quantity", [0, -1])
    def test_a_non_positive_quantity_is_rejected(self, quantity: int) -> None:
        with pytest.raises(ValueError, match="quantity must be positive"):
            make_intent(quantity=quantity)

    def test_a_naive_entry_bar_is_rejected(self) -> None:
        with pytest.raises(NaiveDatetimeError):
            make_intent(entry_bar_start=datetime(2026, 8, 21, 3, 50))


class TestEntryTiming:
    """The approved model's first rule, made unrepresentable to break."""

    def test_entering_on_the_signal_bar_is_rejected(self) -> None:
        """The bar's prices have already printed by the time it closes."""
        with pytest.raises(ValueError, match="strictly after the signal bar"):
            make_intent(entry_bar_start=SESSION_OPEN)

    def test_entering_before_the_signal_bar_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="strictly after the signal bar"):
            make_intent(entry_bar_start=SESSION_OPEN - timedelta(minutes=5))


class TestSides:
    def test_a_long_buys_in_and_sells_out(self) -> None:
        intent = make_intent()
        assert intent.entry_side is OrderSide.BUY
        assert intent.exit_side is OrderSide.SELL

    def test_a_short_sells_in_and_buys_out(self) -> None:
        intent = make_intent(signal=make_signal(SignalDirection.SHORT))
        assert intent.entry_side is OrderSide.SELL
        assert intent.exit_side is OrderSide.BUY


class TestShape:
    def test_it_holds_nothing_broker_specific(self) -> None:
        """A backtest has no orders, only assumptions about how one would have
        filled. A field that could hold a broker's order id would invite a
        live-trading path to grow through the simulator."""
        forbidden = {
            "order_id",
            "broker_order_id",
            "exchange_order_id",
            "order_type",
            "product",
            "variety",
            "account",
            "venue",
            "entry_price",
            "price",
        }
        names = {f.name for f in dataclasses.fields(ExecutionIntent)}
        assert names & forbidden == set()
        assert names == {"signal", "quantity", "entry_bar_start"}

    def test_equal_intents_compare_equal(self) -> None:
        """Value semantics, so a replayed run's intents can be diffed."""
        assert make_intent() == make_intent()
        assert make_intent() != make_intent(quantity=71)


# --------------------------------------------------------------------------- #
# Protective stop execution
# --------------------------------------------------------------------------- #

TICK = Decimal("0.05")
ONE_TICK = SlippageConfig()
NO_SLIPPAGE = SlippageConfig(adverse_ticks=0)
THREE_TICKS = SlippageConfig(adverse_ticks=3)

LONG_STOP = Decimal("1390.00")
SHORT_STOP = Decimal("1412.00")


def position_bar(index: int = 0, *, open_: str, high: str, low: str, close: str) -> object:
    """A bar of the open position's life; index 0 is the entry bar."""
    return make_candle(
        NEXT_BAR + CandleInterval.M5.delta * index,
        CandleInterval.M5,
        open_=open_,
        high=high,
        low=low,
        close=close,
    )


def short_intent() -> ExecutionIntent:
    return make_intent(signal=make_signal(SignalDirection.SHORT))


def stop_fill(intent, bar, slippage=ONE_TICK):  # noqa: ANN001, ANN201
    return resolve_stop_fill(intent, bar, tick_size=TICK, slippage=slippage)


class TestLongStop:
    def test_a_touch_fills_at_the_stop(self) -> None:
        bar = position_bar(open_="1400", high="1405", low="1388", close="1395")
        fill = stop_fill(make_intent(), bar)

        assert fill is not None
        assert fill.side is OrderSide.SELL
        assert fill.reason is FillReason.STOP
        assert fill.quantity == 70
        assert fill.reference_price == LONG_STOP
        assert fill.price == Decimal("1389.95")
        assert fill.bar_start == bar.start_at

    def test_reaching_the_stop_exactly_is_a_touch(self) -> None:
        """The order is resting at that price, not one tick past it."""
        bar = position_bar(open_="1400", high="1405", low="1390", close="1398")
        fill = stop_fill(make_intent(), bar)
        assert fill is not None
        assert fill.reference_price == LONG_STOP

    def test_a_bar_that_stays_above_the_stop_survives(self) -> None:
        bar = position_bar(open_="1400", high="1408", low="1390.05", close="1405")
        assert stop_fill(make_intent(), bar) is None

    def test_a_gap_below_the_stop_fills_at_the_opening_price(self) -> None:
        """There was no trade at the stop level to be had. Crediting one would
        be the single most flattering error a backtest can make."""
        bar = position_bar(open_="1380", high="1385", low="1375", close="1378")
        fill = stop_fill(make_intent(), bar)

        assert fill is not None
        assert fill.reference_price == Decimal("1380")
        assert fill.price == Decimal("1379.95")
        assert fill.occurred_at == bar.start_at


class TestShortStop:
    def test_a_touch_fills_at_the_stop(self) -> None:
        bar = position_bar(open_="1400", high="1415", low="1398", close="1405")
        fill = stop_fill(short_intent(), bar)

        assert fill is not None
        assert fill.side is OrderSide.BUY
        assert fill.reason is FillReason.STOP
        assert fill.reference_price == SHORT_STOP
        assert fill.price == Decimal("1412.05")

    def test_a_bar_that_stays_below_the_stop_survives(self) -> None:
        bar = position_bar(open_="1400", high="1411.95", low="1395", close="1405")
        assert stop_fill(short_intent(), bar) is None

    def test_a_gap_above_the_stop_fills_at_the_opening_price(self) -> None:
        bar = position_bar(open_="1420", high="1425", low="1418", close="1422")
        fill = stop_fill(short_intent(), bar)

        assert fill is not None
        assert fill.reference_price == Decimal("1420")
        assert fill.price == Decimal("1420.05")
        assert fill.occurred_at == bar.start_at


class TestSlippageIsAlwaysAdverse:
    def test_a_long_exits_lower_and_a_short_higher(self) -> None:
        """Three ticks is 0.15, and it moves against the position both ways."""
        touched_long = position_bar(open_="1400", high="1405", low="1388", close="1395")
        touched_short = position_bar(open_="1400", high="1415", low="1398", close="1405")

        assert stop_fill(make_intent(), touched_long, THREE_TICKS).price == Decimal("1389.85")
        assert stop_fill(short_intent(), touched_short, THREE_TICKS).price == Decimal("1412.15")

    def test_without_slippage_the_fill_is_the_reference_price(self) -> None:
        bar = position_bar(open_="1400", high="1405", low="1388", close="1395")
        fill = stop_fill(make_intent(), bar, NO_SLIPPAGE)

        assert fill is not None
        assert fill.price == fill.reference_price == LONG_STOP
        assert fill.slippage_per_unit == Decimal("0")

    def test_the_fill_is_always_the_reference_moved_by_the_slippage(self) -> None:
        """The audit identity Fill's reference_price exists for."""
        for intent, bar in (
            (make_intent(), position_bar(open_="1400", high="1405", low="1388", close="1395")),
            (make_intent(), position_bar(open_="1380", high="1385", low="1375", close="1378")),
            (short_intent(), position_bar(open_="1400", high="1415", low="1398", close="1405")),
            (short_intent(), position_bar(open_="1420", high="1425", low="1418", close="1422")),
        ):
            fill = stop_fill(intent, bar, THREE_TICKS)
            assert fill is not None
            moved = (
                fill.reference_price - fill.slippage_per_unit
                if intent.direction.is_long
                else fill.reference_price + fill.slippage_per_unit
            )
            assert fill.price == moved


class TestTheStopCannotBeSkipped:
    def test_a_bar_that_dips_through_and_recovers_still_fills(self) -> None:
        """The recovery is only visible with hindsight the position did not
        have. The order was resting in the book when the low printed."""
        bar = position_bar(open_="1400", high="1406", low="1385", close="1402")
        fill = stop_fill(make_intent(), bar)

        assert fill is not None
        assert fill.reference_price == LONG_STOP

    def test_a_short_spike_through_and_back_still_fills(self) -> None:
        bar = position_bar(open_="1400", high="1420", low="1396", close="1401")
        fill = stop_fill(short_intent(), bar)

        assert fill is not None
        assert fill.reference_price == SHORT_STOP


class TestNoFutureBarPeeking:
    def test_a_surviving_bar_does_not_anticipate_the_next_one(self) -> None:
        """The resolver is handed one bar and no series, so it cannot consult
        what came after even in principle. This checks the consequence."""
        intent = make_intent()
        survives = position_bar(0, open_="1400", high="1408", low="1391", close="1405")
        blows_through = position_bar(1, open_="1392", high="1393", low="1370", close="1375")

        assert stop_fill(intent, survives) is None
        assert stop_fill(intent, blows_through) is not None

    def test_the_same_bar_always_resolves_the_same_way(self) -> None:
        intent = make_intent()
        bar = position_bar(open_="1400", high="1405", low="1388", close="1395")
        assert stop_fill(intent, bar) == stop_fill(intent, bar)


class TestGuards:
    def test_an_in_progress_bar_is_rejected(self) -> None:
        bar = make_candle(
            NEXT_BAR,
            CandleInterval.M5,
            open_="1400",
            high="1405",
            low="1388",
            close="1395",
            status=CandleStatus.IN_PROGRESS,
        )
        with pytest.raises(ValueError, match="high and low can still move"):
            stop_fill(make_intent(), bar)

    def test_a_bar_before_the_entry_bar_is_rejected(self) -> None:
        """A protective stop cannot fill before the position it protects."""
        bar = position_bar(-1, open_="1400", high="1405", low="1388", close="1395")
        with pytest.raises(ValueError, match="precedes the entry bar"):
            stop_fill(make_intent(), bar)


# --------------------------------------------------------------------------- #
# Engine-fired target execution
# --------------------------------------------------------------------------- #

THROUGH_ONE_TICK = ExecutionConfig()
THROUGH_NOTHING = ExecutionConfig(target_requires_through_ticks=0)
THROUGH_TWO_TICKS = ExecutionConfig(target_requires_through_ticks=2)

#: A long entered at 1400.00 with its stop at 1390.00 risks 10.00, so a 2R
#: target sits at 1420.00 and the default trigger one tick through is 1420.05.
LONG_TARGET = Decimal("1420.00")

#: A short entered at 1400.00 with its stop at 1412.00 risks 12.00, so a 2R
#: target sits at 1376.00 and the default trigger is 1375.95.
SHORT_TARGET = Decimal("1376.00")


def entry_fill(side: OrderSide = OrderSide.BUY) -> Fill:
    return Fill(
        side=side,
        reason=FillReason.ENTRY,
        quantity=70,
        price=Decimal("1400.00"),
        reference_price=Decimal("1400.00"),
        slippage_per_unit=Decimal("0"),
        costs=Decimal("0"),
        occurred_at=NEXT_BAR,
        bar_start=NEXT_BAR,
    )


def target_fill(  # noqa: ANN201
    intent,  # noqa: ANN001
    bar,  # noqa: ANN001
    execution=THROUGH_ONE_TICK,  # noqa: ANN001
    slippage=ONE_TICK,  # noqa: ANN001
):
    entry = entry_fill(OrderSide.BUY if intent.direction.is_long else OrderSide.SELL)
    return resolve_target_fill(
        intent, entry, bar, tick_size=TICK, execution=execution, slippage=slippage
    )


class TestLongTarget:
    def test_trading_through_the_target_fills(self) -> None:
        bar = position_bar(open_="1405", high="1425", low="1404", close="1422")
        fill = target_fill(make_intent(), bar)

        assert fill is not None
        assert fill.side is OrderSide.SELL
        assert fill.reason is FillReason.TARGET
        assert fill.quantity == 70
        assert fill.reference_price == LONG_TARGET
        assert fill.price == Decimal("1419.95")

    def test_reaching_the_target_exactly_does_not_fill(self) -> None:
        """The engine fires on a through-print, and there was not one."""
        bar = position_bar(open_="1405", high="1420.00", low="1404", close="1418")
        assert target_fill(make_intent(), bar) is None

    def test_stopping_one_tick_short_of_the_trigger_does_not_fill(self) -> None:
        bar = position_bar(open_="1405", high="1420.04", low="1404", close="1418")
        assert target_fill(make_intent(), bar) is None

    def test_reaching_the_trigger_exactly_fills(self) -> None:
        """Through by exactly the configured threshold is through by the
        configured threshold."""
        bar = position_bar(open_="1405", high="1420.05", low="1404", close="1419")
        fill = target_fill(make_intent(), bar)

        assert fill is not None
        assert fill.reference_price == LONG_TARGET

    def test_a_favourable_gap_is_not_credited_beyond_the_target(self) -> None:
        """Taking the gap would assume the engine reacted faster than modelled.
        The mirror of the stop, which is filled at the worse price a gap opened
        at - neither exit is credited with the good half of a surprise."""
        bar = position_bar(open_="1450", high="1460", low="1448", close="1455")
        fill = target_fill(make_intent(), bar)

        assert fill is not None
        assert fill.reference_price == LONG_TARGET
        assert fill.price == Decimal("1419.95")


class TestShortTarget:
    def test_trading_through_the_target_fills(self) -> None:
        bar = position_bar(open_="1395", high="1396", low="1370", close="1374")
        fill = target_fill(short_intent(), bar)

        assert fill is not None
        assert fill.side is OrderSide.BUY
        assert fill.reason is FillReason.TARGET
        assert fill.reference_price == SHORT_TARGET
        assert fill.price == Decimal("1376.05")

    def test_reaching_the_target_exactly_does_not_fill(self) -> None:
        bar = position_bar(open_="1395", high="1396", low="1376.00", close="1380")
        assert target_fill(short_intent(), bar) is None

    def test_reaching_the_trigger_exactly_fills(self) -> None:
        bar = position_bar(open_="1395", high="1396", low="1375.95", close="1380")
        fill = target_fill(short_intent(), bar)

        assert fill is not None
        assert fill.reference_price == SHORT_TARGET


class TestTheThresholdFollowsTheConfiguration:
    def test_zero_through_ticks_makes_a_touch_enough(self) -> None:
        """Resting-limit behaviour, if a run chooses to assume it."""
        bar = position_bar(open_="1405", high="1420.00", low="1404", close="1418")

        assert target_fill(make_intent(), bar, THROUGH_NOTHING) is not None
        assert target_fill(make_intent(), bar, THROUGH_ONE_TICK) is None

    def test_two_through_ticks_needs_the_extra_tick(self) -> None:
        one_through = position_bar(open_="1405", high="1420.05", low="1404", close="1419")
        two_through = position_bar(open_="1405", high="1420.10", low="1404", close="1419")

        assert target_fill(make_intent(), one_through, THROUGH_TWO_TICKS) is None
        assert target_fill(make_intent(), two_through, THROUGH_TWO_TICKS) is not None


class TestTargetSlippage:
    def test_slippage_is_adverse_in_both_directions(self) -> None:
        long_bar = position_bar(open_="1405", high="1425", low="1404", close="1422")
        short_bar = position_bar(open_="1395", high="1396", low="1370", close="1374")

        assert target_fill(make_intent(), long_bar, slippage=THREE_TICKS).price == Decimal(
            "1419.85"
        )
        assert target_fill(short_intent(), short_bar, slippage=THREE_TICKS).price == Decimal(
            "1376.15"
        )

    def test_without_slippage_the_fill_is_the_target(self) -> None:
        bar = position_bar(open_="1405", high="1425", low="1404", close="1422")
        fill = target_fill(make_intent(), bar, slippage=NO_SLIPPAGE)

        assert fill is not None
        assert fill.price == fill.reference_price == LONG_TARGET


class TestTargetDeterminismAndGuards:
    def test_the_same_bar_always_resolves_the_same_way(self) -> None:
        bar = position_bar(open_="1405", high="1425", low="1404", close="1422")
        assert target_fill(make_intent(), bar) == target_fill(make_intent(), bar)

    def test_an_exit_fill_cannot_stand_in_for_the_entry(self) -> None:
        """R is measured from the price actually paid to open the position."""
        bar = position_bar(open_="1405", high="1425", low="1404", close="1422")
        not_an_entry = dataclasses.replace(entry_fill(), reason=FillReason.STOP)

        with pytest.raises(ValueError, match="must be an ENTRY fill"):
            resolve_target_fill(
                make_intent(),
                not_an_entry,
                bar,
                tick_size=TICK,
                execution=THROUGH_ONE_TICK,
                slippage=ONE_TICK,
            )

    def test_an_entry_at_the_stop_has_no_target(self) -> None:
        """Zero risk means no R multiple describes anything."""
        bar = position_bar(open_="1405", high="1425", low="1404", close="1422")
        at_the_stop = dataclasses.replace(entry_fill(), price=LONG_STOP, reference_price=LONG_STOP)

        with pytest.raises(ValueError, match="risk is zero"):
            resolve_target_fill(
                make_intent(),
                at_the_stop,
                bar,
                tick_size=TICK,
                execution=THROUGH_ONE_TICK,
                slippage=ONE_TICK,
            )


# --------------------------------------------------------------------------- #
# Gap and invalid-data handling
# --------------------------------------------------------------------------- #


def entry_bar(**overrides: object) -> object:
    """The bar named by the intent, so an entry can be resolved against it."""
    values: dict[str, object] = {
        "open_": "1405",
        "high": "1410",
        "low": "1403",
        "close": "1408",
    }
    values.update(overrides)
    return make_candle(NEXT_BAR, CandleInterval.M5, **values)  # type: ignore[arg-type]


def gap_over(bar, **overrides: object) -> DataGap:  # noqa: ANN001
    values: dict[str, object] = {
        "provider": "test",
        "started_at": bar.start_at,
        "ended_at": bar.end_at,
        "reason": "socket reconnect",
    }
    values.update(overrides)
    return DataGap(**values)  # type: ignore[arg-type]


class TestEntryExecution:
    def test_an_entry_fills_at_the_next_bar_opening_price(self) -> None:
        outcome = resolve_entry_fill(make_intent(), entry_bar(), tick_size=TICK, slippage=ONE_TICK)

        assert outcome.status is ExecutionStatus.FILLED
        assert outcome.fill is not None
        assert outcome.fill.side is OrderSide.BUY
        assert outcome.fill.reason is FillReason.ENTRY
        assert outcome.fill.reference_price == Decimal("1405")
        assert outcome.fill.price == Decimal("1405.05")

    def test_a_short_entry_sells_down(self) -> None:
        outcome = resolve_entry_fill(short_intent(), entry_bar(), tick_size=TICK, slippage=ONE_TICK)

        assert outcome.fill is not None
        assert outcome.fill.side is OrderSide.SELL
        assert outcome.fill.price == Decimal("1404.95")

    def test_a_signal_with_no_next_bar_reports_no_execution_bar(self) -> None:
        """The session ended before the entry bar existed. An ordinary
        end-of-day outcome, and nothing is fabricated for it."""
        outcome = resolve_entry_fill(make_intent(), None, tick_size=TICK, slippage=ONE_TICK)

        assert outcome == EntryOutcome(ExecutionStatus.NO_EXECUTION_BAR, None)
        assert outcome.fill is None

    def test_entering_on_a_bar_the_intent_did_not_name_is_rejected(self) -> None:
        wrong = make_candle(
            NEXT_BAR + CandleInterval.M5.delta,
            CandleInterval.M5,
            open_="1405",
            high="1410",
            low="1403",
            close="1408",
        )
        with pytest.raises(ValueError, match="but the intent names"):
            resolve_entry_fill(make_intent(), wrong, tick_size=TICK, slippage=ONE_TICK)

    def test_an_outcome_cannot_misreport_itself(self) -> None:
        with pytest.raises(ValueError, match="disagree"):
            EntryOutcome(ExecutionStatus.FILLED, None)
        with pytest.raises(ValueError, match="disagree"):
            EntryOutcome(ExecutionStatus.NO_EXECUTION_BAR, entry_fill())


class TestUnusableBars:
    """A bar that cannot answer the question is not the same as a bar that
    answers "no", and the two must not both come back as silence."""

    def test_a_zero_volume_bar_cannot_establish_an_entry(self) -> None:
        with pytest.raises(UnexecutableBarError) as caught:
            resolve_entry_fill(
                make_intent(), entry_bar(volume=0), tick_size=TICK, slippage=ONE_TICK
            )
        assert caught.value.status is ExecutionStatus.NO_VOLUME

    def test_a_zero_volume_bar_cannot_establish_a_stop(self) -> None:
        """Without this the stop would fill at a price nobody paid."""
        bar = position_bar(open_="1400", high="1405", low="1388", close="1395")
        assert stop_fill(make_intent(), bar) is not None

        tradeless = dataclasses.replace(bar, volume=0)
        with pytest.raises(UnexecutableBarError) as caught:
            stop_fill(make_intent(), tradeless)
        assert caught.value.status is ExecutionStatus.NO_VOLUME

    def test_a_zero_volume_bar_cannot_establish_a_target(self) -> None:
        bar = position_bar(open_="1405", high="1425", low="1404", close="1422")
        assert target_fill(make_intent(), bar) is not None

        tradeless = dataclasses.replace(bar, volume=0)
        with pytest.raises(UnexecutableBarError) as caught:
            target_fill(make_intent(), tradeless)
        assert caught.value.status is ExecutionStatus.NO_VOLUME

    def test_a_flat_tradeless_bar_is_reported_as_no_range(self) -> None:
        """The more specific fact: it neither moved nor traded, so it is a
        placeholder rather than a bar."""
        placeholder = entry_bar(open_="1405", high="1405", low="1405", close="1405", volume=0)

        with pytest.raises(UnexecutableBarError) as caught:
            resolve_entry_fill(make_intent(), placeholder, tick_size=TICK, slippage=ONE_TICK)
        assert caught.value.status is ExecutionStatus.NO_RANGE

    def test_a_flat_bar_that_did_trade_is_still_executable(self) -> None:
        """ "Where execution cannot be established" is a real qualifier. One
        price printed and it is a real one - an instrument that traded once in
        five minutes is thin, not corrupt, and discarding it would throw away
        data the market actually made."""
        thin = entry_bar(open_="1405", high="1405", low="1405", close="1405", volume=1)
        outcome = resolve_entry_fill(make_intent(), thin, tick_size=TICK, slippage=ONE_TICK)

        assert outcome.status is ExecutionStatus.FILLED
        assert outcome.fill is not None
        assert outcome.fill.reference_price == Decimal("1405")


class TestRecordedDataGaps:
    def test_a_bar_inside_a_recorded_gap_cannot_be_executed_on(self) -> None:
        """The feed was not delivering, so the bar's extremes are whatever
        happened to arrive rather than what traded."""
        bar = entry_bar()
        with pytest.raises(UnexecutableBarError) as caught:
            resolve_entry_fill(
                make_intent(),
                bar,
                tick_size=TICK,
                slippage=ONE_TICK,
                gaps=[gap_over(bar)],
            )
        assert caught.value.status is ExecutionStatus.INSIDE_DATA_GAP

    def test_a_stop_will_not_fill_from_a_bar_inside_a_gap(self) -> None:
        bar = position_bar(open_="1400", high="1405", low="1388", close="1395")
        with pytest.raises(UnexecutableBarError):
            resolve_stop_fill(
                make_intent(),
                bar,
                tick_size=TICK,
                slippage=ONE_TICK,
                gaps=[gap_over(bar)],
            )

    def test_a_gap_that_ends_before_the_bar_starts_does_not_taint_it(self) -> None:
        """Touching endpoints do not overlap: a gap that closed exactly as the
        bar opened left the bar with a complete feed."""
        bar = entry_bar()
        earlier = gap_over(
            bar, started_at=bar.start_at - timedelta(minutes=10), ended_at=bar.start_at
        )

        outcome = resolve_entry_fill(
            make_intent(), bar, tick_size=TICK, slippage=ONE_TICK, gaps=[earlier]
        )
        assert outcome.status is ExecutionStatus.FILLED

    def test_a_gap_recorded_for_another_instrument_is_ignored(self) -> None:
        bar = entry_bar()
        elsewhere = gap_over(bar, instrument_tokens=(408065,))

        outcome = resolve_entry_fill(
            make_intent(), bar, tick_size=TICK, slippage=ONE_TICK, gaps=[elsewhere]
        )
        assert outcome.status is ExecutionStatus.FILLED

    def test_a_gap_naming_this_instrument_still_bites(self) -> None:
        bar = entry_bar()
        ours = gap_over(bar, instrument_tokens=(RELIANCE_TOKEN,))

        with pytest.raises(UnexecutableBarError):
            resolve_entry_fill(make_intent(), bar, tick_size=TICK, slippage=ONE_TICK, gaps=[ours])


class TestGapThroughStopStillFillsAtTheOpen:
    def test_a_stop_gapped_through_fills_at_the_opening_price(self) -> None:
        """Held here as well as with the stop itself: this is the one gap case
        that must still produce a fill, and hardening the others must not have
        quietly turned it into a refusal."""
        bar = position_bar(open_="1380", high="1385", low="1375", close="1378")
        fill = stop_fill(make_intent(), bar)

        assert fill is not None
        assert fill.reference_price == Decimal("1380")
        assert fill.occurred_at == bar.start_at


class TestNothingIsManufacturedSilently:
    def test_every_invalid_case_is_explicit_rather_than_silent(self) -> None:
        """The point of the whole group: a bar that cannot answer must never
        come back looking like a bar that answered "no"."""
        would_have_filled = position_bar(open_="1400", high="1405", low="1388", close="1395")
        assert stop_fill(make_intent(), would_have_filled) is not None

        for broken in (
            dataclasses.replace(would_have_filled, volume=0),
            dataclasses.replace(
                would_have_filled,
                high=Decimal("1400"),
                low=Decimal("1400"),
                open=Decimal("1400"),
                close=Decimal("1400"),
                volume=0,
            ),
        ):
            with pytest.raises(UnexecutableBarError):
                stop_fill(make_intent(), broken)

    def test_the_same_unusable_bar_always_fails_the_same_way(self) -> None:
        bar = entry_bar(volume=0)
        statuses = set()
        for _ in range(3):
            with pytest.raises(UnexecutableBarError) as caught:
                resolve_entry_fill(make_intent(), bar, tick_size=TICK, slippage=ONE_TICK)
            statuses.add(caught.value.status)
        assert statuses == {ExecutionStatus.NO_VOLUME}


# --------------------------------------------------------------------------- #
# Same-bar stop/target ambiguity
# --------------------------------------------------------------------------- #

#: A 5m bar that reached the stop (low 1385 <= 1390) *and* traded through the
#: target (high 1425 >= 1420.05). On its own it says nothing about the order,
#: and the difference is -1R against +2R.
AMBIGUOUS_BAR = position_bar(open_="1400", high="1425", low="1385", close="1410")


def minute(index: int, *, open_: str, high: str, low: str, close: str, offset: int = 0):  # noqa: ANN201
    """The ``index``-th 1m bar of the 5m window starting ``offset`` windows on."""
    start = NEXT_BAR + CandleInterval.M5.delta * offset + CandleInterval.M1.delta * index
    return make_candle(start, CandleInterval.M1, open_=open_, high=high, low=low, close=close)


QUIET = {"open_": "1400", "high": "1402", "low": "1398", "close": "1400"}
STOP_MINUTE = {"open_": "1400", "high": "1401", "low": "1385", "close": "1392"}
TARGET_MINUTE = {"open_": "1400", "high": "1425", "low": "1399", "close": "1424"}
BOTH_MINUTE = {"open_": "1400", "high": "1425", "low": "1385", "close": "1400"}

#: Stop in minute 1, target not until minute 3.
STOP_FIRST = (
    minute(0, **QUIET),
    minute(1, **STOP_MINUTE),
    minute(2, **QUIET),
    minute(3, **TARGET_MINUTE),
    minute(4, **QUIET),
)

#: Target in minute 1, stop not until minute 3.
TARGET_FIRST = (
    minute(0, **QUIET),
    minute(1, **TARGET_MINUTE),
    minute(2, **QUIET),
    minute(3, **STOP_MINUTE),
    minute(4, **QUIET),
)


def exit_res(  # noqa: ANN201
    intent,  # noqa: ANN001
    bar,  # noqa: ANN001
    minute_bars=(),  # noqa: ANN001
    execution=THROUGH_ONE_TICK,  # noqa: ANN001
    slippage=ONE_TICK,  # noqa: ANN001
):
    entry = entry_fill(OrderSide.BUY if intent.direction.is_long else OrderSide.SELL)
    return resolve_exit_fill(
        intent,
        entry,
        bar,
        tick_size=TICK,
        execution=execution,
        slippage=slippage,
        minute_bars=minute_bars,
    )


class TestUnambiguousBars:
    """Nothing to resolve, so nothing is recorded as resolved."""

    def test_only_the_stop_reached(self) -> None:
        bar = position_bar(open_="1400", high="1405", low="1388", close="1395")
        result = exit_res(make_intent(), bar, STOP_FIRST)

        assert result.ambiguity is AmbiguityResolution.UNAMBIGUOUS
        assert result.fill is not None
        assert result.fill.reason is FillReason.STOP

    def test_only_the_target_reached(self) -> None:
        bar = position_bar(open_="1405", high="1425", low="1404", close="1422")
        result = exit_res(make_intent(), bar, STOP_FIRST)

        assert result.ambiguity is AmbiguityResolution.UNAMBIGUOUS
        assert result.fill is not None
        assert result.fill.reason is FillReason.TARGET

    def test_neither_reached(self) -> None:
        bar = position_bar(open_="1400", high="1405", low="1395", close="1402")
        assert exit_res(make_intent(), bar, STOP_FIRST) == ExitResolution(None)


class TestTierOneUsesTheMinuteBars:
    def test_the_stop_coming_first_takes_the_stop(self) -> None:
        result = exit_res(make_intent(), AMBIGUOUS_BAR, STOP_FIRST)

        assert result.ambiguity is AmbiguityResolution.RESOLVED_BY_1M
        assert result.fill is not None
        assert result.fill.reason is FillReason.STOP
        assert result.fill.reference_price == LONG_STOP

    def test_the_target_coming_first_takes_the_target(self) -> None:
        result = exit_res(make_intent(), AMBIGUOUS_BAR, TARGET_FIRST)

        assert result.ambiguity is AmbiguityResolution.RESOLVED_BY_1M
        assert result.fill is not None
        assert result.fill.reason is FillReason.TARGET
        assert result.fill.reference_price == LONG_TARGET

    def test_the_fill_still_comes_from_the_five_minute_bar(self) -> None:
        """The minute bars order the two events; they do not re-price them, so
        the execution model is unchanged by this resolution."""
        resolved = exit_res(make_intent(), AMBIGUOUS_BAR, STOP_FIRST).fill
        direct = stop_fill(make_intent(), AMBIGUOUS_BAR)
        assert resolved == direct


class TestTierTwoAssumesTheStop:
    def test_no_minute_data_falls_back_to_the_stop(self) -> None:
        result = exit_res(make_intent(), AMBIGUOUS_BAR, ())

        assert result.ambiguity is AmbiguityResolution.PESSIMISTIC_FALLBACK
        assert result.fill is not None
        assert result.fill.reason is FillReason.STOP

    def test_partial_minute_coverage_is_treated_as_none(self) -> None:
        """The missing minute is exactly the one that might have held the
        answer, and filling the hole from its neighbours would be inventing the
        ordering rather than reading it."""
        four_of_five = TARGET_FIRST[:4]
        result = exit_res(make_intent(), AMBIGUOUS_BAR, four_of_five)

        assert result.ambiguity is AmbiguityResolution.PESSIMISTIC_FALLBACK
        assert result.fill.reason is FillReason.STOP

    def test_both_levels_inside_one_minute_falls_back(self) -> None:
        """Ambiguous at 1m resolution too. The minute data was read but did not
        decide, so the label names what actually decided."""
        same_minute = (
            minute(0, **QUIET),
            minute(1, **BOTH_MINUTE),
            minute(2, **QUIET),
            minute(3, **QUIET),
            minute(4, **QUIET),
        )
        result = exit_res(make_intent(), AMBIGUOUS_BAR, same_minute)

        assert result.ambiguity is AmbiguityResolution.PESSIMISTIC_FALLBACK
        assert result.fill.reason is FillReason.STOP

    def test_an_unusable_minute_degrades_rather_than_crashing(self) -> None:
        """A minute that cannot answer is not evidence about the ordering."""
        holed = (
            minute(0, **QUIET),
            dataclasses.replace(minute(1, **TARGET_MINUTE), volume=0),
            minute(2, **QUIET),
            minute(3, **STOP_MINUTE),
            minute(4, **QUIET),
        )
        result = exit_res(make_intent(), AMBIGUOUS_BAR, holed)

        assert result.ambiguity is AmbiguityResolution.PESSIMISTIC_FALLBACK
        assert result.fill.reason is FillReason.STOP

    def test_the_target_is_never_assumed(self) -> None:
        """Across every shape of ambiguous bar, with no minute data, the answer
        is always the stop. An engine that guessed favourably here would turn
        its worst data into its best results."""
        shapes = (
            AMBIGUOUS_BAR,
            position_bar(open_="1380", high="1430", low="1375", close="1400"),
            position_bar(open_="1421", high="1440", low="1389", close="1400"),
        )
        for bar in shapes:
            result = exit_res(make_intent(), bar, ())
            assert result.ambiguity is AmbiguityResolution.PESSIMISTIC_FALLBACK
            assert result.fill.reason is FillReason.STOP


class TestNoFutureMinuteData:
    def test_minutes_from_a_later_window_cannot_change_the_answer(self) -> None:
        """The caller may hand over a whole session. Only the minutes inside
        this bar's own window are consulted."""
        later = tuple(
            minute(index, offset=1, **(STOP_MINUTE if index == 0 else QUIET)) for index in range(5)
        )
        with_future = exit_res(make_intent(), AMBIGUOUS_BAR, (*TARGET_FIRST, *later))
        without = exit_res(make_intent(), AMBIGUOUS_BAR, TARGET_FIRST)

        assert with_future == without
        assert with_future.fill.reason is FillReason.TARGET

    def test_a_later_window_alone_is_not_coverage(self) -> None:
        """Minutes that do not lie inside the bar give no coverage at all, so
        the resolution falls back rather than reading the wrong window."""
        later = tuple(minute(index, offset=1, **QUIET) for index in range(5))
        result = exit_res(make_intent(), AMBIGUOUS_BAR, later)
        assert result.ambiguity is AmbiguityResolution.PESSIMISTIC_FALLBACK


class TestResolutionIsDeterministic:
    def test_the_same_inputs_always_resolve_the_same_way(self) -> None:
        for minutes in ((), STOP_FIRST, TARGET_FIRST):
            first = exit_res(make_intent(), AMBIGUOUS_BAR, minutes)
            second = exit_res(make_intent(), AMBIGUOUS_BAR, minutes)
            assert first == second

    def test_the_minute_order_supplied_does_not_matter(self) -> None:
        """They are sorted by time before the walk, so a caller that hands them
        over shuffled gets the same answer as one that does not."""
        shuffled = (STOP_FIRST[3], STOP_FIRST[0], STOP_FIRST[4], STOP_FIRST[1], STOP_FIRST[2])
        assert exit_res(make_intent(), AMBIGUOUS_BAR, shuffled) == exit_res(
            make_intent(), AMBIGUOUS_BAR, STOP_FIRST
        )


class TestResolutionCannotMisreportItself:
    def test_recording_a_tier_without_a_fill_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="nothing to resolve"):
            ExitResolution(None, AmbiguityResolution.RESOLVED_BY_1M)
