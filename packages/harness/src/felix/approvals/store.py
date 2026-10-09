"""Approval CRUD."""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from sqlalchemy import collate, select, update

from felix.approvals.interrupt import DEFAULT_TIMEOUT_SECONDS
from felix.config import Settings
from felix.db.models import Approval
from felix.db.session import _use_memory, get_session_factory

now_ms = lambda: int(time.time() * 1000)

# How much of the gate's own words a row will hold.
#
# `reason` comes from `ApprovalRule.description` or `CommandRule.reason`, both authored by a
# *tenant*-scoped manifest author and neither length-capped by the schema (`pattern` beside
# them is capped at 256). Rows here are never reclaimed -- `approvals` is not in the retention
# sweep's `TABLES`, has no cascade, and nothing anywhere issues a delete against it -- so an
# unbounded field copied in on every gate firing grows without a ceiling.
#
# Truncating here rather than adding `max_length` to the schema, deliberately: per CLAUDE.md,
# narrowing what a manifest field accepts retroactively invalidates every manifest already
# stored with a longer value, and the store is read ahead of bundled YAML. A cap at the
# persistence boundary bounds the table without refusing a manifest that parses today.
MAX_REASON_CHARS = 2048

_memory_approvals: dict[tuple[str, str], dict[str, Any]] = {}


# Who a row names as its decider when nobody answered: the harness stopped waiting.
TIMEOUT_DECIDER = "felix"
TIMEOUT_NOTE = "timeout"
# The note on a row whose call was interrupted: the run that asked died before it was answered.
INTERRUPTED_NOTE = "interrupted"


def _lapsed(expires_at: int | None, created_at: int, now: int) -> bool:
    """Whether a pending row's wait is over, by the row's own deadline.

    A null `expires_at` means the rule set no ttl, and `wait_for_decision` then waits its
    own default -- so the row still has a deadline, it is just not written down.
    """
    deadline = expires_at if expires_at is not None else created_at + int(DEFAULT_TIMEOUT_SECONDS * 1000)
    return deadline <= now


def reset_approvals_for_tests() -> None:
    """Clear the in-memory approvals."""
    _memory_approvals.clear()


def _approval_dict(row: Approval | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        data = dict(row)
    else:
        # Read from the table's own columns rather than twenty hand-written `"x": row.x`
        # pairs. The pairs were exactly the column list, so this is the same dict -- but the
        # two input shapes failed differently: a column missed here raised `AttributeError`
        # (loud) while one missed in the in-memory row literal below falls through
        # `data.get(..., "")` and reads as an empty value (silent). Adding a column is now
        # one edit rather than two, on the store that gates tool execution.
        data = {c.key: getattr(row, c.key) for c in Approval.__table__.columns}
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
        "reason": data.get("reason", ""),
        "thread_id": data.get("thread_id", ""),
        "tool_call_id": data.get("tool_call_id", ""),
    }


