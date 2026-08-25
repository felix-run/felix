"""One contract, run against every session-store backend.

`memory://` is the supported no-infrastructure path and the one CI runs, and
`tests/unit/test_invariants.py` asserts every Postgres-touching module *has* an in-memory
twin. Nothing asserted the twins behave the same, so every green CI run was evidence
about the twin rather than about production.

This audit has now twice found that copies of a thing drift apart when nothing compares
them — two model-metadata tables that disagreed on a context window, and two turn loops
that disagreed on what to audit. Storage is the same shape of risk with worse
consequences, so the contract is written once and both backends run it.

Add a backend to `BACKENDS` and it inherits every assertion here.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.session.types import AppendableEvent, GetEventsOpts

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("store", BACKENDS, indirect=True)


def _msg(content: str, *, role: str = "user", kind: str = "message") -> AppendableEvent:
    return AppendableEvent(kind=kind, role=role, content=content)


async def _append_all(session: Any, events: list[AppendableEvent]) -> None:
    for ev in events:
        await session.append(ev)


# --- appending and ordering -----------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_events_come_back_in_the_order_they_went_in(store: Any) -> None:
    session = store.open("t-order")
    await _append_all(session, [_msg("one"), _msg("two"), _msg("three")])
    events = await session.get_events()
    assert [e.content for e in events] == ["one", "two", "three"]


@parametrized
@pytest.mark.asyncio
async def test_seq_is_dense_and_monotonic_from_zero(store: Any) -> None:
    session = store.open("t-seq")
    await _append_all(session, [_msg(str(i)) for i in range(5)])
    events = await session.get_events()
    assert [e.seq for e in events] == [0, 1, 2, 3, 4]


@parametrized
@pytest.mark.asyncio
async def test_batch_append_continues_the_same_sequence(store: Any) -> None:
    session = store.open("t-batch")
    await session.append(_msg("first"))
    await session.append_batch([_msg("second"), _msg("third")])
    events = await session.get_events()
    assert [e.seq for e in events] == [0, 1, 2]
    assert [e.content for e in events] == ["first", "second", "third"]


@parametrized
@pytest.mark.asyncio
async def test_an_empty_batch_is_a_no_op(store: Any) -> None:
    session = store.open("t-empty-batch")
    await session.append(_msg("only"))
    await session.append_batch([])
    events = await session.get_events()
    assert len(events) == 1


@parametrized
@pytest.mark.asyncio
async def test_threads_are_isolated_from_each_other(store: Any) -> None:
    a, b = store.open("t-a"), store.open("t-b")
    await a.append(_msg("belongs to a"))
    await b.append(_msg("belongs to b"))
    a_events = await a.get_events()
    b_events = await b.get_events()
    assert [e.content for e in a_events] == ["belongs to a"]
    assert [e.content for e in b_events] == ["belongs to b"]


@parametrized
@pytest.mark.asyncio
async def test_reopening_a_thread_sees_what_was_written(store: Any) -> None:
    await store.open("t-reopen").append(_msg("persisted"))
    events = await store.open("t-reopen").get_events()
    assert [e.content for e in events] == ["persisted"]


# --- what an event carries ------------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_every_field_survives_the_round_trip(store: Any) -> None:
    session = store.open("t-fields")
    await session.append(
        AppendableEvent(
            kind="tool_result",
            role="tool",
            content="output",
            tool_call_id="call-1",
            name="search",
            tool_calls=[{"id": "call-1", "name": "search", "args": {"q": "x"}}],
            metadata={"thinking": [{"type": "thinking", "signature": "sig"}]},
        )
    )
    (event,) = await session.get_events()
    assert event.kind == "tool_result"
    assert event.role == "tool"
    assert event.content == "output"
    assert event.tool_call_id == "call-1"
    assert event.name == "search"
    assert event.tool_calls == [{"id": "call-1", "name": "search", "args": {"q": "x"}}]
    assert event.metadata == {"thinking": [{"type": "thinking", "signature": "sig"}]}


@parametrized
@pytest.mark.asyncio
async def test_optional_fields_stay_absent(store: Any) -> None:
    session = store.open("t-sparse")
    await session.append(AppendableEvent(kind="message", role="assistant", content="plain"))
    (event,) = await session.get_events()
    assert event.tool_call_id is None
    assert event.name is None
    assert event.tool_calls is None
    assert event.metadata is None


@parametrized
@pytest.mark.asyncio
async def test_a_supplied_timestamp_is_kept(store: Any) -> None:
    session = store.open("t-ts")
    await session.append(AppendableEvent(kind="message", role="user", content="x", ts=1234.5))
    (event,) = await session.get_events()
    assert event.ts == pytest.approx(1234.5)


@parametrized
@pytest.mark.asyncio
async def test_a_missing_timestamp_is_filled_in(store: Any) -> None:
    session = store.open("t-ts-auto")
    await session.append(_msg("x"))
    (event,) = await session.get_events()
    assert event.ts > 0


@parametrized
@pytest.mark.asyncio
async def test_empty_content_is_preserved_not_nulled(store: Any) -> None:
    session = store.open("t-blank")
    await session.append(AppendableEvent(kind="message", role="assistant", content=""))
    (event,) = await session.get_events()
    assert event.content == ""


# --- querying -------------------------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_from_seq_is_inclusive(store: Any) -> None:
    session = store.open("t-from")
    await _append_all(session, [_msg(str(i)) for i in range(5)])
    events = await session.get_events(GetEventsOpts(from_seq=2))
    assert [e.seq for e in events] == [2, 3, 4]


@parametrized
@pytest.mark.asyncio
async def test_to_seq_is_exclusive(store: Any) -> None:
    session = store.open("t-to")
    await _append_all(session, [_msg(str(i)) for i in range(5)])
    events = await session.get_events(GetEventsOpts(to_seq=2))
    assert [e.seq for e in events] == [0, 1]


@parametrized
@pytest.mark.asyncio
async def test_from_and_to_bound_a_window(store: Any) -> None:
    session = store.open("t-window")
    await _append_all(session, [_msg(str(i)) for i in range(6)])
    events = await session.get_events(GetEventsOpts(from_seq=2, to_seq=5))
    assert [e.seq for e in events] == [2, 3, 4]


@parametrized
@pytest.mark.asyncio
async def test_kinds_filters_to_the_named_kinds(store: Any) -> None:
    session = store.open("t-kinds")
    await _append_all(
        session,
        [
            _msg("a", kind="message"),
            _msg("b", kind="thinking"),
            _msg("c", kind="message"),
            _msg("d", kind="audit"),
        ],
    )
    events = await session.get_events(GetEventsOpts(kinds=["message"]))
    assert [e.content for e in events] == ["a", "c"]


@parametrized
@pytest.mark.asyncio
async def test_limit_takes_the_earliest_events(store: Any) -> None:
    session = store.open("t-limit")
    await _append_all(session, [_msg(str(i)) for i in range(5)])
    events = await session.get_events(GetEventsOpts(limit=2))
    assert [e.seq for e in events] == [0, 1]


@parametrized
@pytest.mark.asyncio
async def test_limit_applies_after_from_seq(store: Any) -> None:
    """Order matters: filter the window first, then take from it."""
    session = store.open("t-limit-from")
    await _append_all(session, [_msg(str(i)) for i in range(6)])
    events = await session.get_events(GetEventsOpts(from_seq=3, limit=2))
    assert [e.seq for e in events] == [3, 4]


@parametrized
@pytest.mark.asyncio
async def test_querying_an_unknown_thread_is_empty_not_an_error(store: Any) -> None:
    events = await store.open("t-never-written").get_events()
    assert events == []


# --- head, reset, wake ----------------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_head_counts_what_is_stored(store: Any) -> None:
    session = store.open("t-head")
    head = await session.head()
    assert head["seq"] == 0
    await _append_all(session, [_msg("a"), _msg("b")])
    head = await session.head()
    assert head["seq"] == 2


@parametrized
@pytest.mark.asyncio
async def test_reset_empties_the_thread(store: Any) -> None:
    session = store.open("t-reset")
    await _append_all(session, [_msg("a"), _msg("b")])
    await session.reset()
    events = await session.get_events()
    head = await session.head()
    assert events == []
    assert head["seq"] == 0


@parametrized
@pytest.mark.asyncio
async def test_appending_after_reset_starts_over(store: Any) -> None:
    session = store.open("t-reset-append")
    await _append_all(session, [_msg("a"), _msg("b")])
    await session.reset()
    await session.append(_msg("fresh"))
    events = await session.get_events()
    assert [(e.seq, e.content) for e in events] == [(0, "fresh")]


@parametrized
@pytest.mark.asyncio
async def test_reset_does_not_touch_other_threads(store: Any) -> None:
    keep, drop = store.open("t-keep"), store.open("t-drop")
    await keep.append(_msg("kept"))
    await drop.append(_msg("dropped"))
    await drop.reset()
    events = await keep.get_events()
    assert [e.content for e in events] == ["kept"]


@parametrized
@pytest.mark.asyncio
async def test_wake_reports_a_clean_session_as_clean(store: Any) -> None:
    session = store.open("t-wake-clean")
    await _append_all(session, [_msg("hi"), _msg("done", role="assistant")])
    state = await session.wake()
    assert not getattr(state, "pending_tool_calls", None)


@parametrized
@pytest.mark.asyncio
async def test_wake_sees_a_tool_call_with_no_result(store: Any) -> None:
    """Crash recovery depends on this being identical across backends."""
    session = store.open("t-wake-pending")
    await session.append(
        AppendableEvent(
            kind="message",
            role="assistant",
            content="calling",
            tool_calls=[{"id": "c1", "name": "search", "args": {}}],
        )
    )
    state = await session.wake()
    assert getattr(state, "pending_tool_calls", None)


# --- secret handling ------------------------------------------------------------


SECRET = "super-secret-value-9f2b"


@pytest.fixture
def configured_secret(monkeypatch: pytest.MonkeyPatch) -> str:
    """Make one value a known secret, the way a manifest `secret:NAME` ref would."""
    import felix.secrets as secrets_mod

    monkeypatch.setattr(secrets_mod, "collected_secret_values", lambda: [SECRET])
    return SECRET


@parametrized
@pytest.mark.asyncio
async def test_secrets_in_content_are_masked_on_the_way_in(store: Any, configured_secret: str) -> None:
    """Masking is a storage guarantee, so it has to hold on every backend."""
    session = store.open("t-redact")
    await session.append(_msg(f"the key is {configured_secret}"))
    (event,) = await session.get_events()
    assert configured_secret not in (event.content or "")
    assert "[REDACTED]" in (event.content or "")


@parametrized
@pytest.mark.asyncio
async def test_secrets_in_tool_arguments_are_masked(store: Any, configured_secret: str) -> None:
    session = store.open("t-redact-args")
    await session.append(
        AppendableEvent(
            kind="message",
            role="assistant",
            content="calling",
            tool_calls=[{"id": "c1", "name": "post", "args": {"token": configured_secret}}],
        )
    )
    (event,) = await session.get_events()
    assert configured_secret not in str(event.tool_calls)


@parametrized
@pytest.mark.asyncio
async def test_secrets_in_metadata_are_masked(store: Any, configured_secret: str) -> None:
    session = store.open("t-redact-meta")
    await session.append(
        AppendableEvent(kind="message", role="assistant", content="x", metadata={"note": configured_secret})
    )
    (event,) = await session.get_events()
    assert configured_secret not in str(event.metadata)


# --- concurrency ----------------------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_concurrent_appends_do_not_collide(store: Any) -> None:
    """Several surfaces append to one thread by design — an SSE stream, /chat/steer,
    /chat/tool_result. Postgres serializes with an advisory lock because computing
    `max(seq) + 1` is a read-modify-write; the in-memory twin has to end up in the same
    place or a race reproduces on only one of them."""
    import asyncio

    session = store.open("t-concurrent")
    await asyncio.gather(*[session.append(_msg(f"m{i}")) for i in range(12)])

    events = await session.get_events()
    seqs = [e.seq for e in events]
    assert len(events) == 12, "every append has to survive"
    assert len(set(seqs)) == 12, "no two events may share a seq"
    assert seqs == sorted(seqs), "events come back ordered"
    assert seqs == list(range(12)), "the sequence stays dense"
    assert {e.content for e in events} == {f"m{i}" for i in range(12)}


@parametrized
@pytest.mark.asyncio
async def test_concurrent_batches_keep_their_events_contiguous(store: Any) -> None:
    """A batch is one unit: its events must not be interleaved with another batch's."""
    import asyncio

    session = store.open("t-concurrent-batch")

    async def batch(tag: str) -> None:
        await session.append_batch([_msg(f"{tag}-{i}") for i in range(3)])

    await asyncio.gather(batch("a"), batch("b"), batch("c"))

    events = await session.get_events()
    assert len(events) == 9
    assert [e.seq for e in events] == list(range(9))
    for start in (0, 3, 6):
        tags = {(e.content or "").split("-")[0] for e in events[start : start + 3]}
        assert len(tags) == 1, f"batch was interleaved at seq {start}: {tags}"


@parametrized
@pytest.mark.asyncio
async def test_append_returns_the_sequence_numbers_it_allocated(store: Any) -> None:
    """The writer computes these under the lock; handing them back saves a `max(seq)`
    read to learn a number that was just decided.

    Asserted against both arms because sequence allocation is the one thing the
    in-memory twin models differently — it counts a list, Postgres reads and locks —
    so a returned value that is right for one arm proves nothing about the other.
    """
    session = store.open("seq-return")

    first = await session.append_batch([_msg("a"), _msg("b")])
    assert first == [0, 1], f"expected the first batch to start at 0, got {first}"

    second = await session.append_batch([_msg("c")])
    assert second == [2], f"the second batch must continue the run, got {second}"

    # The numbers reported are the numbers stored, not a parallel count.
    stored = [e.seq for e in await session.get_events()]
    assert stored == first + second == [0, 1, 2]

    single = await session.append(_msg("d"))
    assert single == 3, f"append should report its own seq, got {single}"


@parametrized
@pytest.mark.asyncio
async def test_an_empty_append_allocates_nothing(store: Any) -> None:
    session = store.open("seq-empty")
    assert await session.append_batch([]) == []
    assert await session.get_events() == []
