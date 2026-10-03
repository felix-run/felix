"""The skill quality loop's worker sweep: accepted improvements, then queued evaluations.

`felix_worker.tasks.skill_jobs` calls `run_skill_jobs` every minute. One sweep runs at a time
across every worker (`quality_store.sweep_lock`); a tick that finds one running skips, so
overlapping ticks cannot multiply the model calls in flight. Each job is claimed one at a time,
just before it runs (`claim_next`, fair across tenants), so nothing sits claimed while the jobs
ahead of it spend their model calls. Each job records its own failure on its row
(`run_claimed_*` never raise), so one bad job does not end the sweep.
"""

from __future__ import annotations

import time
from typing import Any

from felix.config import Settings
from felix.skills.eval_store import get_skill_eval_store
from felix.skills.evaluate import run_claimed_eval
from felix.skills.feedback_store import get_skill_feedback_store
from felix.skills.improve import run_claimed_improvement
from felix.skills.quality_store import sweep_lock

now_ms = lambda: int(time.time() * 1000)

# Jobs of each kind one sweep runs at most. An evaluation is up to forty model calls, so the
# bound is what keeps one sweep from running for many scheduler intervals.
SKILL_JOB_BATCH = 5


async def run_skill_jobs(
    settings: Settings, *, limit: int = SKILL_JOB_BATCH, object_store: Any | None = None
) -> dict[str, int]:
    """Run up to ``limit`` improvements and ``limit`` evaluations. Returns how many of each ran,
    how many of them ended `failed`, and `skipped` 1 when another sweep held the lock."""
    counts = {"improvements": 0, "evals": 0, "failed": 0, "skipped": 0}
    async with sweep_lock(settings) as held:
        if not held:
            counts["skipped"] = 1
            return counts
        feedback = get_skill_feedback_store(settings)
        for _ in range(limit):
            row = await feedback.claim_next(now=now_ms())
            if row is None:
                break
            done = await run_claimed_improvement(settings, row, object_store=object_store)
            counts["improvements"] += 1
            counts["failed"] += done.get("status") == "failed"
        evals = get_skill_eval_store(settings)
        for _ in range(limit):
            row = await evals.claim_next(now=now_ms())
            if row is None:
                break
            done = await run_claimed_eval(settings, row, object_store=object_store)
            counts["evals"] += 1
            counts["failed"] += done.get("status") == "failed"
    return counts


__all__ = ["SKILL_JOB_BATCH", "run_skill_jobs"]
