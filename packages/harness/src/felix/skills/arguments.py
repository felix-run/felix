"""What follows `/skill-name`, put into the skill's body where it asks (Claude Code's convention)."""

from __future__ import annotations

import re

# `$ARGUMENTS`, `$ARGUMENTS[N]` and `$N`, as Claude Code substitutes them into a skill's body.
_ARGUMENT_RE = re.compile(r"\$ARGUMENTS\[(\d+)\]|\$ARGUMENTS\b|\$(\d+)\b")
# Whether a body asks for the arguments by name. `$N` alone does not count: it is also `awk '{print
# $1}'` and "a $20 fee", and a body holding only those must still get the arguments appended.
_ASKS_RE = re.compile(r"\$ARGUMENTS\b")


def with_arguments(body: str, arguments: str, *, limit: int) -> str:
    """``body`` with what followed `/name` put where it asks, at most ``limit`` characters.

    `$ARGUMENTS` is the whole text, `$ARGUMENTS[N]` and `$N` the Nth argument (from 0, split as a
    shell would; one past the end is empty). A body that never names `$ARGUMENTS` gets them
    appended, so they are never dropped. Built piece by piece and stopped at ``limit``: a body of a
    hundred thousand `$1`s and a long argument would otherwise expand to gigabytes before any cap.
    """
    from felix.prompts.commands import split_args

    parts = split_args(arguments)
    if _ASKS_RE.search(body) is None:
        body = f"{body}\n\nARGUMENTS: $ARGUMENTS"
    out: list[str] = []
    size = 0
    last = 0
    for match in _ARGUMENT_RE.finditer(body):
        index = match.group(1) or match.group(2)
        if index is None:
            value = arguments
        else:
            i = int(index)
            value = parts[i] if i < len(parts) else ""
        for piece in (body[last : match.start()], value):
            out.append(piece)
            size += len(piece)
        last = match.end()
        if size >= limit:
            return "".join(out)[:limit]
    out.append(body[last:])
    return "".join(out)[:limit]


__all__ = ["with_arguments"]
