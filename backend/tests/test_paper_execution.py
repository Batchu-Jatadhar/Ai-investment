"""Paper execution: order lifecycle, exits, flatten, invalid transitions, broker isolation."""

from __future__ import annotations

import ast
import pathlib
import socket
from datetime import datetime, time, timedelta
from decimal import Decimal

import httpx
import pytest

from app.adapters.paper import PaperExecutionAdapter
from app.config.settings import TradingMode
from app.domain.backtest.config import (
    NO_COSTS,
    NSE_INTRADAY_EQUITY,
    CostSchedule,
    ExecutionConfig,
    SlippageConfig,
)
from app.domain.backtest.models import FillReason, OrderSide
from app.domain.execution.ports import InvalidTransitionError, OrderStatus
from app.domain.market.models import CandleInterval, CandleStatus
from app.domain.risk.sizing import (
    LimitingConstraint,
    RejectionCode,
    RiskConfig,
    RiskDecision,
    RiskRejection,
    size_position,
)
from app.domain.strategy.contract import Signal, SignalDirection
from app.services.execution import ExecutionModeError, build_execution_port
from tests.backtest.conftest import INFY_TOKEN, SESSION_OPEN, make_candle, make_instrument

M5 = timedelta(minutes=5)
BAR1 = SESSION_OPEN + M5  # the entry bar for a signal on the opening bar
#: 15:10-15:15 IST, the bar whose close reaches the 15:15 hard exit.
HARD_EXIT_BAR = SESSION_OPEN + timedelta(hours=5, minutes=55)
INSTRUMENT = make_instrument()
APPROVED = RiskDecision(quantity=10, limited_by=LimitingConstraint.RISK_BUDGET)


def signal(
    direction: SignalDirection = SignalDirection.LONG,
    stop: str = "1395.00",
    at: datetime = SESSION_OPEN,
) -> Signal:
    return Signal(
        instrument_token=INSTRUMENT.instrument_token,
        direction=direction,
        stop_price=Decimal(stop),
        target_r_multiple=Decimal("2"),
        signal_bar_start=at,
        reason="test breakout",
    )


def adapter(cost_schedule: CostSchedule = NO_COSTS) -> PaperExecutionAdapter:
    return PaperExecutionAdapter(
        instrument=INSTRUMENT,
        starting_capital=Decimal("100000"),
        signal_interval=CandleInterval.M5,
        hard_exit_time=time(15, 15),
        execution=ExecutionConfig(),
        slippage=SlippageConfig(adverse_ticks=1),
        cost_schedule=cost_schedule,
    )


def entered(direction: SignalDirection = SignalDirection.LONG) -> PaperExecutionAdapter:
    paper = adapter()
    stop = "1395.00" if direction.is_long else "1405.00"
    paper.submit("o1", signal(direction, stop), APPROVED)
    paper.on_bar(make_candle(BAR1, open_="1400.00", high="1404.00", low="1397.00"))
    return paper


