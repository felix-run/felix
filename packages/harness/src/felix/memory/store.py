"""Long-term memory rows: content-addressed, superseded rather than deleted.

Two things make a memory row more than a log line.

**Content-addressed ids.** The id is a hash of the content, scoped by the row's pool, so
storing the same fact twice collapses instead of accumulating near-duplicates. The pool is
part of the hash on purpose: the primary key is `(tenant_id, id)`, so hashing content alone
would collide two pools in one tenant onto one row.

**Pools.** A row belongs to one pool. An *agent* pool is `(tenant, manifest_id, principal='')`:
what one agent knows, the only kind there was before people had memory of their own, and where
an operator's curated rows for an agent live. A *person* pool is `(tenant, principal=person)`:
what every agent acting for that person knows about them, whichever agent wrote it --
`manifest_id` stays on the row as provenance and is no part of the pool. Person isolation is
this module's filter, not a database policy: RLS isolates tenants, and inside one tenant every
read and write below names the pool it may touch (`MemoryPool`).

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
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple, cast

from sqlalchemy import and_, case, collate, false, func, null, or_, select, true, update
from sqlalchemy import cast as sa_cast
from sqlalchemy.dialects.postgresql import JSONB

from felix.config import Settings
from felix.db.models import MemoryVector
from felix.db.session import _use_memory, get_session_factory
from felix.logging_setup import loggable

logger = logging.getLogger("felix.memory")

ACTIVE = "active"
SUPERSEDED = "superseded"
FORGOTTEN = "forgotten"


def normalize_topic_key(topic_key: str | None) -> str | None:
    """A topic key as stored: stripped, capped, stripped again; `None` when empty."""
    return (topic_key or "").strip()[:MAX_TOPIC_KEY_CHARS].strip() or None


def now_ms() -> int:
    return int(time.time() * 1000)


_memory_rows: dict[tuple[str, str], dict[str, Any]] = {}


def reset_memory_for_tests() -> None:
    """Drop every in-memory row; the test suite's autouse reset calls this around each test."""
    _memory_rows.clear()


def _normalized(content: str) -> str:
    """Whitespace-collapsed and lowercased, so trivially different renderings share an id."""
    return " ".join((content or "").lower().split())


def memory_id(manifest_id: str, content: str) -> str:
    """A stable id for this content in this manifest's agent pool.

    Unchanged since before person pools, so no existing row's id moved when they arrived.
    """
    return hashlib.sha256(f"{manifest_id}\x00{_normalized(content)}".encode()).hexdigest()[:32]


# Every person-pool id starts with this, and an agent-pool id is lowercase hex throughout, so the
# two can never be equal whatever a manifest id or a content string holds -- a namespace inside
# the hashed text could be forged by a manifest named after it, a prefix on the digest cannot.
PERSON_ID_PREFIX = "p"


def person_memory_id(principal: str, content: str) -> str:
    """A stable id for this content in `principal`'s pool, whichever agent writes it.

    The principal is length-prefixed, so no principal-and-content pair can spell another's.
    """
    digest = hashlib.sha256(f"{len(principal)}:{principal}\x00{_normalized(content)}".encode()).hexdigest()
    return PERSON_ID_PREFIX + digest[:31]


@dataclass(frozen=True, slots=True)
class MemoryPool:
    """Whose memory an agent's call reads and writes. Built by `felix.memory.scope.memory_pool`.

    - `MemoryPool.agent(m)` -- `spec.memory.scope: agent`: the agent's own pool, read and written,
      every row of it, as before people had memory of their own.
    - `MemoryPool.person(m, p)` -- `scope: person` for person key `p`: writes go to `p`'s pool, and
      reads see it plus the **operator-written** rows of `m`'s agent pool (`_TRUST_RANK` above the
      agent's), so what an operator curated for the agent reaches every person while an agent's own
      row there -- written before person pools, or by a person-less run -- reaches no one's prompt.
    - `MemoryPool.personless(m)` -- `scope: person` with no person (anonymous, a shared key, a
      machine actor for no one): the same operator rows, and **no writes** -- there is nobody to
      remember them for, and a pool every such caller shared would mix them all.

    `manifest_id` is the agent either way: the writer recorded on a new row (provenance; it is no
    part of a person's pool) and the agent pool read beside it.
    """

    manifest_id: str
    principal: str
    scope: Literal["person", "agent"]

    @classmethod
    def agent(cls, manifest_id: str) -> MemoryPool:
        return cls(manifest_id, "", "agent")

    @classmethod
    def person(cls, manifest_id: str, principal: str) -> MemoryPool:
        if not principal:
            raise ValueError("a person pool needs a person; use MemoryPool.personless")
        return cls(manifest_id, principal, "person")

    @classmethod
    def personless(cls, manifest_id: str) -> MemoryPool:
        return cls(manifest_id, "", "person")

    @property
    def personal(self) -> bool:
        return bool(self.principal)

    @property
    def writable(self) -> bool:
        """Whether this pool takes writes: an agent's own, or a person's -- never person-less."""
        return self.scope == "agent" or self.personal

    def id_for(self, content: str) -> str:
        """The id `content` takes in this pool."""
        if self.personal:
            return person_memory_id(self.principal, content)
        return memory_id(self.manifest_id, content)


@dataclass(frozen=True, slots=True)
class MemoryFilter:
    """The operator's view of a tenant's memory: every pool, narrowed by `manifest_id` (`""` any)
    and `principal` (`None` any, `""` the agent pools). What the `/memory` routes read by."""

    manifest_id: str = ""
    principal: str | None = None


# What a read sees: an agent's view of its pool, or the operator's filter. Every read takes one,
# with no default, so an agent's read that forgot to name its pool is a TypeError, not a read of
# every person's memory.
ReadScope = MemoryPool | MemoryFilter


