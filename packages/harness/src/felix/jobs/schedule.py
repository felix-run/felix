"""The grammar a job's `schedule` is written in, and when it next fires.

The documented grammar is standard five-field cron, evaluated in UTC unless the job names a
`timezone` (below). The parser this replaces
understood only `*/N * * * *` and answered every other expression -- `0 3 * * *` included, the
docs' own example -- with "in sixty seconds", so a daily job ran 1,440 times a day and nothing
said so. An expression that does not parse is now refused where it is written, and one already
stored is refused where it is read (`scheduler.run_due_jobs`), never guessed at.

Accepted:

* empty -- manual only: the job never fires on its own, only through ``POST /jobs/{name}/run``.
  It used to mean "every 60 seconds", so a job created to be run by hand -- the route's
  `schedule` defaults to empty -- fired 1,440 times a day on its one thread, its context
  growing with every run, until someone noticed the bill.
* integer seconds -- ``300``
* an interval -- ``every:30s``, ``every:5m``, ``@every 2h``
* a macro -- ``@hourly``, ``@daily``, ``@weekly``, ``@monthly``, ``@yearly``
* five-field cron -- ``minute hour day-of-month month day-of-week``, each field ``*``, ``N``,
  ``A-B``, a ``/step`` on either, or a comma list of those. Day-of-week is 0-6 with Sunday as 0
  (7 also means Sunday). When both day fields are restricted -- neither starts with ``*`` -- a
  day matching *either* fires, as in every cron since Vixie's.
* once -- ``at:2026-10-21T09:00:00Z``, an ISO-8601 timestamp with its offset (``Z`` or
  ``+hh:mm``). It fires once, at that moment, and never again (`Schedule.at_ms`). A timestamp
  with no offset is refused rather than read as UTC or as the server's zone: the writer is often
  a model, and a guessed zone is a task that fires hours early or late with nothing saying so.

A job's `timezone` -- an IANA name, `America/New_York` -- moves a cron expression and a macro
onto that zone's wall clock: `0 8 * * *` in `America/Phoenix` is 08:00 there, every day. None is
UTC, which every job written before it existed keeps. It changes nothing else: an interval counts
from the last firing and an `at:` carries its own offset, so `parse_schedule` refuses a zone with
either rather than store one that does nothing. Across a daylight-saving change a cron fires
once per matching local time: a time the change skips fires at the first instant after the gap,
and a time it repeats fires at its first occurrence only -- so `@daily` fires exactly once every
local day, and an hourly cron fires 23 times on the spring day and 24 (not 25) on the autumn one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, tzinfo
from functools import cache
from typing import Any
from zoneinfo import ZoneInfoNotFoundError, available_timezones

from felix.clock import zone as clock_zone

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
    "a five-field cron expression, or at:<ISO-8601 timestamp with offset>"
)
_AT_PREFIX = "at:"


class ScheduleError(ValueError):
    """A schedule that does not parse, or that can never fire. Its message is written for the caller."""


TIMEZONE_NEEDS_CRON = (
    "timezone: only a cron expression or an @hourly/@daily/@weekly/@monthly/@yearly macro is read "
    "in a timezone; every: and seconds count from the last firing and at: carries its own offset, "
    "so leave timezone out"
)


_UNKNOWN_ZONE = "timezone: not an IANA zone name, e.g. America/New_York or Europe/London"
# In some tzdata trees, and loadable, but no place: `Factory` reads "-00", `posixrules` and
# `localtime` are whatever the host was built with.
_NOT_PLACES = frozenset({"Factory", "posixrules", "localtime"})


@cache
def _canonical_zones() -> frozenset[str]:
    # Scans the zone tree on disk (or the tzdata package), so once per process.
    return frozenset(available_timezones()) - _NOT_PLACES


def known_zone(name: str) -> tzinfo:
    """The IANA zone `name`, as `FELIX_TIMEZONE` is read (`felix.clock.zone`), or `ScheduleError`.

    Only a canonical key: `ZoneInfo` opens whatever file the name resolves to, so on a
    case-insensitive filesystem (macOS) `america/new_york` loads, would be stored, and then fails
    to load on a Linux worker -- a job that silently stops firing."""
    if name not in _canonical_zones():
        raise ScheduleError(_UNKNOWN_ZONE)
    try:
        return clock_zone(name)
    # `OSError`: a name too long for the filesystem the zone files are read from.
    except ZoneInfoNotFoundError, ValueError, OSError:
        raise ScheduleError(_UNKNOWN_ZONE) from None


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _wall(at_ms: int, zone: tzinfo) -> datetime:
    """The naive local wall time `zone` shows at `at_ms`."""
    return datetime.fromtimestamp(at_ms / 1000, tz=zone).replace(tzinfo=None)


def _offset_ms(at_ms: int, zone: tzinfo) -> int:
    offset = datetime.fromtimestamp(at_ms / 1000, tz=zone).utcoffset()
    return int(offset.total_seconds() * 1000) if offset is not None else 0


def _instant(wall: datetime, zone: tzinfo) -> int:
    """The moment local wall time `wall` names in `zone`: its first occurrence when the clocks go
    back over it (fold=0), and the first instant after the gap when they skip it."""
    first = wall.replace(tzinfo=zone, fold=0).astimezone(UTC)
    if _wall(_ms(first), zone) == wall:
        return _ms(first)
    # In a gap fold=0 reads `wall` with the offset from before the change, fold=1 with the one
    # after, so the change itself lies between the two readings. Bisect to it, by the second.
    lo, hi = sorted((_ms(wall.replace(tzinfo=zone, fold=1).astimezone(UTC)) // 1000, _ms(first) // 1000))
    after = _offset_ms(hi * 1000, zone)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if _offset_ms(mid * 1000, zone) == after:
            hi = mid
        else:
            lo = mid
    return hi * 1000


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

    def next_after(self, from_ms: int, zone: tzinfo = UTC) -> int | None:
        """The first matching minute strictly after `from_ms`, on `zone`'s wall clock, or None
        within the search span. The walk is over naive local wall time; each match becomes a moment
        through `_instant`, and one at or before `from_ms` -- the second pass of a repeated hour, or
        the end of a gap a match inside it already fired at -- is passed over."""
        t = _wall(from_ms, zone).replace(second=0, microsecond=0)
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
            at = _instant(t, zone)
            if at > from_ms:
                return at
            t += timedelta(minutes=1)
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


def _parse_at(text: str) -> int:
    # Upper-cased, because `parse_schedule` lowercases its input and `fromisoformat` reads `T`
    # and `Z` but not `t` and `z`. Nothing else in an ISO-8601 timestamp has a case.
    try:
        when = datetime.fromisoformat(text.upper())
    except ValueError:
        raise ScheduleError(
            "schedule: at: needs an ISO-8601 timestamp with an offset, e.g. at:2026-10-21T09:00:00Z"
        ) from None
    if when.tzinfo is None or when.utcoffset() is None:
        raise ScheduleError(
            "schedule: at: needs an offset (Z or +hh:mm); a timestamp without one names no moment"
        )
    return int(when.timestamp() * 1000)


@dataclass(frozen=True, slots=True)
class Schedule:
    """A parsed schedule. Build one with `parse_schedule`; with no field set it is manual only."""

    interval_ms: int | None = None
    cron: _Cron | None = None
    # A one-shot (`at:`): the epoch-ms moment it fires, once.
    at_ms: int | None = None
    # The IANA name a cron is read in, as the job stores it (None: UTC), and that zone. Only ever
    # set beside `cron`: `parse_schedule` refuses a zone with any other kind.
    timezone: str | None = None
    zone: tzinfo = UTC

    @property
    def one_shot(self) -> bool:
        return self.at_ms is not None

    def next_after(self, from_ms: int) -> int | None:
        """The next firing after `from_ms`, or None for a manual-only schedule, which never fires,
        and for a one-shot whose moment is not after `from_ms`."""
        if self.at_ms is not None:
            return self.at_ms if self.at_ms > from_ms else None
        if self.cron is not None:
            nxt = self.cron.next_after(from_ms, self.zone)
            if nxt is None:  # unreachable for a cron that passed `_parse_cron`
                raise ScheduleError("schedule: this cron expression never fires")
            return nxt
        if self.interval_ms is None:
            return None
        return from_ms + self.interval_ms


def parse_schedule(schedule: str, timezone: str | None = None) -> Schedule:
    """Parse `schedule`, read in `timezone` (None: UTC), raising `ScheduleError` for anything
    outside the grammar above, an unknown zone, or a zone given with a schedule it cannot move."""
    parsed = _parse(schedule)
    if timezone is None:
        return parsed
    zone = known_zone(timezone)
    if parsed.cron is None:
        raise ScheduleError(TIMEZONE_NEEDS_CRON)
    return replace(parsed, timezone=timezone, zone=zone)


def stored_zone(value: Any) -> str | None:
    """A job's zone as it is written and read: an empty one is no zone (UTC), so a row holding `""`
    and one holding NULL compare -- and parse -- the same everywhere."""
    return value if isinstance(value, str) and value else None


def job_schedule(job: dict[str, Any]) -> Schedule:
    """A stored job's schedule, in the zone it names: what every reader of a job row parses, so
    none of them reads a zoned job in UTC."""
    return parse_schedule(str(job.get("schedule") or ""), stored_zone(job.get("timezone")))


def _parse(schedule: str) -> Schedule:
    s = (schedule or "").strip().lower()
    if not s:
        return Schedule()
    if s.startswith(_AT_PREFIX):
        return Schedule(at_ms=_parse_at(s.removeprefix(_AT_PREFIX).strip()))
    if s.isdigit():
        return Schedule(interval_ms=max(int(s), 1) * 1000)
    m = _INTERVAL.match(s)
    if m:
        return Schedule(interval_ms=max(int(m.group(1)), 1) * _UNIT_MS[m.group(2)])
    return Schedule(cron=_parse_cron(_MACROS.get(s, s)))


# How many of a cron's next firings `shortest_gap_ms` walks. Enough to cross a week of a
# weekday-only schedule many times over; cheap, since each step jumps whole days or hours.
_GAP_SAMPLES = 200
_DAY_MS = 86_400_000
_DAYS_SAMPLED = 7
# How far ahead the caps look for a change of a zone's offset: a year holds both of its DST changes.
_CHANGE_HORIZON_DAYS = 400
_MINUTE_MS = 60_000


def _offset_change_days(zone: tzinfo, now: int) -> list[int]:
    """The start of each UTC day within the horizon in which `zone`'s offset changes. A cron's
    gaps and a UTC day's firings differ there from every other day -- `30 2,3 * * *` in New York
    fires 30 minutes apart on the spring day, and a daily `0 19 * * *` twice in that UTC day --
    so the caps sample those days as well as the next ones, which may hold none."""
    if zone is UTC:
        return []
    day = now - now % _DAY_MS
    found, before = [], _offset_ms(day, zone)
    for _ in range(_CHANGE_HORIZON_DAYS):
        after = _offset_ms(day + _DAY_MS, zone)
        if after != before:
            found.append(day)
        day, before = day + _DAY_MS, after
    return found


def _shortest_gap_from(parsed: Schedule, at: int | None, *, samples: int, until: int | None) -> int | None:
    """The smallest gap between consecutive firings from `at`, over `samples` of them or until
    one passes `until`."""
    shortest: int | None = None
    for _ in range(samples):
        if at is None or (until is not None and at > until):
            break
        following = parsed.next_after(at)
        if following is None:
            break
        shortest = following - at if shortest is None else min(shortest, following - at)
        if shortest <= _MINUTE_MS:  # a cron fires at most once a minute: nothing is shorter
            break
        at = following
    return shortest


def shortest_gap_ms(parsed: Schedule, now: int) -> int | None:
    """The shortest gap between firings of a recurring schedule, or None for one that does not
    recur (manual only, or a one-shot). A cron's gaps are uneven (`0 9,10 * * *`), so its next
    firings from now are walked and the smallest gap taken -- and, for a zoned cron, every firing
    across each offset change in the next year, where the gap can be shorter than on any other
    day. The floor `schedule_task` and `spec.heartbeat` each hold a recurring schedule to."""
    if parsed.one_shot:
        return None
    if parsed.interval_ms is not None:
        return parsed.interval_ms
    if parsed.cron is None:
        return None
    gaps = [_shortest_gap_from(parsed, parsed.next_after(now), samples=_GAP_SAMPLES, until=None)]
    for day in _offset_change_days(parsed.zone, now):
        window = parsed.next_after(day - 2 * _DAY_MS)
        gaps.append(_shortest_gap_from(parsed, window, samples=10 * 1440, until=day + 3 * _DAY_MS))
    found = [g for g in gaps if g is not None]
    return min(found) if found else None


def most_firings_per_utc_day(parsed: Schedule, now: int) -> int | None:
    """The most times a recurring schedule fires in one UTC day, or None for one that does not
    recur. An interval's phase follows its last firing, so it is the most any day can hold
    (`ceil(day / interval)`); a cron's days differ (`0 9 * * 1-5`), so the busiest of the next
    seven whole UTC days is taken -- and, for a zoned cron, of the UTC days around each offset
    change in the next year, where a daily local time can fall twice in one UTC day."""
    if parsed.one_shot:
        return None
    if parsed.interval_ms is not None:
        return -(-_DAY_MS // parsed.interval_ms)
    if parsed.cron is None:
        return None
    first = now - now % _DAY_MS + _DAY_MS
    days = {first + day * _DAY_MS for day in range(_DAYS_SAMPLED)}
    for change in _offset_change_days(parsed.zone, now):
        days.update((change - _DAY_MS, change, change + _DAY_MS))
    most = 0
    for start in sorted(days):
        fired, at = 0, parsed.next_after(start - 1)
        while at is not None and at < start + _DAY_MS:
            fired += 1
            at = parsed.next_after(at)
        most = max(most, fired)
    return most


__all__ = [
    "SCHEDULE_GRAMMAR",
    "TIMEZONE_NEEDS_CRON",
    "Schedule",
    "ScheduleError",
    "job_schedule",
    "known_zone",
    "most_firings_per_utc_day",
    "parse_schedule",
    "shortest_gap_ms",
    "stored_zone",
]
