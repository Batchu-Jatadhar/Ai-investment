"""PAPER replay: stored candles -> PaperSession -> dashboard, deterministically, with no broker."""

from __future__ import annotations

import ast
import dataclasses
import pathlib
import socket
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config.settings import TradingMode
from app.core.time import IST, to_ist
from app.domain.backtest.config import NSE_INTRADAY_EQUITY
from app.domain.backtest.models import FillReason
from app.domain.execution.ports import OrderStatus
from app.domain.market.aggregation import aggregate_minutes
from app.domain.market.models import Candle, CandleInterval
from app.domain.market.ports import CandlePage
from app.domain.risk.sizing import RiskConfig
from app.services.backtest_data import HistoricalDataError, InsufficientWarmupError
from app.services.paper_dashboard import build_paper_dashboard
from app.services.paper_replay import (
    PaperReplayError,
    PaperReplayModeError,
    PaperReplayResult,
    replay_paper_session,
)
from app.services.paper_session import get_active_paper_session, set_active_paper_session
from tests.backtest.test_golden_backtest import FRI_5M, MON_5M, THU_5M, WED_5M, run_golden
from tests.market.conftest import RELIANCE_TOKEN, make_instrument

RELIANCE = make_instrument(RELIANCE_TOKEN, "RELIANCE")
WED, THU, FRI, MON, TUE = (date(2026, 8, d) for d in (19, 20, 21, 24, 25))
CAPITAL = Decimal("500000")
NOW = datetime(2026, 8, 24, 5, 0, tzinfo=UTC)


def midnight(day: date) -> datetime:
    return datetime.combine(day, time(0), IST).astimezone(UTC)


def minutes_for(bar: Candle) -> list[Candle]:
    """Five stored minutes that aggregate exactly to ``bar``: the first carries the
    range, the rest sit at the close. Nothing about the 5m bar is changed."""
    share = bar.volume // 5
    first = Candle(
        instrument_token=bar.instrument_token,
        interval=CandleInterval.M1,
        start_at=bar.start_at,
        end_at=bar.start_at + CandleInterval.M1.delta,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=share,
        status=bar.status,
        source="zerodha_historical",
    )
    rest = [
        dataclasses.replace(
            first,
            start_at=bar.start_at + CandleInterval.M1.delta * i,
            end_at=bar.start_at + CandleInterval.M1.delta * (i + 1),
            open=bar.close,
            high=bar.close,
            low=bar.close,
        )
        for i in range(1, 5)
    ]
    return [first, *rest]


def store(repository, bars) -> None:  # noqa: ANN001
    minutes = [m for bar in bars for m in minutes_for(bar)]
    repository.save_historical_candles(minutes)
    repository.save_historical_candles(aggregate_minutes(minutes, CandleInterval.M5).candles)


def replay(repository, **overrides: object) -> PaperReplayResult:  # noqa: ANN001
    values: dict[str, object] = {
        "trading_mode": TradingMode.PAPER,
        "warmup_start": midnight(WED),
        "start": midnight(THU),
        "end": midnight(TUE),
        "starting_capital": CAPITAL,
        "risk_config": RiskConfig(Decimal("0.01")),
        "cost_schedule": NSE_INTRADAY_EQUITY,
    }
    values.update(overrides)
    return replay_paper_session(repository, RELIANCE, **values)  # type: ignore[arg-type]


@pytest.fixture
def golden(repository) -> Iterator[object]:  # noqa: ANN001
    store(repository, (*WED_5M, *THU_5M, *FRI_5M, *MON_5M))
    yield repository
    set_active_paper_session(None)


def at(day: date, hh_mm: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(hh_mm), IST).astimezone(UTC)


