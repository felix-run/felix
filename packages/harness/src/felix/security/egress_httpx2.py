"""The egress guard for httpx2, the HTTP stack the official MCP SDK's client is built on.

`felix.security.egress` guards httpx. The `mcp` SDK's Streamable HTTP client takes an
`httpx2.AsyncClient` instead -- a separate package with its own `httpcore2` pool -- so a client
handed to it needs the same guard built on that stack: connections pinned to the address that
was validated (`connect_pinned`), and every hop, redirects included, checked before it is sent
(`guard_request`). The logic is the one in `egress`; only the classes it is installed on differ.
"""

from __future__ import annotations

from typing import Any

import httpx2

# As in `egress`: `AutoBackend` is what the pool actually uses, and is not re-exported.
from httpcore2._backends.auto import AutoBackend

from felix.security.egress import (
    ROUTES_AROUND_THE_PIN,
    connect_pinned,
    guard_request,
    install_pin,
    refuse_bypass_kwargs,
)
from felix.security.ssrf import BLOCKED_MESSAGE, EgressBlocked


class _PinningBackend2(AutoBackend):
    """`egress._PinningBackend`, for httpcore2."""

    def __init__(self, *, allow_http: bool = False) -> None:
        super().__init__()
        self._allow_http = allow_http

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:
        return await connect_pinned(
            super().connect_tcp,
            host,
            port,
            allow_http=self._allow_http,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(self, *args: Any, **kwargs: Any) -> Any:
        raise EgressBlocked(BLOCKED_MESSAGE)


class GuardedAsyncTransport2(httpx2.AsyncHTTPTransport):
    """`egress.GuardedAsyncTransport`, for httpx2: the same bypass refusal, pin and per-hop checks.

    No `egress=` (an agent's `allow_hosts`) or `credentials=` yet: nothing on this stack passes
    them, and an argument nothing exercises would read as a guarantee. Add them with a caller.
    """

    _ROUTES_AROUND_THE_PIN = ROUTES_AROUND_THE_PIN

    def __init__(self, *, allow_http: bool = False, **kwargs: Any) -> None:
        refuse_bypass_kwargs(kwargs)
        super().__init__(**kwargs)
        self._allow_http = allow_http
        install_pin(self._pool, _PinningBackend2(allow_http=allow_http))

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        guard_request(request, allow_http=self._allow_http, egress=None, credentials=None)
        return await super().handle_async_request(request)


def safe_httpx2_client(
    *,
    timeout: httpx2.Timeout | float,
    allow_http: bool = False,
    headers: dict[str, str] | None = None,
) -> httpx2.AsyncClient:
    """An `httpx2.AsyncClient` that can only reach addresses the guard approved.

    Supplying a transport also keeps httpx2 from reading proxy variables from the environment,
    as it does for httpx. Redirects are not followed by the client; the MCP SDK follows a
    same-origin one itself, and that hop reaches `handle_async_request` like any other.
    """
    return httpx2.AsyncClient(
        transport=GuardedAsyncTransport2(allow_http=allow_http),
        timeout=timeout,
        headers=headers,
        follow_redirects=False,
    )


__all__ = ["GuardedAsyncTransport2", "safe_httpx2_client"]
