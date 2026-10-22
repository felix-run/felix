"""`felix/clock.py` renders the moment in the configured zone, and an unknown zone fails at boot."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from felix.clock import date_block, time_note, wall_clock
from pydantic import ValidationError

from tests.support.factories import make_settings

# 03:30 UTC on 2026-10-20 is still the 19th in Los Angeles.
NOW = datetime(2026, 10, 20, 3, 30, tzinfo=UTC)
LA = ZoneInfo("America/Los_Angeles")


def test_the_date_is_the_zones_own_day() -> None:
    assert date_block(NOW.astimezone(LA)) == (
        "<current_date>Today is Monday, October 19, 2026 (2026-10-19, America/Los_Angeles).</current_date>"
    )
    assert "Tuesday, October 20, 2026" in date_block(NOW)


def test_the_time_carries_its_offset() -> None:
    assert (
        time_note(NOW.astimezone(LA)) == "The current time is 2026-10-19T20:30-07:00 (America/Los_Angeles)."
    )


def test_the_wall_clock_reads_now_in_its_zone() -> None:
    now = wall_clock("Pacific/Kiritimati")()
    assert now.utcoffset() is not None and now.utcoffset().total_seconds() == 14 * 3600
    assert abs((now - datetime.now(UTC)).total_seconds()) < 60


def test_an_unknown_zone_is_refused_at_load() -> None:
    assert make_settings(timezone="Europe/Paris").timezone == "Europe/Paris"
    for bad in ("Mars/Olympus", "../etc/passwd", ""):
        with pytest.raises(ValidationError, match="FELIX_TIMEZONE"):
            make_settings(timezone=bad)
