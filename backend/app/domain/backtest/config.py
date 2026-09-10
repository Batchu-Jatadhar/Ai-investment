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
from typing import ClassVar

from app.core.canonical import canonical_decimal

__all__ = [
    "NSE_INTRADAY_EQUITY",
    "CostSchedule",
    "ExecutionConfig",
    "SlippageConfig",
]


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
    """A dated, versioned set of statutory charge rates, and its provenance.

    Every rate is a **fraction of the leg's turnover**, not a percentage and not
    a round-trip figure: charges differ by side, so a round trip is the two legs
    computed separately and added. A single blended percentage would be wrong
    for every trade whose two legs are not the same size, which is all of them.

    Rates default to zero. A ``CostSchedule`` built without them is a free
    schedule - the Phase 2.3/2.4 behaviour, where fills carry no costs - and
    :data:`NSE_INTRADAY_EQUITY` is the verified one. That default is deliberate:
    a schedule cannot acquire plausible-looking rates by accident.

    ``verified_on`` is the honest flag, and the type enforces it: a schedule
    claiming verification must name a ``source_url``, and that date cannot
    precede ``effective_from``. A schedule with ``verified_on is None`` is a
    placeholder, usable for engine correctness work on synthetic data and not
    usable for any result reported as a finding.

    Rates live here, as data on a dated value, rather than in environment
    variables or as literals scattered through the arithmetic. A rate that lived
    in the environment could not be fingerprinted, and a run whose costs cannot
    be reproduced from its manifest is not reproducible at all.
    """

    schedule_id: str
    version: str
    effective_from: date
    source_url: str = ""
    verified_on: date | None = None

    #: Brokerage as a fraction of turnover, capped per executed order.
    brokerage_rate: Decimal = Decimal(0)
    brokerage_cap_inr: Decimal | None = None
    #: Securities Transaction Tax. Equity intraday charges the sell leg only.
    stt_sell_rate: Decimal = Decimal(0)
    #: Exchange turnover charge, both legs.
    exchange_transaction_rate: Decimal = Decimal(0)
    #: SEBI turnover fee, both legs.
    sebi_turnover_rate: Decimal = Decimal(0)
    #: Stamp duty. Charged to the buy leg only.
    stamp_duty_buy_rate: Decimal = Decimal(0)
    #: GST, charged on brokerage + SEBI fee + exchange transaction charge only.
    gst_rate: Decimal = Decimal(0)

    RATE_FIELDS: ClassVar[tuple[str, ...]] = (
        "brokerage_rate",
        "stt_sell_rate",
        "exchange_transaction_rate",
        "sebi_turnover_rate",
        "stamp_duty_buy_rate",
        "gst_rate",
    )

    def __post_init__(self) -> None:
        if not self.schedule_id.strip():
            raise ValueError("schedule_id must name the cost schedule")
        if not self.version.strip():
            raise ValueError("version must identify which revision of the schedule was used")

        for name in self.RATE_FIELDS:
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be Decimal, never float")
            if value < 0:
                raise ValueError(f"{name} must not be negative, got {value}")
        if self.brokerage_cap_inr is not None:
            if not isinstance(self.brokerage_cap_inr, Decimal):
                raise TypeError("brokerage_cap_inr must be Decimal, never float")
            if self.brokerage_cap_inr <= 0:
                raise ValueError(
                    f"brokerage_cap_inr must be positive when set, got {self.brokerage_cap_inr}"
                )

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
        """Deterministic rendering, rates included.

        The rates are part of the identity on purpose: two runs over identical
        bars under different charge schedules are different runs, and a
        fingerprint that ignored the rates would claim otherwise.
        """
        rendered = {
            "brokerage_cap_inr": (
                canonical_decimal(self.brokerage_cap_inr)
                if self.brokerage_cap_inr is not None
                else ""
            ),
            "effective_from": self.effective_from.isoformat(),
            "schedule_id": self.schedule_id,
            "source_url": self.source_url,
            "verified_on": self.verified_on.isoformat() if self.verified_on else "",
            "version": self.version,
        }
        rendered.update({name: canonical_decimal(getattr(self, name)) for name in self.RATE_FIELDS})
        return rendered


NSE_INTRADAY_EQUITY = CostSchedule(
    schedule_id="nse-intraday-equity",
    version="2026-03-01",
    effective_from=date(2026, 3, 1),
    source_url="https://zerodha.com/charges/",
    verified_on=date(2026, 9, 10),
    brokerage_rate=Decimal("0.0003"),
    brokerage_cap_inr=Decimal("20"),
    stt_sell_rate=Decimal("0.00025"),
    exchange_transaction_rate=Decimal("0.0000307"),
    sebi_turnover_rate=Decimal("0.000001"),
    stamp_duty_buy_rate=Decimal("0.00003"),
    gst_rate=Decimal("0.18"),
)
"""NSE equity intraday charges, verified 2026-09-10.

Every rate below was read from a named source on the verification date rather
than recalled. What each one is, and where it came from:

``brokerage_rate`` / ``brokerage_cap_inr``
    0.03% of turnover or Rs 20 per executed order, whichever is lower. The cap
    is per *order*, which is why it is applied per leg rather than per trade.
``stt_sell_rate``
    0.025%, **sell leg only**, for equity intraday. The Budget 2026 STT changes
    that took effect on 2026-04-01 raised the futures and options rates and left
    equity intraday alone.
``exchange_transaction_rate``
    0.00307%, both legs. This is the rate that moved most recently: NSE's
    circular of 2026-02-27, effective 2026-03-01, reduced the IPFT contribution
    to Rs 0.01 per crore with corresponding adjustments to transaction charges.
    It supersedes the Rs 2.97 per lakh (0.00297%) rate that had applied since
    2024-10-01, and that older figure is still widely quoted - which is exactly
    why this was checked rather than remembered. The rate is the all-in figure a
    broker bills, so the IPFT component is inside it rather than listed
    separately.
``sebi_turnover_rate``
    Rs 10 per crore, i.e. 0.0001%, both legs.
``stamp_duty_buy_rate``
    0.003% (Rs 300 per crore), **buy leg only**, under the uniform stamp duty
    regime for non-delivery trades.
``gst_rate``
    18%, charged on brokerage + SEBI turnover fee + exchange transaction charge.
    STT and stamp duty are outside the GST base.

DP charges are deliberately absent. They apply per scrip when holdings are
debited on a delivery sale, and an intraday position never reaches the demat
account, so charging one here would invent a cost the trade could not incur.

.. rubric:: Verification caveat

NSE's own site refused every direct connection during verification
(``ECONNRESET`` on both ``nseindia.com`` and the circular archive), so the
0.00307% figure rests on Zerodha's live charges page, independent corroboration
giving the same rate and the same 2026-03-01 effective date, and a confirmed
circular of 2026-02-27 whose effective date matches but whose rate text could not
be read directly. That is three consistent sources rather than one, but it is
not the exchange's own words. Anyone reporting a result from this schedule
should re-read the exchange circular first, and a real contract note settles it
outright.
"""
