"""iCalendar read the same way by every calendar family: occurrences in a window, one line each.

The iCloud tools (CalDAV) and the feed tools (a published `.ics`) both hold iCalendar written by
whoever could invite the person, so both expand a repeating event only after estimating what it
costs: expansion walks a rule from its first occurrence, and a rule repeating each second since
1970 is millions of steps. One series is held to `MAX_STEPS`, and a whole calendar to a budget --
events read, steps walked, occurrences inside the window -- since a published feed is a calendar
anyone can write, and fifty thousand cheap series in a few megabytes would otherwise cost minutes
of CPU and the memory of every occurrence. What is left out is counted, and the answer says so.
Needs `icalendar` and `recurring-ical-events`, imported when used.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, tzinfo
from typing import Any

from felix_connectors.tools import one_line

#: Rule steps one series may cost to expand: years of a weekly meeting are a few hundred, a rule
#: that repeats each second is millions. A series over it is left out, and the answer says so.
MAX_STEPS = 20_000
#: One calendar's budget: the events read from it, the rule steps walked across all of them, and
#: the occurrences they may put in the window. Years of a busy calendar fit; a crafted one does not.
MAX_COMPONENTS = 20_000
MAX_TOTAL_STEPS = 500_000
MAX_IN_WINDOW = 2_000

_STEP_S = {
    "SECONDLY": 1,
    "MINUTELY": 60,
    "HOURLY": 3600,
    "DAILY": 86_400,
    "WEEKLY": 604_800,
    "MONTHLY": 28 * 86_400,
    "YEARLY": 365 * 86_400,
}
_BY = ("BYSECOND", "BYMINUTE", "BYHOUR", "BYDAY", "BYMONTHDAY", "BYYEARDAY", "BYWEEKNO", "BYMONTH")


def start_moment(value: Any, zone: tzinfo | None) -> datetime:
    """A start as a sortable aware datetime: a day at its midnight, a floating time as `zone`'s."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=zone)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=zone)
    raise ValueError("an event with no start")


def default_start() -> datetime:
    """Now, in the deployment's zone (`FELIX_TIMEZONE`): where a calendar read without a `start`
    begins, and the zone all-day and floating events are placed in. Not the server's own zone,
    which in a container is UTC -- a Phoenix evening is already tomorrow there."""
    from zoneinfo import ZoneInfo

    from felix.context import try_get_context

    ctx = try_get_context()
    zone = getattr(getattr(ctx, "settings", None), "timezone", "") or "UTC"
    return datetime.now(ZoneInfo(zone))


def expansion_steps(event: Any, end: datetime, start: datetime | None = None) -> float:
    """An upper bound on the rule steps expanding `event` up to `end` takes: from the series'
    first occurrence -- or from `start`, to bound the occurrences that land in [start, end) --
    one per FREQ interval, times every BY-list's length (each can multiply what one step yields).
    For an event with no rule, 1 -- with `start`, 1 only if it overlaps [start, end)."""
    rules = event.get("RRULE")
    first = start_moment(event["DTSTART"].dt, UTC)
    if rules is None:
        if start is None:
            return 1
        # A one-off lands in the window when it overlaps it: years of past events cost nothing.
        last = start_moment(event["DTEND"].dt, UTC) if event.get("DTEND") else first
        return 1 if first < end and max(first, last) >= start else 0
    span = max((end - max(first, start) if start is not None else end - first).total_seconds(), 0.0)
    if span == 0:
        return 0 if start is not None else 1
    worst = 1.0
    for rule in rules if isinstance(rules, list) else [rules]:
        step = _STEP_S.get(str((rule.get("FREQ") or ["SECONDLY"])[0]).upper(), 1)
        steps = span / (step * max(int((rule.get("INTERVAL") or [1])[0]), 1)) + 1
        for part in _BY:
            steps *= max(len(rule.get(part) or ()), 1)
        worst = max(worst, steps)
    return worst


def occurrences(ics: str, start: datetime, end: datetime) -> tuple[list[Any], int]:
    """(each occurrence in [start, end) of the events `ics` holds, how many events were left out:
    a series too costly to expand, or past the calendar's budget). An event with no start is no
    event and is skipped. CPU only -- run it off the event loop."""
    import icalendar
    import recurring_ical_events

    cal = icalendar.Calendar.from_ical(ics)
    kept, steps, in_window, left_out = 0, 0.0, 0.0, 0
    for component in list(cal.walk("VEVENT")):
        if "DTSTART" not in component:
            cal.subcomponents.remove(component)
            continue
        cost = expansion_steps(component, end)
        landing = expansion_steps(component, end, start)
        over = kept >= MAX_COMPONENTS or steps + cost > MAX_TOTAL_STEPS or in_window + landing > MAX_IN_WINDOW
        if cost > MAX_STEPS or over:
            cal.subcomponents.remove(component)
            left_out += 1
            continue
        kept, steps, in_window = kept + 1, steps + cost, in_window + landing
    return list(recurring_ical_events.of(cal).between(start, end)), left_out


def left_out_note(count: int) -> str:
    return f"({count} events left out: they repeat too often, or the calendar holds too many, to list)"


def _shown(value: Any, zone: tzinfo | None) -> str:
    if isinstance(value, datetime):
        return (value.astimezone(zone) if value.tzinfo else value).isoformat()
    return value.isoformat() if isinstance(value, date) else "?"


def event_line(event: Any, calendar: str, zone: tzinfo | None) -> str:
    start, end = event.get("DTSTART"), event.get("DTEND")
    line = (
        f"{_shown(start.dt if start else None, zone)} to {_shown(end.dt if end else None, zone)} · "
        f"{one_line(str(event.get('SUMMARY') or '(no title)'), 160)}"
    )
    if event.get("LOCATION"):
        line += f" · at {one_line(str(event['LOCATION']), 120)}"
    if calendar:
        line += f" · calendar {one_line(calendar, 80)}"
    return line + f" · uid {one_line(str(event.get('UID') or ''), 128)}"


__all__ = [
    "MAX_IN_WINDOW",
    "MAX_STEPS",
    "MAX_TOTAL_STEPS",
    "default_start",
    "event_line",
    "expansion_steps",
    "left_out_note",
    "occurrences",
    "start_moment",
]
