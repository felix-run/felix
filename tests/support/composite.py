"""Driving a composite pattern (`patterns/delegating.py`) with recording fakes.

The calls a composite makes itself -- the router's classifier, reflect's verifier, the planner,
the synthesis -- go to a `ScriptedModel` patched in for `delegating._model_for` or
`delegating.build_model`; its children are `RecordingChild`s. Both record what they were sent,
so a test asserts on the prompts and options a pattern built.
"""

from __future__ import annotations

from typing import Any

from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.patterns.model import ModelChatResult, StreamDelta, TokenUsage
from felix.patterns.types import ChatMessage, Event, InvokeInput, InvokeOutput

from tests.support.factories import make_settings

THREAD = "default:composite"


class ScriptedModel:
    """Answers each call with the next of `replies`, the last one repeating; records every call's
    messages in `seen` and its options in `opts`."""

    model_id = "composite-fake"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.seen: list[list[ChatMessage]] = []
        self.opts: list[Any] = []

    def _next(self, messages: list[ChatMessage], opts: Any) -> ModelChatResult:
        self.seen.append(list(messages))
        self.opts.append(opts)
        text = self.replies[min(len(self.seen), len(self.replies)) - 1]
        return ModelChatResult(message=ChatMessage(role="assistant", content=text), usage=TokenUsage())

    async def chat(self, messages: list[ChatMessage], tools: list, opts: Any = None) -> ModelChatResult:
        return self._next(messages, opts)


class StreamingScriptedModel(ScriptedModel):
    async def stream_turn(self, messages: list[ChatMessage], tools: list, opts: Any = None) -> Any:
        result = self._next(messages, opts)
        yield StreamDelta(kind="text", text=result.message.content)
        yield result


class RecordingChild:
    """A sub-agent that answers `text` and records the last user message of every turn."""

    pattern = "react"
    manifest_id = "child"
    manifest_version = "1"

    def __init__(self, text: str) -> None:
        self.text = text
        self.asked: list[str] = []

    def _out(self, input: InvokeInput) -> InvokeOutput:
        self.asked.append(str(input.messages[-1].content))
        msg = ChatMessage(role="assistant", content=self.text)
        return InvokeOutput(messages=[*input.messages, msg], final=msg)

    async def invoke(self, input: InvokeInput) -> InvokeOutput:
        return self._out(input)

    async def stream_events(self, input: InvokeInput) -> Any:
        out = self._out(input)
        yield Event(event="on_chain_end", data={"output": out})
        yield Event(event="done", data={"final": out.final.model_dump()})


def delegating_agent(pattern: str, **kw: Any) -> Any:
    from felix.patterns import delegating

    return delegating._DelegatingAgent(
        tools=[], pattern=pattern, manifest_id="composite", manifest_version="1", **kw
    )


async def run_composite(agent: Any, *, streaming: bool = False) -> InvokeOutput | None:
    """One turn of "the request" on `THREAD`, invoked or streamed, inside a request context."""
    ctx = RequestContext(settings=make_settings(), auth=AuthContext(tenant_id="default"))
    input = InvokeInput(messages=[ChatMessage(role="user", content="the request")], thread_id=THREAD)
    async with async_run_with_context(ctx):
        if not streaming:
            return await agent.invoke(input)
        out = None
        async for ev in agent.stream_events(input):
            if ev.event == "on_chain_end":
                out = ev.data.get("output")
        return out
