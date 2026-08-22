"""Human-in-the-loop approvals."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from felix.context import try_get_context
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


def _tenant(request: Request) -> str:
    ctx = try_get_context()
    if ctx is not None:
        return ctx.auth.tenant_id
    auth = getattr(request.state, "auth", None)
    return getattr(auth, "tenant_id", "default") if auth else "default"


def _subj(request: Request) -> str:
    ctx = try_get_context()
    if ctx is not None:
        return ctx.auth.principal_sub
    auth = getattr(request.state, "auth", None)
    return getattr(auth, "principal_sub", "anonymous") if auth else "anonymous"


@router.get("")
@router.get("/")
async def list_approvals(
    request: Request,
    status: str | None = "pending",
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    from felix.approvals import store as approvals_store

    items = await approvals_store.list_approvals(
        request.app.state.settings,
        _tenant(request),
        status=status,
        limit=limit,
    )
    return {"items": items, "requests": items}


@router.get("/{approval_id}")
async def get_approval(approval_id: str, request: Request) -> Any:
    from felix.approvals import store as approvals_store

    row = await approvals_store.get_approval(
        request.app.state.settings, _tenant(request), approval_id
    )
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    return row


@router.post("/{approval_id}/decide")
async def decide_approval(approval_id: str, body: DecideRequest, request: Request) -> Any:
    from felix.approvals import store as approvals_store
    from felix.approvals.interrupt import signal_decision

    try:
        decision = body.resolved()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="decision_required") from exc

    row = await approvals_store.decide(
        request.app.state.settings,
        _tenant(request),
        approval_id,
        decision=decision,
        decided_by=_subj(request),
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
