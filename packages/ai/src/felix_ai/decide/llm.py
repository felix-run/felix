"""Answer typed questions with an ordinary chat model.

What makes the decision seam model-agnostic in practice rather than in name: a deployment
with no Jev key routes a decision to `{"provider": "llm", "model": "claude-haiku"}` and
every consumer still works. The answer shape is enforced through `output_schema`, so the
reply is a value rather than prose to parse.

What it does not do is invent certainty. A chat model asked for a probability produces a
number, not a calibrated one, so answers here carry the pick and `confidence=None`, and a
consumer gating on confidence treats that as "no signal" rather than as sure.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from felix_ai.decide.types import (
    Answer,
    Choice,
    ChoiceAnswer,
    DecisionResult,
    NoulAnswer,
    Question,
    Score,
    ScoreAnswer,
    question_to_wire,
)
from felix_ai.types import ChatMessage, ModelChatOptions, ModelProvider

_SYSTEM = (
    "You answer typed questions about a state. Read the state, answer every question "
    "independently, and reply with JSON matching the schema. For a choice, pick exactly one "
    "of the listed options. For a score, give the index of the level that fits (0 = first). "
    "For a noul, give the probability, 0 to 1, that the statement is true."
)


def _schema_for(q: Question) -> dict[str, Any]:
    if isinstance(q, Choice):
        return {"type": "string", "enum": list(q.criteria)}
    if isinstance(q, Score):
        return {"type": "integer", "minimum": 0, "maximum": len(q.levels) - 1}
    return {"type": "number", "minimum": 0, "maximum": 1}


def output_schema_for(questions: Mapping[str, Question]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {key: _schema_for(q) for key, q in questions.items()},
        "required": list(questions),
        "additionalProperties": False,
    }


def _answer(q: Question, value: Any) -> Answer:
    if isinstance(q, Choice):
        choice = str(value)
        if choice not in q.criteria:
            raise ValueError(f"choice {choice!r} is not one of the offered options")
        return ChoiceAnswer(choice=choice, probabilities={choice: 1.0}, confidence=None)
    if isinstance(q, Score):
        return ScoreAnswer(score=float(value), confidence=None)
    return NoulAnswer(p=min(1.0, max(0.0, float(value))))


class LLMDecider:
    """A `DecisionProvider` over any `ModelProvider`."""

    def __init__(self, model: ModelProvider, *, model_id: str = "") -> None:
        self._model = model
        self.model_id = model_id or model.model_id
        route = getattr(model, "route", None)
        self.wire_model = str(getattr(route, "model", "") or model.model_id)

    async def decide(
        self, state: str | Mapping[str, Any] | list[Any], questions: Mapping[str, Question]
    ) -> DecisionResult:
        prompt = json.dumps(
            {"state": state, "questions": {k: question_to_wire(q) for k, q in questions.items()}},
            ensure_ascii=False,
        )
        result = await self._model.chat(
            [ChatMessage(role="system", content=_SYSTEM), ChatMessage(role="user", content=prompt)],
            [],
            ModelChatOptions(isolate_cache=True, output_schema=output_schema_for(questions)),
        )
        text = (result.message.content or "").strip()
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("chat model returned no JSON decision") from exc
        if not isinstance(raw, dict):
            raise ValueError("chat model decision is not a JSON object")
        missing = [key for key in questions if key not in raw]
        if missing:
            raise ValueError(f"chat model left unanswered: {', '.join(missing)}")
        return DecisionResult(
            answers={key: _answer(q, raw[key]) for key, q in questions.items()},
            usage=result.usage,
            model=self.wire_model,
        )


__all__ = ["LLMDecider", "output_schema_for"]
