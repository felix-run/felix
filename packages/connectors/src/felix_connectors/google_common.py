"""What the Google families (`google_calendar`, `gmail`, `google_drive`) share: whose Google, sent how.

Each tool acts for the person the run acts for (`AuthContext.person`), with the Google grant they
connected (`felix.auth.google_connect`) and keep in their vault (`felix.auth.connections`): never
an operator's key, never another person's. A run that acts for no one person -- an anonymous
caller, a shared API key, a trigger -- has no Google account to use, and the tool says so.

Requests are built by Google's own client (`googleapiclient`, the `google` extra) from the
discovery documents it ships -- the method, the URL, the parameters' encoding -- and sent by
Felix's egress-guarded client (`http.send`), held to the hosts each tool declares. The person's
access token goes in the request's header and nowhere else: not in an answer, not in an error.

What a person's Google holds is written by anyone who can email or share with them, so every
answer is untrusted text (transport `connectors`): a manifest offering these tools should turn on
content screening and put the ones that change things behind approvals.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Sequence
from functools import lru_cache
from typing import Annotated, Any

from felix.config import Settings
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from felix_connectors.http import UpstreamError, allow_http, send, user_agent
from felix_connectors.tools import one_line

SOURCE = "Google"
GOOGLEAPIS = "https://www.googleapis.com"
GMAIL_API = "https://gmail.googleapis.com"
#: What one mail body or file contributes to an answer.
MAX_TEXT = 8000
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class Unavailable(Exception):
    """Something the model should be told in words and not retry around: the person has no
    Google account connected, it lacks a scope, they named an account they do not have."""


def require_client() -> None:
    """`ValueError` without the `google` extra: a manifest naming a Google tool fails its compile
    by name, and a manifest write leaves the tool to the compile (`offline_tools`)."""
    try:
        import googleapiclient.discovery  # noqa: F401
    except ImportError:
        raise ValueError("the Google tools need the felix-connectors[google] extra") from None


@lru_cache(maxsize=8)
def _service(name: str, version: str, endpoint: str) -> Any:
    """Google's client for one API, built from the discovery document it ships, once. It builds
    requests and is never asked to send one, so the `http` it is given is never used."""
    import httplib2
    from googleapiclient.discovery import build

    return build(
        name,
        version,
        http=httplib2.Http(),
        static_discovery=True,
        cache_discovery=False,
        client_options={"api_endpoint": endpoint},
    )


def scope_refusal(label: str, scopes: Sequence[str]) -> str:
    wanted = " or ".join(scopes)
    return f"{one_line(label, 120)} has not granted {wanted}; the person can reconnect Google with it"


async def connection(scopes: Sequence[str], account: str | None) -> tuple[Any, str, str, str]:
    """(settings, tenant, person, account id) for the person this run acts for, with a Google
    account that granted one of `scopes` (`google_connect.SCOPES` names). `Unavailable` otherwise."""
    from felix.auth import connections
    from felix.auth.google_connect import SCOPES
    from felix.context import try_get_context

    ctx = try_get_context()
    person = ctx.auth.person if ctx is not None else ""
    if ctx is None or not person or not ctx.auth.tenant_id:
        raise Unavailable("this run acts for no one person, so there is no Google account to use")
    mine = [
        c
        for c in await connections.list_for_person(ctx.settings, ctx.auth.tenant_id, person)
        if c["provider"] == "google"
    ]
    if account:
        mine = [c for c in mine if account in (c["label"], c["account"])]
        if not mine:
            raise Unavailable(f"the person has not connected the Google account {one_line(account, 120)}")
    if not mine:
        raise Unavailable("the person has not connected a Google account; they can connect one in Felix")
    if len(mine) > 1:
        labels = ", ".join(one_line(c["label"], 120) for c in mine)
        raise Unavailable(
            f"the person has connected several Google accounts ({labels}); name one with `account`"
        )
    (row,) = mine
    if row["status"] != "active":
        raise Unavailable(
            f"the Google connection for {one_line(row['label'], 120)} no longer works; "
            "the person must reconnect it"
        )
    if not any(SCOPES[s] in set(row["scopes"]) for s in scopes):
        raise Unavailable(scope_refusal(row["label"], scopes))
    return ctx.settings, ctx.auth.tenant_id, person, row["account"]


class GoogleApis:
    """Where the APIs are and how to reach them: the endpoints Google's client builds URLs
    against, and so the hosts each family declares. A test points both at a loopback server."""

    def __init__(
        self, *, googleapis: str = GOOGLEAPIS, gmail: str = GMAIL_API, ua: str = "", insecure: bool = False
    ) -> None:
        from felix.security.host_allowlist import url_host

        self.googleapis = googleapis.rstrip("/")
        self.gmail = gmail.rstrip("/")
        self.user_agent = ua
        self.allow_http = insecure
        self.googleapis_hosts = frozenset({url_host(self.googleapis)})
        self.gmail_hosts = frozenset({url_host(self.gmail)})

    @classmethod
    def from_settings(cls, settings: Settings) -> GoogleApis:
        require_client()
        return cls(ua=user_agent(settings), insecure=allow_http(settings))

    def service(self, name: str, version: str) -> Any:
        # An `api_endpoint` replaces the API's whole base URL, path included, so each is given
        # in full -- the same URLs Google's client builds with no endpoint at all.
        base = f"{self.gmail}/" if name == "gmail" else f"{self.googleapis}/{name}/{version}/"
        return _service(name, version, base)

    async def call(
        self, request: Any, scopes: Sequence[str], account: str | None, hosts: frozenset[str]
    ) -> bytes:
        """Send one request Google's client built, as the person, through the egress guard."""
        from felix.auth import connections

        settings, tenant, person, account_id = await connection(scopes, account)
        try:
            token = await connections.access_token(settings, tenant, person, "google", account_id)
        except connections.ConnectionRevoked:
            # Google refused the refresh just now: the person withdrew Felix, or the grant expired.
            raise Unavailable(
                "the person's Google connection no longer works; they must reconnect it"
            ) from None
        except connections.NotConnected, connections.AmbiguousConnection:
            raise Unavailable("the person's Google connection changed just now; try again") from None
        except connections.ProviderUnavailable:
            raise Unavailable("this deployment cannot refresh Google connections") from None
        headers = {
            "authorization": f"Bearer {token}",
            "accept": "application/json",
            "user-agent": self.user_agent,
        }
        if request.body is not None:
            headers["content-type"] = request.headers.get("content-type", "application/json")
        body = request.body.encode() if isinstance(request.body, str) else request.body
        try:
            return await send(
                request.method,
                request.uri,
                headers=headers,
                hosts=hosts,
                content=body,
                allow_http=self.allow_http,
            )
        except UpstreamError as exc:
            if exc.status == 401:
                raise Unavailable(
                    "Google no longer accepts this connection; the person may need to reconnect"
                ) from None
            if exc.status == 403:
                raise Unavailable(
                    "Google refused: the connected account may lack the access this needs"
                ) from None
            if exc.status == 404:
                raise Unavailable("Google has nothing by that id for this account") from None
            raise

    async def json(
        self, request: Any, scopes: Sequence[str], account: str | None, hosts: frozenset[str]
    ) -> Any:
        body = json.loads(await self.call(request, scopes, account, hosts))
        if not isinstance(body, dict):
            raise ValueError("Google's answer is not an object")
        return body


