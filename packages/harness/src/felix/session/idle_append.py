"""Adding entries to a conversation when no run is going on in it, or once the run there ends.

Something outside a conversation's own turns writes into it: a scheduled firing's `notify`
(`push/agent_tool.py`) and a task's report (`jobs/agent_tools.py:report_back`), both into the
conversation the task came from (`jobs/origin_thread.py`). Neither may land between a turn's events,
so `append_when_idle` refuses while a run is live, and `append_or_queue` holds the entries on the
thread until it ends: `deliver_pending`, called where a run ends (`patterns/react.py`'s settle, and
`durability/fibers.py:_after_terminal`).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from felix.config import Settings
    from felix.session.types import AppendableEvent

logger = logging.getLogger("felix.session.idle_append")

# What became of an entry in the conversation it was meant for.
APPENDED = "appended"
RUN_LIVE = "run_live"
MISSING = "missing"
FAILED = "failed"
# Kept out by a live run, and waiting on the thread for it to end (`append_or_queue`).
QUEUED = "queued"

# The thread queue entries wait in (`thread_state.push_thread_queue`), how many may wait -- the
# newest are kept -- and for how long. A run that died with its process leaves its thread marked
# for at most `workspace_notes.ACTIVE_TTL_SECONDS`, and a durable run waiting on a person can be
# live for longer; past a day the report is old news, and its run history still has it.
PENDING_QUEUE = "pending_entries"
MAX_PENDING = 20
PENDING_MAX_AGE_MS = 24 * 3600 * 1000


async def run_live(settings: Settings, tenant_id: str, thread_id: str) -> bool:
    """Whether a run is going on in `thread_id`: a turn that marked itself
    (`workspace_notes.run_active`, here or, through Redis, on any replica), or a durable run
    not yet finished, which may sit between steps with no turn marked."""
    from felix.durability.runs import active_durable_run
    from felix.workspace_notes import run_active

    if await run_active(tenant_id, thread_id):
        return True
    return await active_durable_run(settings, tenant_id, thread_id) is not None


async def append_when_idle(
    settings: Settings, tenant_id: str, thread_id: str, entries: Sequence[AppendableEvent]
) -> str:
    """Add `entries` to `thread_id`; what became of them, one of the values above.

    Never into a thread that does not exist (an entry creates no conversation), and never while a
    run is going on in it: a turn's events are a sequence -- its user turn and its reply, a tool
    call and its result -- and an entry landing between them would split it. Checked inside the
    thread's `leaf_lock` and appended in the same hold, through `annotate_and_append(sync=True)`,
    so the entry is parented on the leaf the store holds -- not on one this replica last saw -- and
    becomes it: on the active branch, where the thread's next turn continues from it.

    An exclusive lease is not a run. A client holds one for as long as it has the thread open,
    idle or not, and that is the person this is for, watching the thread's stream."""
    from felix.logging_setup import loggable
    from felix.session.store import get_session_store
    from felix.session.thread_state import thread_exists
    from felix.session.tree import annotate_and_append, leaf_lock

    session = get_session_store(settings, tenant_id=tenant_id).open(thread_id)
    try:
        if not await thread_exists(session, settings=settings, tenant_id=tenant_id):
            return MISSING
        async with leaf_lock(session):
            if await run_live(settings, tenant_id, thread_id):
                return RUN_LIVE
            await annotate_and_append(session, list(entries), sync=True, lock_held=True)
    except Exception:
        logger.warning(
            "idle append failed tenant=%s thread=%s",
            loggable(tenant_id, limit=80),
            loggable(thread_id, limit=200),
            exc_info=True,
        )
        return FAILED
    return APPENDED


