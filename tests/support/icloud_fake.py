"""iCloud's CalDAV and CardDAV, played at two loopback addresses, for the iCloud tools and route.

Shaped the way iCloud answers, because each kindness here is a bug the tools could keep:

- two services: the CalDAV principal names only the calendar home, the CardDAV one only the
  address-book home, each by *absolute* URL, as iCloud names a partition host of the person's;
- the principal lookup at a service's root answers with a relative href;
- a PROPFIND answers a property a resource lacks in a second propstat, `404 Not Found`, holding
  that property empty -- the scheduling inbox has no component set, the home no displayname;
- the calendar home lists, in this order, a calendar someone shared with the person read-only
  (`Family`, CalendarServer `shared`), the person's `Home` and `Work`, a reminders-only
  calendar, a read-only `Old` that does not say what it holds, and the inbox, which is no
  calendar at all; the address-book home holds two books;
- a request without the person's Basic credential is answered 401; a PUT into the shared
  calendar 403; a PUT of a body that is not one well-formed VEVENT named by its file 400; a
  `PUT` with `If-None-Match: *` over an existing resource 412.

`events` and `cards` are what the person has, per collection path; `resources` what was PUT.
Queries' filters are not applied: what a tool asks for is checked in the request it sent.
"""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

from tests.support.loopback_http import Request, respond

APPLE_ID = "me@icloud.com"
APP_PASSWORD = "abcd-efgh-ijkl-mnop"

D = "DAV:"
C = "urn:ietf:params:xml:ns:caldav"
CR = "urn:ietf:params:xml:ns:carddav"
CS = "http://calendarserver.org/ns/"

PRINCIPAL = "/123/principal/"
CALENDAR_HOME = "/123/calendars/"
SHARED = "/123/calendars/family/"
EVENTS = "/123/calendars/home/"
WORK = "/123/calendars/work/"
OLD = "/123/calendars/old/"
TASKS = "/123/calendars/tasks/"
INBOX = "/123/calendars/inbox/"
CARD_HOME = "/123/carddavhome/"
CARDS = "/123/carddavhome/card/"
MORE_CARDS = "/123/carddavhome/shared/"

_WRITE = "<d:current-user-privilege-set><d:privilege><d:read/></d:privilege><d:privilege><d:write/></d:privilege></d:current-user-privilege-set>"
_READ = "<d:current-user-privilege-set><d:privilege><d:read/></d:privilege></d:current-user-privilege-set>"


def ics(*events: str) -> str:
    body = "".join(events)
    return f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Apple Inc.//iCloud//EN\r\n{body}END:VCALENDAR\r\n"


def vevent(uid: str, summary: str, start: str, end: str, *extra: str) -> str:
    """`start`/`end` are a property's value with its parameters, e.g. `;TZID=America/Phoenix:2026…`
    or `:20261102T170000Z` -- a bare value gets the `:`."""
    when = [
        f"DTSTART{start if start[0] in ':;' else ':' + start}",
        f"DTEND{end if end[0] in ':;' else ':' + end}",
    ]
    lines = [f"UID:{uid}", f"SUMMARY:{summary}", *when, *extra]
    return "BEGIN:VEVENT\r\n" + "".join(f"{line}\r\n" for line in lines) + "END:VEVENT\r\n"


def vcard(name: str, *extra: str) -> str:
    lines = ["VERSION:3.0", f"FN:{name}", *extra]
    return "BEGIN:VCARD\r\n" + "".join(f"{line}\r\n" for line in lines) + "END:VCARD\r\n"


def _response(href: str, found: str, missing: str = "") -> str:
    """One response: `found` under 200, and -- as iCloud does -- `missing` empty under 404."""
    stats = f"<d:propstat><d:prop>{found}</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
    if missing:
        stats += (
            f"<d:propstat><d:prop>{missing}</d:prop><d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>"
        )
    return f"<d:response><d:href>{href}</d:href>{stats}</d:response>"


