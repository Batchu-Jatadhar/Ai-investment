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
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from decimal import Decimal

from app.core.canonical import canonical_decimal
from app.domain.market.models import CandleInterval

__all__ = ["OrbParams"]


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

    def canonical(self) -> dict[str, str]:
        """Deterministic rendering for fingerprints and run manifests."""
        return {
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
