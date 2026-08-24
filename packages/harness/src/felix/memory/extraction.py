"""Turning a finished turn into memories worth keeping.

The first version asked for "durable facts, one per line" and stored whatever came
back. Run against a real model it stored this, verbatim, as a durable fact:

    "If you need me to reference this information later in our current conversation,
     I'll have it available."

That is the assistant talking about itself. It is not knowledge, it will be recalled
in future turns, and it will be recalled forever, because nothing supersedes it.

Three things fix that, and none of them is a longer prompt:

**Structure.** Asking for JSON with a `topic_key` gets a key that later values
supersede, so a store of facts stays a store of current facts rather than an
accumulation of everything ever said. Line-oriented output has nowhere to put one.

**An explicit exclusion.** "Skip ephemeral chatter" does not tell a model that a
sentence about its own capabilities is not a fact about the world. Saying so does.

**Optional verification.** A second pass keeps only what the excerpt actually
supports. It costs another call, so it is opt-in — but when it returns nothing
parseable the unverified set is kept, because a broken verifier must not silently
empty the store.

Parsing never raises. A malformed response yields fewer memories, never an exception
into the turn loop.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("felix.memory.extraction")

_KINDS = ("fact", "event", "instruction", "task")


class ExtractedMemory(BaseModel):
    """One memory the extractor proposes.

    `extra="ignore"` rather than the repo's usual `forbid`: this parses model output,
    and one stray key must not discard an otherwise good memory.
    """

    model_config = ConfigDict(extra="ignore")

    content: str
    kind: str = "fact"
    topic_key: str = ""
    importance: float = Field(default=0.5, ge=0.0, le=1.0)


EXTRACT_SYSTEM = """You extract durable memories from a conversation so an assistant \
can use them in later, unrelated sessions.

Return ONLY a JSON array. Each element:
  {"content": "<one self-contained sentence>",
   "kind": "fact|event|instruction|task",
   "topic_key": "<stable.dotted.key or empty>",
   "importance": <0.0-1.0>}

Rules:
- content must stand alone. Resolve pronouns, name the subject. A reader with no
  access to this conversation must understand it.
- topic_key groups values that replace each other, so a newer one supersedes the
  older: "user.timezone", "deploy.runbook". Set it for facts and instructions. Leave
  it empty for events and tasks, which accumulate rather than replace.
- kind: "fact" is stable knowledge or preference; "event" is something that happened;
  "instruction" is a rule to follow; "task" is work in progress.

Never extract:
- anything the assistant says about itself, its memory, its tools or its limitations;
- pleasantries, acknowledgements, apologies, or offers to help;
- restatements of what the user just said, unless they carry information worth
  keeping on their own;
- anything true only inside this conversation.

Extract only what the excerpt clearly supports. Return [] if nothing is worth
keeping — that is the common case, and an empty array is a better answer than a
weak memory."""

VERIFY_SYSTEM = """You check proposed memories against the conversation they came from.

Return ONLY a JSON array: the subset that is clearly and directly supported by the \
excerpt, with each kept item unchanged. Drop anything speculative, anything about the \
assistant rather than the world, and anything the excerpt does not actually state."""


def _extract_json_array(text: str) -> str | None:
    """Find the first balanced `[...]` span, ignoring fences and surrounding prose.

    A model asked for JSON often returns it wrapped in explanation or a code fence.
    Scanning for balance — while tracking string state so a bracket inside a string
    does not confuse the count — recovers the array from all of it.
    """
    stripped = (text or "").strip()
    start = stripped.find("[")
    if start == -1:
        return None

    depth, in_string, escaped = 0, False, False
    for i in range(start, len(stripped)):
        ch = stripped[i]
        if escaped:
            escaped = False
            continue
        if in_string and ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return stripped[start : i + 1]
    return None


def parse_memories(text: str) -> list[ExtractedMemory]:
    """Best-effort parse. Never raises; skips whatever it cannot read."""
    raw = _extract_json_array(text)
    if raw is None:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []

    out: list[ExtractedMemory] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            memory = ExtractedMemory.model_validate(item)
        except Exception:
            continue
        if not memory.content.strip():
            continue
        if memory.kind not in _KINDS:
            memory.kind = "fact"
        out.append(memory)
    return out


def dedupe_key(content: str) -> str:
    return " ".join((content or "").lower().split())


def merge(*groups: list[ExtractedMemory]) -> list[ExtractedMemory]:
    """Merge proposals, first occurrence winning, matched on normalised content."""
    seen: dict[str, ExtractedMemory] = {}
    for group in groups:
        for memory in group:
            seen.setdefault(dedupe_key(memory.content), memory)
    return list(seen.values())


async def _ask(model: Any, system: str, user: str, *, max_tokens: int = 2048) -> str:
    from felix.patterns.model import ModelChatOptions
    from felix.patterns.types import ChatMessage

    result = await model.chat(
        [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=user),
        ],
        [],
        # A side request in the middle of somebody's turn: it carries a completely
        # different prefix, so it must not spend the conversation's prompt cache.
        ModelChatOptions(isolate_cache=True, max_tokens=max_tokens),
    )
    return result.message.content or ""


async def extract_memories(
    model: Any,
    excerpt: str,
    *,
    max_facts: int = 3,
    verify: bool = False,
) -> list[ExtractedMemory]:
    """Propose memories from a conversation excerpt.

    Returns `[]` rather than raising on any failure — a turn must not fail because
    memory extraction did.
    """
    try:
        proposed = parse_memories(await _ask(model, EXTRACT_SYSTEM, excerpt))
    except Exception:
        logger.debug("memory extraction call failed", exc_info=True)
        return []

    proposed = merge(proposed)[:max_facts]
    if not proposed or not verify:
        return proposed

    try:
        payload = json.dumps([m.model_dump() for m in proposed])
        checked = parse_memories(
            await _ask(model, VERIFY_SYSTEM, f"Excerpt:\n{excerpt}\n\nProposed:\n{payload}")
        )
    except Exception:
        logger.debug("memory verification failed; keeping the unverified set", exc_info=True)
        return proposed
    # An unparseable verification is not evidence that everything was wrong.
    return checked or proposed


_META = re.compile(
    r"\b(i'?ll |i am |i'?m |i can|i have|i don'?t|my memory|as an ai|let me know|"
    r"happy to|feel free|if you need)",
    re.IGNORECASE,
)


def looks_like_assistant_meta(text: str) -> bool:
    """Whether a sentence is the assistant talking about itself rather than the world.

    A backstop for the heuristic path, which has no model to apply judgement — and a
    cheap second line of defence for the model path, since this is the exact failure
    that made capture store an apology as a durable fact.
    """
    return bool(_META.search(text or ""))


__all__ = [
    "EXTRACT_SYSTEM",
    "VERIFY_SYSTEM",
    "ExtractedMemory",
    "dedupe_key",
    "extract_memories",
    "looks_like_assistant_meta",
    "merge",
    "parse_memories",
]
