"""`DurableBuffer.drain`: what the database refuses is set aside; what it cannot take goes back.

The store conformance suites prove the Postgres half with a real refused row. These pin the
decision itself, including the part a real database makes slow to reach: when the database is
down, the per-event pass must stop at the first failure rather than spend a round trip — and a
connect timeout — on every event in a ten-thousand-event backlog.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.buffers import DurableBuffer
from sqlalchemy.exc import DataError, OperationalError


def _events(*ids: str) -> list[dict[str, Any]]:
    return [{"id": i} for i in ids]


def _refused(event_id: str) -> DataError:
    return DataError("INSERT", {}, Exception(f"refused {event_id}"))


async def test_a_refused_event_is_quarantined_and_its_neighbours_are_written() -> None:
    buffer = DurableBuffer("t")
    for e in _events("a", "poison", "b"):
        buffer.append(e)
    written: list[str] = []

    async def write(batch: list[dict[str, Any]]) -> None:
        if any(e["id"] == "poison" for e in batch):
            raise _refused("poison")
        written.extend(e["id"] for e in batch)

    assert await buffer.drain(write) == 2
    assert written == ["a", "b"] and len(buffer) == 0 and buffer.quarantined == 1


async def test_an_unavailable_database_requeues_everything_after_one_try() -> None:
    buffer = DurableBuffer("t")
    for e in _events("a", "b", "c"):
        buffer.append(e)
    calls: list[int] = []

    async def write(batch: list[dict[str, Any]]) -> None:
        calls.append(len(batch))
        raise OperationalError("INSERT", {}, Exception("connection refused"))

    with pytest.raises(OperationalError):
        await buffer.drain(write)
    assert calls == [3, 1], "the batch, then one event — not one round trip per event"
    assert [e["id"] for e in buffer.snapshot()] == ["a", "b", "c"], "requeued in order"
    assert buffer.quarantined == 0


async def test_an_outage_after_a_quarantine_keeps_the_rest() -> None:
    buffer = DurableBuffer("t")
    for e in _events("poison", "a", "b"):
        buffer.append(e)
    state = {"up": True}

    async def write(batch: list[dict[str, Any]]) -> None:
        if len(batch) > 1:
            raise _refused("poison")
        if batch[0]["id"] == "poison":
            state["up"] = False  # the database goes away right after refusing it
            raise _refused("poison")
        if not state["up"]:
            raise OperationalError("INSERT", {}, Exception("gone"))

    with pytest.raises(OperationalError):
        await buffer.drain(write)
    assert buffer.quarantined == 1
    assert [e["id"] for e in buffer.snapshot()] == ["a", "b"]


async def test_a_single_refused_event_is_quarantined_without_a_second_try() -> None:
    buffer = DurableBuffer("t")
    buffer.append({"id": "poison"})
    calls: list[int] = []

    async def write(batch: list[dict[str, Any]]) -> None:
        calls.append(len(batch))
        raise _refused("poison")

    assert await buffer.drain(write) == 0
    assert calls == [1] and len(buffer) == 0 and buffer.quarantined == 1