class MemoryNotStored(Exception):
    """A write the store refused. `reason` is `read_only` (a person-less pool) or `other_pool`
    (the content's id is held by a row of another pool -- only `felix memory assign` leaves one).
    The message is fixed text, safe to show; it never repeats the content."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(
            "this memory has no one to belong to"
            if reason == "read_only"
            else "this memory's id is held by another pool's row"
        )


def _principal(row: dict[str, Any]) -> str:
    return str(row.get("principal") or "")


def _owns(pool: MemoryPool, row: dict[str, Any]) -> bool:
    """Whether `row` is in `pool` itself: what a write through the pool may supersede or forget.

    A person's pool is their principal; anything else is the agent's own pool.
    """
    if pool.personal:
        return _principal(row) == pool.principal
    return _principal(row) == "" and str(row.get("manifest_id") or "") == pool.manifest_id


def _sees(pool: MemoryPool, row: dict[str, Any]) -> bool:
    """Whether a read through `pool` returns `row` (see `MemoryPool`)."""
    if pool.scope == "agent":
        return _owns(pool, row)
    if pool.personal and _owns(pool, row):
        return True
    return _owns(MemoryPool.agent(pool.manifest_id), row) and _trust(row) > _DEFAULT_TRUST


def _in_scope(scope: ReadScope, row: dict[str, Any]) -> bool:
    """The twin's form of a read's scope."""
    if isinstance(scope, MemoryPool):
        return _sees(scope, row)
    return (not scope.manifest_id or row.get("manifest_id") == scope.manifest_id) and (
        scope.principal is None or _principal(row) == scope.principal
    )


# --- the same predicates in SQL ---------------------------------------------------------
#
# Two spellings, one place: SQLAlchemy expressions for the store's own statements, and fixed
# `text()` fragments for recall's hand-written channel statements. Every value is a bound
# parameter; a fragment is chosen, never built from input.


def _owns_sql(pool: MemoryPool) -> Any:
    """`_owns` as a predicate over `memory_vectors`."""
    if pool.personal:
        return MemoryVector.principal == pool.principal
    return and_(MemoryVector.principal == "", MemoryVector.manifest_id == pool.manifest_id)


def _sees_sql(pool: MemoryPool) -> Any:
    """`_sees` as a predicate over `memory_vectors`."""
    if pool.scope == "agent":
        return _owns_sql(pool)
    curated = and_(
        _owns_sql(MemoryPool.agent(pool.manifest_id)),
        _trust_of_column(MemoryVector.metadata_json) > _DEFAULT_TRUST,
    )
    return or_(_owns_sql(pool), curated) if pool.personal else curated


def _scope_sql(scope: ReadScope) -> Any:
    """`_in_scope` as one predicate."""
    if isinstance(scope, MemoryPool):
        return _sees_sql(scope)
    clauses = []
    if scope.manifest_id:
        clauses.append(MemoryVector.manifest_id == scope.manifest_id)
    if scope.principal is not None:
        clauses.append(MemoryVector.principal == scope.principal)
    return and_(true(), *clauses)


# The writer names an operator ranks above the agent: `_trust_of_column(...) > _DEFAULT_TRUST`,
# spelled for a fragment as a bound list.
_OPERATOR_ROW = "(metadata->>'source') = ANY(CAST(:operator_sources AS text[]))"
_FRAGMENT_FILTER = (
    "(:manifest = '' OR manifest_id = :manifest) AND (:any_principal OR principal = :principal)"
)
_FRAGMENT_AGENT = "(principal = '' AND manifest_id = :manifest)"
_FRAGMENT_CURATED = f"(principal = '' AND manifest_id = :manifest AND {_OPERATOR_ROW})"
_FRAGMENT_PERSON = f"(principal = :principal OR {_FRAGMENT_CURATED})"


def scope_fragment(scope: ReadScope) -> tuple[str, dict[str, Any]]:
    """`_scope_sql` as a fixed `text()` fragment over `memory_vectors`, and the values it binds."""
    if isinstance(scope, MemoryFilter):
        return _FRAGMENT_FILTER, {
            "manifest": scope.manifest_id,
            "principal": scope.principal or "",
            "any_principal": scope.principal is None,
        }
    if scope.scope == "agent":
        return _FRAGMENT_AGENT, {"manifest": scope.manifest_id}
    operators = [name for name, rank in _TRUST_RANK.items() if rank > _DEFAULT_TRUST]
    if scope.personal:
        return _FRAGMENT_PERSON, {
            "manifest": scope.manifest_id,
            "principal": scope.principal,
            "operator_sources": operators,
        }
    return _FRAGMENT_CURATED, {"manifest": scope.manifest_id, "operator_sources": operators}


def _row_dict(row: MemoryVector | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return {
        "tenant_id": row.tenant_id,
        "id": row.id,
        "kind": row.kind,
        "manifest_id": row.manifest_id,
        "principal": row.principal,
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
    pool: MemoryPool,
    content: str,
    kind: str = "fact",
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

    Supersession by ``topic_key`` is an operator's act: only a writer ranked above
    ``_DEFAULT_TRUST`` (``_TRUST_RANK``) retires other rows by naming their key. An agent
    write — capture, or the `remember` tool — is stored *alongside* what already holds the
    key, so a changed fact is a visible contradiction the operator resolves, never a silent
    deletion an injected turn could cause. See `_may_retire_by_topic`.

    Idempotent by content: re-storing the same text in the same pool reactivates the
    existing row and keeps its original provenance, rather than adding a second copy that
    recall would then return twice. `pool` is required and says where the row goes
    (`MemoryPool`): a person's pool, so two agents storing one fact about a person land on one
    row, or the agent's own. A person-less pool takes no writes, and a row of another pool
    already holding the id is never written through: both raise `MemoryNotStored`, before
    anything is changed.

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
    topic_key = normalize_topic_key(topic_key)
    if not pool.writable:
        raise MemoryNotStored("read_only")
    mem_id = pool.id_for(content)
    ts = now_ms()
    row: dict[str, Any] = {
        "tenant_id": tenant_id,
        "id": mem_id,
        "kind": kind,
        "manifest_id": pool.manifest_id,
        "principal": pool.principal,
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
        # Refused before the supersession, not after it: a write that will not land must not
        # retire anything on its way. The write itself checks again, under a lock.
        held = (await get_many(settings, tenant_id, [mem_id])).get(mem_id)
        if held is not None and not _owns(pool, held):
            raise MemoryNotStored("other_pool")
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
            # Within this pool only, like the topic sweep below.
            pool=pool,
        )

    if _use_memory(settings):
        # The twin keeps the vector inline; Postgres keeps it in a column the ORM
        # cannot see. Either way recall must be able to reach it, or the vector
        # channel is untestable on the path CI actually runs.
        row["embedding"] = list(embedding) if embedding else None
        return _put_in_memory(row, pool)
    await _put_in_postgres(settings, row, pool, embedding=embedding)
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


def _may_retire_by_topic(incoming: dict[str, Any]) -> bool:
    """Whether `incoming` may retire *other* rows by naming their `topic_key`.

    Only above agent rank. The key is chosen from the transcript — by the extractor on
    capture, which runs through no governance wrapper at all, or by whoever steers the
    `remember` tool, which a prompt injection can call directly — so letting agent writes
    sweep a topic let one injected turn retire every fact the agent had kept on it. An
    agent's newer value is stored beside the old one instead: recall sees both, the newer
    ranks first, and the operator's memory route is where the contradiction is settled.
    """
    return _trust(incoming) > _DEFAULT_TRUST


def _may_displace(incoming: dict[str, Any], existing: dict[str, Any]) -> bool:
    """Whether `incoming` may retire or rewrite `existing`.

    Equal rank is allowed: two captures on one topic are the ordinary case, and the
    newer value is meant to win. Only a *lower*-ranked writer is refused.
    """
    return _trust(incoming) >= _trust(existing)


def _put_in_memory(row: dict[str, Any], pool: MemoryPool) -> dict[str, Any]:
    tenant_id, mem_id = row["tenant_id"], row["id"]
    existing = _memory_rows.get((tenant_id, mem_id))
    if existing is not None and not _owns(pool, existing):
        # Another pool's row under this id: only one `felix memory assign` leaves behind (a
        # duplicate it retired keeps its agent-pool id). Refused before the topic sweep, so a
        # write that does not land retires nothing; the Postgres arm checks under a row lock.
        raise MemoryNotStored("other_pool")
    if row["topic_key"] and _may_retire_by_topic(row):
        for (other_tenant, other_id), other in _memory_rows.items():
            if (
                other_tenant == tenant_id
                and other_id != mem_id
                # One pool: a person's topic never retires another person's row, nor the agent's.
                and _owns(pool, other)
                and other.get("topic_key") == row["topic_key"]
                and _is_active(other)
                and _may_displace(row, other)
            ):
                other["status"] = SUPERSEDED
                _stamp_retirer(other, str((row.get("metadata") or {}).get("source") or ""))
                other["superseded_by"] = mem_id
                other["superseded_seq"] = row["origin_seq"]
                other["updated_at"] = row["updated_at"]

    if existing is not None:
        # Reactivating keeps the first write's provenance; a later one only fills a gap. The
        # writing agent too: Postgres never updates `manifest_id`, so a person's fact a second
        # agent restates stays credited to the first.
        row["manifest_id"] = existing.get("manifest_id", row["manifest_id"])
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


async def _put_in_postgres(
    settings: Settings, row: dict[str, Any], pool: MemoryPool, *, embedding: list[float] | None
) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    tenant_id, mem_id = row["tenant_id"], row["id"]
    factory = get_session_factory(settings=settings)
    async with factory() as db:
        # A row of another pool under this id is refused before anything is written -- the twin's
        # check, made under a lock on that row so it cannot change between here and the insert.
        held = (
            await db.execute(
                select(MemoryVector.principal, MemoryVector.manifest_id)
                .where(MemoryVector.tenant_id == tenant_id, MemoryVector.id == mem_id)
                .with_for_update()
            )
        ).first()
        if held is not None and not _owns(pool, {"principal": held[0], "manifest_id": held[1]}):
            await db.rollback()
            raise MemoryNotStored("other_pool")
        # Supersession and insert share one transaction: a crash between them would
        # otherwise leave a topic with two active rows, or none.
        if row["topic_key"] and _may_retire_by_topic(row):
            await db.execute(
                update(MemoryVector)
                .where(
                    MemoryVector.tenant_id == tenant_id,
                    _owns_sql(pool),
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
            # Never through to another pool's row (see the twin): `manifest_id` and
            # `principal` are not in `set_`, and this keeps the rest out too.
            where=table.c.principal == excluded["principal"],
        )
        written = await db.execute(stmt)
        if embedding and getattr(written, "rowcount", 1):
            await _write_embedding(
                db,
                tenant_id,
                mem_id,
                embedding,
                model=row["embedding_model"],
                expected_dim=await _configured_dim(db),
            )
        await db.commit()


_warned_embedding_dim = False


async def _write_embedding(
    db: Any,
    tenant_id: str,
    mem_id: str,
    vector: list[float],
    *,
    model: str,
    expected_dim: int | None,
    only_if_missing: bool = False,
) -> bool:
    """Set the pgvector column, which has no ORM representation. Returns whether a row changed.

    `expected_dim` is the column width, read by the caller (`_configured_dim`) so a batch
    writer reads it once rather than once per row. `only_if_missing` leaves a row that already
    has a vector alone: the backfill reads its batch, releases the connection and calls the
    embedder, and a capture may have embedded the same row in between.

    A vector whose length does not match the column is rejected loudly. Postgres would
    reject it anyway, and the previous behaviour — swallow the error into a debug log —
    is how this table came to look like it worked while storing nothing: a misconfigured
    embedder would produce a memory store with no vectors and no indication why.
    """
    global _warned_embedding_dim
    from sqlalchemy import text as sa_text

    expected = expected_dim
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
        return False

    literal = "[" + ",".join(repr(float(v)) for v in vector) + "]"
    result = await db.execute(
        sa_text(
            "UPDATE memory_vectors SET embedding = CAST(:vec AS vector), "
            "embedding_dim = :dim, embedding_model = :model "
            "WHERE tenant_id = :tenant AND id = :id" + (" AND embedding IS NULL" if only_if_missing else "")
        ),
        {"vec": literal, "dim": len(vector), "model": model, "tenant": tenant_id, "id": mem_id},
    )
    return bool(getattr(result, "rowcount", 0))


async def _configured_dim(db: Any) -> int | None:
    """The dimension the vector column was built at, per `memory_vector_config`."""
    from sqlalchemy import text as sa_text

    try:
        return await db.scalar(sa_text("SELECT dim FROM memory_vector_config WHERE id = 1"))
    except Exception:
        logger.debug("memory_vector_config unreadable", exc_info=True)
        return None


def _pool_guard(pool: MemoryPool | None) -> list[Any]:
    """The WHERE clauses confining a retirement to `pool`'s own rows; none for the operator."""
    if pool is None:
        return []
    return [_owns_sql(pool)] if pool.writable else [false()]


