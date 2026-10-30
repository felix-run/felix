"""iCloud Calendar and Contacts: the person's events, an event added for them, their contacts.

Acts as the person with the Apple ID and app-specific password they connected
(`POST /connections/icloud`, kept in their vault), over CalDAV and CardDAV (`icloud_dav`).
Recurring events are expanded here, from the series the calendar holds, so a weekly meeting shows
on each day it falls -- after a series' cost is estimated, because expansion walks a rule from its
first occurrence and anyone who can invite the person can put a rule on their calendar that
repeats every second since 1970. Reminders are not offered: Apple stopped serving upgraded
Reminders lists (iOS 13 and later) over CalDAV, so a tool for them would find nothing.

Reading covers every calendar the person sees, shared ones included. Adding an event writes only
to a calendar that is the person's own and writable: a calendar someone shared with them is one
that person reads, and an instruction hidden in one of its events could otherwise have the agent
copy what else it read into it. Adding changes the person's calendar, so that tool is not
read-only: plan mode holds it, a resumed run never repeats it, and a manifest should put it behind
an approval. It takes no guests -- an invitation is mail from the person's account -- and its UID
is derived from what the event is, so a call repeated after a timeout finds the one it already made
(412) instead of adding another. What a calendar and an address book hold was written by anyone who
could invite or email the person: every answer is untrusted text, to be screened.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any
from xml.sax.saxutils import escape

from felix.config import Settings
from felix.tools.types import Tool
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from felix_connectors.http import UpstreamError
from felix_connectors.ical import default_start, event_line, left_out_note, occurrences, start_moment
from felix_connectors.icloud_dav import (
    CALDAV,
    CARDDAV,
    DAV,
    PROVIDER,
    SOURCE,
    Dav,
    basic,
    multistatus,
    require_parsers,
)
from felix_connectors.person import answered, event_key, person_connection
from felix_connectors.tools import Header, connector_tool, one_line

logger = logging.getLogger("felix_connectors.icloud")

CALENDAR = "iCloud Calendar"
CONTACTS = "iCloud Contacts"
MAX_CALENDARS = 20
MAX_BOOKS = 5
MAX_EVENTS = 25
MAX_CONTACTS = 10
PRODID = "-//Felix//felix-connectors//EN"
CS = "http://calendarserver.org/ns/"

_CALENDARS = f"""<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="{CALDAV}"><d:prop>
<d:resourcetype/><d:displayname/><d:current-user-privilege-set/><c:supported-calendar-component-set/>
</d:prop></d:propfind>""".encode()
_BOOKS = b"""<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:displayname/></d:prop></d:propfind>"""
_WRITE = {f"{{{DAV}}}write", f"{{{DAV}}}write-content", f"{{{DAV}}}all"}


class ICloudArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account: str | None = Field(
        default=None, max_length=320, description="Which connected Apple ID, if several."
    )


class ListEventsArgs(ICloudArgs):
    start: AwareDatetime | None = Field(
        default=None, description="From when (ISO 8601 with offset); now if omitted."
    )
    days: int = Field(default=7, ge=1, le=60)
    limit: int = Field(default=10, ge=1, le=MAX_EVENTS)


class CreateEventArgs(ICloudArgs):
    summary: Header = Field(min_length=1, max_length=300)
    start: AwareDatetime
    end: AwareDatetime
    description: str = Field(default="", max_length=8000)
    location: Header = Field(default="", max_length=300)
    calendar: Header | None = Field(
        default=None, max_length=200, description="The calendar's name; the person's first one if omitted."
    )

    @model_validator(mode="after")
    def _ordered(self) -> CreateEventArgs:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class SearchContactsArgs(ICloudArgs):
    query: Header = Field(min_length=1, max_length=100, description="Part of a name, email or phone number.")
    limit: int = Field(default=5, ge=1, le=MAX_CONTACTS)


@dataclass(frozen=True, slots=True)
class Collection:
    """One calendar or address book the person sees, and what they may do with it."""

    url: str
    name: str
    components: frozenset[str] = frozenset()
    writable: bool = False
    shared: bool = False


async def credential(account: str | None) -> tuple[str, str]:
    """(person, Basic credential) for the person's connected Apple ID, from their vault."""
    from felix.auth import connections

    found = await person_connection(PROVIDER, SOURCE, account)
    return found.person, basic(found.account, await found.opened(connections.secret))


# --- collections ------------------------------------------------------------------------------


def _text(found: Any, tag: str) -> str:
    prop = found.props.get(tag)
    return (prop.text or "").strip() if prop is not None else ""


