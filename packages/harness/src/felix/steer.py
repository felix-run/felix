"""In-flight message queue — steer vs follow-up while an agent run is active."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class QueueKind(str, Enum):
    STEER = "steer"
    FOLLOW_UP = "follow_up"


@dataclass(slots=True)
class QueuedMessage:
    kind: QueueKind
    text: str


@dataclass
class _RunQueue:
    steer: asyncio.Queue[QueuedMessage] = field(default_factory=asyncio.Queue)
    follow_up: asyncio.Queue[QueuedMessage] = field(default_factory=asyncio.Queue)
    cancel_remaining_tools: bool = False


_queues: dict[str, _RunQueue] = {}
_lock = asyncio.Lock()


def _key(tenant_id: str, thread_id: str) -> str:
    return f"{tenant_id}:{thread_id}"


async def ensure_run_queue(tenant_id: str, thread_id: str) -> _RunQueue:
    async with _lock:
        k = _key(tenant_id, thread_id)
        if k not in _queues:
            _queues[k] = _RunQueue()
        return _queues[k]


async def enqueue(
    tenant_id: str,
    thread_id: str,
    *,
    kind: Literal["steer", "follow_up"] | QueueKind,
    text: str,
) -> dict[str, str]:
    q = await ensure_run_queue(tenant_id, thread_id)
    qk = QueueKind(kind) if not isinstance(kind, QueueKind) else kind
    msg = QueuedMessage(kind=qk, text=text)
    if qk is QueueKind.STEER:
        q.cancel_remaining_tools = True
        await q.steer.put(msg)
    else:
        await q.follow_up.put(msg)
    return {"queued": qk.value, "thread_id": thread_id}


async def drain_steer(tenant_id: str, thread_id: str) -> list[QueuedMessage]:
    q = await ensure_run_queue(tenant_id, thread_id)
    out: list[QueuedMessage] = []
    while not q.steer.empty():
        try:
            out.append(q.steer.get_nowait())
        except asyncio.QueueEmpty:
            break
    return out


async def drain_follow_up(tenant_id: str, thread_id: str) -> list[QueuedMessage]:
    q = await ensure_run_queue(tenant_id, thread_id)
    out: list[QueuedMessage] = []
    while not q.follow_up.empty():
        try:
            out.append(q.follow_up.get_nowait())
        except asyncio.QueueEmpty:
            break
    return out


async def should_cancel_remaining_tools(tenant_id: str, thread_id: str) -> bool:
    q = await ensure_run_queue(tenant_id, thread_id)
    return q.cancel_remaining_tools


async def clear_cancel_flag(tenant_id: str, thread_id: str) -> None:
    q = await ensure_run_queue(tenant_id, thread_id)
    q.cancel_remaining_tools = False


async def release_run_queue(tenant_id: str, thread_id: str) -> None:
    async with _lock:
        _queues.pop(_key(tenant_id, thread_id), None)


__all__ = [
    "QueueKind",
    "QueuedMessage",
    "clear_cancel_flag",
    "drain_follow_up",
    "drain_steer",
    "enqueue",
    "ensure_run_queue",
    "release_run_queue",
    "should_cancel_remaining_tools",
]
