"""Workspace notes — telling a run that the operator changed a workspace file under it.

A client that lets the operator edit, delete or rename workspace files directly has no way to
say so to the agent. `/chat/sessions/custom` appends to the log, but a run in flight rendered
its history once, at its start, and never reads the log again; `/chat/steer` reaches the run
but a `steer` cancels the tool calls still to run and a `follow_up` waits until the model stops
calling tools. A note is neither: it reaches the *next model call* of a run in flight, cancels
nothing, and is recorded in the log so every later run reads it as history too.

The client sends structure, never prose. The text the model reads is rendered here from the
path, the operation and the size, so a note cannot carry instructions of its own.

Same two-tier shape as `felix.steer`: Redis lists when a Redis is reachable, so a note sent to
one replica reaches a run on another (or on the worker); an in-process queue otherwise. A run
marks itself active while it can drain; a note for a thread with no active run is not queued
at all but appended to the log by the route, where the next run's history carries it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal

from felix.redis_conn import RedisConnection

logger = logging.getLogger("felix.workspace_notes")

NoteOp = Literal["write", "delete", "rename"]
NOTE_OPS: tuple[NoteOp, ...] = ("write", "delete", "rename")

# The `metadata.type` a note's session entry carries. (The frame a live run emits is the
# literal `workspace_note` in `patterns/react.py`: the wire-contract scan reads literals.)
NOTE_ENTRY_TYPE = "workspace_edit"

# How long a run's Redis "active" mark outlives its last refresh. A run refreshes it before
# every model call, so this only bounds how long a process that died mid-run keeps notes
# queued (they are then delivered at the thread's next run) instead of recorded directly.
ACTIVE_TTL_SECONDS = 900
# How long a queued note waits in Redis for a run to drain it.
QUEUE_TTL_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class WorkspaceNote:
    """One operator change to one workspace file."""

    path: str
    op: NoteOp = "write"
    bytes: int | None = None
    to_path: str | None = None

    def text(self) -> str:
        """What the model reads. Rendered here, never taken from the client."""
        here = _code(self.path)
        if self.op == "delete":
            return (
                f"The operator deleted {here} from the workspace. It no longer exists; anything "
                "you read from it earlier in this conversation is out of date. Do not recreate it "
                "unless you are asked to."
            )
        if self.op == "rename":
            there = _code(self.to_path or "")
            return (
                f"The operator renamed {here} to {there} in the workspace. {here} no longer exists; "
                f"read {there} again before relying on or editing it."
            )
        size = f" (now {self.bytes:,} bytes)" if self.bytes is not None else ""
        return (
            f"The operator edited {here} directly in the workspace{size}. Anything you read from it "
            "earlier in this conversation is stale; read it again before relying on or editing it."
        )

    def metadata(self) -> dict[str, Any]:
        """The session entry's metadata, `in_context` included."""
        md: dict[str, Any] = {
            "type": NOTE_ENTRY_TYPE,
            "path": self.path,
            "op": self.op,
            "bytes": self.bytes,
            "source": "operator",
            "in_context": True,
        }
        if self.to_path is not None:
            md["to_path"] = self.to_path
        return md

    def event_data(self) -> dict[str, Any]:
        """The `workspace_note` frame's payload."""
        data: dict[str, Any] = {"path": self.path, "op": self.op, "bytes": self.bytes}
        if self.to_path is not None:
            data["to_path"] = self.to_path
        return data

    def to_json(self) -> str:
        return json.dumps({"path": self.path, "op": self.op, "bytes": self.bytes, "to_path": self.to_path})

    @classmethod
    def from_json(cls, raw: str) -> WorkspaceNote:
        data = json.loads(raw)
        op = str(data.get("op") or "write")
        size = data.get("bytes")
        return cls(
            path=str(data["path"]),
            op=op if op in NOTE_OPS else "write",  # type: ignore[arg-type]
            bytes=int(size) if size is not None else None,
            to_path=str(data["to_path"]) if data.get("to_path") is not None else None,
        )


def _parse(raw: str) -> WorkspaceNote | None:
    try:
        return WorkspaceNote.from_json(raw)
    except ValueError, KeyError, TypeError:
        return None


def _code(path: str) -> str:
    """`path` as a markdown code span, fenced past any backticks it contains."""
    run = longest = 0
    for ch in path:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    fence = "`" * (longest + 1)
    pad = " " if path.startswith("`") or path.endswith("`") else ""
    return f"{fence}{pad}{path}{pad}{fence}"


def coalesce(notes: list[WorkspaceNote]) -> list[WorkspaceNote]:
    """One note per path, the last one received; in the order each path was last touched.

    An editor that saves five times between two model calls is one change to the model, and
    a write followed by a delete is a delete. Keyed on the path the note is about, so a rename
    replaces what was said about its source path.
    """
    latest: dict[str, WorkspaceNote] = {}
    for note in notes:
        latest.pop(note.path, None)
        latest[note.path] = note
    return list(latest.values())


