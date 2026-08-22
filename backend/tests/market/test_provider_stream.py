"""Provider lifecycle: connect, subscribe, reconnect, resubscribe, gap recording.

Driven by an injected fake socket, so the whole lifecycle - including failures
that are hard to provoke against a live feed - is deterministic.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.adapters.zerodha.client import ZerodhaRestClient
from app.adapters.zerodha.errors import ZerodhaAuthError, ZerodhaNotConfiguredError
from app.adapters.zerodha.provider import ReconnectPolicy, ZerodhaMarketDataProvider
from app.core.time import FixedClock
from app.domain.market.models import TickMode
from app.domain.market.ports import (
    ConnectionEvent,
    ConnectionEventType,
    DataGap,
    ProviderState,
    TickBatch,
)
from tests.market.conftest import (
    HEARTBEAT,
    RELIANCE_TOKEN,
    frame,
    quote_packet,
)

START = datetime(2026, 8, 21, 4, 0, tzinfo=UTC)


class SocketClosedError(Exception):
    """Raised by the fake socket to simulate a dropped connection."""


class FakeSocket:
    """Replays a scripted list of inbound frames, then raises or stops."""

    def __init__(self, script: list[object]) -> None:
        self._script = list(script)
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self) -> bytes | str:
        if not self._script:
            raise SocketClosedError("script exhausted")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[return-value]

    async def close(self) -> None:
        self.closed = True

    @property
    def subscriptions(self) -> list[int]:
        out: list[int] = []
        for message in self.sent:
            if message.get("a") == "subscribe":
                out.extend(message["v"])
        return out

    @property
    def mode_messages(self) -> list[dict]:
        return [m for m in self.sent if m.get("a") == "mode"]


def ok_profile(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"data": {"user_id": "AB1234"}})


def expired_token(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        403,
        json={"message": "Invalid `access_token`.", "error_type": "TokenException"},
    )


def build_provider(
    sockets: list[FakeSocket],
    *,
    handler=ok_profile,  # noqa: ANN001
    api_key: str | None = "key",
    access_token: str | None = "token",
    policy: ReconnectPolicy | None = None,
    clock: FixedClock | None = None,
) -> tuple[ZerodhaMarketDataProvider, list[str]]:
    urls: list[str] = []
    queue = list(sockets)

    async def connect(url: str):  # noqa: ANN202
        urls.append(url)
        if not queue:
            raise SocketClosedError("no more sockets")
        return queue.pop(0)

    client = ZerodhaRestClient(
        api_key=api_key,
        access_token=access_token,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    provider = ZerodhaMarketDataProvider(
        client,
        clock=clock or FixedClock(START),
        connect_factory=connect,
        reconnect=policy or ReconnectPolicy(initial_seconds=0, max_seconds=0, jitter=0),
    )
    return provider, urls


async def collect(provider: ZerodhaMarketDataProvider, limit: int = 40) -> list:
    out = []
    async for event in provider.stream():
        out.append(event)
        if len(out) >= limit:
            provider._running = False  # noqa: SLF001 - deterministic stop for the test
            break
    return out


class TestConfigurationStates:
    async def test_missing_credentials_reports_not_configured(self) -> None:
        provider, _ = build_provider([], api_key=None, access_token=None)
        events = await collect(provider)
        assert provider.state is ProviderState.NOT_CONFIGURED
        assert provider.labelled_state == "ZERODHA_NOT_CONFIGURED"
        assert events[0].event_type is ConnectionEventType.NOT_CONFIGURED
        assert len(events) == 1  # it must not proceed to connect

    async def test_connect_raises_when_unconfigured(self) -> None:
        provider, _ = build_provider([], api_key=None, access_token=None)
        with pytest.raises(ZerodhaNotConfiguredError):
            await provider.connect()

    async def test_expired_token_stops_the_stream(self) -> None:
        provider, urls = build_provider([FakeSocket([])], handler=expired_token)
        events = await collect(provider)
        assert provider.state is ProviderState.AUTH_FAILED
        assert provider.labelled_state == "ZERODHA_AUTH_FAILED"
        assert events[0].event_type is ConnectionEventType.AUTH_FAILED
        # No socket is opened, and no retry loop is entered.
        assert urls == []

    async def test_connect_raises_on_auth_failure(self) -> None:
        provider, _ = build_provider([FakeSocket([])], handler=expired_token)
        with pytest.raises(ZerodhaAuthError):
            await provider.connect()


class TestConnectAndReceive:
    async def test_connects_authenticates_and_yields_ticks(self) -> None:
        socket = FakeSocket([frame(quote_packet(RELIANCE_TOKEN, 140000))])
        provider, urls = build_provider([socket])
        events = await collect(provider, limit=4)

        kinds = [e.event_type for e in events if isinstance(e, ConnectionEvent)]
        assert ConnectionEventType.AUTH_SUCCEEDED in kinds
        assert ConnectionEventType.CONNECTED in kinds
        batches = [e for e in events if isinstance(e, TickBatch)]
        assert len(batches) == 1
        assert batches[0].ticks[0].instrument_token == RELIANCE_TOKEN
        assert urls[0].startswith("wss://ws.kite.trade?api_key=key")

    async def test_heartbeats_do_not_produce_ticks(self) -> None:
        socket = FakeSocket([HEARTBEAT, HEARTBEAT])
        provider, _ = build_provider([socket])
        events = await collect(provider, limit=3)
        assert not [e for e in events if isinstance(e, TickBatch)]

    async def test_error_text_message_is_surfaced(self) -> None:
        socket = FakeSocket([json.dumps({"type": "error", "data": "subscription limit"})])
        provider, _ = build_provider([socket])
        events = await collect(provider, limit=4)
        errors = [
            e
            for e in events
            if isinstance(e, ConnectionEvent) and e.event_type is ConnectionEventType.PROVIDER_ERROR
        ]
        assert any("subscription limit" in str(e.detail) for e in errors)

    async def test_malformed_frame_is_reported_and_the_stream_survives(self) -> None:
        socket = FakeSocket(
            [
                frame(quote_packet(RELIANCE_TOKEN, 140000))[:-8],  # truncated
                frame(quote_packet(RELIANCE_TOKEN, 141000)),  # then a good one
            ]
        )
        provider, _ = build_provider([socket])
        events = await collect(provider, limit=6)
        assert provider.malformed_frames == 1
        assert [e for e in events if isinstance(e, TickBatch)]


class TestSubscription:
    async def test_subscribe_sends_subscribe_then_mode(self) -> None:
        socket = FakeSocket([HEARTBEAT])
        provider, _ = build_provider([socket])
        await provider.connect()
        await provider.subscribe([RELIANCE_TOKEN], TickMode.FULL)
        assert socket.sent[0] == {"a": "subscribe", "v": [RELIANCE_TOKEN]}
        assert socket.sent[1] == {"a": "mode", "v": ["full", [RELIANCE_TOKEN]]}

    async def test_duplicate_subscription_is_not_resent(self) -> None:
        socket = FakeSocket([HEARTBEAT])
        provider, _ = build_provider([socket])
        await provider.connect()
        await provider.subscribe([RELIANCE_TOKEN], TickMode.FULL)
        await provider.subscribe([RELIANCE_TOKEN], TickMode.FULL)
        assert socket.subscriptions == [RELIANCE_TOKEN]

    async def test_changing_mode_resends(self) -> None:
        socket = FakeSocket([HEARTBEAT])
        provider, _ = build_provider([socket])
        await provider.connect()
        await provider.subscribe([RELIANCE_TOKEN], TickMode.LTP)
        await provider.subscribe([RELIANCE_TOKEN], TickMode.FULL)
        assert [m["v"][0] for m in socket.mode_messages] == ["ltp", "full"]

    async def test_unsubscribe_removes_the_token(self) -> None:
        socket = FakeSocket([HEARTBEAT])
        provider, _ = build_provider([socket])
        await provider.connect()
        await provider.subscribe([RELIANCE_TOKEN])
        await provider.unsubscribe([RELIANCE_TOKEN])
        assert provider.subscribed_tokens == frozenset()
        assert {"a": "unsubscribe", "v": [RELIANCE_TOKEN]} in socket.sent

    async def test_unsubscribing_an_unknown_token_is_a_no_op(self) -> None:
        socket = FakeSocket([HEARTBEAT])
        provider, _ = build_provider([socket])
        await provider.connect()
        await provider.unsubscribe([12345])
        assert socket.sent == []


class TestReconnection:
    async def test_reconnects_after_a_drop(self) -> None:
        first = FakeSocket([frame(quote_packet(RELIANCE_TOKEN, 140000)), SocketClosedError("drop")])
        second = FakeSocket([frame(quote_packet(RELIANCE_TOKEN, 141000))])
        provider, urls = build_provider([first, second])
        provider._tokens = {RELIANCE_TOKEN}  # noqa: SLF001 - pre-existing subscription
        provider._modes = {RELIANCE_TOKEN: TickMode.FULL}  # noqa: SLF001

        events = await collect(provider, limit=12)
        kinds = [e.event_type for e in events if isinstance(e, ConnectionEvent)]
        assert ConnectionEventType.DISCONNECTED in kinds
        assert ConnectionEventType.RECONNECTED in kinds
        assert len(urls) == 2
        assert provider.reconnect_count == 1

    async def test_reconnect_records_a_data_gap(self) -> None:
        clock = FixedClock(START)
        first = FakeSocket([SocketClosedError("drop")])
        second = FakeSocket([HEARTBEAT])
        provider, _ = build_provider([first, second], clock=clock)
        provider._tokens = {RELIANCE_TOKEN}  # noqa: SLF001
        provider._modes = {RELIANCE_TOKEN: TickMode.FULL}  # noqa: SLF001

        events = await collect(provider, limit=12)
        gaps = [e for e in events if isinstance(e, DataGap)]
        assert len(gaps) == 1
        assert gaps[0].reason == "reconnect"
        assert RELIANCE_TOKEN in gaps[0].instrument_tokens

    async def test_resubscribes_after_reconnect(self) -> None:
        first = FakeSocket([SocketClosedError("drop")])
        second = FakeSocket([HEARTBEAT])
        provider, _ = build_provider([first, second])
        provider._tokens = {RELIANCE_TOKEN}  # noqa: SLF001
        provider._modes = {RELIANCE_TOKEN: TickMode.FULL}  # noqa: SLF001

        events = await collect(provider, limit=12)
        kinds = [e.event_type for e in events if isinstance(e, ConnectionEvent)]
        assert ConnectionEventType.RESUBSCRIBED in kinds
        assert second.subscriptions == [RELIANCE_TOKEN]
        assert second.mode_messages[0]["v"] == ["full", [RELIANCE_TOKEN]]

    async def test_exhausted_reconnect_attempts_stop_the_stream(self) -> None:
        provider, _ = build_provider(
            [],  # every connect attempt fails
            policy=ReconnectPolicy(initial_seconds=0, max_seconds=0, jitter=0, max_attempts=2),
        )
        events = await collect(provider, limit=20)
        assert provider.state is ProviderState.STOPPED
        reasons = [
            str(e.detail.get("reason", "")) for e in events if isinstance(e, ConnectionEvent)
        ]
        assert any("exhausted" in r or "no more sockets" in r for r in reasons)


class TestStaleness:
    async def test_a_silent_stream_is_reported_once(self) -> None:
        clock = FixedClock(START)
        socket = FakeSocket([frame(quote_packet(RELIANCE_TOKEN, 140000))] + [HEARTBEAT] * 3)
        provider, _ = build_provider([socket], clock=clock)
        provider._stale_after = timedelta(seconds=30)  # noqa: SLF001

        events = []
        async for event in provider.stream():
            events.append(event)
            clock.advance(timedelta(seconds=45))
            if len(events) >= 8:
                provider._running = False  # noqa: SLF001
                break

        stale = [
            e
            for e in events
            if isinstance(e, ConnectionEvent) and e.event_type is ConnectionEventType.STREAM_STALE
        ]
        assert len(stale) == 1  # reported once, not on every frame


class TestHealth:
    async def test_health_reflects_reality(self) -> None:
        socket = FakeSocket([frame(quote_packet(RELIANCE_TOKEN, 140000))])
        provider, _ = build_provider([socket])
        await collect(provider, limit=4)
        health = provider.health()
        assert health["configured"] is True
        assert health["provider"] == "zerodha"
        assert health["ticks_received"] == 1
        assert health["frames_received"] == 1

    async def test_unconfigured_health_is_not_optimistic(self) -> None:
        provider, _ = build_provider([], api_key=None, access_token=None)
        health = provider.health()
        assert health["configured"] is False
        assert health["connected"] is False
        assert health["state"] == "ZERODHA_NOT_CONFIGURED"


class TestReconnectPolicy:
    def test_backoff_grows_and_is_capped(self) -> None:
        policy = ReconnectPolicy(initial_seconds=1, max_seconds=8, factor=2, jitter=0)
        delays = [policy.delay_for(n, rand=lambda: 0.5) for n in range(1, 6)]
        assert delays == [1.0, 2.0, 4.0, 8.0, 8.0]

    def test_unlimited_attempts_by_default(self) -> None:
        assert ReconnectPolicy().should_retry(10_000) is True

    def test_attempt_limit_is_respected(self) -> None:
        policy = ReconnectPolicy(max_attempts=3)
        assert policy.should_retry(2) is True
        assert policy.should_retry(3) is False