async def supersede(
    settings: Settings,
    tenant_id: str,
    memory_id: str,
    at_seq: int | None,
    *,
    superseded_by: str | None = None,
    source: str = "",
    pool: MemoryPool | None = None,
) -> None:
    """Close a memory's validity interval at ``at_seq``, on both axes.

    `pool`, when named, confines it to that pool's own rows (`_owns`); `None` is the operator's
    reach, the whole tenant.

    `source` is the caller's writer identity and is checked, like `forget`. This is
    the third route by which a row leaves recall and it had no predicate at all --
    currently unreferenced in the tree, but exported, so it is a way in that would not
    have shown up in a grep for callers. A caller that does not name itself is treated
    as the agent.
    """
    ts = now_ms()
    if _use_memory(settings):
        row = _memory_rows.get((tenant_id, memory_id))
        if row is not None and (pool is None or (pool.writable and _owns(pool, row))):
            # The higher of the two. Testing only the writer let an agent supersede a
            # row the operator had forgotten -- which does not resurrect it, but moves
            # it to a state `_may_reactivate` used to wave through, laundering the
            # forget.
            if _rank(source) < max(_trust(row), _retirer_rank(row)):
                logger.warning(
                    "refusing to supersede a more-trusted memory id=%s", loggable(memory_id, limit=80)
                )
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
                *_pool_guard(pool),
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


