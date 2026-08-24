"""Long-term memory rows: content-addressed, superseded rather than deleted.

Two things make a memory row more than a log line.

**Content-addressed ids.** The id is a hash of the content, scoped by manifest, so
storing the same fact twice collapses instead of accumulating near-duplicates. The
manifest is part of the hash on purpose: the primary key is `(tenant_id, id)`, so
hashing content alone would collide two manifests in one tenant onto one row.

**Supersession, along two axes that must agree.** `status` is current state and is
the only one that can express `forgotten`, which has no position in turn time.
`superseded_seq` closes the row's validity interval in turn time, which is what lets
`as_of` reconstruct what was known at turn N — including facts later replaced, which
a query over `status='active'` cannot do. Every write that closes a memory sets both
in the same statement.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, cast

from sqlalchemy import case, func, null, select, update
from sqlalchemy import cast as sa_cast
from sqlalchemy.dialects.postgresql import JSONB

from felix.config import Settings
from felix.db.models import MemoryVector
from felix.db.session import _use_memory, get_session_factory

logger = logging.getLogger("felix.memory")

ACTIVE = "active"
SUPERSEDED = "superseded"
FORGOTTEN = "forgotten"


def now_ms() -> int:
    return int(time.time() * 1000)


_memory_rows: dict[tuple[str, str], dict[str, Any]] = {}


def memory_id(manifest_id: str, content: str) -> str:
    """A stable id for this content under this manifest.

    Whitespace-normalised and lowercased so trivially different renderings of the same
    sentence land on the same row.
    """
    normalized = " ".join((content or "").lower().split())
    return hashlib.sha256(f"{manifest_id}\x00{normalized}".encode()).hexdigest()[:32]


def _row_dict(row: MemoryVector | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return {
        "tenant_id": row.tenant_id,
        "id": row.id,
        "kind": row.kind,
        "manifest_id": row.manifest_id,
        "content": row.content,
        "metadata": row.metadata_json,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "last_used_at": row.last_used_at,
        "thread_id": row.thread_id,
        "topic_key": row.topic_key,
        "status": row.status,
        "superseded_by": row.superseded_by,
        "importance": row.importance,
        "origin_seq": row.origin_seq,
        "superseded_seq": row.superseded_seq,
        "embedding_dim": row.embedding_dim,
        "embedding_model": row.embedding_model,
        "embedding_json": row.embedding_json,
    }


def _is_active(row: dict[str, Any]) -> bool:
    return str(row.get("status") or ACTIVE) == ACTIVE


async def current_turn_seq(settings: Settings, tenant_id: str, *, manifest_id: str = "") -> int:
    """The highest turn ordinal any memory in this scope was written at (0 if none)."""
    if _use_memory(settings):
        seqs = [
            int(r.get("origin_seq") or 0)
            for (t, _), r in _memory_rows.items()
            if t == tenant_id and (not manifest_id or r.get("manifest_id") == manifest_id)
        ]
        return max(seqs, default=0)

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        stmt = select(func.coalesce(func.max(MemoryVector.origin_seq), 0)).where(
            MemoryVector.tenant_id == tenant_id
        )
        if manifest_id:
            stmt = stmt.where(MemoryVector.manifest_id == manifest_id)
        return int(await db.scalar(stmt) or 0)


async def put_memory(
    settings: Settings,
    tenant_id: str,
    *,
    content: str,
    kind: str = "fact",
    manifest_id: str = "",
    origin_seq: int | None = None,
    metadata: dict[str, Any] | None = None,
    supersedes_id: str | None = None,
    topic_key: str | None = None,
    importance: float = 0.5,
    thread_id: str = "",
    embedding: list[float] | None = None,
    embedding_model: str = "",
) -> dict[str, Any]:
    """Store a memory, superseding active rows that share its ``topic_key``.

    Supersession is conditional on the writer: a row is retired only by a writer of
    equal or greater standing (``_TRUST_RANK``). A lower-ranked write is stored
    *alongside* the row it cannot displace, so a topic can briefly carry two active
    values — a visible contradiction rather than a silent deletion, which is the
    better failure but is not the invariant the older docstrings promised.

    Idempotent by content: re-storing the same text under the same manifest reactivates
    the existing row and keeps its original provenance, rather than adding a second
    copy that recall would then return twice.

    Wide by design — a memory is a wide record, and every field past ``content`` is
    keyword-only with a default, so callers name what they mean.
    """
    # Truncated, not rejected: this is on the turn path, and a capture that raises
    # would fail the user's turn over a memory. The id is derived from the bounded
    # text so re-storing the same over-long content stays idempotent.
    # Stripped rather than trusted. No writer sets it today, but the invariant should
    # not depend on two `case` expressions staying in step with each other.
    metadata = {k: v for k, v in (metadata or {}).items() if k != RETIRED_BY_KEY}
    content = (content or "")[:MAX_CONTENT_CHARS]
    # Stripped, so a blank key cannot exist. Without this the approval gate and the
    # store disagreed about what "empty" means: `when_args` treats "   " as absent and
    # lets the call through ungated, while this line kept it as a real key and ran the
    # topic sweep under it. Bounded -- the sweep matches byte-identically, so it could
    # only reach rows written through the same ungated path, never a curated one -- but
    # it is two components disagreeing about emptiness, which is the exact shape of the
    # last several bugs here. Stripping also stops near-duplicate keys that differ only
    # by whitespace from being separately storable. Stripped on both sides of the
    # truncation, because each order alone is wrong at the boundary: cut-then-strip
    # turns 250 leading spaces plus content into "" while the gate still sees content
    # -- a presence disagreement, the exact thing the test below pins -- and
    # strip-then-cut leaves an over-long key ending in whitespace after the cut.
    topic_key = (topic_key or "").strip()[:MAX_TOPIC_KEY_CHARS].strip() or None
    mem_id = memory_id(manifest_id, content)
    ts = now_ms()
    row: dict[str, Any] = {
        "tenant_id": tenant_id,
        "id": mem_id,
        "kind": kind,
        "manifest_id": manifest_id,
        "content": content,
        "metadata": metadata or {},
        "created_at": ts,
        "updated_at": ts,
        "last_used_at": None,
        "thread_id": thread_id,
        "topic_key": topic_key,
        "status": ACTIVE,
        "superseded_by": None,
        "importance": max(0.0, min(float(importance), 1.0)),
        "origin_seq": origin_seq,
        "superseded_seq": None,
        "embedding_dim": len(embedding) if embedding else None,
        "embedding_model": embedding_model,
        "embedding_json": None,
    }

    if supersedes_id:
        # The ordinal a supersession closes at is this turn's, not the old row's.
        await supersede(
            settings,
            tenant_id,
            supersedes_id,
            origin_seq,
            superseded_by=mem_id,
            # Named, or this always ran at agent rank -- so the guard added to
            # `supersede` turned an operator write carrying `supersedes_id` into a
            # silent no-op that still reported success.
            source=str((metadata or {}).get("source") or ""),
        )

    if _use_memory(settings):
        # The twin keeps the vector inline; Postgres keeps it in a column the ORM
        # cannot see. Either way recall must be able to reach it, or the vector
        # channel is untestable on the path CI actually runs.
        row["embedding"] = list(embedding) if embedding else None
        return _put_in_memory(row)
    await _put_in_postgres(settings, row, embedding=embedding)
    return row


# Writers, most trusted first. `management_api` is an authenticated operator holding
# `memory:write`; the rest are the agent acting on its own, and `assistant` in
# particular is auto-capture from a turn that may repeat hostile tool output.
#
# The ranking exists because supersession and upsert both used to ignore it. A
# `topic_key` is chosen by the extractor from the transcript, so an injected payload
# could name the key of an operator-curated memory and silently retire it -- an
# attacker who cannot write a memory could still delete the one protecting you. The
# content-hash id gave the same result by a second route: re-remembering the exact
# text of a curated row rewrote its `kind` and provenance to the new writer's.
# Bounded at the store rather than at one route. The management API already capped
# content at 4000 chars, on the reasoning that "a memory long enough to carry a whole
# instruction set is not a memory" -- but the capture path wrote straight past it, and
# capture is the writer whose content is model-authored from an untrusted turn. The
# bound belongs where every writer passes.
MAX_CONTENT_CHARS = 4000
MAX_TOPIC_KEY_CHARS = 200

# Two ranks, not four. `remember_tool` and `remember_procedure` sat above capture on
# the reasoning that a tool write traverses the governance stack -- but all three are
# the agent acting on a transcript it does not control, and `remember` is the one a
# prompt injection can invoke *directly* with attacker-chosen content and topic_key.
# Ranking it above capture would have let injection win every supersession race
# against auto-captured memory. The line that means something is operator versus
# agent, so that is the line this draws.
_TRUST_RANK = {"management_api": 2}
_DEFAULT_TRUST = 1


def _trust_of_column(metadata_col: Any) -> Any:
    """`_trust` as a SQL expression over the stored metadata JSON.

    One ranking, two dialects. Kept beside `_TRUST_RANK` so a writer added to one is
    not missed in the other — the in-memory twin and Postgres disagreeing about who
    may displace whom is the kind of difference CI would never show, because the
    memory:// arm is the arm CI runs.
    """
    source = metadata_col.op("->>")("source")
    whens = [(source == name, rank) for name, rank in _TRUST_RANK.items()]
    return case(*whens, else_=_DEFAULT_TRUST)


def _loggable(value: object) -> str:
    """A caller-supplied identifier, made safe to put in a log line.

    `memory_id` on the retirement routes comes straight from a tool call, so the model
    chooses it -- and a newline in it forges a log entry. That matters more here than
    the usual because these particular lines record refusals: an attacker who can write
    "refusing to forget ..." into the log can make a retirement that never happened look
    like one that was declined, which is the opposite of what the audit trail is for.

    Legitimate ids are hex content hashes, so this is lossless for every real value and
    only ever truncates something that was already not an id.
    """
    text = str(value)
    kept = "".join(ch for ch in text if ch.isalnum() or ch in "._:-")
    return (kept[:64] or "<unprintable>") + ("…" if len(kept) > 64 else "")


def _rank(source: str) -> int:
    return _TRUST_RANK.get(str(source or ""), _DEFAULT_TRUST)


# The metadata key naming who retired a row. Distinct from `source`, which names who
# *wrote* it: forgetting is a decision by whoever forgot, and gating resurrection on
# the writer's rank meant an operator deleting an agent-written row -- which is nearly
# every row, and exactly the population the memory route exists to clean up -- left a
# rank-1 row that any rank-1 writer reactivated by re-storing the same text.
# One key rather than one per route. `forget` stamped it and supersession did not, so
# an operator correcting a stale memory the *documented preferred way* -- remember the
# new value under the same topic_key -- produced a retirement nothing recorded, and an
# injected turn restating the stale sentence brought it back.
RETIRED_BY_KEY = "retired_by"


def trust_of(row: dict[str, Any]) -> int:
    """The writer rank recorded on a stored row. Public: the prelude ranks by it."""
    return _trust(row)


def _retirer_rank_of_column(metadata_col: Any) -> Any:
    """`_retirer_rank` as SQL. Falls back to the writer rank when nothing forgot it."""
    retirer = metadata_col.op("->>")(RETIRED_BY_KEY)
    whens = [(retirer == name, rank) for name, rank in _TRUST_RANK.items()]
    return case(
        # `""` too, not only NULL: Python tests truthiness here and SQL tested
        # IS NULL, so an empty stamp fell back on one arm and did not on the other.
        ((retirer.is_(None)) | (retirer == ""), _trust_of_column(metadata_col)),
        *whens,
        else_=_DEFAULT_TRUST,
    )


def _stamp_retirer(row: dict[str, Any], source: str) -> None:
    """Record who retired a row. Callers must have passed the rank check first."""
    metadata = dict(row.get("metadata") or {})
    metadata[RETIRED_BY_KEY] = source
    row["metadata"] = metadata
    row["metadata_json"] = metadata


def _retirer_rank(row: dict[str, Any]) -> int:
    """The rank of whoever retired this row, or the writer's if nobody has."""
    metadata = row.get("metadata") or row.get("metadata_json") or {}
    if isinstance(metadata, dict) and metadata.get(RETIRED_BY_KEY):
        return _rank(str(metadata[RETIRED_BY_KEY]))
    return _trust(row)


