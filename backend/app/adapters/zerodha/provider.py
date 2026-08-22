"""Zerodha streaming market-data provider.

Implements :class:`~app.domain.market.ports.MarketDataProvider` over the Kite
WebSocket feed. The domain never imports this module.

Lifecycle::

    NOT_CONFIGURED -> (credentials present)
    AUTHENTICATED  -> (profile call succeeds)   | AUTH_FAILED (403/TokenException)
    CONNECTING     -> CONNECTED -> ... -> DISCONNECTED -> CONNECTING (reconnect)

Two rules the reconnect loop obeys:

*   **A reconnect always produces a recorded gap.** The stream was not
    delivering between the disconnect and the resubscribe, and any bar
    overlapping that window is suspect. Pretending otherwise is how a backtest
    silently inherits data that never existed.
*   **Authentication failure does not retry.** Kite access tokens expire at
    06:00 IST daily and can only be renewed by an interactive login; a retry
    loop against a dead token burns rate limit and hides the real problem.

The socket is injected through ``connect_factory`` so the test suite can drive
the whole lifecycle from recorded frames without touching the live API.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from app.adapters.zerodha.client import ZerodhaRestClient
from app.adapters.zerodha.errors import (
    ZerodhaAuthError,
    ZerodhaError,
    ZerodhaNotConfiguredError,
)
from app.adapters.zerodha.protocol import ParsedFrame, parse_frame
from app.core.logging import get_logger
from app.core.time import Clock, SystemClock
from app.domain.market.models import TickMode
from app.domain.market.ports import (
    ConnectionEvent,
    ConnectionEventType,
    DataGap,
    ProviderState,
    StreamEvent,
    TickBatch,
)

logger = get_logger(__name__)

__all__ = ["ReconnectPolicy", "WebSocketLike", "ZerodhaMarketDataProvider"]

WS_ROOT = "wss://ws.kite.trade"


class WebSocketLike(Protocol):
    """The slice of a WebSocket connection this provider uses."""

    async def send(self, message: str) -> None: ...
    async def recv(self) -> bytes | str: ...
    async def close(self) -> None: ...


ConnectFactory = Callable[[str], Any]


@dataclass(frozen=True, slots=True)
class ReconnectPolicy:
    """Exponential backoff with jitter.

    ``max_attempts=0`` means keep trying: a personal system left running through
    a network outage should recover on its own, and every attempt is recorded.
    """

    initial_seconds: float = 1.0
    max_seconds: float = 30.0
    factor: float = 2.0
    jitter: float = 0.25
    max_attempts: int = 0

    def delay_for(self, attempt: int, *, rand: Callable[[], float] = random.random) -> float:
        base = min(self.initial_seconds * (self.factor ** max(0, attempt - 1)), self.max_seconds)
        return base * (1.0 + self.jitter * (rand() * 2 - 1))

    def should_retry(self, attempt: int) -> bool:
        return self.max_attempts <= 0 or attempt < self.max_attempts


async def _default_connect(url: str) -> WebSocketLike:
    from websockets.asyncio.client import connect  # imported lazily for testability

    return await connect(url, max_size=None)  # type: ignore[return-value]


class ZerodhaMarketDataProvider:
    """Read-only streaming provider. It cannot place, modify or cancel an order."""

    name = "zerodha"

    def __init__(
        self,
        client: ZerodhaRestClient,
        *,
        clock: Clock | None = None,
        connect_factory: ConnectFactory = _default_connect,
        reconnect: ReconnectPolicy | None = None,
        ws_root: str = WS_ROOT,
        stale_after: timedelta = timedelta(seconds=60),
        verify_session: bool = True,
    ) -> None:
        self._client = client
        self._clock = clock or SystemClock()
        self._connect = connect_factory
        self._policy = reconnect or ReconnectPolicy()
        self._ws_root = ws_root
        self._stale_after = stale_after
        self._verify_session = verify_session

        self._socket: WebSocketLike | None = None
        self._state = ProviderState.NOT_CONFIGURED
        self._tokens: set[int] = set()
        self._modes: dict[int, TickMode] = {}
        self._running = False
        self._disconnected_at: datetime | None = None
        self._last_tick_at: datetime | None = None
        self._stale_reported = False

        self.frames_received = 0
        self.ticks_received = 0
        self.reconnect_count = 0
        self.malformed_frames = 0

    # ------------------------------------------------------------------ #
    # state
    # ------------------------------------------------------------------ #

    @property
    def state(self) -> ProviderState:
        return self._state

    @property
    def labelled_state(self) -> str:
        return self._state.labelled(self.name)

    @property
    def subscribed_tokens(self) -> frozenset[int]:
        return frozenset(self._tokens)

    @property
    def last_tick_at(self) -> datetime | None:
        return self._last_tick_at

    @property
    def is_configured(self) -> bool:
        return self._client.is_configured

    def _event(self, event_type: ConnectionEventType, **detail: object) -> ConnectionEvent:
        return ConnectionEvent(
            event_type=event_type,
            provider=self.name,
            occurred_at=self._clock.now(),
            state=self._state,
            detail=detail,
        )

    def _set_state(self, state: ProviderState) -> None:
        if state is not self._state:
            logger.info(
                "provider_state_changed",
                extra={
                    "provider": self.name,
                    "from": self._state.value,
                    "to": state.value,
                },
            )
        self._state = state

    # ------------------------------------------------------------------ #
    # connection
    # ------------------------------------------------------------------ #

    async def authenticate(self) -> ConnectionEvent:
        """Verify configuration and session before any socket is opened."""
        if not self._client.is_configured:
            self._set_state(ProviderState.NOT_CONFIGURED)
            return self._event(
                ConnectionEventType.NOT_CONFIGURED,
                reason="ZERODHA_API_KEY / ZERODHA_ACCESS_TOKEN not set",
            )
        if not self._verify_session:
            self._set_state(ProviderState.AUTHENTICATED)
            return self._event(ConnectionEventType.AUTH_SUCCEEDED, verified=False)
        try:
            profile = await self._client.verify_session()
        except ZerodhaAuthError as exc:
            self._set_state(ProviderState.AUTH_FAILED)
            return self._event(ConnectionEventType.AUTH_FAILED, reason=str(exc))
        except ZerodhaError as exc:
            self._set_state(ProviderState.DISCONNECTED)
            return self._event(ConnectionEventType.PROVIDER_ERROR, reason=str(exc))
        self._set_state(ProviderState.AUTHENTICATED)
        return self._event(
            ConnectionEventType.AUTH_SUCCEEDED,
            user_id=str(profile.get("user_id", "")),
            verified=True,
        )

    async def connect(self) -> None:
        """Authenticate and open the socket. Raises when it cannot."""
        event = await self.authenticate()
        if self._state is ProviderState.NOT_CONFIGURED:
            raise ZerodhaNotConfiguredError(str(event.detail.get("reason", "")))
        if self._state is ProviderState.AUTH_FAILED:
            raise ZerodhaAuthError(str(event.detail.get("reason", "")))
        await self._open_socket()

    async def _open_socket(self) -> None:
        self._set_state(ProviderState.CONNECTING)
        url = self._client.websocket_url(self._ws_root)
        self._socket = await self._connect(url)
        self._set_state(ProviderState.CONNECTED)
        self._stale_reported = False

    async def disconnect(self) -> None:
        self._running = False
        socket, self._socket = self._socket, None
        if socket is not None:
            try:
                await socket.close()
            except Exception:  # noqa: BLE001 - closing must never raise onward
                logger.debug("socket_close_failed", extra={"provider": self.name})
        self._set_state(ProviderState.STOPPED)

    # ------------------------------------------------------------------ #
    # subscription
    # ------------------------------------------------------------------ #

    async def subscribe(self, tokens: Iterable[int], mode: TickMode = TickMode.FULL) -> None:
        """Subscribe, ignoring tokens already subscribed in the same mode."""
        requested = {int(t) for t in tokens}
        new = {t for t in requested if self._modes.get(t) is not mode}
        self._tokens |= requested
        for token in requested:
            self._modes[token] = mode
        if new and self._socket is not None:
            await self._send({"a": "subscribe", "v": sorted(new)})
            await self._send({"a": "mode", "v": [mode.value, sorted(new)]})

    async def unsubscribe(self, tokens: Iterable[int]) -> None:
        requested = {int(t) for t in tokens} & self._tokens
        if not requested:
            return
        self._tokens -= requested
        for token in requested:
            self._modes.pop(token, None)
        if self._socket is not None:
            await self._send({"a": "unsubscribe", "v": sorted(requested)})

    async def _resubscribe(self) -> None:
        """Restore every subscription after a reconnect, grouped by mode."""
        if not self._tokens or self._socket is None:
            return
        by_mode: dict[TickMode, list[int]] = {}
        for token in sorted(self._tokens):
            by_mode.setdefault(self._modes.get(token, TickMode.FULL), []).append(token)
        for mode, tokens in by_mode.items():
            await self._send({"a": "subscribe", "v": tokens})
            await self._send({"a": "mode", "v": [mode.value, tokens]})

    async def _send(self, message: dict[str, Any]) -> None:
        if self._socket is None:
            raise ZerodhaError("cannot send: socket is not connected")
        await self._socket.send(json.dumps(message, separators=(",", ":")))

    # ------------------------------------------------------------------ #
    # streaming
    # ------------------------------------------------------------------ #

    async def stream(self) -> AsyncIterator[StreamEvent]:
        """Run the connect / receive / reconnect loop, yielding events.

        Terminates on authentication failure, on missing configuration, or when
        the reconnect policy is exhausted. It never terminates silently: the
        final event explains why.
        """
        self._running = True
        attempt = 0

        auth_event = await self.authenticate()
        yield auth_event
        if self._state in (ProviderState.NOT_CONFIGURED, ProviderState.AUTH_FAILED):
            self._running = False
            return

        while self._running:
            try:
                yield self._event(ConnectionEventType.CONNECT_ATTEMPT, attempt=attempt + 1)
                await self._open_socket()
            except Exception as exc:  # noqa: BLE001 - any connect failure is retryable
                attempt += 1
                yield self._event(
                    ConnectionEventType.PROVIDER_ERROR,
                    reason=f"{type(exc).__name__}: {exc}",
                    attempt=attempt,
                )
                if not self._policy.should_retry(attempt):
                    self._set_state(ProviderState.STOPPED)
                    return
                await asyncio.sleep(self._policy.delay_for(attempt))
                continue

            reconnected = attempt > 0 or self._disconnected_at is not None
            yield self._event(
                ConnectionEventType.RECONNECTED if reconnected else ConnectionEventType.CONNECTED
            )

            if self._disconnected_at is not None:
                gap = DataGap(
                    provider=self.name,
                    started_at=self._disconnected_at,
                    ended_at=self._clock.now(),
                    reason="reconnect",
                    instrument_tokens=tuple(sorted(self._tokens)),
                )
                self._disconnected_at = None
                self.reconnect_count += 1
                yield gap

            if self._tokens:
                await self._resubscribe()
                yield self._event(ConnectionEventType.RESUBSCRIBED, tokens=len(self._tokens))

            attempt = 0

            try:
                async for event in self._receive_loop():
                    yield event
            except Exception as exc:  # noqa: BLE001 - transport failures are expected
                yield self._event(
                    ConnectionEventType.DISCONNECTED,
                    reason=f"{type(exc).__name__}: {exc}",
                )

            if not self._running:
                break

            self._set_state(ProviderState.DISCONNECTED)
            self._disconnected_at = self._clock.now()
            self._socket = None
            attempt += 1
            if not self._policy.should_retry(attempt):
                yield self._event(
                    ConnectionEventType.PROVIDER_ERROR, reason="reconnect attempts exhausted"
                )
                self._set_state(ProviderState.STOPPED)
                return
            await asyncio.sleep(self._policy.delay_for(attempt))

    async def _receive_loop(self) -> AsyncIterator[StreamEvent]:
        socket = self._socket
        if socket is None:
            return
        while self._running:
            raw = await socket.recv()
            now = self._clock.now()

            if isinstance(raw, str):
                event = self._handle_text(raw, now)
                if event is not None:
                    yield event
                continue

            self.frames_received += 1
            parsed: ParsedFrame = parse_frame(raw, now, source=self.name)

            if parsed.findings:
                self.malformed_frames += 1
                yield self._event(
                    ConnectionEventType.PROVIDER_ERROR,
                    reason="malformed_frame",
                    findings=[f.reason for f in parsed.findings],
                )

            if parsed.heartbeat or not parsed.ticks:
                stale = self._stale_event(now)
                if stale is not None:
                    yield stale
                continue

            self.ticks_received += len(parsed.ticks)
            self._last_tick_at = now
            self._stale_reported = False
            yield TickBatch(ticks=parsed.ticks, received_at=now, provider=self.name)

    def _handle_text(self, raw: str, now: datetime) -> StreamEvent | None:
        """Handle the JSON control messages the server interleaves with ticks."""
        try:
            payload = json.loads(raw)
        except ValueError:
            return self._event(ConnectionEventType.PROVIDER_ERROR, reason="non_json_text_message")
        kind = payload.get("type")
        if kind == "error":
            return self._event(
                ConnectionEventType.PROVIDER_ERROR, reason=str(payload.get("data", ""))
            )
        if kind == "message":
            return self._event(
                ConnectionEventType.PROVIDER_MESSAGE, message=str(payload.get("data", ""))
            )
        # "order" postbacks are order-management traffic. This phase is
        # read-only and has nothing to do with them, so they are ignored.
        return None

    def _stale_event(self, now: datetime) -> ConnectionEvent | None:
        """Report a silent stream once per stale period, not on every frame."""
        if self._last_tick_at is None or self._stale_reported:
            return None
        age = now - self._last_tick_at
        if age < self._stale_after:
            return None
        self._stale_reported = True
        return self._event(
            ConnectionEventType.STREAM_STALE, age_seconds=round(age.total_seconds(), 3)
        )

    # ------------------------------------------------------------------ #

    def health(self, now: datetime | None = None) -> dict[str, object]:
        moment = now or self._clock.now()
        age_ms: float | None = None
        if self._last_tick_at is not None:
            age_ms = round((moment - self._last_tick_at).total_seconds() * 1000, 1)
        return {
            "provider": self.name,
            "state": self.labelled_state,
            "configured": self.is_configured,
            "authenticated": self._state
            in (ProviderState.AUTHENTICATED, ProviderState.CONNECTING, ProviderState.CONNECTED),
            "connected": self._state is ProviderState.CONNECTED,
            "subscribed_instruments": len(self._tokens),
            "last_tick_at": self._last_tick_at.isoformat() if self._last_tick_at else None,
            "last_tick_age_ms": age_ms,
            "frames_received": self.frames_received,
            "ticks_received": self.ticks_received,
            "malformed_frames": self.malformed_frames,
            "reconnects": self.reconnect_count,
        }
