"""Per-tenant bounds on the skill quality loop's jobs: how many may wait, and how many a day.

An evaluation is up to forty model calls and an improvement one large one, both on the tenant's
bill, and the worker runs them one at a time across every tenant. So a tenant may hold at most
`FELIX_SKILL_JOBS_MAX_QUEUED` jobs queued or running, and create at most
`FELIX_SKILL_JOBS_DAILY_LIMIT` per UTC day; past either, queueing one more is refused with
`skill_jobs_cap_reached` (429), which waiting fixes. A job is an evaluation, or feedback accepted
with `improve`.

Count, then insert: two requests racing at the cap can both get in. The caps bound a tenant's
spend and its share of the worker, not an exact count.
"""

from __future__ import annotations

import time

from felix.config import Settings
from felix.skills.eval_store import get_skill_eval_store
from felix.skills.feedback_store import get_skill_feedback_store
from felix.skills.library import SkillLibraryError

_DAY_MS = 86_400_000


class SkillJobsCapReached(SkillLibraryError):
    code = "skill_jobs_cap_reached"


def utc_day_start(now_ms: int) -> int:
    return now_ms - now_ms % _DAY_MS


async def check_job_caps(settings: Settings, tenant_id: str, *, now_ms: int | None = None) -> None:
    """Raise `SkillJobsCapReached` when ``tenant_id`` may not queue another job now."""
    feedback, evals = get_skill_feedback_store(settings), get_skill_eval_store(settings)
    waiting = await feedback.count_jobs_in_flight(tenant_id) + await evals.count_jobs_in_flight(tenant_id)
    if waiting >= settings.skill_jobs_max_queued:
        raise SkillJobsCapReached(
            f"{waiting} skill jobs are already queued or running (limit {settings.skill_jobs_max_queued})"
        )
    since = utc_day_start(now_ms if now_ms is not None else int(time.time() * 1000))
    today = await feedback.count_jobs_since(tenant_id, since) + await evals.count_jobs_since(tenant_id, since)
    if today >= settings.skill_jobs_daily_limit:
        raise SkillJobsCapReached(
            f"{today} skill jobs were created today (UTC; limit {settings.skill_jobs_daily_limit})"
        )


__all__ = ["SkillJobsCapReached", "check_job_caps", "utc_day_start"]
