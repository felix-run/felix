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


# Retried statuses: rate limiting and transient upstream failures. 4xx other than these
# will not succeed on a retry, so retrying them just burns latency.
_RETRY_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504})
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
            # Not backpressure. The bytes were accepted (or are still going out) and a retry
            # re-sends identical input to wait out an identical ceiling, so this used to cost
            # three full timeouts before surfacing. Fail once and say so — the fix is a larger
            # FELIX_MODEL_TIMEOUT_SECONDS, not another attempt.
            #
            # ConnectTimeout is deliberately NOT here: nothing was accepted, the far side may
            # be briefly unreachable, and the next attempt is a genuinely different bet.
            record_counter("felix_model_timeout", {"provider": label})
            logger.warning(
                "%s timed out mid-request; not retrying — raise FELIX_MODEL_TIMEOUT_SECONDS "
                "if this request is legitimately long",
                label,
            )
            raise
        except httpx.HTTPError as exc:
            last = exc
            if attempt >= max_retries:
                raise
            await asyncio.sleep(_backoff_delay(attempt, None))
            continue
        if resp.status_code in _RETRY_STATUSES and attempt < max_retries:
            if resp.status_code == 429 and _is_exhausted_quota(resp):
                logger.warning("%s rate limit is a spent quota, not backpressure; not retrying", label)
                record_counter("felix_model_retry_skipped", {"provider": label, "reason": "quota"})
                return resp
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
            await asyncio.sleep(delay)
            continue
        return resp
    raise last if last is not None else RuntimeError("unreachable")


__all__ = [
    "DEFAULT_CONNECT_TIMEOUT_S",
    "MODEL_MAX_RETRIES",
    "ModelGatewayError",
    "post_with_retry",
]