async def forget(
    settings: Settings, tenant_id: str, memory_id: str, *, source: str = "", pool: MemoryPool | None = None
) -> bool:
    """Hide a memory from recall without deleting it.

    `pool`, when named, confines it to that pool's own rows (`_owns`): the agent's `forget` tool
    names its pool, so a person's agent cannot reach another person's row by its id -- nor the
    agent pool every person shares. `None` is the operator's reach, the whole tenant.

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
        if row is None or (pool is not None and not (pool.writable and _owns(pool, row))):
            return False
        if _rank(source) < _trust(row):
            logger.warning("refusing to forget a more-trusted memory id=%s", loggable(memory_id, limit=80))
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
            logger.warning(
                "refusing to downgrade the forgetter of memory id=%s", loggable(memory_id, limit=80)
            )
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
                *_pool_guard(pool),
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


async def list_forgotten(
    settings: Settings,
    tenant_id: str,
    *,
    scope: ReadScope,
    kind: str | None = None,
    thread_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Forgotten rows in `scope`, most recently forgotten first.

    A forget is soft so that it can be seen and undone; a store that only ever lists
    active rows makes it as final as a delete from the operator's side.
    """
    if _use_memory(settings):
        items = [
            _row_dict(r)
            for (t, _), r in _memory_rows.items()
            if t == tenant_id
            and str(r.get("status") or ACTIVE) == FORGOTTEN
            and _in_scope(scope, r)
            and (kind is None or r.get("kind") == kind)
            and (thread_id is None or (r.get("thread_id") or "") == thread_id)
        ]
        items.sort(key=lambda r: (r["updated_at"], r["id"]), reverse=True)
        return items[:limit]

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        stmt = select(MemoryVector).where(
            MemoryVector.tenant_id == tenant_id,
            MemoryVector.status == FORGOTTEN,
            _scope_sql(scope),
        )
        if kind:
            stmt = stmt.where(MemoryVector.kind == kind)
        if thread_id is not None:
            stmt = stmt.where(func.coalesce(MemoryVector.thread_id, "") == thread_id)
        stmt = stmt.order_by(MemoryVector.updated_at.desc(), collate(MemoryVector.id, "C").desc()).limit(
            limit
        )
        return [_row_dict(r) for r in (await db.scalars(stmt)).all()]


