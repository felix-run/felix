"""Rank a tool catalogue with a decision model: `tools_retrieval.decider: true`.

One typed `Choice` over every candidate tool, asked once per user turn, answered with a
probability for each tool. The shortlist is the `top_k` most probable. Embeddings rank by
how much a tool's description *resembles* the request; a decision model is asked which tool
the request *needs*, which is the question tool retrieval is actually trying to answer.

Two properties keep this from becoming a control that looks present and does nothing:

* **Honest fallback.** An error, or a shortlist that holds less than
  `spec.decider.min_confidence` of the probability mass, returns `None` and the caller
  ranks the way it always has. A "no tool" option is offered, so a request that needs no
  tool does not force the mass onto whichever tool looked least wrong.
* **Asked once per turn.** Selection runs up to four times per loop step, and the state
  here is the user's request, which does not change between steps — so answers are cached
  on the agent, keyed by the state and the candidate set.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from felix_ai.decide import MAX_CHOICE_OPTIONS, Choice

from felix.observability.metrics import record_counter
from felix.patterns.types import ChatMessage
from felix.tools.types import Tool

if TYPE_CHECKING:
    from felix.decisions import MeteredDecider

logger = logging.getLogger("felix.tools.decider_retrieval")

NO_TOOL = "(no tool)"
_INSTRUCTIONS = "Which tool should the assistant use next to carry out the request?"
_NO_TOOL_MEANING = "None of these tools is needed; the assistant can answer directly."
# Jev reads 32k tokens of state plus its longest question. Descriptions are cut and the
# catalogue chunked by characters as well as by count, so a verbose MCP server cannot push
# a question past the window. A chunk is one more question in the *same* request.
_DESCRIPTION_CHARS = 200
_CHUNK_CHARS = 48_000
_REQUEST_CHARS = 4_000
_PREVIOUS_CHARS = 1_000

Ranking = tuple[str, ...] | None


def decision_state(messages: list[ChatMessage]) -> dict[str, str] | None:
    """What the decider sees: the request, and the one before it for follow-ups.

    Deliberately not the transcript. Jev's accuracy drops with irrelevant detail in the
    state, and tool results are the least relevant and the most untrusted text there is.

    `None` when the latest user turn carries no text — an image alone, say. Skipping it
    would rank tools for the turn before, and cache that answer for this one.
    """
    requests = [m.content for m in messages if m.role == "user"]
    if not requests or not isinstance(requests[-1], str) or not requests[-1].strip():
        return None
    state = {"request": requests[-1][:_REQUEST_CHARS]}
    previous = [r for r in requests[:-1] if isinstance(r, str) and r.strip()]
    if previous:
        state["previous_request"] = previous[-1][:_PREVIOUS_CHARS]
    return state


def _chunks(tools: list[Tool]) -> list[list[Tool]]:
    """Groups that each fit one `Choice` — by option count and by characters."""
    per_chunk = MAX_CHOICE_OPTIONS - 1  # one slot is the "no tool" option
    chunks: list[list[Tool]] = [[]]
    size = 0
    for tool in tools:
        cost = len(tool.name) + min(len(tool.description or ""), _DESCRIPTION_CHARS)
        if chunks[-1] and (len(chunks[-1]) >= per_chunk or size + cost > _CHUNK_CHARS):
            chunks.append([])
            size = 0
        chunks[-1].append(tool)
        size += cost
    return chunks


def _questions(chunks: list[list[Tool]]) -> dict[str, Choice]:
    return {
        f"tool_{i}": Choice(
            instructions=_INSTRUCTIONS,
            criteria={
                **{t.name: (t.description or "")[:_DESCRIPTION_CHARS] or None for t in chunk},
                NO_TOOL: _NO_TOOL_MEANING,
            },
        )
        for i, chunk in enumerate(chunks)
    }


def shortlist(
    answers: Mapping[str, Any],
    order: list[str],
    slots: int,
    min_confidence: float,
    *,
    kept: frozenset[str] = frozenset(),
) -> Ranking:
    """The `slots` most probable tools from `order`, or `None` when the decider was unsure.

    `order` is the fallback ranking of the tools still to choose from, and breaks ties, so a
    provider that names only its top pick still yields a full shortlist. `kept` are tools the
    thread already used: offered regardless, so the mass on them counts as covered.

    Coverage is taken per question — the mass on everything the model will be offered, plus
    "no tool" — and the weakest question decides, because each is the decider's answer about
    a different slice of the catalogue. An answer with no distribution (the `llm` backend)
    gives no coverage evidence at all; it is ranked by its pick and not gated — see
    `rank_with_decider`, which says so rather than pretending the gate held.
    """
    probs: dict[str, float] = {}
    distributions: list[Mapping[str, float]] = []
    for answer in answers.values():
        if answer.probabilities:
            distributions.append(answer.probabilities)
            probs.update({n: p for n, p in answer.probabilities.items() if n != NO_TOOL})
        elif answer.choice != NO_TOOL:
            probs[answer.choice] = max(probs.get(answer.choice, 0.0), 1.0)
    position = {name: i for i, name in enumerate(order)}
    ranked = sorted(order, key=lambda name: (-probs.get(name, 0.0), position[name]))
    chosen = tuple(ranked[:slots])
    offered = set(chosen) | kept | {NO_TOOL}
    if distributions:
        coverage = min(sum(p for n, p in dist.items() if n in offered) for dist in distributions)
        if coverage < min_confidence:
            return None
    return chosen


def _is_unscored(answers: Mapping[str, Any]) -> bool:
    return not any(a.probabilities for a in answers.values())


# Deciders already warned about an ungated shortlist, so it is a fact rather than per-turn noise.
_WARNED_UNSCORED: set[str] = set()


async def _answers(
    tools: list[Tool],
    state: dict[str, str],
    decider: MeteredDecider,
    cache: dict[tuple[Any, ...], Any] | None,
) -> Mapping[str, Any] | None:
    """The decider's answers over the whole catalogue, asked once per request.

    Keyed on the request and the *full* catalogue, not on what is left to choose: a turn that
    uses three tools moves each into `kept` in turn, and a key on the remainder re-asked (and
    re-billed, and during an outage re-waited) at every step.
    """
    key = (json.dumps(state, sort_keys=True), tuple(t.name for t in tools))
    if cache is not None and key in cache:
        return cache[key]
    answers: Mapping[str, Any] | None = None
    try:
        result = await decider.decide(state, _questions(_chunks(tools)), purpose="tool_selection")
        answers = result.answers
    except Exception as exc:
        # Logged, not raised: the fallback ranking is a working answer, and a decider outage
        # must not take every turn of every agent using it down with it.
        logger.warning(
            "decider tool selection failed; ranking without it: %s %s",
            type(exc).__name__,
            getattr(exc, "status", ""),
        )
        record_counter("felix_tool_selection", {"method": "error"})
    if cache is not None:
        cache[key] = answers
    return answers


async def rank_with_decider(
    tools: list[Tool],
    kept: frozenset[str],
    fallback_order: list[str],
    messages: list[ChatMessage],
    *,
    slots: int,
    decider: MeteredDecider,
    cache: dict[tuple[Any, ...], Any] | None,
) -> Ranking:
    """Tool names for the shortlist beyond `kept`, most probable first, or `None` to fall back."""
    state = decision_state(messages)
    if state is None or slots <= 0:
        return None
    answers = await _answers(tools, state, decider, cache)
    if answers is None:
        return None
    ranking = shortlist(answers, fallback_order, slots, decider.min_confidence, kept=kept)
    if ranking is not None and decider.min_confidence > 0 and _is_unscored(answers):
        name = str(getattr(decider, "model_id", "") or "decider")
        if name not in _WARNED_UNSCORED:
            _WARNED_UNSCORED.add(name)
            logger.warning(
                "decider %s returns picks without probabilities; spec.decider.min_confidence "
                "cannot gate its tool shortlist and is not applied",
                name,
            )
    method = "unsure" if ranking is None else ("unscored" if _is_unscored(answers) else "decider")
    record_counter("felix_tool_selection", {"method": method})
    return ranking


__all__ = ["NO_TOOL", "decision_state", "rank_with_decider", "shortlist"]
