"""Streaming is a capability some providers have, and the type system should say so.

`stream_turn` has been in three places on this Protocol. Absent entirely, so a third-party
provider could implement everything published and still land in the unmetered path with
nothing to say why. Then required on `ModelProvider`, which claimed every provider must
stream — while the scripted provider, the wire clients, four call sites and the traced
wrapper all treated it as optional, and a type checker reading that saw a wrapper hiding a
mandatory member.

`StreamingModelProvider` says the true thing, and `supports_stream_turn` is the one way to
ask. That matters beyond tidiness: a wrapper that answers to the name pushes a
non-streaming provider into the streamed path, where `record_usage` is never reached and
the turn escapes `max_cost_usd` and both token limits.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix_ai.types import (
    ChatMessage,
    ModelChatResult,
    ModelProvider,
    ModelRoute,
    StreamingModelProvider,
    supports_stream_turn,
)

ROUTE = ModelRoute(provider="anthropic", model="claude-sonnet-4-6")


class _NonStreaming:
    """The published minimum: chat + stream, no streamed metering."""

    model_id = "sonnet"
    route = ROUTE

    async def chat(self, messages: Any, tools: Any, opts: Any = None) -> ModelChatResult:
        return ModelChatResult(message=ChatMessage(role="assistant", content="hi"))

    async def stream(self, messages: Any, tools: Any, opts: Any = None) -> Any:
        yield "hi"


class _Streaming(_NonStreaming):
    async def stream_turn(self, messages: Any, tools: Any, opts: Any = None) -> Any:
        yield ModelChatResult(message=ChatMessage(role="assistant", content="hi"))


def test_a_non_streaming_provider_satisfies_the_base_contract() -> None:
    """The regression this split fixes.

    While `stream_turn` was a required member of a `runtime_checkable` `ModelProvider`, a
    provider implementing the whole documented contract failed `isinstance` against it —
    the Protocol contradicted its own docs.
    """
    assert isinstance(_NonStreaming(), ModelProvider)
    assert not isinstance(_NonStreaming(), StreamingModelProvider)


def test_a_streaming_provider_satisfies_both() -> None:
    assert isinstance(_Streaming(), ModelProvider)
    assert isinstance(_Streaming(), StreamingModelProvider)


@pytest.mark.parametrize(
    ("client", "expected"),
    [(_NonStreaming(), False), (_Streaming(), True), (object(), False)],
)
def test_the_predicate_answers_for_each_shape(client: object, expected: bool) -> None:
    assert supports_stream_turn(client) is expected


def test_an_attribute_that_is_not_callable_does_not_count() -> None:
    """A forwarding wrapper can answer to the name without implementing it."""

    class _Liar(_NonStreaming):
        stream_turn = "not callable"

    assert supports_stream_turn(_Liar()) is False


def test_nothing_probes_stream_turn_by_hand_any_more() -> None:
    """Four sites hand-rolled `getattr(model, "stream_turn", None)`.

    One predicate means "does this provider stream" has a single answer, and the narrowing
    is a type the checker follows rather than a convention each caller re-implements.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    # Only the predicate's own module is exempt. Matching on the basename skipped five
    # different `types.py` files across the workspace.
    predicate_module = (root / "packages/ai/src/felix_ai/types.py").resolve()
    offenders: list[str] = []
    for path in (root / "packages").rglob("*.py"):
        if path.resolve() == predicate_module:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id != "getattr" or len(node.args) < 2:
                continue
            arg = node.args[1]
            if isinstance(arg, ast.Constant) and arg.value == "stream_turn":
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not offenders, f"hand-rolled stream_turn probes: {offenders} — use supports_stream_turn"


@pytest.mark.asyncio
async def test_a_fallback_chain_that_cannot_stream_still_answers() -> None:
    """The composite is the one wrapper `supports_stream_turn` cannot judge.

    `_FallbackClient` defines `stream_turn` unconditionally, so the predicate answers True
    for a chain whose every member answers False. Before this was settled inside the
    composite, such a chain yielded nothing and made no call — and `delegating`'s streaming
    branch returns without reaching its `chat()` path, so a `plan_execute` or `parallel`
    run with `spec.model.fallbacks` and a chat-only provider produced an empty answer with
    no log line and no metering, because no inference had happened.
    """
    from felix.patterns.model_composites import _FallbackClient
    from felix_ai.types import StreamDelta

    leaf = _NonStreaming()
    calls: list[str] = []

    async def _chat(messages: Any, tools: Any, opts: Any = None) -> ModelChatResult:
        calls.append("chat")
        return ModelChatResult(message=ChatMessage(role="assistant", content="the answer"))

    leaf.chat = _chat  # type: ignore[method-assign]
    chain = _FallbackClient(primary=leaf, fallbacks=[], model_id=leaf.model_id, route=ROUTE)

    assert supports_stream_turn(chain), "the composite always claims the capability"
    assert not supports_stream_turn(leaf), "while its only member does not have it"

    emitted = [item async for item in chain.stream_turn([], [])]
    text = "".join(i.text for i in emitted if isinstance(i, StreamDelta))
    assert text == "the answer", "a caller that trusts the predicate must still get an answer"
    assert any(isinstance(i, ModelChatResult) for i in emitted), "and something to meter"
    assert calls == ["chat"], "settled with exactly one inference"
