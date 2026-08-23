"""Approval CRUD."""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from sqlalchemy import select

from felix.config import Settings
from felix.db.models import Approval
from felix.db.session import _use_memory, get_session_factory

now_ms = lambda: int(time.time() * 1000)

_memory_approvals: dict[tuple[str, str], dict[str, Any]] = {}


def _approval_dict(row: Approval | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        data = dict(row)
    else:
        data = {
            "id": row.id,
            "tenant_id": row.tenant_id,
            "manifest_id": row.manifest_id,
            "tool_name": row.tool_name,
            "call_signature": row.call_signature,
            "args_json": row.args_json,
            "principal_subj": row.principal_subj,
            "consumed_at": row.consumed_at,
            "status": row.status,
            "created_at": row.created_at,
            "decided_at": row.decided_at,
            "decided_by": row.decided_by,
            "decision_note": row.decision_note,
            "edited_args_json": row.edited_args_json,
            "ttl_seconds": row.ttl_seconds,
            "expires_at": row.expires_at,
            "rule_id": row.rule_id,
        }
    return {
        "id": data["id"],
        "tenant_id": data["tenant_id"],
        "manifest_id": data.get("manifest_id", ""),
        "tool_name": data["tool_name"],
        "call_signature": data["call_signature"],
        "args": data.get("args_json") or data.get("args") or {},
        "principal_subj": data.get("principal_subj", ""),
        "consumed_at": data.get("consumed_at"),
        "status": data["status"],
        "created_at": data["created_at"],
        "decided_at": data.get("decided_at"),
        "decided_by": data.get("decided_by", ""),
        "decision_note": data.get("decision_note", ""),
        "edited_args": data.get("edited_args_json") or data.get("edited_args"),
        "ttl_seconds": data.get("ttl_seconds"),
        "expires_at": data.get("expires_at"),
        "rule_id": data.get("rule_id", ""),
    }


async def list_approvals(
    settings: Settings,
    tenant_id: str,
    *,
    status: str | None = "pending",
    limit: int = 50,
) -> list[dict[str, Any]]:
    if _use_memory(settings):
        items = [
            _approval_dict(row)
            for (t, _), row in _memory_approvals.items()
            if t == tenant_id and (status is None or row["status"] == status)
        ]
        items.sort(key=lambda r: r["created_at"], reverse=True)
        return items[:limit]

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        stmt = (
            select(Approval)
            .where(Approval.tenant_id == tenant_id)
            .order_by(Approval.created_at.desc())
            .limit(limit)
        )
        if status is not None:
            stmt = stmt.where(Approval.status == status)
        rows = (await db.scalars(stmt)).all()
        return [_approval_dict(r) for r in rows]


async def get_approval(settings: Settings, tenant_id: str, approval_id: str) -> dict[str, Any] | None:
    if _use_memory(settings):
        row = _memory_approvals.get((tenant_id, approval_id))
        return _approval_dict(row) if row else None

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        row = await db.get(Approval, (tenant_id, approval_id))
        return _approval_dict(row) if row else None


async def find_approved(
    settings: Settings,
    tenant_id: str,
    *,
    manifest_id: str,
    tool_name: str,
    call_signature: str,
    principal_subj: str | None = None,
    unconsumed_only: bool = False,
) -> dict[str, Any] | None:
    """Return an approved grant for this call signature, if still valid.

    ``principal_subj`` implements ``ApprovalRule.bind_principal``: without it, principal
    A's approval authorizes principal B's byte-identical call in the same tenant.
    ``unconsumed_only`` implements ``ApprovalRule.one_shot``: without it, a single grant
    authorizes unlimited replays until it expires.
    """
    ts = now_ms()
    if _use_memory(settings):
        for row in _memory_approvals.values():
            if (
                row["tenant_id"] == tenant_id
                and row.get("manifest_id", "") == manifest_id
                and row["tool_name"] == tool_name
                and row["call_signature"] == call_signature
                and row["status"] == "approved"
            ):
                exp = row.get("expires_at")
                if exp is not None and exp < ts:
                    continue
                if principal_subj is not None and row.get("principal_subj", "") != principal_subj:
                    continue
                if unconsumed_only and row.get("consumed_at") is not None:
                    continue
                return _approval_dict(row)
        return None

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        stmt = (
            select(Approval)
            .where(
                Approval.tenant_id == tenant_id,
                Approval.manifest_id == manifest_id,
                Approval.tool_name == tool_name,
                Approval.call_signature == call_signature,
                Approval.status == "approved",
            )
            .order_by(Approval.decided_at.desc())
            .limit(1)
        )
        if principal_subj is not None:
            stmt = stmt.where(Approval.principal_subj == principal_subj)
        if unconsumed_only:
            stmt = stmt.where(Approval.consumed_at.is_(None))
        row = (await db.scalars(stmt)).first()
        if row is None:
            return None
        if row.expires_at is not None and row.expires_at < ts:
            return None
        return _approval_dict(row)


async def create_pending(
    settings: Settings,
    tenant_id: str,
    *,
    tool_name: str,
    call_signature: str,
    args: dict[str, Any] | None = None,
    manifest_id: str = "",
    principal_subj: str = "",
    ttl_seconds: int | None = None,
    rule_id: str = "",
) -> dict[str, Any]:
    # Reuse existing pending for the same signature.
    if _use_memory(settings):
        for row in _memory_approvals.values():
            if (
                row["tenant_id"] == tenant_id
                and row.get("manifest_id", "") == manifest_id
                and row["tool_name"] == tool_name
                and row["call_signature"] == call_signature
                and row["status"] == "pending"
            ):
                return _approval_dict(row)
    else:
        factory = get_session_factory(settings=settings)
        async with factory() as db:
            existing = (
                await db.scalars(
                    select(Approval)
                    .where(
                        Approval.tenant_id == tenant_id,
                        Approval.manifest_id == manifest_id,
                        Approval.tool_name == tool_name,
                        Approval.call_signature == call_signature,
                        Approval.status == "pending",
                    )
                    .limit(1)
                )
            ).first()
            if existing is not None:
                return _approval_dict(existing)

    approval_id = uuid.uuid4().hex
    ts = now_ms()
    expires_at = ts + ttl_seconds * 1000 if ttl_seconds is not None else None

    if _use_memory(settings):
        row = {
            "id": approval_id,
            "tenant_id": tenant_id,
            "manifest_id": manifest_id,
            "tool_name": tool_name,
            "call_signature": call_signature,
            "args_json": args or {},
            "principal_subj": principal_subj,
            "status": "pending",
            "created_at": ts,
            "decided_at": None,
            "decided_by": "",
            "decision_note": "",
            "edited_args_json": None,
            "ttl_seconds": ttl_seconds,
            "expires_at": expires_at,
            "rule_id": rule_id,
        }
        _memory_approvals[(tenant_id, approval_id)] = row
        return _approval_dict(row)

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        row = Approval(
            tenant_id=tenant_id,
            id=approval_id,
            manifest_id=manifest_id,
            tool_name=tool_name,
            call_signature=call_signature,
            args_json=args or {},
            principal_subj=principal_subj,
            status="pending",
            created_at=ts,
            ttl_seconds=ttl_seconds,
            expires_at=expires_at,
            rule_id=rule_id,
        )
        db.add(row)
        await db.commit()
        return _approval_dict(row)


async def decide(
    settings: Settings,
    tenant_id: str,
    approval_id: str,
    *,
    decision: Literal["approved", "denied"],
    decided_by: str,
    note: str = "",
    edited_args: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    ts = now_ms()

    if _use_memory(settings):
        row = _memory_approvals.get((tenant_id, approval_id))
        if row is None:
            return None
        row["status"] = decision
        row["decided_at"] = ts
        row["decided_by"] = decided_by
        row["decision_note"] = note
        if edited_args is not None:
            row["edited_args_json"] = edited_args
        return _approval_dict(row)

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        row = await db.get(Approval, (tenant_id, approval_id))
        if row is None:
            return None
        row.status = decision
        row.decided_at = ts
        row.decided_by = decided_by
        row.decision_note = note
        if edited_args is not None:
            row.edited_args_json = edited_args
        await db.commit()
        return _approval_dict(row)


async def consume_approval(settings: Settings, tenant_id: str, approval_id: str) -> bool:
    """Mark a one_shot grant spent. Returns False when it was already consumed.

    The check-and-set is a single conditional UPDATE so two concurrent identical calls
    cannot both spend the same grant.
    """
    ts = now_ms()
    if _use_memory(settings):
        row = _memory_approvals.get((tenant_id, approval_id))
        if row is None or row.get("consumed_at") is not None:
            return False
        row["consumed_at"] = ts
        return True

    from sqlalchemy import update

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        result = await db.execute(
            update(Approval)
            .where(
                Approval.tenant_id == tenant_id,
                Approval.id == approval_id,
                Approval.consumed_at.is_(None),
            )
            .values(consumed_at=ts)
        )
        await db.commit()
        return bool(getattr(result, "rowcount", 0))


__all__ = [
    "consume_approval",
    "create_pending",
    "decide",
    "find_approved",
    "get_approval",
    "list_approvals",
]
