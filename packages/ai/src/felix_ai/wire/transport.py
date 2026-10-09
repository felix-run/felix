"""HTTP transport shared by every wire format: retry, backoff, and the gateway error.

This was private (`post_with_retry` and friends, excluded from `__all__`), which meant a
third-party provider had to re-derive retry-on-429, `Retry-After` handling, the
spent-quota distinction and the timeout policy — and everything it got wrong there failed
open on `limits.max_cost_usd`. It is public now.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import weakref
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager, suppress
from typing import Any

import httpx

from felix_ai.observability import record_counter

logger = logging.getLogger("felix_ai.wire.transport")

# Connect is TCP-establish plus TLS, not work. Reaching a host takes seconds or never, so it
# must not scale with the request ceiling: otherwise raising a timeout to accommodate one
# slow response also lets an unreachable host park a socket for that long, and connect
# failures are the class worth retrying. Mirrors `felix.timeouts.DEFAULT_CONNECT_TIMEOUT_S`;
# duplicated rather than imported because this package may not import the harness.
DEFAULT_CONNECT_TIMEOUT_S = 10.0

# How long an idle pooled connection to a provider is kept. httpx's default is 5s, which is
# shorter than the gap between two steps of a tool loop, so the second step reconnected anyway.
SHARED_KEEPALIVE_EXPIRY_S = 60.0


class _SharedTransport(httpx.AsyncBaseTransport):
    """A pooled transport that outlives the `AsyncClient` wrapping it.

    Every model call used to open its own `httpx.AsyncClient`, so every turn, judge, screen
    and decision paid a TCP connect and a TLS handshake, and built a fresh SSL context on
    the event loop. Call sites keep their `async with httpx.AsyncClient(...)` — the timeout
    stays per call, and tests that replace `httpx.AsyncClient` keep working — and pass this
    as ``transport=``. The client's exit closes its transport; this one ignores that, so the
    pool survives until `aclose_shared_transports`.
    """

    def __init__(self) -> None:
        # No connection cap: a stream holds its connection for the whole turn, and one
        # client per call never had a cap either. A bounded pool would queue the 101st
        # concurrent turn behind a pool timeout.
        self._inner = httpx.AsyncHTTPTransport(
            limits=httpx.Limits(
                max_connections=None,
                max_keepalive_connections=64,
                keepalive_expiry=SHARED_KEEPALIVE_EXPIRY_S,
            )
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        """Deliberately nothing: the per-call client closing must not close the pool."""

    async def close_pool(self) -> None:
        await self._inner.aclose()


# One pool per event loop: httpcore's connections are bound to the loop that opened them, and
# a test suite (or a worker running `asyncio.run` per task) has more than one.
_SHARED: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _SharedTransport] = weakref.WeakKeyDictionary()


def shared_transport() -> httpx.AsyncBaseTransport:
    """The connection pool model calls on the running loop share; see `_SharedTransport`."""
    loop = asyncio.get_running_loop()
    transport = _SHARED.get(loop)
    if transport is None:
        transport = _SharedTransport()
        _SHARED[loop] = transport
    return transport


async def aclose_shared_transports() -> None:
    """Close the running loop's shared pool. Called from API and worker shutdown."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    transport = _SHARED.pop(loop, None)
    if transport is not None:
        await transport.close_pool()


class ModelGatewayError(Exception):
    """An upstream model provider returned an error response.

    ``str(exc)`` is relayed to API clients verbatim by both `/chat` and
    `/v1/chat/completions`, so the provider's response body is deliberately kept **out**
    of the message: it can carry provider request ids, organization identifiers, quota
    and billing detail, and echoed request content. The body is retained on ``.body``
    for server-side logging only.
    """

    def __init__(self, label: str, status: int, body: str) -> None:
        super().__init__(f"{label} provider returned HTTP {status}")
        self.status = status
        self.label = label
        self.body = (body or "")[:2000]
        self.name = "ModelGatewayError"


class ModelUnreachableError(ModelGatewayError):
    """The provider never answered: refused connection, DNS failure, or a timeout.

    A `ModelGatewayError` rather than the bare `httpx` exception, because the routes relay
    only typed errors to the client: a model endpoint that is not running reached the chat UI as
    `internal error (request …)`, which reads as a Felix bug rather than a dead endpoint.
    The status is the one a gateway would answer with, which also lets a fallback chain
    advance past it — `_is_provider_error` treats 5xx as the provider's fault.

    The message names the exception class only. `str(exc)` can carry the endpoint URL, and
    a self-hosted endpoint is internal topology; it goes to `.body` for the log.
    """

    def __init__(self, label: str, exc: httpx.TransportError) -> None:
        status = 504 if isinstance(exc, httpx.TimeoutException) else 503
        super().__init__(label, status, f"{type(exc).__name__}: {exc}")
        self.args = (f"{label} provider unreachable ({type(exc).__name__})",)


