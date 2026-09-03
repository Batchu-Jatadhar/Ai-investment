"""Backtest result value objects.

Pure data. **Nothing here simulates, fills, prices or computes a metric.** The
components that produce these values arrive later: fills in Phase 2.3, trades
and the equity curve in Phase 2.4, metrics in Phase 2.5.

What these types do own is their own consistency. A :class:`Trade` whose net
does not equal gross minus costs, or whose entry and exit quantities differ, is
not a trade this engine could have produced - so it cannot be constructed. That
is validation of a value, not calculation of one, and it is what turns a
portfolio-accounting bug in a later milestone into an immediate failure rather
than a plausible-looking number.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from app.core.time import ensure_utc
from app.domain.strategy.contract import Signal, SignalDirection

__all__ = [
    "AmbiguityResolution",
    "BacktestResult",
    "EquityPoint",
    "Fill",
    "FillReason",
    "OrderSide",
    "RunManifest",
    "SignalRecord",
    "Trade",
]

_HEX_DIGITS = frozenset("0123456789abcdef")


class OrderSide(StrEnum):
    """Which side a fill traded.

    Distinct from :class:`~app.domain.strategy.contract.SignalDirection` because
    the two answer different questions: a LONG signal produces a BUY entry and a
    SELL exit. Costs depend on this side, not on the direction - Indian STT
    applies to the sell leg only and stamp duty to the buy leg only.
    """

    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> OrderSide:
        return OrderSide.SELL if self is OrderSide.BUY else OrderSide.BUY

    @classmethod
    def entry_for(cls, direction: SignalDirection) -> OrderSide:
        return cls.BUY if direction.is_long else cls.SELL


class FillReason(StrEnum):
    """Why a fill happened. Every exit must name its cause."""

    ENTRY = "entry"
    STOP = "stop"
    TARGET = "target"
    TIME_EXIT = "time_exit"

    @property
    def is_exit(self) -> bool:
        return self is not FillReason.ENTRY


class AmbiguityResolution(StrEnum):
    """How a bar containing both the stop and the target was resolved.

    Recorded per trade because it is a quality signal about the result, not an
    implementation detail. A backtest in which many exits fell back to the
    pessimistic assumption is weaker than one in which most were resolved from
    the 1-minute series, and the report must be able to say so.
    """

    UNAMBIGUOUS = "unambiguous"
    RESOLVED_BY_1M = "resolved_by_1m"
    PESSIMISTIC_FALLBACK = "pessimistic_fallback"


@dataclass(frozen=True, slots=True)
class Fill:
    """One executed leg.

    ``reference_price`` is the price before slippage was applied - the bar open,
    the stop level, or the target level. Keeping it alongside ``price`` makes
    every fill auditable: the difference between them is the slippage, and a
    reader can check that by eye.
    """

    side: OrderSide
    reason: FillReason
    quantity: int
    price: Decimal
    reference_price: Decimal
    slippage_per_unit: Decimal
    costs: Decimal
    occurred_at: datetime
    bar_start: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", ensure_utc(self.occurred_at))
        object.__setattr__(self, "bar_start", ensure_utc(self.bar_start))
        for name in ("price", "reference_price", "slippage_per_unit", "costs"):
            if not isinstance(getattr(self, name), Decimal):
                raise TypeError(f"{name} must be Decimal, never float")
        if self.quantity <= 0:
            raise ValueError(f"quantity must be positive, got {self.quantity}")
        if self.price <= 0:
            raise ValueError(f"price must be positive, got {self.price}")
        if self.reference_price <= 0:
            raise ValueError(f"reference_price must be positive, got {self.reference_price}")
        if self.slippage_per_unit < 0:
            raise ValueError(
                f"slippage_per_unit must not be negative, got {self.slippage_per_unit}; "
                "slippage is adverse by definition and its direction comes from the side"
            )
        if self.costs < 0:
            raise ValueError(f"costs must not be negative, got {self.costs}")


@dataclass(frozen=True, slots=True)
class Trade:
    """A completed round trip: one entry fill and one exit fill.

    Phase 2 does not model partial fills, so a trade is exactly two fills of
    equal quantity. The arithmetic identities below are validated rather than
    computed - whoever builds the trade does the arithmetic, and this type
    refuses to hold a result that does not add up.
    """

    instrument_token: int
    direction: SignalDirection
    entry: Fill
    exit: Fill
    gross_pnl: Decimal
    costs: Decimal
    net_pnl: Decimal
    exit_reason: FillReason
    ambiguity: AmbiguityResolution = AmbiguityResolution.UNAMBIGUOUS
    r_multiple: Decimal | None = None

    def __post_init__(self) -> None:
        for name in ("gross_pnl", "costs", "net_pnl"):
            if not isinstance(getattr(self, name), Decimal):
                raise TypeError(f"{name} must be Decimal, never float")
        if self.r_multiple is not None and not isinstance(self.r_multiple, Decimal):
            raise TypeError("r_multiple must be Decimal or None, never float")
        if self.instrument_token <= 0:
            raise ValueError(f"instrument_token must be positive, got {self.instrument_token}")

        if self.entry.reason is not FillReason.ENTRY:
            raise ValueError(f"entry fill must have reason ENTRY, got {self.entry.reason.value}")
        if not self.exit.reason.is_exit:
            raise ValueError(f"exit fill must have an exit reason, got {self.exit.reason.value}")
        if self.exit.reason is not self.exit_reason:
            raise ValueError(
                f"exit_reason ({self.exit_reason.value}) must match the exit fill's reason "
                f"({self.exit.reason.value})"
            )

        expected_entry_side = OrderSide.entry_for(self.direction)
        if self.entry.side is not expected_entry_side:
            raise ValueError(
                f"a {self.direction.value} trade must enter {expected_entry_side.value}, "
                f"got {self.entry.side.value}"
            )
        if self.exit.side is not expected_entry_side.opposite:
            raise ValueError(
                f"a {self.direction.value} trade must exit "
                f"{expected_entry_side.opposite.value}, got {self.exit.side.value}"
            )
        if self.entry.quantity != self.exit.quantity:
            raise ValueError(
                f"entry quantity ({self.entry.quantity}) must equal exit quantity "
                f"({self.exit.quantity}); partial fills are not modelled in Phase 2"
            )
        if self.exit.occurred_at < self.entry.occurred_at:
            raise ValueError(
                f"exit ({self.exit.occurred_at.isoformat()}) cannot precede entry "
                f"({self.entry.occurred_at.isoformat()})"
            )

        quantity = Decimal(self.entry.quantity)
        move = (
            self.exit.price - self.entry.price
            if self.direction.is_long
            else self.entry.price - self.exit.price
        )
        expected_gross = move * quantity
        if self.gross_pnl != expected_gross:
            raise ValueError(
                f"gross_pnl ({self.gross_pnl}) does not match the fills: a "
                f"{self.direction.value} of {self.entry.quantity} from {self.entry.price} to "
                f"{self.exit.price} is {expected_gross}"
            )
        expected_costs = self.entry.costs + self.exit.costs
        if self.costs != expected_costs:
            raise ValueError(
                f"costs ({self.costs}) must be the sum of both legs ({expected_costs})"
            )
        if self.net_pnl != self.gross_pnl - self.costs:
            raise ValueError(
                f"net_pnl ({self.net_pnl}) must equal gross_pnl minus costs "
                f"({self.gross_pnl - self.costs})"
            )


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """One sample of the equity curve."""

    at: datetime
    cash: Decimal
    position_value: Decimal
    equity: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", ensure_utc(self.at))
        for name in ("cash", "position_value", "equity"):
            if not isinstance(getattr(self, name), Decimal):
                raise TypeError(f"{name} must be Decimal, never float")
        if self.equity != self.cash + self.position_value:
            raise ValueError(
                f"equity ({self.equity}) must equal cash plus position_value "
                f"({self.cash + self.position_value})"
            )


@dataclass(frozen=True, slots=True)
class SignalRecord:
    """One signal and what was decided about it.

    Recorded for **every** signal, taken or not. This is what makes the later
    phase comparisons exact rather than merely different: Phase 3 runs the same
    signal set through a risk gate, and Phase 5 runs it through an AI that may
    only subtract. Because the AI's verdict space is TAKE_TRADE / WAIT / REJECT
    with no price or quantity, an AI-armed run's accepted set must be a strict
    subset of the strategy-only run's accepted set - which is a testable
    invariant, and this log is what makes it testable.

    In Phase 2 every record is accepted by ``"strategy"``.
    """

    signal: Signal
    accepted: bool
    decision_reason: str
    decided_by: str = "strategy"

    def __post_init__(self) -> None:
        if not self.decision_reason.strip():
            raise ValueError("decision_reason must say why the signal was accepted or rejected")
        if not self.decided_by.strip():
            raise ValueError("decided_by must name the stage that made the decision")


@dataclass(frozen=True, slots=True)
class RunManifest:
    """Everything needed to identify and reproduce a run.

    ``input_fingerprint`` is the SHA-256 of the :class:`BacktestInput`, and it is
    the reproducibility key: the same fingerprint and the same engine version
    must yield the same result.

    ``generated_at`` is wall-clock and therefore deliberately **outside** the
    fingerprint - it lives here, on the manifest, and never contributes to the
    input's identity. Two runs of the same input at different times are the same
    run. The value is supplied by the caller from an injected clock; nothing in
    this package reads a clock.
    """

    input_fingerprint: str
    strategy_name: str
    strategy_version: str
    engine_version: str
    generated_at: datetime
    git_commit: str | None = None
    seed: int | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "generated_at", ensure_utc(self.generated_at))
        if len(self.input_fingerprint) != 64 or not _HEX_DIGITS.issuperset(self.input_fingerprint):
            raise ValueError(
                "input_fingerprint must be a lowercase SHA-256 hex digest, got "
                f"{self.input_fingerprint!r}"
            )
        for name in ("strategy_name", "strategy_version", "engine_version"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must be recorded so a run can be identified")


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """The complete output of one backtest run.

    A value object. It computes nothing: the typed performance-metrics field is
    added in Phase 2.5, once the metrics themselves exist.
    """

    manifest: RunManifest
    signal_log: tuple[SignalRecord, ...] = ()
    trades: tuple[Trade, ...] = ()
    equity_curve: tuple[EquityPoint, ...] = ()

    def __post_init__(self) -> None:
        for name in ("signal_log", "trades", "equity_curve"):
            if not isinstance(getattr(self, name), tuple):
                raise TypeError(
                    f"{name} must be a tuple; a result is immutable so that it cannot be "
                    "edited after the run that produced it"
                )
        previous: datetime | None = None
        for point in self.equity_curve:
            if previous is not None and point.at < previous:
                raise ValueError(
                    f"equity_curve must be ordered in time; {point.at.isoformat()} follows "
                    f"{previous.isoformat()}"
                )
            previous = point.at