async def append_or_queue(
    settings: Settings, tenant_id: str, thread_id: str, entries: Sequence[AppendableEvent]
) -> str:
    """`append_when_idle`, except that entries a live run kept out wait on the thread (`QUEUED`) and
    land when that run ends (`deliver_pending`), rather than being dropped.

    The writer is the worker and the run is usually the API's, which `leaf_lock` does not
    serialise, so the queue is the thread row's (`thread_state.push_thread_queue`). A run that
    ended between the check and the push already looked and found nothing; so after the push this
    looks again, and delivers itself when no run is live. The take is atomic, so whichever of the
    two delivers, the entries land once."""
    from felix.logging_setup import loggable
    from felix.session.thread_state import push_thread_queue

    outcome = await append_when_idle(settings, tenant_id, thread_id, entries)
    if outcome != RUN_LIVE:
        return outcome
    try:
        await push_thread_queue(
            settings=settings,
            tenant_id=tenant_id,
            thread_id=thread_id,
            name=PENDING_QUEUE,
            items=[_stored(entry) for entry in entries],
            keep=MAX_PENDING,
        )
    except Exception:
        logger.warning("idle append queue failed thread=%s", loggable(thread_id, limit=200), exc_info=True)
        return FAILED
    if not await run_live(settings, tenant_id, thread_id):
        await deliver_pending(settings, tenant_id, thread_id)
    return QUEUED


async def deliver_pending(settings: Settings | None, tenant_id: str, thread_id: str | None) -> None:
    """Add the entries waiting on `thread_id` (`append_or_queue`), now that a run there ended.

    Called where a run ends: a turn's (`patterns/react.py`), and a durable run's
    (`durability/fibers.py:_after_terminal`), which can sit between turns with none marked. Both
    pass what they hold, so ``settings`` and ``thread_id`` may be absent: nothing to deliver then,
    nor for a thread no entry is queued to (`thread_ids.openable_thread`), which skips the read.
    A run still live, or an append that failed, puts them back first in line; a thread that is gone
    drops them, as does age (`PENDING_MAX_AGE_MS`). Never raises: it runs as a run finishes."""
    from felix.thread_ids import openable_thread

    if settings is None or not thread_id or not openable_thread(tenant_id, thread_id):
        return
    from felix.logging_setup import loggable
    from felix.session.thread_state import push_thread_queue, take_thread_queue

    try:
        taken = await take_thread_queue(
            settings=settings, tenant_id=tenant_id, thread_id=thread_id, name=PENDING_QUEUE
        )
        oldest = _now_ms() - PENDING_MAX_AGE_MS
        fresh = [item for item in taken if int(item.get("at") or 0) >= oldest]
        if len(fresh) < len(taken):
            logger.info(
                "idle append: %d waiting entr(ies) expired thread=%s",
                len(taken) - len(fresh),
                loggable(thread_id, limit=200),
            )
        if not fresh:
            return
        outcome = await append_when_idle(settings, tenant_id, thread_id, [_restored(i) for i in fresh])
        if outcome in {RUN_LIVE, FAILED}:
            # Live again, or the append failed: back on the thread, ahead of anything queued since.
            await push_thread_queue(
                settings=settings,
                tenant_id=tenant_id,
                thread_id=thread_id,
                name=PENDING_QUEUE,
                items=fresh,
                keep=MAX_PENDING,
                front=True,
            )
        elif outcome != APPENDED:
            logger.warning(
                "idle append: %d waiting entr(ies) dropped (%s) thread=%s",
                len(fresh),
                outcome,
                loggable(thread_id, limit=200),
            )
    except Exception:
        logger.warning("idle append delivery failed thread=%s", loggable(thread_id, limit=200), exc_info=True)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _stored(entry: AppendableEvent) -> dict[str, Any]:
    """An entry as it waits on the thread's row: masked as the log masks it on the way in
    (`secrets.redact_text` / `redact_json`), since the row is not the log and nothing masks it on
    the way out; and stamped, so one left waiting too long is dropped rather than delivered."""
    from felix.secrets import redact_json, redact_text

    content = entry.content
    return {
        "kind": entry.kind,
        "role": entry.role,
        "content": redact_text(content) if isinstance(content, str) else content,
        "metadata": redact_json(entry.metadata) if entry.metadata else entry.metadata,
        "at": _now_ms(),
    }


def _restored(item: dict[str, Any]) -> AppendableEvent:
    from felix.session.types import AppendableEvent

    return AppendableEvent(
        kind=item.get("kind") or "custom",
        role=item.get("role"),
        content=item.get("content"),
        metadata=dict(item.get("metadata") or {}),
    )


__all__ = [
    "APPENDED",
    "FAILED",
    "MAX_PENDING",
    "MISSING",
    "PENDING_MAX_AGE_MS",
    "PENDING_QUEUE",
    "QUEUED",
    "RUN_LIVE",
    "append_or_queue",
    "append_when_idle",
    "deliver_pending",
    "run_live",
]
