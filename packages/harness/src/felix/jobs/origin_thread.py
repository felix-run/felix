"""Writing into the conversation a scheduled firing came from, never into one it was not.

A scheduled task's or a heartbeat's firing runs in a thread of its own (`job:` …); the job recorded
the conversation it belongs to (`payload.scheduled_by.thread_id`), and the scheduler carries that on
the firing's context (`jobs/scheduler.py:SCHEDULED_FROM_EXTRA`). Two things write there: a `notify`
the firing sends (`push/agent_tool.py`) and a task's report of its answer (`jobs/scheduler.py`).
Both go through `origin_of` -- which conversation, if any -- and `append_when_idle` -- whether it
landed -- so neither can reach a thread the other could not.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from felix.config import Settings
    from felix.context import RequestContext
    from felix.session.types import AppendableEvent

logger = logging.getLogger("felix.jobs.origin_thread")

# What became of an entry in the conversation it was meant for.
APPENDED = "appended"
RUN_LIVE = "run_live"
MISSING = "missing"
FAILED = "failed"
# No recorded conversation, the run's own, or one that is not a chat thread: nothing to add it to.
NOT_ELIGIBLE = "not_eligible"

# No `:` -- a chat client cannot make a thread with one (`thread_ids.effective_thread_id`), while
# every thread the harness mints for itself has one (`job:`, `channel:`, `a2a:`, `workflow:`,
# `fiber:`) -- so a recorded origin naming a harness thread is neither linked nor written to.
_THREAD_SUFFIX = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]*")


def openable_thread(tenant_id: str, thread_id: object) -> bool:
    """Whether a recorded thread id is a chat thread this tenant's person could open: the tenant's
    own (`thread_ids.thread_belongs_to_tenant`), with a suffix of plain id characters -- no path,
    scheme or `..` for a client to follow, and no `:`, which only the harness's own threads have."""
    from felix.thread_ids import thread_belongs_to_tenant

    if not isinstance(thread_id, str) or not thread_belongs_to_tenant(tenant_id, thread_id):
        return False
    suffix = thread_id[len(tenant_id) + 1 :]
    return _THREAD_SUFFIX.fullmatch(suffix) is not None and ".." not in suffix


def origin_of(ctx: RequestContext, tenant_id: str, acting_for: str) -> str | None:
    """The conversation this firing was scheduled from, or None.

    Only `schedule_task` and a heartbeat's enrolment (`jobs/heartbeat.py:enrol`) write that record
    (`PUT /jobs` strips a caller's), it is used only for a job the person this run acts for wrote,
    and only a chat thread id of this tenant's (`openable_thread`)."""
    from felix.jobs.scheduler import SCHEDULED_FROM_EXTRA

    origin = ctx.extras.get(SCHEDULED_FROM_EXTRA)
    if not isinstance(origin, dict) or not acting_for or origin.get("created_by") != acting_for:
        return None
    thread = origin.get("thread_id")
    return str(thread) if openable_thread(tenant_id, thread) else None


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
            "origin thread append failed tenant=%s thread=%s",
            loggable(tenant_id, limit=80),
            loggable(thread_id, limit=200),
            exc_info=True,
        )
        return FAILED
    return APPENDED


__all__ = [
    "APPENDED",
    "FAILED",
    "MISSING",
    "NOT_ELIGIBLE",
    "RUN_LIVE",
    "append_when_idle",
    "openable_thread",
    "origin_of",
    "run_live",
]
