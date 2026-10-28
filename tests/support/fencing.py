"""Comparing prompts that carry fences named anew for each call.

`felix.security.fencing.fence_fresh` names a region `<tag-<12 hex>>`, so two renders of the same
prompt differ. `unnamed` maps each such name of one tag back to the base tag, so a test can
compare the prompt it captured, or count its regions, as it would with a fixed tag. A neutralised
marker keeps its zero-width break after `<` and is never matched, so the forms a test asserts were
neutralised stay as they are. Whether the name is fresh is pinned on its own, in
`tests/unit/test_trust_boundary.py`.
"""

from __future__ import annotations

import re
from collections import Counter


def unnamed(text: str | None, tag: str = "untrusted_transcript") -> str:
    """`text` with each `<tag-<hex>>` and `</tag-<hex>>` written as `<tag>` and `</tag>`.

    Each fresh name must open exactly once and close exactly once: mapping every name to one
    spelling would otherwise let a region that opened under one name and closed under another --
    one never closed at all -- read as well formed.
    """
    fresh = re.compile(rf"<(/?){re.escape(tag)}-([0-9a-f]{{12}})>")
    seen = Counter((m.group(1), m.group(2)) for m in fresh.finditer(text or ""))
    for name in {n for _, n in seen}:
        assert (seen[("", name)], seen[("/", name)]) == (1, 1), f"{tag}-{name} is not one open and one close"
    return fresh.sub(rf"<\1{tag}>", text or "")