class TestGoldenReplay:
    def test_the_golden_friday_trades_like_the_backtest(self, golden) -> None:  # noqa: ANN001
        result = replay(golden)
        assert result.trading_sessions == (THU, FRI, MON)
        assert len(result.evaluations) == len(THU_5M) + len(FRI_5M) + len(MON_5M)

        (trade,) = result.session.paper.trades
        (expected,) = run_golden().trades
        for leg in ("entry", "exit"):
            ours, theirs = getattr(trade, leg), getattr(expected, leg)
            assert (ours.price, ours.reference_price, ours.occurred_at, ours.reason) == (
                theirs.price,
                theirs.reference_price,
                theirs.occurred_at,
                theirs.reason,
            )
        assert trade.exit_reason is FillReason.TARGET
        # sized by the risk layer, not the backtest's fixed notional: 5000 / 14.00
        assert trade.entry.quantity == 357

    def test_reference_entry_and_actual_execution_stay_distinct(self, golden) -> None:  # noqa: ANN001
        by_bar = {e.bar.start_at: e for e in replay(golden).evaluations}
        signal = by_bar[at(FRI, "09:35")]
        assert signal.signal is not None and signal.order is not None
        assert signal.bar.close == Decimal("1008.00")  # the reference risk was sized on
        assert signal.fills == ()  # nothing fills on the signal bar

        (fill,) = by_bar[at(FRI, "09:40")].fills
        assert fill.reason is FillReason.ENTRY
        assert fill.reference_price == Decimal("1007.95")  # the next bar's open
        assert fill.price == Decimal("1008.00")  # plus one adverse tick

    def test_a_missing_entry_bar_expires_the_order_and_is_reported(self, golden) -> None:  # noqa: ANN001
        result = replay(golden)
        assert result.missing_bars == (at(MON, "09:40"),)
        order_id = f"{RELIANCE_TOKEN}:{at(MON, '09:35').isoformat()}"
        assert result.session.paper.order(order_id).status is OrderStatus.EXPIRED
        assert len(result.session.paper.trades) == 1  # nothing was filled on a later bar
        assert all(not e.fills for e in result.evaluations if to_ist(e.bar.start_at).date() == MON)

    def test_the_same_candles_give_the_same_session_and_dashboard(self, golden) -> None:  # noqa: ANN001
        first, second = replay(golden), replay(golden)
        assert first.evaluations == second.evaluations
        assert first.session.paper.trades == second.session.paper.trades
        assert first.session.paper.cash == second.session.paper.cash

        def dashboard(result: PaperReplayResult) -> dict[str, object]:
            return build_paper_dashboard(
                result.session,
                trading_mode=TradingMode.PAPER,
                now=NOW,
                stale_after=timedelta(minutes=6),
            ).model_dump()

        assert dashboard(first) == dashboard(second)

    def test_the_replay_drives_the_active_dashboard(self, golden, client: TestClient) -> None:  # noqa: ANN001
        result = replay(golden)
        assert get_active_paper_session() is result.session

        body = client.get("/dashboard/paper").json()
        (trade,) = result.session.paper.trades
        assert body["session_running"] is True
        assert body["instrument"]["symbol"] == "NSE:RELIANCE"
        assert body["data"]["last_bar_start"] == at(MON, "09:50").isoformat()
        assert body["data"]["status"] == "stale"  # historical bars, real clock
        assert body["position"]["side"] == "FLAT"
        assert Decimal(body["position"]["realized_pnl"]) == trade.net_pnl
        assert body["strategy"]["reason_code"] == "DIRECTION_ALREADY_SIGNALLED"

    def test_register_false_leaves_the_dashboard_alone(self, golden) -> None:  # noqa: ANN001
        replay(golden, register=False)
        assert get_active_paper_session() is None


class TestSessionEnds:
    def test_a_working_order_is_cancelled_when_the_session_data_ends(self, repository) -> None:  # noqa: ANN001
        store(repository, (*WED_5M, *THU_5M, *FRI_5M[:5]))  # Friday stops at the signal bar
        result = replay(repository, end=midnight(date(2026, 8, 22)))
        order_id = f"{RELIANCE_TOKEN}:{at(FRI, '09:35').isoformat()}"
        assert result.cancelled_at_session_end == (order_id,)
        assert result.session.paper.order(order_id).status is OrderStatus.CANCELLED
        assert result.session.paper.trades == ()
        dashboard = build_paper_dashboard(
            result.session,
            trading_mode=TradingMode.PAPER,
            now=NOW,
            stale_after=timedelta(minutes=6),
        )
        assert "The entry order was cancelled before it could execute" in dashboard.blocked_reasons
        set_active_paper_session(None)

    def test_a_position_left_open_by_missing_bars_fails_closed(self, repository) -> None:  # noqa: ANN001
        store(repository, (*WED_5M, *THU_5M, *FRI_5M[:6]))  # entered 09:40, no later bars
        with pytest.raises(PaperReplayError, match="rather than invent a price"):
            replay(repository, end=midnight(date(2026, 8, 22)))
        set_active_paper_session(None)


