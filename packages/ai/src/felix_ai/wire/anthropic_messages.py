"""The Anthropic messages wire format.

Carries the two things the OpenAI shape has no equivalent of: explicit `cache_control`
breakpoints, and signed thinking blocks that must be replayed verbatim across a tool-call
turn or the provider rejects the whole request.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from felix_ai.catalog import clamp_effort, entry_for
from felix_ai.types import (
    ChatMessage,
    ModelChatResult,
    StopReason,
    StreamDelta,
    TokenUsage,
    ToolCall,
    ToolSchema,
)
from felix_ai.wire.base import (
    HttpModelClient,
    map_stop,
    parse_tool_arguments,
    tool_json_schema,
)
from felix_ai.wire.transport import ModelGatewayError, post_with_retry

logger = logging.getLogger("felix_ai.wire.anthropic_messages")

# Non-streaming requests must stay under the SDK/HTTP timeout, so this is a floor that
# leaves room for a real answer rather than the previous 4096.
_DEFAULT_MAX_TOKENS = 16_000


def _effort_from_budget(budget: int) -> str:
    """Map a legacy thinking budget onto an effort level."""
    if budget < 4_096:
        return "low"
    if budget < 16_384:
        return "medium"
    if budget < 32_768:
        return "high"
    return "xhigh"


def apply_anthropic_thinking_cache(
    body: dict[str, Any], spec: Any, model: str = "", *, isolate_cache: bool = False
) -> None:
    """Attach thinking + ephemeral cache_control in the shape this model accepts.

    The previous version emitted one shape for every Claude model:
    ``thinking: {"type": "enabled", "budget_tokens": N}`` plus ``temperature: 1``. Both
    are **removed** on the current generation and return HTTP 400, so the manifest's
    thinking levels hard-failed against Opus 5, Sonnet 5, Fable 5, and Opus 4.7/4.8.
    """
    entry = entry_for(model or str(body.get("model") or ""))
    caps = entry.quirks

    # Sampling params are rejected outright on 4.6+, so drop what the caller set rather
    # than letting the request 400 on a parameter the model no longer accepts.
    if not caps.sampling:
        body.pop("temperature", None)
        body.pop("top_p", None)
        body.pop("top_k", None)

    def _clamp_output() -> None:
        # Never ask for more output than the model will grant. Applies regardless of
        # whether a model spec was supplied.
        requested = int(body.get("max_tokens") or _DEFAULT_MAX_TOKENS)
        body["max_tokens"] = min(requested, entry.max_output_tokens)

    if spec is None:
        _clamp_output()
        return

    budget = getattr(spec, "thinking_budget", None)
    if budget:
        n = int(budget)
        if caps.adaptive_thinking:
            # Depth is expressed as effort now; the budget is only a hint about how hard
            # the operator wants the model to think.
            body["thinking"] = {"type": "adaptive"}
            if caps.effort:
                body.setdefault("output_config", {})["effort"] = clamp_effort(_effort_from_budget(n), caps)
            if caps.sampling:
                body["temperature"] = 1
        elif caps.budget_tokens:
            body["thinking"] = {"type": "enabled", "budget_tokens": n}
            # Pre-4.6 requires temperature=1 when thinking is enabled.
            body["temperature"] = 1
            current = int(body.get("max_tokens") or _DEFAULT_MAX_TOKENS)
            if current <= n:
                body["max_tokens"] = n + 1024

    _clamp_output()
    if getattr(spec, "cache", False) and not isolate_cache:
        system = body.get("system")
        if isinstance(system, str) and system:
            body["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        tools = body.get("tools")
        if isinstance(tools, list) and tools:
            last = dict(tools[-1])
            last["cache_control"] = {"type": "ephemeral"}
            tools[-1] = last


_ANTHROPIC_STOP: dict[str, StopReason] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
    "pause_turn": "pause_turn",
    "refusal": "refusal",
}


def _anthropic_user_or_plain(m: ChatMessage) -> dict[str, Any]:
    """Convert a non-tool message for Anthropic, including image blocks."""
    if m.role == "user" and (m.attachments or m.content_blocks):
        blocks: list[dict[str, Any]] = []
        if m.content_blocks:
            for b in m.content_blocks:
                if b.type == "text" and b.text:
                    blocks.append({"type": "text", "text": b.text})
                elif b.url:
                    blocks.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "url",
                                "url": b.url,
                                "media_type": b.media_type or "image/png",
                            },
                        }
                    )
        else:
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for att in m.attachments or []:
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "url",
                            "url": att.url,
                            "media_type": att.media_type or "image/png",
                        },
                    }
                )
        return {"role": "user", "content": blocks or m.content}
    return {"role": m.role, "content": m.content}


def _anthropic_thinking_blocks(m: ChatMessage) -> list[dict[str, Any]]:
    """Thinking blocks to replay for an assistant turn, in the order the model emitted them.

    Extended thinking combined with tool use is stateful: the provider signs each thinking
    block, and a later turn that replays the tool call must replay the signed reasoning
    with it. Felix captured neither, so a thinking-enabled manifest lost its reasoning at
    the first tool call and every following turn was answered without it.

    A `thinking` block is only replayable with the signature that was issued for it, so an
    unsigned one is dropped rather than sent — the provider rejects the whole turn on a
    missing or unverifiable signature. `redacted_thinking` carries no readable text but
    must still be echoed back, so it travels on its opaque `data` field alone.
    """
    blocks: list[dict[str, Any]] = []
    for raw in m.thinking or []:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("type")
        if kind == "thinking" and raw.get("signature"):
            blocks.append(
                {
                    "type": "thinking",
                    "thinking": str(raw.get("thinking") or ""),
                    "signature": str(raw["signature"]),
                }
            )
        elif kind == "redacted_thinking" and raw.get("data"):
            blocks.append({"type": "redacted_thinking", "data": str(raw["data"])})
    return blocks


@dataclass
class AnthropicMessagesClient(HttpModelClient):
    """The Anthropic messages wire format, including thinking blocks and cache points."""

    def _body(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        temperature: float,
        max_tokens: int | None,
        *,
        isolate_cache: bool = False,
    ) -> dict[str, Any]:
        system = ""
        converted: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                system = (system + "\n" + m.content).strip() if system else m.content
                continue
            if m.role == "tool":
                converted.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m.tool_call_id,
                                "content": m.content,
                            }
                        ],
                    }
                )
                continue
            if m.role == "assistant" and m.tool_calls:
                # Thinking blocks come first and verbatim: with extended thinking on, the
                # provider rejects a turn that replays a tool call without the signed
                # reasoning that produced it.
                blocks: list[dict[str, Any]] = _anthropic_thinking_blocks(m)
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for tc in m.tool_calls:
                    blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.args})
                converted.append({"role": "assistant", "content": blocks})
                continue
            converted.append(_anthropic_user_or_plain(m))

        body: dict[str, Any] = {
            "model": self.route.model,
            "messages": converted,
            "temperature": temperature,
            "max_tokens": max_tokens or _DEFAULT_MAX_TOKENS,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": tool_json_schema(t),
                }
                for t in tools
            ]
        apply_anthropic_thinking_cache(body, self.spec, self.route.model, isolate_cache=isolate_cache)
        return body

    async def _chat(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        temperature: float,
        max_tokens: int | None,
        *,
        isolate_cache: bool = False,
    ) -> ModelChatResult:
        body = self._body(messages, tools, temperature, max_tokens, isolate_cache=isolate_cache)
        headers = self._headers(
            {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            }
        )
        async with httpx.AsyncClient(timeout=self._timeout()) as client:
            resp = await post_with_retry(
                client,
                f"{self.base_url.rstrip('/')}/v1/messages",
                label="anthropic",
                json=body,
                headers=headers,
            )
            if resp.status_code >= 400:
                raise ModelGatewayError("anthropic", resp.status_code, resp.text)
            data = resp.json()
        content_blocks = data.get("content") or []
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        thinking_blocks: list[dict[str, Any]] = []
        for b in content_blocks:
            if b.get("type") == "text":
                text_parts.append(str(b.get("text") or ""))
            elif b.get("type") == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=str(b.get("id") or ""),
                        name=str(b.get("name") or ""),
                        args=dict(b.get("input") or {}),
                    )
                )
            elif b.get("type") in ("thinking", "redacted_thinking"):
                thinking_blocks.append(dict(b))
        usage_raw = data.get("usage") or {}
        stop = map_stop(data.get("stop_reason"), _ANTHROPIC_STOP, had_tool_calls=bool(tool_calls))
        return ModelChatResult(
            message=ChatMessage(
                role="assistant",
                content="".join(text_parts),
                tool_calls=tool_calls or None,
                thinking=thinking_blocks or None,
            ),
            stop_reason=stop,
            usage=TokenUsage(
                input=int(usage_raw.get("input_tokens") or 0),
                output=int(usage_raw.get("output_tokens") or 0),
                cache_creation=int(usage_raw.get("cache_creation_input_tokens") or 0),
                cache_read=int(usage_raw.get("cache_read_input_tokens") or 0),
            ),
        )

    async def _stream_turn(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        temperature: float,
        max_tokens: int | None,
    ) -> AsyncIterator[StreamDelta | ModelChatResult]:
        body = self._body(messages, tools, temperature, max_tokens)
        body["stream"] = True
        headers = self._headers(
            {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            }
        )

        text_parts: list[str] = []
        thinking_by_index: dict[int, dict[str, Any]] = {}
        tools_by_index: dict[int, dict[str, Any]] = {}
        usage = TokenUsage()
        raw_stop: str | None = None

        async with (
            httpx.AsyncClient(timeout=self._timeout()) as client,
            client.stream(
                "POST",
                f"{self.base_url.rstrip('/')}/v1/messages",
                json=body,
                headers=headers,
            ) as resp,
        ):
            if resp.status_code >= 400:
                raw = await resp.aread()
                raise ModelGatewayError("anthropic", resp.status_code, raw.decode("utf-8", errors="replace"))
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                kind = data.get("type")

                if kind == "message_start":
                    raw_usage = (data.get("message") or {}).get("usage") or {}
                    usage.input = int(raw_usage.get("input_tokens") or 0)
                    usage.cache_creation = int(raw_usage.get("cache_creation_input_tokens") or 0)
                    usage.cache_read = int(raw_usage.get("cache_read_input_tokens") or 0)
                elif kind == "content_block_start":
                    index = int(data.get("index") or 0)
                    block = data.get("content_block") or {}
                    btype = block.get("type")
                    if btype == "tool_use":
                        tools_by_index[index] = {
                            "id": str(block.get("id") or ""),
                            "name": str(block.get("name") or ""),
                            "json": "",
                        }
                    elif btype in ("thinking", "redacted_thinking"):
                        thinking_by_index[index] = dict(block)
                    elif btype == "text" and block.get("text"):
                        text_parts.append(str(block["text"]))
                        yield StreamDelta(kind="text", text=str(block["text"]))
                elif kind == "content_block_delta":
                    index = int(data.get("index") or 0)
                    delta = data.get("delta") or {}
                    dtype = delta.get("type")
                    if dtype == "text_delta" and delta.get("text"):
                        text_parts.append(str(delta["text"]))
                        yield StreamDelta(kind="text", text=str(delta["text"]))
                    elif dtype == "thinking_delta" and delta.get("thinking"):
                        block = thinking_by_index.setdefault(index, {"type": "thinking", "thinking": ""})
                        block["thinking"] = str(block.get("thinking") or "") + str(delta["thinking"])
                        yield StreamDelta(kind="thinking", text=str(delta["thinking"]))
                    elif dtype == "signature_delta" and delta.get("signature"):
                        block = thinking_by_index.setdefault(index, {"type": "thinking", "thinking": ""})
                        block["signature"] = str(block.get("signature") or "") + str(delta["signature"])
                    elif dtype == "input_json_delta":
                        entry = tools_by_index.setdefault(index, {"id": "", "name": "", "json": ""})
                        entry["json"] = str(entry["json"]) + str(delta.get("partial_json") or "")
                elif kind == "message_delta":
                    raw_stop = (data.get("delta") or {}).get("stop_reason") or raw_stop
                    out = (data.get("usage") or {}).get("output_tokens")
                    if out is not None:
                        usage.output = int(out)

        tool_calls = [
            ToolCall(
                id=entry["id"] or f"call_{uuid.uuid4().hex[:12]}",
                name=str(entry["name"]),
                args=parse_tool_arguments(entry["json"]),
            )
            for _, entry in sorted(tools_by_index.items())
            if entry.get("name")
        ]
        thinking = [thinking_by_index[i] for i in sorted(thinking_by_index)]
        yield ModelChatResult(
            message=ChatMessage(
                role="assistant",
                content="".join(text_parts),
                tool_calls=tool_calls or None,
                thinking=thinking or None,
            ),
            stop_reason=map_stop(raw_stop, _ANTHROPIC_STOP, had_tool_calls=bool(tool_calls)),
            usage=usage,
        )

    async def _stream(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        temperature: float,
        max_tokens: int | None,
        *,
        isolate_cache: bool = False,
    ) -> AsyncIterator[str]:
        system = ""
        converted: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                system = (system + "\n" + m.content).strip() if system else m.content
                continue
            if m.role == "tool":
                converted.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m.tool_call_id,
                                "content": m.content,
                            }
                        ],
                    }
                )
                continue
            if m.role == "assistant" and m.tool_calls:
                # Thinking blocks come first and verbatim: with extended thinking on, the
                # provider rejects a turn that replays a tool call without the signed
                # reasoning that produced it.
                blocks: list[dict[str, Any]] = _anthropic_thinking_blocks(m)
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for tc in m.tool_calls:
                    blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.args})
                converted.append({"role": "assistant", "content": blocks})
                continue
            converted.append(_anthropic_user_or_plain(m))

        body: dict[str, Any] = {
            "model": self.route.model,
            "messages": converted,
            "temperature": temperature,
            "max_tokens": max_tokens or _DEFAULT_MAX_TOKENS,
            "stream": True,
        }
        if system:
            body["system"] = system
        apply_anthropic_thinking_cache(body, self.spec, self.route.model, isolate_cache=isolate_cache)
        headers = self._headers(
            {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            }
        )
        async with (
            httpx.AsyncClient(timeout=self._timeout()) as client,
            client.stream(
                "POST",
                f"{self.base_url.rstrip('/')}/v1/messages",
                json=body,
                headers=headers,
            ) as resp,
        ):
            if resp.status_code >= 400:
                text = await resp.aread()
                raise ModelGatewayError(
                    "anthropic",
                    resp.status_code,
                    text.decode("utf-8", errors="replace"),
                )
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "content_block_delta":
                    delta = data.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        yield str(delta["text"])
                elif data.get("type") == "content_block_start":
                    block = data.get("content_block") or {}
                    if block.get("type") == "text" and block.get("text"):
                        yield str(block["text"])


__all__ = [
    "AnthropicMessagesClient",
    "apply_anthropic_thinking_cache",
]
