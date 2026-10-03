"""The skill caps are exact, on both backends: requests racing at a cap get in one at a time.

Each cap used to be counted in one transaction and the row written in another, so requests at
the cap could all see room and all land -- and the draft cap, which re-counted after the write
to catch that, could refuse every racer. Now the count and the write share one transaction under
an advisory lock (`quality_store.hold_job_caps`, `feedback_lock_key`,
`library_store.pending_lock_key`), and in the twin they run with no await between them.

So: ``RACERS`` requests gathered at once against a cap of ``CAP`` land exactly ``CAP`` rows and
refuse the rest, for the tenant's queued-jobs cap, its daily cap, both together across the
evaluation and feedback tables, an agent's pending feedback, and an agent's pending drafts. On
Postgres each request is its own session on its own pooled connection, so they really
interleave. Run through the production entry points (`queue_eval`, `accept_feedback`,
`submit_feedback`, `save_draft`), not the stores, so the caps are the ones production passes.

A race test that passes by luck proves nothing, so `_widened` holds every Postgres count open
for ``WINDOW_S`` before its write. Under the lock that only queues the racers behind it; without
it every racer has counted before the first one commits, and the test fails every time rather
than most times.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from felix.skills import eval_store, evaluate, feedback, feedback_store, library, quality_store
from felix.skills.eval_store import get_skill_eval_store
from felix.skills.feedback_store import PostgresSkillFeedbackStore, get_skill_feedback_store
from felix.skills.format import serialize_skill_md
from felix.skills.job_limits import SkillJobsCapReached
from felix.skills.library_store import PostgresSkillLibraryStore, get_skill_library_store
from felix.storage import MemoryObjectStore

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("store_settings", BACKENDS, indirect=True)

TENANT, NAME = "acme", "invoice-triage"
CAP, RACERS = 3, 10
# Room enough that only the cap under test can refuse.
UNCAPPED = 1000
WINDOW_S = 0.05


@pytest.fixture(autouse=True)
def _widened(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each Postgres cap count, then a sleep, then the write it guards. The twins count
    synchronously and are left alone: there is no await to widen."""

    def then_wait(count: Any) -> Any:
        async def counted(*args: Any, **kwargs: Any) -> Any:
            out = await count(*args, **kwargs)
            await asyncio.sleep(WINDOW_S)
            return out

        return counted

    monkeypatch.setattr(eval_store, "hold_job_caps", then_wait(quality_store.hold_job_caps))
    monkeypatch.setattr(feedback_store, "hold_job_caps", then_wait(quality_store.hold_job_caps))
    monkeypatch.setattr(
        PostgresSkillFeedbackStore,
        "_pending_agent",
        staticmethod(then_wait(PostgresSkillFeedbackStore._pending_agent)),
    )
    monkeypatch.setattr(
        PostgresSkillLibraryStore, "_pending", staticmethod(then_wait(PostgresSkillLibraryStore._pending))
    )


def _capped(settings: Any, **caps: int) -> Any:
    return settings.model_copy(
        update={"skill_jobs_max_queued": UNCAPPED, "skill_jobs_daily_limit": UNCAPPED, **caps}
    )


async def _versions(settings: Any, n: int) -> list[str]:
    """``n`` versions of one skill: an evaluation is one in flight per version, so each racer
    needs its own."""
    lib = get_skill_library_store(settings)
    versions = [f"0.1.{i}" for i in range(n)]
    for i, v in enumerate(versions):
        row = {
            "name": NAME,
            "version": v,
            "status": "published",
            "source": "operator",
            "author": "ops",
            "description": "Triage invoices.",
            "security_status": "pass",
            "created_at": i + 1,
        }
        await lib.insert_version(TENANT, row, [], created_by="ops", at=i + 1)
    return versions


async def _pending_feedback(settings: Any, n: int) -> list[str]:
    who = feedback.FeedbackProvenance(source="human", author="ops")
    return [
        (await feedback.submit_feedback(settings, TENANT, name=NAME, body=f"fix {i}", provenance=who))["id"]
        for i in range(n)
    ]


def _split(results: list[Any], refusal: type[Exception]) -> tuple[int, int]:
    """(landed, refused); anything else fails the test by name."""
    others = [r for r in results if isinstance(r, BaseException) and not isinstance(r, refusal)]
    assert not others, others
    refused = sum(isinstance(r, refusal) for r in results)
    return len(results) - refused, refused


