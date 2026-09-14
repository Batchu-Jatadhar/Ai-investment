"""GVS v1 historical universe: candidate-file provenance and the 50-slot validator.

Implements the universe clauses of the frozen GVS v1 specification and nothing
else. No signal, gap, volume, price-floor, corporate-action or exit rule lives
here; those are session-level rules applied later to the instruments selected.

*   **§1.2** membership: the top 50 by median daily Σ(1-minute price × volume)
    over the 60 sessions before 2023-06-01. The medians are computed elsewhere
    from Zerodha bars and arrive here as :class:`MinuteEvidence`.
*   **§1.7** a shortlisted security whose data cannot be obtained is never
    replaced. If its candidate-file rank is 50 or better it holds an UNRESOLVED
    slot that no lower-ranked security is promoted into; more than five such
    slots make the universe INVALID: UNIVERSE UNOBTAINABLE.
*   **§1.8** eligibility is decided only by the authoritative point-in-time
    security classification. The broker's ``instrument_type``, the name, the
    symbol and the ISIN never decide it; the ISIN is an identity cross-check.

.. rubric:: The external dependency

The authoritative point-in-time exchange files (candidate trading record,
security classification, symbol changes) are **not** in this repository. This
module defines the normalised, hashed form they must be supplied in and refuses
anything that does not verify. Translating the exchange's own vocabulary into
:class:`SecurityClass` is the job of whoever prepares the file, and that mapping
must be recorded with the source.

Nothing here reads a file, a clock or the network: documents arrive as bytes
with their declared SHA-256, and every structural problem is collected and
raised as :class:`UniverseValidationError` rather than resolved silently.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from app.core.canonical import canonical_decimal
from app.domain.market.models import Instrument

__all__ = [
    "EVALUATION_START",
    "MAX_UNRESOLVED_SLOTS",
    "SELECTION_SESSIONS",
    "UNIVERSE_SIZE",
    "CandidateFileSet",
    "CandidateRow",
    "ClassificationRow",
    "DocumentRole",
    "HistoricalUniverse",
    "MappingStatus",
    "MinuteEvidence",
    "Reason",
    "SecurityClass",
    "SecurityRecord",
    "SecurityStatus",
    "SelectionPeriod",
    "SourceDocument",
    "SymbolChange",
    "UniverseOutcome",
    "UniverseValidationError",
    "build_universe",
    "parse_file_set",
]

UNIVERSE_SIZE = 50
SELECTION_SESSIONS = 60
MAX_UNRESOLVED_SLOTS = 5
EVALUATION_START = date(2023, 6, 1)

#: Bumped only when the canonical rendering changes shape.
FINGERPRINT_SCHEMA = "aitrade.universe.gvs_v1.v1"

CANDIDATE_HEADER = ("symbol", "isin", "company_name", "median_traded_value_inr", "sessions_traded")
CLASSIFICATION_HEADER = ("symbol", "isin", "security_class", "source_classification")
SYMBOL_CHANGE_HEADER = ("old_symbol", "new_symbol", "effective_date")

_SHA256 = re.compile(r"[0-9a-f]{64}")
_SYMBOL = re.compile(r"[A-Z0-9&_-]+")
_ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")
_AMOUNT = re.compile(r"[0-9]+(\.[0-9]+)?")
_COUNT = re.compile(r"[0-9]+")
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class UniverseValidationError(ValueError):
    """The inputs cannot produce a universe. Carries every problem found."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


class DocumentRole(StrEnum):
    CANDIDATES = "candidates"
    CLASSIFICATION = "classification"
    SYMBOL_CHANGES = "symbol_changes"


