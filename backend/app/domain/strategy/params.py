"""Opening Range Breakout parameters.

.. rubric:: INITIAL FIXED HYPOTHESIS

**Every value below is an INITIAL FIXED HYPOTHESIS. It was chosen a priori from
structural reasoning about how the NSE session works, before any data was seen,
and it must NOT be optimized against the dataset.**

The point of Phase 2 is to falsify a stated hypothesis, not to search for one
that fits. Tuning these against the full history would manufacture an edge that
exists only in the sample, and it would destroy the meaning of the
out-of-sample test that decides whether the strategy is real.

Sensitivity analysis in Phase 2.9 is *diagnostic, not selective*: each value is
varied one step either side to confirm performance degrades smoothly rather
than sitting on a knife edge. A knife-edge optimum is noise. **The best value
found is not adopted.**

Justification for each value, so a later reader can judge the reasoning rather
than the number:

``opening_range_minutes = 15``
    NSE runs a 09:00-09:15 pre-open call auction, so 09:15 is an auction-cleared
    price. The first 15 minutes of continuous trading is the standard window in
    which the auction's price discovery resolves into a direction.
``signal_interval = 5m``
    1m is noise- and cost-dominated; 15m leaves too few decision points in a
    375-minute session. 5m is the conventional intraday compromise.
``resolution_interval = 1m``
    The finest bar Phase 1 stores. 5m bars are aggregated from completed 1m
    bars, so the two series are consistent by construction - which is what makes
    intrabar stop/target ordering resolvable rather than guessed.
``target_r_multiple = 2.0``
    A round, conventional, unfitted choice.
``hard_exit_time = 15:15 IST``
    Before the 15:30 close, so the position is flat ahead of closing-auction
    illiquidity. Being flat overnight also removes gap risk entirely.
``no_new_entry_after = 14:45 IST``
    Leaves at least 30 minutes for a trade to resolve before the hard exit.
``min_range_ticks = 4``
    Rejects a degenerate opening range whose stop would sit inside the spread.
``max_range_atr_multiple = 1.5``
    Rejects a day whose opening range is so wide that a 2R target is unreachable.
``fixed_notional_inr = 100000``
    A sizing placeholder only. Real position sizing is Phase 3; putting it here
    would make the Phase 2 vs Phase 3 comparison impossible to attribute.

.. rubric:: Hypothesis revisions

``atr_interval`` names the bars the prior ATR is measured on, and it is the only
thing that separates the two hypotheses:

**ORB v1** (``OrbParams()``) measures ATR(14) on prior completed **5-minute**
signal bars.

**ORB v2** (:data:`ORB_V2`) measures ATR(14) on prior completed **15-minute**
bars. It exists because of a structural unit mismatch in v1, not because of its
results: ``max_range_atr_multiple`` compares a 15-minute opening range against a
volatility measure, and in v1 that measure is the true range of a single
5-minute bar, a third of the range's span. A 15-minute range is structurally
several multiples of one 5-minute bar, so the comparison could almost never
pass. v2 measures both on the same 15-minute scale. The 1.5 threshold and every
other value are unchanged, and nothing was tuned.

The two are distinct hypotheses with distinct identities: v2 renders
``atr_interval`` into the canonical parameters and is reported as strategy
version ``"2"``. v1's canonical rendering is exactly what it was before v2
existed, so every v1 fingerprint stays reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from decimal import Decimal

from app.core.canonical import canonical_decimal
from app.domain.market.models import CandleInterval

__all__ = ["ORB_V2", "OrbParams"]


@dataclass(frozen=True, slots=True)
class OrbParams:
    """Opening Range Breakout parameters - the INITIAL FIXED HYPOTHESIS.

    ``hard_exit_time`` and ``no_new_entry_after`` are **IST wall-clock times**,
    naive by design and combined with a session date at the point of use. This
    follows the Phase 1 convention where ``NSE_EQUITY_SESSION`` states its
    boundaries the same way: IST is used for session logic, UTC for storage.
    """

    opening_range_minutes: int = 15
    signal_interval: CandleInterval = CandleInterval.M5
    resolution_interval: CandleInterval = CandleInterval.M1
    target_r_multiple: Decimal = Decimal("2.0")
    hard_exit_time: time = time(15, 15)
    no_new_entry_after: time = time(14, 45)
    min_range_ticks: int = 4
    max_range_atr_multiple: Decimal = Decimal("1.5")
    fixed_notional_inr: Decimal = Decimal("100000")
    #: The bars the prior ATR is measured on: ``None`` for the signal bars (ORB v1),
    #: or 15m (ORB v2). See :attr:`atr_bars` for the interval actually used.
    atr_interval: CandleInterval | None = None

    def __post_init__(self) -> None:
        for name in ("target_r_multiple", "max_range_atr_multiple", "fixed_notional_inr"):
            if not isinstance(getattr(self, name), Decimal):
                raise TypeError(f"{name} must be Decimal, never float")
        for name in ("hard_exit_time", "no_new_entry_after"):
            if getattr(self, name).tzinfo is not None:
                raise ValueError(
                    f"{name} is an IST wall-clock time and must be naive; it is combined "
                    "with a session date at the point of use"
                )

        if self.opening_range_minutes <= 0:
            raise ValueError(
                f"opening_range_minutes must be positive, got {self.opening_range_minutes}"
            )
        if self.resolution_interval.seconds > self.signal_interval.seconds:
            raise ValueError(
                f"resolution_interval ({self.resolution_interval.value}) must not be coarser "
                f"than signal_interval ({self.signal_interval.value})"
            )
        if self.signal_interval.seconds % self.resolution_interval.seconds != 0:
            raise ValueError(
                f"signal_interval ({self.signal_interval.value}) must be a whole multiple of "
                f"resolution_interval ({self.resolution_interval.value}), so every signal bar "
                "maps onto a whole number of resolution bars"
            )
        if (self.opening_range_minutes * 60) % self.signal_interval.seconds != 0:
            raise ValueError(
                f"opening_range_minutes ({self.opening_range_minutes}) must be a whole "
                f"multiple of signal_interval ({self.signal_interval.value}), otherwise the "
                "opening range does not end on a bar boundary"
            )
        if self.target_r_multiple <= 0:
            raise ValueError(f"target_r_multiple must be positive, got {self.target_r_multiple}")
        if self.max_range_atr_multiple <= 0:
            raise ValueError(
                f"max_range_atr_multiple must be positive, got {self.max_range_atr_multiple}"
            )
        if self.min_range_ticks < 1:
            raise ValueError(f"min_range_ticks must be at least 1, got {self.min_range_ticks}")
        if self.fixed_notional_inr <= 0:
            raise ValueError(f"fixed_notional_inr must be positive, got {self.fixed_notional_inr}")
        if self.no_new_entry_after >= self.hard_exit_time:
            raise ValueError(
                f"no_new_entry_after ({self.no_new_entry_after.isoformat()}) must precede "
                f"hard_exit_time ({self.hard_exit_time.isoformat()}), otherwise a trade could "
                "be opened at or after the moment it must be closed"
            )
        if self.atr_interval is not None and (
            self.atr_interval is not CandleInterval.M15
            or self.signal_interval.seconds >= CandleInterval.M15.seconds
        ):
            raise ValueError(
                f"atr_interval must be None (ATR on the signal bars, ORB v1) or 15m over a finer "
                f"signal interval (ORB v2), got {self.atr_interval.value} with "
                f"{self.signal_interval.value} signal bars"
            )

    @property
    def hypothesis_version(self) -> str:
        """``"1"`` when ATR is measured on the signal bars, ``"2"`` when on 15m bars."""
        return "1" if self.atr_interval is None else "2"

    @property
    def atr_bars(self) -> CandleInterval:
        """The interval of the bars the prior ATR is computed over."""
        return self.signal_interval if self.atr_interval is None else self.atr_interval

    def canonical(self) -> dict[str, str]:
        """Deterministic rendering for fingerprints and run manifests.

        ``atr_interval`` is rendered only for v2. Omitting it for v1 keeps v1's
        rendering byte-for-byte what it was before v2 existed, so no v1
        fingerprint changed; v2 inputs cannot collide with v1 because they carry
        the extra key.
        """
        rendered = {
            "fixed_notional_inr": canonical_decimal(self.fixed_notional_inr),
            "hard_exit_time": self.hard_exit_time.isoformat(),
            "max_range_atr_multiple": canonical_decimal(self.max_range_atr_multiple),
            "min_range_ticks": str(self.min_range_ticks),
            "no_new_entry_after": self.no_new_entry_after.isoformat(),
            "opening_range_minutes": str(self.opening_range_minutes),
            "resolution_interval": self.resolution_interval.value,
            "signal_interval": self.signal_interval.value,
            "target_r_multiple": canonical_decimal(self.target_r_multiple),
        }
        if self.atr_interval is not None:
            rendered["atr_interval"] = self.atr_interval.value
        return rendered


ORB_V2 = OrbParams(atr_interval=CandleInterval.M15)
"""ORB v2: v1 with the prior ATR measured on 15-minute bars. See the module docstring."""
