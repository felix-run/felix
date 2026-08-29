"""One place for outbound timeout policy.

The ms->seconds conversion existed in four copies and had already diverged: three floored
at one second and two did not, so a manifest could hand `httpx` a 0.001s or a negative
timeout through the copies that skipped the floor. `_CONNECT_TIMEOUT_S = 10.0` existed in
five, each with a differently-worded comment asserting the *same* policy — which makes
divergence a bug rather than per-integration tuning, and that is the test for whether
duplication should be retired.

The per-integration *defaults* deliberately stay at their call sites: 30s for an MCP
server and 60s for a peer agent differ for real reasons.
"""

from __future__ import annotations

import httpx

# Connect is TCP-establish plus TLS, not work. Reaching a host takes seconds or never, so
# it must not scale with the request ceiling: otherwise raising a timeout to accommodate one
# slow response also lets an unreachable host park a socket for that long, and connect
# failures are the class worth retrying.
DEFAULT_CONNECT_TIMEOUT_S = 10.0

# Below a second nothing real completes, so a smaller value is a manifest that validates and
# can never work. Floor rather than reject, because the schema bound is the place to say no.
MIN_TIMEOUT_S = 1.0


def timeout_seconds(timeout_ms: int | None, *, default_s: float) -> float:
    """Seconds for a per-integration `timeout_ms`, or `default_s` when unset."""
    if not timeout_ms:
        return default_s
    return max(MIN_TIMEOUT_S, int(timeout_ms) / 1000)


def request_timeout(
    timeout_ms: int | None,
    *,
    default_s: float,
    connect_s: float = DEFAULT_CONNECT_TIMEOUT_S,
) -> httpx.Timeout:
    """An httpx timeout whose connect budget does not scale with the request budget."""
    return httpx.Timeout(timeout_seconds(timeout_ms, default_s=default_s), connect=connect_s)


__all__ = [
    "DEFAULT_CONNECT_TIMEOUT_S",
    "MIN_TIMEOUT_S",
    "request_timeout",
    "timeout_seconds",
]
