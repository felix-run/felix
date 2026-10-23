"""Whose memory a compiled agent uses: `spec.memory.scope` against the person it is compiled for.

Resolved once per compile (`manifests/builder.py`) and handed, as one `MemoryPool`, to everything
that reads or writes long-term memory for that agent -- the prelude, the memory tools, and capture
after each turn -- so the three cannot disagree about whose memory it is.

The person comes in explicitly: `runtime.build_tenant_agent(person=...)` takes the caller's
`AuthContext.person` from the same `auth` it compiles for, and puts it on `BuildDeps`. It is never
read from ambient request context, which a caller compiling for someone else (`governed_tools`)
need not have installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from felix.memory.store import MemoryPool

if TYPE_CHECKING:
    from felix.config import Settings


def memory_pool(settings: Settings, *, manifest_id: str, scope: str, person: str) -> MemoryPool:
    """The pool an agent compiled for `person` (an `AuthContext.person`, `""` for none) uses.

    `scope: agent`: the agent's own pool. `scope: person`: the person's pool, through
    `FELIX_PRINCIPAL_ALIASES`, or -- with no person -- `MemoryPool.personless`, which reads the
    agent's operator-written rows and writes nothing.
    """
    from felix.auth.persons import person_of

    if scope != "person":
        return MemoryPool.agent(manifest_id)
    resolved = person_of(settings, person)
    return MemoryPool.person(manifest_id, resolved) if resolved else MemoryPool.personless(manifest_id)


__all__ = ["memory_pool"]