def answered(service: str, fn: Callable[[Any], Awaitable[str]]) -> Callable[[Any], Awaitable[str]]:
    """`fn`, with an `Unavailable` answered as a finding rather than an outage to retry."""

    async def run(args: Any) -> str:
        try:
            return await fn(args)
        except Unavailable as exc:
            return f"source: {service}\n{exc}"

    return run


def capped(text: str) -> str:
    """Line breaks kept -- a mail or a document reads in paragraphs -- control characters not."""
    clean = "".join(ch if ch.isprintable() or ch in "\n\t" else " " for ch in text)
    return clean[:MAX_TEXT] + "\n(… cut)" if len(clean) > MAX_TEXT else clean


def _id(value: str) -> str:
    if not _ID.match(value):
        raise ValueError("an id is letters, digits, - and _")
    return value


def _one_line_header(value: str) -> str:
    if any(ch in value for ch in "\r\n"):
        raise ValueError("a header value is one line")
    return value


Id = Annotated[str, AfterValidator(_id)]
Header = Annotated[str, AfterValidator(_one_line_header)]


def is_id(value: object) -> bool:
    return isinstance(value, str) and bool(_ID.match(value))


class AccountArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account: str | None = Field(
        default=None, max_length=320, description="Which connected Google account (its email), if several."
    )


__all__ = [
    "GMAIL_API",
    "GOOGLEAPIS",
    "MAX_TEXT",
    "SOURCE",
    "AccountArgs",
    "GoogleApis",
    "Header",
    "Id",
    "Unavailable",
    "answered",
    "capped",
    "connection",
    "is_id",
    "require_client",
    "scope_refusal",
]
