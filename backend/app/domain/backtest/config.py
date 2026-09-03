"""Backtest configuration value objects.

Configuration only. **None of these types performs a simulation, computes a
charge, or adjusts a price.** They state what the run assumed, so that the
assumption is recorded in the fingerprint and the manifest rather than living
implicitly inside the code that acts on it. The behaviour that reads them
arrives in Phase 2.3 (execution, slippage) and Phase 2.5 (costs).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.core.canonical import canonical_decimal

__all__ = ["CostSchedule", "ExecutionConfig", "SlippageConfig"]


@dataclass(frozen=True, slots=True)
class SlippageConfig:
    """How much the fill is assumed to move against us, in instrument ticks.

    Ticks rather than a percentage, because the tick is the real quantum of
    price movement and Phase 1 already stores ``tick_size`` per instrument.
    Slippage is always adverse: a buy fills higher, a sell fills lower. The
    model that applies it lands in Phase 2.3.
    """

    model_id: str = "fixed_ticks"
    adverse_ticks: int = 1

    def __post_init__(self) -> None:
        if not self.model_id.strip():
            raise ValueError("model_id must name the slippage model")
        if self.adverse_ticks < 0:
            raise ValueError(
                f"adverse_ticks must not be negative, got {self.adverse_ticks}; "
                "slippage is adverse by definition"
            )

    def canonical(self) -> dict[str, str]:
        return {"adverse_ticks": str(self.adverse_ticks), "model_id": self.model_id}


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    """The fill assumptions a run was executed under.

    ``target_requires_through_ticks`` encodes the architecture's accepted cost of
    OCO Design B: the target is fired by the engine as an IOC order, not rested
    at the broker, so a bar that merely grazes the level does not fill. The bar
    must trade *through* it by at least this many ticks.

    ``allow_partial_fills`` exists to make the Phase 2 assumption explicit rather
    than silent. It must be ``False``: partial fills are not modelled, because
    modelling them without order-book depth would be inventing precision we do
    not have. Setting it ``True`` raises rather than being quietly ignored, so a
    manifest can never claim a behaviour the engine does not implement.

    ``gap_quarantine_percent`` guards the most dangerous data hazard in Indian
    equities: an unadjusted series shows a fake ~50% gap on a split day, which a
    breakout strategy reads as a spectacular signal. A session opening beyond
    this distance from the prior close is quarantined and reported, never
    silently traded.
    """

    entry_timing: str = "next_bar_open"
    target_requires_through_ticks: int = 1
    allow_partial_fills: bool = False
    gap_quarantine_percent: Decimal = Decimal("15")

    def __post_init__(self) -> None:
        if self.entry_timing != "next_bar_open":
            raise ValueError(
                f"entry_timing must be 'next_bar_open', got {self.entry_timing!r}; a signal "
                "produced on a bar's close cannot be filled on that same bar"
            )
        if self.target_requires_through_ticks < 0:
            raise ValueError(
                "target_requires_through_ticks must not be negative, got "
                f"{self.target_requires_through_ticks}"
            )
        if self.allow_partial_fills:
            raise ValueError(
                "partial fills are not modelled in Phase 2 and allow_partial_fills must be "
                "False; enabling it would let a run manifest claim a behaviour the execution "
                "simulator does not implement"
            )
        if not isinstance(self.gap_quarantine_percent, Decimal):
            raise TypeError("gap_quarantine_percent must be Decimal, never float")
        if self.gap_quarantine_percent <= 0:
            raise ValueError(
                f"gap_quarantine_percent must be positive, got {self.gap_quarantine_percent}"
            )

    def canonical(self) -> dict[str, str]:
        return {
            "allow_partial_fills": str(self.allow_partial_fills).lower(),
            "entry_timing": self.entry_timing,
            "gap_quarantine_percent": canonical_decimal(self.gap_quarantine_percent),
            "target_requires_through_ticks": str(self.target_requires_through_ticks),
        }


@dataclass(frozen=True, slots=True)
class CostSchedule:
    """Identity and provenance of a transaction-cost schedule.

    **Deliberately carries no rates yet.** The statutory Indian intraday equity
    charges - brokerage, STT, exchange transaction charge, SEBI turnover fee,
    stamp duty and GST - change on known dates and differ between the buy and
    sell leg. They are added in Phase 2.5, and only after each component has been
    verified against a named authoritative source. Guessing them now and fixing
    them later would mean every result produced in between is quietly wrong.

    What this type provides today is the provenance a run must record: which
    schedule was used, which version of it, from when it was effective, where the
    rates came from, and whether anyone has actually checked them.

    ``verified_on`` is the honest flag. A schedule with ``verified_on is None``
    is a placeholder: usable for engine correctness work on synthetic data, and
    not usable for any result that gets reported as a finding.
    """

    schedule_id: str
    version: str
    effective_from: date
    source_url: str = ""
    verified_on: date | None = None

    def __post_init__(self) -> None:
        if not self.schedule_id.strip():
            raise ValueError("schedule_id must name the cost schedule")
        if not self.version.strip():
            raise ValueError("version must identify which revision of the schedule was used")
        if self.verified_on is not None:
            if not self.source_url.strip():
                raise ValueError(
                    "a schedule cannot claim verification without a source_url naming where "
                    "the rates were verified against"
                )
            if self.verified_on < self.effective_from:
                raise ValueError(
                    f"verified_on ({self.verified_on.isoformat()}) precedes effective_from "
                    f"({self.effective_from.isoformat()}); the rates were checked before they "
                    "were in force"
                )

    @property
    def rates_verified(self) -> bool:
        """Whether the rates were checked against a named source.

        Results produced with an unverified schedule must be labelled as such.
        """
        return self.verified_on is not None

    def canonical(self) -> dict[str, str]:
        return {
            "effective_from": self.effective_from.isoformat(),
            "schedule_id": self.schedule_id,
            "source_url": self.source_url,
            "verified_on": self.verified_on.isoformat() if self.verified_on else "",
            "version": self.version,
        }
