"""Durable plan store (agent-authored multi-step plans)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from felix.auth.mgmt import (
    SCOPE_PLANS_READ,
    SCOPE_PLANS_WRITE,
    require_mgmt_scopes,
    subject_from_request,
    tenant_id_from_request,
)
from pydantic import BaseModel

router = APIRouter(tags=["Plans"])

#: The key a plan written through `PUT /plans/{id}` carries: `{"by": principal, "at": epoch ms}`.
#: The plan tools build their blob from named keys, so the agent cannot write it; a step update
#: copies the stored blob, so it survives the agent's later progress on an edited plan, and a
#: `plan_create` over the same id drops it, because the agent has rewritten the plan.
EDITED_KEY = "edited"


def now_ms() -> int:
    import time

    return int(time.time() * 1000)


class PlanUpsert(BaseModel):
    model_config = {"extra": "forbid"}

    plan: dict[str, Any]
    # Omitted means "leave as stored" (a new plan gets "" / no expiry). Sending a
    # value, including `expires_at: null`, sets it.
    manifest_id: str = ""
    expires_at: int | None = None
    # The `updated_at` this edit was based on. When set, the write succeeds only if
    # the plan is still exactly that version, and answers 409 with the current row
    # otherwise — the agent updates step status while a run is live, and without
    # this an operator's save and the agent's update erase each other silently.
    expected_updated_at: int | None = None


@router.get("")
@router.get("/")
async def list_plans(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    thread_id: str | None = Query(default=None),
) -> dict[str, Any]:
    """Plans, most recently updated first.

    `thread_id` narrows to one conversation, and takes the **suffix** a client holds — the
    same form every chat route takes — composed into `{tenant}:{suffix}` here, so a caller
    can never name another tenant's thread. It is applied in the store before `LIMIT`.
    `?thread_id=` (empty) is a real value meaning "plans written outside a chat", distinct
    from omitting it; a suffix that cannot be a thread id answers 400 `invalid_thread_id`.
    Each row's `thread_id` is the stored `{tenant}:{suffix}` form, `''` for none.
    """
    from felix.plans import store as plans_store
    from felix.thread_ids import effective_thread_id

    require_mgmt_scopes(request, SCOPE_PLANS_READ)
    tenant_id = tenant_id_from_request(request)
    scoped: str | None = None
    if thread_id is not None:
        if thread_id == "":
            scoped = ""
        else:
            scoped = effective_thread_id(tenant_id, thread_id)
            if scoped is None:
                raise HTTPException(status_code=400, detail="invalid_thread_id")
    items = await plans_store.list_plans(request.app.state.settings, tenant_id, limit=limit, thread_id=scoped)
    return {"items": items, "plans": items}


@router.get("/{plan_id}")
async def get_plan(plan_id: str, request: Request) -> Any:
    from felix.plans import store as plans_store

    require_mgmt_scopes(request, SCOPE_PLANS_READ)
    row = await plans_store.get_plan(request.app.state.settings, tenant_id_from_request(request), plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    return row


@router.put("/{plan_id}")
async def upsert_plan(plan_id: str, body: PlanUpsert, request: Request) -> Any:
    from felix.plans import store as plans_store

    require_mgmt_scopes(request, SCOPE_PLANS_WRITE)
    settings = request.app.state.settings
    tenant_id = tenant_id_from_request(request)
    sent = body.model_fields_set
    # This route is how a person (or a client acting for one) writes a plan; the agent writes
    # through its plan tools. So the plan says it was edited, by whom and when, and the client
    # cannot say otherwise: an `edited` it sent is replaced, never kept.
    who = subject_from_request(request)
    plan = {**body.plan, EDITED_KEY: {"by": who, "at": now_ms()}}
    try:
        row = await plans_store.put_plan(
            settings,
            tenant_id,
            plan_id,
            plan=plan,
            manifest_id=body.manifest_id if "manifest_id" in sent else plans_store.KEEP,
            expires_at=body.expires_at if "expires_at" in sent else plans_store.KEEP,
            expected_updated_at=body.expected_updated_at,
        )
    except plans_store.PlanConflict as conflict:
        raise HTTPException(
            status_code=409,
            detail={"error": "plan_changed", "current": conflict.current},
        ) from conflict
    from felix.audit import store as audit_store

    audit_store.record_event(
        settings,
        tenant_id,
        "plan_edit",
        principal_subj=who,
        status="ok",
        payload={"plan_id": plan_id, "thread_id": row.get("thread_id") or ""},
    )
    # The plan's own conversation hears about it, as it hears about a workspace file the
    # operator changed: before its next model call if a run is going, in its log otherwise.
    # Its plan tools would show the change, but only if it thought to look.
    told = None
    if row.get("thread_id"):
        from felix.workspace_notes import WorkspaceNote

        from felix_api.routes.chat import _deliver_workspace_note

        told, _ = await _deliver_workspace_note(
            request, tenant_id, row["thread_id"], WorkspaceNote(path=plan_id, kind="plan")
        )
    return {**row, "agent_told": told}


@router.delete("/{plan_id}")
async def delete_plan(plan_id: str, request: Request) -> dict[str, str]:
    from felix.plans import store as plans_store

    require_mgmt_scopes(request, SCOPE_PLANS_WRITE)
    ok = await plans_store.delete_plan(request.app.state.settings, tenant_id_from_request(request), plan_id)
    if not ok:
        raise HTTPException(status_code=404, detail="not_found")
    return {"status": "deleted"}
