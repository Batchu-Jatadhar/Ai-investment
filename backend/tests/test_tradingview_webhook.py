"""TradingView alert webhook: accepted, rejected, duplicate, malformed, stale, unauthenticated.

Real application, real migrated database; only the clock is fixed.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.api.webhooks import get_clock
from app.config.settings import TradingMode, clear_settings_cache, set_settings_override
from app.core.time import FixedClock
from app.domain.alerts import AlertAction, ExternalAlert
from app.infrastructure import db as db_module
from app.infrastructure.models import WebhookEventRecord
from app.infrastructure.repositories.alerts import SqlAlertLedger
from tests.conftest import build_settings

SECRET = "tv-webhook-test-secret-9f2c"
NOW = datetime(2026, 8, 21, 3, 50, tzinfo=UTC)  # 09:20 IST
URL = "/webhooks/tradingview"


def payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "secret": SECRET,
        "alert_id": "orb-reliance-2026-08-21T03:50:00Z",
        "symbol": "NSE:RELIANCE",
        "action": "long",
        "timestamp": "2026-08-21T03:50:00Z",
        "note": "opening range broken",
    }
    body.update(overrides)
    return {k: v for k, v in body.items() if v is not DROP}


DROP = object()

ClientFactory = Callable[..., TestClient]


@pytest.fixture
def make_client(migrated_database_url: str) -> Iterator[ClientFactory]:
    """A TestClient over a fresh app, with the webhook table emptied first."""
    clients: list[TestClient] = []

    def factory(**settings_overrides: object) -> TestClient:
        from app.main import create_app

        values: dict[str, object] = {
            "database_url": migrated_database_url,
            "tradingview_webhook_secret": SECRET,
        }
        values.update(settings_overrides)
        settings = build_settings(**values)
        db_module.reset_engine()
        set_settings_override(settings)
        with db_module.get_engine(settings).begin() as conn:
            conn.execute(text("DELETE FROM webhook_event"))
        app = create_app(settings)
        app.dependency_overrides[get_clock] = lambda: FixedClock(NOW)
        client = TestClient(app)
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.close()
    set_settings_override(None)
    clear_settings_cache()
    db_module.reset_engine()


def stored() -> list[WebhookEventRecord]:
    with db_module.get_session_factory()() as session:
        return list(session.execute(select(WebhookEventRecord)).scalars())


def post(client: TestClient, body: object, **kwargs: object):  # noqa: ANN201
    content = body if isinstance(body, bytes | str) else json.dumps(body)
    return client.post(URL, content=content, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# accepted
# --------------------------------------------------------------------------- #


def test_a_valid_alert_is_accepted_normalised_and_recorded(make_client: ClientFactory) -> None:
    client = make_client()

    response = post(client, payload(), headers={"Content-Type": "text/plain"})

    assert response.status_code == 202
    assert response.json() == {
        "status": "accepted",
        "source": "tradingview",
        "event_id": "orb-reliance-2026-08-21T03:50:00Z",
        "symbol": "NSE:RELIANCE",
        "action": "long",
    }
    (row,) = stored()
    assert (row.source, row.event_id, row.exchange, row.tradingsymbol, row.action) == (
        "tradingview",
        "orb-reliance-2026-08-21T03:50:00Z",
        "NSE",
        "RELIANCE",
        "long",
    )
    assert row.note == "opening range broken"


def test_an_ist_timestamp_is_normalised_to_utc(make_client: ClientFactory) -> None:
    client = make_client()

    response = post(client, payload(timestamp="2026-08-21T09:19:30+05:30", action="exit"))

    assert response.status_code == 202
    (row,) = stored()
    occurred = row.occurred_at if row.occurred_at.tzinfo else row.occurred_at.replace(tzinfo=UTC)
    assert occurred == datetime(2026, 8, 21, 3, 49, 30, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# duplicates
# --------------------------------------------------------------------------- #


def test_the_same_alert_is_accepted_once(make_client: ClientFactory) -> None:
    client = make_client()

    first = post(client, payload())
    again = post(client, payload())
    same_id_other_body = post(client, payload(action="short", note="changed"))

    assert first.status_code == 202
    assert (again.status_code, again.json()["code"]) == (409, "webhook_duplicate")
    assert (same_id_other_body.status_code, same_id_other_body.json()["code"]) == (
        409,
        "webhook_duplicate",
    )
    assert len(stored()) == 1


def test_the_ledger_arbitrates_duplicates_atomically(make_client: ClientFactory) -> None:
    make_client()
    ledger = SqlAlertLedger(db_module.get_session_factory())
    alert = ExternalAlert(
        source="tradingview",
        event_id="evt-1",
        exchange="NSE",
        tradingsymbol="INFY",
        action=AlertAction.NOTIFY,
        occurred_at=NOW,
        received_at=NOW,
    )

    assert [ledger.record_if_new(alert) for _ in range(3)] == [True, False, False]
    assert len(stored()) == 1


# --------------------------------------------------------------------------- #
# unauthenticated
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(payload(secret=DROP), id="missing-secret"),
        pytest.param(payload(secret="wrong-secret"), id="wrong-secret"),
        pytest.param(payload(secret=SECRET + "x"), id="secret-prefix-match"),
        pytest.param(payload(secret=12345), id="non-string-secret"),
        pytest.param(
            payload(secret="wrong", symbol="bad symbol", timestamp="stale"), id="bad-and-wrong"
        ),
    ],
)
def test_unauthenticated_alerts_are_rejected_before_validation(
    make_client: ClientFactory, body: dict[str, object]
) -> None:
    client = make_client()

    response = post(client, body)

    assert (response.status_code, response.json()["code"]) == (401, "webhook_unauthorized")
    assert "errors" not in response.json()  # nothing about the contract leaks
    assert stored() == []


# --------------------------------------------------------------------------- #
# malformed
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("body", "field"),
    [
        pytest.param(b"{not json", None, id="not-json"),
        pytest.param(b"[1, 2, 3]", None, id="json-array"),
        pytest.param(payload(alert_id=DROP), "alert_id", id="missing-alert-id"),
        pytest.param(payload(alert_id="bad id with spaces"), "alert_id", id="bad-alert-id"),
        pytest.param(payload(alert_id="x" * 129), "alert_id", id="alert-id-too-long"),
        pytest.param(payload(symbol=DROP), "symbol", id="missing-symbol"),
        pytest.param(payload(symbol="RELIANCE"), "symbol", id="symbol-without-exchange"),
        pytest.param(payload(symbol="nse:reliance"), "symbol", id="lowercase-symbol"),
        pytest.param(payload(action="buy"), "action", id="unknown-action"),
        pytest.param(payload(action=DROP), "action", id="missing-action"),
        pytest.param(payload(timestamp=DROP), "timestamp", id="missing-timestamp"),
        pytest.param(payload(timestamp="yesterday"), "timestamp", id="unparseable-timestamp"),
        pytest.param(payload(timestamp="2026-08-21T03:50:00"), "timestamp", id="naive-timestamp"),
        pytest.param(payload(timestamp=1787284200), "timestamp", id="epoch-timestamp"),
        pytest.param(payload(quantity=10), "quantity", id="quantity-field"),
        pytest.param(payload(price="1400.05"), "price", id="price-field"),
        pytest.param(payload(stop="1390"), "stop", id="stop-field"),
        pytest.param(payload(target="1420"), "target", id="target-field"),
        pytest.param(payload(note="n" * 281), "note", id="note-too-long"),
    ],
)
def test_malformed_alerts_are_rejected(
    make_client: ClientFactory, body: object, field: str | None
) -> None:
    client = make_client()

    response = post(client, body)

    problem = response.json()
    assert (response.status_code, problem["code"]) == (422, "webhook_malformed")
    if field is not None:
        assert field in {error["field"] for error in problem["errors"]}
    assert SECRET not in response.text
    assert stored() == []


# --------------------------------------------------------------------------- #
# stale and future
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("offset", "accepted"),
    [
        pytest.param(timedelta(seconds=-60), True, id="exactly-max-age"),
        pytest.param(timedelta(seconds=-61), False, id="one-second-too-old"),
        pytest.param(timedelta(hours=-6), False, id="hours-old"),
        pytest.param(timedelta(seconds=30), True, id="exactly-max-future"),
        pytest.param(timedelta(seconds=31), False, id="too-far-in-future"),
    ],
)
def test_freshness_is_enforced_at_the_configured_bounds(
    make_client: ClientFactory, offset: timedelta, accepted: bool
) -> None:
    client = make_client()
    stamp = (NOW + offset).isoformat()

    response = post(
        client, payload(timestamp=stamp, alert_id=f"fresh-{offset.total_seconds():.0f}")
    )

    if accepted:
        assert response.status_code == 202
        assert len(stored()) == 1
    else:
        assert (response.status_code, response.json()["code"]) == (422, "webhook_stale")
        assert stored() == []


# --------------------------------------------------------------------------- #
# size, mode, configuration
# --------------------------------------------------------------------------- #


def test_an_oversized_body_is_rejected_before_anything_else(make_client: ClientFactory) -> None:
    client = make_client(tradingview_webhook_max_body_bytes=512)
    oversized = payload(note="n" * 280, alert_id="a" * 128, secret="wrong" * 60)

    response = post(client, oversized)

    assert len(json.dumps(oversized)) > 512
    assert (response.status_code, response.json()["code"]) == (413, "webhook_payload_too_large")
    assert stored() == []


def test_only_paper_mode_accepts_alerts(make_client: ClientFactory) -> None:
    client = make_client(trading_mode=TradingMode.BACKTEST)

    response = post(client, payload())

    assert (response.status_code, response.json()["code"]) == (403, "webhook_mode_not_permitted")
    assert stored() == []


@pytest.mark.parametrize("secret", [None, ""])
def test_without_a_configured_secret_nothing_is_accepted(
    make_client: ClientFactory, secret: str | None
) -> None:
    client = make_client(tradingview_webhook_secret=secret)

    response = post(client, payload(secret=""))

    assert (response.status_code, response.json()["code"]) == (503, "webhook_not_configured")
    assert stored() == []


def test_only_post_is_allowed(make_client: ClientFactory) -> None:
    client = make_client()
    assert client.get(URL).status_code == 405
    assert client.put(URL, content=json.dumps(payload())).status_code == 405
    assert stored() == []
