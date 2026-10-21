"""What the model is told about the current moment.

A model knows the world only up to its training data, so without this it answers "how old am I"
against the year that data ends in. The harness tells it, in two renderings split by how often
they change — the prompt cache is a prefix match, and anything that differs between runs re-bills
everything after it:

- the date ends the system prompt, which every step of a run sees and which is the channel for
  what the harness itself asserts. It changes once a day, so it costs one cache write per
  conversation per day;
- the time goes in a transient note on the run's first model call, at the tail of the list, where
  changing every run costs nothing.

The zone is `FELIX_TIMEZONE`: an install's notion of "today", not each caller's.
"""

from __future__ import annotations

from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo

DATE_TAG = "current_date"


def zone(name: str) -> tzinfo:
    """The IANA zone `name`; raises `ZoneInfoNotFoundError` (a `KeyError`) for an unknown one."""
    return ZoneInfo(name)


def _local(tz: tzinfo, now: datetime | None) -> datetime:
    return (now or datetime.now(tz)).astimezone(tz)


def date_block(tz: tzinfo, now: datetime | None = None) -> str:
    """The date for the end of the system prompt, tagged so the model reads it as one fact."""
    local = _local(tz, now)
    today = f"{local:%A}, {local:%B} {local.day}, {local.year} ({local:%Y-%m-%d}, {tz})"
    return f"<{DATE_TAG}>Today is {today}.</{DATE_TAG}>"


def time_note(tz: tzinfo, now: datetime | None = None) -> str:
    """The time for the run's first call. The offset is in it so the model need not know the zone."""
    return f"The current time is {_local(tz, now).isoformat(timespec='minutes')} ({tz})."
