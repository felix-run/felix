"""What every wire format shares: the transport dataclass and the neutral parsers.

`HttpModelClient` owns only what does not differ between wire formats — the connection
fields, the timeout policy, and resolving `ModelChatOptions` against `spec` defaults once so
the three entry points cannot disagree about what temperature or token ceiling a turn ran
with. The subclasses hold everything wire-specific.

Public, unlike the `_`-prefixed original: re-deriving SSE parsing, tool-argument repair and
stop-reason mapping is most of the work of writing a provider, and getting usage wrong
there fails open on `limits.max_cost_usd`.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from felix_ai.types import (
    ChatMessage,
    ModelChatOptions,
    ModelChatResult,
    ModelConfig,
    ModelRoute,
    StopReason,
    StreamDelta,
    ToolSchema,
)
from felix_ai.wire.transport import DEFAULT_CONNECT_TIMEOUT_S

logger = logging.getLogger("felix_ai.wire.base")


def map_stop(raw: Any, table: dict[str, StopReason], *, had_tool_calls: bool) -> StopReason:
    """Translate a provider stop reason, falling back to the old inference."""
    key = str(raw or "").strip().lower()
    mapped = table.get(key)
    if mapped is not None:
        return mapped
    if key:
        logger.debug("unrecognised stop_reason %r from provider", key)
        return "unknown"
    # Provider omitted it — preserve the previous behaviour rather than guess.
    return "tool_use" if had_tool_calls else "end_turn"


def parse_tool_arguments(raw: str) -> dict[str, Any]:
    """Parse tool-call arguments accumulated from a stream, repairing what is repairable.

    Arguments arrive as JSON fragments concatenated across many events. Models routinely
    emit raw control characters inside string literals and invalid backslash escapes,
    which are not legal JSON, so a strict parse throws away an otherwise complete call.
    Repair those two, then try once more.

    A fragment that is still unparseable yields `{}` rather than raising: the call is
    surfaced to the loop, where a tool invoked with the wrong arguments is refused by
    schema validation, which is a better failure than an exception that loses the turn.
    Genuinely truncated calls are caught earlier, by the `max_tokens` quarantine.
    """
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            parsed = json.loads(_repair_json(text))
        except json.JSONDecodeError:
            logger.warning("unparseable streamed tool arguments (%d chars)", len(text))
            return {}
    return parsed if isinstance(parsed, dict) else {}


def _repair_json(text: str) -> str:
    """Escape raw control characters and doubtful backslashes inside JSON string literals."""
    out: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if escaped:
            # A backslash that does not begin a legal escape is itself literal data.
            out.append(ch if ch in '"\\/bfnrtu' else "\\" + ch)
            escaped = False
            continue
        if ch == "\\" and in_string:
            escaped = True
            out.append(ch)
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            continue
        if in_string and ch in "\n\r\t":
            out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}[ch])
            continue
        if in_string and ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
            continue
        out.append(ch)
    return "".join(out)


def tool_json_schema(tool: ToolSchema) -> dict[str, Any]:
    if tool.raw_input_schema is not None:
        return tool.raw_input_schema
    schema = tool.args_schema
    if schema is None:
        return {"type": "object", "properties": {}}
    if isinstance(schema, dict):
        return schema
    if hasattr(schema, "model_json_schema"):
        return schema.model_json_schema()
    return {"type": "object", "properties": {}}


@dataclass
class HttpModelClient(ABC):
    """Shared transport for the HTTP providers: OpenAI-style and Anthropic.

    This was one class carrying a `style: Literal["openai", "anthropic"]` flag and
    branching on it in `chat`, `stream_turn` and `stream`, with eight provider-specific
    methods behind them — while the `ModelProvider` Protocol and the provider registry
    right above it already described exactly the seam that flag was standing in for.
    All three factories returned this one class.

    The subclasses now hold everything wire-specific. This base owns only what does not
    differ: the connection fields, and resolving `ModelChatOptions` against `spec`
    defaults once so the three entry points cannot disagree about what temperature or
    token ceiling a turn ran with.
    """

    model_id: str
    route: ModelRoute
    settings: ModelConfig
    spec: Any
    base_url: str
    api_key: str

    def _timeout(self) -> httpx.Timeout:
        """Request timeout for this client, from the `Settings` it was constructed with.

        Read off the instance rather than the process-global settings: `build_model` exists
        so a caller can pass its own `Settings`, and every other field here honours that.
        On a streaming call the read bound applies between chunks, not to the whole turn.
        """
        return httpx.Timeout(
            float(self.settings.model_timeout_seconds),
            connect=DEFAULT_CONNECT_TIMEOUT_S,
        )

    def _resolve(self, opts: ModelChatOptions | None) -> tuple[ModelChatOptions, float, int | None]:
        """Options for this turn, with `spec` supplying whatever the caller left unset."""
        opts = opts or ModelChatOptions()
        temperature = (
            opts.temperature if opts.temperature is not None else getattr(self.spec, "temperature", 0)
        )
        max_tokens = opts.max_tokens or getattr(self.spec, "max_tokens", None)
        return opts, temperature, max_tokens

    async def chat(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        opts: ModelChatOptions | None = None,
    ) -> ModelChatResult:
        opts, temperature, max_tokens = self._resolve(opts)
        return await self._chat(messages, tools, temperature, max_tokens, isolate_cache=opts.isolate_cache)

    async def stream_turn(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        opts: ModelChatOptions | None = None,
    ) -> AsyncIterator[StreamDelta | ModelChatResult]:
        """Stream one turn in a single request, ending with the authoritative result.

        The agent loop used to stream a turn for display and then call `chat()` to get
        the real answer — two full inferences for one turn. That billed the input twice,
        metered only the second (so `limits.max_cost_usd` and the token budgets counted
        roughly half of what a streaming run actually spent), and sampled the answer
        twice, so the text a user watched arrive could differ from the text that was
        saved. It also meant the streamed request carried no tools at all.

        One request now yields display deltas and finishes by yielding the
        `ModelChatResult` — same message, tool calls, stop reason and usage that `chat()`
        would have returned. Callers distinguish the final item by type.
        """
        _, temperature, max_tokens = self._resolve(opts)
        async for item in self._stream_turn(messages, tools, temperature, max_tokens):
            yield item

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        opts: ModelChatOptions | None = None,
    ) -> AsyncIterator[str]:
        opts, temperature, max_tokens = self._resolve(opts)
        # `isolate_cache` was dropped here, so a side request on the text-stream path still
        # wrote the conversation's prompt-cache key — churning the cached prefix the next
        # real turn would have hit, which is the exact thing the option exists to prevent.
        async for chunk in self._stream(
            messages, tools, temperature, max_tokens, isolate_cache=opts.isolate_cache
        ):
            yield chunk

    # --- what a wire format must provide -------------------------------------------
    #
    # Abstract, so a wire format missing one fails at construction rather than at the
    # first call that happens to need it.

    @abstractmethod
    def _body(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        temperature: float,
        max_tokens: int | None,
        *,
        isolate_cache: bool = False,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def _chat(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        temperature: float,
        max_tokens: int | None,
        *,
        isolate_cache: bool = False,
    ) -> ModelChatResult:
        raise NotImplementedError

    @abstractmethod
    def _stream_turn(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        temperature: float,
        max_tokens: int | None,
    ) -> AsyncIterator[StreamDelta | ModelChatResult]:
        raise NotImplementedError

    @abstractmethod
    def _stream(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        temperature: float,
        max_tokens: int | None,
        *,
        isolate_cache: bool = False,
    ) -> AsyncIterator[str]:
        raise NotImplementedError


__all__ = [
    "HttpModelClient",
    "map_stop",
    "parse_tool_arguments",
    "tool_json_schema",
]
