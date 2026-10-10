"""The grammar a job's `schedule` is written in, and when it next fires.

The documented grammar is standard five-field cron, evaluated in UTC. The parser this replaces
understood only `*/N * * * *` and answered every other expression -- `0 3 * * *` included, the
docs' own example -- with "in sixty seconds", so a daily job ran 1,440 times a day and nothing
said so. An expression that does not parse is now refused where it is written, and one already
stored is refused where it is read (`scheduler.run_due_jobs`), never guessed at.

Accepted:

* empty -- every 60 seconds (the default an unscheduled job has always had)
* integer seconds -- ``300``
* an interval -- ``every:30s``, ``every:5m``, ``@every 2h``
* a macro -- ``@hourly``, ``@daily``, ``@weekly``, ``@monthly``, ``@yearly``
* five-field cron -- ``minute hour day-of-month month day-of-week``, each field ``*``, ``N``,
  ``A-B``, a ``/step`` on either, or a comma list of those. Day-of-week is 0-6 with Sunday as 0
  (7 also means Sunday). When both day fields are restricted -- neither starts with ``*`` -- a
  day matching *either* fires, as in every cron since Vixie's.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

DEFAULT_INTERVAL_MS = 60_000

_INTERVAL = re.compile(r"^@?every[:\s]+(\d+)\s*([smh])$")
_UNIT_MS = {"s": 1_000, "m": 60_000, "h": 3_600_000}
_MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}
# (low, high) per field, in cron order.
_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_FIELD_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")
# Long enough to reach the next 29 February from any start, including across a skipped
# century leap year (2096 -> 2104); a cron that matches nothing in that span matches nothing.
_SEARCH_DAYS = 366 * 9

SCHEDULE_GRAMMAR = (
    "expected seconds, every:<n><s|m|h>, @hourly/@daily/@weekly/@monthly/@yearly, "
    "or a five-field cron expression"
)


class ScheduleError(ValueError):
    """A schedule that does not parse, or that can never fire. Its message is written for the caller."""


@dataclass(frozen=True, slots=True)
class _Cron:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0-6, Sunday = 0
    days_restricted: bool
    weekdays_restricted: bool

    def _day_matches(self, d: datetime) -> bool:
        weekday = (d.weekday() + 1) % 7  # Python's Monday = 0 -> cron's Sunday = 0
        in_days = d.day in self.days
        in_weekdays = weekday in self.weekdays
        if self.days_restricted and self.weekdays_restricted:
            return in_days or in_weekdays
        return in_days and in_weekdays

    def next_after(self, from_ms: int) -> int | None:
        """The first matching minute strictly after `from_ms`, or None within the search span."""
        t = datetime.fromtimestamp(from_ms / 1000, tz=UTC).replace(second=0, microsecond=0)
        t += timedelta(minutes=1)
        end = t + timedelta(days=_SEARCH_DAYS)
        while t < end:
            if t.month not in self.months or not self._day_matches(t):
                t = (t + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if t.hour not in self.hours:
                t = (t + timedelta(hours=1)).replace(minute=0)
                continue
            if t.minute not in self.minutes:
                t += timedelta(minutes=1)
                continue
            return int(t.timestamp() * 1000)
        return None


def _parse_field(text: str, index: int) -> frozenset[int]:
    low, high = _BOUNDS[index]
    name = _FIELD_NAMES[index]
    values: set[int] = set()
    for part in text.split(","):
        m = re.fullmatch(r"(\*|\d+(?:-\d+)?)(?:/(\d+))?", part)
        if m is None:
            raise ScheduleError(f"schedule: cannot read the {name} field; {SCHEDULE_GRAMMAR}")
        span, step_text = m.group(1), m.group(2)
        if span == "*":
            start, stop = low, high
        elif "-" in span:
            a, b = span.split("-")
            start, stop = int(a), int(b)
        else:
            start = int(span)
            # `5/15` means "from 5, every 15", as in Vixie cron.
            stop = high if step_text else start
        step = int(step_text) if step_text else 1
        if not (low <= start <= stop <= high) or step < 1:
            raise ScheduleError(f"schedule: the {name} field must stay within {low}-{high}")
        values.update(range(start, stop + 1, step))
    return frozenset(values)


def _parse_cron(text: str) -> _Cron:
    fields = text.split()
    if len(fields) != 5:
        raise ScheduleError(f"schedule: {SCHEDULE_GRAMMAR}")
    minutes, hours, days, months, weekdays = (_parse_field(f, i) for i, f in enumerate(fields))
    cron = _Cron(
        minutes=minutes,
        hours=hours,
        days=days,
        months=months,
        weekdays=frozenset(d % 7 for d in weekdays),
        # Vixie's rule: a day field starting with `*` (`*/2` included) is unrestricted, so
        # `0 0 */2 * 1` means odd days that are Mondays, not odd days or Mondays.
        days_restricted=not fields[2].startswith("*"),
        weekdays_restricted=not fields[4].startswith("*"),
    )
    # `0 0 30 2 *` parses and never fires. Refuse it here rather than store a job that sits
    # enabled forever.
    if cron.next_after(0) is None:
        raise ScheduleError("schedule: this cron expression never fires")
    return cron


@dataclass(frozen=True, slots=True)
class Schedule:
    """A parsed schedule. Build one with `parse_schedule`."""

    interval_ms: int | None = None
    cron: _Cron | None = None

    def next_after(self, from_ms: int) -> int:
        if self.cron is not None:
            nxt = self.cron.next_after(from_ms)
            if nxt is None:  # unreachable for a cron that passed `_parse_cron`
                raise ScheduleError("schedule: this cron expression never fires")
            return nxt
        return from_ms + (self.interval_ms or DEFAULT_INTERVAL_MS)


def parse_schedule(schedule: str) -> Schedule:
    """Parse `schedule`, raising `ScheduleError` for anything outside the grammar above."""
    s = (schedule or "").strip().lower()
    if not s:
        return Schedule(interval_ms=DEFAULT_INTERVAL_MS)
    if s.isdigit():
        return Schedule(interval_ms=max(int(s), 1) * 1000)
    m = _INTERVAL.match(s)
    if m:
        return Schedule(interval_ms=max(int(m.group(1)), 1) * _UNIT_MS[m.group(2)])
    return Schedule(cron=_parse_cron(_MACROS.get(s, s)))


__all__ = ["SCHEDULE_GRAMMAR", "Schedule", "ScheduleError", "parse_schedule"]
