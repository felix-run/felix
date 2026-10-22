"""A request's memo of `thread_state` reads.

A chat turn read one thread's `thread_state` row three times, each in a transaction of its own:
the route's pin check, the react loop's thinking level, and the preview check -- plus the output
style and the permission mode when a manifest declares them. Measured on a plain turn against
Postgres, that was three of its thirteen transactions.

The memo lives in `RequestContext.extras`, so it dies with the request, and only a route that
asks for one has it: a durable fiber, a job or a workflow step builds its own context and reads
the row every time, as everything did before. Within the request a repeat read can miss another
replica's write, which is the same as the request having read once at its start.
"""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable
from typing import Any

THREAD_META_MEMO_EXTRA = "thread_meta_memo"

Key = tuple[str, str]


class ThreadMetaMemo:
    """What a thread's metadata read answered, per `(tenant, thread)`, until it is forgotten."""

    __slots__ = ("_forgotten", "_values")

    def __init__(self) -> None:
        self._values: dict[Key, dict[str, Any]] = {}
        self._forgotten: dict[Key, int] = {}

    async def read(self, key: Key, load: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        """The memo's answer for `key`, or `load()`'s, kept if nothing forgot `key` meanwhile.

        A turn's tool calls run as tasks of one request, so a read can start, a tool's write
        forget the thread and commit, and the read land afterwards with the row as it was before
        the write. The count is what keeps that answer out. Copies both ways: a caller merges
        into what it is handed.
        """
        if key in self._values:
            return copy.deepcopy(self._values[key])
        forgotten = self._forgotten.get(key, 0)
        meta = await load()
        if self._forgotten.get(key, 0) == forgotten:
            self._values[key] = copy.deepcopy(meta)
        return meta

    def forget(self, key: Key) -> None:
        self._values.pop(key, None)
        self._forgotten[key] = self._forgotten.get(key, 0) + 1


def memoize_thread_meta() -> ThreadMetaMemo:
    """Turn the memo on for the current request, and return it.

    The current context gets it; a route that runs the turn under a `RequestContext` of its own
    passes the returned memo on under `THREAD_META_MEMO_EXTRA`, so both share one.
    """
    from felix.context import try_get_context

    ctx = try_get_context()
    if ctx is None:
        return ThreadMetaMemo()
    return ctx.extras.setdefault(THREAD_META_MEMO_EXTRA, ThreadMetaMemo())


def current_memo() -> ThreadMetaMemo | None:
    from felix.context import try_get_context

    ctx = try_get_context()
    return None if ctx is None else ctx.extras.get(THREAD_META_MEMO_EXTRA)
