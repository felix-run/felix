"""Typed decisions: a question with a closed answer, and the answer with its certainty.

A chat model answers a decision in prose that the caller then parses — "reply with only the
agent name", "reply with a number" — and gets back a string with no notion of how sure the
model was. A decision provider takes the question *as a type* and returns a value the code
can branch on, with the probability mass behind it. Three shapes cover it:

* `Choice` — pick one option from a closed set. Answer: the option, a distribution, a confidence.
* `Score` — place something on an ordered scale of described levels.
* `Noul` — how likely a statement is true, 0..1.

The vocabulary is TypeSafe's, because Jev is the provider this seam was written for and its
three primitives are the right cut. Nothing here is Jev-specific: `llm.py` answers the same
questions with any chat model, and a plugin can register another backend.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from felix_ai.types import TokenUsage

# The provider's own ceilings, checked before the request so a 256-tool catalogue fails
# here with the number in the message rather than as an opaque 422 from upstream.
MAX_CHOICE_OPTIONS = 255
MIN_SCORE_LEVELS = 2
MAX_SCORE_LEVELS = 10


@dataclass(frozen=True, slots=True)
class Choice:
    """Select one option. `criteria` maps each option to what it means (or `None`)."""

    instructions: str
    criteria: Mapping[str, str | None]

    def __post_init__(self) -> None:
        if not self.criteria:
            raise ValueError("a Choice needs at least one option")
        if len(self.criteria) > MAX_CHOICE_OPTIONS:
            raise ValueError(
                f"a Choice accepts at most {MAX_CHOICE_OPTIONS} options, got {len(self.criteria)}"
            )


@dataclass(frozen=True, slots=True)
class Score:
    """Rate against ordered levels, lowest first. The answer may land between levels."""

    instructions: str
    levels: tuple[str, ...]

    def __post_init__(self) -> None:
        if not MIN_SCORE_LEVELS <= len(self.levels) <= MAX_SCORE_LEVELS:
            raise ValueError(
                f"a Score takes {MIN_SCORE_LEVELS}-{MAX_SCORE_LEVELS} levels, got {len(self.levels)}"
            )


@dataclass(frozen=True, slots=True)
class Noul:
    """How likely a statement holds. `criteria` may say what true and false mean."""

    instructions: str
    criteria: Mapping[str, str] | None = None


Question = Choice | Score | Noul


@dataclass(frozen=True, slots=True)
class ChoiceAnswer:
    choice: str
    # Option -> probability, summing to 1. Empty when the provider gave only the pick.
    probabilities: Mapping[str, float] = field(default_factory=dict)
    # How concentrated the distribution is, 0..1. `None` when the provider cannot say —
    # a chat model's self-reported certainty is not calibrated, so it is not invented.
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class ScoreAnswer:
    # Probability-weighted level index; may land between levels.
    score: float
    probabilities: Mapping[int, float] = field(default_factory=dict)
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class NoulAnswer:
    p: float


Answer = ChoiceAnswer | ScoreAnswer | NoulAnswer


@dataclass(frozen=True, slots=True)
class DecisionResult:
    answers: Mapping[str, Answer]
    usage: TokenUsage | None = None
    # The provider's own model version, e.g. `jev-1.13.0`, for the audit trail.
    model: str = ""


@runtime_checkable
class DecisionProvider(Protocol):
    """Answers typed questions about one state in a single call.

    `model_id` is the logical route name an operator configured; `wire_model` is what the
    provider calls it, and is what the catalog prices.
    """

    model_id: str
    wire_model: str

    async def decide(
        self,
        state: str | Mapping[str, Any] | list[Any],
        questions: Mapping[str, Question],
    ) -> DecisionResult: ...


def question_to_wire(q: Question) -> dict[str, Any]:
    """The TypeSafe wire shape of one question — also what `llm.py` shows a chat model."""
    if isinstance(q, Choice):
        return {"type": "choice", "instructions": q.instructions, "criteria": dict(q.criteria)}
    if isinstance(q, Score):
        return {"type": "score", "instructions": q.instructions, "criteria": list(q.levels)}
    body: dict[str, Any] = {"type": "noul", "instructions": q.instructions}
    if q.criteria:
        body["criteria"] = dict(q.criteria)
    return body


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except TypeError, ValueError:
        return default


def answer_from_wire(q: Question, raw: Mapping[str, Any]) -> Answer:
    """Parse one answer, checked against the question it answers.

    A choice outside the offered set is an error rather than a value: the caller indexes
    its own options with it, and a silently-accepted stranger is how the router ended up
    falling back to its first sub-agent without saying so.
    """
    if isinstance(q, Choice):
        choice = str(raw.get("choice", ""))
        if choice not in q.criteria:
            raise ValueError(f"choice {choice!r} is not one of the offered options")
        probs = raw.get("probabilities") or {}
        conf = raw.get("confidence")
        return ChoiceAnswer(
            choice=choice,
            probabilities={str(k): _float(v) for k, v in probs.items() if str(k) in q.criteria},
            confidence=None if conf is None else _float(conf),
        )
    if isinstance(q, Score):
        probs = raw.get("probabilities") or {}
        conf = raw.get("confidence")
        return ScoreAnswer(
            score=_float(raw.get("score")),
            probabilities={int(k): _float(v) for k, v in probs.items()},
            confidence=None if conf is None else _float(conf),
        )
    if "noul" not in raw:
        raise ValueError("noul answer carries no `noul` value")
    return NoulAnswer(p=min(1.0, max(0.0, _float(raw.get("noul")))))


def answers_from_wire(questions: Mapping[str, Question], raw: Mapping[str, Any]) -> dict[str, Answer]:
    """Every question answered, or `ValueError` naming the one that was not."""
    missing = [key for key in questions if key not in raw]
    if missing:
        raise ValueError(f"decision provider left unanswered: {', '.join(missing)}")
    return {key: answer_from_wire(q, raw[key]) for key, q in questions.items()}


_ANSWER_TYPE: dict[type, type] = {Choice: ChoiceAnswer, Score: ScoreAnswer, Noul: NoulAnswer}


def validate_answers(questions: Mapping[str, Question], answers: Mapping[str, Answer]) -> None:
    """Every question answered, with the right type, and every choice one that was offered.

    Run by the harness on whatever a provider returns, not only inside the built-in parsers:
    the registry is open, and a plugin's provider is exactly the one that has not been read.
    """
    for key, q in questions.items():
        answer = answers.get(key)
        if answer is None:
            raise ValueError(f"decision provider left {key!r} unanswered")
        if not isinstance(answer, _ANSWER_TYPE[type(q)]):
            raise ValueError(f"decision provider answered {key!r} with a {type(answer).__name__}")
        if isinstance(answer, NoulAnswer) and not (math.isfinite(answer.p) and 0.0 <= answer.p <= 1.0):
            # NaN compares False against every threshold, so a judge gating on `p < t` passes it.
            raise ValueError(f"decision provider answered {key!r} with p={answer.p}, not a probability")
        if isinstance(answer, ChoiceAnswer) and isinstance(q, Choice) and answer.choice not in q.criteria:
            raise ValueError(f"decision provider chose {answer.choice!r} for {key!r}, which was not offered")
        if (
            isinstance(answer, ScoreAnswer)
            and isinstance(q, Score)
            and not 0 <= answer.score <= len(q.levels) - 1
        ):
            raise ValueError(
                f"decision provider scored {key!r} at {answer.score}, outside its {len(q.levels)} levels"
            )


__all__ = [
    "MAX_CHOICE_OPTIONS",
    "MAX_SCORE_LEVELS",
    "MIN_SCORE_LEVELS",
    "Answer",
    "Choice",
    "ChoiceAnswer",
    "DecisionProvider",
    "DecisionResult",
    "Noul",
    "NoulAnswer",
    "Question",
    "Score",
    "ScoreAnswer",
    "answer_from_wire",
    "answers_from_wire",
    "question_to_wire",
    "validate_answers",
]
