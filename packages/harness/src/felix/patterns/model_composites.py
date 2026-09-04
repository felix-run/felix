"""Resilience composites — what a model client does when a provider lets it down.

Lifted out of `patterns/model.py`, whose subject is routing, metering and the provider
factories. Failing over to another model and escalating to a stronger one are the same
kind of decision as each other and a different kind from either of those: a policy about
what to do when an answer is unavailable or not good enough.

Both wrap a `ModelClient` and are themselves `ModelClient`s, so `build_model` composes them
around whatever `build_one_model` produced without either knowing about the other.

One property both share and neither can express in a type: they define `stream_turn`
unconditionally, so `supports_stream_turn` answers True for a composite whatever its
members do. Each therefore settles the turn itself rather than leaving a caller to notice —
`_FallbackClient` only when no member could stream it, `_EscalationClient` always, because
it needs the finished reply before it can judge confidence.

Both are also the only place that can see a turn the caller never receives, which is why
`_EscalationClient` folds the discarded primary's usage into what it returns. A caller
meters what it is handed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass, replace

from felix_ai.types import (
    ChatMessage,
    ModelChatOptions,
    ModelChatResult,
    ModelClient,
    ModelRoute,
    StreamDelta,
    TokenUsage,
    ToolSchema,
    supports_stream_turn,
)
from felix_ai.wire import ModelGatewayError

from felix.observability.metrics import record_counter


@dataclass
class _FallbackClient:
    primary: ModelClient
    fallbacks: list[ModelClient]
    model_id: str
    route: ModelRoute

    async def chat(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        opts: ModelChatOptions | None = None,
    ) -> ModelChatResult:
        chain = [self.primary, *self.fallbacks]
        last_err: Exception | None = None
        for i, client in enumerate(chain):
            try:
                result = await client.chat(messages, tools, opts)
                if i > 0:
                    record_counter(
                        "felix_model_switch",
                        {
                            "from": self.primary.model_id,
                            "to": client.model_id,
                            "reason": "provider_error",
                        },
                    )
                return result
            except Exception as exc:
                if not _is_provider_error(exc):
                    raise
                last_err = exc
                continue
        assert last_err is not None
        raise last_err

    async def stream_turn(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        opts: ModelChatOptions | None = None,
    ) -> AsyncIterator[StreamDelta | ModelChatResult]:
        """Advance to the next model on a provider error, but only before anything shipped.

        Once a delta has been yielded the caller has already rendered it, so switching
        models mid-stream would splice two different answers together. After that point
        the error propagates instead.
        """
        chain = [self.primary, *self.fallbacks]
        last_err: Exception | None = None
        for i, client in enumerate(chain):
            emitted = False
            if not supports_stream_turn(client):
                continue
            turn = client.stream_turn
            try:
                async for item in turn(messages, tools, opts):
                    emitted = True
                    yield item
                if i > 0:
                    record_counter(
                        "felix_model_switch",
                        {
                            "from": self.primary.model_id,
                            "to": client.model_id,
                            "reason": "provider_error",
                        },
                    )
                return
            except Exception as exc:
                if emitted or not _is_provider_error(exc):
                    raise
                last_err = exc
                continue
        if last_err is not None:
            raise last_err

        # Nothing in the chain could stream, and nothing failed — every member implements
        # only `chat`/`stream`. Falling through here would end the generator having yielded
        # nothing and having made no model call at all, and `supports_stream_turn` cannot
        # warn a caller off: this composite defines `stream_turn` unconditionally, so it
        # answers True for a chain of members that all answer False.
        #
        # `react` happened to survive that (`if result is None: result = await model.chat`),
        # `delegating` did not — its `return` sits inside the streaming branch, so a
        # plan_execute or parallel run with `spec.model.fallbacks` and a chat-only provider
        # synthesised an empty answer, unlogged and unmetered because no call occurred.
        # Settling it here rather than in each caller matches `_EscalationClient`, which
        # already resolves the answer with `chat()` and chunks it for display.
        result = await self.chat(messages, tools, opts)
        for item in _settled_stream(result):
            yield item

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        opts: ModelChatOptions | None = None,
    ) -> AsyncIterator[str]:
        chain = [self.primary, *self.fallbacks]
        last_err: Exception | None = None
        for i, client in enumerate(chain):
            try:
                async for chunk in client.stream(messages, tools, opts):
                    yield chunk
                if i > 0:
                    record_counter(
                        "felix_model_switch",
                        {
                            "from": self.primary.model_id,
                            "to": client.model_id,
                            "reason": "provider_error",
                        },
                    )
                return
            except Exception as exc:
                if not _is_provider_error(exc):
                    raise
                last_err = exc
                continue
        assert last_err is not None
        raise last_err


# A settled answer, chunked for a smooth SSE render. Both composites resolve turns their
# members cannot stream, and the three copies of this loop had already begun to differ —
# two yielded `StreamDelta`, one yielded `str`, and each named 48 itself. Stream and
# non-stream copies drifting is the one duplication this repo has repeatedly shipped bugs
# from; three of them inside 240 lines is where it starts.
_CHUNK_CHARS = 48


def _chunks(text: str) -> Iterator[str]:
    for i in range(0, len(text), _CHUNK_CHARS):
        yield text[i : i + _CHUNK_CHARS]


def _settled_stream(result: ModelChatResult) -> Iterator[StreamDelta | ModelChatResult]:
    """Display deltas for an answer that was resolved with `chat()`, then the result itself.

    The terminal `ModelChatResult` is what the caller meters; without it the turn is billed
    and invisible to every budget.
    """
    for chunk in _chunks(result.message.content or ""):
        yield StreamDelta(kind="text", text=chunk)
    yield result


def _with_usage_of(kept: ModelChatResult, discarded: ModelChatResult) -> ModelChatResult:
    """Fold a discarded turn's usage into the one being returned.

    Escalation makes two billed calls and returns the second. Callers meter what they get
    back — `record_usage(result, ...)` in `react` and `delegating` only ever sees the
    returned result — so the primary's tokens reached neither `ctx.limit_state` nor the
    usage table, and `limits.max_cost_usd` under-counted an escalating run by whatever the
    discarded turn cost. Measured on a plausible pair: 2,100 tokens billed, 100 metered.

    This is the "billed twice, metered once" shape the metering invariant in
    `tests/unit/test_invariants.py` cites as having already shipped twice, and the composite
    is the only place that can see both turns.

    Pricing is per-model and both turns are summed here under the *returned* model's id, so
    a two-model escalation prices the primary's tokens at the escalation target's rate. That
    is wrong in the third decimal and right about the thing that matters — a budget that
    counts every token spent. Metering each turn under its own model needs `record_usage`
    inside the composite, which would double-count against the caller that also meters.
    """
    a, b = kept.usage, discarded.usage
    if b is None:
        return kept
    if a is None:
        return replace(kept, usage=b)
    return replace(
        kept,
        usage=TokenUsage(
            input=a.input + b.input,
            output=a.output + b.output,
            cache_creation=a.cache_creation + b.cache_creation,
            cache_read=a.cache_read + b.cache_read,
        ),
    )


@dataclass
class _EscalationClient:
    primary: ModelClient
    escalate_to: ModelClient
    markers: list[str]
    min_response_chars: int
    model_id: str
    route: ModelRoute

    def _low_confidence(self, text: str) -> bool:
        lower = text.lower()
        if len(text.strip()) < self.min_response_chars:
            return True
        return any(m.lower() in lower for m in self.markers)

    async def chat(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        opts: ModelChatOptions | None = None,
    ) -> ModelChatResult:
        result = await self.primary.chat(messages, tools, opts)
        if result.message.tool_calls or not self._low_confidence(result.message.content):
            return result
        record_counter(
            "felix_model_switch",
            {
                "from": self.primary.model_id,
                "to": self.escalate_to.model_id,
                "reason": "low_confidence",
            },
        )
        escalated = await self.escalate_to.chat(messages, tools, opts)
        return _with_usage_of(escalated, result)

    async def stream_turn(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        opts: ModelChatOptions | None = None,
    ) -> AsyncIterator[StreamDelta | ModelChatResult]:
        """Escalation needs the finished reply to judge confidence, so it cannot stream.

        The answer is settled first and then chunked for a smooth SSE render. That is one
        model call, not two, so the metering and divergence problems do not apply — only
        the time-to-first-token, which escalation trades away by design.
        """
        result = await self.chat(messages, tools, opts)
        for item in _settled_stream(result):
            yield item

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        opts: ModelChatOptions | None = None,
    ) -> AsyncIterator[str]:
        # Confidence check needs the full reply; stream escalate path when needed.
        result = await self.chat(messages, tools, opts)
        for chunk in _chunks(result.message.content or ""):
            yield chunk


def _is_provider_error(err: object) -> bool:
    if isinstance(err, ModelGatewayError):
        return err.status >= 500 or err.status == 429
    status = getattr(err, "status", None) or getattr(err, "status_code", None)
    return bool(isinstance(status, int) and (status >= 500 or status == 429))
