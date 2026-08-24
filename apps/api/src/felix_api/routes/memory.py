"""Long-term memory: inspect, search, correct, and prune what an agent has stored.

An agent that remembers across sessions accumulates a store nobody can see. When it
starts answering from a fact that is stale, wrong, or was extracted from a hostile
tool result, an operator needs to be able to find that fact and remove it — without a
database console.

Reads are gated by `memory:read`, mutations by `memory:write`; `memory:write` implies
`memory:read` through the usual rule. The tenant always comes from the authenticated
principal, never from the request, so one tenant cannot read another's memory by
asking nicely.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from felix.auth.mgmt import (
    SCOPE_MEMORY_READ,
    SCOPE_MEMORY_WRITE,
    require_mgmt_scopes,
    tenant_id_from_request,
)
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(tags=["Memory"])

# Bounded because the content is model-written text that is later injected into
# prompts. A memory long enough to carry a whole instruction set is not a memory.
MAX_CONTENT_CHARS = 4000
MAX_TOPIC_KEY_CHARS = 200


class MemoryWriteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=MAX_CONTENT_CHARS)
    kind: str = "fact"
    manifest_id: str = ""
    topic_key: str = Field(default="", max_length=MAX_TOPIC_KEY_CHARS)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)


@router.get("")
@router.get("/")
async def list_memories(
    request: Request,
    manifest_id: str = "",
    kind: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Active memories, newest first."""
    from felix.memory import store as memory_store

    require_mgmt_scopes(request, SCOPE_MEMORY_READ)
    items = await memory_store.list_active(
        request.app.state.settings,
        tenant_id_from_request(request),
        manifest_id=manifest_id,
        kind=kind,
        limit=limit,
    )
    return {"items": items}


@router.get("/search")
async def search_memories(
    request: Request,
    q: str = Query(min_length=1, description="What to search for."),
    manifest_id: str = "",
    kind: str | None = None,
    limit: int = Query(default=8, ge=1, le=50),
) -> dict[str, Any]:
    """Hybrid recall — the same ranking the agent sees, so an operator can reproduce it."""
    from felix.memory.embedder import build_embedder
    from felix.memory.recall import recall

    require_mgmt_scopes(request, SCOPE_MEMORY_READ)
    settings = request.app.state.settings
    hits = await recall(
        settings,
        tenant_id_from_request(request),
        q,
        manifest_id=manifest_id,
        limit=limit,
        kinds=[kind] if kind else None,
        embedder=build_embedder(settings),
    )
    return {
        "items": [
            {
                "id": h.id,
                "content": h.content,
                "kind": h.kind,
                "score": h.score,
                "topic_key": h.topic_key,
                "importance": h.importance,
                # Which retrievers found it. The reason a result looks wrong is
                # usually which channel produced it, and that is otherwise invisible.
                "channels": list(h.channels),
            }
            for h in hits
        ]
    }


@router.get("/as-of/{turn_seq}")
async def memories_as_of(
    request: Request,
    turn_seq: int,
    manifest_id: str = "",
    kind: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    """What was believed at a past turn, including facts since superseded.

    Read-only on purpose. Rewinding memory is a data-loss primitive on a shared
    multi-tenant table, and Felix's session rewind is deliberately non-destructive.
    """
    from felix.memory import store as memory_store

    require_mgmt_scopes(request, SCOPE_MEMORY_READ)
    items = await memory_store.as_of(
        request.app.state.settings,
        tenant_id_from_request(request),
        turn_seq,
        manifest_id=manifest_id,
        kind=kind,
        limit=limit,
    )
    return {"turn_seq": turn_seq, "items": items}


@router.post("")
@router.post("/")
async def write_memory(request: Request, body: MemoryWriteRequest) -> dict[str, Any]:
    """Store a memory directly.

    This is a prompt-injection ingress: whatever is written here is text the model
    will later read. It is gated on `memory:write` and the content is length-bounded;
    neutralisation happens on the render path, where every source of recalled text is
    treated the same.
    """
    from felix.memory import store as memory_store

    require_mgmt_scopes(request, SCOPE_MEMORY_WRITE)
    row = await memory_store.put_memory(
        request.app.state.settings,
        tenant_id_from_request(request),
        content=body.content,
        kind=body.kind,
        manifest_id=body.manifest_id,
        topic_key=body.topic_key or None,
        importance=body.importance,
        metadata={"source": "management_api"},
    )
    return {"id": row["id"], "status": row["status"]}


@router.delete("/{memory_id}")
async def forget_memory(request: Request, memory_id: str) -> dict[str, Any]:
    """Hide a memory from recall. It is marked forgotten, not deleted."""
    from felix.memory import store as memory_store

    require_mgmt_scopes(request, SCOPE_MEMORY_WRITE)
    ok = await memory_store.forget(request.app.state.settings, tenant_id_from_request(request), memory_id)
    if not ok:
        raise HTTPException(status_code=404, detail="unknown_memory")
    return {"id": memory_id, "status": "forgotten"}
