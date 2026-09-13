"""Human-in-the-loop approvals."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from felix.auth.mgmt import (
    SCOPE_APPROVALS_READ,
    SCOPE_APPROVALS_WRITE,
    require_mgmt_scopes,
    subject_from_request,
    tenant_id_from_request,
)
from pydantic import BaseModel

router = APIRouter(tags=["Approvals"])


class DecideRequest(BaseModel):
    model_config = {"extra": "forbid"}

    decision: Literal["approved", "denied"] | None = None
    # chat-ui sends ``status``; accept either field.
    status: Literal["approved", "denied"] | None = None
    note: str = ""
    edited_args: dict[str, Any] | None = None

    def resolved(self) -> Literal["approved", "denied"]:
        value = self.decision or self.status
        if value is None:
            raise ValueError("decision required")
        return value


@router.get("")
@router.get("/")
async def list_approvals(
    request: Request,
    status: str | None = "pending",
    limit: int = Query(default=50, ge=1, le=200),
    thread_id: str | None = Query(default=None),
) -> dict[str, Any]:
    """Approvals awaiting a decision, newest first.

    `thread_id` narrows to one conversation, and it has to be applied *here* rather than by
    the caller: filtering a returned page client-side drops whatever the page already cut
    off, so a busy tenant hides the one thread the caller asked about. The store applies it
    in SQL before `LIMIT` for exactly that reason.

    `?thread_id=` (empty) is a real value meaning "approvals with no thread" — a gated tool
    called outside a chat context — and is distinct from omitting the parameter. It
    under-reports rather than over-reports: `create_pending` reuses a pending row across
    threads, so the row names whichever thread asked first. See `deploy/GOVERNANCE.md`.
    """
    from felix.approvals import store as approvals_store

    require_mgmt_scopes(request, SCOPE_APPROVALS_READ)
    items = await approvals_store.list_approvals(
        request.app.state.settings,
        tenant_id_from_request(request),
        status=status,
        limit=limit,
        thread_id=thread_id,
    )
    return {"items": items, "requests": items}


@router.get("/{approval_id}")
async def get_approval(approval_id: str, request: Request) -> Any:
    from felix.approvals import store as approvals_store

    require_mgmt_scopes(request, SCOPE_APPROVALS_READ)
    row = await approvals_store.get_approval(
        request.app.state.settings, tenant_id_from_request(request), approval_id
    )
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    return row


@router.post("/{approval_id}/decide")
async def decide_approval(approval_id: str, body: DecideRequest, request: Request) -> Any:
    from felix.approvals import store as approvals_store
    from felix.approvals.interrupt import signal_decision

    require_mgmt_scopes(request, SCOPE_APPROVALS_WRITE)
    try:
        decision = body.resolved()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="decision_required") from exc

    row = await approvals_store.decide(
        request.app.state.settings,
        tenant_id_from_request(request),
        approval_id,
        decision=decision,
        decided_by=subject_from_request(request),
        note=body.note,
        edited_args=body.edited_args,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    await signal_decision(
        approval_id,
        decision,
        edited_args=body.edited_args,
        note=body.note,
    )
    return row
