"""The AI analyst: a strict three-word filter that can subtract but never create or size a trade."""

from __future__ import annotations

import dataclasses
import json
import pathlib
from decimal import Decimal

import pytest

from app.adapters.ai.fake import FakeAnalyst
from app.domain.ai.analyst import (
    FORBIDDEN_FIELDS,
    AnalystContext,
    AnalystDecision,
    AnalystVerdict,
    MalformedAnalystResponseError,
    parse_analyst_response,
)
from app.domain.backtest.models import SignalRecord
from app.domain.risk.sizing import LimitingConstraint, RejectionCode, RiskDecision, RiskRejection
from app.domain.strategy.contract import Signal, SignalDirection
from app.services.ai_filter import EntryDecision, ai_signal_gate, decide_entry
from tests.backtest.conftest import SESSION_OPEN, make_candle
from tests.backtest.test_golden_backtest import run_golden

BARS = (make_candle(SESSION_OPEN),)
SIGNAL = Signal(
    instrument_token=BARS[0].instrument_token,
    direction=SignalDirection.LONG,
    stop_price=Decimal("1395.00"),
    target_r_multiple=Decimal("2"),
    signal_bar_start=SESSION_OPEN,
    reason="opening range breakout",
)
APPROVED = RiskDecision(quantity=10, limited_by=LimitingConstraint.RISK_BUDGET)
REJECTED = RiskDecision(
    quantity=0, rejections=(RiskRejection(RejectionCode.INSUFFICIENT_CAPITAL, "no cash"),)
)


def response(**fields: object) -> dict[str, object]:
    base: dict[str, object] = {
        "decision": "TAKE_TRADE",
        "reason": "trend and volume agree",
        "model_id": "fake-local",
        "prompt_version": "fake-v1",
    }
    base.update(fields)
    return {k: v for k, v in base.items() if v is not ...}


def decide(analyst: FakeAnalyst | None, risk: RiskDecision = APPROVED) -> EntryDecision:
    return decide_entry(SIGNAL, risk, session_bars=BARS, analyst=analyst)


def answering(raw: object) -> FakeAnalyst:
    return FakeAnalyst(respond=lambda _: raw)


class TestValidVerdicts:
    def test_take_trade_passes_the_risk_decision_through_unchanged(self) -> None:
        d = decide(FakeAnalyst(lambda _: AnalystDecision.TAKE_TRADE))
        assert d.take
        assert d.risk is APPROVED
        assert d.decided_by == "ai:fake-local:fake-v1"
        assert d.verdict is not None and d.verdict.decision is AnalystDecision.TAKE_TRADE

    @pytest.mark.parametrize("decision", [AnalystDecision.WAIT, AnalystDecision.REJECT])
    def test_wait_and_reject_mean_no_trade(self, decision: AnalystDecision) -> None:
        d = decide(FakeAnalyst(lambda _: decision))
        assert not d.take
        assert d.verdict is not None and d.verdict.decision is decision

    def test_strategy_only_path_takes_without_asking(self) -> None:
        d = decide(None)
        assert (d.take, d.decided_by, d.verdict) == (True, "strategy", None)

    def test_confidence_is_optional_exact_and_bounded(self) -> None:
        verdict = parse_analyst_response(
            json.dumps(response()).replace("}", ', "confidence": 0.7}')
        )
        assert verdict.confidence == Decimal("0.7")
        assert parse_analyst_response(response(confidence=1)).confidence == Decimal(1)

    def test_parsing_json_and_mapping_agree(self) -> None:
        assert parse_analyst_response(json.dumps(response())) == parse_analyst_response(response())


class TestMalformedResponses:
    @pytest.mark.parametrize(
        ("raw", "code"),
        [
            ("not json", "invalid_json"),
            ("[1, 2]", "not_an_object"),
            (None, "not_an_object"),
            (42, "not_an_object"),
            (response(decision="take_trade"), "invalid_decision"),
            (response(decision="BUY"), "invalid_decision"),
            (response(decision=""), "invalid_decision"),
            (response(decision=1), "invalid_decision"),
            (response(reason=""), "invalid_field"),
            (response(reason="x" * 281), "invalid_field"),
            (response(model_id=" "), "invalid_field"),
            (response(confidence=0.9), "invalid_field"),
            (response(confidence=True), "invalid_field"),
            (response(confidence=Decimal("1.01")), "invalid_field"),
            (response(confidence="0.5"), "invalid_field"),
            (response(extra="x"), "unknown_field"),
            (
                '{"decision": "WAIT", "reason": "r", "model_id": "m", "prompt_version": "p", '
                '"confidence": NaN}',
                "invalid_field",
            ),
        ],
    )
    def test_is_rejected_with_a_code(self, raw: object, code: str) -> None:
        with pytest.raises(MalformedAnalystResponseError) as exc:
            parse_analyst_response(raw)
        assert exc.value.code == code

    @pytest.mark.parametrize("field", ["decision", "reason", "model_id", "prompt_version"])
    def test_a_missing_required_field_is_rejected(self, field: str) -> None:
        with pytest.raises(MalformedAnalystResponseError) as exc:
            parse_analyst_response(response(**{field: ...}))
        assert exc.value.code == "missing_field"

    def test_a_malformed_response_never_approves(self) -> None:
        d = decide(answering("TAKE_TRADE"))
        assert not d.take
        assert d.reason.startswith("ai_malformed_response: invalid_json")

    def test_a_response_claiming_another_model_is_refused(self) -> None:
        d = decide(answering(response(model_id="gpt-imposter")))
        assert not d.take and "provenance_mismatch" in d.reason

    def test_an_analyst_that_raises_fails_closed(self) -> None:
        def boom(_: AnalystContext) -> object:
            raise TimeoutError("no answer")

        d = decide(FakeAnalyst(respond=boom))
        assert not d.take and d.reason == "ai_unavailable: TimeoutError"


