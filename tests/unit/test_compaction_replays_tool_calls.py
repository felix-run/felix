"""A compaction checkpoint replays the turns it kept, tool calls and all.

The checkpoint records each kept turn, `tool_calls` included, and the replay rebuilt them by
hand from `role`, `content`, `tool_call_id` and `name` only. So after any compaction a kept
tool result arrived with no assistant turn calling it -- a request Anthropic refuses -- and a
signed thinking block, which extended thinking must see again, was gone. `contributor` and
`triage` both compact, and both run long tool-heavy sessions.
"""

from __future__ import annotations

from typing import Any

from felix.patterns.model import ModelChatResult
from felix.patterns.types import ChatMessage
from felix.session.compaction import CompactingSessionStrategy
from felix.session.store import InMemorySessionStore
from felix.session.tree import annotate_and_append
from felix.session.types import AppendableEvent

THINKING = [{"type": "thinking", "thinking": "work it out", "signature": "sig-abc"}]


class _Summarizer:
    async def chat(self, messages: list[ChatMessage], tools: Any, opts: Any = None) -> ModelChatResult:
        return ModelChatResult(
            message=ChatMessage(role="assistant", content="summary"), stop_reason="end_turn"
        )


async def _tool_heavy_session(thread: str, rounds: int = 12) -> Any:
    session = InMemorySessionStore(tenant_id="acme").open(thread)
    for i in range(rounds):
        await annotate_and_append(
            session,
            [
                AppendableEvent(kind="message", role="user", content=f"step {i} " + "detail " * 60),
                AppendableEvent(
                    kind="message",
                    role="assistant",
                    content="",
                    tool_calls=[{"id": f"call-{i}", "name": "read_file", "args": {"path": f"f{i}.txt"}}],
                    metadata={"thinking": THINKING},
                ),
                AppendableEvent(
                    kind="tool_result",
                    role="tool",
                    content=f"contents {i} " * 40,
                    tool_call_id=f"call-{i}",
                    name="read_file",
                ),
                AppendableEvent(kind="message", role="assistant", content=f"done with step {i}"),
            ],
        )
    return session


async def _render(strategy: CompactingSessionStrategy, session: Any) -> list[ChatMessage]:
    return await strategy.render(
        session,
        [ChatMessage(role="user", content="continue")],
        {"system_prompt": "sys", "model": _Summarizer()},
    )


def _assert_every_tool_result_is_answered(messages: list[ChatMessage]) -> int:
    """Each tool message directly follows the assistant turn calling it, or a sibling result.

    Directly, because a user turn between a call and its result is refused as well. Returns
    how many tool messages there were.
    """
    seen = 0
    for i, m in enumerate(messages):
        if m.role != "tool":
            continue
        seen += 1
        j = i - 1
        while j >= 0 and messages[j].role == "tool":
            j -= 1
        caller = messages[j] if j >= 0 else None
        assert caller is not None and caller.role == "assistant", (
            f"tool result {m.tool_call_id} is not directly after an assistant turn"
        )
        assert m.tool_call_id in {tc.id for tc in (caller.tool_calls or [])}, (
            f"tool result {m.tool_call_id} replayed with no call answering it"
        )
    return seen


async def test_a_checkpoint_replays_its_tool_calls_and_thinking() -> None:
    session = await _tool_heavy_session("acme:tail")
    tight = CompactingSessionStrategy(reserve_tokens=10, keep_recent_tokens=600, context_window_tokens=2_000)
    await _render(tight, session)
    events = await session.get_events()
    (checkpoint,) = [e for e in events if e.kind == "compaction"]
    assert any(item.get("tool_calls") for item in checkpoint.metadata["retainedTail"]), (
        "the kept tail holds no tool call; the test proves nothing"
    )

    # Room to spare, so the checkpoint branch returns the replay rather than re-walking.
    roomy = CompactingSessionStrategy(
        reserve_tokens=10, keep_recent_tokens=600, context_window_tokens=10_000_000
    )
    replayed = await _render(roomy, session)

    # The exact shape of the replay: system, summary, the kept turns in order, the new turn.
    # With the log intact the re-walk renders the same kept events and passes this too, so it
    # does not prove which branch ran -- the two tests below do, because their kept turns
    # exist only in the checkpoint. This one proves a real compaction ends answerable.
    tail = checkpoint.metadata["retainedTail"]
    assert [e.kind for e in await session.get_events()].count("compaction") == 1, "it compacted again"
    assert len(replayed) == 2 + len(tail) + 1, "system, summary, the retained tail, the new turn"
    assert [(m.role, m.tool_call_id) for m in replayed[2:-1]] == [
        (item["role"], item["tool_call_id"]) for item in tail
    ]

    assert _assert_every_tool_result_is_answered(replayed) == sum(item["role"] == "tool" for item in tail)
    calls = [m for m in replayed if m.role == "assistant" and m.tool_calls]
    assert calls and all(m.thinking == THINKING for m in calls), "a signed thinking block was dropped"


