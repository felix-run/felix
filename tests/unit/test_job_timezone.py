"""A job's cron read on the wall clock of the zone it names (`felix.jobs.schedule`).

Cron was UTC only: "08:00 every day" for someone in Phoenix was `0 15 * * *`, and in a zone with
daylight saving no expression fired at the same local time all year. The rules pinned here are
the ones a naive conversion gets wrong: a local time the spring change skips fires once, at the
first instant after the gap; one the autumn change repeats fires once, at its first occurrence;
so a daily job fires exactly once every local day -- and the caps, which sample a cron's firings,
look across those changes, where the gaps are shortest and a UTC day can hold an extra firing.

Transition dates used: America/New_York springs forward 2027-03-14 at 02:00 and falls back
2026-11-01 at 02:00; Europe/London 2027-03-28 at 01:00 GMT and 2026-10-25 at 02:00 BST.
America/Phoenix keeps no daylight saving and is the control.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from felix.config import Settings
from felix.jobs import store as jobs_store
from felix.jobs import watch
from felix.jobs.schedule import (
    TIMEZONE_NEEDS_CRON,
    ScheduleError,
    most_firings_per_utc_day,
    parse_schedule,
    shortest_gap_ms,
)
from felix.jobs.scheduler import next_run_at_ms, run_due_jobs

from tests.support.factories import make_settings

NEW_YORK, LONDON, PHOENIX = "America/New_York", "Europe/London", "America/Phoenix"


def _utc(*args: int) -> int:
    return int(datetime(*args, tzinfo=UTC).timestamp() * 1000)


def _local(ms: int, zone: str) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=ZoneInfo(zone)).isoformat()


def _firings(schedule: str, zone: str | None, start: int, end: int) -> list[int]:
    parsed = parse_schedule(schedule, zone)
    out, at = [], parsed.next_after(start - 1)
    while at is not None and at < end:
        out.append(at)
        at = parsed.next_after(at)
    return out


# --- the grammar ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("schedule", "zone", "after", "expected"),
    [
        # Spring, New York: 02:30 does not exist on 2027-03-14; it fires at 03:00 EDT, the first
        # instant after the gap, and the next day at 02:30 again.
        ("30 2 * * *", NEW_YORK, _utc(2027, 3, 13, 12), "2027-03-14T03:00:00-04:00"),
        ("30 2 * * *", NEW_YORK, _utc(2027, 3, 14, 7), "2027-03-15T02:30:00-04:00"),
        # Autumn, New York: 01:30 happens twice on 2026-11-01; it fires at the first (EDT)...
        ("30 1 * * *", NEW_YORK, _utc(2026, 10, 31, 12), "2026-11-01T01:30:00-04:00"),
        # ...and not again at the second (EST), asked from 01:15 EST -- inside the repeated hour,
        # after the first 01:30 and before the second.
        ("30 1 * * *", NEW_YORK, _utc(2026, 11, 1, 6, 15), "2026-11-02T01:30:00-05:00"),
        # Spring, London: 01:30 does not exist on 2027-03-28; 02:00 BST is the first instant after.
        ("30 1 * * *", LONDON, _utc(2027, 3, 27, 12), "2027-03-28T02:00:00+01:00"),
        # Autumn, London: 01:30 happens twice on 2026-10-25; the first is BST.
        ("30 1 * * *", LONDON, _utc(2026, 10, 24, 12), "2026-10-25T01:30:00+01:00"),
        ("30 1 * * *", LONDON, _utc(2026, 10, 25, 1, 15), "2026-10-26T01:30:00+00:00"),
        # A macro is read in the zone too.
        ("@daily", NEW_YORK, _utc(2026, 10, 30, 12), "2026-10-31T00:00:00-04:00"),
        # The control: no daylight saving, so 08:00 is 15:00 UTC on both of New York's change days.
        ("0 8 * * *", PHOENIX, _utc(2027, 3, 14, 0), "2027-03-14T08:00:00-07:00"),
        ("0 8 * * *", PHOENIX, _utc(2026, 11, 1, 0), "2026-11-01T08:00:00-07:00"),
    ],
)
def test_a_zoned_cron_fires_at_that_zones_wall_time(
    schedule: str, zone: str, after: int, expected: str
) -> None:
    assert _local(next_run_at_ms(schedule, after, zone), zone) == expected


@pytest.mark.parametrize(
    ("zone", "schedule", "first", "last"),
    [
        (NEW_YORK, "30 2 * * *", date(2027, 3, 10), date(2027, 3, 18)),  # a skipped time
        (NEW_YORK, "30 1 * * *", date(2026, 10, 28), date(2026, 11, 5)),  # a repeated time
        (LONDON, "30 1 * * *", date(2027, 3, 24), date(2027, 4, 1)),
        (LONDON, "30 1 * * *", date(2026, 10, 21), date(2026, 10, 29)),
        (PHOENIX, "30 1 * * *", date(2026, 10, 28), date(2026, 11, 5)),
    ],
)
def test_a_daily_job_fires_exactly_once_every_local_day_across_a_change(
    zone: str, schedule: str, first: date, last: date
) -> None:
    tz = ZoneInfo(zone)
    start = int(datetime(first.year, first.month, first.day, tzinfo=tz).timestamp() * 1000)
    end = int(datetime(last.year, last.month, last.day, tzinfo=tz).timestamp() * 1000)
    days = [datetime.fromtimestamp(at / 1000, tz=tz).date() for at in _firings(schedule, zone, start, end)]
    assert days == [first + timedelta(days=n) for n in range((last - first).days)]


@pytest.mark.parametrize(("day", "count"), [(date(2027, 3, 14), 23), (date(2026, 11, 1), 24)])
def test_an_hourly_cron_fires_once_per_local_hour_on_a_change_day(day: date, count: int) -> None:
    """23 local hours in the spring day, and the repeated one in the autumn fires once, not twice."""
    tz = ZoneInfo(NEW_YORK)
    start = int(datetime(day.year, day.month, day.day, tzinfo=tz).timestamp() * 1000)
    end = int(datetime.combine(day + timedelta(days=1), datetime.min.time(), tz).timestamp() * 1000)
    fired = _firings("@hourly", NEW_YORK, start, end)
    assert len(fired) == len(set(fired)) == count


@pytest.mark.parametrize("schedule", ["every:30m", "300", "at:2026-12-01T09:00:00Z", ""])
def test_a_zone_beside_a_schedule_it_cannot_move_is_refused(schedule: str) -> None:
    with pytest.raises(ScheduleError) as refused:
        parse_schedule(schedule, NEW_YORK)
    assert str(refused.value) == TIMEZONE_NEEDS_CRON
    parse_schedule(schedule)  # the same schedule, with no zone, is fine


@pytest.mark.parametrize(
    "zone",
    [
        "Mars/Olympus_Mons",
        "",
        "../etc/passwd",
        "America",
        "x" * 300,
        # Each of these loads through `ZoneInfo` somewhere -- a case-insensitive filesystem (macOS)
        # opens the lowercase file, and a tzdata tree may ship the other two -- so a stored one
        # could work where it was written and fail on the Linux worker that reads it.
        "america/new_york",
        "Factory",
        "posixrules",
        "localtime",
    ],
    ids=[
        "unknown",
        "empty",
        "a-path",
        "a-directory",
        "too-long",
        "lowercase",
        "factory",
        "posixrules",
        "localtime",
    ],
)
def test_a_name_that_is_no_zone_is_refused_in_words_that_do_not_echo_it(zone: str) -> None:
    with pytest.raises(ScheduleError) as refused:
        parse_schedule("0 8 * * *", zone)
    assert str(refused.value) == "timezone: not an IANA zone name, e.g. America/New_York or Europe/London"


# --- the caps ------------------------------------------------------------------------------------

# Late October: New York's autumn change is days away, its spring one months -- past the 200
# firings `shortest_gap_ms` walks from now for a twice-daily cron.
OCT_30 = _utc(2026, 10, 30, 12)


def test_the_shortest_gap_counts_the_one_a_spring_gap_makes() -> None:
    """`30 2,3 * * *` is an hour apart every day but one: on 2027-03-14 02:30 fires at 03:00 and
    03:30 at 03:30. A floor checked only against the next 200 firings would pass it at 45 minutes."""
    assert shortest_gap_ms(parse_schedule("30 2,3 * * *"), OCT_30) == 3_600_000
    assert shortest_gap_ms(parse_schedule("30 2,3 * * *", NEW_YORK), OCT_30) == 30 * 60_000
    assert shortest_gap_ms(parse_schedule("30 2,3 * * *", PHOENIX), OCT_30) == 3_600_000


def test_an_hourly_cron_is_never_closer_than_an_hour_across_either_change() -> None:
    assert shortest_gap_ms(parse_schedule("@hourly", NEW_YORK), OCT_30) == 3_600_000
    assert most_firings_per_utc_day(parse_schedule("@hourly", NEW_YORK), OCT_30) == 24


def test_a_daily_local_time_can_fall_twice_in_one_utc_day() -> None:
    """19:00 New York is 00:00 UTC on 2027-03-14 and, after the change that day, 23:00 UTC: two in
    one UTC day, which a `max_firings_per_day: 1` heartbeat would skip one of."""
    assert most_firings_per_utc_day(parse_schedule("0 19 * * *"), OCT_30) == 1
    assert most_firings_per_utc_day(parse_schedule("0 19 * * *", NEW_YORK), OCT_30) == 2
    assert most_firings_per_utc_day(parse_schedule("0 19 * * *", PHOENIX), OCT_30) == 1


# --- the readers of a stored job -----------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture(autouse=True)
def _clean() -> None:
    jobs_store.reset_jobs_for_tests()


async def test_the_sweep_moves_a_zoned_job_to_its_next_local_slot(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from felix.jobs import scheduler

    now = _utc(2026, 10, 30, 12)  # 08:00 EDT
    await jobs_store.put_job(settings, "default", "j", schedule="0 8 * * *", timezone=NEW_YORK)
    monkeypatch.setattr(scheduler, "now_ms", lambda: now)
    assert await run_due_jobs(settings) == 1
    job = await jobs_store.get_job(settings, "default", "j")
    assert _local(job["next_run_at"], NEW_YORK) == "2026-10-31T08:00:00-04:00"


async def test_a_stored_zone_tzdata_no_longer_has_does_not_fire_and_the_watch_names_it(
    settings: Settings,
) -> None:
    """A row whose zone no longer resolves (renamed in a tzdata update, or written by hand) is
    refused where it is read, never read in UTC; and the watch's alert classes that refusal as the
    schedule's, which it can only do by reading the job in its zone."""
    await jobs_store.put_job(settings, "default", "j", schedule="0 8 * * *", timezone=NEW_YORK)
    jobs_store._memory_jobs[("default", "j")]["timezone"] = "Mars/Olympus_Mons"

    assert await run_due_jobs(settings) == 0
    job = await jobs_store.get_job(settings, "default", "j")
    assert job["last_error"].startswith("timezone:")
    assert watch.error_class(job) == "invalid_schedule"


