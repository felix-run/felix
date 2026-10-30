"""Google Calendar: the person's events, their free time, and an event created for them.

Acts as the person through their vault (`google_common`). Creating an event changes their
calendar, so that tool is not read-only: plan mode holds it, a resumed run never repeats it, and
a manifest should put it behind an approval. It takes no guests: an invitation is mail from the
person's account, and an instruction hidden in an email or a shared Doc the agent read could
otherwise address one, with whatever else it read in the description, to anyone. The person adds
guests in Calendar. Each event's id is derived from what it is, so a call repeated after a
timeout finds the one it already made instead of making another.
"""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta
from functools import partial
from typing import Any

from felix.config import Settings
from felix.tools.types import Tool
from pydantic import AwareDatetime, Field, model_validator

from felix_connectors.google_common import AccountArgs, GoogleApis, Header, answered
from felix_connectors.http import UpstreamError
from felix_connectors.tools import connector_tool, one_line

SERVICE = "Google Calendar"
MAX_EVENTS = 25
MAX_WINDOWS = 10
READ = ("calendar_read", "calendar_events")
WRITE = ("calendar_events",)


def _when(value: Any) -> str:
    if not isinstance(value, dict):
        return "?"
    return one_line(value.get("dateTime") or value.get("date") or "?", 40)


def event_line(e: dict[str, Any]) -> str:
    title = one_line(e.get("summary") or "(no title)", 160)
    line = f"{_when(e.get('start'))} to {_when(e.get('end'))} · {title}"
    if e.get("location"):
        line += f" · at {one_line(e['location'], 120)}"
    attendees = e.get("attendees")
    if isinstance(attendees, list) and attendees:
        line += f" · {len(attendees)} attendees"
    return line + f" · id {one_line(e.get('id', ''), 64)}"


def render_events(items: list[Any]) -> str:
    lines = [f"source: {SERVICE}"]
    lines += [f"- {event_line(e)}" for e in items[:MAX_EVENTS] if isinstance(e, dict)]
    if len(lines) == 1:
        lines.append("no events")
    return "\n".join(lines)


def event_id(person: str, args: CreateEventArgs) -> str:
    """A Calendar event id (base32hex, lowercase) for this person's event of this title and time.
    The same call twice -- a retry after a timeout -- names the same event, which Google refuses
    to create again (409)."""
    key = "\x1f".join([person, args.summary, args.start.isoformat(), args.end.isoformat()])
    digest = hashlib.sha256(key.encode()).digest()
    return base64.b32hexencode(digest).decode().rstrip("=").lower()


def free_windows(
    busy: list[tuple[datetime, datetime]], start: datetime, end: datetime, minutes: int
) -> list[tuple[datetime, datetime]]:
    """The gaps of at least `minutes` in [start, end] that no busy block covers."""
    out: list[tuple[datetime, datetime]] = []
    need = timedelta(minutes=minutes)
    cursor = start
    for b_start, b_end in sorted(busy):
        if b_start >= end:
            break
        if b_start - cursor >= need:
            out.append((cursor, b_start))
        cursor = max(cursor, b_end)
    if end - cursor >= need:
        out.append((cursor, end))
    return out


def _busy(body: dict[str, Any]) -> list[tuple[datetime, datetime]]:
    primary = (body.get("calendars") or {}).get("primary") or {}
    out = []
    for block in primary.get("busy") or []:
        try:
            out.append((datetime.fromisoformat(block["start"]), datetime.fromisoformat(block["end"])))
        except KeyError, TypeError, ValueError:
            continue
    return out


class ListEventsArgs(AccountArgs):
    start: AwareDatetime | None = Field(
        default=None, description="From when (ISO 8601 with offset); now if omitted."
    )
    days: int = Field(default=7, ge=1, le=60)
    limit: int = Field(default=10, ge=1, le=MAX_EVENTS)


class FreeTimeArgs(AccountArgs):
    start: AwareDatetime
    end: AwareDatetime
    duration_minutes: int = Field(default=30, ge=5, le=480)

    @model_validator(mode="after")
    def _ordered(self) -> FreeTimeArgs:
        if not timedelta(0) < self.end - self.start <= timedelta(days=31):
            raise ValueError("end must be after start, at most 31 days later")
        return self