async def list_approvals(
    settings: Settings,
    tenant_id: str,
    *,
    status: str | None = "pending",
    limit: int = 50,
    thread_id: str | None = None,
) -> list[dict[str, Any]]:
    """Approvals for a tenant, newest first.

    `thread_id` narrows to one conversation. It **under-reports by construction**:
    `create_pending` reuses a pending row keyed on (tenant, manifest, tool, call signature),
    so the row names whichever thread asked first, and a second thread blocked on the same
    reused row is not listed under its own id. That is the safe direction — a caller asking
    for one thread never learns about another's — and it is why this is attribution rather
    than ownership. Widening the reuse key would change grant scope, which is a product
    decision and not a filter's business.

    The Postgres arm filters in SQL rather than after `LIMIT`: filtering afterwards would
    let 50 other threads' rows hide this thread's, which is the same shape of bug
    `find_approved` already carries a comment about.
    """
    if _use_memory(settings):
        items = [
            _approval_dict(row)
            for (t, _), row in _memory_approvals.items()
            if t == tenant_id
            and (status is None or row["status"] == status)
            and (thread_id is None or row.get("thread_id", "") == thread_id)
        ]
        # Ending on the id, as `find_approved` does: a tie on `created_at` is broken the same way
        # on both arms, so a page cut through the tie holds the same rows.
        items.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
        return items[:limit]

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        stmt = (
            select(Approval)
            .where(Approval.tenant_id == tenant_id)
            .order_by(Approval.created_at.desc(), collate(Approval.id, "C").desc())
            .limit(limit)
        )
        if status is not None:
            stmt = stmt.where(Approval.status == status)
        if thread_id is not None:
            stmt = stmt.where(Approval.thread_id == thread_id)
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
        matches = []
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
                matches.append(row)
        if not matches:
            return None
        # Most recently decided wins, which is what Postgres does with
        # `ORDER BY decided_at DESC LIMIT 1`. The twin used to return the first row the dict
        # happened to yield -- the *oldest* match -- so with two live grants for one call the
        # two backends handed back different rows, and with them a different `principal_subj`
        # binding and a different `edited_args`. A tool would then run with the arguments an
        # operator had substituted on one backend and not on the other.
        matches.sort(
            key=lambda r: (r.get("decided_at") or 0, r.get("created_at") or 0, r.get("id") or ""),
            reverse=True,
        )
        return _approval_dict(matches[0])

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        from sqlalchemy import or_

        stmt = (
            select(Approval)
            .where(
                Approval.tenant_id == tenant_id,
                Approval.manifest_id == manifest_id,
                Approval.tool_name == tool_name,
                Approval.call_signature == call_signature,
                Approval.status == "approved",
                # Expiry belongs in the WHERE, not after the LIMIT. Filtering it afterwards
                # meant one expired grant *hid* a still-valid older one: `LIMIT 1` took the
                # newest row, the expiry check then discarded it, and the call was denied even
                # though a live grant existed. The twin scanned every row and skipped expired
                # ones, so it authorised where Postgres refused -- reachable in production,
                # because `create_pending` only reuses *pending* rows, so approved grants
                # accumulate per signature and an operator re-approving after a short TTL
                # lapsed produced exactly this pair.
                or_(Approval.expires_at.is_(None), Approval.expires_at >= ts),
            )
            # A tiebreaker, so "the most recent decision wins" is a contract rather than a
            # coincidence: `decided_at` is milliseconds and two decisions inside one tie,
            # which `ORDER BY` alone leaves to physical row order.
            .order_by(Approval.decided_at.desc(), Approval.created_at.desc(), Approval.id.desc())
            .limit(1)
        )
        if principal_subj is not None:
            stmt = stmt.where(Approval.principal_subj == principal_subj)
        if unconsumed_only:
            stmt = stmt.where(Approval.consumed_at.is_(None))
        row = (await db.scalars(stmt)).first()
        if row is None:
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
    reason: str = "",
    thread_id: str = "",
    tool_call_id: str = "",
) -> dict[str, Any]:
    # Reuse a *live* pending row for the same signature, so identical concurrent calls share
    # one decision. A row past its deadline is not live: the wait that opened it has already
    # been denied, and until it was closed here a re-ask joined it -- a card that read
    # "denied" to every client while the harness waited a fresh ttl on it. It is closed as
    # the timeout it was, and the call gets a row of its own.
    ts = now_ms()
    if _use_memory(settings):
        for row in _memory_approvals.values():
            if (
                row["tenant_id"] == tenant_id
                and row.get("manifest_id", "") == manifest_id
                and row["tool_name"] == tool_name
                and row["call_signature"] == call_signature
                and row["status"] == "pending"
            ):
                if _lapsed(row.get("expires_at"), row["created_at"], ts):
                    _close_memory_row(row, ts)
                    continue
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
                    .order_by(Approval.created_at.desc())
                )
            ).all()
            stale = [row.id for row in existing if _lapsed(row.expires_at, row.created_at, ts)]
            live = next((row for row in existing if row.id not in stale), None)
            if stale:
                await db.execute(_close_statement(tenant_id, stale, ts))
                await db.commit()
            if live is not None:
                return _approval_dict(live)

    approval_id = uuid.uuid4().hex
    expires_at = ts + ttl_seconds * 1000 if ttl_seconds is not None else None
    # Bounded on the way in, once, so both arms store the same thing. See MAX_REASON_CHARS.
    reason = reason[:MAX_REASON_CHARS]

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
            "reason": reason,
            "thread_id": thread_id,
            "tool_call_id": tool_call_id,
        }
        _memory_approvals[(tenant_id, approval_id)] = row
        created = _approval_dict(row)
    else:
        created = await _insert_pending(
            settings,
            Approval(
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
                reason=reason,
                thread_id=thread_id,
                tool_call_id=tool_call_id,
            ),
        )
    # Once, after both arms: this is the only place that knows the row is new.
    _announce_pending_approval(settings, created)
    return created


