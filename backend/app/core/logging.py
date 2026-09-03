"""Structured application logging.

Design notes for later phases:

*   One line per event, JSON by default, so trading/execution events can be
    added as new ``event`` values without changing the transport.
*   ``correlation_id`` is carried in a context variable and attached to every
    record automatically, so a request (later: a decision run, an order group)
    can be followed end to end.
*   A redaction filter scrubs anything that looks like a credential.  This is a
    backstop, not a licence to log secrets: secrets are ``SecretStr`` in
    settings and are never passed to the logger in the first place.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

# Substrings that mark a field as sensitive, matched case-insensitively.
_SENSITIVE_KEY_PARTS = (
    "secret",
    "token",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "credential",
    "private_key",
    "session",
    "cookie",
)

REDACTED = "***REDACTED***"

# Query-string parameters that carry a credential. Broker WebSocket URLs put the
# access token in the query string (the Kite streaming endpoint is
# ``wss://ws.kite.trade?api_key=...&access_token=...``), so any string that
# quotes such a URL - an exception message, most of all - carries the secret in
# a shape no key-name check can see.
_CREDENTIAL_QUERY_PARAMS = (
    "access_token",
    "api_key",
    "api_secret",
    "auth_token",
    "checksum",
    "password",
    "request_token",
    "secret",
    "token",
)

# Keeps the parameter name, replaces only its value: the endpoint stays
# diagnosable, the credential does not survive.
_QUERY_SECRET_PATTERN = re.compile(
    r"\b(" + "|".join(_CREDENTIAL_QUERY_PARAMS) + r")=[^&\s\"'<>\\]+",
    re.IGNORECASE,
)

# Values that look like credentials even when the key is innocuous.
_SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE),
    re.compile(r"\btoken\s+[A-Za-z0-9._\-]+:[A-Za-z0-9._\-]+", re.IGNORECASE),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9\-_]{20,}"),
)


def sanitize_text(value: str) -> str:
    """Mask credential-shaped content in a free-text string.

    Call this at the point a string is *built* - an exception message about to
    become a persisted event detail, say - not only on the way to a log handler.
    A value that reaches the database never passes through the logging filter,
    so relying on that filter alone leaves the database unprotected.
    """
    value = _QUERY_SECRET_PATTERN.sub(rf"\1={REDACTED}", value)
    for pattern in _SENSITIVE_VALUE_PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def describe_exception(exc: BaseException) -> str:
    """Render an exception as ``Type: message`` with any credential masked.

    Exception text is the most common way a secret escapes: a URL carrying an
    access token lands in a transport error, which is then formatted into an
    event detail and a log line.
    """
    return sanitize_text(f"{type(exc).__name__}: {exc}")


# Reserved LogRecord attributes; anything else in __dict__ is caller-supplied extra.
_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def set_correlation_id(value: str | None = None) -> str:
    """Bind a correlation id to the current context and return it."""
    cid = value or uuid.uuid4().hex
    _correlation_id.set(cid)
    return cid


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, dict):
        return redact(value)
    if isinstance(value, (list, tuple)):
        return [_redact_value(v) for v in value]
    return value


def redact(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``payload`` with sensitive keys and values masked."""
    out: dict[str, Any] = {}
    for key, value in payload.items():
        lowered = str(key).lower()
        if any(part in lowered for part in _SENSITIVE_KEY_PARTS):
            out[key] = REDACTED
        else:
            out[key] = _redact_value(value)
    return out


class RedactionFilter(logging.Filter):
    """Scrub credential-shaped content from the message and any extras."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact_value(record.msg)
        for key in list(record.__dict__):
            if key in _RESERVED:
                continue
            lowered = key.lower()
            if any(part in lowered for part in _SENSITIVE_KEY_PARTS):
                record.__dict__[key] = REDACTED
            else:
                record.__dict__[key] = _redact_value(record.__dict__[key])
        return True


class CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", None),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key in payload:
                continue
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


class ConsoleFormatter(logging.Formatter):
    """Human-readable format for local development."""

    def format(self, record: logging.LogRecord) -> str:
        cid = getattr(record, "correlation_id", None)
        prefix = f"[{cid[:8]}] " if cid else ""
        base = f"{self.formatTime(record)} {record.levelname:<8} {prefix}{record.name}: "
        extras = {
            k: v for k, v in record.__dict__.items() if k not in _RESERVED and k != "correlation_id"
        }
        suffix = f" {extras}" if extras else ""
        text = base + record.getMessage() + suffix
        if record.exc_info:
            text += "\n" + self.formatException(record.exc_info)
        return text


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Install handlers on the root logger. Idempotent."""
    formatter: logging.Formatter = JsonFormatter() if fmt == "json" else ConsoleFormatter()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(CorrelationFilter())
    handler.addFilter(RedactionFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn installs its own handlers; route them through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv = logging.getLogger(name)
        uv.handlers.clear()
        uv.propagate = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