class CreateEventArgs(AccountArgs):
    summary: Header = Field(min_length=1, max_length=300)
    start: AwareDatetime
    end: AwareDatetime
    description: str = Field(default="", max_length=8000)
    location: Header = Field(default="", max_length=300)

    @model_validator(mode="after")
    def _ordered(self) -> CreateEventArgs:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


#: Every tool `build_tools` returns, registered by name before any is built.
TOOL_NAMES = (
    "connectors__calendar_list_events",
    "connectors__calendar_find_free_time",
    "connectors__calendar_create_event",
)


def build_tools(settings: Settings) -> list[Tool]:
    """The family's tools under `settings`; `ValueError` without the `google` extra."""
    return calendar_tools(GoogleApis.from_settings(settings))


def calendar_tools(apis: GoogleApis) -> list[Tool]:
    hosts = apis.googleapis_hosts

    def events() -> Any:
        return apis.service("calendar", "v3").events()

    async def list_events(args: ListEventsArgs) -> str:
        start = args.start or datetime.now().astimezone()
        request = events().list(
            calendarId="primary",
            timeMin=start.isoformat(),
            timeMax=(start + timedelta(days=args.days)).isoformat(),
            maxResults=args.limit,
            singleEvents=True,
            orderBy="startTime",
        )
        return render_events((await apis.json(request, READ, args.account, hosts)).get("items") or [])

    async def free_time(args: FreeTimeArgs) -> str:
        query = {
            "timeMin": args.start.isoformat(),
            "timeMax": args.end.isoformat(),
            "items": [{"id": "primary"}],
        }
        request = apis.service("calendar", "v3").freebusy().query(body=query)
        busy = _busy(await apis.json(request, READ, args.account, hosts))
        windows = free_windows(busy, args.start, args.end, args.duration_minutes)[:MAX_WINDOWS]
        zone = args.start.tzinfo
        lines = [f"source: {SERVICE}", f"free for at least {args.duration_minutes} min:"]
        lines += [
            f"- {a.astimezone(zone).isoformat()} to {b.astimezone(zone).isoformat()}" for a, b in windows
        ]
        if not windows:
            lines.append("no such window")
        return "\n".join(lines)

    async def create_event(args: CreateEventArgs) -> str:
        from felix.context import try_get_context

        ctx = try_get_context()
        event = {
            "id": event_id(ctx.auth.person if ctx is not None else "", args),
            "summary": args.summary,
            "start": {"dateTime": args.start.isoformat()},
            "end": {"dateTime": args.end.isoformat()},
            "description": args.description,
            "location": args.location,
        }
        request = events().insert(calendarId="primary", body=event, sendUpdates="none")
        try:
            made = await apis.json(request, WRITE, args.account, hosts)
        except UpstreamError as exc:
            if exc.status == 409:
                return f"source: {SERVICE}\nthis event is already in the calendar (id {event['id']})"
            raise
        return f"source: {SERVICE}\ncreated: {event_line(made)}"

    tool = partial(connector_tool, family="google_calendar", service=SERVICE, hosts=hosts)
    return [
        tool(
            name="connectors__calendar_list_events",
            args=ListEventsArgs,
            run=answered(SERVICE, list_events),
            description="The person's upcoming Google Calendar events.",
        ),
        tool(
            name="connectors__calendar_find_free_time",
            args=FreeTimeArgs,
            run=answered(SERVICE, free_time),
            description="Free windows of at least a duration in the person's Google Calendar.",
        ),
        tool(
            name="connectors__calendar_create_event",
            args=CreateEventArgs,
            run=answered(SERVICE, create_event),
            read_only=False,
            description="Create an event in the person's Google Calendar, with no guests (they add any).",
        ),
    ]


__all__ = [
    "TOOL_NAMES",
    "build_tools",
    "calendar_tools",
    "event_id",
    "event_line",
    "free_windows",
    "render_events",
]
