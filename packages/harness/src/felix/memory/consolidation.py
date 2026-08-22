"""Memory pool consolidation — exact-hash dedupe worker entrypoint.

This is not LLM summarization. Manifest ``MemoryConsolidate.max_facts`` is the
scan cap; semantic/procedural merging is out of scope for v1.
"""

from __future__ import annotations

from felix.config import Settings
from felix.memory.store import consolidate_pools as _consolidate


async def consolidate_pools(settings: Settings, *, max_facts: int = 500) -> int:
    return await _consolidate(settings, max_facts=max_facts)