async def calendars(dav: Dav, auth: str) -> list[Collection]:
    """The person's calendars, theirs and shared with them alike."""
    out = []
    for f in await dav.propfind(await dav.calendar_home(auth), auth, _CALENDARS, "1"):
        kind = f.props.get(f"{{{DAV}}}resourcetype")
        if kind is None or kind.find(f"{{{CALDAV}}}calendar") is None:
            continue
        comps = f.props.get(f"{{{CALDAV}}}supported-calendar-component-set")
        # A calendar that does not say what it holds may hold anything (RFC 4791 5.2.3).
        names = (
            frozenset(c.get("name", "") for c in comps)
            if comps is not None
            else frozenset({"VEVENT", "VTODO"})
        )
        privileges = f.props.get(f"{{{DAV}}}current-user-privilege-set")
        held = {p.tag for p in privileges.iter()} if privileges is not None else set()
        # A calendar someone else shared with the person is marked `shared` (CalendarServer);
        # one the person shared themselves is `shared-owner`, and is theirs.
        shared = kind.find(f"{{{CS}}}shared") is not None
        out.append(Collection(f.href, _text(f, f"{{{DAV}}}displayname"), names, bool(held & _WRITE), shared))
    return out[:MAX_CALENDARS]


async def address_books(dav: Dav, auth: str) -> list[Collection]:
    out = []
    for f in await dav.propfind(await dav.addressbook_home(auth), auth, _BOOKS, "1"):
        kind = f.props.get(f"{{{DAV}}}resourcetype")
        if kind is not None and kind.find(f"{{{CARDDAV}}}addressbook") is not None:
            out.append(Collection(f.href, _text(f, f"{{{DAV}}}displayname")))
    return out[:MAX_BOOKS]


# --- events -----------------------------------------------------------------------------------


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def events_query(start: datetime, end: datetime) -> bytes:
    """The events overlapping [start, end): the server finds them, a series' master included."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="{CALDAV}">
<d:prop><c:calendar-data/></d:prop>
<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">
<c:time-range start="{_utc(start)}" end="{_utc(end)}"/>
</c:comp-filter></c:comp-filter></c:filter>
</c:calendar-query>""".encode()


def event_uid(person: str, args: CreateEventArgs) -> str:
    """The UID (and file name) of this person's event of this title and time (`event_key`)."""
    return f"felix-{event_key(person, args.summary, args.start, args.end).hex()[:40]}"


def event_ics(uid: str, args: CreateEventArgs, now: datetime) -> bytes:
    """The event as iCalendar: no ORGANIZER and no ATTENDEE, so nothing is ever mailed."""
    import icalendar

    cal = icalendar.Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    event = icalendar.Event()
    event.add("uid", uid)
    event.add("dtstamp", now.astimezone(UTC))
    # In UTC: an offset is no time zone, and icalendar would write one as `TZID=-07:00`, an id
    # with no VTIMEZONE behind it that a calendar reads as floating time or refuses.
    event.add("dtstart", args.start.astimezone(UTC))
    event.add("dtend", args.end.astimezone(UTC))
    event.add("summary", args.summary)
    if args.description:
        event.add("description", args.description)
    if args.location:
        event.add("location", args.location)
    cal.add_component(event)
    return cal.to_ical()


async def _calendar_events(
    dav: Dav, auth: str, cal: Collection, start: datetime, end: datetime
) -> tuple[list[tuple[datetime, str]], int]:
    """(sort key, line) of each occurrence in one calendar, and how many series it left out."""
    body = await dav.request("REPORT", cal.url, auth, events_query(start, end), depth="1")
    found, left_out = [], 0
    for f in multistatus(body, cal.url):
        data = f.props.get(f"{{{CALDAV}}}calendar-data")
        if data is None or not data.text:
            continue
        try:
            events, costly = await asyncio.to_thread(occurrences, data.text, start, end)
            found += [
                (start_moment(e["DTSTART"].dt, start.tzinfo), event_line(e, cal.name, start.tzinfo))
                for e in events
            ]
        except Exception as exc:  # one object the libraries cannot read is skipped, not fatal
            logger.info("icloud_event_skipped error=%s", type(exc).__name__)
            continue
        left_out += costly
    return found, left_out


async def list_events(dav: Dav, args: ListEventsArgs) -> str:
    start = args.start or default_start()
    end = start + timedelta(days=args.days)
    _, auth = await credential(args.account)
    found: list[tuple[datetime, str]] = []
    left_out = 0
    for cal in await calendars(dav, auth):
        if "VEVENT" in cal.components:
            lines, costly = await _calendar_events(dav, auth, cal, start, end)
            found += lines
            left_out += costly
    found.sort(key=lambda pair: pair[0])
    lines = [f"source: {CALENDAR}", *(f"- {line}" for _, line in found[: args.limit])]
    if len(lines) == 1:
        lines.append("no events")
    if left_out:
        lines.append(left_out_note(left_out))
    return "\n".join(lines)


def _target(cals: list[Collection], name: str | None) -> Collection | str:
    """The person's own writable event calendar to add to, or why there is none."""
    own = [c for c in cals if "VEVENT" in c.components and c.writable and not c.shared]
    if name is None:
        return own[0] if own else "the person has no iCloud calendar of their own that takes events"
    named = [c for c in own if c.name == name]
    if len(named) == 1:
        return named[0]
    if named:
        return f"the person has several calendars named {one_line(name, 120)}; they can rename one"
    return f"the person has no iCloud calendar of their own named {one_line(name, 120)} that takes events"


