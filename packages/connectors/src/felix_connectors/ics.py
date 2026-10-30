"""Calendar feeds: the events in the published `.ics` links a person connected.

Outlook, Google Calendar and iCloud can each publish a calendar as a link that serves the whole
calendar as iCalendar, no sign-in needed. That is how a calendar with no OAuth here -- Outlook, a
work account's, a shared family one -- reaches an agent: the person connects the link
(`POST /connections/ics`, `ics_connect`) and it is kept sealed in their vault (provider `ics`,
kind `secret`, account the label they gave it). The link is the credential -- anyone holding it
reads the calendar -- so it is never shown to the model, logged or put in an answer.

Where a link may point is the operator's: `FELIX_ICS_HOSTS`, checked when it is connected and
again on every read, for a link kept before the list changed. It is also what the tool declares
(`Tool.egress_hosts`), so an agent's `network.allow_hosts` must cover it. Read-only: a feed is a
copy. What it holds was written by anyone who could invite the person, so every answer is
untrusted text, and a repeating event is expanded only after its cost is estimated (`ical`).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from felix.config import Settings
from felix.tools.types import Tool
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from felix_connectors.http import ResponseTooLarge, UpstreamError, allow_http, send, user_agent
from felix_connectors.ical import default_start, event_line, left_out_note, occurrences, start_moment
from felix_connectors.person import PersonConnection, Unavailable, answered, person_connections
from felix_connectors.tools import connector_tool, one_line

logger = logging.getLogger("felix_connectors.ics")

PROVIDER = "ics"
SOURCE = "Calendar feed"
#: Feeds one person may connect, each read on every call: the connect route refuses one more.
MAX_FEEDS = 5
MAX_EVENTS = 25
MAX_LABEL = 64
MAX_URL = 2048
#: A published calendar is the whole calendar, years of it: more than any JSON answer here, and
#: parsing it is CPU on every call (about three seconds for three megabytes), so held at that.
MAX_FEED_BYTES = 3_000_000
#: The port a link may name, outside the development exemption: what a calendar service publishes on.
HTTPS_PORT = 443
FEED_TIMEOUT_S = 20.0


class HostNotAllowed(ValueError):
    """A feed link on a host `FELIX_ICS_HOSTS` does not cover: refused when connected, and said
    of a kept link when the operator has since narrowed the list."""


class NotACalendar(ValueError):
    """What a link served is not one iCalendar calendar."""


def require_parsers() -> None:
    """`ValueError` without the `ics` extra: a manifest naming the tool fails its compile by name."""
    try:
        import icalendar  # noqa: F401
        import recurring_ical_events  # noqa: F401
    except ImportError:
        raise ValueError("the calendar feed tools need the felix-connectors[ics] extra") from None


def check_label(label: str) -> str:
    """The name a person gives a feed ("Work", "Family"), trimmed, or `ValueError`. It is what the
    model names the feed by, so it is one short printable line, and its `account_of` is a path
    segment of `DELETE /connections/ics/{account}`, so it holds no slash."""
    value = " ".join(label.split())
    if not value or len(value) > MAX_LABEL or not value.isprintable() or any(ch in value for ch in "/\\"):
        raise ValueError(f"a feed's name is 1 to {MAX_LABEL} printable characters, with no slash")
    return value


def account_of(label: str) -> str:
    """The vault account a feed is kept under: its name, case folded, so "Work" and "work" are
    one feed -- the name it was connected with is kept as its label."""
    return label.casefold()


def check_feed_url(url: str, hosts: frozenset[str], *, plain: bool = False) -> str:
    """`url` as kept and fetched -- `webcal://` read as `https://`, as calendar apps do -- or
    `ValueError` for a link that is not https, carries credentials or a fragment, or points
    anywhere `hosts` (`FELIX_ICS_HOSTS`) does not cover. `plain` admits http (development only)."""
    from felix.security.host_allowlist import HostAllowlist

    text = url.strip()
    if len(text) > MAX_URL or not text.isprintable() or any(ch.isspace() for ch in text):
        raise ValueError("not a link")
    parts = urlsplit(text)
    scheme = "https" if parts.scheme.lower() in {"webcal", "webcals"} else parts.scheme.lower()
    if scheme not in ({"https", "http"} if plain else {"https"}):
        raise ValueError("a feed link is https")
    if not parts.hostname or parts.username or parts.password or parts.fragment:
        raise ValueError("a feed link names a host, with no credentials or fragment")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("a feed link's port is a number") from None
    # A link on an allowed host still names the port it is fetched from: only the one calendars
    # are published on, so a link cannot have this deployment probe a host's other ports.
    if not plain and port not in (None, HTTPS_PORT):
        raise ValueError("a feed link is on the https port")
    if not HostAllowlist(tuple(sorted(hosts))).allows(parts.hostname):
        raise HostNotAllowed("this deployment does not read feeds from that host")
    return urlunsplit((scheme, parts.netloc, parts.path or "/", parts.query, ""))


def check_calendar(text: str) -> None:
    """`NotACalendar` unless `text` is one iCalendar calendar: what a link must serve to be kept."""
    import icalendar

    try:
        cal = icalendar.Calendar.from_ical(text)
    except Exception:
        raise NotACalendar("not iCalendar") from None
    if getattr(cal, "name", "") != "VCALENDAR":
        raise NotACalendar("not a calendar")


@dataclass(frozen=True, slots=True)
class Feeds:
    """Where feeds may be read from, and as whom: the deployment's half of every read."""

    hosts: frozenset[str]
    user_agent: str
    allow_http: bool = False

    @classmethod
    def from_settings(cls, settings: Settings) -> Feeds:
        hosts = frozenset(h for h in settings.ics_hosts.split(",") if h)
        return cls(hosts=hosts, user_agent=user_agent(settings), allow_http=allow_http(settings))

    async def fetch(self, url: str) -> str:
        """The feed at `url`, checked against `hosts` first, as text."""
        checked = check_feed_url(url, self.hosts, plain=self.allow_http)
        body = await send(
            "GET",
            checked,
            headers={"user-agent": self.user_agent, "accept": "text/calendar"},
            hosts=self.hosts,
            allow_http=self.allow_http,
            timeout_s=FEED_TIMEOUT_S,
            max_bytes=MAX_FEED_BYTES,
        )
        return body.decode("utf-8", errors="replace")


class EventsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feed: str | None = Field(
        default=None, max_length=MAX_LABEL, description="One connected feed by its name; all if omitted."
    )
    start: AwareDatetime | None = Field(
        default=None, description="From when (ISO 8601 with offset); now if omitted."
    )
    days: int = Field(default=7, ge=1, le=60)
    limit: int = Field(default=10, ge=1, le=MAX_EVENTS)


async def _connected(feed: str | None) -> list[PersonConnection]:
    """The person's own feeds, by name when `feed` is given, in name order, or `Unavailable`."""
    mine = await person_connections(PROVIDER, SOURCE, noun="calendar feed")
    if feed:
        wanted = account_of(" ".join(feed.split()))
        mine = [c for c in mine if c.account == wanted]
        if not mine:
            raise Unavailable(
                f"the person has not connected a calendar feed named {one_line(feed, MAX_LABEL)}"
            )
    if not mine:
        raise Unavailable("the person has not connected a calendar feed; they can connect one in Felix")
    return sorted(mine, key=lambda c: c.account)[:MAX_FEEDS]


def _unread(label: str, exc: Exception) -> str:
    """Why one feed went unread, in words that never carry its link: what the person must do, or
    that it may work later -- never both readings of one failure."""
    if isinstance(exc, Unavailable):
        why = str(exc)  # the vault's refusal, worded by `PersonConnection.opened`
    elif isinstance(exc, UpstreamError):
        why = f"its calendar service answered {exc.status}"
        if exc.status in (401, 403, 404, 410):
            why += "; the link may have been withdrawn, so the person may need to connect it again"
    elif isinstance(exc, TimeoutError):
        why = "it did not answer in time"
    elif isinstance(exc, HostNotAllowed):
        why = "this deployment no longer reads feeds from its host"
    elif isinstance(exc, ResponseTooLarge):
        why = "it is larger than this deployment reads"
    elif isinstance(exc, ParseError):
        why = "what its link serves is not a calendar this deployment can read"
    else:
        why = "it could not be read just now"
    return f"(feed {one_line(label, MAX_LABEL)} not read: {why})"


class ParseError(Exception):
    """What a kept link served could not be read as iCalendar: permanent until the link changes."""


def _read(text: str, start: datetime, end: datetime) -> tuple[list[Any], int]:
    try:
        return occurrences(text, start, end)
    except Exception as exc:
        raise ParseError(type(exc).__name__) from None


async def list_events(feeds: Feeds, args: EventsArgs) -> str:
    from felix.auth import connections

    rows = await _connected(args.feed)
    start = args.start or default_start()
    end = start + timedelta(days=args.days)
    found: list[tuple[datetime, str]] = []
    notes: list[str] = []
    left_out = 0
    for row in rows:
        label = row.row["label"]
        try:
            text = await feeds.fetch(await row.opened(connections.secret))
            events, costly = await asyncio.to_thread(_read, text, start, end)
        except Exception as exc:  # one feed that cannot be read is said, not fatal to the rest
            logger.info("ics_feed_unread error=%s", type(exc).__name__)
            notes.append(_unread(label, exc))
            continue
        found += [
            (start_moment(e["DTSTART"].dt, start.tzinfo), event_line(e, label, start.tzinfo)) for e in events
        ]
        left_out += costly
    found.sort(key=lambda pair: pair[0])
    lines = [f"source: {SOURCE}s", *(f"- {line}" for _, line in found[: args.limit])]
    if len(lines) == 1:
        lines.append("no events")
    if left_out:
        lines.append(left_out_note(left_out))
    return "\n".join(lines + notes)


TOOL_NAMES = ("connectors__ics_events",)


def build_tools(settings: Settings) -> list[Tool]:
    """The family's tools under `settings`; `ValueError` without the `ics` extra."""
    require_parsers()
    return ics_tools(Feeds.from_settings(settings))


def ics_tools(feeds: Feeds) -> list[Tool]:
    return [
        connector_tool(
            name="connectors__ics_events",
            family="ics",
            service=SOURCE,
            hosts=feeds.hosts,
            args=EventsArgs,
            run=answered(SOURCE, partial(list_events, feeds)),
            description=(
                "Upcoming events from the published calendar feeds (.ics links) the person "
                "connected, such as an Outlook or shared calendar, each named by the feed."
            ),
        )
    ]


__all__ = [
    "MAX_FEEDS",
    "MAX_FEED_BYTES",
    "MAX_LABEL",
    "MAX_URL",
    "PROVIDER",
    "TOOL_NAMES",
    "EventsArgs",
    "Feeds",
    "HostNotAllowed",
    "NotACalendar",
    "account_of",
    "build_tools",
    "check_calendar",
    "check_feed_url",
    "check_label",
    "ics_tools",
    "list_events",
    "require_parsers",
]
