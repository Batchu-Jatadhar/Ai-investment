"""Structured logging: format, correlation and credential redaction."""

from __future__ import annotations

import json
import logging

import pytest

from app.core.logging import (
    REDACTED,
    JsonFormatter,
    RedactionFilter,
    configure_logging,
    get_correlation_id,
    redact,
    set_correlation_id,
)


def _record(msg: str, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, msg, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


class TestJsonFormat:
    def test_emits_parseable_json_with_expected_fields(self) -> None:
        payload = json.loads(JsonFormatter().format(_record("hello", order_id="X1")))
        assert payload["message"] == "hello"
        assert payload["level"] == "INFO"
        assert payload["order_id"] == "X1"
        assert "ts" in payload

    def test_includes_correlation_id(self) -> None:
        set_correlation_id("corr-123")
        record = _record("hi")
        record.correlation_id = get_correlation_id()
        assert json.loads(JsonFormatter().format(record))["correlation_id"] == "corr-123"


class TestCorrelation:
    def test_generates_an_id_when_none_given(self) -> None:
        generated = set_correlation_id(None)
        assert len(generated) >= 16
        assert get_correlation_id() == generated

    def test_accepts_an_incoming_id(self) -> None:
        assert set_correlation_id("abc") == "abc"


class TestRedaction:
    @pytest.mark.parametrize(
        "key",
        [
            "api_key",
            "ZERODHA_API_SECRET",
            "access_token",
            "password",
            "authorization",
            "webhook_secret",
            "private_key",
            "session_id",
        ],
    )
    def test_sensitive_keys_are_masked(self, key: str) -> None:
        assert redact({key: "leaky-value"})[key] == REDACTED

    def test_nested_structures_are_masked(self) -> None:
        out = redact({"outer": {"api_key": "leaky", "safe": "ok"}})
        assert out["outer"]["api_key"] == REDACTED
        assert out["outer"]["safe"] == "ok"

    def test_credential_shaped_values_are_masked_even_under_safe_keys(self) -> None:
        out = redact({"note": "Authorization: Bearer abcdef1234567890xyz"})
        assert "abcdef1234567890xyz" not in out["note"]
        assert REDACTED in out["note"]

    def test_filter_scrubs_record_message_and_extras(self) -> None:
        record = _record(
            "calling broker with Bearer supersecrettokenvalue123",
            access_token="tok-should-vanish",
            symbol="RELIANCE",
        )
        RedactionFilter().filter(record)
        assert "supersecrettokenvalue123" not in record.getMessage()
        assert record.access_token == REDACTED
        assert record.symbol == "RELIANCE"

    def test_end_to_end_handler_does_not_emit_secrets(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_logging(level="INFO", fmt="json")
        logging.getLogger("app.test").info(
            "broker_login", extra={"api_secret": "zzz-secret", "symbol": "TCS"}
        )
        out = capsys.readouterr().out
        assert "zzz-secret" not in out
        assert REDACTED in out
        assert "TCS" in out