async def create_event(dav: Dav, args: CreateEventArgs) -> str:
    person, auth = await credential(args.account)
    target = _target(await calendars(dav, auth), args.calendar)
    if isinstance(target, str):
        return f"source: {CALENDAR}\n{target}"
    uid = event_uid(person, args)
    headers = {"content-type": "text/calendar; charset=utf-8", "if-none-match": "*"}
    body = event_ics(uid, args, datetime.now(UTC))
    try:
        await dav.request("PUT", f"{target.url.rstrip('/')}/{uid}.ics", auth, body, headers=headers)
    except UpstreamError as exc:
        if exc.status == 412:
            return f"source: {CALENDAR}\nthis event is already in the calendar (uid {uid})"
        if exc.status == 403:
            return f"source: {CALENDAR}\niCloud did not let this calendar take the event"
        raise
    return (
        f"source: {CALENDAR}\ncreated: {args.start.isoformat()} to {args.end.isoformat()} · "
        f"{one_line(args.summary, 160)} · calendar {one_line(target.name, 80)} · uid {uid}"
    )


# --- contacts ---------------------------------------------------------------------------------


def contacts_query(text: str, limit: int) -> bytes:
    """Cards whose name, email or phone contains `text`, matched case-insensitively. The words are
    escaped for XML: in the element's text a `<` would otherwise open an element of the model's."""
    match = (
        f'<cr:text-match collation="i;unicode-casemap" match-type="contains">{escape(text)}</cr:text-match>'
    )
    filters = "".join(f'<cr:prop-filter name="{p}">{match}</cr:prop-filter>' for p in ("FN", "EMAIL", "TEL"))
    return f"""<?xml version="1.0" encoding="utf-8"?>
<cr:addressbook-query xmlns:d="DAV:" xmlns:cr="{CARDDAV}">
<d:prop><cr:address-data/></d:prop>
<cr:filter test="anyof">{filters}</cr:filter>
<cr:limit><cr:nresults>{limit}</cr:nresults></cr:limit>
</cr:addressbook-query>""".encode()


def contact_line(vcard: str) -> str:
    import vobject

    card = vobject.readOne(vcard)
    parts = [one_line(str(card.fn.value) if hasattr(card, "fn") else "(no name)", 160)]
    emails = [one_line(str(e.value), 120) for e in card.contents.get("email", [])][:3]
    phones = [one_line(str(t.value), 40) for t in card.contents.get("tel", [])][:3]
    if emails:
        parts.append("email " + ", ".join(emails))
    if phones:
        parts.append("tel " + ", ".join(phones))
    org = card.contents.get("org")
    if org:
        value = org[0].value
        parts.append(one_line(" ".join(value) if isinstance(value, list) else str(value), 120))
    return "- " + " · ".join(parts)


async def search_contacts(dav: Dav, args: SearchContactsArgs) -> str:
    _, auth = await credential(args.account)
    found: list[str] = []
    for book in await address_books(dav, auth):
        if len(found) >= args.limit:
            break
        body = await dav.request("REPORT", book.url, auth, contacts_query(args.query, args.limit), depth="1")
        for card in multistatus(body, book.url):
            data = card.props.get(f"{{{CARDDAV}}}address-data")
            if data is None or not data.text:
                continue
            try:
                found.append(contact_line(data.text))
            except Exception as exc:  # one card vobject cannot read is skipped, not fatal
                logger.info("icloud_contact_skipped error=%s", type(exc).__name__)
    return "\n".join([f"source: {CONTACTS}", *(found[: args.limit] or ["no contacts"])])


# --- the tools --------------------------------------------------------------------------------

#: Every tool `build_tools` returns, registered by name before any is built.
TOOL_NAMES = (
    "connectors__icloud_calendar_events",
    "connectors__icloud_calendar_create_event",
    "connectors__icloud_contacts_search",
)


def build_tools(settings: Settings) -> list[Tool]:
    """The family's tools under `settings`; `ValueError` without the `icloud` extra."""
    require_parsers()
    return icloud_tools(Dav.from_settings(settings))


def icloud_tools(dav: Dav) -> list[Tool]:
    tool = partial(connector_tool, family="icloud", hosts=dav.hosts)
    return [
        tool(
            name="connectors__icloud_calendar_events",
            service=CALENDAR,
            args=ListEventsArgs,
            run=answered(CALENDAR, partial(list_events, dav)),
            description="The person's upcoming iCloud Calendar events, from every calendar they see.",
        ),
        tool(
            name="connectors__icloud_calendar_create_event",
            service=CALENDAR,
            args=CreateEventArgs,
            run=answered(CALENDAR, partial(create_event, dav)),
            read_only=False,
            description="Add an event to one of the person's own iCloud calendars, with no guests.",
        ),
        tool(
            name="connectors__icloud_contacts_search",
            service=CONTACTS,
            args=SearchContactsArgs,
            run=answered(CONTACTS, partial(search_contacts, dav)),
            description="Find people in the person's iCloud Contacts by name, email or phone.",
        ),
    ]


__all__ = [
    "TOOL_NAMES",
    "Collection",
    "address_books",
    "build_tools",
    "calendars",
    "contact_line",
    "contacts_query",
    "event_ics",
    "event_uid",
    "events_query",
    "icloud_tools",
]
