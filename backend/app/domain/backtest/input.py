"""The immutable input to one backtest run.

Everything a backtest consumes, frozen into a single validated value: the
instrument, both candle series, the session calendar, the strategy parameters,
and the cost, slippage and execution configuration.

**Determinism here is structural, not a matter of discipline.** A
:class:`BacktestInput` holds no repository, no clock, no provider, no settings
object and no file handle. Given only this value, the engine has nothing
time-varying or external it could reach even if it tried. That is why the
reproducibility guarantee - same input, same result - is something the type
system enforces rather than something a reviewer has to check.

The other half of the guarantee is :meth:`BacktestInput.fingerprint`, a SHA-256
over a canonical rendering of the logical data. It is stable across processes
and machines, and it deliberately excludes wall-clock time and provenance
metadata: two ingestions of the same bars are the same input.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime

from app.core.canonical import canonical_datetime, canonical_decimal
from app.core.time import to_ist
from app.domain.backtest.config import CostSchedule, ExecutionConfig, SlippageConfig
from app.domain.market.models import Candle, CandleInterval, CandleStatus, Instrument
from app.domain.market.session import MarketSessionCalendar
from app.domain.strategy.params import OrbParams

__all__ = ["BacktestInput", "InvalidBacktestInputError"]

#: Bumped only when the canonical rendering changes shape. A fingerprint is
#: meaningless without knowing which rendering produced it.
FINGERPRINT_SCHEMA = "aitrade.backtest.input.v1"


class InvalidBacktestInputError(ValueError):
    """Raised when a backtest input could not be a real, tradable data set.

    Following the Phase 1 pattern (``UniverseSpecError``), this is a
    ``ValueError`` subclass so callers can catch it precisely without matching
    on message text.
    """


def _candle_line(candle: Candle) -> str:
    """Canonical one-line rendering of a candle's logical content.

    Deliberately excludes ``source``, ``tradingsymbol``, ``exchange`` and
    ``tick_count``. Those are provenance and metadata, not data: the same bar
    re-ingested from a different source, or replayed rather than streamed, is
    still the same bar, and a backtest over it must fingerprint identically.
    """
    return "|".join(
        (
            canonical_datetime(candle.start_at),
            canonical_datetime(candle.end_at),
            canonical_decimal(candle.open),
            canonical_decimal(candle.high),
            canonical_decimal(candle.low),
            canonical_decimal(candle.close),
            str(candle.volume),
            candle.status.value,
        )
    )


def _series_digest(candles: tuple[Candle, ...]) -> str:
    """SHA-256 over every candle in order.

    Digesting the series separately keeps the top-level payload small and
    readable while staying fully sensitive to any change in any bar.
    """
    digest = hashlib.sha256()
    for candle in candles:
        digest.update(_candle_line(candle).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _series_payload(candles: tuple[Candle, ...], interval: CandleInterval) -> dict[str, str]:
    return {
        "count": str(len(candles)),
        "digest": _series_digest(candles),
        "first": canonical_datetime(candles[0].start_at),
        "interval": interval.value,
        "last": canonical_datetime(candles[-1].start_at),
    }


def _instrument_payload(instrument: Instrument) -> dict[str, str]:
    return {
        "exchange": instrument.exchange,
        "instrument_token": str(instrument.instrument_token),
        "is_index": str(instrument.is_index).lower(),
        "lot_size": str(instrument.lot_size),
        "segment": instrument.segment,
        "tick_size": canonical_decimal(instrument.tick_size),
        "tradingsymbol": instrument.tradingsymbol,
    }


def _calendar_payload(calendar: MarketSessionCalendar) -> dict[str, str]:
    window = calendar.window
    return {
        "close_time": window.close_time.isoformat(),
        "holidays": ",".join(day.isoformat() for day in sorted(calendar.holidays)),
        "open_time": window.open_time.isoformat(),
        "post_close_end": window.post_close_end.isoformat(),
        "pre_open_start": window.pre_open_start.isoformat(),
        "weekend_days": ",".join(str(day) for day in sorted(calendar.weekend_days)),
        "window": window.name,
    }


@dataclass(frozen=True, slots=True)
class BacktestInput:
    """One instrument, one date range, one fully specified set of assumptions.

    A multi-instrument study is a *sequence* of these rather than one input
    holding many instruments. That keeps the engine simple, makes per-instrument
    results fall out naturally for the cross-instrument robustness check, and
    keeps any single input small enough to reason about whole.
    """

    instrument: Instrument
    candles_5m: tuple[Candle, ...]
    candles_1m: tuple[Candle, ...]
    calendar: MarketSessionCalendar
    strategy_params: OrbParams
    cost_schedule: CostSchedule
    slippage_config: SlippageConfig
    execution_config: ExecutionConfig

    def __post_init__(self) -> None:
        for name in ("candles_5m", "candles_1m"):
            if not isinstance(getattr(self, name), tuple):
                raise InvalidBacktestInputError(
                    f"{name} must be a tuple; a backtest input is immutable so that the run "
                    "it fingerprints cannot change underneath the result"
                )

        params = self.strategy_params
        self._validate_series(self.candles_5m, params.signal_interval, "candles_5m")
        self._validate_series(self.candles_1m, params.resolution_interval, "candles_1m")
        self._validate_coverage()

    # ------------------------------------------------------------------ #
    # validation
    # ------------------------------------------------------------------ #

    def _validate_series(
        self, candles: tuple[Candle, ...], interval: CandleInterval, label: str
    ) -> None:
        if not candles:
            raise InvalidBacktestInputError(f"{label} is empty; a backtest needs bars to run over")

        token = self.instrument.instrument_token
        previous: datetime | None = None

        for index, candle in enumerate(candles):
            if candle.status is not CandleStatus.COMPLETED:
                raise InvalidBacktestInputError(
                    f"{label}[{index}] at {candle.start_at.isoformat()} is "
                    f"{candle.status.value}; only completed bars may be backtested, because an "
                    "in-progress bar still changes and a strategy that saw it would be reading "
                    "a value that did not settle until later"
                )
            if candle.interval is not interval:
                raise InvalidBacktestInputError(
                    f"{label}[{index}] has interval {candle.interval.value}, expected "
                    f"{interval.value}"
                )
            if candle.instrument_token != token:
                raise InvalidBacktestInputError(
                    f"{label}[{index}] belongs to instrument {candle.instrument_token}, but "
                    f"this input is for {token}; one input holds exactly one instrument"
                )
            if candle.start_at.tzinfo is None or candle.end_at.tzinfo is None:
                raise InvalidBacktestInputError(
                    f"{label}[{index}] has a naive timestamp; every timestamp must be "
                    "timezone-aware UTC"
                )
            if int(candle.start_at.timestamp()) % interval.seconds != 0:
                raise InvalidBacktestInputError(
                    f"{label}[{index}] starts at {candle.start_at.isoformat()}, which is not "
                    f"aligned to a {interval.value} boundary; bar boundaries are epoch-aligned "
                    "so that they coincide exactly with IST clock boundaries"
                )
            if previous is not None:
                if candle.start_at == previous:
                    raise InvalidBacktestInputError(
                        f"{label} contains a duplicate bucket at {candle.start_at.isoformat()}"
                    )
                if candle.start_at < previous:
                    raise InvalidBacktestInputError(
                        f"{label} is not ascending: {candle.start_at.isoformat()} follows "
                        f"{previous.isoformat()}"
                    )
            previous = candle.start_at

    def _validate_coverage(self) -> None:
        """Every session that has signal bars must also have resolution bars.

        Without this, the execution simulator would silently fall back to its
        pessimistic same-bar assumption for a whole session and the result would
        look like a modelling choice rather than missing data.
        """
        signal_sessions = {to_ist(candle.start_at).date() for candle in self.candles_5m}
        resolution_sessions = {to_ist(candle.start_at).date() for candle in self.candles_1m}
        missing = sorted(signal_sessions - resolution_sessions)
        if missing:
            shown = ", ".join(day.isoformat() for day in missing[:5])
            more = f" (and {len(missing) - 5} more)" if len(missing) > 5 else ""
            raise InvalidBacktestInputError(
                f"{len(missing)} session(s) have {self.strategy_params.signal_interval.value} "
                f"bars but no {self.strategy_params.resolution_interval.value} bars: {shown}"
                f"{more}. Intrabar stop/target ordering could not be resolved for them"
            )

    # ------------------------------------------------------------------ #
    # identity
    # ------------------------------------------------------------------ #

    def canonical_payload(self) -> dict[str, object]:
        """The exact structure the fingerprint is taken over.

        Exposed so a failing reproducibility check can be diagnosed by diffing
        two payloads rather than by staring at two different hex digests.
        """
        return {
            "calendar": _calendar_payload(self.calendar),
            "candles_1m": _series_payload(
                self.candles_1m, self.strategy_params.resolution_interval
            ),
            "candles_5m": _series_payload(self.candles_5m, self.strategy_params.signal_interval),
            "cost_schedule": self.cost_schedule.canonical(),
            "execution_config": self.execution_config.canonical(),
            "instrument": _instrument_payload(self.instrument),
            "schema": FINGERPRINT_SCHEMA,
            "slippage_config": self.slippage_config.canonical(),
            "strategy_params": self.strategy_params.canonical(),
        }

    def fingerprint(self) -> str:
        """Deterministic SHA-256 identity of this input.

        Stable across processes and machines for identical logical inputs.
        Python's builtin ``hash()`` is never used: string hashing is salted per
        process unless ``PYTHONHASHSEED`` is pinned, so it cannot identify a run.

        Wall-clock time contributes nothing - two runs of the same data at
        different moments are the same run, and ``generated_at`` lives on the
        manifest instead.
        """
        canonical = json.dumps(
            self.canonical_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------ #

    @property
    def sessions(self) -> tuple[date, ...]:
        """The IST session dates covered by the signal series, in order.

        The engine iterates sessions rather than bars, because the strategy is
        handed one session's prefix at a time.
        """
        return tuple(sorted({to_ist(candle.start_at).date() for candle in self.candles_5m}))