class TestRefusals:
    def test_insufficient_warmup(self, golden) -> None:  # noqa: ANN001
        with pytest.raises(InsufficientWarmupError):
            replay(golden, warmup_start=midnight(THU))

    def test_an_empty_range(self, repository) -> None:  # noqa: ANN001
        with pytest.raises(HistoricalDataError, match="no stored"):
            replay(repository)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"start": midnight(THU) + timedelta(hours=9)},  # not an IST midnight
            {"end": midnight(THU)},  # end not after start
            {"warmup_start": midnight(FRI)},  # warmup after start
        ],
    )
    def test_an_invalid_range(self, golden, overrides: dict[str, object]) -> None:  # noqa: ANN001
        with pytest.raises(HistoricalDataError):
            replay(golden, **overrides)

    def test_non_chronological_candles(self) -> None:
        class Reversed:
            def candles_page(self, token, interval, start, end, *, page_size, after=None):  # noqa: ANN001, ANN201
                bars = [*WED_5M, *THU_5M]
                if interval is CandleInterval.M1:
                    bars = [m for bar in bars for m in minutes_for(bar)]
                else:
                    bars = [dataclasses.replace(b, source="zerodha_historical") for b in bars]
                return CandlePage(candles=tuple(reversed(bars)), next_after=None)

        with pytest.raises(HistoricalDataError, match="out of order"):
            replay(Reversed())

    @pytest.mark.parametrize("mode", [TradingMode.LIVE, TradingMode.BACKTEST])
    def test_any_mode_but_paper_fails_before_reading_anything(self, mode: TradingMode) -> None:
        class Untouchable:
            def __getattr__(self, name: str) -> object:
                raise AssertionError(f"the repository was touched ({name}) in {mode.value} mode")

        with pytest.raises(PaperReplayModeError, match="PAPER mode only"):
            replay(Untouchable(), trading_mode=mode)
        assert get_active_paper_session() is None


class InMemoryCandles:
    """The repository port over golden candles held in memory: no database, no socket."""

    def __init__(self, bars: tuple[Candle, ...]) -> None:
        self.minutes = tuple(m for bar in bars for m in minutes_for(bar))
        self.signal = tuple(aggregate_minutes(self.minutes, CandleInterval.M5).candles)

    def candles_page(self, token, interval, start, end, *, page_size, after=None):  # noqa: ANN001, ANN201
        series = self.minutes if interval is CandleInterval.M1 else self.signal
        return CandlePage(
            candles=tuple(c for c in series if start <= c.start_at < end),
            next_after=None,
        )


class TestBrokerIsolation:
    def test_the_replay_makes_no_network_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def refuse(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("paper replay attempted network I/O")

        monkeypatch.setattr(socket.socket, "connect", refuse)
        monkeypatch.setattr(socket.socket, "connect_ex", refuse)
        monkeypatch.setattr(socket, "create_connection", refuse)
        monkeypatch.setattr(httpx.Client, "send", refuse)
        monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
        try:
            result = replay(InMemoryCandles((*WED_5M, *THU_5M, *FRI_5M, *MON_5M)))
        finally:
            set_active_paper_session(None)
        assert len(result.session.paper.trades) == 1

    def test_the_replay_path_imports_no_broker_live_or_vendor_code(self) -> None:
        app = pathlib.Path(__file__).resolve().parents[2] / "app"
        modules = ("services/paper_replay.py", "services/paper_session.py")
        forbidden = (
            "app.adapters.zerodha",
            "app.adapters.tradingview",
            "app.adapters.replay",
            "app.api",
            "httpx",
            "websockets",
            "socket",
            "kiteconnect",
            "anthropic",
        )
        offenders = []
        for module in modules:
            for node in ast.walk(ast.parse((app / module).read_text(encoding="utf-8"))):
                names = (
                    [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else []
                )
                offenders += [f"{module}: {n}" for n in names if n.startswith(forbidden)]
        assert offenders == []
