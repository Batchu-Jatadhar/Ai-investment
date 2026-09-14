"""A local, deterministic stand-in for an LLM analyst. No network, no randomness."""

from __future__ import annotations

import json
from collections.abc import Callable

from app.domain.ai.analyst import AnalystContext, AnalystDecision

__all__ = ["FakeAnalyst"]


class FakeAnalyst:
    """Answers ``rule(context)`` as a well-formed JSON response.

    ``respond`` replaces the whole raw response instead, so tests can feed the
    parser anything a real model might return.
    """

    def __init__(
        self,
        rule: Callable[[AnalystContext], AnalystDecision] = lambda _: AnalystDecision.TAKE_TRADE,
        *,
        model_id: str = "fake-local",
        prompt_version: str = "fake-v1",
        respond: Callable[[AnalystContext], object] | None = None,
    ) -> None:
        self.model_id = model_id
        self.prompt_version = prompt_version
        self._rule = rule
        self._respond = respond
        self.calls: list[AnalystContext] = []

    def analyse(self, context: AnalystContext) -> object:
        self.calls.append(context)
        if self._respond is not None:
            return self._respond(context)
        decision = self._rule(context)
        return json.dumps(
            {
                "decision": decision.value,
                "reason": f"fake rule chose {decision.value}",
                "model_id": self.model_id,
                "prompt_version": self.prompt_version,
            }
        )
