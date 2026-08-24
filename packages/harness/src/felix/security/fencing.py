"""Marking untrusted text as untrusted, in a way the text cannot undo.

Several prompt surfaces wrap content in a labelled region — a compaction transcript,
recalled memories, the two speaker regions of a memory excerpt — and every one of them
depends on the payload being unable to close that region or open one of its own. The
rule was hand-rolled three times, drifted three ways, and each copy was fixed
separately: `compaction` neutralised only its closing token until a review caught it,
`capture` covered one tag while the prelude emitted two, and both matched exactly, so
`</known_facts >` with one space walked through.

One implementation, and it is deliberately liberal about what counts as a tag: a model
reading `< / KNOWN_FACTS >` sees a closing tag whatever a `str.replace` thinks.
"""

from __future__ import annotations

import re
from functools import lru_cache

# Inserted between `<` and the tag name. Keeps the marker legible to a human reading
# the prompt while making it not read as a tag.
BREAK = "​"


@lru_cache(maxsize=64)
def _pattern(tags: tuple[str, ...]) -> re.Pattern[str]:
    names = "|".join(re.escape(t) for t in tags)
    # Tolerates whitespace after `<` and around the slash, any case, and any
    # attributes — `\b` stops `<known_factsimile>` from matching.
    return re.compile(rf"<\s*(/?)\s*({names})\b", re.IGNORECASE)


def neutralize_tags(text: str, *tags: str) -> str:
    """Stop untrusted text from opening or closing a region of any of `tags`.

    Both directions for every tag. Neutralising only the closing form lets a payload
    close a region, speak in its own voice, and reopen one — after which everything
    following reads as a fresh region of that kind.
    """
    if not tags:
        return text or ""
    return _pattern(tuple(tags)).sub(lambda m: f"<{BREAK}{m.group(1)}{m.group(2)}", text or "")


def fence(text: str, tag: str, *also: str) -> str:
    """Wrap `text` in `<tag>`, neutralising `tag` and `also` inside it.

    The block renders its own escaping, so a surface cannot emit a marker it forgot to
    defend — which is how both previous versions of this shipped a forgeable region.
    """
    body = neutralize_tags(text or "", tag, *also)
    return f"<{tag}>\n{body}\n</{tag}>"


__all__ = ["BREAK", "fence", "neutralize_tags"]