async def _jobs(settings: Any) -> int:
    return await get_skill_eval_store(settings).count_jobs_in_flight(TENANT) + await get_skill_feedback_store(
        settings
    ).count_jobs_in_flight(TENANT)


def _queue(settings: Any, version: str) -> Any:
    return evaluate.queue_eval(settings, TENANT, NAME, version, requested_by="ops")


@parametrized
async def test_racing_evaluations_fill_the_queued_cap_exactly(store_settings: Any) -> None:
    settings = _capped(store_settings, skill_jobs_max_queued=CAP)
    versions = await _versions(settings, RACERS)
    results = await asyncio.gather(*(_queue(settings, v) for v in versions), return_exceptions=True)
    assert _split(results, SkillJobsCapReached) == (CAP, RACERS - CAP)
    assert await _jobs(settings) == CAP


@parametrized
async def test_racing_evaluations_fill_the_daily_cap_exactly(store_settings: Any) -> None:
    settings = _capped(store_settings, skill_jobs_daily_limit=CAP)
    versions = await _versions(settings, RACERS)
    results = await asyncio.gather(*(_queue(settings, v) for v in versions), return_exceptions=True)
    assert _split(results, SkillJobsCapReached) == (CAP, RACERS - CAP)
    refusals = [str(r) for r in results if isinstance(r, SkillJobsCapReached)]
    assert all("today" in r for r in refusals), refusals
    assert await _jobs(settings) == CAP


@parametrized
async def test_evaluations_and_improvements_race_for_one_cap(store_settings: Any) -> None:
    """The cap is the tenant's, across both tables: half the racers queue an evaluation and half
    accept feedback with `improve`, and between them exactly ``CAP`` land."""
    settings = _capped(store_settings, skill_jobs_max_queued=CAP)
    half = RACERS // 2
    versions = await _versions(settings, half)
    feedback_ids = await _pending_feedback(settings, half)
    results = await asyncio.gather(
        *(_queue(settings, v) for v in versions),
        *(feedback.accept_feedback(settings, TENANT, f, by="ops") for f in feedback_ids),
        return_exceptions=True,
    )
    assert _split(results, SkillJobsCapReached) == (CAP, RACERS - CAP)
    assert await _jobs(settings) == CAP


@parametrized
async def test_an_agents_racing_feedback_fills_its_pending_cap_exactly(store_settings: Any) -> None:
    await _versions(store_settings, 1)
    agent = feedback.FeedbackProvenance(source="agent", author="contributor", max_pending=CAP)
    results = await asyncio.gather(
        *(
            feedback.submit_feedback(store_settings, TENANT, name=NAME, body=f"fix {i}", provenance=agent)
            for i in range(RACERS)
        ),
        return_exceptions=True,
    )
    assert _split(results, feedback.FeedbackCapReached) == (CAP, RACERS - CAP)
    assert await get_skill_feedback_store(store_settings).count_pending_agent(TENANT, "contributor") == CAP


@parametrized
async def test_an_agents_racing_drafts_fill_its_pending_cap_exactly(store_settings: Any) -> None:
    """Each racer saves a skill of its own, so nothing but the pending cap stands between them;
    a version race on one name would serialize them on the primary key instead."""
    agent = library.DraftProvenance(source="agent", author="m", origin_manifest_id="m")
    objects = MemoryObjectStore()

    def bundle(i: int) -> dict[str, str]:
        return {
            "SKILL.md": serialize_skill_md({"name": f"race-{i}", "description": "d"}, "\n# Race\n\nSteps.\n")
        }

    results = await asyncio.gather(
        *(
            library.save_draft(
                store_settings,
                TENANT,
                files=bundle(i),
                provenance=agent,
                max_pending=CAP,
                object_store=objects,
            )
            for i in range(RACERS)
        ),
        return_exceptions=True,
    )
    assert _split(results, library.SkillPendingCapReached) == (CAP, RACERS - CAP)
    assert await get_skill_library_store(store_settings).count_pending(TENANT, "m") == CAP
    # A refused save left nothing behind: no row, and no bytes.
    saved = {r["name"] for r in results if isinstance(r, dict)}
    assert {k.split("/")[2] for k in objects._data if k.startswith("skill-library/")} == saved