async def test_a_checkpoint_written_before_the_fix_still_replays_its_calls() -> None:
    # Stored checkpoints in deployed databases have `tool_calls` in each tail item and no
    # `metadata`. They must not keep producing orphaned tool results after this ships.
    session = InMemorySessionStore(tenant_id="acme").open("acme:legacy-tail")
    await annotate_and_append(session, [AppendableEvent(kind="message", role="user", content="old")])
    await session.append(
        AppendableEvent(
            kind="compaction",
            content="summary",
            metadata={
                "type": "compaction",
                "covers_to_seq": 0,
                "retainedTail": [
                    {
                        "role": "user",
                        "content": "read it",
                        "tool_call_id": None,
                        "name": None,
                        "tool_calls": None,
                    },
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_call_id": None,
                        "name": None,
                        "tool_calls": [{"id": "call-x", "name": "read_file", "args": {"path": "a"}}],
                    },
                    {
                        "role": "tool",
                        "content": "a's text",
                        "tool_call_id": "call-x",
                        "name": "read_file",
                        "tool_calls": None,
                    },
                ],
            },
        )
    )
    roomy = CompactingSessionStrategy(
        reserve_tokens=10, keep_recent_tokens=600, context_window_tokens=10_000_000
    )

    replayed = await _render(roomy, session)

    assert _assert_every_tool_result_is_answered(replayed) == 1


async def test_a_checkpoint_replays_a_kept_turns_attachments() -> None:
    image = {"url": "data:image/png;base64,AAAA", "media_type": "image/png", "filename": "shot.png"}
    session = InMemorySessionStore(tenant_id="acme").open("acme:tail-image")
    await annotate_and_append(session, [AppendableEvent(kind="message", role="user", content="old")])
    await session.append(
        AppendableEvent(
            kind="compaction",
            content="summary",
            metadata={
                "type": "compaction",
                "covers_to_seq": 0,
                "retainedTail": [
                    {"role": "user", "content": "what is this?", "metadata": {"attachments": [image]}},
                ],
            },
        )
    )
    roomy = CompactingSessionStrategy(
        reserve_tokens=10, keep_recent_tokens=600, context_window_tokens=10_000_000
    )

    replayed = await _render(roomy, session)

    (asked,) = [m for m in replayed if m.content == "what is this?"]
    assert [a.url for a in asked.attachments or []] == [image["url"]]


def test_a_retained_turn_converts_exactly_as_the_event_it_came_from() -> None:
    # Save (`retained_turn`) and load (`chat_message_from_parts`) must agree on every field the
    # converter reads. A field the converter learns and the checkpoint does not record is
    # dropped from every replay -- the defect this file exists for -- so round-trip one turn
    # carrying everything and compare with the live conversion.
    from felix.session.types import (
        SessionEvent,
        chat_message_from_parts,
        event_to_chat_message,
        retained_turn,
    )

    event = SessionEvent(
        seq=3,
        ts=1.0,
        kind="message",
        role="assistant",
        content="looking",
        tool_calls=[{"id": "c1", "name": "read_file", "args": {"path": "a"}}],
        metadata={
            "thinking": THINKING,
            "attachments": [{"url": "https://x/img.png", "media_type": "image/png"}],
            "event_id": "not-replayed",
        },
    )

    assert chat_message_from_parts(**retained_turn(event)) == event_to_chat_message(event)
