"""Authoritative session snapshots — HTTP/SSE truth, not CBOR."""

from __future__ import annotations

import time
from typing import Any, Literal

from felix.session.types import SessionEvent, analyze_wake

SessionPhase = Literal["idle", "turn", "compaction", "branch_summary", "retry", "aborted"]

_PHASES = frozenset({"idle", "turn", "compaction", "branch_summary", "retry", "aborted"})


def event_to_transcript_item(ev: SessionEvent) -> dict[str, Any]:
    """Normalize a session event into a transcript item."""
    md = dict(ev.metadata or {})
    status = str(md.get("status") or "complete")
    item: dict[str, Any] = {
        "id": md.get("event_id") or f"seq-{ev.seq}",
        "seq": ev.seq,
        "kind": ev.kind,
        "role": ev.role,
        "content": ev.content or "",
        "timestamp": int((ev.ts or time.time()) * 1000),
        "status": status,
        "metadata": md,
    }
    if ev.tool_call_id:
        item["toolCallId"] = ev.tool_call_id
    if ev.name:
        item["toolName"] = ev.name
    if ev.tool_calls:
        item["toolCalls"] = ev.tool_calls
    if md.get("usage"):
        item["usage"] = md["usage"]
    return item


def build_snapshot(
    *,
    thread_id: str,
    events: list[SessionEvent],
    leaf_id: str | None = None,
    session_name: str | None = None,
    phase: str = "idle",
    model_id: str | None = None,
    thinking_level: str | None = None,
    parent_session_id: str | None = None,
    labels: dict[str, str] | None = None,
    queued_steer: list[dict[str, Any]] | None = None,
    attached: bool = False,
    locked: bool = False,
    revision: int | None = None,
) -> dict[str, Any]:
    """Build an authoritative session snapshot for clients."""
    wake = analyze_wake(events)
    # Resolve model / thinking from latest change events if not provided.
    resolved_model = model_id
    resolved_thinking = thinking_level
    created_at = int((events[0].ts if events else time.time()) * 1000)
    updated_at = int((events[-1].ts if events else time.time()) * 1000)
    for ev in events:
        md = ev.metadata or {}
        if ev.kind == "model_change" or md.get("type") == "model_change":
            resolved_model = str(md.get("model_id") or ev.content or resolved_model or "")
        if ev.kind == "thinking_level_change" or md.get("type") == "thinking_level_change":
            resolved_thinking = str(md.get("thinking_level") or ev.content or resolved_thinking or "")

    phase_norm: str = phase if phase in _PHASES else "idle"
    if wake.pending_tool_calls and phase_norm == "idle":
        phase_norm = "turn"

    transcript = [event_to_transcript_item(e) for e in events]
    return {
        "id": thread_id,
        "name": session_name,
        "parentSessionId": parent_session_id,
        "createdAt": created_at,
        "updatedAt": updated_at,
        "phase": phase_norm,
        "model": {"id": resolved_model} if resolved_model else None,
        "thinkingLevel": resolved_thinking or "off",
        "attached": attached,
        "locked": locked,
        "revision": revision if revision is not None else (events[-1].seq + 1 if events else 0),
        "leafId": leaf_id,
        "labels": labels or {},
        "transcript": transcript,
        "queuedSteer": queued_steer or [],
        "queuedSteerCount": len(queued_steer or []),
        "wake": {
            "fresh": wake.fresh,
            "headSeq": wake.head_seq,
            "endedOnAssistant": wake.ended_on_assistant,
            "pendingToolCalls": [
                {"id": tc.id, "name": tc.name, "args": tc.args} for tc in wake.pending_tool_calls
            ],
        },
    }


def build_session_metadata(
    *,
    thread_id: str,
    created_at: int | None = None,
    updated_at: int | None = None,
    parent_session_id: str | None = None,
    session_name: str | None = None,
) -> dict[str, Any]:
    now = int(time.time() * 1000)
    return {
        "id": thread_id,
        "createdAt": created_at or now,
        "updatedAt": updated_at or now,
        "parentSessionId": parent_session_id,
        "sessionName": session_name,
    }


async def gather_thread_snapshot(*, settings: Any, tenant_id: str, thread: str) -> dict[str, Any]:
    """Read everything a snapshot needs, then build one.

    `build_snapshot` above is pure — hand it rows and it shapes them. This is the half that
    goes and gets the rows, and it lives here rather than in an HTTP route because nothing
    about it is HTTP: it takes a tenant and a thread and returns a dict, and the worker and
    the A2A surface can reach for it on the same terms a route does.

    (It spent a while in `felix_api.routes._streaming`, which made `POST /chat/abort` import
    a module named for streaming in order to read a session. Five of its six callers were
    not streaming at all.)

    The store imports are deferred because this module is imported by the session package
    itself; at module scope they would close a cycle.
    """
    import asyncio

    from felix.session.lease import lease_status
    from felix.session.store import get_session_store
    from felix.session.thread_state import get_thread_meta, load_leaf
    from felix.session.tree import get_leaf
    from felix.steer import peek_steer_count

    store = get_session_store(settings, tenant_id=tenant_id)
    # Five reads against four different stores, none of which depends on another. They
    # ran in series on `GET /chat/sessions/{id}`, on both lease endpoints and on every
    # cold SSE reconnect -- the reattach path, where latency is the most visible thing
    # in the product.
    #
    # `gather` holds more pool connections at once, which is why it waited for the pool
    # to become a setting rather than a hardcoded 5 + 10.
    events, meta, stored_leaf, steer_n, lease = await asyncio.gather(
        store.open(thread).get_events(),
        get_thread_meta(settings=settings, tenant_id=tenant_id, thread_id=thread),
        load_leaf(settings=settings, tenant_id=tenant_id, thread_id=thread),
        peek_steer_count(tenant_id, thread),
        lease_status(thread),
    )
    # `get_leaf` is synchronous and in-process, so it stays out of the fan-out.
    leaf = stored_leaf or get_leaf(thread)
    return build_snapshot(
        thread_id=thread,
        events=events,
        leaf_id=leaf,
        session_name=meta.get("session_name"),
        phase=str(meta.get("phase") or "idle"),
        model_id=meta.get("model_id"),
        thinking_level=meta.get("thinking_level"),
        parent_session_id=meta.get("parent_session_id"),
        labels=dict(meta.get("labels") or {}),
        queued_steer=[{"placeholder": True}] * steer_n if steer_n else [],
        revision=int(meta.get("revision") or 0),
        attached=bool(lease.get("attached")),
        locked=bool(lease.get("locked")),
    )


__all__ = [
    "SessionPhase",
    "build_session_metadata",
    "build_snapshot",
    "event_to_transcript_item",
    "gather_thread_snapshot",
]
