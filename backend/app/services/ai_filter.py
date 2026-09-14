"""The AI analyst port, and the two comparable paths: strategy-only and strategy + AI.

Authority runs one way: **strategy -> risk -> AI filter -> execution safety**.

*   The strategy must have produced a signal. The AI is never asked otherwise.
*   A rejected risk decision is final. The AI is never asked, and its opinion
    could not change the answer anyway.
*   Only then may the AI subtract: ``TAKE_TRADE`` lets the opportunity through
    unchanged; ``WAIT``, ``REJECT``, a malformed answer, a provenance mismatch
    or an analyst that raises all mean no trade.

What passes through is the strategy's own :class:`Signal` and the risk layer's
own :class:`RiskDecision`, never anything built from the AI's answer. Execution
safety (the paper adapter's lifecycle checks) still applies afterwards.

``run_backtest(...)`` is the strategy-only path and
``run_backtest(..., signal_gate=ai_signal_gate(analyst))`` the filtered one, over
the same input, so the two can be compared. The AI run's accepted signals are
always a subset of the strategy-only run's.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.domain.ai.analyst import (
    AnalystContext,
    AnalystDecision,
    AnalystVerdict,
    MalformedAnalystResponseError,
    parse_analyst_response,
)
from app.domain.backtest.engine import SignalGate
from app.domain.backtest.models import SignalRecord
from app.domain.market.models import Candle
from app.domain.risk.sizing import RiskDecision
from app.domain.strategy.contract import Signal, StrategyContext

__all__ = [
    "AiAnalyst",
    "EntryDecision",
    "ai_signal_gate",
    "decide_entry",
]


class AiAnalyst(Protocol):
    """An analyst answers with a raw response; it is parsed, never trusted."""

    model_id: str
    prompt_version: str

    def analyse(self, context: AnalystContext) -> object: ...


@dataclass(frozen=True, slots=True)
class EntryDecision:
    """Whether to submit, who decided, and why.

    ``risk`` is the unchanged risk decision when ``take`` is true. There is no
    field an AI answer could have populated with an execution parameter.
    """

    take: bool
    decided_by: str
    reason: str
    risk: RiskDecision | None = None
    verdict: AnalystVerdict | None = None

    def __post_init__(self) -> None:
        if self.take and (self.risk is None or not self.risk.approved):
            raise ValueError("only an approved risk decision can be taken")


def _consult(analyst: AiAnalyst, context: AnalystContext) -> tuple[AnalystVerdict | None, str]:
    """Ask, parse and check provenance. Any failure is a refusal with a reason."""
    try:
        raw = analyst.analyse(context)
    except Exception as exc:  # an unavailable analyst must fail closed, never approve
        return None, f"ai_unavailable: {type(exc).__name__}"
    try:
        verdict = parse_analyst_response(raw)
    except MalformedAnalystResponseError as exc:
        return None, f"ai_malformed_response: {exc}"
    if (verdict.model_id, verdict.prompt_version) != (analyst.model_id, analyst.prompt_version):
        return None, (
            "ai_malformed_response: provenance_mismatch: response claims "
            f"{verdict.model_id}/{verdict.prompt_version}"
        )
    return verdict, verdict.reason


def _decided_by(analyst: AiAnalyst) -> str:
    return f"ai:{analyst.model_id}:{analyst.prompt_version}"


def decide_entry(
    signal: Signal,
    risk: RiskDecision,
    *,
    session_bars: Sequence[Candle],
    prior_atr: Decimal | None = None,
    analyst: AiAnalyst | None = None,
) -> EntryDecision:
    """strategy -> risk -> (optional) AI. ``analyst=None`` is the strategy-only path."""
    if not risk.approved:
        return EntryDecision(
            take=False,
            decided_by="risk",
            reason=", ".join(r.code.value for r in risk.rejections),
            risk=risk,
        )
    if analyst is None:
        return EntryDecision(take=True, decided_by="strategy", reason=signal.reason, risk=risk)

    context = AnalystContext(
        signal=signal,
        session_bars=tuple(session_bars),
        prior_atr=prior_atr,
        risk=risk,
    )
    verdict, reason = _consult(analyst, context)
    take = verdict is not None and verdict.decision is AnalystDecision.TAKE_TRADE
    return EntryDecision(
        take=take,
        decided_by=_decided_by(analyst),
        reason=reason,
        risk=risk,
        verdict=verdict,
    )


def ai_signal_gate(analyst: AiAnalyst) -> SignalGate:
    """A backtest gate that lets the analyst filter each strategy signal."""

    def gate(
        signal: Signal, session_bars: Sequence[Candle], context: StrategyContext
    ) -> SignalRecord:
        verdict, reason = _consult(
            analyst,
            AnalystContext(
                signal=signal, session_bars=tuple(session_bars), prior_atr=context.prior_atr
            ),
        )
        return SignalRecord(
            signal=signal,
            accepted=verdict is not None and verdict.decision is AnalystDecision.TAKE_TRADE,
            decision_reason=reason,
            decided_by=_decided_by(analyst),
        )

    return gate
