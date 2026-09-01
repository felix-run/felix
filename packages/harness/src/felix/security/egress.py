"""Outbound HTTP that connects only to an address the guard has approved.

`assert_safe_outbound_url` is advisory: it resolves a hostname, checks the answers, throws
them away, and lets httpx resolve again independently at connect. Two things get through.

A hostname can resolve differently the second time — classic DNS rebinding, and a TTL of
zero is free to publish. And a nameserver that answers the two lookups differently does not
even need the timing: `resolve_host` returning empty is treated as "defer to the
connection", so dropping the guard's query while answering the client's is a complete
bypass with no race at all.

The fix is to stop having two lookups. This backend resolves once, validates every address,
and connects to one of the approved ones — so the address that was checked is the address
that is used. TLS is unaffected: httpcore passes the *origin* hostname to `start_tls`
regardless of what `connect_tcp` was given, so SNI and certificate verification still run
against the name the caller asked for.
"""

from __future__ import annotations

import asyncio
import ipaddress
from typing import Any

import httpcore
import httpx

# `AutoBackend` picks asyncio vs trio at first use and is not re-exported from the package
# root; importing it from its module is the only way to subclass what httpx actually uses.
from httpcore._backends.auto import AutoBackend

# Imported as a module, not by value: `resolve_host` is monkeypatched on `ssrf` throughout
# the suite, and a by-value import would silently ignore those patches.
from felix.security import ssrf
from felix.security.ssrf import EgressBlocked


class _PinningBackend(AutoBackend):
    """Resolves, validates, and connects to the address it validated."""

    def __init__(self, *, allow_http: bool = False) -> None:
        super().__init__()
        self._allow_http = allow_http

    async def _approved_address(self, host: str) -> str:
        literal = ssrf._parse_ip_literal(host)
        if literal is not None:
            candidates = [str(literal)]
        else:
            # `getaddrinfo` is synchronous and this runs on the event loop, so it goes to
            # a thread on the same budget as the pre-dial check. Without this the fix for
            # the validator stall would be undone one layer down.
            candidates = await asyncio.wait_for(
                asyncio.to_thread(ssrf.resolve_host, host), timeout=ssrf._RESOLVE_BUDGET_S
            )
        if not candidates:
            # Fail closed. The advisory guard deferred here because a lookup failure meant
            # the connection would fail anyway — but this *is* the connection, and a
            # resolver that answers the client while starving the checker is exactly the
            # bypass this backend exists to remove.
            raise EgressBlocked("egress_blocked: destination could not be verified")

        approved: str | None = None
        for addr in candidates:
            try:
                ip = ipaddress.ip_address(addr)
            except ValueError:
                continue
            reason = ssrf._is_blocked_ip(ip)
            if reason and not (self._allow_http and ip.is_loopback):
                # One bad answer refuses the whole name: a round-robin that includes a
                # private address must not be reachable by retrying.
                raise EgressBlocked("egress_blocked: destination not permitted")
            if approved is None:
                approved = addr
        if approved is None:
            raise EgressBlocked("egress_blocked: destination could not be verified")
        return approved

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            approved = await self._approved_address(host)
        except TimeoutError:
            raise EgressBlocked("egress_blocked: destination could not be verified") from None
        return await super().connect_tcp(
            approved,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


class GuardedAsyncTransport(httpx.AsyncHTTPTransport):
    """`AsyncHTTPTransport` whose connections are pinned to a validated address.

    httpx builds its own `AsyncConnectionPool` and does not expose `network_backend`, so the
    backend is swapped on the constructed pool. That is one private attribute rather than a
    reimplementation of httpx's pool wiring (ssl context, limits, http1/http2, proxies), and
    a test asserts the attribute still exists so a httpcore rename fails loudly here rather
    than silently returning an unguarded client.
    """

    def __init__(self, *, allow_http: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if not hasattr(self._pool, "_network_backend"):  # pragma: no cover - guarded by test
            raise RuntimeError(
                "httpcore connection pool has no _network_backend; the egress guard cannot "
                "be installed and an unguarded client must not be returned"
            )
        self._pool._network_backend = _PinningBackend(allow_http=allow_http)


def safe_async_client(
    *,
    timeout: httpx.Timeout | float,
    allow_http: bool = False,
    **kwargs: Any,
) -> httpx.AsyncClient:
    """An `httpx.AsyncClient` that can only reach addresses the guard approved.

    Use this for every outbound call to a manifest- or model-supplied URL.

    `timeout` is required rather than defaulted: httpx falls back to a silent 5s, which is
    wrong in both directions here — too short for a slow peer, and invisible when it bites.
    `follow_redirects` defaults to False, as at every other outbound call site: a redirect is
    a fresh destination and belongs to the caller to decide about.
    """
    kwargs.setdefault("follow_redirects", False)
    return httpx.AsyncClient(
        transport=GuardedAsyncTransport(allow_http=allow_http),
        timeout=timeout,
        **kwargs,
    )


__all__ = ["GuardedAsyncTransport", "safe_async_client"]
