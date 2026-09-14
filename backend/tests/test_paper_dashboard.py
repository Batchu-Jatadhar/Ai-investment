"""PAPER dashboard read model: every figure derived by the backend, nothing invented."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.adapters.ai.fake import FakeAnalyst
from app.config.settings import TradingMode
from app.domain.ai.analyst import AnalystDecision
from app.domain.backtest.config import NO_COSTS
from app.domain.risk.sizing import RiskConfig
from app.services.paper_dashboard import PaperDashboard, build_paper_dashboard
from app.services.paper_session import PaperSession, set_active_paper_session
from tests.backtest.conftest import make_instrument
from tests.backtest.test_golden_backtest import FRI_5M

ATR = Decimal("10.00")
STALE_AFTER = timedelta(minutes=6)


def session(
    capital: str = "500000", analyst: FakeAnalyst | None = None, bars: int = 0
) -> PaperSession:
    s = PaperSession(
        instrument=make_instrument(),
        starting_capital=Decimal(capital),
        risk_config=RiskConfig(Decimal("0.01")),
        cost_schedule=NO_COSTS,
        analyst=analyst,
    )
    for bar in FRI_5M[:bars]:
        s.on_bar(bar, prior_atr=ATR)
    return s


def view(
    s: PaperSession | None,
    *,
    mode: TradingMode = TradingMode.PAPER,
    late: timedelta = timedelta(0),
) -> PaperDashboard:
    now = (s.last.bar.end_at if s is not None and s.last is not None else FRI_5M[0].end_at) + late
    return build_paper_dashboard(s, trading_mode=mode, now=now, stale_after=STALE_AFTER)


class TestUnavailable:
    def test_no_session_shows_nothing_invented(self) -> None:
        v = view(None)
        assert (v.is_paper, v.session_running, v.trade_status) == (True, False, "UNAVAILABLE")
        assert v.instrument is None
        assert v.data.status == "unavailable"
        assert v.plan.model_dump() == dict.fromkeys(v.plan.model_dump())
        assert v.position.model_dump() == dict.fromkeys(v.position.model_dump())
        assert v.risk.status == "NOT_EVALUATED" and v.ai.status == "NOT_CONFIGURED"
        assert "No paper session is running in this process" in v.blocked_reasons

    def test_a_session_without_bars_says_so(self) -> None:
        v = view(session())
        assert v.session_running and v.instrument is not None
        assert v.instrument.symbol == "NSE:RELIANCE"
        assert v.trade_status == "UNAVAILABLE"
        assert any("not received a completed bar" in r for r in v.blocked_reasons)

    def test_a_non_paper_mode_is_flagged(self) -> None:
        v = view(session(bars=4), mode=TradingMode.BACKTEST)
        assert not v.is_paper
        assert any("serves PAPER mode only" in r for r in v.blocked_reasons)


class TestLifecycle:
    """The golden Friday: signal 09:35, entry 09:40 at 1008.00, target hit 09:45."""

    def test_no_breakout_waits_with_the_strategy_reason(self) -> None:
        v = view(session(bars=4))
        assert (v.strategy.action, v.trade_status) == ("WAIT", "NO_SIGNAL")
        assert v.strategy.reason_code == "NO_BREAKOUT"
        assert v.plan.entry is None and v.risk.status == "NOT_EVALUATED"
        assert v.data.status == "fresh" and v.data.last_close == "1004.00"

    def test_a_signal_is_sized_and_submitted(self) -> None:
        v = view(session(bars=5))
        assert (v.strategy.action, v.trade_status) == ("BUY", "ORDER_WORKING")
        assert v.risk.status == "APPROVED"
        # budget 5000 / (1008.00 - 994.00) = 357.1 -> 357
        assert v.risk.quantity == 357 and v.risk.limited_by == "risk_budget"
        assert v.ai.status == "NOT_CONFIGURED"
        assert v.plan.model_dump() == {
            "direction": "LONG",
            "entry": "1008.00",
            "entry_basis": "signal_bar_close",
            "stop_loss": "994.00",
            "target": "1036.00",
            "reward_to_risk": "2.0",
            "quantity": 357,
        }
        assert v.blocked_reasons == []

    def test_the_filled_position_is_held_and_marked(self) -> None:
        v = view(session(bars=6))
        assert (v.strategy.action, v.trade_status) == ("HOLD", "IN_POSITION")
        assert v.plan.entry == "1008.00" and v.plan.entry_basis == "fill"  # 1007.95 + 1 tick
        assert v.plan.target == "1036.00"
        assert v.position.side == "LONG" and v.position.quantity == 357
        assert v.position.mark_price == "1012.00"
        assert v.position.unrealized_pnl == "1428.00"  # (1012 - 1008) x 357
        assert v.position.realized_pnl == "0.00"

    def test_the_target_exit_is_shown_as_exit(self) -> None:
        v = view(session(bars=7))
        assert (v.strategy.action, v.trade_status) == ("EXIT", "EXITED")
        assert v.position.side == "FLAT" and v.position.unrealized_pnl is None
        assert v.position.realized_pnl == "9978.15"  # (1035.95 - 1008.00) x 357, no costs
        assert v.position.closed_trades == 1


class TestBlocked:
    def test_a_risk_rejection_is_shown_even_when_the_ai_would_approve(self) -> None:
        analyst = FakeAnalyst(lambda _: AnalystDecision.TAKE_TRADE)
        v = view(session(capital="500", analyst=analyst, bars=5))
        assert v.strategy.action == "BUY"
        assert v.risk.status == "REJECTED"
        assert "insufficient_capital" in [r.code for r in v.risk.reasons]
        assert v.ai.status == "NOT_CONSULTED" and analyst.calls == []
        assert v.trade_status == "BLOCKED"
        assert v.plan.quantity is None
        assert any(r.startswith("Risk rejected (insufficient_capital)") for r in v.blocked_reasons)

    def test_an_ai_rejection_blocks_and_explains(self) -> None:
        v = view(session(analyst=FakeAnalyst(lambda _: AnalystDecision.REJECT), bars=5))
        assert (v.risk.status, v.ai.status, v.trade_status) == ("APPROVED", "REJECT", "BLOCKED")
        assert v.ai.model_id == "fake-local" and v.ai.prompt_version == "fake-v1"
        assert any(r.startswith("AI filter did not approve") for r in v.blocked_reasons)

    def test_a_malformed_ai_answer_is_shown_as_invalid(self) -> None:
        analyst = FakeAnalyst(respond=lambda _: "TAKE_TRADE please")
        v = view(session(analyst=analyst, bars=5))
        assert (v.ai.status, v.trade_status) == ("INVALID_RESPONSE", "BLOCKED")

    def test_stale_data_is_flagged(self) -> None:
        v = view(session(bars=6), late=timedelta(minutes=30))
        assert v.data.status == "stale" and v.data.age_seconds == 1800.0
        assert any(r.startswith("Market data is stale") for r in v.blocked_reasons)


class TestEndpoint:
    @pytest.fixture
    def registered(self) -> Iterator[PaperSession]:
        s = session(bars=5)
        set_active_paper_session(s)
        yield s
        set_active_paper_session(None)

    def test_without_a_session_the_endpoint_reports_unavailable(self, client: TestClient) -> None:
        body = client.get("/dashboard/paper").json()
        assert body["trading_mode"] == "paper" and body["session_running"] is False
        assert body["trade_status"] == "UNAVAILABLE"

    def test_the_endpoint_serves_the_registered_session(
        self, client: TestClient, registered: PaperSession
    ) -> None:
        body = client.get("/dashboard/paper").json()
        assert body["instrument"]["symbol"] == "NSE:RELIANCE"
        assert body["strategy"]["action"] == "BUY"
        # the test clock is real time, long after the synthetic bar
        assert body["data"]["status"] == "stale"