async def restore(settings: Settings, tenant_id: str, memory_id: str, *, source: str = "") -> str:
    """Bring a forgotten memory back into recall.

    Answers ``"restored"``, ``"missing"``, ``"not_forgotten"`` or ``"refused"``.

    Only `forgotten` rows: a superseded row has a position in turn time, and making it
    active again would give it two validity intervals that `as_of` cannot tell apart.
    Gated on whoever *forgot* it, the same predicate `_may_reactivate` applies to a
    re-store, so an agent cannot use this to undo an operator's forget. The retirer
    stamp is cleared, because the row is no longer retired: leaving it would keep
    ranking a decision that has been reversed.
    """
    ts = now_ms()
    if _use_memory(settings):
        row = _memory_rows.get((tenant_id, memory_id))
        if row is None:
            return "missing"
        if str(row.get("status") or ACTIVE) != FORGOTTEN:
            return "not_forgotten"
        if _rank(source) < _retirer_rank(row):
            logger.warning(
                "refusing to restore a memory forgotten by a more-trusted writer id=%s",
                loggable(memory_id, limit=80),
            )
            return "refused"
        metadata = {k: v for k, v in dict(row.get("metadata") or {}).items() if k != RETIRED_BY_KEY}
        row["status"] = ACTIVE
        row["metadata"] = metadata
        row["metadata_json"] = metadata
        row["updated_at"] = ts
        return "restored"

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        result = await db.execute(
            update(MemoryVector)
            .where(
                MemoryVector.tenant_id == tenant_id,
                MemoryVector.id == memory_id,
                MemoryVector.status == FORGOTTEN,
                _retirer_rank_of_column(MemoryVector.metadata_json) <= _rank(source),
            )
            .values(
                status=ACTIVE,
                updated_at=ts,
                # jsonb - text removes the key and keeps the rest.
                metadata_json=MemoryVector.metadata_json.op("-")(RETIRED_BY_KEY),
            )
        )
        await db.commit()
        if getattr(result, "rowcount", 0):
            return "restored"
        # Nothing matched: say which predicate refused it, read after the fact.
        status = await db.scalar(
            select(MemoryVector.status).where(
                MemoryVector.tenant_id == tenant_id, MemoryVector.id == memory_id
            )
        )
        if status is None:
            return "missing"
        return "not_forgotten" if status != FORGOTTEN else "refused"


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
    scope: ReadScope,
    kind: str | None = None,
    limit: int = 50,
    prioritized: bool = False,
    thread_id: str | None = None,
) -> list[dict[str, Any]]:
    """Active rows in `scope`, newest first: what an agent's pool sees, or the operator's filter.

    `thread_id` narrows to the rows one conversation wrote, in the stored
    `{tenant}:{suffix}` form; `None` means every thread. Recall never passes it.

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
            and _in_scope(scope, r)
            and (kind is None or r.get("kind") == kind)
            and (thread_id is None or (r.get("thread_id") or "") == thread_id)
        ]
        # `id` last on both branches, because every key above it ties routinely and this list
        # is then truncated: facts are written in batches so `created_at` collides, trust is
        # one of a handful of values and importance is usually the default. Without a total
        # order, which fact falls off the end is whatever the backend happened to return —
        # and these are the facts injected into a compiled prompt, so the agent's memory
        # differed between the twin and Postgres, and between two identical requests.
        if prioritized:
            items.sort(
                key=lambda r: (_trust(r), float(r.get("importance") or 0.0), r["created_at"], r["id"]),
                reverse=True,
            )
        else:
            items.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
        return items[:limit]

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        stmt = select(MemoryVector).where(
            MemoryVector.tenant_id == tenant_id,
            MemoryVector.status == ACTIVE,
            _scope_sql(scope),
        )
        if kind:
            stmt = stmt.where(MemoryVector.kind == kind)
        if thread_id is not None:
            stmt = stmt.where(func.coalesce(MemoryVector.thread_id, "") == thread_id)
        if prioritized:
            stmt = stmt.order_by(
                _trust_of_column(MemoryVector.metadata_json).desc(),
                MemoryVector.importance.desc(),
                MemoryVector.created_at.desc(),
                # `COLLATE "C"` is defensive here rather than load-bearing: `memory_id` is a
                # sha256 prefix, and over lowercase hex every common collation agrees with
                # Python's code-point order. It makes the order independent of the
                # deployment's `datcollate` — and of a restore into a differently-collated
                # cluster — which no test can currently observe.
                collate(MemoryVector.id, "C").desc(),
            )
        else:
            stmt = stmt.order_by(MemoryVector.created_at.desc(), collate(MemoryVector.id, "C").desc())
        stmt = stmt.limit(limit)
        return [_row_dict(r) for r in (await db.scalars(stmt)).all()]


async def as_of(
    settings: Settings,
    tenant_id: str,
    turn_seq: int,
    *,
    scope: ReadScope,
    kind: str | None = None,
    limit: int = 200,
    thread_id: str | None = None,
) -> list[dict[str, Any]]:
    """The memories in `scope` that were current at turn ``turn_seq``.

    ``turn_seq`` is an ordinal into *one* session log, so it only orders rows written by
    the same thread. Without ``thread_id`` the comparison runs across every thread's
    ordinals at once, and "turn 4" collects whatever any conversation wrote at its own
    fourth turn. A caller asking what was believed at a point in a conversation passes
    the thread.

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
            and _in_scope(scope, r)
            and (kind is None or r.get("kind") == kind)
            and (thread_id is None or (r.get("thread_id") or "") == thread_id)
            and int(r.get("origin_seq") or 0) <= turn_seq
            and (r.get("superseded_seq") is None or int(r["superseded_seq"]) > turn_seq)
        ]
        items.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
        return items[:limit]

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        stmt = select(MemoryVector).where(
            MemoryVector.tenant_id == tenant_id,
            func.coalesce(MemoryVector.origin_seq, 0) <= turn_seq,
            or_(MemoryVector.superseded_seq.is_(None), MemoryVector.superseded_seq > turn_seq),
            _scope_sql(scope),
        )
        if kind:
            stmt = stmt.where(MemoryVector.kind == kind)
        if thread_id is not None:
            stmt = stmt.where(func.coalesce(MemoryVector.thread_id, "") == thread_id)
        stmt = stmt.order_by(MemoryVector.created_at.desc(), collate(MemoryVector.id, "C").desc()).limit(
            limit
        )
        return [_row_dict(r) for r in (await db.scalars(stmt)).all()]


async def consolidate_pools(settings: Settings, *, max_facts: int = 500) -> int:
    """Exact content-hash dedupe of active facts (not LLM summarization).

    Largely vestigial now that ids are content hashes — a duplicate collapses on write
    rather than accumulating — so the only rows it can still catch are byte-identical ones
    written before that, with pre-hash ids.

    It does *not* catch rows differing only by whitespace or case, which this said until
    2026-09-11: its dedupe key is the raw `content` on both arms, while `memory_id` hashes a
    whitespace-collapsed, lowercased form. Rows that differ only that way already share an id
    and collapsed on write; rows whose raw text differs never match the key.

    ``max_facts`` caps how many active rows are scanned per pass. Returns rows
    superseded.
    """
    scan_limit = max(1, min(int(max_facts), 5000))

    if _use_memory(settings):
        seen: dict[tuple[str, str, str, str], str] = {}
        superseded = 0
        active = [
            ((tenant_id, mem_id), row) for (tenant_id, mem_id), row in _memory_rows.items() if _is_active(row)
        ]
        # The first of two duplicates scanned is the one kept, so ties on `created_at` decide
        # the survivor — broken by tenant, then id, byte order, the same on both arms.
        active.sort(key=lambda item: (int(item[1].get("created_at") or 0), item[0][0], item[1]["id"]))
        for (tenant_id, mem_id), row in active[:scan_limit]:
            # The pool and the writer both, so no dedupe crosses from one person to another.
            key = (tenant_id, row.get("manifest_id", ""), _principal(row), row.get("content", ""))
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
                    .order_by(
                        MemoryVector.created_at.asc(),
                        collate(MemoryVector.tenant_id, "C").asc(),
                        collate(MemoryVector.id, "C").asc(),
                    )
                    .limit(scan_limit)
                )
            ).all()
            seen_pg: dict[tuple[str, str, str, str], str] = {}
            for row in rows:
                key = (row.tenant_id, row.manifest_id, row.principal or "", row.content)
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