@contextmanager
def typed_transport_errors(label: str) -> Iterator[None]:
    """Raise a transport failure inside the block as `ModelUnreachableError`.

    Public for the same reason `post_with_retry` is: a provider that does not subclass
    `HttpModelClient` would otherwise leak the bare `httpx` exception, which the routes
    answer as `internal error` and a fallback chain does not advance past.
    """
    try:
        yield
    except httpx.TransportError as exc:
        raise ModelUnreachableError(label, exc) from exc


# Retried statuses: rate limiting and transient upstream failures. 4xx other than these
# will not succeed on a retry, so retrying them just burns latency. 529 is "overloaded" —
# Anthropic and TypeSafe both send it, and both document it as retry-after-a-moment.
_RETRY_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
MODEL_MAX_RETRIES = 2  # three attempts total
_BASE_BACKOFF_S = 0.5
_MAX_BACKOFF_S = 20.0

# A 429 has two very different causes. Transient overload clears on its own and is worth
# a retry; a spent quota or a billing problem does not clear within a request, so retrying
# only adds latency to a failure the caller is going to see anyway.
_HARD_LIMIT_MARKERS = (
    "insufficient_quota",
    "insufficient quota",
    "billing",
    "payment",
    "credit balance",
    "exceeded your current quota",
    "monthly usage limit",
    "spending limit",
    "account is not active",
)


def _is_exhausted_quota(resp: Any) -> bool:
    """True when a rate-limit response reflects a spent budget rather than backpressure."""
    try:
        body = (resp.text or "").lower()
    except Exception:
        return False
    return any(marker in body for marker in _HARD_LIMIT_MARKERS)


def _retry_after_seconds(resp: Any) -> float | None:
    """Honour the provider's own Retry-After, in seconds or as an HTTP date."""
    raw = ""
    try:
        raw = (resp.headers.get("retry-after") or "").strip()
    except Exception:
        return None
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(raw)
        if when is None:
            return None
        delta = when.timestamp() - time.time()
        return max(0.0, delta)
    except Exception:
        return None


def _backoff_delay(attempt: int, retry_after: float | None) -> float:
    """Exponential backoff with jitter, never shorter than the provider asked for."""
    if retry_after is not None:
        return min(retry_after, _MAX_BACKOFF_S)
    base = min(_BASE_BACKOFF_S * (2**attempt), _MAX_BACKOFF_S)
    # Jitter spreads retries from concurrent runs; not a security decision.
    return base + random.uniform(0, base / 2)


def _retry_delay(resp: Any, attempt: int, max_retries: int, label: str) -> float | None:
    """Seconds to wait before retrying `resp`, or None when it is the answer to return."""
    if resp.status_code not in _RETRY_STATUSES or attempt >= max_retries:
        return None
    if resp.status_code == 429 and _is_exhausted_quota(resp):
        logger.warning("%s rate limit is a spent quota, not backpressure; not retrying", label)
        record_counter("felix_model_retry_skipped", {"provider": label, "reason": "quota"})
        return None
    delay = _backoff_delay(attempt, _retry_after_seconds(resp))
    record_counter("felix_model_retry", {"provider": label, "status": str(resp.status_code)})
    logger.warning(
        "%s returned %s; retrying in %.1fs (attempt %d/%d)",
        label,
        resp.status_code,
        delay,
        attempt + 1,
        max_retries,
    )
    return delay


def _record_mid_request_timeout(label: str) -> None:
    # Not backpressure. The bytes were accepted (or are still going out) and a retry
    # re-sends identical input to wait out an identical ceiling, so this used to cost
    # three full timeouts before surfacing. Fail once and say so — the fix is a larger
    # FELIX_MODEL_TIMEOUT_SECONDS, not another attempt.
    #
    # ConnectTimeout is deliberately NOT caught with it: nothing was accepted, the far side
    # may be briefly unreachable, and the next attempt is a genuinely different bet.
    record_counter("felix_model_timeout", {"provider": label})
    logger.warning(
        "%s timed out mid-request; not retrying — raise FELIX_MODEL_TIMEOUT_SECONDS "
        "if this request is legitimately long",
        label,
    )