class TestLifecycle:
    def test_submit_accepts_then_the_next_bar_open_fills(self) -> None:
        paper = adapter()
        record = paper.submit("o1", signal(), APPROVED)
        assert record.status is OrderStatus.ACCEPTED
        assert paper.position is None

        (fill,) = paper.on_bar(make_candle(BAR1, open_="1400.00", high="1404.00", low="1397.00"))
        assert (fill.reason, fill.side, fill.quantity) == (FillReason.ENTRY, OrderSide.BUY, 10)
        assert fill.price == Decimal("1400.05")  # open plus one adverse tick
        assert paper.order("o1").status is OrderStatus.FILLED
        assert paper.order("o1").fill == fill
        assert paper.position is not None and paper.position.quantity == 10
        assert paper.cash == Decimal("100000") - Decimal("14000.50")

    def test_bars_before_the_entry_bar_leave_the_order_working(self) -> None:
        paper = adapter()
        paper.submit("o1", signal(at=BAR1), APPROVED)
        assert paper.on_bar(make_candle(BAR1)) == ()
        assert paper.order("o1").status is OrderStatus.ACCEPTED

    def test_a_missing_entry_bar_expires_the_order(self) -> None:
        paper = adapter()
        paper.submit("o1", signal(), APPROVED)
        assert paper.on_bar(make_candle(BAR1 + M5)) == ()
        assert paper.order("o1").status is OrderStatus.EXPIRED
        assert paper.position is None

    def test_a_rejected_risk_decision_is_a_rejected_order(self) -> None:
        paper = adapter()
        decision = RiskDecision(
            quantity=0,
            rejections=(RiskRejection(RejectionCode.INSUFFICIENT_CAPITAL, "no cash"),),
        )
        record = paper.submit("o1", signal(), decision)
        assert record.status is OrderStatus.REJECTED
        assert record.reasons == ("insufficient_capital",)
        assert paper.on_bar(make_candle(BAR1)) == ()

    def test_a_real_risk_sizing_flows_through(self) -> None:
        decision = size_position(
            RiskConfig(Decimal("0.01")),
            equity=Decimal("100000"),
            available_cash=Decimal("100000"),
            direction=SignalDirection.LONG,
            entry_price=Decimal("1400"),
            stop_price=Decimal("1395"),
            instrument=INSTRUMENT,
        )
        paper = adapter()
        assert paper.submit("o1", signal(), decision).quantity == 71  # min(1000/5, 100000/1400)

    def test_a_stale_signal_is_rejected(self) -> None:
        paper = adapter()
        paper.on_bar(make_candle(BAR1))
        assert paper.submit("o1", signal(), APPROVED).reasons == ("entry_bar_already_passed",)

    def test_another_instrument_is_rejected(self) -> None:
        other = Signal(
            instrument_token=INFY_TOKEN,
            direction=SignalDirection.LONG,
            stop_price=Decimal("1395"),
            target_r_multiple=Decimal("2"),
            signal_bar_start=SESSION_OPEN,
            reason="x",
        )
        assert adapter().submit("o1", other, APPROVED).reasons == ("instrument_mismatch",)

    def test_costs_are_charged_on_paper_fills(self) -> None:
        paper = adapter(NSE_INTRADAY_EQUITY)
        paper.submit("o1", signal(), APPROVED)
        (fill,) = paper.on_bar(make_candle(BAR1, high="1404.00", low="1397.00"))
        assert fill.costs > 0


class TestDuplicateSubmission:
    def test_an_identical_resubmit_returns_the_original_record(self) -> None:
        paper = adapter()
        first = paper.submit("o1", signal(), APPROVED)
        assert paper.submit("o1", signal(), APPROVED) is first
        (fill,) = paper.on_bar(make_candle(BAR1, high="1404.00", low="1397.00"))
        assert fill.quantity == 10  # one entry, not two

    def test_reusing_an_id_for_a_different_request_raises(self) -> None:
        paper = adapter()
        paper.submit("o1", signal(), APPROVED)
        with pytest.raises(InvalidTransitionError, match="already used"):
            paper.submit("o1", signal(), RiskDecision(20, limited_by=LimitingConstraint.CAPITAL))

    def test_a_second_order_while_one_is_working_is_rejected(self) -> None:
        paper = adapter()
        paper.submit("o1", signal(), APPROVED)
        assert paper.submit("o2", signal(), APPROVED).reasons == ("order_or_position_active",)

    def test_a_second_order_while_holding_is_rejected(self) -> None:
        paper = entered()
        record = paper.submit("o2", signal(at=BAR1), APPROVED)
        assert record.reasons == ("order_or_position_active",)


class TestCancellation:
    def test_a_working_order_can_be_cancelled_and_never_fills(self) -> None:
        paper = adapter()
        paper.submit("o1", signal(), APPROVED)
        assert paper.cancel("o1").status is OrderStatus.CANCELLED
        assert paper.on_bar(make_candle(BAR1)) == ()
        assert paper.position is None

    @pytest.mark.parametrize("setup", ["cancelled", "filled", "rejected", "expired"])
    def test_a_terminal_order_cannot_be_cancelled(self, setup: str) -> None:
        paper = adapter()
        if setup == "rejected":
            paper.submit("o0", signal(), APPROVED)
        paper.submit("o1", signal(), APPROVED)
        if setup == "cancelled":
            paper.cancel("o1")
        elif setup == "filled":
            paper.on_bar(make_candle(BAR1, high="1404.00", low="1397.00"))
        elif setup == "expired":
            paper.on_bar(make_candle(BAR1 + M5))
        with pytest.raises(InvalidTransitionError, match="cannot become cancelled"):
            paper.cancel("o1")

    def test_an_unknown_order_cannot_be_cancelled(self) -> None:
        with pytest.raises(InvalidTransitionError, match="no order"):
            adapter().cancel("nope")

    def test_after_cancelling_a_new_order_is_accepted(self) -> None:
        paper = adapter()
        paper.submit("o1", signal(), APPROVED)
        paper.cancel("o1")
        assert paper.submit("o2", signal(), APPROVED).status is OrderStatus.ACCEPTED


