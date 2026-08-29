"""Optional prompt-cache key resolver.

`felix.context.try_get_context` cannot be imported here. The harness installs a resolver at
import time so the cache key stays `felix:<thread_id>`; with none installed the key is the
constant `felix`, which is correct for a standalone caller with no conversation context.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable

CacheKeyResolver = Callable[[], str | None]

_resolver: CacheKeyResolver | None = None


def set_cache_key_resolver(resolver: CacheKeyResolver | None) -> None:
    """Install the process-wide cache-key resolver. Called once by `felix.patterns`."""
    global _resolver
    _resolver = resolver


def resolve_cache_key(default: str = "felix") -> str:
    """The prompt-cache key for the current turn, or `default` when unresolvable."""
    if _resolver is None:
        return default
    with contextlib.suppress(Exception):
        key = _resolver()
        if key:
            return key
    return default


__all__ = ["CacheKeyResolver", "resolve_cache_key", "set_cache_key_resolver"]