class TestNoExecutionParameters:
    @pytest.mark.parametrize("field", sorted(FORBIDDEN_FIELDS))
    def test_any_execution_field_rejects_the_whole_response(self, field: str) -> None:
        with pytest.raises(MalformedAnalystResponseError) as exc:
            parse_analyst_response(response(**{field: "1500"}))
        assert exc.value.code == "forbidden_field"

    def test_an_approval_carrying_order_parameters_is_not_taken(self) -> None:
        raw = response(entry_price="1500", stop_loss="1400", quantity=5000, leverage=10)
        d = decide(answering(raw))
        assert not d.take and "forbidden_field" in d.reason

    def test_the_verdict_has_no_field_for_an_execution_parameter(self) -> None:
        names = {f.name for f in dataclasses.fields(AnalystVerdict)}
        assert names == {"decision", "reason", "model_id", "prompt_version", "confidence"}
        assert set(AnalystDecision.__members__) == {"TAKE_TRADE", "WAIT", "REJECT"}

    def test_what_is_taken_is_the_strategy_signal_and_the_risk_quantity(self) -> None:
        d = decide(FakeAnalyst())
        assert d.risk is not None and d.risk.quantity == APPROVED.quantity
        assert {f.name for f in dataclasses.fields(EntryDecision)} == {
            "take",
            "decided_by",
            "reason",
            "risk",
            "verdict",
        }

    def test_the_context_shows_no_future_bar(self) -> None:
        with pytest.raises(ValueError, match="nothing later"):
            AnalystContext(
                signal=SIGNAL, session_bars=(*BARS, make_candle(SESSION_OPEN.replace(minute=50)))
            )


class TestDeterministicAuthority:
    def test_risk_rejection_overrides_an_approving_ai_which_is_never_asked(self) -> None:
        analyst = FakeAnalyst(lambda _: AnalystDecision.TAKE_TRADE)
        d = decide(analyst, REJECTED)
        assert (d.take, d.decided_by, d.reason) == (False, "risk", "insufficient_capital")
        assert analyst.calls == []

    def test_a_rejected_risk_decision_cannot_even_be_shown_to_the_analyst(self) -> None:
        with pytest.raises(ValueError, match="never shown"):
            AnalystContext(signal=SIGNAL, session_bars=BARS, risk=REJECTED)

    def test_the_fake_analyst_is_deterministic(self) -> None:
        def rule(context: AnalystContext) -> AnalystDecision:
            return (
                AnalystDecision.TAKE_TRADE
                if context.signal.direction.is_long
                else AnalystDecision.WAIT
            )

        context = AnalystContext(signal=SIGNAL, session_bars=BARS)
        answers = {FakeAnalyst(rule).analyse(context) for _ in range(5)}
        assert len(answers) == 1
        assert parse_analyst_response(answers.pop()).decision is AnalystDecision.TAKE_TRADE


class TestComparablePaths:
    """Strategy-only and strategy + AI over the golden backtest input."""

    def test_an_approving_ai_reproduces_the_strategy_only_run(self) -> None:
        baseline = run_golden()
        analyst = FakeAnalyst()
        filtered = run_golden(signal_gate=ai_signal_gate(analyst))
        assert filtered.trades == baseline.trades
        assert len(analyst.calls) == len(baseline.signal_log)  # asked once per strategy signal
        assert [r.signal for r in filtered.signal_log] == [r.signal for r in baseline.signal_log]
        assert all(r.decided_by == "ai:fake-local:fake-v1" for r in filtered.signal_log)

    def test_a_rejecting_ai_can_only_subtract(self) -> None:
        baseline = run_golden()
        filtered = run_golden(
            signal_gate=ai_signal_gate(FakeAnalyst(lambda _: AnalystDecision.REJECT))
        )
        assert filtered.trades == ()
        assert [r.signal for r in filtered.signal_log] == [r.signal for r in baseline.signal_log]
        assert not any(r.accepted for r in filtered.signal_log)
        assert all(r.execution_status is None for r in filtered.signal_log)

    def test_a_malformed_ai_filters_everything_out(self) -> None:
        filtered = run_golden(signal_gate=ai_signal_gate(answering('{"decision": "TAKE_TRADE"}')))
        assert filtered.trades == ()
        assert all("missing_field" in r.decision_reason for r in filtered.signal_log)

    def test_a_gate_cannot_substitute_a_different_signal(self) -> None:
        def forging_gate(signal: Signal, bars: object, context: object) -> SignalRecord:
            forged = dataclasses.replace(signal, stop_price=Decimal("1"))
            return SignalRecord(signal=forged, accepted=True, decision_reason="forged")

        with pytest.raises(ValueError, match="only accept or reject the signal it was handed"):
            run_golden(signal_gate=forging_gate)

    def test_the_default_run_is_still_the_golden_run(self) -> None:
        from tests.backtest.test_golden_backtest import assert_golden

        assert_golden(run_golden())


def test_no_llm_client_or_network_is_reachable_from_the_ai_path() -> None:
    app = pathlib.Path(__file__).resolve().parents[1] / "app"
    modules = ("domain/ai/analyst.py", "services/ai_filter.py", "adapters/ai/fake.py")
    forbidden = (
        "anthropic",
        "openai",
        "httpx",
        "socket",
        "urllib",
        "requests",
        "zerodha",
        "app.adapters.paper",
        "app.domain.execution",
        "tradingview",
    )
    offenders = [
        f"{m}: {n}"
        for m in modules
        for n in forbidden
        if f"import {n}" in (text := (app / m).read_text(encoding="utf-8")) or f"from {n}" in text
    ]
    assert offenders == []
