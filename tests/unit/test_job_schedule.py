"""A job's schedule is the cron the docs describe, or it is refused.

The parser this replaces read `*/N * * * *` and answered everything else -- `0 3 * * *`, the
docs' own example, `@daily`, a typo -- with "in sixty seconds". A daily job fired 1,440 times a
day and `PUT /jobs` answered 200.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from felix.config import Settings
from felix.jobs import store as jobs_store
from felix.jobs.schedule import ScheduleError, parse_schedule
from felix.jobs.scheduler import next_run_at_ms, run_due_jobs


def _ms(*args: int) -> int:
    return int(datetime(*args, tzinfo=UTC).timestamp() * 1000)


# 2026-10-10 is a Saturday.
SAT_1030 = _ms(2026, 10, 10, 10, 30)


@pytest.mark.parametrize(
    ("schedule", "expected"),
    [
        ("0 3 * * *", _ms(2026, 10, 11, 3, 0)),
        ("0 9 * * 1", _ms(2026, 10, 12, 9, 0)),
        ("0 9 * * 7", _ms(2026, 10, 11, 9, 0)),  # 7 is Sunday too
        ("*/10 * * * *", _ms(2026, 10, 10, 10, 40)),
        ("15,45 * * * *", _ms(2026, 10, 10, 10, 45)),
        ("0 9-17/4 * * *", _ms(2026, 10, 10, 13, 0)),
        ("0 0 1 * *", _ms(2026, 11, 1, 0, 0)),
        ("0 0 29 2 *", _ms(2028, 2, 29, 0, 0)),
        # Both day fields restricted: either matches (Monday the 12th beats the 15th).
        ("0 0 15 * 1", _ms(2026, 10, 12, 0, 0)),
        ("@daily", _ms(2026, 10, 11, 0, 0)),
        ("@hourly", _ms(2026, 10, 10, 11, 0)),
        ("@weekly", _ms(2026, 10, 11, 0, 0)),
        ("every:5m", SAT_1030 + 5 * 60_000),
        ("300", SAT_1030 + 300_000),
        ("", SAT_1030 + 60_000),
    ],
)
def test_the_next_firing_is_the_one_cron_would_pick(schedule: str, expected: int) -> None:
    assert next_run_at_ms(schedule, SAT_1030) == expected


def test_a_cron_firing_is_strictly_after_the_moment_it_is_asked_from() -> None:
    """The scheduler advances from the slot it just claimed; returning that slot would re-fire it."""
    assert next_run_at_ms("30 10 * * *", SAT_1030) == _ms(2026, 10, 11, 10, 30)


@pytest.mark.parametrize(
    "schedule",
    [
        "daily at 9",
        "* * * *",
        "60 * * * *",
        "0 24 * * *",
        "0 0 0 * *",
        "0 0 * 13 *",
        "0 0 * * 8",
        "*/0 * * * *",
        "5-1 * * * *",
        "0 0 30 2 *",  # parses, never fires
        "@fortnightly",
    ],
)
def test_a_schedule_outside_the_grammar_is_refused(schedule: str) -> None:
    with pytest.raises(ScheduleError):
        parse_schedule(schedule)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="memory://schedule",
        object_store="memory",
        allow_insecure=True,
        auth_mode="none",
        environment="development",
    )


@pytest.fixture(autouse=True)
def _clean() -> None:
    jobs_store._memory_jobs.clear()
    jobs_store._memory_runs.clear()


@pytest.mark.asyncio
async def test_put_job_refuses_a_schedule_it_cannot_read(settings: Settings) -> None:
    with pytest.raises(ScheduleError):
        await jobs_store.put_job(settings, "default", "j", schedule="every tuesday")
    assert await jobs_store.get_job(settings, "default", "j") is None


@pytest.mark.asyncio
async def test_a_stored_unreadable_schedule_does_not_fire_and_says_why(settings: Settings) -> None:
    """A row written before validation existed. It fired every minute; now it sits, with a reason."""
    await jobs_store.put_job(settings, "default", "j", schedule="@daily")
    jobs_store._memory_jobs[("default", "j")]["schedule"] = "every tuesday"

    assert await run_due_jobs(settings) == 0
    (job,) = await jobs_store.list_jobs(settings, "default")
    assert job["last_status"] == "error"
    assert "cron" in job["last_error"]
    assert job["next_run_at"] is None
    assert await jobs_store.list_runs(settings, "default", "j") == []
