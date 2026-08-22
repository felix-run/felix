"""Constant-time string compare."""

from __future__ import annotations

import hmac


def constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
