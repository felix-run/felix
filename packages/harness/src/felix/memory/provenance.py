"""Who said it, and whether that earns a memory the standing to be obeyed.

Split out of `extraction.py` because this is not parsing. The rule spans two modules
— capture writes provenance, recall reads it — and it was previously joined by a bare
`"source"` string literal in one file and two constants two hundred lines away in
another. A third tier, a per-manifest threshold, or a renamed metadata key had to be
got right in both places by someone who first had to find the second one.

The asymmetry that matters: a memory in the reference tier is read, and a memory in
the trusted tier is *obeyed*. So the extractor's claim about where a memory came from
is treated as a request rather than a verdict — a prompt-injected tool result reaching
the extractor could otherwise simply ask for the standing that gets it followed.
"""

from __future__ import annotations

import re
from typing import Any

USER_SOURCE = "user"
ASSISTANT_SOURCE = "assistant"

# The metadata key provenance is stored under. Named, because capture writes it and
# recall reads it from different modules.
SOURCE_KEY = "source"

# The one kind that can be obeyed. A fact the user stated is still knowledge rather
# than a rule, so provenance alone does not promote it.
INSTRUCTION_KIND = "instruction"

_WORD_RE = re.compile(r"[a-z0-9']+")

# Words carried by almost any sentence. Counting them toward grounding would let a
# memory built entirely from assistant text pass on "the", "a", "is" — measured, an
# unfiltered comparison scores 0.80 between "see the credentials in the vault" and
# "see the report in the dashboard".
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "was",
        "were",
        "will",
        "with",
        "you",
        "your",
        "i",
        "me",
        "my",
        "we",
        "our",
        "they",
        "their",
        "this",
        "these",
        "those",
        "not",
        "no",
        "do",
        "does",
        "did",
        "can",
        "could",
        "should",
        "would",
    ]
)

# How much of a memory's substance must appear in the user's own words before its
# claim of user provenance is honoured. Not 1.0: extraction resolves pronouns and
# names subjects, so a faithful memory is a paraphrase rather than a quotation.
GROUNDING_THRESHOLD = 0.6


def content_words(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall((text or "").lower()) if w not in _STOPWORDS}


def resolve_provenance(claimed_source: str, content: str, *, user_text: str) -> str:
    """Decide a memory's provenance from the text, not from the model's claim.

    Failing the check downgrades to assistant provenance rather than dropping the
    memory: a misattributed instruction becomes reference material, which is the
    behaviour that existed before the trusted tier and is safe.
    """
    if claimed_source != USER_SOURCE:
        return ASSISTANT_SOURCE
    words = content_words(content)
    if not words:
        return ASSISTANT_SOURCE
    overlap = len(words & content_words(user_text)) / len(words)
    return USER_SOURCE if overlap >= GROUNDING_THRESHOLD else ASSISTANT_SOURCE


def is_trusted_instruction(row: dict[str, Any]) -> bool:
    """Whether a stored row may be surfaced as something to obey.

    Both halves are required and neither is sufficient. `kind` says the memory is a
    rule rather than a fact; provenance says the rule came from the person rather than
    from a reply that may be echoing tool output. A row written before this tier
    existed carries no source and reads as untrusted, which is the safe default.
    """
    if str(row.get("kind") or "") != INSTRUCTION_KIND:
        return False
    metadata = row.get("metadata") or {}
    return isinstance(metadata, dict) and metadata.get(SOURCE_KEY) == USER_SOURCE


__all__ = [
    "ASSISTANT_SOURCE",
    "GROUNDING_THRESHOLD",
    "INSTRUCTION_KIND",
    "SOURCE_KEY",
    "USER_SOURCE",
    "content_words",
    "is_trusted_instruction",
    "resolve_provenance",
]
