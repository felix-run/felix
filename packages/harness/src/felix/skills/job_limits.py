"""Per-tenant bounds on the skill quality loop's jobs: how many may wait, and how many a day.

An evaluation is up to forty model calls and an improvement one large one, both on the tenant's
bill, and the worker runs them one at a time across every tenant. So a tenant may hold at most
`FELIX_SKILL_JOBS_MAX_QUEUED` jobs queued or running, and create at most
`FELIX_SKILL_JOBS_DAILY_LIMIT` per UTC day; past either, queueing one more is refused with
`skill_jobs_cap_reached` (429), which waiting fixes. A job is an evaluation, or feedback accepted
with `improve`.

The caps are exact. They are not checked here and then written elsewhere: `job_caps` hands the
limits to the store write that adds the job, which counts both tables and writes its row in one
transaction under a per-tenant lock (`quality_store.hold_job_caps`), so requests racing at the
cap get in one at a time and the one past it is refused.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

from felix.config import Settings
from felix.skills.library import SkillLibraryError
from felix.skills.quality_store import JobCaps, SkillJobsAtCap

_DAY_MS = 86_400_000


class SkillJobsCapReached(SkillLibraryError):
    code = "skill_jobs_cap_reached"


def utc_day_start(now_ms: int) -> int:
    return now_ms - now_ms % _DAY_MS


def job_caps(settings: Settings, *, now_ms: int | None = None) -> JobCaps:
    """The tenant job caps as of ``now_ms`` (now), for the store write that adds a job."""
    return JobCaps(
        max_queued=settings.skill_jobs_max_queued,
        daily_limit=settings.skill_jobs_daily_limit,
        since=utc_day_start(now_ms if now_ms is not None else int(time.time() * 1000)),
    )


@contextmanager
def refused_at_cap() -> Iterator[None]:
    """Around a capped store write: the store's `SkillJobsAtCap` as `SkillJobsCapReached`."""
    try:
        yield
    except SkillJobsAtCap as exc:
        if exc.what == "queued":
            message = f"{exc.count} skill jobs are already queued or running (limit {exc.limit})"
        else:
            message = f"{exc.count} skill jobs were created today (UTC; limit {exc.limit})"
        raise SkillJobsCapReached(message) from exc


__all__ = ["SkillJobsCapReached", "job_caps", "refused_at_cap", "utc_day_start"]
