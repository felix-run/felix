"""Comparing prompts that carry fences named anew for each call.

`felix.security.fencing.fence_fresh` names a region `<tag-<12 hex>>`, so two renders of the same
prompt differ. `unnamed` maps every such name back to its base tag, so a test can compare the
prompt it captured, or count its regions, as it would with a fixed tag. A neutralised marker
keeps its zero-width break after `<` and is never matched, so the forms a test asserts were
neutralised stay as they are. Whether the name is fresh is pinned on its own, in
`tests/unit/test_trust_boundary.py`.
"""

from __future__ import annotations

import re

_FRESH = re.compile(r"<(/?)([a-z_]+)-[0-9a-f]{12}>")


def unnamed(text: str) -> str:
    """`text` with each `<tag-<hex>>` and `</tag-<hex>>` written as `<tag>` and `</tag>`."""
    return _FRESH.sub(r"<\1\2>", text or "")
