"""CalDAV and CardDAV against iCloud, as the person, through the egress guard.

Apple offers no OAuth for iCloud data, so a person connects with their Apple ID and an
**app-specific password** (appleid.apple.com, Sign-In and Security), kept sealed in their vault
(`felix.auth.connections`, provider `icloud`, kind `secret`). Only that format is accepted: a
person's own Apple password is refused before it is sent anywhere, and an app-specific one can be
revoked at Apple without touching the account.

The protocol is a handful of WebDAV requests -- PROPFIND to find the person's calendars and
address books, REPORT to query them, PUT to add an event -- built here and sent by Felix's guarded
client (`http.send`), held to `*.icloud.com`: iCloud answers discovery with a numbered partition
host of the person's (`p42-caldav.icloud.com`), named by Apple's answer, never by the model.
What travels in them, iCalendar and vCard, is read and written by the `icloud` extra's libraries.
"""

from __future__ import annotations

import base64
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urljoin
from xml.parsers import expat

from felix.config import Settings

from felix_connectors.http import USER_AGENT, UpstreamError, allow_http, send, user_agent
from felix_connectors.person import Unavailable

PROVIDER = "icloud"
SOURCE = "iCloud"
ICLOUD_CALDAV = "https://caldav.icloud.com"
ICLOUD_CARDDAV = "https://contacts.icloud.com"
#: Discovery hands back a partition host of the person's under this; nothing else is reached.
ICLOUD_HOSTS = frozenset({"*.icloud.com"})
#: How appleid.apple.com shows one: four groups of four lowercase letters.
APP_PASSWORD = re.compile(r"^[a-z]{4}-[a-z]{4}-[a-z]{4}-[a-z]{4}$")
MAX_APPLE_ID = 320

DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
CARDDAV = "urn:ietf:params:xml:ns:carddav"
_XML = {"content-type": 'application/xml; charset="utf-8"'}

PRINCIPAL = b"""<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>"""


class Refused(Unavailable):
    """Apple did not accept the Apple ID and app-specific password (401). An `Unavailable`, so a
    tool answers it in words with no wrapping of its own; the connect route catches it by name."""

    def __init__(self) -> None:
        super().__init__(
            "Apple no longer accepts this app-specific password; the person must connect iCloud again"
        )


def require_parsers() -> None:
    """`ValueError` without the `icloud` extra: a manifest naming an iCloud tool fails its compile
    by name, and a manifest write leaves the tool to the compile (`offline_tools`)."""
    try:
        import icalendar  # noqa: F401
        import recurring_ical_events  # noqa: F401
        import vobject  # noqa: F401
    except ImportError:
        raise ValueError("the iCloud tools need the felix-connectors[icloud] extra") from None


def check_apple_id(apple_id: str) -> str:
    """`apple_id` as stored (trimmed, lowercased), or `ValueError`. It becomes the user half of a
    Basic credential, where a `:` would end it, and a header, where a line break would."""
    value = apple_id.strip().lower()
    broken = not value.isprintable() or any(ch in value for ch in ": ")
    if broken or "@" not in value or len(value) > MAX_APPLE_ID:
        raise ValueError("an Apple ID is the email address the person signs in to Apple with")
    return value


def check_app_password(password: str) -> str:
    value = password.strip()
    if not APP_PASSWORD.match(value):
        raise ValueError(
            "use an app-specific password from appleid.apple.com (xxxx-xxxx-xxxx-xxxx), "
            "never the Apple ID's own password"
        )
    return value


