"""SessionStore — Postgres-backed + InMemory for tests."""

from __future__ import annotations

import logging
import time
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
    _events: list[SessionEvent] = field(default_factory=list)

    async def append(self, event: AppendableEvent) -> None:
        await self.append_batch([event])

    async def append_batch(self, events: list[AppendableEvent]) -> None:
        now = time.time()
        from felix.secrets import redact_json, redact_text

        for ev in events:
            content = ev.content
            if isinstance(content, str):
                content = redact_text(content)
            self._events.append(
                SessionEvent(
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
            )

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

    async def wake(self) -> WakeState:
        return analyze_wake(self._events)


class InMemorySessionStore:
    """Process-local session store for unit tests."""

    def __init__(self) -> None:
        self._sessions: dict[str, _MemorySession] = {}

    def open(self, thread_id: str) -> Session:
        if not thread_id:
            return _MemorySession(id="")
        if thread_id not in self._sessions:
            self._sessions[thread_id] = _MemorySession(id=thread_id)
        return self._sessions[thread_id]


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

    async def append(self, event: AppendableEvent) -> None:
        await self.append_batch([event])

    async def append_batch(self, events: list[AppendableEvent]) -> None:
        if not self.id or not events:
            return
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
            now = time.time()
            for i, ev in enumerate(events):
                content = ev.content
                if isinstance(content, str):
                    content = redact_text(content)
                db.add(
                    SessionEventRow(
                        tenant_id=self.tenant_id,
                        thread_id=self.id,
                        seq=next_seq + i,
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

    def __init__(self, session_factory: Any, *, tenant_id: str = "default") -> None:
        self._session_factory = session_factory
        self._tenant_id = tenant_id

    def open(self, thread_id: str) -> Session:
        return _PostgresSession(
            id=thread_id,
            tenant_id=self._tenant_id,
            session_factory=self._session_factory,
        )


_memory_session_store = InMemorySessionStore()


def _use_in_memory_session(settings: Settings) -> bool:
    url = settings.database_url
    return ":memory:" in url or "sqlite" in url or url.startswith("memory://")


def get_session_store(settings: Settings, *, tenant_id: str = "default") -> SessionStore:
    if _use_in_memory_session(settings):
        return _memory_session_store
    from felix.db.session import get_session_factory

    return PostgresSessionStore(
        get_session_factory(settings=settings),
        tenant_id=tenant_id,
    )


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
    "InMemorySessionStore",
    "PostgresSessionStore",
    "SessionStore",
    "append_event",
    "get_session_store",
]