class SecurityClass(StrEnum):
    """Normalised authoritative classification. Only the first is eligible (§1.8)."""

    ORDINARY_EQUITY_FULLY_PAID = "ORDINARY_EQUITY_FULLY_PAID"
    ETF = "ETF"
    MUTUAL_FUND_UNIT = "MUTUAL_FUND_UNIT"
    REIT = "REIT"
    INVIT = "INVIT"
    PREFERENCE_SHARE = "PREFERENCE_SHARE"
    PARTLY_PAID_SHARE = "PARTLY_PAID_SHARE"
    WARRANT = "WARRANT"
    RIGHTS_ENTITLEMENT = "RIGHTS_ENTITLEMENT"
    DEPOSITORY_RECEIPT = "DEPOSITORY_RECEIPT"
    OTHER = "OTHER"


class MappingStatus(StrEnum):
    EXACT = "EXACT"
    SYMBOL_CHANGE = "SYMBOL_CHANGE"
    UNMAPPED = "UNMAPPED"
    AMBIGUOUS = "AMBIGUOUS"
    #: Not attempted: the security was already ineligible by classification.
    NOT_ATTEMPTED = "NOT_ATTEMPTED"


class SecurityStatus(StrEnum):
    SELECTED = "SELECTED"
    UNRESOLVED = "UNRESOLVED"
    NOT_SELECTED = "NOT_SELECTED"
    INELIGIBLE = "INELIGIBLE"
    #: Unobtainable under §1.7(a), candidate-file rank below 50.
    UNMAPPED = "UNMAPPED"
    #: Unobtainable under §1.7(b), candidate-file rank below 50.
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    INVALID = "INVALID"


class Reason(StrEnum):
    MISSING_CLASSIFICATION = "MISSING_CLASSIFICATION"
    ISIN_MISMATCH = "ISIN_MISMATCH"
    NOT_ORDINARY_EQUITY = "NOT_ORDINARY_EQUITY"
    SEGMENT_NOT_NSE = "SEGMENT_NOT_NSE"
    INSTRUMENT_TYPE_NOT_EQ = "INSTRUMENT_TYPE_NOT_EQ"
    LOT_SIZE_NOT_ONE = "LOT_SIZE_NOT_ONE"
    IS_INDEX = "IS_INDEX"
    NO_EXACT_MATCH = "NO_EXACT_MATCH"
    SYMBOL_CHANGE_TARGET_MISSING = "SYMBOL_CHANGE_TARGET_MISSING"
    AMBIGUOUS_MAPPING = "AMBIGUOUS_MAPPING"
    INSUFFICIENT_MINUTE_SESSIONS = "INSUFFICIENT_MINUTE_SESSIONS"
    BELOW_TURNOVER_CUTOFF = "BELOW_TURNOVER_CUTOFF"
    UNIVERSE_NOT_EVALUABLE = "UNIVERSE_NOT_EVALUABLE"


class UniverseOutcome(StrEnum):
    VALID = "VALID"
    UNIVERSE_UNOBTAINABLE = "INVALID: UNIVERSE UNOBTAINABLE"
    AMBIGUOUS_MAPPING = "INVALID: AMBIGUOUS MAPPING"
    SHORTLIST_EXHAUSTED = "INVALID: SHORTLIST EXHAUSTED"


# --------------------------------------------------------------------------- #
# provenance
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SelectionPeriod:
    """The 60 selection sessions, all strictly before the evaluation start."""

    first_session: date
    last_session: date

    def __post_init__(self) -> None:
        if self.first_session > self.last_session:
            raise UniverseValidationError(["selection period first_session is after last_session"])
        if self.last_session >= EVALUATION_START:
            raise UniverseValidationError(
                [f"selection period must end before {EVALUATION_START.isoformat()}"]
            )


