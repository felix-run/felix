"""What to do about a stored manifest written against an older schema.

The schema is `extra=forbid`, and that is load-bearing: a manifest naming `spec.toolz`
should fail rather than silently configure nothing, which is this repo's characteristic
defect. But `forbid` judges *authored* input, and a row already in Postgres is not input
— it was authored once, validated then, and has been sitting there ever since.

So removing a field from the schema retroactively invalidated every stored manifest that
set it. `spec.model.region` went in #125, with the removal's own reasoning being "a
manifest setting it fails validation rather than carrying a field that configures
nothing" — correct for someone writing a manifest today, and fatal for one written before
the removal. A deployment whose `quick` was stored in August answered every request naming
it with `spec.model.region: Extra inputs are not permitted`, and because the store is
consulted before the bundled YAML, a perfectly good `manifests/quick.yaml` sat there
shadowed. The default manifest was dead and nothing said so until a request failed.

The fix is not to loosen validation. It is to say, explicitly and one field at a time,
which fields *used* to exist — so a stored manifest carrying one keeps loading while a
typo keeps failing. `RETIRED` is that list, and adding to it is the price of removing a
field from the schema.

**This covers a removed key, and nothing else.** A field whose accepted *values* narrowed
is the same outage through a different mechanism — `spec.memory.checkpointer` went from a
`Literal` to a registry lookup in #109, so a stored `agentcore` still parses here and then
raises `unknown checkpointer` deep in `build_tenant_agent`. That needs its own answer; do
not reach for this one.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("felix.manifests.compat")

# Path -> why it went, and when. The path is the dotted location in the manifest body,
# exactly as pydantic reports it in `extra_forbidden` errors.
#
# Only add a field here when dropping it is **inert** — when a manifest with it and one
# without it compile to the same agent. `region` qualifies: it was read by nothing, so a
# stored manifest setting it was already getting the behaviour it will get without it.
#
# A field that *did* something is a different question and does not belong here. Silently
# dropping one would start an agent whose governance or routing quietly changed, which is
# worse than refusing to start it. Leave that one out, let it fail loudly, and write the
# migration that rewrites the stored rows.
#
# Two shapes this cannot express, so check before assuming it applies:
#
# * **Inside a list.** `spec.skills`, `spec.mcp`, `spec.policies` and the rest are lists of
#   models; a path cannot descend into their items. Retiring a field from `McpServerRef`
#   means teaching `_parent_of` a list segment first, rather than adding an entry that
#   silently matches nothing.
# * **An aliased field, under one spelling only.** `Spec.mcp` also accepts `mcp_servers`
#   (`populate_by_name`), so a stored manifest may hold either. Retiring an aliased field
#   means listing *every* accepted spelling; listing one leaves the other failing, which is
#   the outage this exists to end, half-fixed.
RETIRED: dict[tuple[str, ...], str] = {
    ("spec", "model", "region"): "removed in 0.3.0 (#125); was declared and read by nothing",
}


# A log line's separator is the newline, and `origin` carries a tenant id.
#
# `assert_valid_tenant_id` rejects `:` and `#` — the delimiters *it* cares about — and
# nothing else, so `acme\nWARNING  all clear` is an accepted tenant id. Interpolated raw,
# it ends the record and starts a second one that reads like the harness said it. This
# repo already names the rule: validating a value for one grammar does not validate it for
# the next, and a value crossing into a log line must be re-validated against *that*
# grammar. Flagged by CodeQL on first review of this module, and reproduced before fixing.
#
# Escaped rather than rejected: the point of the message is to name an unserviceable row,
# and refusing to log because the name is strange loses exactly the information the
# operator needs. Truncated too, since `origin` is attacker-influenced and a log line is
# not a place to put an unbounded string.
_MAX_LOGGED = 200


def one_line(value: str) -> str:
    """`value` with control characters escaped, safe to interpolate into one log record."""
    text = "".join(ch if ch.isprintable() else repr(ch)[1:-1] for ch in str(value))
    return text if len(text) <= _MAX_LOGGED else text[:_MAX_LOGGED] + "…"


def _parent_of(root: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any] | None:
    """The mapping that would hold `path`'s last segment, or None if the path is not there."""
    node: Any = root
    for key in path[:-1]:
        node = node.get(key)
        if not isinstance(node, dict):
            return None
    return node if path[-1] in node else None


def _copy_along(root: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    """A copy of `root` with fresh dicts along `path` and every other value shared."""
    out = dict(root)
    node = out
    for key in path[:-1]:
        child = dict(node[key])
        node[key] = child
        node = child
    return out


def drop_retired(raw: Any) -> tuple[Any, list[tuple[str, ...]]]:
    """Strip retired fields from a raw manifest body, reporting what was dropped.

    Returns the input unchanged (and an empty list) when there is nothing to do, so the
    overwhelmingly common case allocates nothing.

    Never mutates the caller's object — it may be a cache entry someone else is reading,
    and a corruption there surfaces somewhere with no connection to this function. That
    means copying afresh along *each* dropped path: copying once and then editing shares
    every subtree the first copy did not touch, so a second retirement reaches straight
    through into the caller's dict. The first version of this function did exactly that,
    and the single-entry `RETIRED` hid it.
    """
    if not isinstance(raw, dict):
        return raw, []

    dropped: list[tuple[str, ...]] = []
    result = raw
    for path in RETIRED:
        if _parent_of(result, path) is None:
            continue
        result = _copy_along(result, path)
        parent = _parent_of(result, path)
        assert parent is not None  # the copy preserves the shape just probed
        parent.pop(path[-1])
        dropped.append(path)
    return result, dropped


def log_dropped(dropped: list[tuple[str, ...]], *, origin: str) -> None:
    """Say it once per load, naming the manifest — silence here would be the same bug.

    A stored manifest quietly rewritten on every read is exactly the kind of invisible
    accommodation that makes the *next* schema change hard to reason about.

    The remedy is spelled out rather than left as "re-save it": `GET /manifests/{name}`
    echoes the stored body verbatim, retired field included, and `PUT` validates strictly,
    so a plain read-modify-write round trip is refused. The operator has to delete the
    named field, and this says which.
    """
    if not dropped:
        return
    # Every value interpolated into the record goes through `one_line`, including this
    # one, which is drawn from `RETIRED` rather than from the manifest. Two reasons, and
    # the second is the one that will actually happen:
    #
    # * The reasons are hand-written source strings. Nothing stops the next one being
    #   wrapped across two lines, and a newline here splits the record exactly as a
    #   hostile tenant id would — a self-inflicted version of the same bug.
    # * `drop_retired` returns `(cleaned, dropped)`, so taint analysis reasonably treats
    #   the whole tuple as derived from the manifest body. Arguing that half of it is not
    #   is a worse answer than making the argument unnecessary.
    logger.warning(
        "stored manifest %s carries fields the schema has retired (%s); they were ignored. "
        "Delete them from the manifest and re-save it to clear this.",
        one_line(origin),
        # Escaped per entry rather than after the join: `one_line` also truncates, and
        # capping the joined string would drop the tail of the field list — which is the
        # actionable half of the message. Each entry is short; the list length is bounded
        # by `RETIRED`, which is a source constant rather than anything a caller supplies.
        ", ".join(one_line(f"{'.'.join(path)} — {RETIRED[path]}") for path in dropped),
    )


__all__ = ["RETIRED", "drop_retired", "log_dropped", "one_line"]
