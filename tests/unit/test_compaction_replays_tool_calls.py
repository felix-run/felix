"""A compaction checkpoint replays the turns it kept, tool calls and all.

The checkpoint records each kept turn, `tool_calls` included, and the replay rebuilt them by
hand from `role`, `content`, `tool_call_id` and `name` only. So after any compaction a kept
tool result arrived with no assistant turn calling it -- a request Anthropic refuses -- and a
signed thinking block, which extended thinking must see again, was gone. `contributor` and
`triage` both compact, and both run long tool-heavy sessions.
"""

from __future__ import annotations

from typing import Any

import pytest
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
    """Each tool message follows an assistant turn whose calls include its id. Returns how many."""
    calling: set[str] = set()
    seen = 0
    for m in messages:
        if m.role == "assistant":
            calling = {tc.id for tc in (m.tool_calls or [])}
        elif m.role == "tool":
            seen += 1
            assert m.tool_call_id in calling, (
                f"tool result {m.tool_call_id} replayed with no call answering it"
            )
    return seen


@pytest.mark.asyncio
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

    assert _assert_every_tool_result_is_answered(replayed) >= 1
    calls = [m for m in replayed if m.role == "assistant" and m.tool_calls]
    assert calls and all(m.thinking == THINKING for m in calls), "a signed thinking block was dropped"


@pytest.mark.asyncio
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