@dataclass(frozen=True, slots=True)
class SourceDocument:
    """Declared identity of one hashed document in the candidate file set."""

    role: DocumentRole
    name: str
    #: Publisher and reference of the authoritative original.
    source: str
    retrieved_on: date
    sha256: str

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.source.strip():
            raise UniverseValidationError([f"{self.role.value}: name and source are required"])
        if not _SHA256.fullmatch(self.sha256):
            raise UniverseValidationError(
                [f"{self.role.value}: sha256 must be 64 lowercase hex characters"]
            )

    def canonical(self) -> dict[str, str]:
        return {
            "name": self.name,
            "retrieved_on": self.retrieved_on.isoformat(),
            "role": self.role.value,
            "sha256": self.sha256,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class CandidateRow:
    symbol: str
    isin: str | None
    company_name: str
    median_traded_value_inr: Decimal
    sessions_traded: int


@dataclass(frozen=True, slots=True)
class ClassificationRow:
    symbol: str
    isin: str | None
    security_class: SecurityClass
    #: The authority's own classification text, verbatim.
    source_classification: str


@dataclass(frozen=True, slots=True)
class SymbolChange:
    old_symbol: str
    new_symbol: str
    effective_date: date


@dataclass(frozen=True, slots=True)
class CandidateFileSet:
    """The verified, parsed candidate file set. Only :func:`parse_file_set` builds one."""

    selection: SelectionPeriod
    documents: tuple[SourceDocument, ...]
    candidates: tuple[CandidateRow, ...]
    classifications: tuple[ClassificationRow, ...]
    symbol_changes: tuple[SymbolChange, ...]


def _read_rows(
    document: SourceDocument,
    role: DocumentRole,
    content: bytes,
    header: tuple[str, ...],
    errors: list[str],
) -> list[tuple[int, dict[str, str]]]:
    label = role.value
    if document.role is not role:
        errors.append(f"{label}: document declares role {document.role.value}")
        return []
    actual = hashlib.sha256(content).hexdigest()
    if actual != document.sha256:
        errors.append(f"{label}: sha256 mismatch (declared {document.sha256}, content {actual})")
        return []
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        errors.append(f"{label}: content is not UTF-8")
        return []
    rows = list(csv.reader(io.StringIO(text, newline="")))
    if not rows or tuple(rows[0]) != header:
        errors.append(f"{label}: header must be exactly {','.join(header)}")
        return []
    parsed: list[tuple[int, dict[str, str]]] = []
    for line, row in enumerate(rows[1:], start=2):
        if len(row) != len(header):
            errors.append(f"{label} line {line}: expected {len(header)} fields, got {len(row)}")
            continue
        if any(value != value.strip() for value in row):
            errors.append(f"{label} line {line}: surrounding whitespace is not allowed")
            continue
        parsed.append((line, dict(zip(header, row, strict=True))))
    return parsed


def _required(row: dict[str, str], name: str, where: str, errors: list[str]) -> str | None:
    value = row[name]
    if not value:
        errors.append(f"{where}: missing required field {name}")
        return None
    return value


def _symbol(value: str | None, where: str, errors: list[str]) -> str | None:
    if value is not None and not _SYMBOL.fullmatch(value):
        errors.append(f"{where}: invalid symbol {value!r}")
        return None
    return value


def _isin(value: str, where: str, errors: list[str]) -> str | None:
    if value and not _ISIN.fullmatch(value):
        errors.append(f"{where}: invalid ISIN {value!r}")
    return value or None


def _duplicates(values: Sequence[str | None], what: str, label: str, errors: list[str]) -> None:
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        if value in seen:
            errors.append(f"{label}: duplicate {what} {value}")
        seen.add(value)


def _parse_candidates(
    document: SourceDocument, content: bytes, errors: list[str]
) -> list[CandidateRow]:
    out: list[CandidateRow] = []
    for line, row in _read_rows(
        document, DocumentRole.CANDIDATES, content, CANDIDATE_HEADER, errors
    ):
        where = f"candidates line {line}"
        before = len(errors)
        symbol = _symbol(_required(row, "symbol", where, errors), where, errors)
        name = _required(row, "company_name", where, errors)
        value = _required(row, "median_traded_value_inr", where, errors)
        sessions = _required(row, "sessions_traded", where, errors)
        isin = _isin(row["isin"], where, errors)
        if value is not None and not _AMOUNT.fullmatch(value):
            errors.append(f"{where}: median_traded_value_inr must be a non-negative decimal")
        if sessions is not None and (
            not _COUNT.fullmatch(sessions) or not 1 <= int(sessions) <= SELECTION_SESSIONS
        ):
            errors.append(f"{where}: sessions_traded must be an integer in 1..{SELECTION_SESSIONS}")
        if len(errors) > before:
            continue
        assert symbol and name and value and sessions
        out.append(CandidateRow(symbol, isin, name, Decimal(value), int(sessions)))
    _duplicates([r.symbol for r in out], "symbol", "candidates", errors)
    _duplicates([r.isin for r in out], "ISIN", "candidates", errors)
    return out


def _parse_classifications(
    document: SourceDocument, content: bytes, errors: list[str]
) -> list[ClassificationRow]:
    out: list[ClassificationRow] = []
    rows = _read_rows(document, DocumentRole.CLASSIFICATION, content, CLASSIFICATION_HEADER, errors)
    for line, row in rows:
        where = f"classification line {line}"
        before = len(errors)
        symbol = _symbol(_required(row, "symbol", where, errors), where, errors)
        code = _required(row, "security_class", where, errors)
        original = _required(row, "source_classification", where, errors)
        isin = _isin(row["isin"], where, errors)
        if code is not None and code not in SecurityClass.__members__:
            errors.append(f"{where}: unknown security_class {code!r}")
        if len(errors) > before:
            continue
        assert symbol and code and original
        out.append(ClassificationRow(symbol, isin, SecurityClass(code), original))
    _duplicates([r.symbol for r in out], "symbol", "classification", errors)
    _duplicates([r.isin for r in out], "ISIN", "classification", errors)
    return out


def _parse_symbol_changes(
    document: SourceDocument, content: bytes, errors: list[str]
) -> list[SymbolChange]:
    out: list[SymbolChange] = []
    rows = _read_rows(document, DocumentRole.SYMBOL_CHANGES, content, SYMBOL_CHANGE_HEADER, errors)
    for line, row in rows:
        where = f"symbol_changes line {line}"
        before = len(errors)
        old = _symbol(_required(row, "old_symbol", where, errors), where, errors)
        new = _symbol(_required(row, "new_symbol", where, errors), where, errors)
        effective = _required(row, "effective_date", where, errors)
        parsed_date: date | None = None
        if effective is not None:
            try:
                if not _ISO_DATE.fullmatch(effective):
                    raise ValueError
                parsed_date = date.fromisoformat(effective)
            except ValueError:
                errors.append(f"{where}: effective_date must be YYYY-MM-DD")
        if old is not None and old == new:
            errors.append(f"{where}: old_symbol equals new_symbol")
        if len(errors) > before:
            continue
        assert old and new and parsed_date
        out.append(SymbolChange(old, new, parsed_date))
    _duplicates([c.old_symbol for c in out], "old_symbol", "symbol_changes", errors)
    olds = {c.old_symbol for c in out}
    for change in out:
        if change.new_symbol in olds:
            errors.append(
                f"symbol_changes: chained change through {change.new_symbol}; "
                "record each security's change as a single row"
            )
    return out


def parse_file_set(
    selection: SelectionPeriod,
    *,
    candidates: tuple[SourceDocument, bytes],
    classification: tuple[SourceDocument, bytes],
    symbol_changes: tuple[SourceDocument, bytes],
) -> CandidateFileSet:
    """Verify every document against its declared SHA-256, then parse it strictly."""
    errors: list[str] = []
    rows = _parse_candidates(*candidates, errors)
    classes = _parse_classifications(*classification, errors)
    changes = _parse_symbol_changes(*symbol_changes, errors)
    if not rows and not errors:
        errors.append("candidates: the file lists no securities")
    if errors:
        raise UniverseValidationError(errors)
    return CandidateFileSet(
        selection=selection,
        documents=(candidates[0], classification[0], symbol_changes[0]),
        candidates=tuple(rows),
        classifications=tuple(classes),
        symbol_changes=tuple(changes),
    )


# --------------------------------------------------------------------------- #
# universe
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class MinuteEvidence:
    """One ingestion attempt's result for a mapped security over the selection sessions."""

    #: Of the sessions the candidate file records as traded, how many have 1m bars.
    sessions_with_bars: int
    #: Median daily Σ(1-minute price × volume); ``None`` when coverage is short.
    median_turnover_inr: Decimal | None


@dataclass(frozen=True, slots=True)
class SecurityRecord:
    """One shortlisted security's audit row."""

    symbol: str
    company_name: str
    isin: str | None
    security_class: SecurityClass | None
    source_classification: str | None
    mapping: MappingStatus
    instrument_key: str | None
    instrument_token: int | None
    file_median_traded_value_inr: Decimal
    sessions_traded: int
    file_rank: int | None
    sessions_with_bars: int | None
    median_turnover_inr: Decimal | None
    turnover_rank: int | None
    status: SecurityStatus
    reasons: tuple[Reason, ...]

    def canonical(self) -> dict[str, object]:
        def text(value: object) -> str:
            if value is None:
                return ""
            return canonical_decimal(value) if isinstance(value, Decimal) else str(value)

        return {
            "company_name": self.company_name,
            "file_median_traded_value_inr": text(self.file_median_traded_value_inr),
            "file_rank": text(self.file_rank),
            "instrument_key": text(self.instrument_key),
            "instrument_token": text(self.instrument_token),
            "isin": text(self.isin),
            "mapping": self.mapping.value,
            "median_turnover_inr": text(self.median_turnover_inr),
            "reasons": [r.value for r in self.reasons],
            "security_class": text(self.security_class and self.security_class.value),
            "sessions_traded": str(self.sessions_traded),
            "sessions_with_bars": text(self.sessions_with_bars),
            "source_classification": text(self.source_classification),
            "status": self.status.value,
            "symbol": self.symbol,
            "turnover_rank": text(self.turnover_rank),
        }


@dataclass(frozen=True, slots=True)
class HistoricalUniverse:
    file_set: CandidateFileSet
    master_retrieved_on: date
    outcome: UniverseOutcome
    #: Every shortlisted security, in candidate-file order.
    records: tuple[SecurityRecord, ...]
    #: The 50 slots when VALID - selected by turnover rank, then unresolved by
    #: file rank - and empty otherwise.
    slots: tuple[SecurityRecord, ...]

    @property
    def is_evaluable(self) -> bool:
        return self.outcome is UniverseOutcome.VALID

    @property
    def selected(self) -> tuple[SecurityRecord, ...]:
        return tuple(r for r in self.slots if r.status is SecurityStatus.SELECTED)

    @property
    def unresolved(self) -> tuple[SecurityRecord, ...]:
        """Unresolved presumed members, by candidate-file rank."""
        found = [r for r in self.records if r.status is SecurityStatus.UNRESOLVED]
        return tuple(sorted(found, key=lambda r: r.file_rank or 0))

    @property
    def unresolved_count(self) -> int:
        return len(self.unresolved)

    @property
    def unresolved_value_share(self) -> Decimal | None:
        """Unresolved slots' share of the candidate-file top 50's traded value."""
        top = [r for r in self.records if r.file_rank is not None and r.file_rank <= UNIVERSE_SIZE]
        total = sum((r.file_median_traded_value_inr for r in top), Decimal(0))
        if not total:
            return None
        return sum((r.file_median_traded_value_inr for r in self.unresolved), Decimal(0)) / total

    @property
    def membership_statement(self) -> str | None:
        if not self.unresolved_count:
            return None
        return (
            "Exact top-50 membership under §1.2 could not be fully established; "
            f"{self.unresolved_count} slots are unresolved."
        )

    def canonical_payload(self) -> dict[str, object]:
        share = self.unresolved_value_share
        return {
            "documents": [d.canonical() for d in self.file_set.documents],
            "master_retrieved_on": self.master_retrieved_on.isoformat(),
            "outcome": self.outcome.value,
            "records": [r.canonical() for r in self.records],
            "rules": {
                "evaluation_start": EVALUATION_START.isoformat(),
                "max_unresolved_slots": str(MAX_UNRESOLVED_SLOTS),
                "selection_sessions": str(SELECTION_SESSIONS),
                "universe_size": str(UNIVERSE_SIZE),
            },
            "schema": FINGERPRINT_SCHEMA,
            "selection": {
                "first_session": self.file_set.selection.first_session.isoformat(),
                "last_session": self.file_set.selection.last_session.isoformat(),
            },
            "slots": [r.symbol for r in self.slots],
            "unresolved_count": str(self.unresolved_count),
            "unresolved_value_share": "" if share is None else canonical_decimal(share),
        }

    def fingerprint(self) -> str:
        canonical = json.dumps(
            self.canonical_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _map(
    symbol: str,
    by_symbol: Mapping[str, list[Instrument]],
    changes: Mapping[str, SymbolChange],
) -> tuple[MappingStatus, Instrument | None, tuple[Reason, ...]]:
    """Exact NSE symbol match, or a documented change. Never inferred."""
    exact = by_symbol.get(symbol, [])
    change = changes.get(symbol)
    if change is None:
        if len(exact) == 1:
            return MappingStatus.EXACT, exact[0], ()
        if not exact:
            return MappingStatus.UNMAPPED, None, (Reason.NO_EXACT_MATCH,)
        return MappingStatus.AMBIGUOUS, None, (Reason.AMBIGUOUS_MAPPING,)
    if exact:
        # The old symbol still resolves, yet a change away from it is documented:
        # the symbol may have been reissued, so neither reading is safe.
        return MappingStatus.AMBIGUOUS, None, (Reason.AMBIGUOUS_MAPPING,)
    target = by_symbol.get(change.new_symbol, [])
    if len(target) == 1:
        return MappingStatus.SYMBOL_CHANGE, target[0], ()
    if not target:
        return MappingStatus.UNMAPPED, None, (Reason.SYMBOL_CHANGE_TARGET_MISSING,)
    return MappingStatus.AMBIGUOUS, None, (Reason.AMBIGUOUS_MAPPING,)


def _instrument_reasons(instrument: Instrument) -> tuple[Reason, ...]:
    """The unchanged §1.1 instrument conditions."""
    reasons: list[Reason] = []
    if instrument.segment != "NSE":
        reasons.append(Reason.SEGMENT_NOT_NSE)
    if instrument.instrument_type != "EQ":
        reasons.append(Reason.INSTRUMENT_TYPE_NOT_EQ)
    if instrument.lot_size != 1:
        reasons.append(Reason.LOT_SIZE_NOT_ONE)
    if instrument.is_index:
        reasons.append(Reason.IS_INDEX)
    return tuple(reasons)


@dataclass(slots=True)
class _Draft:
    """Mutable working state for one shortlisted security while the rules apply."""

    row: CandidateRow
    cls: ClassificationRow | None
    reasons: tuple[Reason, ...]
    mapping: MappingStatus = MappingStatus.NOT_ATTEMPTED
    instrument: Instrument | None = None
    status: SecurityStatus | None = None
    evidence: MinuteEvidence | None = None

    def record(self, file_rank: int | None, turnover_rank: int | None) -> SecurityRecord:
        assert self.status is not None
        return SecurityRecord(
            symbol=self.row.symbol,
            company_name=self.row.company_name,
            isin=self.row.isin,
            security_class=self.cls.security_class if self.cls else None,
            source_classification=self.cls.source_classification if self.cls else None,
            mapping=self.mapping,
            instrument_key=self.instrument.key if self.instrument else None,
            instrument_token=self.instrument.instrument_token if self.instrument else None,
            file_median_traded_value_inr=self.row.median_traded_value_inr,
            sessions_traded=self.row.sessions_traded,
            file_rank=file_rank,
            sessions_with_bars=self.evidence.sessions_with_bars if self.evidence else None,
            median_turnover_inr=self.evidence.median_turnover_inr if self.evidence else None,
            turnover_rank=turnover_rank,
            status=self.status,
            reasons=self.reasons,
        )


def _ranked(values: Mapping[str, Decimal], cutoff: int, what: str) -> list[str]:
    """Symbols by descending value. An exact tie across the cutoff is an error, not a coin toss."""
    order = sorted(values, key=lambda symbol: (-values[symbol], symbol))
    if 0 < cutoff < len(order) and values[order[cutoff - 1]] == values[order[cutoff]]:
        raise UniverseValidationError(
            [
                f"{what}: {order[cutoff - 1]} and {order[cutoff]} tie at the rank-{cutoff} "
                "boundary; membership cannot be decided deterministically"
            ]
        )
    return order


def build_universe(
    file_set: CandidateFileSet,
    *,
    instruments: Sequence[Instrument],
    master_retrieved_on: date,
    minute_evidence: Mapping[str, MinuteEvidence],
) -> HistoricalUniverse:
    """Apply §1.2, §1.7 and §1.8 to a verified candidate file set.

    ``instruments`` is the instrument master used for ingestion. ``minute_evidence``
    is keyed by point-in-time symbol and must cover exactly the securities that are
    eligible and mapped: evidence for any other security would be substituted data,
    and a missing entry would be a silent omission.
    """
    by_symbol: dict[str, list[Instrument]] = {}
    for instrument in instruments:
        if instrument.exchange == "NSE":
            by_symbol.setdefault(instrument.tradingsymbol, []).append(instrument)
    classes = {c.symbol: c for c in file_set.classifications}
    changes = {c.old_symbol: c for c in file_set.symbol_changes}

    drafts: dict[str, _Draft] = {}
    for row in file_set.candidates:
        draft = _Draft(row=row, cls=classes.get(row.symbol), reasons=())
        drafts[row.symbol] = draft
        if draft.cls is None:
            draft.reasons = (Reason.MISSING_CLASSIFICATION,)
        else:
            reasons: list[Reason] = []
            if row.isin and draft.cls.isin and row.isin != draft.cls.isin:
                reasons.append(Reason.ISIN_MISMATCH)
            if draft.cls.security_class is not SecurityClass.ORDINARY_EQUITY_FULLY_PAID:
                reasons.append(Reason.NOT_ORDINARY_EQUITY)
            draft.reasons = tuple(reasons)
        if draft.reasons:
            draft.status = SecurityStatus.INELIGIBLE
            continue
        draft.mapping, draft.instrument, draft.reasons = _map(row.symbol, by_symbol, changes)
        if draft.mapping is MappingStatus.AMBIGUOUS:
            draft.status = SecurityStatus.INVALID
        elif draft.instrument is not None and (failed := _instrument_reasons(draft.instrument)):
            draft.status, draft.reasons = SecurityStatus.INELIGIBLE, failed

    # Two eligible securities resolving to one instrument cannot both be it.
    claimed: dict[str, list[_Draft]] = {}
    for draft in drafts.values():
        if draft.instrument is not None and draft.status is None:
            claimed.setdefault(draft.instrument.key, []).append(draft)
    for claimants in claimed.values():
        if len(claimants) > 1:
            for draft in claimants:
                draft.mapping, draft.instrument = MappingStatus.AMBIGUOUS, None
                draft.status, draft.reasons = SecurityStatus.INVALID, (Reason.AMBIGUOUS_MAPPING,)

    errors = [
        f"minute evidence for {symbol}, which is not shortlisted"
        for symbol in sorted(set(minute_evidence) - set(drafts))
    ]
    obtainable: dict[str, Decimal] = {}
    for symbol, draft in drafts.items():
        evidence = minute_evidence.get(symbol)
        rankable = draft.status is None and draft.mapping in (
            MappingStatus.EXACT,
            MappingStatus.SYMBOL_CHANGE,
        )
        if not rankable:
            if evidence is not None:
                errors.append(
                    f"minute evidence for {symbol}, which is not an eligible mapped security; "
                    "no substituted data may enter the ranking"
                )
            continue
        if evidence is None:
            errors.append(f"no minute evidence for eligible mapped security {symbol}")
            continue
        draft.evidence = evidence
        median = evidence.median_turnover_inr
        if not 0 <= evidence.sessions_with_bars <= draft.row.sessions_traded:
            errors.append(f"{symbol}: sessions_with_bars must be in 0..{draft.row.sessions_traded}")
        elif evidence.sessions_with_bars < draft.row.sessions_traded:
            draft.reasons = (Reason.INSUFFICIENT_MINUTE_SESSIONS,)
            if median is not None:
                errors.append(
                    f"{symbol}: a turnover median from incomplete coverage cannot be ranked"
                )
        elif not isinstance(median, Decimal) or median < 0:
            errors.append(f"{symbol}: complete coverage requires a non-negative Decimal median")
        else:
            obtainable[symbol] = median
    if errors:
        raise UniverseValidationError(errors)

    eligible = {
        s: d.row.median_traded_value_inr
        for s, d in drafts.items()
        if d.status is not SecurityStatus.INELIGIBLE
    }
    file_order = _ranked(eligible, UNIVERSE_SIZE, "candidate file")
    file_rank = {symbol: rank for rank, symbol in enumerate(file_order, start=1)}
    presumed = [
        s for s in file_order[:UNIVERSE_SIZE] if drafts[s].status is None and s not in obtainable
    ]
    k = len(presumed)

    if any(d.status is SecurityStatus.INVALID for d in drafts.values()):
        outcome = UniverseOutcome.AMBIGUOUS_MAPPING
    elif k > MAX_UNRESOLVED_SLOTS:
        outcome = UniverseOutcome.UNIVERSE_UNOBTAINABLE
    elif len(obtainable) < UNIVERSE_SIZE - k:
        outcome = UniverseOutcome.SHORTLIST_EXHAUSTED
    else:
        outcome = UniverseOutcome.VALID

    turnover_order = (
        _ranked(obtainable, UNIVERSE_SIZE - k, "1-minute turnover")
        if outcome is UniverseOutcome.VALID
        else []
    )
    turnover_rank = {symbol: rank for rank, symbol in enumerate(turnover_order, start=1)}

    for symbol, draft in drafts.items():
        if draft.status is not None:
            continue
        if symbol in presumed:
            draft.status = SecurityStatus.UNRESOLVED
        elif symbol not in obtainable:
            unmapped = draft.mapping is MappingStatus.UNMAPPED
            draft.status = SecurityStatus.UNMAPPED if unmapped else SecurityStatus.INSUFFICIENT_DATA
        elif outcome is not UniverseOutcome.VALID:
            draft.status = SecurityStatus.NOT_SELECTED
            draft.reasons = (Reason.UNIVERSE_NOT_EVALUABLE,)
        elif turnover_rank[symbol] <= UNIVERSE_SIZE - k:
            draft.status = SecurityStatus.SELECTED
        else:
            draft.status = SecurityStatus.NOT_SELECTED
            draft.reasons = (Reason.BELOW_TURNOVER_CUTOFF,)

    records = tuple(d.record(file_rank.get(s), turnover_rank.get(s)) for s, d in drafts.items())
    slots: tuple[SecurityRecord, ...] = ()
    if outcome is UniverseOutcome.VALID:
        by_record = {r.symbol: r for r in records}
        slots = (
            *(by_record[s] for s in turnover_order[: UNIVERSE_SIZE - k]),
            *(by_record[s] for s in presumed),
        )
        assert len(slots) == UNIVERSE_SIZE

    return HistoricalUniverse(
        file_set=file_set,
        master_retrieved_on=master_retrieved_on,
        outcome=outcome,
        records=records,
        slots=slots,
    )
