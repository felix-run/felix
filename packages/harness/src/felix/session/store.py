"""SessionStore — Postgres-backed + InMemory for tests."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from felix.config import Settings
from felix.session.types import (
    AppendableEvent,
    GetEventsOpts,
    Session,
    SessionEvent,
    SessionEventKind,
    SessionStore,
    WakeState,
    analyze_wake,
)

logger = logging.getLogger("felix.session.store")


@dataclass
class _MemorySession:
    id: str
    # Carried for the same reason `_PostgresSession` carries it: the notification channel
    # is keyed by tenant, and a twin that cannot name its tenant announces on someone
    # else's channel. Not used for storage isolation -- `InMemorySessionStore` is
    # per-tenant, so a session only ever reaches the store that made it.
    tenant_id: str = "default"
    _events: list[SessionEvent] = field(default_factory=list)

    async def append(self, event: AppendableEvent) -> int | None:
        seqs = await self.append_batch([event])
        return seqs[0] if seqs else None

    async def append_batch(self, events: list[AppendableEvent]) -> list[int]:
        now = time.time()
        from felix.secrets import redact_json, redact_text

        allocated: list[int] = []
        for ev in events:
            content = ev.content
            if isinstance(content, str):
                content = redact_text(content)
            allocated.append(len(self._events))
            stored = SessionEvent(
                seq=len(self._events),
                ts=ev.ts if ev.ts is not None else now,
                kind=ev.kind,
                role=ev.role,
                content=content,
                tool_call_id=ev.tool_call_id,
                name=ev.name,
                tool_calls=redact_json(ev.tool_calls) if ev.tool_calls else ev.tool_calls,
                metadata=redact_json(ev.metadata) if ev.metadata else ev.metadata,
            )
            self._events.append(stored)
            # Feed the search index the way Postgres does. `session_events.content_tsv` is a
            # generated column, so on the system of record every append is searchable with no
            # call site at all -- while `index_event_memory` had no production caller, so the
            # twin's index was written by exactly one unit test and `GET /chat/sessions/search`
            # returned nothing for anything the product had actually stored. The twin has to
            # behave like what it stands in for, not merely exist.
            _index_for_search(
                tenant_id=self.tenant_id,
                thread_id=self.id,
                seq=allocated[-1],
                content=content if isinstance(content, str) else None,
                # The stored metadata, not `ev.metadata`: the line above deliberately indexes
                # the redacted content, and the index should be fed from what the row holds
                # in both fields rather than only in the one that obviously matters.
                event_id=(stored.metadata or {}).get("event_id"),
            )
        await _announce(self.id, tenant_id=self.tenant_id)
        return allocated

    async def get_events(self, opts: GetEventsOpts | None = None) -> list[SessionEvent]:
        opts = opts or GetEventsOpts()
        items = list(self._events)
        if opts.from_seq is not None:
            items = [e for e in items if e.seq >= opts.from_seq]
        if opts.to_seq is not None:
            items = [e for e in items if e.seq < opts.to_seq]
        if opts.kinds is not None:
            kinds = set(opts.kinds)
            items = [e for e in items if e.kind in kinds]
        if opts.limit is not None:
            items = items[: opts.limit]
        return items

    async def head(self) -> dict[str, int]:
        return {"seq": len(self._events)}

    async def reset(self) -> None:
        self._events.clear()
        # The search index is a second copy of this thread's content, so it goes too. On
        # Postgres `DELETE FROM session_events` takes the generated `content_tsv` with it;
        # without this the twin would answer a search with text the caller had just deleted,
        # at `seq` numbers the next append immediately re-uses.
        _drop_search_index(tenant_id=self.tenant_id, thread_id=self.id)

    async def wake(self) -> WakeState:
        return analyze_wake(self._events)


class InMemorySessionStore:
    """Process-local session store for unit tests.

    Takes a tenant the way `PostgresSessionStore` does. It does not need one to keep
    events apart -- `get_session_store` hands out one store per tenant, so two tenants
    using the same thread id already get different stores -- but a session that cannot
    name its tenant cannot announce on the right channel, and the twin then behaves
    differently from the Postgres store it stands in for.
    """

    def __init__(self, *, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        self._sessions: dict[str, _MemorySession] = {}

    def open(self, thread_id: str) -> Session:
        if not thread_id:
            return _MemorySession(id="", tenant_id=self.tenant_id)
        if thread_id not in self._sessions:
            self._sessions[thread_id] = _MemorySession(id=thread_id, tenant_id=self.tenant_id)
        return self._sessions[thread_id]


def _drop_search_index(*, tenant_id: str, thread_id: str) -> None:
    """Best effort, for the same reason as `_index_for_search`."""
    if not thread_id:
        return
    try:
        from felix.session.search import drop_thread_index

        drop_thread_index(tenant_id=tenant_id, thread_id=thread_id)
    except Exception:  # pragma: no cover — the index is a plain list; nothing here raises
        logger.debug("in-memory search index drop failed", exc_info=True)


def _index_for_search(
    *,
    tenant_id: str,
    thread_id: str,
    seq: int,
    content: str | None,
    event_id: str | None,
) -> None:
    """Best effort: an unsearchable event must never fail the append that wrote it."""
    if not thread_id or not content:
        return
    try:
        from felix.session.search import index_event_memory

        index_event_memory(
            tenant_id=tenant_id,
            thread_id=thread_id,
            seq=seq,
            content=content,
            event_id=event_id,
        )
    except Exception:  # pragma: no cover — the index is a plain list; nothing here raises
        logger.debug("in-memory search indexing failed", exc_info=True)


async def _announce(thread_id: str, *, tenant_id: str) -> None:
    """Tell any waiting reader this thread moved. Best effort by construction.

    Called from the store rather than a route, so it covers every writer -- the agent
    loop, steering, tool results, the management API -- instead of only the ones a
    particular endpoint knows about. A notification failure must never fail an append
    that already succeeded, which is why nothing here propagates.

    `tenant_id` is required rather than defaulted. It was defaulted to "default" once,
    and the in-memory twin -- which had no tenant to pass -- silently took it, so every
    `memory://` append announced on tenant "default": a real tenant's reader was never
    woken, and a "default" reader was woken by other tenants' writes. A wrong channel
    key produces no error, only silence, so the type checker has to be the thing that
    catches it.
    """
    if not thread_id:
        return
    try:
        from felix.session.notify import notify_appended

        await notify_appended(tenant_id, thread_id)
    except Exception:
        # `notify_appended` swallows its own failures, so this is the second line of
        # defence rather than the first -- but it is reachable: `_wake_local` sets
        # `asyncio.Event`s, and an event created by a loop that has since closed raises
        # from `set()`. This `except` is the only thing between that and a failed append.
        logger.debug("thread notification failed", exc_info=True)


async def _lock_thread(db: Any, tenant_id: str, thread_id: str) -> None:
    """Take a transaction-scoped advisory lock for one thread's event log.

    No-ops on backends without advisory locks (SQLite in local experiments); Postgres is
    the supported system of record and the only place concurrent appends occur.
    """
    from sqlalchemy import text as sql_text

    bind = getattr(db, "bind", None)
    dialect = getattr(getattr(bind, "dialect", None), "name", "") or ""
    if dialect and dialect != "postgresql":
        return
    try:
        await db.execute(
            sql_text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"felix:session:{tenant_id}:{thread_id}"},
        )
    except Exception:
        # Never fail an append because the lock could not be taken; the unique PK still
        # protects correctness, this only avoids the conflict.
        logger.debug("advisory lock unavailable for thread=%s", thread_id, exc_info=True)


@dataclass
class _PostgresSession:
    id: str
    tenant_id: str
    session_factory: Any  # async_sessionmaker

    async def append(self, event: AppendableEvent) -> int | None:
        seqs = await self.append_batch([event])
        return seqs[0] if seqs else None

    async def append_batch(self, events: list[AppendableEvent]) -> list[int]:
        if not self.id or not events:
            return []
        from sqlalchemy import func, select

        from felix.db.models import SessionEventRow
        from felix.secrets import redact_json, redact_text

        async with self.session_factory() as db:
            # Serialize appends for this thread. The PK is (tenant_id, thread_id, seq),
            # and computing seq as max(seq)+1 is a read-modify-write: an SSE stream,
            # /chat/steer, /chat/tool_result, and /chat/sessions/custom all append to the
            # same thread by design, so two concurrent appends computed the same head and
            # one died with an unhandled IntegrityError — a 500 with the events lost.
            # The advisory lock is transaction-scoped and released on commit.
            await _lock_thread(db, self.tenant_id, self.id)
            head = await db.scalar(
                select(func.coalesce(func.max(SessionEventRow.seq), -1)).where(
                    SessionEventRow.tenant_id == self.tenant_id,
                    SessionEventRow.thread_id == self.id,
                )
            )
            next_seq = int(head) + 1
            allocated = list(range(next_seq, next_seq + len(events)))
            now = time.time()
            for i, ev in enumerate(events):
                content = ev.content
                if isinstance(content, str):
                    content = redact_text(content)
                db.add(
                    SessionEventRow(
                        tenant_id=self.tenant_id,
                        thread_id=self.id,
                        seq=allocated[i],
                        ts=ev.ts if ev.ts is not None else now,
                        kind=ev.kind,
                        role=ev.role,
                        content=content,
                        tool_call_id=ev.tool_call_id,
                        name=ev.name,
                        tool_calls=redact_json(ev.tool_calls) if ev.tool_calls else ev.tool_calls,
                        event_metadata=redact_json(ev.metadata) if ev.metadata else ev.metadata,
                    )
                )
            await db.commit()
        # Outside the transaction. A reader woken before the commit lands would query,
        # find nothing, and wait again on a notification already spent.
        await _announce(self.id, tenant_id=self.tenant_id)
        # After the commit, so a rolled-back append reports nothing allocated.
        return allocated

    async def get_events(self, opts: GetEventsOpts | None = None) -> list[SessionEvent]:
        if not self.id:
            return []
        from sqlalchemy import select

        from felix.db.models import SessionEventRow

        opts = opts or GetEventsOpts()
        async with self.session_factory() as db:
            stmt = (
                select(SessionEventRow)
                .where(
                    SessionEventRow.tenant_id == self.tenant_id,
                    SessionEventRow.thread_id == self.id,
                )
                .order_by(SessionEventRow.seq)
            )
            if opts.from_seq is not None:
                stmt = stmt.where(SessionEventRow.seq >= opts.from_seq)
            if opts.to_seq is not None:
                stmt = stmt.where(SessionEventRow.seq < opts.to_seq)
            if opts.kinds is not None:
                stmt = stmt.where(SessionEventRow.kind.in_(list(opts.kinds)))
            if opts.limit is not None:
                stmt = stmt.limit(opts.limit)
            rows = (await db.scalars(stmt)).all()
            return [
                SessionEvent(
                    seq=r.seq,
                    ts=float(r.ts),
                    kind=r.kind,  # type: ignore[arg-type]
                    role=r.role,  # type: ignore[arg-type]
                    content=r.content,
                    tool_call_id=r.tool_call_id,
                    name=r.name,
                    tool_calls=r.tool_calls,
                    metadata=r.event_metadata,
                )
                for r in rows
            ]

    async def head(self) -> dict[str, int]:
        """The next sequence number this thread would allocate.

        `max(seq) + 1` rather than a count: identical while `seq` is dense, which it
        is because `append_batch` allocates from the max and nothing deletes an
        individual event — but O(1) instead of loading and parsing every event, which
        matters now that memory capture reads this once per turn.
        """
        if not self.id:
            return {"seq": 0}
        from sqlalchemy import func, select

        from felix.db.models import SessionEventRow

        async with self.session_factory() as db:
            head = await db.scalar(
                select(func.coalesce(func.max(SessionEventRow.seq), -1)).where(
                    SessionEventRow.tenant_id == self.tenant_id,
                    SessionEventRow.thread_id == self.id,
                )
            )
        return {"seq": int(head) + 1}

    async def reset(self) -> None:
        if not self.id:
            return
        from sqlalchemy import delete

        from felix.db.models import SessionEventRow

        async with self.session_factory() as db:
            await db.execute(
                delete(SessionEventRow).where(
                    SessionEventRow.tenant_id == self.tenant_id,
                    SessionEventRow.thread_id == self.id,
                )
            )
            await db.commit()

    async def wake(self) -> WakeState:
        return analyze_wake(await self.get_events())


class PostgresSessionStore:
    """Postgres-backed SessionStore (session_events table)."""

    def __init__(self, session_factory: Any, *, tenant_id: str) -> None:
        self._session_factory = session_factory
        self._tenant_id = tenant_id

    def open(self, thread_id: str) -> Session:
        return _PostgresSession(
            id=thread_id,
            tenant_id=self._tenant_id,
            session_factory=self._session_factory,
        )


# One store per tenant, created on first use. Process-lifetime, like the single store
# this replaced: `memory://` callers rely on reopening a thread and finding its events,
# so the state has to outlive any one `get_session_store` call.
_memory_session_stores: dict[str, InMemorySessionStore] = {}


def _use_in_memory_session(settings: Settings) -> bool:
    url = settings.database_url
    return ":memory:" in url or "sqlite" in url or url.startswith("memory://")


def get_session_store(settings: Settings, *, tenant_id: str) -> SessionStore:
    """The session store for one tenant.

    `tenant_id` has no default on purpose. A default made omitting it silent rather
    than wrong-looking, which is how a caller came to read tenant "default"'s log
    for every tenant. A missing tenant is now a TypeError where the mistake is,
    rather than a quiet cross-tenant read three layers away.

    Note the residual risk this does *not* cover: an explicit `tenant_id="default"`
    still compiles. Only a test that asserts on the resulting ordinal catches that.
    """
    if _use_in_memory_session(settings):
        store = _memory_session_stores.get(tenant_id)
        if store is None:
            store = _memory_session_stores[tenant_id] = InMemorySessionStore(tenant_id=tenant_id)
        return store
    from felix.db.session import get_session_factory

    return PostgresSessionStore(
        get_session_factory(settings=settings),
        tenant_id=tenant_id,
    )


# --- checkpointers -----------------------------------------------------------
#
# `spec.memory.checkpointer` names where a manifest's session state is kept. The
# session event log *is* Felix's checkpoint: `postgres` persists it and `none` keeps
# nothing. See below for why there is no in-process built-in.
#
# The field shipped as a closed `Literal["agentcore", "sqlite", "do", "postgres",
# "none"]` that no code read, so every value silently meant "whatever
# FELIX_DATABASE_URL points at". Three of those five could never be implemented
# here — `do` is Durable Objects, which this stack does not run; `agentcore` is a
# vendor service; `sqlite` has no store. They now fail validation instead of
# quietly meaning `postgres`, and a plugin can register the real thing.

CheckpointerFactory = Callable[[Settings, str], SessionStore | None]

_checkpointers: dict[str, CheckpointerFactory] = {}


def register_checkpointer(name: str, factory: CheckpointerFactory) -> None:
    """Register a session-state backend for ``spec.memory.checkpointer``.

    The factory takes ``(settings, tenant_id)`` and returns a ``SessionStore``, or
    ``None`` to run the agent with no session state at all.
    """
    if name in _BUILTIN_CHECKPOINTERS:
        # Same rule as auth modes and session strategies. A checkpointer decides
        # where every conversation lands, so shadowing `postgres` from an installed
        # package is a larger blast radius than either.
        raise ValueError(
            f"checkpointer {name!r} is built in and cannot be overridden "
            f"(built-ins: {', '.join(sorted(_BUILTIN_CHECKPOINTERS))})"
        )
    _checkpointers[name] = factory


def list_checkpointers() -> list[str]:
    return sorted(_checkpointers)


def _checkpoint_postgres(settings: Settings, tenant_id: str) -> SessionStore | None:
    """The configured store — Postgres, or its in-memory twin under `memory://`."""
    return get_session_store(settings, tenant_id=tenant_id)


def _checkpoint_none(settings: Settings, tenant_id: str) -> SessionStore | None:
    """No session state. Each turn starts from the messages it was given.

    Returning ``None`` rather than a null store reuses the path the react loop
    already takes for a request with no thread — every session call there is
    guarded on ``session_store is None``, so this needs no new branch and cannot
    half-persist.
    """
    _ = settings, tenant_id
    return None


_checkpointers["postgres"] = _checkpoint_postgres
_checkpointers["none"] = _checkpoint_none
_BUILTIN_CHECKPOINTERS = frozenset(_checkpointers)

# There is deliberately no in-process built-in. A thread is not manifest-scoped:
# fifteen `/chat` routes address one by `thread_id` with no manifest in hand, so they
# resolve the store from `FELIX_DATABASE_URL` and cannot see a per-manifest choice.
# A manifest picking a *different backend* would therefore split-brain — the agent
# reading one log while `/history`, `/continue`, `/compact`, `/fork` and `/rewind`
# read another. `none` is exempt because it is a claim about the agent ("keeps no
# session state"), enforced where the agent reads, not a competing backend.
# A plugin that registers one owns that consistency problem knowingly.


def build_checkpointer(name: str, settings: Settings, *, tenant_id: str) -> SessionStore | None:
    """Resolve `spec.memory.checkpointer` to a store, or None for no persistence."""
    return _require_checkpointer(name)(settings, tenant_id)


def _require_checkpointer(name: str) -> CheckpointerFactory:
    factory = _checkpointers.get(name)
    if factory is None:
        raise ValueError(f"unknown checkpointer {name!r} (registered: {', '.join(list_checkpointers())})")
    return factory


def validate_checkpointer_config(
    name: str,
    *,
    session_strategy: str = "full_replay",
    compact_after_turn: bool = False,
    memory_capture: bool = False,
    memory_recall_tools: bool = False,
) -> None:
    """Reject a checkpointer that cannot do what the rest of the spec asks of it.

    Raises rather than warns, because the failure it prevents is silence: with no
    session store the react loop skips strategy rendering entirely (every call
    there is guarded on ``session_store is None``), so a manifest asking for
    `compacting` alongside `none` would get full replay of nothing and no
    indication that its strategy was dropped.
    """
    _require_checkpointer(name)
    if name != "none":
        return

    # Everything that silently degrades with no session store. Most are guarded on
    # `session_store is None` in the react loop; the memory ones instead read a
    # session head that is never written. Either way the request is dropped without
    # an error, which is the failure this validator exists to convert.
    strategy = (session_strategy or "full_replay").strip()
    dropped: list[str] = []
    if strategy != "full_replay":
        dropped.append(f"session.strategy {strategy!r}")
    if compact_after_turn:
        dropped.append("session.compact_after_turn")
    if memory_recall_tools:
        # `memory/tools.py:_provenance` stamps `origin_seq` from the session head via
        # `get_session_store`, not through the agent's checkpointer, so with no store
        # `head()` on an unwritten thread returns seq 0 and every remembered fact
        # lands at genesis — the same collapse as capture, by a different route.
        dropped.append("memory.recall.tools")
    if memory_capture:
        # `_turn_seq` reads the session head to stamp `origin_seq`; with no store it
        # is None every turn, so every fact lands at genesis and supersession
        # ordering collapses rather than erroring.
        dropped.append("memory.capture.enabled")
    if dropped:
        # Phrased to read correctly for one item or several.
        raise ValueError(
            f"memory.checkpointer is 'none', which silently drops: {', '.join(dropped)}. "
            "Use a checkpointer that persists, or remove them from the spec."
        )


def screenable_text(event_type: str, payload: dict[str, Any]) -> str:
    """Every string this payload will contribute to an event, joined for screening.

    Derived from `_payload_to_appendable` rather than from a list of key names, because a
    list of key names is the bug: the internal landing path screened `content`, `text`,
    `message` and `output` while the lift also carried `tool_calls` and `metadata` — and
    `event_to_chat_message` replays both into model context, `metadata.attachments` as image
    attachments and `metadata.thinking` as thinking blocks.

    Screening what is actually stored means a future field is covered the day it is lifted,
    instead of the day someone remembers to add it here.
    """
    event = _payload_to_appendable(event_type, payload)
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            if value:
                found.append(value)
        elif isinstance(value, dict):
            for key, item in value.items():
                if isinstance(key, str) and key:
                    found.append(key)
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    for part in (event.content, event.name, event.role, event.tool_calls, event.metadata):
        walk(part)
    return "\n".join(found)


def _payload_to_appendable(event_type: str, payload: dict[str, Any]) -> AppendableEvent:
    known_kinds: set[str] = {
        "message",
        "tool_call",
        "tool_result",
        "thinking",
        "audit",
        "compaction",
        "model_change",
        "thinking_level_change",
        "branch_summary",
        "custom",
        "label",
        "session_info",
        "user",
        "assistant",
        "tool",
        "system",
    }
    kind: SessionEventKind | str = event_type if event_type in known_kinds else "message"
    return AppendableEvent(
        kind=kind,  # type: ignore[arg-type]
        role=payload.get("role"),
        content=payload.get("content"),
        tool_call_id=payload.get("tool_call_id"),
        name=payload.get("name"),
        tool_calls=payload.get("tool_calls"),
        metadata=payload.get("metadata") or payload.get("meta"),
        ts=payload.get("ts"),
    )


async def append_event(
    *,
    settings: Settings,
    tenant_id: str,
    session_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    store = get_session_store(settings, tenant_id=tenant_id)
    session = store.open(session_id)
    await session.append(_payload_to_appendable(event_type, payload))
    return {"type": event_type, "payload": payload}


__all__ = [
    "CheckpointerFactory",
    "InMemorySessionStore",
    "PostgresSessionStore",
    "SessionStore",
    "append_event",
    "build_checkpointer",
    "get_session_store",
    "list_checkpointers",
    "register_checkpointer",
    "screenable_text",
    "validate_checkpointer_config",
]