async def post_with_retry(
    client: Any,
    url: str,
    *,
    label: str,
    json: dict[str, Any],
    headers: dict[str, str],
    max_retries: int = MODEL_MAX_RETRIES,
) -> Any:
    """POST, retrying rate limits and transient upstream failures.

    There was no retry anywhere in this layer: `_is_provider_error` existed but was only
    consulted by `_FallbackClient` to advance to the next *model*, and with no
    `spec.fallbacks` configured — the default in every bundled manifest — a single 429
    failed the whole run.
    """
    last: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = await client.post(url, json=json, headers=headers)
        except httpx.ReadTimeout, httpx.WriteTimeout:
            _record_mid_request_timeout(label)
            raise
        except httpx.HTTPError as exc:
            last = exc
            if attempt >= max_retries:
                raise
            await asyncio.sleep(_backoff_delay(attempt, None))
            continue
        delay = _retry_delay(resp, attempt, max_retries, label)
        if delay is None:
            return resp
        await asyncio.sleep(delay)
    raise last if last is not None else RuntimeError("unreachable")


@asynccontextmanager
async def stream_with_retry(
    client: Any,
    url: str,
    *,
    label: str,
    json: dict[str, Any],
    headers: dict[str, str],
    max_retries: int = MODEL_MAX_RETRIES,
) -> AsyncIterator[Any]:
    """Open a streamed POST with `post_with_retry`'s policy, then yield the response.

    Retries happen only while opening — before a byte of the body has reached the caller —
    so nothing is ever replayed into a stream someone is already reading. Streaming is the
    default path for an interactive turn, and it had no retry at all: one 429 or 529 ended
    the turn that the non-streaming path would have recovered.
    """
    for attempt in range(max_retries + 1):
        # One exit stack per attempt: whatever ends an attempt — a retry, a cancellation
        # while reading a rejected body, an error in the policy — closes its stream. The
        # pool outlives the client now, so an unexited stream would hold its connection.
        async with AsyncExitStack() as stack:
            try:
                resp = await stack.enter_async_context(client.stream("POST", url, json=json, headers=headers))
            except httpx.ReadTimeout, httpx.WriteTimeout:
                _record_mid_request_timeout(label)
                raise
            except httpx.HTTPError:
                if attempt >= max_retries:
                    raise
                await asyncio.sleep(_backoff_delay(attempt, None))
                continue
            if resp.status_code in _RETRY_STATUSES and attempt < max_retries:
                # The quota check reads the body, which a streamed response has not loaded yet.
                with suppress(httpx.HTTPError):
                    await resp.aread()
                delay = _retry_delay(resp, attempt, max_retries, label)
                if delay is not None:
                    await stack.aclose()
                    await asyncio.sleep(delay)
                    continue
            yield resp
            return


def model_http_client(timeout: httpx.Timeout) -> httpx.AsyncClient:
    """The client a model, decision or embedding call makes its request with.

    Per call, so the timeout stays per call, over `shared_transport`, so the connection
    does not. Every call site goes through here rather than spelling both: one that built a
    bare `httpx.AsyncClient` would quietly go back to a handshake per call, and nothing
    would fail. `httpx.AsyncClient` is looked up at call time, which keeps it swappable.

    Not under an environment proxy. httpx reads `HTTP(S)_PROXY` / `NO_PROXY` only when no
    `transport=` is passed, so pooling there would send model traffic — prompts, tenant
    data, the provider key — around the operator's egress proxy, past whatever it logs or
    filters. Those deployments keep a client per call and the proxy route they configured.
    """
    if _environment_proxies():
        return httpx.AsyncClient(timeout=timeout)
    return httpx.AsyncClient(timeout=timeout, transport=shared_transport())


def _environment_proxies() -> bool:
    """Whether the environment names a proxy httpx would route through (`trust_env`)."""
    from urllib.request import getproxies

    return any(scheme in getproxies() for scheme in ("http", "https", "all"))


__all__ = [
    "DEFAULT_CONNECT_TIMEOUT_S",
    "MODEL_MAX_RETRIES",
    "ModelGatewayError",
    "ModelUnreachableError",
    "aclose_shared_transports",
    "model_http_client",
    "post_with_retry",
    "shared_transport",
    "stream_with_retry",
    "typed_transport_errors",
]