def _multistatus(*responses: str) -> bytes:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><d:multistatus xmlns:d="{D}" xmlns:c="{C}" '
        f'xmlns:cr="{CR}" xmlns:cs="{CS}">{"".join(responses)}</d:multistatus>'
    ).encode()


def _calendar(href: str, name: str, comp: str, privileges: str, *, shared: bool = False) -> str:
    kind = "<d:collection/><c:calendar/>" + ("<cs:shared/>" if shared else "")
    return _response(
        href,
        f"<d:resourcetype>{kind}</d:resourcetype><d:displayname>{name}</d:displayname>{privileges}"
        f'<c:supported-calendar-component-set><c:comp name="{comp}"/></c:supported-calendar-component-set>',
    )


class FakeICloud:
    """One Apple ID's iCloud: `caldav` and `carddav` are each a loopback responder."""

    def __init__(self, *, apple_id: str = APPLE_ID, password: str = APP_PASSWORD) -> None:
        self.auth = "Basic " + base64.b64encode(f"{apple_id}:{password}".encode()).decode()
        self.events: dict[str, list[str]] = {SHARED: [], EVENTS: [], WORK: [], OLD: []}
        self.names = {SHARED: "Family", EVENTS: "Home", WORK: "Work"}
        #: Whether the person may write to the calendar shared with them (an owner can allow it).
        self.shared_writable = False
        self.cards: dict[str, list[str]] = {CARDS: [], MORE_CARDS: []}
        self.resources: dict[str, bytes] = {}
        self.seen: list[Request] = []
        self.served: dict[str, list[str]] = {"caldav": [], "carddav": []}
        #: Where the CalDAV principal says the calendar home is, when not on its own server.
        self.home_host: str | None = None
        #: (method, path) -> (status, body), answered before anything else, credential or not.
        self.raw: dict[tuple[str, str], tuple[str, bytes]] = {}

    def requests(self, method: str) -> list[Request]:
        return [r for r in self.seen if r.method == method]

    def caldav(self, request: Request, writer: Any) -> None:
        self.served["caldav"].append(f"{request.method} {request.path}")
        self._serve(request, writer, self._caldav)

    def carddav(self, request: Request, writer: Any) -> None:
        self.served["carddav"].append(f"{request.method} {request.path}")
        self._serve(request, writer, self._carddav)

    def _serve(self, request: Request, writer: Any, answer: Any) -> None:
        self.seen.append(request)
        path = urlsplit(request.path).path
        if (request.method, path) in self.raw:
            status, body = self.raw[(request.method, path)]
            respond(writer, body, status=status, ctype="application/xml")
            return
        if request.headers.get("authorization") != self.auth:
            respond(
                writer, b"", status="401 Unauthorized", extra='www-authenticate: Basic realm="iCloud"\r\n'
            )
            return
        status, body = self._root(request, path) if path == "/" else answer(request, path)
        respond(writer, body, status=status, ctype="application/xml; charset=utf-8")

    def _root(self, request: Request, path: str) -> tuple[str, bytes]:
        if request.method != "PROPFIND":
            return "405 Method Not Allowed", b""
        principal = f"<d:current-user-principal><d:href>{PRINCIPAL}</d:href></d:current-user-principal>"
        return "207 Multi-Status", _multistatus(_response(path, principal))

    def _caldav(self, request: Request, path: str) -> tuple[str, bytes]:
        host = self.home_host or f"http://{request.headers['host']}"
        multi = "207 Multi-Status"
        match request.method, path:
            case "PROPFIND", "/123/principal/":
                home = f"<c:calendar-home-set><d:href>{host}{CALENDAR_HOME}</d:href></c:calendar-home-set>"
                return multi, _multistatus(_response(PRINCIPAL, home, "<cr:addressbook-home-set/>"))
            case "PROPFIND", "/123/calendars/":
                return multi, _multistatus(
                    _response(
                        CALENDAR_HOME,
                        "<d:resourcetype><d:collection/></d:resourcetype>",
                        "<d:displayname/><c:supported-calendar-component-set/>",
                    ),
                    _calendar(
                        SHARED,
                        self.names[SHARED],
                        "VEVENT",
                        _WRITE if self.shared_writable else _READ,
                        shared=True,
                    ),
                    _calendar(EVENTS, self.names[EVENTS], "VEVENT", _WRITE),
                    _calendar(WORK, self.names[WORK], "VEVENT", _WRITE),
                    _calendar(TASKS, "Reminders", "VTODO", _WRITE),
                    # Says nothing of what it holds: the set comes back empty under 404.
                    _response(
                        OLD,
                        "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype><d:displayname>Old</d:displayname>"
                        + _READ,
                        "<c:supported-calendar-component-set/>",
                    ),
                    _response(
                        INBOX,
                        "<d:resourcetype><d:collection/><c:schedule-inbox/></d:resourcetype>",
                        "<d:displayname/><c:supported-calendar-component-set/>",
                    ),
                )
            case "REPORT", _ if path in self.events:
                return multi, _multistatus(
                    *(
                        _response(f"{path}e{i}.ics", f"<c:calendar-data>{escape(e)}</c:calendar-data>")
                        for i, e in enumerate(self.events[path])
                    )
                )
            case "PUT", _:
                return self._put(request, path), b""
        return "404 Not Found", b""

    def _put(self, request: Request, path: str) -> str:
        import icalendar

        if path.startswith(SHARED):
            if not self.shared_writable:
                return "403 Forbidden"
            self.resources[path] = request.body
            return "201 Created"
        if not path.startswith((EVENTS, WORK)):
            return "404 Not Found"
        try:
            cal = icalendar.Calendar.from_ical(request.body)
            (event,) = cal.walk("VEVENT")
            assert str(cal["VERSION"]) == "2.0" and cal.get("PRODID") and event.get("DTSTAMP")
            assert f"{event['UID']}.ics" == path.rsplit("/", 1)[1]
        except Exception:
            return "400 Bad Request"
        if path in self.resources and request.headers.get("if-none-match") == "*":
            return "412 Precondition Failed"
        self.resources[path] = request.body
        return "201 Created"

    def _carddav(self, request: Request, path: str) -> tuple[str, bytes]:
        host = f"http://{request.headers['host']}"
        multi = "207 Multi-Status"
        match request.method, path:
            case "PROPFIND", "/123/principal/":
                home = (
                    f"<cr:addressbook-home-set><d:href>{host}{CARD_HOME}</d:href></cr:addressbook-home-set>"
                )
                return multi, _multistatus(_response(PRINCIPAL, home, "<c:calendar-home-set/>"))
            case "PROPFIND", "/123/carddavhome/":
                book = "<d:resourcetype><d:collection/><cr:addressbook/></d:resourcetype>"
                return multi, _multistatus(
                    _response(
                        CARD_HOME, "<d:resourcetype><d:collection/></d:resourcetype>", "<d:displayname/>"
                    ),
                    _response(CARDS, f"{book}<d:displayname>Contacts</d:displayname>"),
                    _response(MORE_CARDS, f"{book}<d:displayname>Shared</d:displayname>"),
                )
            case "REPORT", _ if path in self.cards:
                return multi, _multistatus(
                    *(
                        _response(f"{path}c{i}.vcf", f"<cr:address-data>{escape(c)}</cr:address-data>")
                        for i, c in enumerate(self.cards[path])
                    )
                )
        return "404 Not Found", b""


__all__ = [
    "APPLE_ID",
    "APP_PASSWORD",
    "CARDS",
    "EVENTS",
    "MORE_CARDS",
    "OLD",
    "SHARED",
    "WORK",
    "FakeICloud",
    "ics",
    "vcard",
    "vevent",
]
