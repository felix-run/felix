"""Pure-ASGI middleware for the Felix API.

These were `@app.middleware("http")` functions, which Starlette implements with
`BaseHTTPMiddleware`: a task group, an `anyio.Event` and a zero-buffer memory object
stream per request, with every response chunk handed across that stream. Measured on
this repo, each layer cost ~143us per request, and four layers turned a 500-chunk SSE
response from 0.54ms into 38.79ms — 76.5us of pure scheduling per streamed token, per
open connection. Felix is an SSE-first harness, so that was the tax it paid most.

None of these needs a `Request` body or the response body; they read headers, set a
contextvar, or short-circuit with a response. That is what pure-ASGI middleware is
for, and four pure-ASGI layers measured indistinguishable from zero.

`BaseHTTPMiddleware` also silently broke the body limit — see `BodyLimitMiddleware`.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi.responses import JSONResponse
from felix.config import Settings
from felix.logging_setup import REQUEST_ID_HEADER, new_request_id, reset_request_id, set_request_id
from felix.security.rate_limit import RateLimitConfig, check_rate_limit, client_key, should_skip_rate_limit
from starlette.datastructures import Headers
from starlette.requests import ClientDisconnect, Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# A plugin-supplied key function: returns a rate-limit bucket for a request, or
# None to let the next resolver (ultimately the client address) decide.
RateLimitKeyResolver = Callable[[Request], str | None]


class RequestIdMiddleware:
    """Stamp every request and response with a correlation id.

    Registered outermost so the id covers auth, rate limiting and body limiting too —
    a request rejected by any of those is exactly the one worth correlating. It was
    previously registered *inside* the body limiter despite a comment claiming
    otherwise, so a 413 came back with no id at all.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER, "").strip()
        request_id = incoming[:64] or new_request_id()
        scope.setdefault("state", {})["request_id"] = request_id
        raw_header = (REQUEST_ID_HEADER.encode("latin-1"), request_id.encode("latin-1"))

        async def send_with_id(message: Message) -> None:
            # Only the start message carries headers; body chunks pass straight
            # through, which is what keeps this cheap on a streaming response.
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append(raw_header)
            await send(message)

        token = set_request_id(request_id)
        try:
            await self.app(scope, receive, send_with_id)
        finally:
            reset_request_id(token)


class RateLimitMiddleware:
    """Per-client rate limiting, outside auth so failed credentials are throttled too."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        config: RateLimitConfig,
        settings: Settings,
        key_resolvers: list[RateLimitKeyResolver] | None = None,
    ) -> None:
        self.app = app
        self.config = config
        self.settings = settings
        self.key_resolvers: list[RateLimitKeyResolver] = list(key_resolvers or [])

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or should_skip_rate_limit(scope["path"]):
            await self.app(scope, receive, send)
            return

        request = Request(scope)  # headers and client only; never reads the body
        key: str | None = None
        for resolver in self.key_resolvers:
            key = resolver(request)
            if key:
                break
        if not key:
            key = client_key(request, self.settings)

        if not await check_rate_limit(key, self.config):
            response = JSONResponse(
                {"error": "rate_limited"},
                status_code=429,
                headers={"retry-after": str(self.config.window_seconds)},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class BodyLimitMiddleware:
    """Reject oversized request bodies, including chunked ones.

    The previous version wrapped the request in a counting receive channel and passed
    the wrapper to `call_next`. `BaseHTTPMiddleware.call_next` ignores its `request`
    argument entirely — it closes over the outer scope and receive — so the counting
    channel was never read by anything and the cap did nothing. A 64 KiB chunked body
    passed a 1 KiB limit with a 200. Content-Length was still checked, so this was not
    a total bypass, but any request without that header was read unbounded.

    In pure ASGI the receive channel is ours to hand down, so the cap is real.
    """

    def __init__(self, app: ASGIApp, *, limit: int) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            try:
                if int(declared) > self.limit:
                    await _too_large()(scope, receive, send)
                    return
            except ValueError:
                # A malformed Content-Length is not a reason to skip the limit; fall
                # through to the streaming cap below.
                pass

        seen = 0
        exceeded = False
        response_forwarded = False

        async def capped_receive() -> Message:
            nonlocal seen, exceeded
            message = await receive()
            if message.get("type") == "http.request":
                seen += len(message.get("body") or b"")
                if seen > self.limit:
                    exceeded = True
                    # Cut the body off rather than raising through the receive
                    # channel: an exception there unwinds inside the route and
                    # surfaces as a 500, not a 413.
                    return {"type": "http.disconnect"}
            return message

        async def watched_send(message: Message) -> None:
            nonlocal response_forwarded
            if exceeded and not response_forwarded:
                # Swallow it. Cutting the body off makes the route fail on its own
                # terms — FastAPI turns the resulting ClientDisconnect into a 400
                # "error parsing the body" — and that answer is both wrong and
                # confusing. The 413 below replaces it. Once a response has genuinely
                # started (an app that answered before reading the body) there is
                # nothing to replace, so it passes through untouched.
                return
            if message["type"] == "http.response.start":
                response_forwarded = True
            await send(message)

        try:
            await self.app(scope, capped_receive, watched_send)
        except ClientDisconnect:
            # Expected when the route was reading the body we just cut off.
            if not exceeded:
                raise
        if exceeded and not response_forwarded:
            # The raw `send`, deliberately: `watched_send` would swallow this too.
            await _too_large()(scope, receive, send)


def _too_large() -> JSONResponse:
    return JSONResponse({"error": "payload_too_large"}, status_code=413)


__all__ = ["BodyLimitMiddleware", "RateLimitMiddleware", "RequestIdMiddleware"]
