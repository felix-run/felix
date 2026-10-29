"""Which foreground `task` child is running, so its tool calls can be shown on the parent's stream.

A foreground child runs through `child.invoke()`, which streams nothing: before this, `subagent_start`
and `subagent_end` bracketed a child whose own work was invisible, and a client had a card that
said "researcher working" for as long as the child took. `delegation.py` names the child here for
the length of its run, and the outermost tool wrapper (`apply_subagent_trace` in the builder) emits
a `subagent_tool` side event at the start and end of each call made while one is named.

A context variable rather than a flag on the request: the parent's own calls -- the `task` call
itself among them -- run outside the child's context and are never reported, and a grandchild
names itself for its own run and restores its parent's name after.

Live only, like the frames that bracket it. The calls are not in the parent's session log, so a
reload shows the `task` call as before; and a durable run's agent is in the worker, whose side-event
queue no stream drains.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

# The longest a call's result is quoted on the frame: enough for a client to tell a failure from
# a success by its first line, which is all the frame is for. The result itself is the child's.
RESULT_HEAD_CHARS = 240

_running: ContextVar[str | None] = ContextVar("felix_running_subagent", default=None)


def running_subagent() -> str | None:
    """The foreground child whose run this is, or None outside one."""
    return _running.get()


@contextmanager
def subagent_running(agent: str) -> Iterator[None]:
    token = _running.set(agent)
    try:
        yield
    finally:
        _running.reset(token)


def result_head(text: str) -> str:
    """The first line of a call's result, cut to `RESULT_HEAD_CHARS`."""
    first = text.strip().split("\n", 1)[0]
    return first if len(first) <= RESULT_HEAD_CHARS else first[: RESULT_HEAD_CHARS - 1] + "…"