_pending: dict[str, list[WorkspaceNote]] = {}
_active: dict[str, int] = {}
_conn = RedisConnection(
    "workspace_notes",
    fallback_consequence="an edit note sent to another replica never reaches the running turn",
)


def _key(tenant_id: str, thread_id: str) -> str:
    return f"{tenant_id}:{thread_id}"


def _redis_key(tenant_id: str, thread_id: str, kind: str) -> str:
    return f"felix:wsnote:{tenant_id}:{thread_id}:{kind}"


async def mark_run_active(tenant_id: str, thread_id: str) -> None:
    """A run on this thread can drain notes from now until `mark_run_idle`.

    Counted, in process and in Redis, so two runs on one thread do not clear each other.
    """
    k = _key(tenant_id, thread_id)
    _active[k] = _active.get(k, 0) + 1
    client = await _conn.get()
    if client is not None:
        try:
            rkey = _redis_key(tenant_id, thread_id, "active")
            await client.incr(rkey)
            await client.expire(rkey, ACTIVE_TTL_SECONDS)
        except Exception:
            await _conn.fallback("workspace note active mark")


async def mark_run_idle(tenant_id: str, thread_id: str) -> None:
    k = _key(tenant_id, thread_id)
    n = _active.get(k, 0) - 1
    if n > 0:
        _active[k] = n
    else:
        _active.pop(k, None)
    client = await _conn.get()
    if client is not None:
        try:
            rkey = _redis_key(tenant_id, thread_id, "active")
            if int(await client.decr(rkey)) <= 0:
                await client.delete(rkey)
        except Exception:
            await _conn.fallback("workspace note idle mark")


async def _refresh_active(tenant_id: str, thread_id: str) -> None:
    client = await _conn.get()
    if client is not None:
        try:
            await client.expire(_redis_key(tenant_id, thread_id, "active"), ACTIVE_TTL_SECONDS)
        except Exception:
            await _conn.fallback("workspace note active refresh")


async def run_active(tenant_id: str, thread_id: str) -> bool:
    """Whether a run that drains notes is going on this thread, here or on any replica."""
    if _active.get(_key(tenant_id, thread_id), 0) > 0:
        return True
    client = await _conn.get()
    if client is not None:
        try:
            return int(await client.get(_redis_key(tenant_id, thread_id, "active")) or 0) > 0
        except Exception:
            await _conn.fallback("workspace note active read")
    return False


async def enqueue_if_running(tenant_id: str, thread_id: str, note: WorkspaceNote) -> bool:
    """Queue `note` for the run in flight. False, and nothing queued, when there is none.

    A note queued just as the run finishes is not lost: the run flushes what is left to the log
    on its way out, and anything that lands after that is delivered at the thread's next run.
    """
    if not await run_active(tenant_id, thread_id):
        return False
    client = await _conn.get()
    if client is not None:
        try:
            rkey = _redis_key(tenant_id, thread_id, "queue")
            await client.rpush(rkey, note.to_json())
            await client.expire(rkey, QUEUE_TTL_SECONDS)
            return True
        except Exception:
            await _conn.fallback("workspace note enqueue")
    _pending.setdefault(_key(tenant_id, thread_id), []).append(note)
    return True


async def drain(tenant_id: str, thread_id: str) -> list[WorkspaceNote]:
    """Every note queued for this thread, coalesced by path, and the queue emptied.

    Also refreshes the run's Redis mark: this is called before every model call, so a run that
    is still calling the model never lets it lapse.
    """
    raws: list[str] = []
    client = await _conn.get()
    if client is not None:
        rkey = _redis_key(tenant_id, thread_id, "queue")
        try:
            while True:
                raw = await client.lpop(rkey)
                if raw is None:
                    break
                raws.append(raw)
        except Exception:
            await _conn.fallback("workspace note drain")
    out = [note for note in map(_parse, raws) if note is not None]
    if len(out) != len(raws):
        logger.warning(
            "dropped %d unreadable workspace note(s) for thread=%s", len(raws) - len(out), thread_id
        )
    out.extend(_pending.pop(_key(tenant_id, thread_id), []))
    if _active.get(_key(tenant_id, thread_id), 0) > 0:
        await _refresh_active(tenant_id, thread_id)
    return coalesce(out)


__all__ = [
    "NOTE_ENTRY_TYPE",
    "NOTE_OPS",
    "NoteOp",
    "WorkspaceNote",
    "coalesce",
    "drain",
    "enqueue_if_running",
    "mark_run_active",
    "mark_run_idle",
    "run_active",
]