async def _insert_pending(settings: Settings, row: Approval) -> dict[str, Any]:
    factory = get_session_factory(settings=settings)
    async with factory() as db:
        db.add(row)
        await db.commit()
        return _approval_dict(row)


def _announce_pending_approval(settings: Settings, row: dict[str, Any]) -> None:
    """Push the new row to the tenant's subscribed browsers -- on creation only.

    Here rather than in the callers because only this function knows the row is new: a reused
    row is a call already announced, and pushing again for every byte-identical retry would
    buzz a phone once per attempt. Scheduled, never awaited, so the frame the caller emits
    next is not behind a push service.
    """
    from felix.push.notify import approval_pending

    approval_pending(settings, row)


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


def _close_memory_row(row: dict[str, Any], ts: int) -> None:
    row["status"] = "denied"
    row["decided_at"] = ts
    row["decided_by"] = TIMEOUT_DECIDER
    row["decision_note"] = TIMEOUT_NOTE


def _close_statement(tenant_id: str, approval_ids: list[str], ts: int) -> Any:
    # Conditional on `pending`, so a decision that landed first is never overwritten.
    return (
        update(Approval)
        .where(
            Approval.tenant_id == tenant_id,
            Approval.id.in_(approval_ids),
            Approval.status == "pending",
        )
        .values(status="denied", decided_at=ts, decided_by=TIMEOUT_DECIDER, decision_note=TIMEOUT_NOTE)
    )


async def close_timed_out(settings: Settings, tenant_id: str, approval_id: str) -> bool:
    """Record that nobody answered: `pending` becomes `denied` with the note `timeout`.

    `wait_for_decision` has always returned that denial to the caller and written nothing,
    so the row read `pending` for the life of the deployment. Every client polling
    `/approvals` then offered a decision the harness had already made -- chat.felix.run
    showed one twenty-nine hours past its deadline. Returns False when the row was not
    pending any more, which is a decision that arrived first and is left alone.
    """
    ts = now_ms()
    if _use_memory(settings):
        row = _memory_approvals.get((tenant_id, approval_id))
        if row is None or row["status"] != "pending":
            return False
        _close_memory_row(row, ts)
        return True

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        result = await db.execute(_close_statement(tenant_id, [approval_id], ts))
        await db.commit()
        return bool(getattr(result, "rowcount", 0))


async def close_interrupted(
    settings: Settings, tenant_id: str, thread_id: str, tool_call_ids: list[str]
) -> int:
    """Close the pending rows that interrupted calls on `thread_id` opened. Returns how many.

    A run that died waiting on an approval -- a worker restart, a lost fiber lease -- left its
    row `pending` until its deadline, and every surface kept offering it: the attention line,
    `/approvals`, the reattach stream. Approving it answered nobody, and installed a grant for a
    call the model had since been told did not finish (felix-run/felix#531). The resumed run
    closes those calls as interrupted, and closes their rows with them: `denied`, decided by
    the harness, note `interrupted`. Conditional on `pending`, so a decision that landed first
    is left alone.

    Matched on the call id *and* the thread, because a pending row names the call that opened
    it. One caveat to know: a pending row is reused for every byte-identical call while it is
    open, so a call elsewhere waiting on the same row is refused too -- the same outcome as
    the row timing out, and rare enough to prefer over a grant nobody is waiting for.
    """
    ids = [i for i in tool_call_ids if i]
    if not thread_id or not ids:
        return 0
    ts = now_ms()
    values = {
        "status": "denied",
        "decided_at": ts,
        "decided_by": TIMEOUT_DECIDER,
        "decision_note": INTERRUPTED_NOTE,
    }
    if _use_memory(settings):
        closed = 0
        for (tenant, _), row in _memory_approvals.items():
            if (
                tenant == tenant_id
                and row.get("status") == "pending"
                and row.get("thread_id", "") == thread_id
                and row.get("tool_call_id", "") in ids
            ):
                row.update(values)
                closed += 1
        return closed

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        result = await db.execute(
            update(Approval)
            .where(
                Approval.tenant_id == tenant_id,
                Approval.thread_id == thread_id,
                Approval.tool_call_id.in_(ids),
                Approval.status == "pending",
            )
            .values(**values)
        )
        await db.commit()
        return int(getattr(result, "rowcount", 0) or 0)


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
    "INTERRUPTED_NOTE",
    "close_interrupted",
    "close_timed_out",
    "consume_approval",
    "create_pending",
    "decide",
    "find_approved",
    "get_approval",
    "list_approvals",
    "reset_approvals_for_tests",
]
