"""The API middleware stack: shape, order, and the limits it actually enforces.

Two of these lock down things that were previously only asserted by a comment, and
one covers a limit that silently did nothing for as long as it existed.
"""

from __future__ import annotations

import pytest
from felix.auth.middleware import AuthMiddleware
from felix.logging_setup import REQUEST_ID_HEADER
from felix_api.app import create_app
from felix_api.middleware import BodyLimitMiddleware, RateLimitMiddleware, RequestIdMiddleware
from httpx import ASGITransport, AsyncClient
from starlette.middleware.base import BaseHTTPMiddleware


def _stack(app):
    """The built ASGI chain, outermost first."""
    node = app.build_middleware_stack()
    chain = []
    seen = set()
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        chain.append(node)
        node = getattr(node, "app", None)
    return chain


def test_no_base_http_middleware_in_the_stack() -> None:
    """BaseHTTPMiddleware costs ~143us per request and ~76us per streamed token —
    measured, on a four-layer stack. `@app.middleware("http")` produces one, so this
    is what stops a convenient-looking decorator reintroducing the tax."""
    offenders = [type(n).__name__ for n in _stack(create_app()) if isinstance(n, BaseHTTPMiddleware)]
    assert offenders == [], (
        f"BaseHTTPMiddleware found in the stack: {offenders}. "
        "Write pure-ASGI middleware instead — see felix_api.middleware."
    )


def test_middleware_runtime_order() -> None:
    """Order is load-bearing and was previously documented only in a comment that had
    drifted from the code: request-id must be outermost so even a 413 carries one, and
    auth must be innermost so a request that will 401 is still rate limited."""
    chain = [type(n).__name__ for n in _stack(create_app())]
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


@pytest.mark.asyncio
async def test_oversized_chunked_body_is_rejected() -> None:
    """A chunked request carries no Content-Length, so the header check cannot see it.

    This is the case the streaming cap existed for and never handled: the previous
    implementation handed a counting receive channel to `call_next`, which ignores its
    request argument, so nothing ever read it. A 64 KiB body passed a 1 MiB limit with
    a 200 — and would have passed any limit at all.
    """
    app = create_app()
    limit = 1024 * 1024

    async def oversized():
        chunk = b"x" * 64 * 1024
        for _ in range((limit // len(chunk)) + 2):
            yield chunk

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.post(
            "/chat",
            content=oversized(),
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 413, f"expected 413, got {response.status_code}"
    assert response.json()["error"] == "payload_too_large"


@pytest.mark.asyncio
async def test_oversized_declared_body_is_rejected() -> None:
    """The Content-Length path, which did work, kept working."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.post(
            "/chat",
            content=b"x" * (2 * 1024 * 1024),
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 413
    assert response.json()["error"] == "payload_too_large"


@pytest.mark.asyncio
async def test_body_within_the_limit_still_reaches_the_route() -> None:
    """The cap must not truncate ordinary requests: a body under the limit has to
    arrive intact, or every chat request breaks."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.post("/chat", json={"manifest": "", "messages": []})
    # 400 manifest_required means the route parsed the body — which is the point.
    assert response.status_code == 400
    assert response.json()["detail"] == "manifest_required"


@pytest.mark.asyncio
async def test_request_id_is_echoed_and_generated() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        supplied = await client.get("/health", headers={REQUEST_ID_HEADER: "abc-123"})
        generated = await client.get("/health")
    assert supplied.headers[REQUEST_ID_HEADER] == "abc-123"
    assert generated.headers.get(REQUEST_ID_HEADER), "a request id should be generated when absent"


@pytest.mark.asyncio
async def test_request_id_is_present_on_a_rejected_request() -> None:
    """Request-id sat *inside* the body limiter, so a 413 — one of the responses most
    worth correlating — came back with no id at all."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.post(
            "/chat",
            content=b"x" * (2 * 1024 * 1024),
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 413
    assert response.headers.get(REQUEST_ID_HEADER), "a 413 must still carry a correlation id"
