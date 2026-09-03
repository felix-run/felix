"""A provider driven by a script instead of a network.

Twelve test files each build their own model double, and every one of them re-decides what
a provider owes its caller. That is how `stream_turn` came to be absent from the published
Protocol for so long: the doubles that implemented it and the doubles that did not both
looked correct in isolation.

This is the double, written once, and it is what the provider conformance contract runs
against — the arm that needs no HTTP at all, so the chain from `register_model_provider`
through `FELIX_MODEL_ROUTES` to a metered turn is exercised on every CI run.

Not registered by default: shipping a fake in the production registry invites a typo in
`FELIX_MODEL_ROUTES` to succeed silently and answer every prompt with canned text. Call
`register_scripted_provider()` to opt in, which the conformance suite does.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from felix_ai.types import (
    ChatMessage,
    ModelChatOptions,
    ModelChatResult,
    ModelRoute,
    StopReason,
    StreamDelta,
    TokenUsage,
    ToolCall,
    ToolSchema,
)


@dataclass
class ScriptedTurn:
    """One programmed reply. `usage` defaults to something non-zero on purpose.

    A double that reports `TokenUsage()` leaves the run unmetered, and every budget in
    `limits` then fails open — so a fixture whose default is zero would quietly make the
    metering assertions in the contract untestable.
    """

    content: str = "ok"
    tool_calls: list[ToolCall] | None = None
    stop_reason: StopReason = "end_turn"
    usage: TokenUsage = field(default_factory=lambda: TokenUsage(input=11, output=7))
    error: Exception | None = None


@dataclass
class ScriptedClient:
    """The published `ModelProvider` contract and nothing more.

    Deliberately implements exactly what the Protocol declares — `model_id`, `route`,
    `chat`, `stream`, `stream_turn` — so a contract assertion that passes here is an
    assertion about the contract rather than about an implementation's extras.
    """

    model_id: str
    route: ModelRoute
    script: list[ScriptedTurn] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    def _next(self) -> ScriptedTurn:
        turn = self.script.pop(0) if self.script else ScriptedTurn()
        if turn.error is not None:
            raise turn.error
        return turn

    def _result(self, turn: ScriptedTurn) -> ModelChatResult:
        return ModelChatResult(
            message=ChatMessage(
                role="assistant",
                content=turn.content,
                tool_calls=turn.tool_calls or None,
            ),
            stop_reason=turn.stop_reason,
            usage=turn.usage,
        )

    async def chat(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        opts: ModelChatOptions | None = None,
    ) -> ModelChatResult:
        self.calls.append("chat")
        return self._result(self._next())

    async def stream_turn(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        opts: ModelChatOptions | None = None,
    ) -> AsyncIterator[StreamDelta | ModelChatResult]:
        self.calls.append("stream_turn")
        turn = self._next()
        for chunk in (turn.content[i : i + 4] for i in range(0, len(turn.content), 4)):
            yield StreamDelta(kind="text", text=chunk)
        yield self._result(turn)

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        opts: ModelChatOptions | None = None,
    ) -> AsyncIterator[str]:
        self.calls.append("stream")
        turn = self._next()
        for chunk in (turn.content[i : i + 4] for i in range(0, len(turn.content), 4)):
            yield chunk


def scripted_factory(script: list[ScriptedTurn] | None = None) -> Any:
    """A provider factory over a shared script, in the registry's four-argument shape."""

    def factory(model_id: str, route: ModelRoute, spec: Any, settings: Any) -> ScriptedClient:
        return ScriptedClient(model_id=model_id, route=route, script=list(script or []))

    return factory


def register_scripted_provider(name: str = "scripted", script: list[ScriptedTurn] | None = None) -> None:
    """Opt in to the scripted provider under `name`."""
    from felix_ai.registry import register_model_provider

    register_model_provider(name, scripted_factory(script))


__all__ = [
    "ScriptedClient",
    "ScriptedTurn",
    "register_scripted_provider",
    "scripted_factory",
]