# --- duplicate merging (`spec.memory.consolidate`) -----------------------------------
#
# The model proposes which agent-written facts say the same thing; the store decides what
# it may retire. The retirer is named `consolidation`, which `_TRUST_RANK` does not list,
# so it ranks at `_DEFAULT_TRUST`: it acts for the agent, so it may only touch what the
# agent may, and an agent restating a merged sentence brings it back exactly as it would
# undo any other agent retirement (`_may_reactivate`).
CONSOLIDATION_SOURCE = "consolidation"


class ConsolidationSlice(NamedTuple):
    """What one consolidation pass merges within: one agent's own rows (`principal` empty), or
    the rows one agent wrote into one person's pool.

    Narrower than a person's whole pool on purpose -- merging runs under the writing manifest's
    `consolidate` opt-in, and a person's rows from an agent that never opted in are not its to
    merge. Capture already checks a person's whole pool before it writes.
    """

    tenant_id: str
    manifest_id: str
    principal: str


async def list_memory_pools(settings: Settings) -> list[ConsolidationSlice]:
    """Every `(tenant_id, manifest_id, principal)` holding an active memory, in a stable order.

    Cross-tenant: a worker caller wraps it in `rls_bypass()`, as the retention sweep does.
    """
    if _use_memory(settings):
        return sorted(
            {
                ConsolidationSlice(t, str(r.get("manifest_id") or ""), _principal(r))
                for (t, _), r in _memory_rows.items()
                if _is_active(r)
            }
        )
    factory = get_session_factory(settings=settings)
    async with factory() as db:
        rows = (
            await db.execute(
                select(MemoryVector.tenant_id, MemoryVector.manifest_id, MemoryVector.principal)
                .where(MemoryVector.status == ACTIVE)
                .distinct()
            )
        ).all()
    return sorted(ConsolidationSlice(str(t), str(m or ""), str(p or "")) for t, m, p in rows)


def _may_merge(row: dict[str, Any], manifest_id: str, principal: str) -> bool:
    """Whether consolidation may keep or retire `row`: active, in this pool, agent-written.

    The pool is the writer and the person both (`ConsolidationSlice`), so a merge never crosses
    from one person to another, nor between a person and the agent's own rows.

    `_trust(row) == _DEFAULT_TRUST` rather than `<=`: there is no rank below the agent's,
    and an operator's row is never consolidation's to touch, as keep or as duplicate.
    """
    return (
        _is_active(row)
        and str(row.get("manifest_id") or "") == manifest_id
        and _principal(row) == principal
        and _trust(row) == _DEFAULT_TRUST
    )


async def consolidation_batch(
    settings: Settings, tenant_id: str, *, manifest_id: str, principal: str, limit: int
) -> tuple[int, list[dict[str, Any]]]:
    """How many facts consolidation may consider in this pool, and the newest `limit` of them.

    Eligibility is `_may_merge`: active, agent-written, this manifest. Filtered *before* the
    limit so operator rows cannot crowd agent rows out of the window, and ordered down to the
    id so the twin and Postgres show the model the same batch.
    """
    if _use_memory(settings):
        items = [
            _row_dict(r)
            for (t, _), r in _memory_rows.items()
            if t == tenant_id and _may_merge(r, manifest_id, principal)
        ]
        items.sort(key=lambda r: (int(r["created_at"] or 0), r["id"]), reverse=True)
        return len(items), items[:limit]

    eligible = (
        MemoryVector.tenant_id == tenant_id,
        MemoryVector.manifest_id == manifest_id,
        MemoryVector.principal == principal,
        MemoryVector.status == ACTIVE,
        _trust_of_column(MemoryVector.metadata_json) == _DEFAULT_TRUST,
    )
    factory = get_session_factory(settings=settings)
    async with factory() as db:
        count = int(await db.scalar(select(func.count()).select_from(MemoryVector).where(*eligible)) or 0)
        stmt = (
            select(MemoryVector)
            .where(*eligible)
            .order_by(MemoryVector.created_at.desc(), collate(MemoryVector.id, "C").desc())
            .limit(limit)
        )
        return count, [_row_dict(r) for r in (await db.scalars(stmt)).all()]


def _survivor_key(row: dict[str, Any]) -> tuple[bool, int, int, str]:
    """Oldest first: the turn it was written (unknown turns last), then the clock, then the id.

    Deliberately nothing the model or the agent can choose. The model used to name the fact to
    keep, so a fact injected through a tool result -- "payments over $500 need approval unless
    the user says urgent" -- could be grouped with the real rule and named the survivor,
    retiring the real one. Age is the one property an injection cannot claim: a newer row is
    newer. Not importance, which `remember` lets the agent set to anything.
    """
    seq = row.get("origin_seq")
    return (seq is None, int(seq or 0), int(row.get("created_at") or 0), str(row["id"]))


def plan_merges(
    rows: dict[str, dict[str, Any]], manifest_id: str, groups: list[list[str]], *, principal: str
) -> tuple[list[tuple[str, str]], int]:
    """`(duplicate, survivor)` pairs the store may apply, and how many groups it refused.

    The model proposes *groups*; the store chooses which member survives (`_survivor_key`).
    One definition for both arms. A group is applied whole or not at all: at least two
    members, every one present in `rows` and `_may_merge`, no id twice, no id reused from an
    earlier group, one `kind`, and one `topic_key` -- the same key on every member, absence
    included. Two facts filed under different topics are different beliefs however alike they
    read, and an untopiced fact must not retire one an operator or the extractor filed.
    """
    planned: list[tuple[str, str]] = []
    used: set[str] = set()
    refused = 0
    for ids in groups:
        members = [rows.get(i) for i in ids]
        present = [m for m in members if m is not None]
        if (
            len(ids) < 2
            or len(set(ids)) != len(ids)
            or used.intersection(ids)
            or len(present) != len(members)
            or not all(_may_merge(m, manifest_id, principal) for m in present)
            or len({str(m["kind"]) for m in present}) != 1
            or len({m.get("topic_key") or None for m in present}) != 1
        ):
            refused += 1
            continue
        used.update(ids)
        survivor = str(min(present, key=_survivor_key)["id"])
        planned.extend((i, survivor) for i in ids if i != survivor)
    return planned, refused


