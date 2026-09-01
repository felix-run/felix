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
import logging
from typing import Any

import httpcore
import httpx

# `AutoBackend` picks asyncio vs trio at first use and is not re-exported from the package
# root; importing it from its module is the only way to subclass what httpx actually uses.
from httpcore._backends.auto import AutoBackend

# Imported as a module, not by value: `resolve_host` is monkeypatched on `ssrf` throughout
# the suite, and a by-value import would silently ignore those patches.
from felix.observability.metrics import record_counter
from felix.security import ssrf
from felix.security.ssrf import EgressBlocked

logger = logging.getLogger("felix.security.egress")

# One message for every refusal. Distinguishing "does not resolve" from "resolves into
# private space" would hand the caller a one-bit probe for internal names, and this string
# reaches the model through the tool-error path.
_BLOCKED_MESSAGE = "egress_blocked: destination not permitted"


def _refuse(host: str, address: str, reason: str) -> None:
    """Log the detail an operator needs, raise the one the caller may see."""
    logger.warning("egress blocked host=%s address=%s reason=%s", host, address, reason)
    record_counter("felix_egress_blocked", {"reason": reason[:40]})
    raise EgressBlocked(_BLOCKED_MESSAGE)


class _PinningBackend(AutoBackend):
    """Resolves, validates, and connects to the address it validated."""

    def __init__(self, *, allow_http: bool = False) -> None:
        super().__init__()
        self._allow_http = allow_http

    async def _approved_addresses(self, host: str, timeout: float | None = None) -> list[str]:
        literal = ssrf._parse_ip_literal(host)
        if literal is not None:
            candidates = [str(literal)]
        else:
            # `getaddrinfo` is synchronous and this runs on the event loop, so it goes to
            # a thread on the same budget as the pre-dial check. Without this the fix for
            # the validator stall would be undone one layer down.
            # Bounded by the smaller of the resolve budget and the caller's connect
            # timeout, so resolution cannot stack a second ceiling on top of the dial.
            # asyncio rather than anyio: `AutoBackend` also selects a trio backend, but
            # Felix is asyncio-only and this fails closed under trio rather than open.
            budget = min(ssrf._RESOLVE_BUDGET_S, timeout) if timeout else ssrf._RESOLVE_BUDGET_S
            candidates = await asyncio.wait_for(asyncio.to_thread(ssrf.resolve_host, host), timeout=budget)
        if not candidates:
            # Fail closed. The advisory guard deferred here because a lookup failure meant
            # the connection would fail anyway — but this *is* the connection, and a
            # resolver that answers the client while starving the checker is exactly the
            # bypass this backend exists to remove.
            _refuse(host, "", "resolution returned no addresses")

        approved: list[str] = []
        for addr in candidates:
            try:
                ip = ipaddress.ip_address(addr)
            except ValueError:
                continue
            reason = ssrf._is_blocked_ip(ip)
            if reason and not (self._allow_http and ip.is_loopback):
                # One bad answer refuses the whole name: a round-robin that includes a
                # private address must not be reachable by retrying.
                _refuse(host, addr, reason)
            approved.append(addr)
        if not approved:
            _refuse(host, "", "no usable address")
        # All of them, not just the first. anyio races the addresses a name resolves to —
        # that is Happy Eyeballs, and it is what lets a host with an AAAA record work from a
        # container with no IPv6 egress. Pinning one address discards that fallback; every
        # address here has been validated, so trying them in turn keeps the property.
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
            approved = await self._approved_addresses(host, timeout)
        except TimeoutError:
            _refuse(host, "", "resolution timed out")

        # Try each approved address in turn. anyio races the addresses a name resolves to —
        # Happy Eyeballs — which is what lets a host with an AAAA record work from a
        # container that has no IPv6 egress. Pinning a single address throws that fallback
        # away; every address here has already been validated, so trying them in order keeps
        # the security property and the reachability.
        last: OSError | None = None
        for addr in approved:
            try:
                return await super().connect_tcp(
                    addr,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except OSError as exc:
                last = exc
        raise last if last is not None else EgressBlocked(_BLOCKED_MESSAGE)

    async def connect_unix_socket(self, *args: Any, **kwargs: Any) -> httpcore.AsyncNetworkStream:
        # A unix socket has no address to validate, and the one a container gateway would
        # reach for is the Docker socket. Nothing in the harness needs it.
        raise EgressBlocked(_BLOCKED_MESSAGE)


class GuardedAsyncTransport(httpx.AsyncHTTPTransport):
    """`AsyncHTTPTransport` whose connections are pinned to a validated address.

    httpx builds its own `AsyncConnectionPool` and does not expose `network_backend`, so the
    backend is swapped on the constructed pool. That is one private attribute rather than a
    reimplementation of httpx's pool wiring (ssl context, limits, http1/http2, proxies), and
    a test asserts the attribute still exists so a httpcore rename fails loudly here rather
    than silently returning an unguarded client.
    """

    # Every one of these dials somewhere the backend never sees. A proxied connection
    # dials the proxy and lets *it* choose the destination, which is a bypass by
    # construction, and `mounts` takes precedence over the transport entirely.
    _ROUTES_AROUND_THE_PIN = ("proxy", "mounts", "uds")

    def __init__(self, *, allow_http: bool = False, **kwargs: Any) -> None:
        for name in self._ROUTES_AROUND_THE_PIN:
            if kwargs.get(name) is not None:
                raise TypeError(
                    f"{name}= would bypass the egress guard; a guarded client cannot proxy "
                    "or use a unix socket"
                )
        super().__init__(**kwargs)
        if not hasattr(self._pool, "_network_backend"):  # pragma: no cover - guarded by test
            raise RuntimeError(
                "httpcore connection pool has no _network_backend; the egress guard cannot "
                "be installed and an unguarded client must not be returned"
            )
        self._allow_http = allow_http
        self._pool._network_backend = _PinningBackend(allow_http=allow_http)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # The backend only sees resolved addresses, so it enforces the IP half of the guard
        # and nothing else. Running the syntactic half here rather than at each call site
        # means a new caller cannot get half a guard by forgetting a line, and a redirect
        # target is checked on the same terms as the original URL.
        ssrf.assert_safe_outbound_url(str(request.url), allow_http=self._allow_http, resolve=False)
        return await super().handle_async_request(request)


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
    # Checked here as well as on the transport: `proxy` and `mounts` are client-level
    # arguments, so the transport never sees them and would return a guarded object wired
    # into an unguarded path.
    for name in GuardedAsyncTransport._ROUTES_AROUND_THE_PIN:
        if kwargs.get(name) is not None:
            raise TypeError(
                f"{name}= would bypass the egress guard; a guarded client cannot proxy or use a unix socket"
            )
    kwargs.setdefault("follow_redirects", False)
    return httpx.AsyncClient(
        transport=GuardedAsyncTransport(allow_http=allow_http),
        timeout=timeout,
        **kwargs,
    )


__all__ = ["GuardedAsyncTransport", "safe_async_client"]
