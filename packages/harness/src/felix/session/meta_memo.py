"""A request's memo of `thread_state` reads -- see `thread_state.get_thread_meta`."""

from __future__ import annotations

from typing import Any

# --- the memo ---------------------------------------------------------------------------
#
# A chat turn read this row three times, each in a transaction of its own: the route's pin
# check, the react loop's thinking level, and the preview check -- plus the output style and the
# permission mode when a manifest declares them. Measured on a plain turn against Postgres, that
# was three of its thirteen transactions.
#
# The memo lives in `RequestContext.extras`, so it dies with the request, and only a route that
# asks for one has it: a durable fiber, a job or a workflow step builds its own context and reads
# the row every time, as everything did before. Within the request a repeat read can miss another
# replica's write, which is the same as the request having read once at its start. Every write in
# `thread_state` forgets the thread before it writes and again after it commits (`ThreadMetaMemo`
# says why the second). `_PostgresSession.store_leaf` and `adopt_leaf` write the
# row without forgetting it, deliberately: they change the leaf column and `LEAF_TRACKED_KEY`,
# and `_row_meta` hides both, so nothing a memoized read answers can differ.
THREAD_META_MEMO_EXTRA = "thread_meta_memo"


class ThreadMetaMemo:
    """What `get_thread_meta` answered for each `(tenant, thread)`, and how often each was forgotten.

    The count is what makes forgetting stick under concurrency. A turn's tool calls run as tasks
    of one request, so a read can start, a tool's write forget the thread and commit, and the
    read land afterwards with the row as it was before the write. The read keeps its answer only
    if nothing was forgotten while it was out.
    """

    __slots__ = ("forgotten", "values")

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], dict[str, Any]] = {}
        self.forgotten: dict[tuple[str, str], int] = {}

    def forget(self, key: tuple[str, str]) -> None:
        self.values.pop(key, None)
        self.forgotten[key] = self.forgotten.get(key, 0) + 1


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