class TestProtectiveExits:
    def test_the_stop_closes_a_long(self) -> None:
        paper = entered()
        (fill,) = paper.on_bar(make_candle(BAR1 + M5, high="1401.00", low="1394.00"))
        assert (fill.reason, fill.price) == (FillReason.STOP, Decimal("1394.95"))
        assert paper.position is None
        assert paper.trades[-1].exit_reason is FillReason.STOP
        assert paper.cash == Decimal("100000") + paper.trades[-1].net_pnl

    def test_the_target_closes_a_long(self) -> None:
        """Entry 1400.05, risk 5.05, 2R target 1410.15, trigger one tick through."""
        paper = entered()
        (fill,) = paper.on_bar(make_candle(BAR1 + M5, high="1410.20", low="1400.00"))
        assert (fill.reason, fill.price) == (FillReason.TARGET, Decimal("1410.10"))
        assert paper.trades[-1].net_pnl > 0

    def test_a_graze_of_the_target_does_not_fill(self) -> None:
        paper = entered()
        assert paper.on_bar(make_candle(BAR1 + M5, high="1410.15", low="1400.00")) == ()
        assert paper.position is not None

    def test_the_stop_closes_a_short(self) -> None:
        paper = entered(SignalDirection.SHORT)
        (fill,) = paper.on_bar(make_candle(BAR1 + M5, high="1406.00", low="1399.00"))
        assert (fill.reason, fill.side, fill.price) == (
            FillReason.STOP,
            OrderSide.BUY,
            Decimal("1405.05"),
        )

    def test_both_levels_in_one_bar_without_minutes_takes_the_stop(self) -> None:
        paper = entered()
        (fill,) = paper.on_bar(make_candle(BAR1 + M5, high="1411.00", low="1394.00"))
        assert fill.reason is FillReason.STOP

    def test_the_hard_exit_flattens_at_the_cutoff_close(self) -> None:
        paper = entered()
        (fill,) = paper.on_bar(make_candle(HARD_EXIT_BAR, close="1403.00"))
        assert (fill.reason, fill.price) == (FillReason.TIME_EXIT, Decimal("1402.95"))
        assert paper.position is None


class TestFlatten:
    def test_flatten_exits_a_held_position_at_the_next_bar_open(self) -> None:
        paper = entered()
        paper.flatten()
        assert paper.position is not None  # not until the next bar
        (fill,) = paper.on_bar(make_candle(BAR1 + M5, open_="1401.00"))
        assert (fill.reason, fill.price, fill.reference_price) == (
            FillReason.FLATTEN,
            Decimal("1400.95"),
            Decimal("1401.00"),
        )
        assert paper.position is None
        assert paper.trades[-1].exit_reason is FillReason.FLATTEN

    def test_flatten_takes_precedence_over_the_stop_on_that_bar(self) -> None:
        paper = entered()
        paper.flatten()
        (fill,) = paper.on_bar(make_candle(BAR1 + M5, open_="1400.00", low="1390.00"))
        assert fill.reason is FillReason.FLATTEN

    def test_flatten_cancels_a_working_order(self) -> None:
        paper = adapter()
        paper.submit("o1", signal(), APPROVED)
        paper.flatten()
        assert paper.order("o1").status is OrderStatus.CANCELLED
        assert paper.on_bar(make_candle(BAR1)) == ()

    def test_flatten_with_nothing_open_raises(self) -> None:
        with pytest.raises(InvalidTransitionError, match="nothing to flatten"):
            adapter().flatten()

    def test_a_second_flatten_while_one_is_pending_raises(self) -> None:
        paper = entered()
        paper.flatten()
        with pytest.raises(InvalidTransitionError, match="already pending"):
            paper.flatten()

    def test_no_new_order_while_a_flatten_is_pending(self) -> None:
        paper = entered()
        paper.flatten()
        assert paper.submit("o2", signal(at=BAR1), APPROVED).status is OrderStatus.REJECTED


