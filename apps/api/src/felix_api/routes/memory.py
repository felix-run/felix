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

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from felix.auth.mgmt import (
    SCOPE_MEMORY_READ,
    SCOPE_MEMORY_WRITE,
    require_mgmt_scopes,
    tenant_id_from_request,
)

# Bounded because the content is model-written text later injected into prompts. The
# numbers come from the store, which applies them to every writer; this route rejects
# rather than truncating, because a caller who sent too much should be told. Two
# behaviours, one pair of numbers.
from felix.memory.store import MAX_CONTENT_CHARS, MAX_TOPIC_KEY_CHARS
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(tags=["Memory"])


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
    thread_id: str | None = Query(
        default=None,
        description="The thread suffix; empty for rows written outside any thread.",
    ),
    status: Literal["active", "forgotten"] = "active",
) -> dict[str, Any]:
    """Active memories, newest first; or, with `status=forgotten`, the forgotten ones,
    most recently forgotten first.

    `thread_id` takes the suffix and composes `{tenant}:{suffix}` here, the rule `/usage`
    and `/plans` follow, so a caller can never name another tenant's thread.
    """
    from felix.memory import store as memory_store

    require_mgmt_scopes(request, SCOPE_MEMORY_READ)
    tenant_id = tenant_id_from_request(request)
    scoped = _scoped_thread(tenant_id, thread_id)
    if status == "forgotten":
        items = await memory_store.list_forgotten(
            request.app.state.settings,
            tenant_id,
            manifest_id=manifest_id,
            kind=kind,
            thread_id=scoped,
            limit=limit,
        )
    else:
        items = await memory_store.list_active(
            request.app.state.settings,
            tenant_id,
            manifest_id=manifest_id,
            kind=kind,
            limit=limit,
            thread_id=scoped,
        )
    return {"items": items}


def _scoped_thread(tenant_id: str, thread_id: str | None) -> str | None:
    if thread_id is None or thread_id == "":
        return thread_id
    from felix.thread_ids import effective_thread_id

    scoped = effective_thread_id(tenant_id, thread_id)
    if scoped is None:
        raise HTTPException(status_code=400, detail="invalid_thread_id")
    return scoped


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
    from felix.memory import store as memory_store

    tenant_id = tenant_id_from_request(request)
    hits = await recall(
        settings,
        tenant_id,
        q,
        manifest_id=manifest_id,
        limit=limit,
        kinds=[kind] if kind else None,
        embedder=build_embedder(settings),
    )
    # Where each hit came from. A recalled fact that looks wrong raises "which
    # conversation taught it this?" first, and a hit that cannot answer sends the
    # operator to the database. One read for the whole page of hits.
    rows = await memory_store.get_many(settings, tenant_id, [h.id for h in hits])
    return {"items": [_search_item(h, rows.get(h.id) or {}) for h in hits]}


def _search_item(hit: Any, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": hit.id,
        "content": hit.content,
        "kind": hit.kind,
        "score": hit.score,
        "topic_key": hit.topic_key,
        "importance": hit.importance,
        # Which retrievers found it. The reason a result looks wrong is
        # usually which channel produced it, and that is otherwise invisible.
        "channels": list(hit.channels),
        "manifest_id": row.get("manifest_id", ""),
        "thread_id": row.get("thread_id") or "",
        "origin_seq": row.get("origin_seq"),
        "status": row.get("status") or "active",
        "created_at": row.get("created_at"),
        "last_used_at": row.get("last_used_at"),
    }


@router.get("/as-of/{turn_seq}")
async def memories_as_of(
    request: Request,
    turn_seq: int,
    manifest_id: str = "",
    kind: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    thread_id: str | None = Query(
        default=None,
        description="The thread suffix; empty for rows written outside any thread.",
    ),
) -> dict[str, Any]:
    """What was believed at a past turn, including facts since superseded.

    A turn number is an ordinal into one thread's log, so pass `thread_id` to ask about
    one conversation. Without it the cut runs across every thread's ordinals at once,
    which is kept for compatibility and is rarely the question being asked.

    Read-only on purpose. Rewinding memory is a data-loss primitive on a shared
    multi-tenant table, and Felix's session rewind is deliberately non-destructive.
    """
    from felix.memory import store as memory_store

    require_mgmt_scopes(request, SCOPE_MEMORY_READ)
    tenant_id = tenant_id_from_request(request)
    items = await memory_store.as_of(
        request.app.state.settings,
        tenant_id,
        turn_seq,
        manifest_id=manifest_id,
        kind=kind,
        limit=limit,
        thread_id=_scoped_thread(tenant_id, thread_id),
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
    ok = await memory_store.forget(
        request.app.state.settings,
        tenant_id_from_request(request),
        memory_id,
        source="management_api",
    )
    if not ok:
        raise HTTPException(status_code=404, detail="unknown_memory")
    return {"id": memory_id, "status": "forgotten"}


@router.post("/{memory_id}/restore")
async def restore_memory(request: Request, memory_id: str) -> dict[str, Any]:
    """Undo a forget: the memory is recalled again.

    Only a forgotten memory can be restored (409 `not_forgotten` otherwise), and only by a
    caller ranked at least as high as whoever forgot it (403 `restore_refused`).
    """
    from felix.memory import store as memory_store

    require_mgmt_scopes(request, SCOPE_MEMORY_WRITE)
    outcome = await memory_store.restore(
        request.app.state.settings,
        tenant_id_from_request(request),
        memory_id,
        source="management_api",
    )
    if outcome == "missing":
        raise HTTPException(status_code=404, detail="unknown_memory")
    if outcome == "not_forgotten":
        raise HTTPException(status_code=409, detail="not_forgotten")
    if outcome == "refused":
        raise HTTPException(status_code=403, detail="restore_refused")
    return {"id": memory_id, "status": "active"}
