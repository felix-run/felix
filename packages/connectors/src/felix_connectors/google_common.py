"""What the Google families (`google_calendar`, `gmail`, `google_drive`) share: whose Google, sent how.

Each tool acts for the person the run acts for (`person.person_connection`), with the Google grant
they connected (`felix.auth.google_connect`) and keep in their vault.

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
from collections.abc import Sequence
from functools import lru_cache
from typing import Annotated, Any

from felix.config import Settings
from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from felix_connectors.http import UpstreamError, allow_http, send, user_agent
from felix_connectors.person import PersonConnection, Unavailable, person_connection
from felix_connectors.tools import one_line

SOURCE = "Google"
GOOGLEAPIS = "https://www.googleapis.com"
GMAIL_API = "https://gmail.googleapis.com"
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


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


async def connection(scopes: Sequence[str], account: str | None) -> PersonConnection:
    """The person this run acts for's Google connection, one that granted one of `scopes`
    (`google_connect.SCOPES` names). `Unavailable` otherwise."""
    from felix.auth.google_connect import SCOPES

    found = await person_connection("google", SOURCE, account)
    row = found.row
    if not any(SCOPES[s] in set(row["scopes"]) for s in scopes):
        raise Unavailable(scope_refusal(row["label"], scopes))
    return found


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

        token = await (await connection(scopes, account)).opened(connections.access_token)
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


def _id(value: str) -> str:
    if not _ID.match(value):
        raise ValueError("an id is letters, digits, - and _")
    return value


Id = Annotated[str, AfterValidator(_id)]


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
    "SOURCE",
    "AccountArgs",
    "GoogleApis",
    "Id",
    "connection",
    "is_id",
    "require_client",
    "scope_refusal",
]
