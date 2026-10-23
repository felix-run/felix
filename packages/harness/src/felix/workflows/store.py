"""Workflow definition CRUD, with a revision every conditional write compares.

A definition is replaced whole on each save, as the editor sends it. Each row carries a
`version` that starts at 1 and goes up by one per save, and a `generation` fixed when the row is
created. Together they are the row's `revision`, `"<generation>.<version>"`, an opaque string a
reader hands back to make its write conditional:

- `put_workflow(precondition=r)` writes only over revision `r`;
- `put_workflow(precondition=ABSENT)` writes only where no workflow exists yet;
- `delete_workflow(precondition=r)` deletes only revision `r`.

A miss raises :class:`WorkflowConflict` carrying the row as it stands, so the editor can show what
changed instead of one save silently erasing the other. The generation is what makes a revision
unique for the life of the id: a workflow deleted and created again starts at version 1 once more,
and an editor still holding the old `version 1` must not be able to save over the new one. These
are the shapes of HTTP's `If-Match` and `If-None-Match: *`, which is how the routes expose them.

The definition is stored as `WorkflowSpec` dumps it and handed back as a plain dict. Parsing it
again is the caller's: a reader that only lists names should not fail on a row a later schema
would reject.
"""

from __future__ import annotations

import re
import time
import uuid
from copy import deepcopy
from typing import Any, Final

from sqlalchemy import collate, delete, func, select
from sqlalchemy.exc import IntegrityError

from felix.config import Settings
from felix.db.models import Workflow
from felix.db.session import _use_memory, tenant_session
from felix.workflows.schema import WORKFLOW_ID_PATTERN, WorkflowSpec

now_ms = lambda: int(time.time() * 1000)

_WORKFLOW_ID = re.compile(WORKFLOW_ID_PATTERN)
_VERSION = re.compile(r"[0-9]{1,9}")
MAX_LIST = 500
# How many workflows a tenant may keep. Equal to `MAX_LIST`, so one listing always shows them all;
# enforced here rather than in a route because a job, a plugin or the CLI can create one too.
MAX_PER_TENANT = MAX_LIST

_memory_workflows: dict[tuple[str, str], dict[str, Any]] = {}


def reset_workflows_for_tests() -> None:
    """Clear the in-memory workflow store."""
    _memory_workflows.clear()


class _Absent:
    """Sentinel precondition: the write succeeds only where no workflow exists yet."""

    def __repr__(self) -> str:
        return "ABSENT"


ABSENT: Final = _Absent()
# A revision string to match, `ABSENT`, or `None` for an unconditional write.
Precondition = str | _Absent | None


