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
from collections.abc import Iterable, Mapping
from typing import Any

from felix.config import Settings
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
    paces its bytes."""
    from felix.security.host_allowlist import EgressBound, HostAllowlist, normalise_host_pattern
    from felix.security.ssrf import BLOCKED_MESSAGE, EgressBlocked

    declared = EgressBound((HostAllowlist(tuple(normalise_host_pattern(h) for h in hosts)),))
    # Identity: the cap counts decoded bytes, and one compressed chunk can expand past it at once.
    sent = {**headers, "accept-encoding": "identity"}
    try:
        async with asyncio.timeout(timeout_s):
            body = await _fetch(
                url, params, sent, egress=declared, allow_http=allow_http, timeout_s=timeout_s
            )
    except EgressBlocked:
        raise
    except ValueError:
        # The guard's address checks (`ssrf.assert_safe_outbound_url`) raise a bare `ValueError`
        # naming the address; nothing else in the request does. One fixed refusal, as the fetch
        # tool gives, so a caller cannot tell "loopback" from "private" from "not allowed".
        raise EgressBlocked(BLOCKED_MESSAGE) from None
    return json.loads(body)


async def _fetch(
    url: str,
    params: Mapping[str, str],
    headers: dict[str, str],
    *,
    egress: Any,
    allow_http: bool,
    timeout_s: float,
) -> bytes:
    import httpx
    from felix.security.egress import safe_async_client

    timeout = httpx.Timeout(timeout_s, connect=min(DEFAULT_CONNECT_TIMEOUT_S, timeout_s))
    async with (
        safe_async_client(timeout=timeout, allow_http=allow_http, egress=egress) as client,
        client.stream("GET", url, params=dict(params), headers=headers) as resp,
    ):
        if resp.status_code >= 400:
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
    "ResponseTooLarge",
    "UpstreamError",
    "allow_http",
    "get_json",
    "user_agent",
]
