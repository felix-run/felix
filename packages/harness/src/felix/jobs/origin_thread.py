"""Which conversation a scheduled firing may write into, if any.

A scheduled task's or a heartbeat's firing runs in a thread of its own (`job:` …); the job recorded
the conversation it belongs to (`payload.scheduled_by.thread_id`), and the scheduler carries that on
the firing's context (`jobs/scheduler.py:SCHEDULED_FROM_EXTRA`). Two things write there: a `notify`
the firing sends (`push/agent_tool.py`) and a task's report of its answer (`jobs/agent_tools.py`).
Both ask `origin_of` which conversation, and write through `session/idle_append.py`, so neither can
reach a thread the other could not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from felix.context import RequestContext

# No recorded conversation, the run's own, or one that is not a chat thread: nothing to add it to.
# The other outcomes of an entry are `session/idle_append.py`'s.
NOT_ELIGIBLE = "not_eligible"


def origin_of(ctx: RequestContext, tenant_id: str, acting_for: str) -> str | None:
    """The conversation this firing was scheduled from, or None.

    Only `schedule_task` and a heartbeat's enrolment (`jobs/heartbeat.py:enrol`) write that record
    (`PUT /jobs` strips a caller's), it is used only for a job the person this run acts for wrote,
    and only a chat thread id of this tenant's (`thread_ids.openable_thread`)."""
    from felix.jobs.scheduler import SCHEDULED_FROM_EXTRA
    from felix.thread_ids import openable_thread

    origin = ctx.extras.get(SCHEDULED_FROM_EXTRA)
    if not isinstance(origin, dict) or not acting_for or origin.get("created_by") != acting_for:
        return None
    thread = origin.get("thread_id")
    return str(thread) if openable_thread(tenant_id, thread) else None


__all__ = [
    "NOT_ELIGIBLE",
    "origin_of",
]