class WorkflowLimitReached(Exception):
    """Creating this workflow would take the tenant past `MAX_PER_TENANT`. Replacing one never does."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"a tenant may keep at most {limit} workflows")
        self.limit = limit


class WorkflowConflict(Exception):
    """A conditional write found the workflow at another revision, or (not) there.

    ``current`` is the row as it stands now — ``None`` when there is none — so the caller can
    show the difference rather than guess at it.
    """

    def __init__(self, workflow_id: str, current: dict[str, Any] | None) -> None:
        super().__init__(f"workflow {workflow_id!r} changed since it was read")
        self.workflow_id = workflow_id
        self.current = current


def _revision(generation: str, version: int) -> str:
    return f"{generation}.{version}"


def _workflow_dict(row: Workflow | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        # `deepcopy`: this branch hands back the object it stored, and a caller editing the
        # definition it read would edit the store. Postgres deserializes a fresh dict per read.
        return {**row, "definition": deepcopy(row["definition"])}
    return {
        "tenant_id": row.tenant_id,
        "id": row.id,
        "version": row.version,
        "revision": _revision(row.generation, row.version),
        "definition": row.definition_json,
        "updated_by": row.updated_by,
        "created_by": row.created_by,
        "authored_via": row.authored_via,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _check_id(workflow_id: str) -> None:
    if not _WORKFLOW_ID.fullmatch(workflow_id):
        raise ValueError(f"workflow id must match {WORKFLOW_ID_PATTERN}")


def _precondition_fails(precondition: Precondition, current: str | None) -> bool:
    """`current` is the stored revision, `None` when there is no row.

    Revisions compare as strings on both arms: `"g.07"` is not `"g.7"`, because the store never
    issued it, and a twin that parsed it while Postgres did not would disagree about a conflict.
    """
    if isinstance(precondition, _Absent):
        return current is not None
    return precondition is not None and current != precondition


def _parse_revision(revision: str) -> tuple[str, int] | None:
    """`(generation, version)` for a revision exactly as the store writes it, else `None`."""
    generation, _, version = revision.rpartition(".")
    # Nine digits fits a Postgres `Integer`; `isdigit()` would pass `"²"`, which `int()` refuses.
    if not generation or not _VERSION.fullmatch(version) or _revision(generation, int(version)) != revision:
        return None
    return generation, int(version)


async def list_workflows(settings: Settings, tenant_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    """A tenant's workflows, most recently saved first, ending on the id in byte order."""
    # Clamped here, not only in a route: a negative limit is a short page on the twin and a SQL
    # error on Postgres, and the two arms must not disagree about any input.
    limit = max(1, min(limit, MAX_LIST))
    if _use_memory(settings):
        items = [_workflow_dict(row) for (t, _), row in _memory_workflows.items() if t == tenant_id]
        items.sort(key=lambda r: (r["updated_at"], r["id"]), reverse=True)
        return items[:limit]

    async with tenant_session(settings, tenant_id) as db:
        stmt = (
            select(Workflow)
            .where(Workflow.tenant_id == tenant_id)
            .order_by(Workflow.updated_at.desc(), collate(Workflow.id, "C").desc())
            .limit(limit)
        )
        return [_workflow_dict(r) for r in (await db.scalars(stmt)).all()]


async def get_workflow(settings: Settings, tenant_id: str, workflow_id: str) -> dict[str, Any] | None:
    if _use_memory(settings):
        row = _memory_workflows.get((tenant_id, workflow_id))
        return _workflow_dict(row) if row else None

    async with tenant_session(settings, tenant_id) as db:
        row = await db.get(Workflow, (tenant_id, workflow_id))
        return _workflow_dict(row) if row else None


def _put_in_memory(
    tenant_id: str,
    workflow_id: str,
    definition: dict[str, Any],
    ts: int,
    *,
    precondition: Precondition,
    updated_by: str,
    created_by: str,
    authored_via: str,
) -> dict[str, Any]:
    # No await between the check and the store, so this is atomic on the loop.
    existing = _memory_workflows.get((tenant_id, workflow_id))
    if _precondition_fails(precondition, existing["revision"] if existing else None):
        raise WorkflowConflict(workflow_id, _workflow_dict(existing) if existing else None)
    if existing is None and sum(1 for t, _ in _memory_workflows if t == tenant_id) >= MAX_PER_TENANT:
        raise WorkflowLimitReached(MAX_PER_TENANT)
    # The twin keeps the revision rather than a separate generation, so its rows have exactly the
    # keys a Postgres read returns.
    generation = existing["revision"].rpartition(".")[0] if existing else uuid.uuid4().hex
    version = existing["version"] + 1 if existing else 1
    row = {
        "tenant_id": tenant_id,
        "id": workflow_id,
        "version": version,
        "revision": _revision(generation, version),
        "definition": definition,
        "updated_by": updated_by,
        "created_by": existing["created_by"] if existing else created_by,
        "authored_via": existing["authored_via"] if existing else authored_via,
        "created_at": existing["created_at"] if existing else ts,
        # Past the previous value, so two saves in one millisecond still order.
        "updated_at": max(ts, existing["updated_at"] + 1) if existing else ts,
    }
    _memory_workflows[(tenant_id, workflow_id)] = row
    return _workflow_dict(row)