def _trust(row: dict[str, Any]) -> int:
    metadata = row.get("metadata") or row.get("metadata_json") or {}
    source = metadata.get("source") if isinstance(metadata, dict) else None
    return _rank(str(source or ""))


# Everything a refused write may not change: what the row *is*, and whether it is
# seen. Every field found missing from this set was found by a separate review round
# -- `kind`, then `topic_key` and `importance`, then `metadata`, then `status`.
_PRESERVED_ON_REFUSAL = (
    "kind",
    "topic_key",
    "importance",
    "status",
    "superseded_by",
    "superseded_seq",
)


def _preserve(row: dict[str, Any], existing: dict[str, Any]) -> None:
    """Keep the existing row's identity and visibility; take only its new content."""
    for field in _PRESERVED_ON_REFUSAL:
        if field in existing:
            row[field] = existing[field]
    row["metadata"] = existing.get("metadata_json") or existing.get("metadata") or row["metadata"]


def _may_reactivate(incoming: dict[str, Any], existing: dict[str, Any]) -> bool:
    """Whether `incoming` may bring a forgotten row back.

    Gated on who *forgot* it rather than who wrote it: re-storing the same normalised
    text is something an injected turn causes with no tool at all, so an operator
    cleaning up an agent-written row -- nearly every row, and exactly the population
    the memory route exists to clean up -- must not be undone by the agent that wrote
    it. The operator can still undo their own forget, and the agent its own.
    """
    if (existing.get("status") or ACTIVE) == ACTIVE:
        return True
    # SUPERSEDED counts, not just FORGOTTEN. It is the state the *documented*
    # correction path produces -- "prefer remembering the new value under the same
    # topic_key" -- so leaving it unprotected made the recommended remedy the
    # non-durable one, undone by any turn that restates the stale sentence.
    # `_trust(incoming)` rather than an open-coded copy of it, so a change to the
    # ranking cannot reach the SQL arm and miss this one.
    return _trust(incoming) >= _retirer_rank(existing)


