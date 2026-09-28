"""Suggest the skill a request needs, as a hint — `spec.skill_suggestion`.

The catalog in the system prompt lists every skill's name and description, and the model
decides whether to `activate_skill`. With a large catalog it picks the wrong one, or loads
one it does not need: TypeSafe measured 16.8% wrong and 9.8% needless loads on a 182-skill
catalog, and roughly 2.3x fewer of both with the two-stage suggestion this implements.

1. **Rank** (skipped when the catalog is no bigger than the shortlist): one `Choice` over
   every skill's description, plus a `Noul` gate — does the request ask for a task at all,
   rather than an explanation or a chat.
2. **Rerank** the shortlist with more of each skill (description and the start of its
   body), plus a `Noul` per candidate — does this skill do the specific thing asked.

A hint is emitted only when the gate and the best fit both clear their thresholds, and it
is a *hint*: the model still decides, and still activates. It rides as a transient message
(`ChatMessage.transient`), so it never enters the session log or the cached prefix.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from felix_ai.decide import MAX_CHOICE_OPTIONS, Choice, Noul

from felix.decisions import latest_request
from felix.observability.metrics import record_counter

if TYPE_CHECKING:
    from felix.decisions import MeteredDecider
    from felix.manifests.schema import SkillSuggestionSpec
    from felix.skills.types import Skill

logger = logging.getLogger("felix.skills.suggest")

NO_SKILL = "(no skill)"
_DESCRIPTION_CHARS = 300
_BODY_CHARS = 600
_CHUNK_CHARS = 48_000
_GATE = "The request asks the assistant to carry out a task, not only to explain something or chat."


def _chunks(skills: Sequence[Skill]) -> list[list[Skill]]:
    """Groups that each fit one `Choice`, by option count and by characters."""
    chunks: list[list[Skill]] = [[]]
    size = 0
    for skill in skills:
        cost = len(skill.name) + min(len(skill.description), _DESCRIPTION_CHARS)
        if chunks[-1] and (len(chunks[-1]) >= MAX_CHOICE_OPTIONS - 1 or size + cost > _CHUNK_CHARS):
            chunks.append([])
            size = 0
        chunks[-1].append(skill)
        size += cost
    return chunks


def _probabilities(answer: Any) -> dict[str, float]:
    return dict(answer.probabilities) if answer.probabilities else {answer.choice: 1.0}


@dataclass
class SkillSuggester:
    skills: list[Skill]
    decider: MeteredDecider
    spec: SkillSuggestionSpec

    async def hint(self, messages: Sequence[Any]) -> str | None:
        """A one-line suggestion for this request, or `None`. Never raises."""
        request = latest_request(messages)
        if request is None or not self.skills:
            return None
        try:
            name = await self._suggest(request)
        except Exception as exc:
            logger.warning("skill suggestion failed (%s); no hint this turn", type(exc).__name__)
            record_counter("felix_skill_suggestion", {"outcome": "error"})
            return None
        record_counter("felix_skill_suggestion", {"outcome": "suggested" if name else "none"})
        if name is None:
            return None
        return (
            f"(Felix: the `{name}` skill looks relevant to this request. If it is, load it with "
            "activate_skill before answering; if not, ignore this note.)"
        )

    async def _suggest(self, request: str) -> str | None:
        state = {"request": request}
        by_name = {s.name: s for s in self.skills}
        gate: float | None = None
        candidates = list(self.skills)
        if len(candidates) > self.spec.shortlist:
            candidates, gate = await self._rank(state)
        if (gate is not None and gate < self.spec.min_gate) or not candidates:
            return None
        fits = await self._rerank(state, [by_name[c.name] for c in candidates], ask_gate=gate is None)
        if fits is None:
            return None
        best, best_fit = max(fits.items(), key=lambda item: item[1])
        return best if best_fit >= self.spec.min_fit else None

    async def _rank(self, state: dict[str, str]) -> tuple[list[Skill], float]:
        chunks = _chunks(self.skills)
        questions: dict[str, Any] = {
            f"rank_{i}": Choice(
                instructions="Which skill would help most with the request?",
                criteria={
                    **{s.name: s.description[:_DESCRIPTION_CHARS] or None for s in chunk},
                    NO_SKILL: "No listed skill helps with this request.",
                },
            )
            for i, chunk in enumerate(chunks)
        }
        questions["gate"] = Noul(_GATE)
        result = await self.decider.decide(state, questions, purpose="skill_rank")
        probs: dict[str, float] = {}
        for key, answer in result.answers.items():
            if key.startswith("rank_"):
                probs.update({n: p for n, p in _probabilities(answer).items() if n != NO_SKILL})
        order = {s.name: i for i, s in enumerate(self.skills)}
        # Only skills the ranking gave any weight: when every chunk picked "no skill", a
        # shortlist by catalog order would pay for a rerank of whichever skills come first.
        weighted = [s for s in self.skills if probs.get(s.name, 0.0) > 0.0]
        ranked = sorted(weighted, key=lambda s: (-probs.get(s.name, 0.0), order[s.name]))
        return ranked[: self.spec.shortlist], float(result.answers["gate"].p)

    async def _rerank(
        self, state: dict[str, str], candidates: list[Skill], *, ask_gate: bool
    ) -> dict[str, float] | None:
        questions: dict[str, Any] = {
            f"fit_{i}": Noul(
                f"The skill `{s.name}` does the specific thing the request asks for. "
                f"What it does: {s.description[:_DESCRIPTION_CHARS]} {s.body[:_BODY_CHARS]}".strip()
            )
            for i, s in enumerate(candidates)
        }
        if ask_gate:
            questions["gate"] = Noul(_GATE)
        result = await self.decider.decide(state, questions, purpose="skill_rerank")
        if ask_gate and result.answers["gate"].p < self.spec.min_gate:
            return None
        return {s.name: float(result.answers[f"fit_{i}"].p) for i, s in enumerate(candidates)}


__all__ = ["NO_SKILL", "SkillSuggester"]
