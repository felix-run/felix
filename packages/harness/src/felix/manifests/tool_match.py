"""Match a manifest's `tools:` list against the tools actually bound.

Every governance control that targets tools by name — policies, approvals, judges, content
screening — took its list as an enumeration of exact names. The public docs said otherwise:
*"Tool targeting for policies, approvals, and judges matches by glob so MCP tools named
`server__*` stay gated even if the remote renames suffixes."* The docs described the feature
operators need and the code did not have it, so a rule written the documented way gated
nothing — a control present in the manifest, blessed by `felix validate-manifest`, enforcing
nothing. That is this repo's recurring defect, arrived at by believing its own documentation.

`fnmatchcase`, not `fnmatch`. The latter runs both sides through `os.path.normcase`, which is
a no-op on POSIX and lowercases on Windows — so on every platform this project supports the
two are identical, and no test here can tell them apart. It is the defensive choice rather
than a fix: a governance decision should not become case-insensitive because someone ran the
suite somewhere new.

Full glob rather than the `exact | prefix* | *` subset the docs listed. The subset has a
silent-failure mode of its own — `*__search` would be read as a literal name, match nothing,
and gate nothing — and "the pattern syntax you already know" is a smaller thing to learn than
"the three shapes this one field accepts".
"""

from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatchcase

__all__ = ["matches_any", "select", "unmatched_patterns"]


def matches_any(patterns: Iterable[str], name: str) -> bool:
    """Does `name` match at least one pattern? A plain name matches itself."""
    return any(fnmatchcase(name, p) for p in patterns)


def select(patterns: Iterable[str], names: Iterable[str]) -> set[str]:
    """Every name matching at least one pattern."""
    pats = list(patterns)
    return {n for n in names if matches_any(pats, n)}


def unmatched_patterns(patterns: Iterable[str], names: Iterable[str]) -> list[str]:
    """Patterns that match nothing in `names`.

    A pattern matching no bound tool is the inert-control shape again: a typo, a renamed MCP
    server, or a glob written before the tool it targets existed. Callers log this rather than
    refusing, because the bound set legitimately varies — an MCP server whose discovery failed
    at compile time would otherwise take the whole agent down with it.
    """
    known = list(names)
    return [p for p in patterns if not any(fnmatchcase(n, p) for n in known)]