async def test_rewriting_only_the_zone_moves_the_due_time(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`0 8 * * *` in New York and in London are different slots: a zone change is a schedule
    change, so the job is due at whichever comes first, as it is when the expression changes."""
    now = _utc(2026, 10, 30, 6)  # 15:00 in Tokyo
    monkeypatch.setattr(jobs_store, "now_ms", lambda: now)
    await jobs_store.put_job(settings, "default", "j", schedule="0 8 * * *", timezone=NEW_YORK)
    jobs_store._memory_jobs[("default", "j")]["next_run_at"] = _utc(2026, 10, 31, 12)  # 08:00 New York
    moved = await jobs_store.put_job(settings, "default", "j", schedule="0 8 * * *", timezone="Asia/Tokyo")
    # Tokyo's next 08:00 comes first: 23:00 UTC on the 30th (08:00 UTC would be wrong too, and earlier).
    assert moved["next_run_at"] == 1_793_401_200_000


async def test_a_stored_zone_that_no_longer_resolves_leaves_the_job_overdue_and_holding_its_place(
    settings: Settings,
) -> None:
    """Never swept (no due time) when its zone stopped resolving: the sweep refuses it on every tick
    and keeps the due time it has -- none -- so the watch must count it overdue from its writing
    rather than read it as a one-shot that fired. And a task in that state still holds its place
    under `max_tasks`, as any task whose schedule no longer reads does."""
    from felix.jobs import agent_tools

    row = {"enabled": True, "schedule": "at:2026-12-01T09:00:00Z", "timezone": NEW_YORK, "created_at": 5}
    # Unreadable in its zone (a zone beside an `at:` is refused), readable without it as a fired one-shot.
    assert watch.overdue_since({**row, "next_run_at": None}, now=10**13, grace_ms=1) == 5
    assert agent_tools._is_live({**row, "next_run_at": None}) is True

    cron = {"enabled": True, "schedule": "0 8 * * *", "timezone": "Mars/Olympus_Mons", "created_at": 5}
    assert watch.overdue_since({**cron, "next_run_at": None}, now=10**13, grace_ms=1) == 5


async def test_an_empty_zone_is_no_zone(settings: Settings) -> None:
    """`""` and NULL are the same job: written as UTC, read back as None, compared equal."""
    written = await jobs_store.put_job(settings, "default", "j", schedule="0 8 * * *", timezone="")
    assert written["timezone"] is None
    assert await jobs_store.set_schedule(
        settings,
        "default",
        "j",
        schedule="0 9 * * *",
        timezone=None,
        next_run_at=1,
        expected_schedule="0 8 * * *",
        expected_timezone="",
    )