async def put_workflow(
    settings: Settings,
    tenant_id: str,
    workflow_id: str,
    spec: WorkflowSpec,
    *,
    precondition: Precondition = None,
    updated_by: str = "",
    created_by: str = "",
    authored_via: str = "",
) -> dict[str, Any]:
    """Create or replace a workflow; conditional on ``precondition`` (see module).

    ``created_by`` and ``authored_via`` are recorded when this creates the row and ignored when it
    replaces one: a replace never changes who made a workflow, or through what.
    """
    _check_id(workflow_id)
    definition = spec.model_dump(mode="json", exclude_none=True)
    ts = now_ms()

    if _use_memory(settings):
        return _put_in_memory(
            tenant_id,
            workflow_id,
            definition,
            ts,
            precondition=precondition,
            updated_by=updated_by,
            created_by=created_by,
            authored_via=authored_via,
        )

    async with tenant_session(settings, tenant_id) as db:
        # Locked so the precondition and the write see the same row.
        current = await db.get(Workflow, (tenant_id, workflow_id), with_for_update=True)
        stored = _revision(current.generation, current.version) if current else None
        if _precondition_fails(precondition, stored):
            snapshot = _workflow_dict(current) if current is not None else None
            await db.rollback()
            raise WorkflowConflict(workflow_id, snapshot)
        if current is None:
            # Counted inside the transaction but not locked against another create: two at the
            # cap at once can both land. The cap bounds a tenant's footprint, not an exact count.
            held = await db.scalar(
                select(func.count()).select_from(Workflow).where(Workflow.tenant_id == tenant_id)
            )
            if (held or 0) >= MAX_PER_TENANT:
                await db.rollback()
                raise WorkflowLimitReached(MAX_PER_TENANT)
            current = Workflow(
                tenant_id=tenant_id,
                id=workflow_id,
                version=1,
                generation=uuid.uuid4().hex,
                definition_json=definition,
                updated_by=updated_by,
                created_by=created_by,
                authored_via=authored_via,
                created_at=ts,
                updated_at=ts,
            )
            db.add(current)
        else:
            current.version += 1
            current.definition_json = definition
            current.updated_by = updated_by
            current.updated_at = max(ts, current.updated_at + 1)
        try:
            await db.commit()
        except IntegrityError:
            # Two creates of one id: `FOR UPDATE` locks nothing when there is no row yet, so both
            # passed the check and the second insert hit the primary key. Losing that race is a
            # conflict like any other, unconditional writes included — the winner's definition is
            # now the stored one, and overwriting it unseen is what revisions exist to prevent.
            await db.rollback()
            raise WorkflowConflict(
                workflow_id, await get_workflow(settings, tenant_id, workflow_id)
            ) from None
        return _workflow_dict(current)


async def delete_workflow(
    settings: Settings, tenant_id: str, workflow_id: str, *, precondition: str | None = None
) -> bool:
    """Delete a workflow; with a revision ``precondition``, only that one (else :class:`WorkflowConflict`)."""
    if _use_memory(settings):
        existing = _memory_workflows.get((tenant_id, workflow_id))
        if existing is None:
            return False
        if _precondition_fails(precondition, existing["revision"]):
            raise WorkflowConflict(workflow_id, _workflow_dict(existing))
        del _memory_workflows[(tenant_id, workflow_id)]
        return True

    async with tenant_session(settings, tenant_id) as db:
        stmt = delete(Workflow).where(Workflow.tenant_id == tenant_id, Workflow.id == workflow_id)
        if precondition is not None:
            parsed = _parse_revision(precondition)
            # A revision the store could not have written matches no row: a conflict, below.
            generation, version = parsed if parsed else ("", -1)
            stmt = stmt.where(Workflow.generation == generation, Workflow.version == version)
        result = await db.execute(stmt)
        await db.commit()
        if result.rowcount > 0:
            return True
    if precondition is None:
        return False
    current = await get_workflow(settings, tenant_id, workflow_id)
    if current is None:
        return False
    raise WorkflowConflict(workflow_id, current)


__all__ = [
    "ABSENT",
    "MAX_LIST",
    "MAX_PER_TENANT",
    "Precondition",
    "WorkflowConflict",
    "WorkflowLimitReached",
    "delete_workflow",
    "get_workflow",
    "list_workflows",
    "put_workflow",
    "reset_workflows_for_tests",
]
