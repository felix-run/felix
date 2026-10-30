"""One outbound JSON GET, shared by every connector backend.

Every backend here talks to a host fixed in code (or set by the operator), never one the model
names, so the risk is the search backend's rather than the fetch tool's: the destination is
trusted and the *answer* is not. What each call still owes the harness is the same three things,
so they live here once -- the egress guard (`safe_async_client`), a whole-call deadline rather
than httpx's per-read one, and a cap on what a response may make the process buffer.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from felix.config import Settings
from felix.security.host_allowlist import url_host
from felix.timeouts import DEFAULT_CONNECT_TIMEOUT_S

DEFAULT_TIMEOUT_S = 15.0
#: A forecast for sixteen days is a few tens of kilobytes; nothing a connector reads needs more.
MAX_RESPONSE_BYTES = 1_000_000
#: Sent on every request. api.weather.gov refuses a request without one, and Nominatim's policy
#: asks for one that identifies the application; `FELIX_CONNECTORS_CONTACT` adds the operator's.
USER_AGENT = "felix-connectors (+https://github.com/felix-run/felix)"


class ResponseTooLarge(Exception):
    """A body past `MAX_RESPONSE_BYTES`. Not a `ValueError`: inside the request, that type is the
    egress guard's refusal (`get_json`)."""


class UpstreamError(Exception):
    """A backend answered with an error status. `status` picks the tool error code; the body is
    the upstream's own text and never reaches the model."""

    def __init__(self, status: int) -> None:
        super().__init__(f"upstream answered {status}")
        self.status = status


class QuotaSpent(Exception):
    """The upstream refused because the operator's plan quota is spent -- said with a 200 and a
    note by some (Alpha Vantage), so a status code would misreport it."""


class KeyRefused(Exception):
    """The upstream refused the operator's key -- said with a 200 and a note by some, so a
    status code would misreport it. The operator's to fix; never the model's to retry around."""


class Busy(Exception):
    """A `Pacer` would hold the call past its longest wait: the upstream's rate is spent."""


class Pacer:
    """Spaces calls to one upstream at least `interval_s` apart, in this process.

    For the public instances whose usage policy sets a rate (Nominatim's is one request a
    second): each call reserves the next free slot before it awaits, so concurrent calls queue
    rather than burst. A slot further off than `max_wait_s` raises `Busy` instead of sleeping, so
    a queue the model built cannot hold a turn, and reserves nothing. Per process, not per
    deployment, and shared by every tenant on it: an install with several API workers sends that
    many times the rate, and a busy one should run its own instance, which is not paced.
    """

    def __init__(self, interval_s: float, *, max_wait_s: float = 5.0) -> None:
        self._interval = interval_s
        self._max_wait = max_wait_s
        self._next = 0.0

    async def wait(self) -> None:
        now = time.monotonic()
        at = max(now, self._next)
        if at - now > self._max_wait:
            raise Busy(f"next slot in {at - now:.1f}s")
        self._next = at + self._interval
        if at <= now:
            return
        try:
            await asyncio.sleep(at - now)
        except asyncio.CancelledError:
            # A turn cancelled in the queue gives its slot back when nothing has queued behind it.
            if self._next == at + self._interval:
                self._next = at
            raise


#: The public instances whose usage policy sets a rate, by host: one `Pacer` each per process,
#: whatever builds the backend, and nothing for a host not listed -- an operator's own instance.
_PACERS: dict[str, Pacer] = {}


def pace_host(host: str, interval_s: float, *, max_wait_s: float = 5.0) -> None:
    """Hold every request to `host` from this process at least `interval_s` apart."""
    _PACERS[host.lower()] = Pacer(interval_s, max_wait_s=max_wait_s)


def user_agent(settings: Settings) -> str:
    # `Settings` refuses a contact that is not printable ASCII, so it cannot end the header.
    contact = settings.connectors_contact
    return f"{USER_AGENT} {contact}" if contact else USER_AGENT


def allow_http(settings: Settings) -> bool:
    """The development exemption every outbound integration gets, and nothing wider."""
    return settings.environment == "development" and settings.allow_insecure


