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

from collections.abc import Callable
from datetime import datetime
from functools import partial
from zoneinfo import ZoneInfo

#: What a pattern is handed as `ctx["now"]`: the current moment, aware, in the install's zone.
Now = Callable[[], datetime]

DATE_TAG = "current_date"
TIME_NOTE_PREFIX = "The current time is"


def wall_clock(zone_name: str) -> Now:
    """`datetime.now` in the IANA zone `zone_name`; raises `ZoneInfoNotFoundError` for an unknown one."""
    return partial(datetime.now, ZoneInfo(zone_name))


def date_block(moment: datetime) -> str:
    """The date for the end of the system prompt, tagged so the model reads it as one fact."""
    today = f"{moment:%A}, {moment:%B} {moment.day}, {moment.year} ({moment:%Y-%m-%d}, {moment.tzinfo})"
    return f"<{DATE_TAG}>Today is {today}.</{DATE_TAG}>"


def time_note(moment: datetime) -> str:
    """The time for the run's first call. The offset is in it so the model need not know the zone."""
    return f"{TIME_NOTE_PREFIX} {moment.isoformat(timespec='minutes')} ({moment.tzinfo})."