def _may_displace(incoming: dict[str, Any], existing: dict[str, Any]) -> bool:
    """Whether `incoming` may retire or rewrite `existing`.

    Equal rank is allowed: two captures on one topic are the ordinary case, and the
    newer value is meant to win. Only a *lower*-ranked writer is refused.
    """
    return _trust(incoming) >= _trust(existing)


def _put_in_memory(row: dict[str, Any]) -> dict[str, Any]:
    tenant_id, mem_id = row["tenant_id"], row["id"]
    if row["topic_key"]:
        for (other_tenant, other_id), other in _memory_rows.items():
            if (
                other_tenant == tenant_id
                and other_id != mem_id
                and other.get("manifest_id") == row["manifest_id"]
                and other.get("topic_key") == row["topic_key"]
                and _is_active(other)
                and _may_displace(row, other)
            ):
                other["status"] = SUPERSEDED
                _stamp_retirer(other, str((row.get("metadata") or {}).get("source") or ""))
                other["superseded_by"] = mem_id
                other["superseded_seq"] = row["origin_seq"]
                other["updated_at"] = row["updated_at"]

    existing = _memory_rows.get((tenant_id, mem_id))
    if existing is not None:
        # Reactivating keeps the first write's provenance; a later one only fills a gap.
        row["created_at"] = existing.get("created_at", row["created_at"])
        if existing.get("origin_seq") is not None:
            row["origin_seq"] = existing["origin_seq"]
        # Two independent reasons a write may be refused, and one definition of what
        # "refused" means. They were separate blocks preserving different field sets,
        # which let an agent rewrite the kind, topic_key and importance of a row the
        # operator had forgotten: it could not resurrect the row, but it could *set up*
        # what came back if the operator ever restored it -- including a topic_key,
        # which retires whatever else holds that key.
        if not _may_displace(row, existing) or not _may_reactivate(row, existing):
            _preserve(row, existing)

    _memory_rows[(tenant_id, mem_id)] = {**row, "metadata_json": row["metadata"]}
    return row


