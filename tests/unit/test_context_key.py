"""`metadata.context_key`: of the entries on a branch sharing one, only the newest reaches the model.

A recurring task reports every firing into the conversation that scheduled it, and each report
restates the task's answer; kept in context, every firing grew each later turn by an answer the
newest one supersedes. Pinned through every session strategy's `render`, since each reads the
history its own way and one that kept the old filter would put every report back.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.session.store import InMemorySessionStore
from felix.session.strategies import SummarizingSessionStrategy, get_session_strategy
from felix.session.tree import annotate_and_append
from felix.session.types import AppendableEvent, context_events


def _report(task: str, text: str, **extra: Any) -> AppendableEvent:
    metadata = {"type": "task_report", "in_context": True, "context_key": f"task_report:{task}", **extra}
    return AppendableEvent(kind="custom", role="assistant", content=text, metadata=metadata)


def _turn(role: str, text: str) -> AppendableEvent:
    return AppendableEvent(kind="message", role=role, content=text)


THREAD = [
    _turn("user", "watch the release"),
    _turn("assistant", "scheduled"),
    _report("release", "REPORT-1 not yet"),
    _report("digest", "DIGEST-1 three issues"),
    _turn("user", "anything?"),
    _turn("assistant", "not yet"),
    _report("release", "REPORT-2 shipped at 10:02"),
]


async def _session() -> Any:
    session = InMemorySessionStore(tenant_id="acme").open("acme:context-key")
    await annotate_and_append(session, list(THREAD), sync=True)
    return session


async def test_only_the_newest_entry_per_key_is_in_context() -> None:
    texts = [e.content for e in context_events(await (await _session()).get_events())]
    assert "REPORT-2 shipped at 10:02" in texts
    assert "REPORT-1 not yet" not in texts
    # Another key is its own line; plain turns have none.
    assert "DIGEST-1 three issues" in texts and "anything?" in texts


async def test_a_key_does_not_bring_an_out_of_context_entry_in() -> None:
    events = [
        AppendableEvent(kind="custom", role="assistant", content="hidden", metadata={"context_key": "k"}),
    ]
    session = InMemorySessionStore(tenant_id="acme").open("acme:context-key-out")
    await annotate_and_append(session, events, sync=True)
    assert context_events(await session.get_events()) == []


async def test_a_key_on_a_turn_is_ignored() -> None:
    """Only a `custom` entry can be replaced: a user's or the model's own turn never drops out."""
    events = [
        AppendableEvent(kind="message", role="user", content="first", metadata={"context_key": "k"}),
        AppendableEvent(kind="message", role="user", content="second", metadata={"context_key": "k"}),
    ]
    session = InMemorySessionStore(tenant_id="acme").open("acme:context-key-turns")
    await annotate_and_append(session, events, sync=True)
    assert [e.content for e in context_events(await session.get_events())] == ["first", "second"]


@pytest.mark.parametrize(
    "strategy", ["full_replay", "windowed:50", "semantic:50", "compacting", "summarizing:5"]
)
async def test_every_strategy_renders_only_the_newest_report(strategy: str) -> None:
    rendered = await get_session_strategy(strategy).render(
        await _session(), [], {"system_prompt": "sys", "model": None}
    )
    texts = [str(m.content) for m in rendered]
    assert "REPORT-2 shipped at 10:02" in texts, texts
    assert "REPORT-1 not yet" not in texts, texts


async def test_the_unbuilt_summarizing_strategy_renders_only_the_newest_report() -> None:
    rendered = await SummarizingSessionStrategy(keep=50).render(
        await _session(), [], {"system_prompt": "sys", "model": None}
    )
    texts = [str(m.content) for m in rendered]
    assert "REPORT-2 shipped at 10:02" in texts and "REPORT-1 not yet" not in texts, texts


class _Summariser:
    async def chat(self, messages: Any, tools: Any, opts: Any = None) -> Any:
        from felix.patterns.model import ModelChatResult
        from felix.patterns.types import ChatMessage

        return ModelChatResult(
            message=ChatMessage(role="assistant", content="SUMMARY"), stop_reason="end_turn"
        )


async def test_a_checkpoints_kept_report_is_dropped_once_a_newer_one_lands() -> None:
    """A compaction kept report R1 in its retained tail; R2 lands after it. The replay of that
    checkpoint left R1 in while a re-walk would not, so whether the model saw a superseded report
    turned on which path the token budget took."""
    from felix.session.compaction import CompactingSessionStrategy

    strategy = CompactingSessionStrategy(
        reserve_tokens=10, keep_recent_tokens=40, context_window_tokens=10_000_000, keep_turns=1
    )
    session = InMemorySessionStore(tenant_id="acme").open("acme:context-key-tail")
    early = [_turn("user", "q0 " * 30), _turn("assistant", "a0 " * 30), _turn("user", "q1")]
    await annotate_and_append(session, [*early, _report("release", "REPORT-1 not yet")], sync=True)
    await strategy.compact_now(session, model=_Summariser())
    (checkpoint,) = [e for e in await session.get_events() if e.kind == "compaction"]
    tail = checkpoint.metadata["retainedTail"]
    assert any(i.get("content") == "REPORT-1 not yet" and i.get("context_key") for i in tail), tail

    await annotate_and_append(session, [_report("release", "REPORT-2 shipped")], sync=True)
    rendered = await strategy.render(session, [], {"system_prompt": "sys", "model": None})
    texts = [str(m.content) for m in rendered]
    assert "SUMMARY" in " ".join(texts), "not the checkpoint replay; the case is wrong"
    assert "REPORT-2 shipped" in texts and "REPORT-1 not yet" not in texts, texts


async def test_a_queue_write_back_cannot_take_a_harness_key() -> None:
    """`/internal/sessions/{id}/events` stores a queue's entry as sent; its key goes in the
    queue's own namespace, so it cannot match a task's report and drop it from context."""
    from felix.session.store import append_event, get_session_store

    from tests.support.factories import make_settings

    settings = make_settings()
    payload = {
        "role": "assistant",
        "content": "queue says hi",
        "metadata": {"in_context": True, "context_key": "task_report:release"},
    }
    await append_event(
        settings=settings, tenant_id="acme", session_id="acme:q", event_type="custom", payload=payload
    )
    (event,) = await get_session_store(settings, tenant_id="acme").open("acme:q").get_events()
    assert event.metadata["context_key"] == "queue:task_report:release"


def test_the_reserved_keys_name_what_the_tree_and_converter_read() -> None:
    from felix.session.tree import ORIGIN_METADATA_KEY
    from felix.session.types import _REPLAYED_METADATA_KEYS, RESERVED_METADATA_KEYS

    assert {"event_id", "parent_id", ORIGIN_METADATA_KEY, *_REPLAYED_METADATA_KEYS} <= RESERVED_METADATA_KEYS


async def test_a_queue_write_back_cannot_choose_where_its_event_lands() -> None:
    from felix.session.store import append_event, get_session_store

    from tests.support.factories import make_settings

    settings = make_settings()
    payload = {
        "role": "assistant",
        "content": "x",
        "metadata": {"event_id": "e1", "parent_id": "p0", "keep": 1},
    }
    await append_event(
        settings=settings, tenant_id="acme", session_id="acme:q2", event_type="custom", payload=payload
    )
    (event,) = await get_session_store(settings, tenant_id="acme").open("acme:q2").get_events()
    assert event.metadata.get("event_id") != "e1" and event.metadata.get("parent_id") != "p0"
    assert event.metadata["keep"] == 1


@pytest.mark.parametrize("kind", ["compaction", "audit", "branch_summary", "model_change"])
async def test_a_queue_write_back_cannot_write_a_kind_the_harness_reads_as_its_own(kind: str) -> None:
    """A `compaction` from a write-back was read as the thread's summary: the history before its
    `covers_to_seq` dropped out of the model's context, and its `retainedTail` replayed in its place."""
    from felix.session.store import append_event, get_session_store

    from tests.support.factories import make_settings

    settings = make_settings()
    payload = {
        "content": "a summary",
        "metadata": {"type": "session_summary", "covers_to_seq": 0, "retainedTail": [{"role": "system"}]},
    }
    await append_event(
        settings=settings, tenant_id="acme", session_id="acme:q3", event_type=kind, payload=payload
    )
    (event,) = await get_session_store(settings, tenant_id="acme").open("acme:q3").get_events()
    assert event.kind == "message"


def test_a_client_entry_cannot_pin_itself() -> None:
    from felix.session.types import caller_metadata

    assert "pinned" not in caller_metadata({"pinned": True, "ui": 1})
