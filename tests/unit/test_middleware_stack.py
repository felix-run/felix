"""The API middleware stack: shape, order, and the limits it actually enforces.

Two of these lock down things that were previously only asserted by a comment, and
several cover a body cap that silently did nothing for as long as it existed.
"""

from __future__ import annotations

import pytest
from felix.auth.middleware import AuthMiddleware
from felix.config import Settings
from felix.logging_setup import REQUEST_ID_HEADER
from felix_api.app import CORE_BODY_LIMIT_BYTES, create_app
from felix_api.middleware import BodyLimitMiddleware, RateLimitMiddleware, RequestIdMiddleware
from httpx import ASGITransport, AsyncClient
from starlette.middleware.base import BaseHTTPMiddleware


def _settings(name: str) -> Settings:
    """Explicit settings, so a repo `.env` cannot reach into the test.

    `redis_url=""` matters: with the ambient value these tests open a real TCP
    connection to localhost:6379 on every request, fail, and degrade through
    ResilientRateLimiter. Locally that is connection-refused and instant; in a
    network-restricted sandbox it blocks until the connect timeout.
    """
    return Settings(
        database_url=f"memory://{name}",
        object_store="memory",
        auth_mode="none",
        allow_insecure=True,
        host="127.0.0.1",
        environment="development",
        redis_url="",
    )


def _app(name: str):
    return create_app(settings=_settings(name), plugins=[])


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


def _stack(app):
    """The built ASGI chain, outermost first."""
    node = app.build_middleware_stack()
    chain = []
    seen = set()
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        chain.append(node)
        node = getattr(node, "app", None)
    # The walk follows `.app`, so a middleware storing its inner app under another
    # name would truncate the chain and quietly make the assertions below vacuous.
    assert any(type(n).__name__ == "APIRouter" for n in chain), (
        f"middleware walk did not reach the router; it stopped at {type(chain[-1]).__name__}"
    )
    return chain


def test_no_base_http_middleware_in_the_stack() -> None:
    """BaseHTTPMiddleware costs ~143us per request and ~76us per streamed token —
    measured, on a four-layer stack. `@app.middleware("http")` produces one, so this
    is what stops a convenient-looking decorator reintroducing the tax."""
    offenders = [type(n).__name__ for n in _stack(_app("stack")) if isinstance(n, BaseHTTPMiddleware)]
    assert offenders == [], (
        f"BaseHTTPMiddleware found in the stack: {offenders}. "
        "Write pure-ASGI middleware instead — see felix_api.middleware."
    )


def test_middleware_runtime_order() -> None:
    """Order is load-bearing and was previously documented only in a comment that had
    drifted from the code: request-id must be outermost so even a 413 carries one, and
    auth must be innermost so a request that will 401 is still rate limited."""
    chain = [type(n).__name__ for n in _stack(_app("order"))]
    positions = {
        cls.__name__: chain.index(cls.__name__)
        for cls in (RequestIdMiddleware, BodyLimitMiddleware, RateLimitMiddleware, AuthMiddleware)
    }
    assert (
        positions["RequestIdMiddleware"]
        < positions["BodyLimitMiddleware"]
        < positions["RateLimitMiddleware"]
        < positions["AuthMiddleware"]
    ), f"unexpected middleware order: {positions}"


async def _chunked(total: int, chunk: int = 64 * 1024):
    sent = 0
    while sent < total:
        n = min(chunk, total - sent)
        sent += n
        yield b"x" * n