def _refused_in_sql(table: Any, row: dict[str, Any]) -> Any:
    """The SQL twin of `not _may_displace(...) or not _may_reactivate(...)`.

    One expression, used by every preserved column, because the columns disagreeing
    about what "refused" means is precisely the bug this replaced: `status` grew a
    forgotten branch and `metadata` did not, so a refused write kept the row hidden
    while erasing the stamp that kept it hidden.
    """
    outranked = _trust_of_column(table.c["metadata"]) > _trust(row)
    # `!= ACTIVE`, mirroring `_may_reactivate`, not `== FORGOTTEN`. Renaming the helper
    # without re-scoping it left SUPERSEDED unprotected on this arm only -- and
    # SUPERSEDED is the state the documented correction path produces.
    #
    # `coalesce` rather than a bare `!=` so a NULL status fails closed the same way
    # `(existing.get("status") or ACTIVE)` does on the Python side, instead of going
    # NULL -> false by accident.
    cannot_reactivate = (func.coalesce(table.c.status, ACTIVE) != ACTIVE) & (
        _retirer_rank_of_column(table.c["metadata"]) > _trust(row)
    )
    return outranked | cannot_reactivate


async def _put_in_postgres(settings: Settings, row: dict[str, Any], *, embedding: list[float] | None) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    tenant_id, mem_id = row["tenant_id"], row["id"]
    factory = get_session_factory(settings=settings)
    async with factory() as db:
        # Supersession and insert share one transaction: a crash between them would
        # otherwise leave a topic with two active rows, or none.
        if row["topic_key"]:
            await db.execute(
                update(MemoryVector)
                .where(
                    MemoryVector.tenant_id == tenant_id,
                    MemoryVector.manifest_id == row["manifest_id"],
                    MemoryVector.topic_key == row["topic_key"],
                    MemoryVector.status == ACTIVE,
                    MemoryVector.id != mem_id,
                    # Same rule as the in-memory arm: an automatic writer may not
                    # retire a curated row by naming its topic_key.
                    _trust_of_column(MemoryVector.metadata_json) <= _trust(row),
                )
                .values(
                    status=SUPERSEDED,
                    # Mirrors `_stamp_retirer` on the other arm. Without it
                    # `_retirer_rank` falls back to the retired row's own writer --
                    # rank 1 for a captured row -- and the next agent write brings it
                    # back, undoing an operator's correction.
                    metadata_json=MemoryVector.metadata_json.op("||")(
                        sa_cast(
                            {RETIRED_BY_KEY: str((row.get("metadata") or {}).get("source") or "")},
                            JSONB,
                        )
                    ),
                    superseded_by=mem_id,
                    superseded_seq=row["origin_seq"],
                    updated_at=row["updated_at"],
                )
            )

        # Target the Table, not the ORM class, and address columns by name. The
        # column is `metadata`, but `MemoryVector.metadata` is SQLAlchemy's own
        # MetaData object, so any attribute-based resolution of that key finds the
        # wrong thing and fails deep inside the insert path.
        # `__table__` is declared as `FromClause` but is a `Table` at runtime, and
        # `pg_insert` wants the narrower type; cast rather than lie in an annotation.
        table = cast(Any, MemoryVector.__table__)
        values = {k: v for k, v in row.items() if k not in {"last_used_at", "embedding_json", "embedding"}}
        stmt = pg_insert(table).values(values)
        excluded = stmt.excluded
        refused = _refused_in_sql(table, row)
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "id"],
            set_={
                # Every column that decides what the row is or whether it is seen
                # takes the *same* refusal predicate. They branched differently once
                # and the divergence was live in production for one arm only.
                "kind": case((refused, table.c.kind), else_=excluded["kind"]),
                "content": excluded["content"],
                "metadata": case((refused, table.c["metadata"]), else_=excluded["metadata"]),
                "topic_key": case((refused, table.c.topic_key), else_=excluded["topic_key"]),
                "importance": case((refused, table.c.importance), else_=excluded["importance"]),
                "thread_id": excluded["thread_id"],
                "status": case((refused, table.c.status), else_=ACTIVE),
                "superseded_by": case((refused, table.c.superseded_by), else_=null()),
                "superseded_seq": case((refused, table.c.superseded_seq), else_=null()),
                "updated_at": excluded["updated_at"],
                # Re-remembering reactivates a row; it does not rewrite its history,
                # so the first write's provenance wins and a later one only fills a gap.
                "origin_seq": func.coalesce(table.c.origin_seq, excluded["origin_seq"]),
            },
        )
        await db.execute(stmt)
        if embedding:
            await _write_embedding(db, tenant_id, mem_id, embedding, model=row["embedding_model"])
        await db.commit()


