"""Time tools: the current time anywhere, a wall time in other zones, and calendar arithmetic.

The prompt already carries today's date and the run's start time (`felix.clock`), in the install's
zone (`FELIX_TIMEZONE`). That answers "what day is it", not "what time is it in Tokyo", "is 9am in
New York after my 2pm here", or "how many weekdays until the 14th" -- questions a model gets wrong
by doing time-zone and calendar arithmetic in its head, across a daylight-saving change most of all.
These do it with the IANA database the process already has.

Local, read-only and dependency-free: what they answer is computed, not fetched, so they are not
screened as untrusted. A zone is a name the model writes, so it is checked against a plain IANA
shape before `ZoneInfo` sees it.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

from felix.tools.errors import ToolErrorCode, tool_error_output
from felix.tools.types import Tool, ToolOutput, define_tool

TIME_NOW, TIME_CONVERT, DATE_MATH = "time_now", "time_convert", "date_math"
TOOL_NAMES = (TIME_NOW, TIME_CONVERT, DATE_MATH)
MAX_ZONES = 10
#: An IANA name (`America/Argentina/Buenos_Aires`, `Etc/GMT+5`, `UTC`): no dots, so no `..`.
_ZONE = re.compile(r"[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+){0,2}", re.ASCII)
#: `date_math` spans: past this a weekday count is no question anyone asks, and a year 10000 overflows.
MAX_SPAN_DAYS = 366 * 200


class BadInput(ValueError):
    """An argument the model can correct; its message, written here, is what it is told."""


def parse_zone(name: str) -> tzinfo:
    """The IANA zone `name`, or `BadInput`. The shape check comes first, so a name that is a path
    never reaches `ZoneInfo`, which would refuse it too -- this is the line that does not depend
    on that."""
    if len(name) > 64 or not _ZONE.fullmatch(name):
        raise BadInput(f"{name[:64]!r} is not an IANA time zone name, e.g. Europe/Paris")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError, ValueError:
        raise BadInput(f"unknown time zone {name!r}; use an IANA name, e.g. America/New_York") from None


def _offset(moment: datetime) -> str:
    off = moment.utcoffset() or timedelta(0)
    sign = "-" if off < timedelta(0) else "+"
    minutes = abs(int(off.total_seconds())) // 60
    return f"UTC{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def describe(moment: datetime, name: str = "") -> str:
    """`2026-10-30 06:34 Friday · America/Phoenix · UTC-07:00 (MST)`; without a zone name (a
    moment given by its offset), `2026-10-30 06:34 Friday · UTC-07:00`."""
    abbrev = moment.tzname() or ""
    # A zone with no abbreviation of its own reports its offset (`-03`), and a bare offset names
    # itself `UTC-04:00`: both already shown.
    tail = f" ({abbrev})" if abbrev and not abbrev.startswith(("+", "-", "UTC")) else ""
    where = f" · {name}" if name else ""
    return f"{moment:%Y-%m-%d %H:%M} {moment:%A}{where} · {_offset(moment)}{tail}"


def resolve_wall(naive: datetime, tz: tzinfo) -> tuple[datetime, str]:
    """`naive` read as a wall time in `tz`, and a note when the zone makes that reading unsure:
    a time inside a spring-forward gap (it never happens; read as the moment after the gap) or
    inside a fall-back hour (it happens twice; the first is used)."""
    first, second = naive.replace(tzinfo=tz, fold=0), naive.replace(tzinfo=tz, fold=1)
    if first.utcoffset() == second.utcoffset():
        return first, ""
    # A wall time that round-trips through UTC exists; one that does not is in a gap.
    if first.astimezone(UTC).astimezone(tz).replace(tzinfo=None) == naive:
        later = second.astimezone(UTC).astimezone(tz)
        return first, (
            f"{naive:%Y-%m-%d %H:%M} happens twice in this zone (clocks go back); the first was "
            f"used, the second is {_offset(later)}"
        )
    # In a gap, fold=0 reads the wall time with the offset before the change: the moment after it.
    moved = first.astimezone(UTC).astimezone(tz)
    return moved, (
        f"{naive:%Y-%m-%d %H:%M} does not exist in this zone (clocks go forward); read as {moved:%H:%M}"
    )


def _parse_iso(text: str) -> date | datetime:
    """A date (`2026-11-14`, `20261114`) or a date-time, by what it parses as rather than its length."""
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        raise BadInput(f"{text[:40]!r} is not an ISO 8601 date (2026-11-14) or date-time") from None


def add_months(day: date, months: int) -> date:
    """`day` moved by whole months, its day of the month clamped: Jan 31 + 1 month is Feb 28 (29)."""
    year, month0 = divmod(day.month - 1 + months, 12)
    year += day.year
    if not 1 <= year <= 9999:
        raise BadInput("that lands outside years 1 to 9999")
    return day.replace(
        year=year, month=month0 + 1, day=min(day.day, calendar.monthrange(year, month0 + 1)[1])
    )


def weekdays_between(start: date, end: date) -> int:
    """Monday-to-Friday days in [start, end)."""
    days = (end - start).days
    full, rest = divmod(days, 7)
    return full * 5 + sum(1 for i in range(rest) if (start.weekday() + i) % 7 < 5)


def _count(n: int, unit: str) -> str:
    return f"{n} {unit}{'' if n == 1 else 's'}"


def _phrase(parts: list[str]) -> str:
    if not parts:
        return "0 days"
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def calendar_difference(start: date, end: date) -> str:
    """How far `end` is from `start` in years, months and days, counted from `start` -- so it is
    what `add` would take to get there: Mar 31 to Feb 28 is 1 month back, as Mar 31 - 1 month is
    Feb 28. Unsigned; the caller says which way."""
    step = 1 if end >= start else -1
    months = (end.year - start.year) * 12 + end.month - start.month
    # The most whole months from `start` that do not pass `end`.
    while months and (add_months(start, months) - end).days * step > 0:
        months -= step
    days = abs((end - add_months(start, months)).days)
    years, months = divmod(abs(months), 12)
    return _phrase([_count(n, u) for n, u in ((years, "year"), (months, "month"), (days, "day")) if n])


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TimeNowArgs(_Args):
    zones: list[str] = Field(
        default_factory=list,
        max_length=MAX_ZONES,
        description="IANA time zones, e.g. ['Asia/Tokyo', 'Europe/London']; this deployment's zone if empty.",
    )


class TimeConvertArgs(_Args):
    time: str = Field(
        min_length=5,
        max_length=40,
        description="An ISO 8601 date-time, e.g. 2026-11-14T09:00. With an offset it is that moment; "
        "without one it is a wall time in from_zone.",
    )
    from_zone: str | None = Field(
        default=None,
        description="The zone a time without an offset is in; this deployment's zone if omitted.",
    )
    to_zones: list[str] = Field(min_length=1, max_length=MAX_ZONES, description="IANA zones to show it in.")


class Shift(_Args):
    years: int = Field(default=0, ge=-200, le=200)
    months: int = Field(default=0, ge=-2400, le=2400)
    weeks: int = Field(default=0, ge=-10_000, le=10_000)
    days: int = Field(default=0, ge=-MAX_SPAN_DAYS, le=MAX_SPAN_DAYS)
    hours: int = Field(default=0, ge=-1_000_000, le=1_000_000)
    minutes: int = Field(default=0, ge=-10_000_000, le=10_000_000)


class DateMathArgs(_Args):
    start: str = Field(min_length=8, max_length=40, description="An ISO 8601 date or date-time.")
    op: Literal["add", "between"] = Field(
        description="`add`: move start by `add`. `between`: how far `end` is from start."
    )
    add: Shift | None = Field(default=None, description="For `add`: the amounts, each may be negative.")
    end: str | None = Field(
        default=None, max_length=40, description="For `between`: the other date or date-time."
    )
    zone: str | None = Field(
        default=None,
        description="The zone whose wall clock days, weeks and months move: a date-time without an "
        "offset is read in it, and one with an offset is moved into it. This deployment's zone "
        "if omitted, for a date-time without an offset; one with an offset then keeps it.",
    )

    @model_validator(mode="after")
    def _op_has_its_argument(self) -> DateMathArgs:
        if self.op == "add" and self.add is None:
            raise ValueError("`add` needs the amounts in `add`")
        if self.op == "between" and not self.end:
            raise ValueError("`between` needs `end`")
        return self


def time_now(args: TimeNowArgs, *, default_zone: str, now: datetime | None = None) -> str:
    moment = now or datetime.now(UTC)
    names = args.zones or [default_zone]
    return "\n".join(describe(moment.astimezone(parse_zone(n)), n) for n in names)


def time_convert(args: TimeConvertArgs, *, default_zone: str) -> str:
    when = _parse_iso(args.time)
    if not isinstance(when, datetime):
        raise BadInput("time needs a time of day, e.g. 2026-11-14T09:00")
    note = ""
    if when.tzinfo is None:
        source_name = args.from_zone or default_zone
        when, note = resolve_wall(when, parse_zone(source_name))
        lines = [f"from: {describe(when, source_name)}"]
    else:
        lines = [f"from: {describe(when)}"]
        if args.from_zone:
            note = "from_zone was not used: the time carries its own offset"
    if note:
        lines.append(f"note: {note}")
    lines += [f"- {describe(when.astimezone(parse_zone(n)), n)}" for n in args.to_zones]
    return "\n".join(lines)


def _on_wall(start: datetime, zone: str | None, default_zone: str) -> tuple[datetime, tzinfo, str]:
    """(the wall time to move, the zone whose clock it is on, that zone's name or "" for a bare
    offset). An offset is a moment, so it is moved into an explicit `zone`; without one it keeps
    its offset, the only clock it names."""
    if start.tzinfo is None:
        name = zone or default_zone
        return start, parse_zone(name), name
    if zone:
        tz = parse_zone(zone)
        return start.astimezone(tz).replace(tzinfo=None), tz, zone
    return start.replace(tzinfo=None), start.tzinfo, ""


def _shifted(start: date | datetime, by: Shift, zone: str | None, default_zone: str) -> list[str]:
    months = by.years * 12 + by.months
    span = timedelta(weeks=by.weeks, days=by.days)
    if isinstance(start, date) and not isinstance(start, datetime):
        if by.hours or by.minutes:
            raise BadInput("a date has no time of day: add hours or minutes to a date-time")
        landed_day = add_months(start, months) + span
        return [f"result: {landed_day.isoformat()} {landed_day:%A}"]
    wall, tz, name = _on_wall(start, zone, default_zone)
    # Calendar units move the wall clock (9:00 a week later is 9:00, across a DST change), so the
    # wall time is moved first and read in the zone once; hours and minutes are elapsed time.
    moved = datetime.combine(add_months(wall.date(), months) + span, wall.time())
    landed, note = resolve_wall(moved, tz)
    if by.hours or by.minutes:
        landed = (landed.astimezone(UTC) + timedelta(hours=by.hours, minutes=by.minutes)).astimezone(tz)
    lines = [f"result: {describe(landed, name)}"]
    if note:
        lines.append(f"note: {note}")
    return lines


def _direction(later: bool) -> str:
    return "after start" if later else "before start"


def _between(start: date | datetime, end: date | datetime, zone: str | None, default_zone: str) -> list[str]:
    if isinstance(start, datetime) != isinstance(end, datetime):
        raise BadInput("start and end must both be dates or both be date-times")
    if isinstance(start, datetime) and isinstance(end, datetime):
        tz = parse_zone(zone or default_zone)
        a, a_note = (start, "") if start.tzinfo else resolve_wall(start, tz)
        b, b_note = (end, "") if end.tzinfo else resolve_wall(end, tz)
        # In UTC: two datetimes sharing a tzinfo subtract as wall times, which loses a DST hour.
        delta = b.astimezone(UTC) - a.astimezone(UTC)
        seconds = abs(int(delta.total_seconds()))
        days, rest = divmod(seconds, 86_400)
        hours, minutes = rest // 3600, rest % 3600 // 60
        sign = "-" if delta < timedelta(0) else ""
        lines = [
            f"elapsed: end is {_count(days, 'day')} {_count(hours, 'hour')} {_count(minutes, 'minute')} "
            f"{_direction(delta >= timedelta(0))}",
            f"total: {sign}{seconds / 3600:g} hours",
        ]
        return lines + [f"note: {n}" for n in (a_note, b_note) if n]
    later = end >= start
    lo, hi = (start, end) if later else (end, start)
    if (hi - lo).days > MAX_SPAN_DAYS:
        raise BadInput(f"spans longer than {MAX_SPAN_DAYS} days are not counted")
    days = (hi - lo).days
    weeks = f"{_count(days // 7, 'week')} and {_count(days % 7, 'day')}"
    return [
        f"days: {'' if later else '-'}{days} (end is {weeks} {_direction(later)})",
        f"calendar: end is {calendar_difference(start, end)} {_direction(later)}",
        f"weekdays (Mon-Fri, counting {lo.isoformat()}, not {hi.isoformat()}): {weekdays_between(lo, hi)}",
        f"{start.isoformat()} is a {start:%A}; {end.isoformat()} is a {end:%A}",
    ]


def date_math(args: DateMathArgs, *, default_zone: str) -> str:
    start = _parse_iso(args.start)
    if args.op == "add":
        assert args.add is not None
        return "\n".join(_shifted(start, args.add, args.zone, default_zone))
    assert args.end is not None
    return "\n".join(_between(start, _parse_iso(args.end), args.zone, default_zone))


def _handler(fn: Callable[..., str], default_zone: str) -> Callable[[Any], Awaitable[ToolOutput]]:
    async def handle(args: Any) -> ToolOutput:
        try:
            return fn(args, default_zone=default_zone)
        except BadInput as exc:
            return tool_error_output(ToolErrorCode.INVALID_ARGUMENTS, str(exc))
        except OverflowError:
            return tool_error_output(ToolErrorCode.INVALID_ARGUMENTS, "that lands outside years 1 to 9999")

    return handle


def time_tools(default_zone: str) -> list[Tool]:
    """The three tools, a zone left out meaning `default_zone` (the install's `FELIX_TIMEZONE`)."""
    return [
        define_tool(
            name=TIME_NOW,
            description=(
                "The current date and time in one or more IANA time zones, with each zone's UTC offset."
            ),
            args=TimeNowArgs,
            handler=_handler(time_now, default_zone),
            replay_safe=True,
            read_only=True,
        ),
        define_tool(
            name=TIME_CONVERT,
            description=(
                "Convert a date-time from one time zone to others, accounting for daylight saving; "
                "says when a wall time is skipped or repeated by a clock change."
            ),
            args=TimeConvertArgs,
            handler=_handler(time_convert, default_zone),
            replay_safe=True,
            read_only=True,
        ),
        define_tool(
            name=DATE_MATH,
            description=(
                "Calendar arithmetic: add years, months, weeks, days, hours or minutes to a date or "
                "date-time, or count the days, calendar difference and weekdays between two dates."
            ),
            args=DateMathArgs,
            handler=_handler(date_math, default_zone),
            replay_safe=True,
            read_only=True,
        ),
    ]


__all__ = [
    "DATE_MATH",
    "TIME_CONVERT",
    "TIME_NOW",
    "TOOL_NAMES",
    "BadInput",
    "add_months",
    "calendar_difference",
    "date_math",
    "describe",
    "parse_zone",
    "resolve_wall",
    "time_convert",
    "time_now",
    "time_tools",
    "weekdays_between",
]
