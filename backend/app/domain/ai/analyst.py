"""The AI analyst contract: what it may see, what it may say, and how that is checked.

The analyst is a **filter**. It is shown a signal the strategy already produced
(and, where one exists, the risk decision already made) and answers one of three
words. It cannot propose a trade, and its answer has nowhere to put a price, a
stop, a target, a quantity, leverage, an order type or a broker command.

:func:`parse_analyst_response` is the only way an answer becomes an
:class:`AnalystVerdict`. It is strict: an exact field set, exact decision
spelling, no floats, no extra keys - and a key naming an execution parameter is
reported as such. Anything it refuses raises :class:`MalformedAnalystResponseError`,
which callers treat as "do not trade". A malformed answer never approves.

``confidence`` is optional and recorded for later analysis only. Nothing decides
on it: the decision word is the whole verdict.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.domain.market.models import Candle
from app.domain.risk.sizing import RiskDecision
from app.domain.strategy.contract import Signal

__all__ = [
    "FORBIDDEN_FIELDS",
    "AnalystContext",
    "AnalystDecision",
    "AnalystVerdict",
    "MalformedAnalystResponseError",
    "parse_analyst_response",
]

MAX_REASON_LENGTH = 280

#: Keys an analyst response may never contain. Named so a refusal says *why*.
FORBIDDEN_FIELDS = frozenset(
    {
        "entry",
        "entry_price",
        "price",
        "limit_price",
        "trigger_price",
        "stop",
        "stop_loss",
        "stop_price",
        "target",
        "target_price",
        "target_r_multiple",
        "take_profit",
        "quantity",
        "qty",
        "size",
        "lots",
        "leverage",
        "margin",
        "order_type",
        "order",
        "side",
        "direction",
        "product",
        "broker",
        "command",
    }
)

_REQUIRED = frozenset({"decision", "reason", "model_id", "prompt_version"})
_ALLOWED = _REQUIRED | {"confidence"}


class AnalystDecision(StrEnum):
    TAKE_TRADE = "TAKE_TRADE"
    WAIT = "WAIT"
    REJECT = "REJECT"


class MalformedAnalystResponseError(ValueError):
    """The response is not a valid verdict. ``code`` is machine-readable."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class AnalystContext:
    """Everything the analyst is allowed to see. Nothing here is a future bar.

    ``session_bars`` ends with the bar the signal was produced on. ``risk`` is
    ``None`` where no risk decision is made (the Phase 2 backtest sizes by fixed
    notional), and when present it is an approved one: a rejected proposal never
    reaches the analyst.
    """

    signal: Signal
    session_bars: tuple[Candle, ...]
    prior_atr: Decimal | None = None
    risk: RiskDecision | None = None

    def __post_init__(self) -> None:
        if not self.session_bars:
            raise ValueError("the analyst must be shown the bars that produced the signal")
        if self.session_bars[-1].start_at != self.signal.signal_bar_start:
            raise ValueError("session_bars must end with the signal bar and contain nothing later")
        if self.risk is not None and not self.risk.approved:
            raise ValueError("a rejected risk decision is final and is never shown to the analyst")


@dataclass(frozen=True, slots=True)
class AnalystVerdict:
    decision: AnalystDecision
    reason: str
    model_id: str
    prompt_version: str
    confidence: Decimal | None = None


def _text(payload: Mapping[str, object], name: str) -> str:
    value = payload[name]
    if not isinstance(value, str) or not value.strip():
        raise MalformedAnalystResponseError("invalid_field", f"{name} must be a non-empty string")
    return value.strip()


def parse_analyst_response(raw: object) -> AnalystVerdict:
    """Validate a raw response (a JSON string or a mapping) into a verdict, or raise."""
    if isinstance(raw, str | bytes):
        try:
            raw = json.loads(raw, parse_float=Decimal)
        except ValueError as exc:
            raise MalformedAnalystResponseError("invalid_json", str(exc)) from None
    if not isinstance(raw, Mapping):
        raise MalformedAnalystResponseError("not_an_object", f"got {type(raw).__name__}")
    if not all(isinstance(key, str) for key in raw):
        raise MalformedAnalystResponseError("invalid_field", "every key must be a string")

    keys = set(raw)
    forbidden = sorted(keys & FORBIDDEN_FIELDS)
    if forbidden:
        raise MalformedAnalystResponseError(
            "forbidden_field", f"the analyst may not supply execution parameters: {forbidden}"
        )
    unknown = sorted(keys - _ALLOWED)
    if unknown:
        raise MalformedAnalystResponseError("unknown_field", f"unexpected fields {unknown}")
    missing = sorted(_REQUIRED - keys)
    if missing:
        raise MalformedAnalystResponseError("missing_field", f"required fields absent: {missing}")

    decision_raw = raw["decision"]
    if not isinstance(decision_raw, str) or decision_raw not in AnalystDecision.__members__:
        raise MalformedAnalystResponseError(
            "invalid_decision",
            f"decision must be exactly one of {list(AnalystDecision.__members__)}, "
            f"got {decision_raw!r}",
        )

    reason = _text(raw, "reason")
    if len(reason) > MAX_REASON_LENGTH:
        raise MalformedAnalystResponseError(
            "invalid_field", f"reason must be at most {MAX_REASON_LENGTH} characters"
        )

    confidence = raw.get("confidence")
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, Decimal | int):
            raise MalformedAnalystResponseError(
                "invalid_field", "confidence must be an exact number, never float"
            )
        confidence = Decimal(confidence)
        if not confidence.is_finite() or not Decimal(0) <= confidence <= Decimal(1):
            raise MalformedAnalystResponseError("invalid_field", "confidence must be within [0, 1]")

    return AnalystVerdict(
        decision=AnalystDecision(decision_raw),
        reason=reason,
        model_id=_text(raw, "model_id"),
        prompt_version=_text(raw, "prompt_version"),
        confidence=confidence,
    )