_warned_embedding_dim = False


async def _write_embedding(db: Any, tenant_id: str, mem_id: str, vector: list[float], *, model: str) -> None:
    """Set the pgvector column, which has no ORM representation.

    A vector whose length does not match the column is rejected loudly. Postgres would
    reject it anyway, and the previous behaviour — swallow the error into a debug log —
    is how this table came to look like it worked while storing nothing: a misconfigured
    embedder would produce a memory store with no vectors and no indication why.
    """
    global _warned_embedding_dim
    from sqlalchemy import text as sa_text

    expected = await _configured_dim(db)
    if expected is not None and len(vector) != expected:
        if not _warned_embedding_dim:
            _warned_embedding_dim = True
            logger.warning(
                "embedding is %d-dimensional but memory_vectors.embedding is vector(%d); "
                "vectors are not being stored and recall will run without them. Set "
                "FELIX_MEMORY_EMBEDDING_MODEL to a model of the right size, or rebuild "
                "the column.",
                len(vector),
                expected,
            )
        return

    literal = "[" + ",".join(repr(float(v)) for v in vector) + "]"
    await db.execute(
        sa_text(
            "UPDATE memory_vectors SET embedding = CAST(:vec AS vector), "
            "embedding_dim = :dim, embedding_model = :model "
            "WHERE tenant_id = :tenant AND id = :id"
        ),
        {"vec": literal, "dim": len(vector), "model": model, "tenant": tenant_id, "id": mem_id},
    )