class TestInvalidBars:
    def test_a_repeated_or_earlier_bar_raises(self) -> None:
        paper = entered()
        with pytest.raises(InvalidTransitionError, match="must arrive in order"):
            paper.on_bar(make_candle(BAR1))
        with pytest.raises(InvalidTransitionError, match="must arrive in order"):
            paper.on_bar(make_candle(SESSION_OPEN))

    def test_an_in_progress_bar_raises(self) -> None:
        with pytest.raises(InvalidTransitionError, match="not completed"):
            adapter().on_bar(make_candle(BAR1, status=CandleStatus.IN_PROGRESS))

    def test_another_instruments_bar_raises(self) -> None:
        with pytest.raises(InvalidTransitionError, match="instrument"):
            adapter().on_bar(make_candle(BAR1, token=INFY_TOKEN))

    def test_the_wrong_interval_raises(self) -> None:
        with pytest.raises(InvalidTransitionError, match="interval"):
            adapter().on_bar(make_candle(BAR1, CandleInterval.M1))


class TestDeterminism:
    def test_the_same_calls_produce_the_same_trades(self) -> None:
        def run() -> tuple[object, ...]:
            paper = entered()
            paper.on_bar(make_candle(BAR1 + M5, high="1401.00", low="1394.00"))
            return paper.trades

        assert run() == run()


class TestBrokerIsolation:
    PAPER_PATH = (
        "adapters/paper/__init__.py",
        "adapters/paper/execution.py",
        "domain/execution/ports.py",
        "services/execution.py",
    )

    @pytest.mark.parametrize("mode", [TradingMode.LIVE, TradingMode.BACKTEST])
    def test_only_paper_mode_gets_an_execution_port(self, mode: TradingMode) -> None:
        with pytest.raises(ExecutionModeError, match="no execution port"):
            build_execution_port(mode, **self._port_args())

    def test_paper_mode_gets_the_paper_adapter(self) -> None:
        port = build_execution_port(TradingMode.PAPER, **self._port_args())
        assert type(port) is PaperExecutionAdapter

    def test_the_paper_path_imports_no_broker_network_clock_or_ai(self) -> None:
        app = pathlib.Path(__file__).resolve().parents[1] / "app"
        forbidden = (
            "app.adapters.zerodha",
            "app.adapters.replay",
            "app.adapters.tradingview",
            "app.api",
            "app.domain.ai",
            "kiteconnect",
            "httpx",
            "websockets",
            "socket",
            "urllib",
            "asyncio",
            "anthropic",
            "random",
        )
        offenders = []
        for module in self.PAPER_PATH:
            tree = ast.parse((app / module).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = (
                    [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else []
                )
                offenders += [f"{module}: {n}" for n in names if n.startswith(forbidden)]
        assert offenders == []

    def test_a_full_paper_session_makes_no_network_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("paper execution attempted network I/O")

        monkeypatch.setattr(socket.socket, "connect", refuse)
        monkeypatch.setattr(socket.socket, "connect_ex", refuse)
        monkeypatch.setattr(socket, "create_connection", refuse)
        monkeypatch.setattr(httpx.Client, "send", refuse)
        monkeypatch.setattr(httpx.AsyncClient, "send", refuse)

        port = build_execution_port(TradingMode.PAPER, **self._port_args())
        port.submit("o1", signal(), APPROVED)
        assert isinstance(port, PaperExecutionAdapter)
        port.on_bar(make_candle(BAR1, high="1404.00", low="1397.00"))
        port.flatten()
        port.on_bar(make_candle(BAR1 + M5))
        assert [t.exit_reason for t in port.trades] == [FillReason.FLATTEN]

    @staticmethod
    def _port_args() -> dict[str, object]:
        return {
            "instrument": INSTRUMENT,
            "starting_capital": Decimal("100000"),
            "signal_interval": CandleInterval.M5,
            "hard_exit_time": time(15, 15),
            "execution": ExecutionConfig(),
            "slippage": SlippageConfig(),
            "cost_schedule": NO_COSTS,
        }
