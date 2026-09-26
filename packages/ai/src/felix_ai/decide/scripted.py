"""A decision provider driven by a script instead of a network.

The decision counterpart of `providers/scripted.py`, and not registered by default for the
same reason: a fake in the production registry lets a typo in `FELIX_DECISION_ROUTES` succeed
silently. Call `register_scripted_decider()` to opt in.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from felix_ai.decide.types import Answer, DecisionResult, Question
from felix_ai.types import TokenUsage

Script = Mapping[str, Answer] | Callable[[Any, Mapping[str, Question]], Mapping[str, Answer]]


@dataclass
class ScriptedDecider:
    """Answers from `script`, a fixed mapping or a function of the state and questions.

    `usage` defaults to non-zero for the reason `ScriptedTurn` gives: a double that reports
    nothing leaves the run unmetered, and every budget test built on it passes vacuously.
    """

    model_id: str
    wire_model: str = "scripted-decider"
    script: Script = field(default_factory=dict)
    usage: TokenUsage = field(default_factory=lambda: TokenUsage(input=100, output=1))
    calls: list[tuple[Any, dict[str, Question]]] = field(default_factory=list)

    async def decide(
        self, state: str | Mapping[str, Any] | list[Any], questions: Mapping[str, Question]
    ) -> DecisionResult:
        self.calls.append((state, dict(questions)))
        scripted: Mapping[str, Answer] = (
            self.script if isinstance(self.script, Mapping) else self.script(state, questions)
        )
        missing = [key for key in questions if key not in scripted]
        if missing:
            raise ValueError(f"scripted decider has no answer for: {', '.join(missing)}")
        return DecisionResult(
            answers={key: scripted[key] for key in questions},
            usage=TokenUsage(input=self.usage.input, output=self.usage.output),
            model=self.wire_model,
        )


def scripted_decider_factory(script: Script | None = None, usage: TokenUsage | None = None) -> Any:
    """A factory in the registry's `(model_id, wire_model, options, settings)` shape."""

    def factory(model_id: str, wire_model: str, options: Mapping[str, str], settings: Any) -> ScriptedDecider:
        decider = ScriptedDecider(model_id=model_id, wire_model=wire_model, script=script or {})
        if usage is not None:
            decider.usage = usage
        return decider

    return factory


def register_scripted_decider(
    name: str = "scripted", script: Script | None = None, usage: TokenUsage | None = None
) -> None:
    """Opt in to the scripted decider under `name`."""
    from felix_ai.decide.registry import register_decision_provider

    register_decision_provider(name, scripted_decider_factory(script, usage))


__all__ = ["ScriptedDecider", "register_scripted_decider", "scripted_decider_factory"]