async def _configured_dim(db: Any) -> int | None:
    """The dimension the vector column was built at, per `memory_vector_config`."""
    from sqlalchemy import text as sa_text

    try:
        return await db.scalar(sa_text("SELECT dim FROM memory_vector_config WHERE id = 1"))
    except Exception:
        logger.debug("memory_vector_config unreadable", exc_info=True)
        return None


async def supersede(
    settings: Settings,
    tenant_id: str,
    memory_id: str,
    at_seq: int | None,
    *,
    superseded_by: str | None = None,
    source: str = "",
) -> None:
    """Close a memory's validity interval at ``at_seq``, on both axes.

    `source` is the caller's writer identity and is checked, like `forget`. This is
    the third route by which a row leaves recall and it had no predicate at all --
    currently unreferenced in the tree, but exported, so it is a way in that would not
    have shown up in a grep for callers. A caller that does not name itself is treated
    as the agent.
    """
    ts = now_ms()
    if _use_memory(settings):
        row = _memory_rows.get((tenant_id, memory_id))
        if row is not None:
            # The higher of the two. Testing only the writer let an agent supersede a
            # row the operator had forgotten -- which does not resurrect it, but moves
            # it to a state `_may_reactivate` used to wave through, laundering the
            # forget.
            if _rank(source) < max(_trust(row), _retirer_rank(row)):
                logger.warning("refusing to supersede a more-trusted memory id=%s", _loggable(memory_id))
                return
            row["status"] = SUPERSEDED
            _stamp_retirer(row, source)
            row["superseded_seq"] = at_seq
            row["superseded_by"] = superseded_by
            row["updated_at"] = ts
        return
    factory = get_session_factory(settings=settings)
    async with factory() as db:
        await db.execute(
            update(MemoryVector)
            .where(
                MemoryVector.tenant_id == tenant_id,
                MemoryVector.id == memory_id,
                # Same predicates as the in-memory arm; see the docstring.
                _trust_of_column(MemoryVector.metadata_json) <= _rank(source),
                _retirer_rank_of_column(MemoryVector.metadata_json) <= _rank(source),
            )
            .values(
                status=SUPERSEDED,
                metadata_json=MemoryVector.metadata_json.op("||")(sa_cast({RETIRED_BY_KEY: source}, JSONB)),
                superseded_seq=at_seq,
                superseded_by=superseded_by,
                updated_at=ts,
            )
        )
        await db.commit()