def basic(apple_id: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{apple_id}:{password}".encode()).decode()


@dataclass(frozen=True, slots=True)
class Found:
    """One `<d:response>` of a multistatus: where, and the properties that came back 200."""

    href: str
    props: Mapping[str, ET.Element]


def multistatus(body: bytes, base: str) -> list[Found]:
    """A 207's responses, each `href` made absolute against `base` (the URL asked)."""
    _refuse_dtd(body)
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise ValueError("a DAV answer that is not XML") from None
    out = []
    for response in root.iter(f"{{{DAV}}}response"):
        href = (response.findtext(f"{{{DAV}}}href") or "").strip()
        if not href:
            continue
        props: dict[str, ET.Element] = {}
        for propstat in response.iter(f"{{{DAV}}}propstat"):
            status = propstat.findtext(f"{{{DAV}}}status") or ""
            prop = propstat.find(f"{{{DAV}}}prop")
            if " 200 " in f"{status} " and prop is not None:
                props.update({child.tag: child for child in prop})
        out.append(Found(urljoin(base, href), props))
    return out


def _refuse_dtd(body: bytes) -> None:
    """`ValueError` for a body that declares a DTD. Apple's answers carry none; one that does is
    refused before it is parsed, so no entity of a server's choosing is ever expanded. Asked of the
    parser, not of the bytes: a UTF-16 body spells `<!DOCTYPE` in no byte pattern a search finds."""

    def refuse(*_: object) -> None:
        raise ValueError("a DAV answer with a DTD")

    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    try:
        parser.Parse(body, True)
    except expat.ExpatError:
        raise ValueError("a DAV answer that is not XML") from None


def href_of(prop: ET.Element | None, base: str) -> str:
    """The `<d:href>` a property holds, absolute against `base`; empty when it holds none."""
    href = (prop.findtext(f"{{{DAV}}}href") or "").strip() if prop is not None else ""
    return urljoin(base, href) if href else ""


@dataclass(frozen=True, slots=True)
class Dav:
    """Where iCloud is and how to reach it. A test points both services at a loopback server."""

    caldav: str = ICLOUD_CALDAV
    carddav: str = ICLOUD_CARDDAV
    hosts: frozenset[str] = ICLOUD_HOSTS
    user_agent: str = USER_AGENT
    allow_http: bool = False
    timeout_s: float = field(default=15.0, compare=False)

    @classmethod
    def from_settings(cls, settings: Settings) -> Dav:
        return cls(user_agent=user_agent(settings), allow_http=allow_http(settings))

    async def request(
        self,
        method: str,
        url: str,
        auth: str,
        body: bytes | None = None,
        *,
        depth: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> bytes:
        """One request as the person (`auth`, a Basic credential). `Refused` for a 401 -- the
        credential itself; a 403 is about one resource (a calendar shared read-only) and stays an
        `UpstreamError`, as any other status that is not 2xx does."""
        sent = {"authorization": auth, "user-agent": self.user_agent, **(headers or {})}
        if body is not None:
            sent.update(_XML if "content-type" not in sent else {})
        if depth is not None:
            sent["depth"] = depth
        try:
            return await send(
                method,
                url,
                headers=sent,
                hosts=self.hosts,
                content=body,
                allow_http=self.allow_http,
                timeout_s=self.timeout_s,
            )
        except UpstreamError as exc:
            if exc.status == 401:
                raise Refused from None
            raise

    async def propfind(self, url: str, auth: str, body: bytes, depth: str) -> list[Found]:
        return multistatus(await self.request("PROPFIND", url, auth, body, depth=depth), url)

    async def principal(self, base: str, auth: str) -> str:
        """The person's principal URL on the service at `base` (also how a credential is checked)."""
        found = await self.propfind(f"{base}/", auth, PRINCIPAL, "0")
        for f in found:
            url = href_of(f.props.get(f"{{{DAV}}}current-user-principal"), f.href)
            if url:
                return url
        raise ValueError("iCloud named no principal for this Apple ID")

    async def calendar_home(self, auth: str) -> str:
        return await self._home(self.caldav, auth, CALDAV, "calendar-home-set")

    async def addressbook_home(self, auth: str) -> str:
        return await self._home(self.carddav, auth, CARDDAV, "addressbook-home-set")

    async def _home(self, base: str, auth: str, namespace: str, name: str) -> str:
        """The collection holding the person's calendars (`calendar-home-set`) or address books
        (`addressbook-home-set`), found from their principal."""
        principal = await self.principal(base, auth)
        body = (
            f'<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:" xmlns:x="{namespace}">'
            f"<d:prop><x:{name}/></d:prop></d:propfind>"
        ).encode()
        for f in await self.propfind(principal, auth, body, "0"):
            url = href_of(f.props.get(f"{{{namespace}}}{name}"), f.href)
            if url:
                return url
        raise ValueError(f"iCloud named no {name} for this Apple ID")


__all__ = [
    "APP_PASSWORD",
    "CALDAV",
    "CARDDAV",
    "DAV",
    "ICLOUD_CALDAV",
    "ICLOUD_CARDDAV",
    "ICLOUD_HOSTS",
    "PROVIDER",
    "SOURCE",
    "Dav",
    "Found",
    "Refused",
    "basic",
    "check_app_password",
    "check_apple_id",
    "href_of",
    "multistatus",
    "require_parsers",
]
