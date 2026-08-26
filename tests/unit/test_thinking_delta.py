"""Reasoning gets a frame of its own, and does not disturb the resume cursor.

It was always on the wire, but only inside `session_progress` — a frame whose job
is run phase, carrying model output as a passenger. chat-ui reads `phase` off that
frame and drops the rest, which is how a feature that shipped stayed invisible.

The `id:` half is the part worth pinning. Structural frames carry the thread's next
session sequence and a dropped client resumes from the last one it saw; a token-rate
frame that took a sequence number would burn cursors at token rate. `text_delta` is
excluded for exactly that reason and `thinking_delta` has to be too — a fact no type
records, so it is asserted here.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.patterns.model import ModelChatResult, StreamDelta, TokenUsage
from felix.patterns.react import _ReactAgent
from felix.patterns.types import ChatMessage, InvokeInput
from felix_api.routes._sse import PER_TOKEN_EVENTS, is_resume_point


class _ThinkingModel:
    """A provider that reasons before it answers, as an extended-thinking model does."""

    model_id = "scripted"

    def __init__(self, deltas: list[StreamDelta]) -> None:
        self._deltas = deltas
        self._served = False
        self._result = ModelChatResult(
            message=ChatMessage(role="assistant", content="Hello"),
            stop_reason="end_turn",
            usage=TokenUsage(),
        )

    async def stream_turn(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None):
        if self._served:
            yield self._result
            return
        self._served = True
        for delta in self._deltas:
            yield delta
        yield self._result

    async def chat(self, messages: list[ChatMessage], tools: list[Any], opts: Any = None):
        return self._result


def _agent(model: Any) -> _ReactAgent:
    agent = _ReactAgent(
        tools=[],
        pattern="react",
        manifest_id="test",
        manifest_version="1",
        system_prompt="s",
        model_spec=None,
        settings=None,
        recursion_limit=3,
    )
    agent._resolve_model = lambda _input: model  # type: ignore[method-assign]
    return agent


async def _drain(agent: _ReactAgent) -> list[Any]:
    return [
        e async for e in agent.stream_events(InvokeInput(messages=[ChatMessage(role="user", content="hi")]))
    ]


@pytest.mark.asyncio
async def test_reasoning_arrives_under_its_own_name() -> None:
    events = await _drain(
        _agent(
            _ThinkingModel(
                [
                    StreamDelta(kind="thinking", text="let me "),
                    StreamDelta(kind="thinking", text="work it out"),
                    StreamDelta(kind="text", text="Hello"),
                ]
            )
        )
    )
    thought = "".join(e.data["delta"] for e in events if e.event == "thinking_delta")
    assert thought == "let me work it out"


@pytest.mark.asyncio
async def test_reasoning_stays_out_of_the_answer() -> None:
    # The whole point of separating them: reasoning rendered as the reply is worse
    # than reasoning not rendered at all.
    events = await _drain(
        _agent(
            _ThinkingModel(
                [
                    StreamDelta(kind="thinking", text="hmm"),
                    StreamDelta(kind="text", text="Hello"),
                ]
            )
        )
    )
    assert "".join(e.data["delta"] for e in events if e.event == "text_delta") == "Hello"


@pytest.mark.asyncio
async def test_the_progress_envelope_still_carries_it() -> None:
    # Additive: a consumer already digging reasoning out of `session_progress`
    # keeps working, which is what makes this safe to ship without a client change.
    events = await _drain(_agent(_ThinkingModel([StreamDelta(kind="thinking", text="hmm")])))
    progress = [e.data["progress"] for e in events if e.event == "session_progress" and "progress" in e.data]
    assert {"type": "assistant_delta", "kind": "thinking", "delta": "hmm"} in progress


def test_thinking_delta_is_not_a_resume_point() -> None:
    assert not is_resume_point("thinking_delta")
    assert "thinking_delta" in PER_TOKEN_EVENTS


def test_it_is_excluded_for_the_same_reason_as_text() -> None:
    # Both arrive per token. If one is structural and the other is not, that is a
    # mistake rather than a distinction.
    assert is_resume_point("text_delta") == is_resume_point("thinking_delta")


def test_structural_frames_still_carry_a_cursor() -> None:
    # The denylist must stay short: widening it silently withholds `id:` from the
    # frames that mark the longest pauses, which is when a stream is likeliest to drop.
    for name in ("tool_start", "approval_required", "tool_request", "done"):
        assert is_resume_point(name), name