async def forget(settings: Settings, tenant_id: str, memory_id: str, *, source: str = "") -> bool:
    """Hide a memory from recall without deleting it.

    `forgotten` has no turn-time endpoint on purpose: it is an out-of-band decision by
    an operator, not something a turn did, so it must not appear as a supersession in
    an as-of reconstruction.

    `source` is the caller's writer identity, and it is checked. This is the third way
    a row can leave recall -- supersession and upsert are the other two -- and it was
    the one with no trust predicate on it, so the `forget` tool reached by a prompt
    injection could retire an operator-curated memory that the same injection could
    not have overwritten. A caller that does not name itself is treated as the agent.
    """
    ts = now_ms()
    if _use_memory(settings):
        row = _memory_rows.get((tenant_id, memory_id))
        if row is None:
            return False
        if _rank(source) < _trust(row):
            logger.warning("refusing to forget a more-trusted memory id=%s", _loggable(memory_id))
            return False
        row["status"] = FORGOTTEN
        metadata = dict(row.get("metadata") or {})
        # Monotone. Assigning unconditionally let an agent forget an already-forgotten
        # row -- permitted, because the entry check compares against the *writer's*
        # rank, which is 1 for nearly every row -- and thereby downgrade the stamp from
        # the operator who forgot it, re-arming its own resurrection. The approval
        # prompt says "Confirm retiring a stored memory", which is not what that call
        # does when the row is already retired.
        if _rank(source) < _retirer_rank(row):
            # The Postgres arm expresses this test in the WHERE, so the statement
            # matches nothing and rowcount is 0. Returning True here would make the
            # twins answer differently for the same call, and `_forget_tool` turns
            # that into two different strings back to the model. False is the honest
            # answer on both: the call changed nothing.
            logger.warning("refusing to downgrade the forgetter of memory id=%s", _loggable(memory_id))
            return False
        metadata[RETIRED_BY_KEY] = source
        row["metadata"] = metadata
        row["metadata_json"] = metadata
        row["updated_at"] = ts
        return True
    factory = get_session_factory(settings=settings)
    async with factory() as db:
        result = await db.execute(
            update(MemoryVector)
            .where(
                MemoryVector.tenant_id == tenant_id,
                MemoryVector.id == memory_id,
                # Same predicates as the in-memory arm; see the docstring. The
                # second makes a refused downgrade a no-op rather than a rewrite.
                _trust_of_column(MemoryVector.metadata_json) <= _rank(source),
                _retirer_rank_of_column(MemoryVector.metadata_json) <= _rank(source),
            )
            .values(
                status=FORGOTTEN,
                updated_at=ts,
                # jsonb || jsonb merges, so this preserves `source` and any other keys.
                metadata_json=MemoryVector.metadata_json.op("||")(sa_cast({RETIRED_BY_KEY: source}, JSONB)),
            )
        )
        await db.commit()
        # `execute` is typed as returning `Result`, but an UPDATE yields a
        # `CursorResult`, which is where `rowcount` lives.
        return bool(getattr(result, "rowcount", 0))


async def get_many(settings: Settings, tenant_id: str, ids: list[str]) -> dict[str, dict[str, Any]]:
    """Resolve many memories in one query — recall fuses candidate ids before reading."""
    if not ids:
        return {}
    if _use_memory(settings):
        return {
            mem_id: _row_dict(row)
            for mem_id in ids
            if (row := _memory_rows.get((tenant_id, mem_id))) is not None
        }
    factory = get_session_factory(settings=settings)
    async with factory() as db:
        rows = (
            await db.scalars(
                select(MemoryVector).where(MemoryVector.tenant_id == tenant_id, MemoryVector.id.in_(ids))
            )
        ).all()
        return {row.id: _row_dict(row) for row in rows}


async def list_active(
    settings: Settings,
    tenant_id: str,
    *,
    manifest_id: str = "",
    kind: str | None = None,
    limit: int = 50,
    prioritized: bool = False,
) -> list[dict[str, Any]]:
    """Active rows for a tenant, newest first.

    `prioritized` orders by writer trust, then importance, then recency *before* the
    limit applies. Recall needs that: ranking after a recency-ordered fetch cannot see
    a curated row that fell outside the window, and a busy tenant crosses any window
    in ordinary use -- so an operator's correction silently stopped being shown.
    """
    if _use_memory(settings):
        items = [
            _row_dict(r)
            for (t, _), r in _memory_rows.items()
            if t == tenant_id
            and _is_active(r)
            and (not manifest_id or r.get("manifest_id") == manifest_id)
            and (kind is None or r.get("kind") == kind)
        ]
        if prioritized:
            items.sort(
                key=lambda r: (_trust(r), float(r.get("importance") or 0.0), r["created_at"]),
                reverse=True,
            )
        else:
            items.sort(key=lambda r: r["created_at"], reverse=True)
        return items[:limit]

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        stmt = select(MemoryVector).where(
            MemoryVector.tenant_id == tenant_id,
            MemoryVector.status == ACTIVE,
        )
        if manifest_id:
            stmt = stmt.where(MemoryVector.manifest_id == manifest_id)
        if kind:
            stmt = stmt.where(MemoryVector.kind == kind)
        if prioritized:
            stmt = stmt.order_by(
                _trust_of_column(MemoryVector.metadata_json).desc(),
                MemoryVector.importance.desc(),
                MemoryVector.created_at.desc(),
            )
        else:
            stmt = stmt.order_by(MemoryVector.created_at.desc())
        stmt = stmt.limit(limit)
        return [_row_dict(r) for r in (await db.scalars(stmt)).all()]


