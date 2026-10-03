"""The skill quality loop's worker sweep: accepted improvements, then queued evaluations.

`felix_worker.tasks.skill_jobs` calls `run_skill_jobs` every minute. Each job is claimed one at a
time, just before it runs, so nothing sits claimed while the jobs ahead of it spend their model
calls -- a batch claimed up front could outlive its own lease and be taken over half-finished.
Two ticks overlapping is fine: each claim skips what another holds. Each job records its own
failure on its row (`run_claimed_*` never raise), so one bad job does not end the sweep.
"""

from __future__ import annotations

import time
from typing import Any

from felix.config import Settings
from felix.skills.evaluate import run_claimed_eval
from felix.skills.improve import run_claimed_improvement
from felix.skills.quality_store import get_skill_eval_store, get_skill_feedback_store

now_ms = lambda: int(time.time() * 1000)

# Jobs of each kind one sweep runs at most. An evaluation is up to forty model calls, so the
# bound is what keeps one tick from running for longer than the scheduler's interval many times over.
SKILL_JOB_BATCH = 5


async def run_skill_jobs(
    settings: Settings, *, limit: int = SKILL_JOB_BATCH, object_store: Any | None = None
) -> dict[str, int]:
    """Run up to ``limit`` improvements and ``limit`` evaluations. Returns how many of each ran,
    and how many of them ended `failed`."""
    counts = {"improvements": 0, "evals": 0, "failed": 0}
    feedback = get_skill_feedback_store(settings)
    for _ in range(limit):
        claimed = await feedback.claim_improvements(limit=1, now=now_ms())
        if not claimed:
            break
        done = await run_claimed_improvement(settings, claimed[0], object_store=object_store)
        counts["improvements"] += 1
        counts["failed"] += done.get("status") == "failed"
    evals = get_skill_eval_store(settings)
    for _ in range(limit):
        claimed = await evals.claim_queued(limit=1, now=now_ms())
        if not claimed:
            break
        done = await run_claimed_eval(settings, claimed[0], object_store=object_store)
        counts["evals"] += 1
        counts["failed"] += done.get("status") == "failed"
    return counts


__all__ = ["SKILL_JOB_BATCH", "run_skill_jobs"]