@pytest.mark.asyncio
async def test_oversized_chunked_body_is_rejected() -> None:
    """A chunked request carries no Content-Length, so the header check cannot see it.

    This is the case the streaming cap existed for and never handled: the previous
    implementation handed a counting receive channel to `call_next`, which ignores its
    request argument, so nothing ever read it. A 64 KiB body passed a 1 MiB limit with
    a 200 — and would have passed any limit at all.
    """
    async with _client(_app("chunked")) as client:
        response = await client.post(
            "/chat",
            content=_chunked(CORE_BODY_LIMIT_BYTES * 2),
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 413, f"expected 413, got {response.status_code}"
    assert response.json()["error"] == "payload_too_large"


@pytest.mark.asyncio
async def test_a_body_of_exactly_the_limit_is_accepted() -> None:
    """The boundary, in both directions. Nothing else distinguishes `> limit` from
    `>= limit`, so an off-by-one here would reject a legal body and no test would say
    so. Paired with the test below."""
    async with _client(_app("atlimit")) as client:
        response = await client.post(
            "/chat",
            content=_chunked(CORE_BODY_LIMIT_BYTES),
            headers={"content-type": "application/json"},
        )
    assert response.status_code != 413, "a body of exactly the limit must not be rejected"


@pytest.mark.asyncio
async def test_a_body_one_byte_over_the_limit_is_rejected() -> None:
    async with _client(_app("overlimit")) as client:
        response = await client.post(
            "/chat",
            content=_chunked(CORE_BODY_LIMIT_BYTES + 1),
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_a_lying_content_length_does_not_defeat_the_cap() -> None:
    """A header-only cap is trivially bypassable: declare 10 bytes, send megabytes.

    The streaming counter is what closes this, and it is the reason the cap cannot
    just be a Content-Length check.
    """
    async with _client(_app("lying")) as client:
        response = await client.post(
            "/chat",
            content=_chunked(CORE_BODY_LIMIT_BYTES * 2),
            headers={"content-type": "application/json", "content-length": "10"},
        )
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_a_malformed_content_length_still_hits_the_streaming_cap() -> None:
    """The `except ValueError` branch: an unparseable header must fall through to the
    counter rather than skip the limit."""
    async with _client(_app("malformed")) as client:
        response = await client.post(
            "/chat",
            content=_chunked(CORE_BODY_LIMIT_BYTES * 2),
            headers={"content-type": "application/json", "content-length": "not-a-number"},
        )
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_body_within_the_limit_still_reaches_the_route() -> None:
    """The cap must not truncate ordinary requests: a body under the limit has to
    arrive intact, or every chat request breaks."""
    async with _client(_app("under")) as client:
        response = await client.post("/chat", json={"manifest": "", "messages": []})
    # 400 rather than 422 means the route parsed a complete body — which is the point.
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_request_id_is_echoed_and_generated() -> None:
    async with _client(_app("reqid")) as client:
        supplied = await client.get("/health", headers={REQUEST_ID_HEADER: "abc-123"})
        generated = await client.get("/health")
    assert supplied.headers[REQUEST_ID_HEADER] == "abc-123"
    assert generated.headers.get(REQUEST_ID_HEADER), "a request id should be generated when absent"


@pytest.mark.asyncio
async def test_request_id_is_present_on_a_rejected_request() -> None:
    """Request-id sat *inside* the body limiter, so a 413 — one of the responses most
    worth correlating — came back with no id at all."""
    async with _client(_app("reqid413")) as client:
        response = await client.post(
            "/chat",
            content=b"x" * (2 * CORE_BODY_LIMIT_BYTES),
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 413
    assert response.headers.get(REQUEST_ID_HEADER), "a 413 must still carry a correlation id"


@pytest.mark.asyncio
async def test_a_raising_key_resolver_does_not_take_down_the_request() -> None:
    """A plugin's rate_limit_key runs on every request, so a bug in one must degrade
    to the client-address key rather than 500 the whole surface. The now-deleted
    rate_limit_middleware factory had this guard; losing it with the factory would
    have made a plugin bug indistinguishable from an outage."""

    class _BadPlugin:
        def rate_limit_key(self, request):
            raise RuntimeError("plugin resolver is broken")

    app = create_app(settings=_settings("badresolver"), plugins=[_BadPlugin()])
    async with _client(app) as client:
        # Not /health: it is in the rate-limiter's skip list, so the resolver would
        # never run and the test would pass with or without the guard.
        response = await client.get("/live")
    assert response.status_code == 200
