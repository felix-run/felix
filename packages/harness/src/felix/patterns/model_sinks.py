"""Wire the harness into `felix_ai`'s two optional seams.

`felix_ai` may not import `felix`, so metrics and the prompt-cache key — both of which the
wire clients emit — arrive through sinks the harness installs once at import time. Without
them the model layer still works: counters are dropped and the cache key is the constant
`felix`, which is right for a caller with no conversation context.
"""

from __future__ import annotations

from felix_ai.context import set_cache_key_resolver
from felix_ai.observability import set_counter_sink

from felix.context import try_get_context
from felix.observability.metrics import record_counter


def _thread_cache_key() -> str | None:
    """`felix:<thread_id>` so one conversation shares a cached prefix and others do not."""
    ctx = try_get_context()
    thread_id = getattr(ctx, "thread_id", None) if ctx is not None else None
    return f"felix:{thread_id}" if thread_id else None


def install_felix_ai_sinks() -> None:
    """Idempotent: both setters overwrite, and this runs once at `felix.patterns` import."""
    set_counter_sink(record_counter)
    set_cache_key_resolver(_thread_cache_key)


__all__ = ["install_felix_ai_sinks"]
