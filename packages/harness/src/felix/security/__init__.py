"""Felix security helpers — SSRF, crypto, rate limits, safe expr."""

from __future__ import annotations

from felix.security.constant_time import constant_time_equal
from felix.security.expr import evaluate_expression
from felix.security.rate_limit import (
    InMemoryRateLimiter,
    RateLimitConfig,
    check_rate_limit,
    should_skip_rate_limit,
)
from felix.security.ssrf import (
    EgressBlocked,
    assert_safe_outbound_url,
    assert_safe_outbound_url_async,
    assert_safe_outbound_url_for_hosts,
)

__all__ = [
    "EgressBlocked",
    "InMemoryRateLimiter",
    "RateLimitConfig",
    "assert_safe_outbound_url",
    "assert_safe_outbound_url_async",
    "assert_safe_outbound_url_for_hosts",
    "check_rate_limit",
    "constant_time_equal",
    "evaluate_expression",
    "should_skip_rate_limit",
]
