"""`tree.leaf_lock` and the appends made inside it.

The lock is not reentrant, so an append inside a held `leaf_lock` passes `lock_held=True`
and runs in that hold. A caller claiming a hold it does not have would append unserialised
with nothing to show for it, so that raises. The races the lock closes are in
`tests/conformance/test_turn_leaf.py`, which drives real turns against both stores.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from felix.session import tree
from felix.session.types import AppendableEvent


@dataclass
class _Session:
    id: str
    events: list[AppendableEvent] = field(default_factory=list)

    async def append_batch(self, events: list[AppendableEvent]) -> list[int]:
        self.events.extend(events)
        return list(range(len(events)))

    async def get_events(self, opts: Any = None) -> list[Any]:
        return list(self.events)


def _event() -> AppendableEvent:
    return AppendableEvent(kind="custom", content="x")


async def test_an_append_inside_the_hold_runs_in_it() -> None:
    session = _Session("leaf-lock:held")
    async with tree.leaf_lock(session):
        [eid] = await asyncio.wait_for(tree.annotate_and_append(session, [_event()], lock_held=True), 1)
    assert tree.get_leaf(session.id) == eid


async def test_claiming_a_hold_that_is_not_held_raises() -> None:
    session = _Session("leaf-lock:unheld")
    with pytest.raises(RuntimeError, match="outside leaf_lock"):
        await tree.annotate_and_append(session, [_event()], lock_held=True)
    assert session.events == []


async def test_an_ordinary_append_waits_for_the_hold() -> None:
    session = _Session("leaf-lock:waits")
    async with tree.leaf_lock(session):
        append = asyncio.create_task(tree.annotate_and_append(session, [_event()]))
        await asyncio.sleep(0.05)
        assert not append.done()
        assert session.events == []
    await append
    assert len(session.events) == 1