async def as_of(
    settings: Settings,
    tenant_id: str,
    turn_seq: int,
    *,
    manifest_id: str = "",
    kind: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """The memories that were current at turn ``turn_seq``.

    Includes facts that were later superseded, which is the whole point — a query over
    `status='active'` shows what is believed now, not what was believed then. Rows that
    predate provenance have a null `origin_seq` and read as genesis, so they appear in
    every as-of view.
    """
    if _use_memory(settings):
        items = [
            _row_dict(r)
            for (t, _), r in _memory_rows.items()
            if t == tenant_id
            and (not manifest_id or r.get("manifest_id") == manifest_id)
            and (kind is None or r.get("kind") == kind)
            and int(r.get("origin_seq") or 0) <= turn_seq
            and (r.get("superseded_seq") is None or int(r["superseded_seq"]) > turn_seq)
        ]
        items.sort(key=lambda r: r["created_at"], reverse=True)
        return items[:limit]

    from sqlalchemy import or_

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        stmt = select(MemoryVector).where(
            MemoryVector.tenant_id == tenant_id,
            func.coalesce(MemoryVector.origin_seq, 0) <= turn_seq,
            or_(MemoryVector.superseded_seq.is_(None), MemoryVector.superseded_seq > turn_seq),
        )
        if manifest_id:
            stmt = stmt.where(MemoryVector.manifest_id == manifest_id)
        if kind:
            stmt = stmt.where(MemoryVector.kind == kind)
        stmt = stmt.order_by(MemoryVector.created_at.desc()).limit(limit)
        return [_row_dict(r) for r in (await db.scalars(stmt)).all()]


async def consolidate_pools(settings: Settings, *, max_facts: int = 500) -> int:
    """Exact content-hash dedupe of active facts (not LLM summarization).

    Largely vestigial now that ids are content hashes — a duplicate collapses on write
    rather than accumulating — but it still cleans up rows written before that, and
    rows whose text differs only by whitespace or case.

    ``max_facts`` caps how many active rows are scanned per pass. Returns rows
    superseded.
    """
    scan_limit = max(1, min(int(max_facts), 5000))

    if _use_memory(settings):
        seen: dict[tuple[str, str, str], str] = {}
        superseded = 0
        active = [
            ((tenant_id, mem_id), row) for (tenant_id, mem_id), row in _memory_rows.items() if _is_active(row)
        ]
        active.sort(key=lambda item: int(item[1].get("created_at") or 0))
        for (tenant_id, mem_id), row in active[:scan_limit]:
            key = (tenant_id, row.get("manifest_id", ""), row.get("content", ""))
            if key in seen:
                row["status"] = SUPERSEDED
                row["superseded_by"] = seen[key]
                # Deliberately NOT a timestamp. This column is a turn ordinal, and
                # writing now_ms() into it — as this did — makes every later as-of
                # comparison wrong by thirteen orders of magnitude.
                row["superseded_seq"] = row.get("origin_seq")
                superseded += 1
            else:
                seen[key] = mem_id
        return superseded

    # Cross-tenant sweep, so it must bypass RLS the way the retention job does —
    # without this the worker cron silently sees zero rows under FELIX_DATABASE_RLS.
    from felix.db.session import rls_bypass

    factory = get_session_factory(settings=settings)
    superseded = 0
    with rls_bypass():
        async with factory() as db:
            rows = (
                await db.scalars(
                    select(MemoryVector)
                    .where(MemoryVector.status == ACTIVE)
                    .order_by(MemoryVector.created_at.asc())
                    .limit(scan_limit)
                )
            ).all()
            seen_pg: dict[tuple[str, str, str], str] = {}
            for row in rows:
                key = (row.tenant_id, row.manifest_id, row.content)
                if key in seen_pg:
                    row.status = SUPERSEDED
                    row.superseded_by = seen_pg[key]
                    row.superseded_seq = row.origin_seq
                    row.updated_at = now_ms()
                    superseded += 1
                else:
                    seen_pg[key] = row.id
            await db.commit()
    return superseded


__all__ = [
    "ACTIVE",
    "FORGOTTEN",
    "SUPERSEDED",
    "as_of",
    "consolidate_pools",
    "current_turn_seq",
    "forget",
    "get_many",
    "list_active",
    "memory_id",
    "put_memory",
    "supersede",
]