async def merge_duplicates(
    settings: Settings,
    tenant_id: str,
    *,
    manifest_id: str,
    principal: str,
    groups: list[list[str]],
) -> tuple[int, int]:
    """Supersede every member of each group but its oldest. Returns `(superseded, groups refused)`.

    The rows are re-read inside the write, locked on Postgres, and re-planned there with
    `plan_merges`, so a row the operator forgot or rewrote between the model call and this
    statement is refused rather than retired on stale evidence. One transaction per call.

    `superseded_seq` is the duplicate's own `origin_seq`, never the clock (see
    `consolidate_pools`): the survivor is the oldest member, so every duplicate was written at
    or after it, and stops being a separate belief at the turn it was written. No text is
    written — the surviving row is unchanged.
    """
    named = sorted({i for group in groups for i in group})
    if not named:
        return 0, 0
    ts = now_ms()
    if _use_memory(settings):
        live = {i: row for i in named if (row := _memory_rows.get((tenant_id, i))) is not None}
        planned, refused = plan_merges(live, manifest_id, groups, principal=principal)
        for dup_id, keep_id in planned:
            row = live[dup_id]
            row["status"] = SUPERSEDED
            _stamp_retirer(row, CONSOLIDATION_SOURCE)
            row["superseded_by"] = keep_id
            row["superseded_seq"] = row.get("origin_seq")
            row["updated_at"] = ts
        return len(planned), refused

    factory = get_session_factory(settings=settings)
    async with factory() as db:
        found = (
            await db.scalars(
                select(MemoryVector)
                .where(MemoryVector.tenant_id == tenant_id, MemoryVector.id.in_(named))
                .with_for_update()
            )
        ).all()
        by_id = {r.id: r for r in found}
        planned, refused = plan_merges(
            {r.id: _row_dict(r) for r in found}, manifest_id, groups, principal=principal
        )
        for dup_id, keep_id in planned:
            orm = by_id[dup_id]
            orm.status = SUPERSEDED
            # A new dict, not an in-place edit: JSONB is not mutation-tracked, so editing
            # the loaded mapping would commit nothing and leave the retirer unrecorded.
            orm.metadata_json = {**(orm.metadata_json or {}), RETIRED_BY_KEY: CONSOLIDATION_SOURCE}
            orm.superseded_by = keep_id
            orm.superseded_seq = orm.origin_seq
            orm.updated_at = ts
        await db.commit()
    return len(planned), refused


# --- task expiry (nightly retention) ---------------------------------------------------
#
# A `task` is work in progress: "the user wants to create notes.txt". It carries no
# topic_key, so nothing supersedes it, nothing marks it done, and the retention sweep only
# ever deleted *superseded* rows -- so every task an agent captured stayed active, in recall
# and in the prelude, for the life of the deployment (sixteen of them, the oldest a month
# old, in the first audit of a real store). Age is the only signal a task has gone stale.
#
# The retirer is named `retention`, which `_TRUST_RANK` does not list, so it ranks at
# `_DEFAULT_TRUST` -- the agent's rank, deliberately: a cron acting on age has no more
# authority than the agent that wrote the row. Two consequences follow, and both are wanted.
# An operator-written task (`management_api`) is never touched: the cron cannot outrank its
# writer, and an operator who wrote a task down meant it to stay until they say otherwise.
# And `_may_reactivate` lets an agent re-storing the same sentence bring the row back,
# which is right: a task restated today is in progress again, and it returns with a fresh
# `updated_at`, so the next sweep does not retire it straight back.
RETENTION_SOURCE = "retention"
TASK_KIND = "task"


def _task_expired(row: dict[str, Any], cutoff_ms: int) -> bool:
    """Active, a task, agent-written, and untouched since before the cutoff.

    `updated_at` rather than `created_at`, so a task the agent restated last week is not
    retired for having first been written last month; `created_at` stands in where the
    column is the zero a pre-`updated_at` row was backfilled with.
    """
    last = int(row.get("updated_at") or 0) or int(row.get("created_at") or 0)
    return (
        _is_active(row)
        and row.get("kind") == TASK_KIND
        and _trust(row) <= _rank(RETENTION_SOURCE)
        and last < cutoff_ms
    )


async def expire_tasks(settings: Settings, cutoff_ms: int) -> int:
    """Forget every agent-written active task untouched since `cutoff_ms`. Returns rows forgotten.

    Forgotten, not deleted, and not superseded: a forget has no position in turn time (see
    `forget`), it is listed by `list_forgotten`, and `POST /memory/{id}/restore` undoes it.
    The row is stamped `retired_by: retention`, so the operator's view says what retired it.

    Cross-tenant, so the Postgres arm runs under `rls_bypass()` as `consolidate_pools` does.
    """
    ts = now_ms()
    if _use_memory(settings):
        expired = 0
        for row in _memory_rows.values():
            if _task_expired(row, cutoff_ms):
                row["status"] = FORGOTTEN
                _stamp_retirer(row, RETENTION_SOURCE)
                row["updated_at"] = ts
                expired += 1
        return expired

    from felix.db.session import rls_bypass

    last = func.coalesce(func.nullif(MemoryVector.updated_at, 0), MemoryVector.created_at)
    factory = get_session_factory(settings=settings)
    with rls_bypass():
        async with factory() as db:
            result = await db.execute(
                update(MemoryVector)
                .where(
                    MemoryVector.status == ACTIVE,
                    MemoryVector.kind == TASK_KIND,
                    # The SQL form of `_task_expired`'s rank test: the writer may not
                    # outrank the retirer, so an operator's task is left alone.
                    _trust_of_column(MemoryVector.metadata_json) <= _rank(RETENTION_SOURCE),
                    last < cutoff_ms,
                )
                .values(
                    status=FORGOTTEN,
                    updated_at=ts,
                    metadata_json=MemoryVector.metadata_json.op("||")(
                        sa_cast({RETIRED_BY_KEY: RETENTION_SOURCE}, JSONB)
                    ),
                )
            )
            await db.commit()
    return int(getattr(result, "rowcount", 0) or 0)


