"""GVS v1 historical universe: provenance, §1.7 unresolved slots, §1.8 eligibility.

Every fixture is synthetic. The base scenario is 60 shortlisted ordinary equities
S01..S60, ranked in that order both by the candidate file and by 1-minute
turnover, each with an exact instrument-master match and full minute coverage.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal

import pytest

from app.domain.backtest.universe import (
    CANDIDATE_HEADER,
    CLASSIFICATION_HEADER,
    SYMBOL_CHANGE_HEADER,
    DocumentRole,
    HistoricalUniverse,
    MappingStatus,
    MinuteEvidence,
    Reason,
    SecurityClass,
    SecurityRecord,
    SecurityStatus,
    SelectionPeriod,
    SourceDocument,
    UniverseOutcome,
    UniverseValidationError,
    build_universe,
    parse_file_set,
)
from app.domain.market.models import Instrument

SELECTION = SelectionPeriod(date(2023, 3, 3), date(2023, 5, 31))
MASTER_DAY = date(2026, 9, 14)


def sym(i: int) -> str:
    return f"S{i:02d}"


def isin(i: int) -> str:
    return f"INE{i:06d}A0{i % 10}"


def instrument(symbol: str, token: int, **overrides: object) -> Instrument:
    base = Instrument(
        instrument_token=token,
        exchange_token=token,
        tradingsymbol=symbol,
        name=symbol,
        exchange="NSE",
        segment="NSE",
        instrument_type="EQ",
        tick_size=Decimal("0.05"),
        lot_size=1,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def csv_bytes(header: tuple[str, ...], rows: list[list[str]]) -> bytes:
    lines = [",".join(header), *(",".join(row) for row in rows)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def document(role: DocumentRole, content: bytes) -> SourceDocument:
    return SourceDocument(
        role=role,
        name=f"{role.value}-2023-05-31.csv",
        source="synthetic point-in-time exchange record (test fixture)",
        retrieved_on=date(2026, 9, 15),
        sha256=hashlib.sha256(content).hexdigest(),
    )


@dataclass
class Scenario:
    count: int = 60
    candidates: dict[str, list[str]] = field(default_factory=dict)
    classes: dict[str, list[str]] = field(default_factory=dict)
    changes: list[list[str]] = field(default_factory=list)
    master: dict[str, Instrument] = field(default_factory=dict)
    evidence: dict[str, MinuteEvidence] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for i in range(1, self.count + 1):
            s = sym(i)
            self.candidates[s] = [s, isin(i), f"Company {i}", str(100000 - 100 * i), "60"]
            self.classes[s] = [s, isin(i), "ORDINARY_EQUITY_FULLY_PAID", "Equity shares"]
            self.master[s] = instrument(s, 1000 + i)
            self.evidence[s] = MinuteEvidence(60, Decimal(1_000_000 - 1000 * i))

    def unmap(self, symbol: str) -> None:
        del self.master[symbol], self.evidence[symbol]

    def classify(self, symbol: str, code: str) -> None:
        self.classes[symbol][2] = code
        self.evidence.pop(symbol, None)

    def documents(self) -> dict[str, tuple[SourceDocument, bytes]]:
        contents = {
            DocumentRole.CANDIDATES: csv_bytes(CANDIDATE_HEADER, list(self.candidates.values())),
            DocumentRole.CLASSIFICATION: csv_bytes(
                CLASSIFICATION_HEADER, list(self.classes.values())
            ),
            DocumentRole.SYMBOL_CHANGES: csv_bytes(SYMBOL_CHANGE_HEADER, self.changes),
        }
        return {
            role.value: (document(role, content), content) for role, content in contents.items()
        }

    def build(self) -> HistoricalUniverse:
        file_set = parse_file_set(SELECTION, **self.documents())
        return build_universe(
            file_set,
            instruments=list(self.master.values()),
            master_retrieved_on=MASTER_DAY,
            minute_evidence=self.evidence,
        )


def record(universe: HistoricalUniverse, symbol: str) -> SecurityRecord:
    (found,) = [r for r in universe.records if r.symbol == symbol]
    return found


def statuses(universe: HistoricalUniverse, status: SecurityStatus) -> list[str]:
    return [r.symbol for r in universe.records if r.status is status]


def errors_of(scenario: Scenario) -> str:
    with pytest.raises(UniverseValidationError) as caught:
        scenario.build()
    return "; ".join(caught.value.errors)


# --------------------------------------------------------------------------- #


class TestValidUniverse:
    def test_the_top_fifty_by_turnover_fill_every_slot(self) -> None:
        universe = Scenario().build()
        assert universe.outcome is UniverseOutcome.VALID and universe.is_evaluable
        assert [r.symbol for r in universe.slots] == [sym(i) for i in range(1, 51)]
        assert statuses(universe, SecurityStatus.NOT_SELECTED) == [sym(i) for i in range(51, 61)]
        assert all(
            r.reasons == (Reason.BELOW_TURNOVER_CUTOFF,)
            for r in universe.records
            if r.status is SecurityStatus.NOT_SELECTED
        )
        assert universe.unresolved_count == 0
        assert universe.membership_statement is None
        assert universe.unresolved_value_share == 0

    def test_membership_is_decided_by_minute_turnover_not_by_the_file(self) -> None:
        scenario = Scenario()
        scenario.evidence["S60"] = MinuteEvidence(60, Decimal("5000000"))
        universe = scenario.build()
        assert universe.slots[0].symbol == "S60"
        assert record(universe, "S60").file_rank == 60
        assert record(universe, "S50").status is SecurityStatus.NOT_SELECTED

    def test_exact_mapping_records_the_ingestion_instrument(self) -> None:
        r = record(Scenario().build(), "S01")
        assert (r.mapping, r.instrument_key, r.instrument_token) == (
            MappingStatus.EXACT,
            "NSE:S01",
            1001,
        )
        assert (r.security_class, r.source_classification) == (
            SecurityClass.ORDINARY_EQUITY_FULLY_PAID,
            "Equity shares",
        )

    def test_a_security_that_traded_fewer_sessions_needs_only_those(self) -> None:
        scenario = Scenario()
        scenario.candidates["S02"][4] = "40"
        scenario.evidence["S02"] = MinuteEvidence(40, Decimal("998000"))
        assert record(scenario.build(), "S02").status is SecurityStatus.SELECTED


class TestProvenance:
    def test_documents_are_recorded_with_their_hashes(self) -> None:
        scenario = Scenario()
        docs = scenario.documents()
        universe = scenario.build()
        assert [d.sha256 for d in universe.file_set.documents] == [
            docs[r.value][0].sha256 for r in DocumentRole
        ]
        documents = universe.canonical_payload()["documents"]
        assert isinstance(documents, list)
        assert documents[0]["sha256"] == hashlib.sha256(docs["candidates"][1]).hexdigest()

    def test_a_modified_file_is_refused(self) -> None:
        docs = Scenario().documents()
        meta, content = docs["candidates"]
        docs["candidates"] = (meta, content.replace(b"99900", b"99901"))
        with pytest.raises(UniverseValidationError, match="candidates: sha256 mismatch"):
            parse_file_set(SELECTION, **docs)

    def test_a_document_in_the_wrong_role_is_refused(self) -> None:
        docs = Scenario().documents()
        docs["classification"] = docs["candidates"]
        with pytest.raises(UniverseValidationError, match="declares role candidates"):
            parse_file_set(SELECTION, **docs)

    @pytest.mark.parametrize("sha", ["", "ABC", "A" * 64, "g" * 64])
    def test_a_malformed_declared_hash_is_refused(self, sha: str) -> None:
        with pytest.raises(UniverseValidationError, match="sha256 must be"):
            SourceDocument(DocumentRole.CANDIDATES, "c.csv", "src", date(2026, 9, 15), sha)

    def test_name_and_source_are_required(self) -> None:
        with pytest.raises(UniverseValidationError, match="name and source are required"):
            SourceDocument(DocumentRole.CANDIDATES, "c.csv", " ", date(2026, 9, 15), "0" * 64)

    def test_the_selection_period_must_precede_the_evaluation(self) -> None:
        with pytest.raises(UniverseValidationError, match="must end before 2023-06-01"):
            SelectionPeriod(date(2023, 3, 3), date(2023, 6, 1))
        with pytest.raises(UniverseValidationError, match="first_session is after"):
            SelectionPeriod(date(2023, 5, 31), date(2023, 3, 3))


class TestStructure:
    def test_duplicate_symbols_and_isins_are_errors(self) -> None:
        scenario = Scenario()
        scenario.candidates["dup"] = ["S01", isin(99), "Other", "1", "60"]
        scenario.candidates["dup-isin"] = ["X99", isin(2), "Other", "1", "60"]
        scenario.classes["dup"] = ["S03", isin(98), "ETF", "ETF"]
        text = errors_of(scenario)
        assert "candidates: duplicate symbol S01" in text
        assert f"candidates: duplicate ISIN {isin(2)}" in text
        assert "classification: duplicate symbol S03" in text

    @pytest.mark.parametrize(
        ("column", "value", "message"),
        [
            (2, "", "missing required field company_name"),
            (0, "", "missing required field symbol"),
            (3, "", "missing required field median_traded_value_inr"),
            (3, "-5", "non-negative decimal"),
            (3, "1e5", "non-negative decimal"),
            (4, "61", "integer in 1..60"),
            (4, "0", "integer in 1..60"),
            (0, "s01", "invalid symbol"),
            (1, "BAD", "invalid ISIN"),
        ],
    )
    def test_invalid_candidate_fields(self, column: int, value: str, message: str) -> None:
        scenario = Scenario()
        scenario.candidates["S01"][column] = value
        assert message in errors_of(scenario)

    def test_unknown_classification_code_is_an_error_not_an_exclusion(self) -> None:
        scenario = Scenario()
        scenario.classes["S01"][2] = "EQUITY"
        assert "unknown security_class 'EQUITY'" in errors_of(scenario)

    def test_missing_source_classification_text_is_an_error(self) -> None:
        scenario = Scenario()
        scenario.classes["S01"][3] = ""
        assert "missing required field source_classification" in errors_of(scenario)

    def test_header_field_count_whitespace_and_encoding(self) -> None:
        docs = Scenario().documents()
        for content, message in [
            (b"symbol,isin\nS01,\n", "header must be exactly"),
            (csv_bytes(CANDIDATE_HEADER, [["S01", "", "C", "1"]]), "expected 5 fields, got 4"),
            (csv_bytes(CANDIDATE_HEADER, [["S01 ", "", "C", "1", "60"]]), "whitespace"),
            (b"\xff\xfe", "not UTF-8"),
            (csv_bytes(CANDIDATE_HEADER, []), "lists no securities"),
        ]:
            docs["candidates"] = (document(DocumentRole.CANDIDATES, content), content)
            with pytest.raises(UniverseValidationError, match=message):
                parse_file_set(SELECTION, **docs)

    def test_every_problem_is_reported_at_once(self) -> None:
        scenario = Scenario()
        scenario.candidates["S01"][2] = ""
        scenario.classes["S02"][2] = "NOPE"
        scenario.changes.append(["S03", "S03", "2024-01-01"])
        text = errors_of(scenario)
        assert "company_name" in text and "NOPE" in text and "old_symbol equals" in text

    @pytest.mark.parametrize(
        ("rows", "message"),
        [
            ([["S01", "N01", "2024-13-01"]], "effective_date must be YYYY-MM-DD"),
            ([["S01", "N01", "20240101"]], "effective_date must be YYYY-MM-DD"),
            ([["S01", "N01", "2024-01-01"], ["S01", "N02", "2024-01-01"]], "duplicate old_symbol"),
            ([["S01", "N01", "2024-01-01"], ["N01", "N02", "2025-01-01"]], "chained change"),
        ],
    )
    def test_symbol_change_record_structure(self, rows: list[list[str]], message: str) -> None:
        scenario = Scenario()
        scenario.changes.extend(rows)
        assert message in errors_of(scenario)


class TestEligibility:
    def test_an_etf_labelled_eq_by_the_broker_is_ineligible(self) -> None:
        scenario = Scenario()
        scenario.classify("S03", "ETF")
        universe = scenario.build()
        r = record(universe, "S03")
        assert universe.file_set.candidates[2].symbol == "S03"
        assert scenario.master["S03"].instrument_type == "EQ"
        assert (r.status, r.reasons, r.file_rank) == (
            SecurityStatus.INELIGIBLE,
            (Reason.NOT_ORDINARY_EQUITY,),
            None,
        )
        assert r.mapping is MappingStatus.NOT_ATTEMPTED
        assert record(universe, "S04").file_rank == 3
        assert "S51" in [s.symbol for s in universe.slots]

    @pytest.mark.parametrize(
        "code",
        [c.value for c in SecurityClass if c is not SecurityClass.ORDINARY_EQUITY_FULLY_PAID],
    )
    def test_every_non_ordinary_class_is_excluded(self, code: str) -> None:
        scenario = Scenario()
        scenario.classify("S01", code)
        assert record(scenario.build(), "S01").status is SecurityStatus.INELIGIBLE

    def test_the_classification_alone_decides_not_the_name_or_symbol(self) -> None:
        scenario = Scenario()
        scenario.candidates["S01"][2] = "Nifty 50 ETF Exchange Traded Fund"
        assert record(scenario.build(), "S01").status is SecurityStatus.SELECTED

    def test_missing_classification_is_ineligible_and_reported(self) -> None:
        scenario = Scenario()
        del scenario.classes["S04"], scenario.evidence["S04"]
        r = record(scenario.build(), "S04")
        assert (r.status, r.reasons, r.security_class) == (
            SecurityStatus.INELIGIBLE,
            (Reason.MISSING_CLASSIFICATION,),
            None,
        )

    def test_an_isin_identity_mismatch_is_ineligible_and_reported(self) -> None:
        scenario = Scenario()
        scenario.classes["S05"][1] = isin(77)
        del scenario.evidence["S05"]
        r = record(scenario.build(), "S05")
        assert (r.status, r.reasons) == (SecurityStatus.INELIGIBLE, (Reason.ISIN_MISMATCH,))

    def test_a_mismatch_and_a_wrong_class_are_both_reported(self) -> None:
        scenario = Scenario()
        scenario.classes["S05"][1] = isin(77)
        scenario.classify("S05", "REIT")
        assert record(scenario.build(), "S05").reasons == (
            Reason.ISIN_MISMATCH,
            Reason.NOT_ORDINARY_EQUITY,
        )

    def test_an_absent_isin_is_not_a_mismatch(self) -> None:
        scenario = Scenario()
        scenario.candidates["S05"][1] = ""
        assert record(scenario.build(), "S05").status is SecurityStatus.SELECTED

    @pytest.mark.parametrize(
        ("overrides", "reasons"),
        [
            ({"lot_size": 2}, (Reason.LOT_SIZE_NOT_ONE,)),
            ({"instrument_type": "FUT"}, (Reason.INSTRUMENT_TYPE_NOT_EQ,)),
            ({"segment": "INDICES"}, (Reason.SEGMENT_NOT_NSE, Reason.IS_INDEX)),
        ],
    )
    def test_the_unchanged_instrument_conditions_still_apply(
        self, overrides: dict[str, object], reasons: tuple[Reason, ...]
    ) -> None:
        scenario = Scenario()
        scenario.master["S06"] = instrument("S06", 1006, **overrides)
        del scenario.evidence["S06"]
        r = record(scenario.build(), "S06")
        assert (r.status, r.reasons, r.file_rank) == (SecurityStatus.INELIGIBLE, reasons, None)


class TestMapping:
    def test_a_documented_symbol_change_maps_to_the_new_symbol(self) -> None:
        scenario = Scenario()
        del scenario.master["S07"]
        scenario.master["NEW07"] = instrument("NEW07", 7007)
        scenario.changes.append(["S07", "NEW07", "2024-02-01"])
        r = record(scenario.build(), "S07")
        assert (r.mapping, r.instrument_key, r.status) == (
            MappingStatus.SYMBOL_CHANGE,
            "NSE:NEW07",
            SecurityStatus.SELECTED,
        )

    def test_an_undocumented_symbol_change_is_not_inferred(self) -> None:
        scenario = Scenario()
        scenario.unmap("S07")
        scenario.master["NEW07"] = instrument("NEW07", 7007, name="Company 7")
        r = record(scenario.build(), "S07")
        assert (r.mapping, r.reasons, r.instrument_key) == (
            MappingStatus.UNMAPPED,
            (Reason.NO_EXACT_MATCH,),
            None,
        )
        assert r.status is SecurityStatus.UNRESOLVED

    def test_a_documented_change_whose_target_is_missing_is_unmapped(self) -> None:
        scenario = Scenario()
        scenario.unmap("S07")
        scenario.changes.append(["S07", "NEW07", "2024-02-01"])
        assert record(scenario.build(), "S07").reasons == (Reason.SYMBOL_CHANGE_TARGET_MISSING,)

    def test_a_bse_listing_is_not_an_nse_match(self) -> None:
        scenario = Scenario()
        scenario.unmap("S07")
        scenario.master["BSE:S07"] = instrument("S07", 9007, exchange="BSE", segment="BSE")
        assert record(scenario.build(), "S07").mapping is MappingStatus.UNMAPPED

    def test_old_symbol_still_listed_with_a_documented_change_is_ambiguous(self) -> None:
        scenario = Scenario()
        scenario.master["NEW07"] = instrument("NEW07", 7007)
        scenario.changes.append(["S07", "NEW07", "2024-02-01"])
        del scenario.evidence["S07"]
        universe = scenario.build()
        r = record(universe, "S07")
        assert (r.status, r.mapping, r.reasons) == (
            SecurityStatus.INVALID,
            MappingStatus.AMBIGUOUS,
            (Reason.AMBIGUOUS_MAPPING,),
        )
        assert universe.outcome is UniverseOutcome.AMBIGUOUS_MAPPING
        assert universe.slots == () and not universe.is_evaluable
        assert statuses(universe, SecurityStatus.SELECTED) == []

    def test_two_securities_claiming_one_instrument_are_both_invalid(self) -> None:
        scenario = Scenario()
        del scenario.master["S08"], scenario.evidence["S08"], scenario.evidence["S09"]
        scenario.changes.append(["S08", "S09", "2024-02-01"])
        universe = scenario.build()
        assert statuses(universe, SecurityStatus.INVALID) == ["S08", "S09"]
        assert universe.outcome is UniverseOutcome.AMBIGUOUS_MAPPING

    def test_duplicate_master_entries_are_ambiguous(self) -> None:
        scenario = Scenario()
        del scenario.evidence["S09"]
        instruments = [*scenario.master.values(), instrument("S09", 9999)]
        file_set = parse_file_set(SELECTION, **scenario.documents())
        universe = build_universe(
            file_set,
            instruments=instruments,
            master_retrieved_on=MASTER_DAY,
            minute_evidence=scenario.evidence,
        )
        assert record(universe, "S09").status is SecurityStatus.INVALID


class TestUnobtainable:
    def test_an_unmapped_top_fifty_security_holds_an_unresolved_slot(self) -> None:
        scenario = Scenario()
        scenario.unmap("S05")
        universe = scenario.build()
        r = record(universe, "S05")
        assert (r.status, r.file_rank, r.reasons) == (
            SecurityStatus.UNRESOLVED,
            5,
            (Reason.NO_EXACT_MATCH,),
        )
        assert universe.outcome is UniverseOutcome.VALID
        assert [s.symbol for s in universe.slots][-1] == "S05"
        assert len(universe.selected) == 49 and len(universe.slots) == 50
        assert universe.membership_statement == (
            "Exact top-50 membership under §1.2 could not be fully established; "
            "1 slots are unresolved."
        )

    def test_no_lower_ranked_security_is_promoted_into_the_slot(self) -> None:
        scenario = Scenario()
        scenario.unmap("S05")
        scenario.unmap("S20")
        universe = scenario.build()
        assert [s.symbol for s in universe.selected] == [
            sym(i) for i in range(1, 51) if i not in (5, 20)
        ]
        for symbol in ("S51", "S52"):
            r = record(universe, symbol)
            assert (r.status, r.reasons) == (
                SecurityStatus.NOT_SELECTED,
                (Reason.BELOW_TURNOVER_CUTOFF,),
            )

    def test_short_minute_coverage_is_unobtainable(self) -> None:
        scenario = Scenario()
        scenario.evidence["S10"] = MinuteEvidence(59, None)
        r = record(scenario.build(), "S10")
        assert (r.status, r.reasons, r.sessions_with_bars, r.mapping) == (
            SecurityStatus.UNRESOLVED,
            (Reason.INSUFFICIENT_MINUTE_SESSIONS,),
            59,
            MappingStatus.EXACT,
        )

    def test_unobtainable_below_rank_fifty_is_reported_but_holds_no_slot(self) -> None:
        scenario = Scenario()
        scenario.unmap("S55")
        scenario.evidence["S56"] = MinuteEvidence(0, None)
        universe = scenario.build()
        assert record(universe, "S55").status is SecurityStatus.UNMAPPED
        assert record(universe, "S56").status is SecurityStatus.INSUFFICIENT_DATA
        assert universe.unresolved_count == 0 and len(universe.selected) == 50

    def test_presumed_membership_uses_rank_among_eligible_securities(self) -> None:
        scenario = Scenario()
        scenario.classify("S01", "ETF")
        scenario.unmap("S51")  # file rank 50 once the ETF is excluded
        universe = scenario.build()
        assert record(universe, "S51").file_rank == 50
        assert record(universe, "S51").status is SecurityStatus.UNRESOLVED

    def test_five_unresolved_slots_are_still_evaluable(self) -> None:
        scenario = Scenario()
        for i in (1, 2, 3, 4, 5):
            scenario.unmap(sym(i))
        universe = scenario.build()
        assert universe.outcome is UniverseOutcome.VALID
        assert (universe.unresolved_count, len(universe.selected), len(universe.slots)) == (
            5,
            45,
            50,
        )
        assert [s.symbol for s in universe.slots[45:]] == ["S01", "S02", "S03", "S04", "S05"]

    def test_six_unresolved_slots_invalidate_the_universe(self) -> None:
        scenario = Scenario()
        for i in (1, 2, 3, 4, 5, 6):
            scenario.unmap(sym(i))
        universe = scenario.build()
        assert universe.outcome is UniverseOutcome.UNIVERSE_UNOBTAINABLE
        assert universe.outcome.value == "INVALID: UNIVERSE UNOBTAINABLE"
        assert not universe.is_evaluable and universe.slots == () and universe.selected == ()
        assert universe.unresolved_count == 6
        assert all(
            r.reasons == (Reason.UNIVERSE_NOT_EVALUABLE,)
            for r in universe.records
            if r.status is SecurityStatus.NOT_SELECTED
        )

    def test_the_unresolved_value_share_is_reported(self) -> None:
        scenario = Scenario()
        scenario.unmap("S01")
        top = sum(100000 - 100 * i for i in range(1, 51))
        assert scenario.build().unresolved_value_share == Decimal(99900) / Decimal(top)

    def test_a_shortlist_too_short_to_fill_the_slots_is_invalid(self) -> None:
        universe = Scenario(count=49).build()
        assert universe.outcome is UniverseOutcome.SHORTLIST_EXHAUSTED
        assert universe.slots == ()


class TestEvidenceIsNeverSubstituted:
    def test_evidence_for_an_unmapped_security_is_refused(self) -> None:
        scenario = Scenario()
        del scenario.master["S05"]
        assert "not an eligible mapped security" in errors_of(scenario)

    def test_evidence_for_an_ineligible_security_is_refused(self) -> None:
        scenario = Scenario()
        scenario.classes["S05"][2] = "ETF"
        assert "not an eligible mapped security" in errors_of(scenario)

    def test_evidence_for_an_unknown_security_is_refused(self) -> None:
        scenario = Scenario()
        scenario.evidence["HDFC"] = MinuteEvidence(60, Decimal(1))
        assert "HDFC, which is not shortlisted" in errors_of(scenario)

    def test_missing_evidence_is_an_error_not_an_omission(self) -> None:
        scenario = Scenario()
        del scenario.evidence["S05"]
        assert "no minute evidence for eligible mapped security S05" in errors_of(scenario)

    @pytest.mark.parametrize(
        ("evidence", "message"),
        [
            (MinuteEvidence(59, Decimal(1)), "incomplete coverage cannot be ranked"),
            (MinuteEvidence(61, Decimal(1)), "sessions_with_bars must be in 0..60"),
            (MinuteEvidence(-1, None), "sessions_with_bars must be in 0..60"),
            (MinuteEvidence(60, None), "requires a non-negative Decimal median"),
            (MinuteEvidence(60, Decimal(-1)), "requires a non-negative Decimal median"),
        ],
    )
    def test_inconsistent_evidence_is_refused(self, evidence: MinuteEvidence, message: str) -> None:
        scenario = Scenario()
        scenario.evidence["S05"] = evidence
        assert message in errors_of(scenario)

    def test_a_turnover_tie_at_the_cutoff_is_an_error(self) -> None:
        scenario = Scenario()
        scenario.evidence["S51"] = scenario.evidence["S50"]
        assert "S50 and S51 tie at the rank-50 boundary" in errors_of(scenario)

    def test_a_file_value_tie_at_rank_fifty_is_an_error(self) -> None:
        scenario = Scenario()
        scenario.candidates["S51"][3] = scenario.candidates["S50"][3]
        assert "candidate file: S50 and S51 tie" in errors_of(scenario)

    def test_ties_away_from_the_cutoff_are_harmless(self) -> None:
        scenario = Scenario()
        scenario.candidates["S02"][3] = scenario.candidates["S01"][3]
        scenario.evidence["S02"] = scenario.evidence["S01"]
        assert Scenario().build().outcome is UniverseOutcome.VALID
        assert scenario.build().outcome is UniverseOutcome.VALID


class TestDeterminism:
    #: Pinned over the base scenario. A change here means the canonical rendering
    #: or the rules changed, and FINGERPRINT_SCHEMA must be bumped with it.
    BASE_FINGERPRINT = "8aea0fc713b0618b34924f84ccbd3d8b20f59d64ea7167ab61ff82a79dd385d1"

    def test_repeated_validation_is_identical(self) -> None:
        first, second = Scenario().build(), Scenario().build()
        assert first == second
        assert first.canonical_payload() == second.canonical_payload()
        assert first.fingerprint() == second.fingerprint()

    def test_the_base_fingerprint_is_pinned(self) -> None:
        assert Scenario().build().fingerprint() == self.BASE_FINGERPRINT

    def test_the_fingerprint_moves_with_provenance_and_evidence(self) -> None:
        base = Scenario().build()
        later_master = replace(base, master_retrieved_on=date(2026, 9, 15))
        assert later_master.fingerprint() != base.fingerprint()

        renamed = Scenario()
        docs = renamed.documents()
        meta, content = docs["candidates"]
        docs["candidates"] = (replace(meta, source="another publisher"), content)
        file_set = parse_file_set(SELECTION, **docs)
        assert replace(base, file_set=file_set).fingerprint() != base.fingerprint()

        moved = Scenario()
        moved.evidence["S60"] = MinuteEvidence(60, Decimal("939999"))
        assert moved.build().fingerprint() != base.fingerprint()

    def test_the_payload_is_json_ready_strings(self) -> None:
        payload = Scenario().build().canonical_payload()
        row = payload["records"][0]  # type: ignore[index]
        assert row["median_turnover_inr"] == "999000" and row["file_rank"] == "1"
        assert payload["slots"][:2] == ["S01", "S02"]  # type: ignore[index]
