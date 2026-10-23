"""Moving an agent's memory pool into a person's: `felix memory assign`.

Every row stored before person pools is in an agent pool, and stays there. Under `scope: person`
a person's read sees only the operator-written rows of it (`memory.store.MemoryPool`), so what the
agent learnt there is invisible until an operator who knows whose memory it really is moves it.

By default only **agent-written** rows move -- what the agent learnt from conversations, which is
the person's. Operator-written rows (`/memory`, ranked above the agent) are curated for the agent
and already reach every person, so they stay unless `include_operator` says otherwise.

The retirer of a duplicate the move finds is named `assign`, which `_TRUST_RANK` does not list,
so it ranks as the agent. Nothing can reactivate the duplicate: it keeps its agent-pool id, which
no write into the person's pool produces, and `put_memory` refuses a write landing on another
pool's row (`MemoryNotStored("other_pool")`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import case, collate, select, update
from sqlalchemy import cast as sa_cast
from sqlalchemy.dialects.postgresql import JSONB

from felix.config import Settings
from felix.db.models import MemoryVector
from felix.db.session import _use_memory, get_session_factory
from felix.memory.store import (
    _DEFAULT_TRUST,
    RETIRED_BY_KEY,
    SUPERSEDED,
    MemoryPool,
    _is_active,
    _memory_rows,
    _owns,
    _owns_sql,
    _principal,
    _row_dict,
    _stamp_retirer,
    _trust,
    _trust_of_column,
    now_ms,
    person_memory_id,
)

ASSIGN_SOURCE = "assign"


@dataclass(slots=True)
class AssignReport:
    """What `assign_to_person` did, or with `dry_run` would do.

    `moved` rows took their person-pool id; `merged` ones found that id already taken -- the
    person's pool held the same sentence, or an older row of this pool did -- and were retired as
    superseded by the row holding it rather than overwriting it.
    """

    scanned: int = 0
    moved: int = 0
    merged: int = 0


def _assign_key(row: dict[str, Any]) -> tuple[int, str]:
    """Oldest first, so between two rows of the pool that map to one id the older keeps it."""
    return (int(row.get("created_at") or 0), str(row["id"]))


def _movable(row: dict[str, Any], include_operator: bool) -> bool:
    return include_operator or _trust(row) <= _DEFAULT_TRUST


def _plan_assign(
    rows: list[dict[str, Any]], person: str, taken: set[str]
) -> tuple[dict[str, str], dict[str, str]]:
    """`(moves, merges)`, each `{old id: person-pool id}`, for `rows` in `_assign_key` order.

    `taken` is every person-pool id that already exists; it is extended as the plan claims ids.
    """
    moves: dict[str, str] = {}
    merges: dict[str, str] = {}
    for row in rows:
        new_id = person_memory_id(person, str(row.get("content") or ""))
        if new_id in taken:
            merges[str(row["id"])] = new_id
        else:
            taken.add(new_id)
            moves[str(row["id"])] = new_id
    return moves, merges


async def assign_to_person(
    settings: Settings,
    tenant_id: str,
    *,
    manifest_id: str,
    person: str,
    include_operator: bool = False,
    dry_run: bool = False,
    batch_size: int = 500,
) -> AssignReport:
    """Move the agent pool `(tenant_id, manifest_id)` -- active rows and retired ones, the agent's
    own unless `include_operator` -- into `person`'s pool. `person` is a person key, already
    resolved (`felix.auth.persons`). Returns what moved and what merged.

    Each row takes its person-pool id (`person_memory_id`) and keeps everything else: content,
    vector, provenance, status, metadata. A `superseded_by` pointing at a moved row follows it.
    A row whose new id is taken is *merged*: it never overwrites the row holding the id, it is
    retired as superseded by it (if it was active) and joins the person's pool under its old id,
    so a re-run finds nothing left to move.

    One transaction per batch on Postgres, under `rls_bypass()` like the other operator sweeps;
    the caller validates its arguments (`felix_cli`).
    """
    if not tenant_id or not manifest_id or not person:
        raise ValueError("a tenant, a manifest and a person are all required")
    if _use_memory(settings):
        return _assign_in_memory(
            tenant_id, manifest_id, person, include_operator=include_operator, dry_run=dry_run
        )
    return await _assign_in_postgres(
        settings,
        tenant_id,
        manifest_id,
        person,
        include_operator=include_operator,
        dry_run=dry_run,
        batch_size=max(1, int(batch_size)),
    )


def _assign_in_memory(
    tenant_id: str, manifest_id: str, person: str, *, include_operator: bool, dry_run: bool
) -> AssignReport:
    source = MemoryPool.agent(manifest_id)
    rows = sorted(
        (
            r
            for (t, _), r in _memory_rows.items()
            if t == tenant_id and _owns(source, r) and _movable(r, include_operator)
        ),
        key=_assign_key,
    )
    taken = {i for (t, i), r in _memory_rows.items() if t == tenant_id and _principal(r) == person}
    moves, merges = _plan_assign(rows, person, taken)
    report = AssignReport(scanned=len(rows), moved=len(moves), merged=len(merges))
    if dry_run:
        return report
    ts = now_ms()
    for row in rows:
        old_id = str(row["id"])
        row["principal"] = person
        if old_id in moves:
            del _memory_rows[(tenant_id, old_id)]
            row["id"] = moves[old_id]
            _memory_rows[(tenant_id, row["id"])] = row
        elif _is_active(row):
            row["status"] = SUPERSEDED
            _stamp_retirer(row, ASSIGN_SOURCE)
            row["superseded_by"] = merges[old_id]
            row["superseded_seq"] = row.get("origin_seq")
            row["updated_at"] = ts
    for (t, _), row in _memory_rows.items():
        if t == tenant_id and row.get("superseded_by") in moves:
            row["superseded_by"] = moves[row["superseded_by"]]
    return report


async def _assign_in_postgres(
    settings: Settings,
    tenant_id: str,
    manifest_id: str,
    person: str,
    *,
    include_operator: bool,
    dry_run: bool,
    batch_size: int,
) -> AssignReport:
    from felix.db.session import rls_bypass

    order = (MemoryVector.created_at.asc(), collate(MemoryVector.id, "C").asc())
    in_pool = [MemoryVector.tenant_id == tenant_id, _owns_sql(MemoryPool.agent(manifest_id))]
    if not include_operator:
        in_pool.append(_trust_of_column(MemoryVector.metadata_json) <= _DEFAULT_TRUST)
    factory = get_session_factory(settings=settings)
    report = AssignReport()
    with rls_bypass():
        while True:
            async with factory() as db:
                stmt = select(MemoryVector).where(*in_pool).order_by(*order)
                # A dry run changes nothing, so one read of the whole pool; a real run reads a
                # batch, moves all of it out of the pool, and reads the next.
                stmt = stmt if dry_run else stmt.limit(batch_size).with_for_update()
                rows = [_row_dict(r) for r in (await db.scalars(stmt)).all()]
                if not rows:
                    return report
                wanted = [person_memory_id(person, str(r.get("content") or "")) for r in rows]
                taken = set(
                    (
                        await db.scalars(
                            select(MemoryVector.id).where(
                                MemoryVector.tenant_id == tenant_id, MemoryVector.id.in_(wanted)
                            )
                        )
                    ).all()
                )
                moves, merges = _plan_assign(rows, person, taken)
                report.scanned += len(rows)
                report.moved += len(moves)
                report.merged += len(merges)
                if dry_run:
                    return report
                await _apply_assign(db, tenant_id, manifest_id, person, rows, moves, merges)
                await db.commit()


async def _apply_assign(
    db: Any,
    tenant_id: str,
    manifest_id: str,
    person: str,
    rows: list[dict[str, Any]],
    moves: dict[str, str],
    merges: dict[str, str],
) -> None:
    """One batch of `_assign_in_postgres`, inside its transaction."""
    ts = now_ms()
    for old_id, new_id in moves.items():
        await db.execute(
            update(MemoryVector)
            .where(MemoryVector.tenant_id == tenant_id, MemoryVector.id == old_id)
            .values(id=new_id, principal=person)
        )
    active_merged = [str(r["id"]) for r in rows if str(r["id"]) in merges and _is_active(r)]
    for old_id in active_merged:
        await db.execute(
            update(MemoryVector)
            .where(MemoryVector.tenant_id == tenant_id, MemoryVector.id == old_id)
            .values(
                status=SUPERSEDED,
                metadata_json=MemoryVector.metadata_json.op("||")(
                    sa_cast({RETIRED_BY_KEY: ASSIGN_SOURCE}, JSONB)
                ),
                superseded_by=merges[old_id],
                superseded_seq=MemoryVector.origin_seq,
                updated_at=ts,
            )
        )
    if merges:
        await db.execute(
            update(MemoryVector)
            .where(MemoryVector.tenant_id == tenant_id, MemoryVector.id.in_(list(merges)))
            .values(principal=person)
        )
    if moves:
        # Pointers into this batch, from rows already moved or still waiting their turn -- both
        # are this manifest's, in the agent pool or the person's.
        await db.execute(
            update(MemoryVector)
            .where(
                MemoryVector.tenant_id == tenant_id,
                MemoryVector.manifest_id == manifest_id,
                MemoryVector.principal.in_(["", person]),
                MemoryVector.superseded_by.in_(list(moves)),
            )
            .values(superseded_by=case(moves, value=MemoryVector.superseded_by))
        )


__all__ = ["ASSIGN_SOURCE", "AssignReport", "assign_to_person"]