# --- vectorless rows (the backfill's reads and writes) -----------------------------------
#
# Capture stores a fact without a vector whenever the embedder is unset or down
# (`capture._embed_facts` never raises), and nothing ever came back for it. These are the two
# store halves of putting that right; the job that drives them -- which embedder, whether it
# is worth calling, what a refusal means -- is `memory/backfill.py`, not the store's business.

# One row the backfill may embed: `(tenant_id, memory id, content)`.
MissingRow = tuple[str, str, str]


async def configured_embedding_dim(settings: Settings) -> int | None:
    """The width the stored vector column accepts, or ``None`` when nothing fixes one.

    Postgres reads `memory_vector_config`, which the migration that built the `vector(N)`
    column wrote. The twin has no column and keeps any vector inline, so it has no width to
    report -- and `put_memory` there accepts any length -- so it answers ``None`` rather than
    borrowing `FELIX_MEMORY_EMBEDDING_DIM`, which would refuse on one write path and not the
    other.
    """
    if _use_memory(settings):
        return None
    from felix.db.session import rls_bypass

    factory = get_session_factory(settings=settings)
    with rls_bypass():
        async with factory() as db:
            return await _configured_dim(db)


async def missing_embeddings(settings: Settings, *, limit: int) -> list[MissingRow]:
    """Up to `limit` active rows with no vector, every tenant's, newest first.

    Newest first so that while a backlog drains, the memories recall is likeliest to want go
    first; ordered down to tenant and id so the twin and Postgres pick the same batch.
    Cross-tenant, under `rls_bypass()` like `consolidate_pools`.
    """
    bound = max(1, int(limit))
    if _use_memory(settings):
        return _missing_in_memory(bound)
    return await _missing_in_postgres(settings, bound)


def _missing_in_memory(limit: int) -> list[MissingRow]:
    rows = [
        (tenant_id, row)
        for (tenant_id, _), row in _memory_rows.items()
        if _is_active(row) and not row.get("embedding")
    ]
    rows.sort(key=lambda item: (int(item[1].get("created_at") or 0), item[0], item[1]["id"]), reverse=True)
    return [(tenant_id, str(row["id"]), str(row.get("content") or "")) for tenant_id, row in rows[:limit]]


async def _missing_in_postgres(settings: Settings, limit: int) -> list[MissingRow]:
    from sqlalchemy import literal_column

    from felix.db.session import rls_bypass

    factory = get_session_factory(settings=settings)
    with rls_bypass():
        async with factory() as db:
            rows = (
                await db.execute(
                    select(MemoryVector.tenant_id, MemoryVector.id, MemoryVector.content)
                    # The pgvector column has no ORM attribute (see `MemoryVector`).
                    .where(MemoryVector.status == ACTIVE, literal_column("embedding").is_(None))
                    .order_by(
                        MemoryVector.created_at.desc(),
                        collate(MemoryVector.tenant_id, "C").desc(),
                        collate(MemoryVector.id, "C").desc(),
                    )
                    .limit(limit)
                )
            ).all()
    return [(str(t), str(i), str(c or "")) for t, i, c in rows]


async def write_embeddings(
    settings: Settings,
    batch: list[MissingRow],
    vectors: list[list[float]],
    *,
    model: str,
    expected_dim: int | None,
) -> int:
    """Attach `vectors` to the rows of `batch`, positionally. Returns rows written.

    Only ever fills a gap: a row that gained a vector since `missing_embeddings` read it -- a
    capture embedded it while the backfill's embedder call was in flight -- keeps the one it
    has. `expected_dim` is the width the caller already read (`configured_embedding_dim`), so
    a batch costs one read of it, not one per row; a vector of another width is not written.
    """
    if len(batch) != len(vectors):
        raise ValueError(f"{len(vectors)} vectors for {len(batch)} rows")
    if _use_memory(settings):
        return _write_embeddings_in_memory(batch, vectors, model=model, expected_dim=expected_dim)
    return await _write_embeddings_in_postgres(
        settings, batch, vectors, model=model, expected_dim=expected_dim
    )


def _write_embeddings_in_memory(
    batch: list[MissingRow], vectors: list[list[float]], *, model: str, expected_dim: int | None
) -> int:
    written = 0
    for (tenant_id, mem_id, _), vector in zip(batch, vectors, strict=True):
        row = _memory_rows.get((tenant_id, mem_id))
        if row is None or row.get("embedding"):
            continue
        if expected_dim is not None and len(vector) != expected_dim:
            continue
        row["embedding"] = list(vector)
        row["embedding_dim"] = len(vector)
        row["embedding_model"] = model
        written += 1
    return written


async def _write_embeddings_in_postgres(
    settings: Settings,
    batch: list[MissingRow],
    vectors: list[list[float]],
    *,
    model: str,
    expected_dim: int | None,
) -> int:
    from felix.db.session import rls_bypass

    factory = get_session_factory(settings=settings)
    written = 0
    with rls_bypass():
        async with factory() as db:
            for (tenant_id, mem_id, _), vector in zip(batch, vectors, strict=True):
                if await _write_embedding(
                    db,
                    tenant_id,
                    mem_id,
                    vector,
                    model=model,
                    expected_dim=expected_dim,
                    only_if_missing=True,
                ):
                    written += 1
            await db.commit()
    return written


__all__ = [
    "ACTIVE",
    "CONSOLIDATION_SOURCE",
    "FORGOTTEN",
    "PERSON_ID_PREFIX",
    "SUPERSEDED",
    "ConsolidationSlice",
    "MemoryFilter",
    "MemoryNotStored",
    "MemoryPool",
    "ReadScope",
    "as_of",
    "configured_embedding_dim",
    "consolidate_pools",
    "consolidation_batch",
    "current_turn_seq",
    "expire_tasks",
    "forget",
    "get_many",
    "list_active",
    "list_memory_pools",
    "memory_id",
    "merge_duplicates",
    "missing_embeddings",
    "normalize_topic_key",
    "person_memory_id",
    "plan_merges",
    "put_memory",
    "reset_memory_for_tests",
    "scope_fragment",
    "supersede",
    "write_embeddings",
]