async def get_json(
    url: str,
    *,
    params: Mapping[str, str],
    headers: Mapping[str, str],
    hosts: Iterable[str],
    allow_http: bool = False,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Any:
    """`url` fetched through the egress guard and parsed, or `UpstreamError` for a non-2xx.

    `hosts` is what the calling backend declares it reaches (`Tool.egress_hosts`), and the guard
    holds the request to it: the compile checks the declaration against the agent's
    `allow_hosts`, and this makes the declaration true. `ValueError` for a body over
    `MAX_RESPONSE_BYTES` (`ResponseTooLarge`) or not JSON (`ValueError`); `EgressBlocked` when the
    guard refuses the destination; `TimeoutError` once `timeout_s` has passed, however the upstream
    paces its bytes; `Busy` when the host is paced (`pace_host`) and has no slot soon enough.
    The wait for a slot is not counted against `timeout_s`."""
    body = await send(
        "GET", url, params=params, headers=headers, hosts=hosts, allow_http=allow_http, timeout_s=timeout_s
    )
    return json.loads(body)


async def send(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    hosts: Iterable[str],
    params: Mapping[str, str] | None = None,
    content: bytes | None = None,
    allow_http: bool = False,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> bytes:
    """One request through the egress guard, its body returned raw: what `get_json` does, for any
    method and for a body that is not JSON (a file's bytes). The same bound, deadline and cap."""
    from felix.security.host_allowlist import EgressBound, HostAllowlist, normalise_host_pattern
    from felix.security.ssrf import BLOCKED_MESSAGE, EgressBlocked

    declared = EgressBound((HostAllowlist(tuple(normalise_host_pattern(h) for h in hosts)),))
    # Identity: the cap counts decoded bytes, and one compressed chunk can expand past it at once.
    sent = {**headers, "accept-encoding": "identity"}
    pacer = _PACERS.get(url_host(url))
    if pacer is not None:
        await pacer.wait()
    try:
        async with asyncio.timeout(timeout_s):
            return await _fetch(
                method,
                url,
                params or {},
                sent,
                content=content,
                egress=declared,
                allow_http=allow_http,
                timeout_s=timeout_s,
            )
    except EgressBlocked:
        raise
    except ValueError:
        # The guard's address checks (`ssrf.assert_safe_outbound_url`) raise a bare `ValueError`
        # naming the address; nothing else in the request does. One fixed refusal, as the fetch
        # tool gives, so a caller cannot tell "loopback" from "private" from "not allowed".
        raise EgressBlocked(BLOCKED_MESSAGE) from None


@dataclass(frozen=True, slots=True)
class Upstream:
    """One HTTP API a backend reads: where, as whom, and so the one host it may reach.

    `hosts` is derived from `base_url` rather than written beside it, so what a backend declares
    (`Tool.egress_hosts`) and where its requests go cannot be two separate claims.
    """

    base_url: str
    user_agent: str
    allow_http: bool = False
    accept: str = "application/json"
    #: Sent on every request beside the User-Agent: a key's header. Never in the repr.
    headers: Mapping[str, str] = field(default_factory=dict, repr=False, compare=False)
    hosts: frozenset[str] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))
        object.__setattr__(self, "hosts", frozenset({url_host(self.base_url)}))

    async def get(self, path: str, params: Mapping[str, str]) -> Any:
        """`path` (with its leading slash) under `base_url`, through `get_json`."""
        return await get_json(
            self.base_url + path,
            params=params,
            headers={**self.headers, "user-agent": self.user_agent, "accept": self.accept},
            hosts=self.hosts,
            allow_http=self.allow_http,
        )


async def _fetch(
    method: str,
    url: str,
    params: Mapping[str, str],
    headers: dict[str, str],
    *,
    content: bytes | None,
    egress: Any,
    allow_http: bool,
    timeout_s: float,
) -> bytes:
    import httpx
    from felix.security.egress import safe_async_client

    timeout = httpx.Timeout(timeout_s, connect=min(DEFAULT_CONNECT_TIMEOUT_S, timeout_s))
    async with (
        safe_async_client(timeout=timeout, allow_http=allow_http, egress=egress) as client,
        # No `params` at all when there are none: httpx replaces a URL's own query string with
        # whatever `params` holds, so an empty mapping would strip the one Google's client built.
        client.stream(method, url, params=dict(params) or None, headers=headers, content=content) as resp,
    ):
        # A redirect is never followed (`safe_async_client`), so a 3xx is no answer either: taken
        # as one, a PUT moved elsewhere would read as written.
        if not 200 <= resp.status_code < 300:
            raise UpstreamError(resp.status_code)
        return await _read_capped(resp)


async def _read_capped(resp: Any) -> bytes:
    """The body, streamed, refused past `MAX_RESPONSE_BYTES` before it is all held."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.aiter_bytes():
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise ResponseTooLarge(f"response exceeded {MAX_RESPONSE_BYTES} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "MAX_RESPONSE_BYTES",
    "USER_AGENT",
    "Busy",
    "KeyRefused",
    "Pacer",
    "QuotaSpent",
    "ResponseTooLarge",
    "Upstream",
    "UpstreamError",
    "allow_http",
    "get_json",
    "pace_host",
    "send",
    "user_agent",
]
