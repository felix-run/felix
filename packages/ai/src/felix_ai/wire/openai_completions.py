"""The OpenAI chat-completions wire format.

Also Ollama, LiteLLM, vLLM, and every hosted endpoint that speaks this shape — which is
most of them, and is why a new provider is usually a base URL rather than a new module.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from felix_ai.context import resolve_cache_key
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

logger = logging.getLogger("felix_ai.wire.openai_completions")


def reasoning_effort_from_budget(budget: int) -> str:
    """Map Anthropic-style budget tokens onto OpenAI ``reasoning_effort``."""
    if budget < 4096:
        return "low"
    if budget < 16384:
        return "medium"
    return "high"


def apply_openai_thinking_cache(
    body: dict[str, Any],
    spec: Any,
    *,
    cache_key: str | None = None,
    isolate_cache: bool = False,
) -> None:
    """Attach thinking budget + prompt cache hints to an OpenAI-style request."""
    budget = getattr(spec, "thinking_budget", None) if spec is not None else None
    if budget:
        n = int(budget)
        body["reasoning_effort"] = reasoning_effort_from_budget(n)
        # LiteLLM / Anthropic-via-OpenAI also honor this block.
        body["thinking"] = {"type": "enabled", "budget_tokens": n}
    if isolate_cache:
        return
    if spec is not None and getattr(spec, "cache", False):
        body["prompt_cache_key"] = cache_key or resolve_cache_key()


# OpenAI names the same outcomes differently.
_OPENAI_STOP: dict[str, StopReason] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}


def _openai_usage(usage_raw: dict[str, Any]) -> TokenUsage:
    # prompt_tokens already includes cached tokens.
    return TokenUsage(
        input=int(usage_raw.get("prompt_tokens") or 0),
        output=int(usage_raw.get("completion_tokens") or 0),
    )


def _messages_to_openai(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        content: Any = m.content
        if m.attachments or (m.content_blocks and any(b.type != "text" for b in m.content_blocks)):
            parts: list[dict[str, Any]] = []
            if m.content_blocks:
                for b in m.content_blocks:
                    if b.type == "text" and b.text:
                        parts.append({"type": "text", "text": b.text})
                    elif b.type in {"image_url", "image"} and b.url:
                        img: dict[str, Any] = {"url": b.url}
                        if b.detail:
                            img["detail"] = b.detail
                        parts.append({"type": "image_url", "image_url": img})
            else:
                if m.content:
                    parts.append({"type": "text", "text": m.content})
                for att in m.attachments or []:
                    img = {"url": att.url}
                    if att.detail:
                        img["detail"] = att.detail
                    parts.append({"type": "image_url", "image_url": img})
            content = parts or m.content
        item: dict[str, Any] = {"role": m.role, "content": content}
        if m.tool_call_id:
            item["tool_call_id"] = m.tool_call_id
        if m.name:
            item["name"] = m.name
        if m.tool_calls:
            item["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.args)},
                }
                for tc in m.tool_calls
            ]
        out.append(item)
    return out


def _tools_to_openai(tools: Sequence[ToolSchema]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": tool_json_schema(t),
            },
        }
        for t in tools
    ]


def _parse_openai_tool_calls(raw: list[dict[str, Any]] | None) -> list[ToolCall] | None:
    if not raw:
        return None
    calls: list[ToolCall] = []
    for tc in raw:
        fn = tc.get("function") or {}
        args_raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
        except json.JSONDecodeError:
            args = {"_raw": args_raw}
        calls.append(ToolCall(id=str(tc.get("id") or ""), name=str(fn.get("name") or ""), args=args))
    return calls


@dataclass
class OpenAICompletionsClient(HttpModelClient):
    """The OpenAI chat-completions wire format — also Ollama and any LiteLLM gateway."""

    def _body(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        temperature: float,
        max_tokens: int | None,
        *,
        isolate_cache: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.route.model,
            "messages": _messages_to_openai(messages),
            "temperature": temperature,
        }
        if max_tokens:
            body["max_tokens"] = max_tokens
        if tools:
            body["tools"] = _tools_to_openai(tools)
        apply_openai_thinking_cache(body, self.spec, isolate_cache=isolate_cache)
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
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self._timeout()) as client:
            resp = await post_with_retry(
                client,
                f"{self.base_url.rstrip('/')}/chat/completions",
                label="openai",
                json=body,
                headers=headers,
            )
            if resp.status_code >= 400:
                raise ModelGatewayError("openai", resp.status_code, resp.text)
            data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage_raw = data.get("usage") or {}
        tool_calls = _parse_openai_tool_calls(msg.get("tool_calls"))
        stop = map_stop(choice.get("finish_reason"), _OPENAI_STOP, had_tool_calls=bool(tool_calls))
        return ModelChatResult(
            message=ChatMessage(
                role="assistant",
                content=str(msg.get("content") or ""),
                tool_calls=tool_calls,
            ),
            stop_reason=stop,
            usage=_openai_usage(usage_raw),
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
        # Usage is omitted from a streamed response unless it is asked for, and without
        # it a streaming turn would meter as zero tokens.
        body["stream_options"] = {"include_usage": True}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

        text_parts: list[str] = []
        tools_by_index: dict[int, dict[str, Any]] = {}
        usage = TokenUsage()
        raw_stop: str | None = None

        async with (
            httpx.AsyncClient(timeout=self._timeout()) as client,
            client.stream(
                "POST",
                f"{self.base_url.rstrip('/')}/chat/completions",
                json=body,
                headers=headers,
            ) as resp,
        ):
            if resp.status_code >= 400:
                raw = await resp.aread()
                raise ModelGatewayError("openai", resp.status_code, raw.decode("utf-8", errors="replace"))
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if line.startswith("data:"):
                    payload = line[5:].strip()
                elif line.startswith("{"):
                    payload = line.strip()
                else:
                    continue
                if payload == "[DONE]":
                    break
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                if data.get("usage"):
                    usage = _openai_usage(data["usage"])
                for choice in data.get("choices") or []:
                    raw_stop = choice.get("finish_reason") or raw_stop
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if content:
                        text_parts.append(str(content))
                        yield StreamDelta(kind="text", text=str(content))
                    for raw_call in delta.get("tool_calls") or []:
                        index = int(raw_call.get("index") or 0)
                        entry = tools_by_index.setdefault(index, {"id": "", "name": "", "json": ""})
                        if raw_call.get("id"):
                            entry["id"] = str(raw_call["id"])
                        fn = raw_call.get("function") or {}
                        if fn.get("name"):
                            entry["name"] = str(fn["name"])
                        if fn.get("arguments"):
                            entry["json"] = str(entry["json"]) + str(fn["arguments"])

        tool_calls = [
            ToolCall(
                id=entry["id"] or f"call_{uuid.uuid4().hex[:12]}",
                name=str(entry["name"]),
                args=parse_tool_arguments(entry["json"]),
            )
            for _, entry in sorted(tools_by_index.items())
            if entry.get("name")
        ]
        yield ModelChatResult(
            message=ChatMessage(
                role="assistant",
                content="".join(text_parts),
                tool_calls=tool_calls or None,
            ),
            stop_reason=map_stop(raw_stop, _OPENAI_STOP, had_tool_calls=bool(tool_calls)),
            usage=usage,
        )

    async def _stream(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        temperature: float,
        max_tokens: int | None,
    ) -> AsyncIterator[str]:
        body: dict[str, Any] = {
            "model": self.route.model,
            "messages": _messages_to_openai(messages),
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens:
            body["max_tokens"] = max_tokens
        apply_openai_thinking_cache(body, self.spec)
        # Streaming path is text-oriented; tool calls use chat() in the agent loop.
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        async with (
            httpx.AsyncClient(timeout=self._timeout()) as client,
            client.stream(
                "POST",
                f"{self.base_url.rstrip('/')}/chat/completions",
                json=body,
                headers=headers,
            ) as resp,
        ):
            if resp.status_code >= 400:
                text = await resp.aread()
                raise ModelGatewayError("openai", resp.status_code, text.decode("utf-8", errors="replace"))
            async for line in resp.aiter_lines():
                if not line:
                    continue
                if line.startswith("data:"):
                    payload = line[5:].strip()
                elif line.startswith("{"):
                    payload = line.strip()
                else:
                    continue
                if payload == "[DONE]":
                    break
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                for choice in data.get("choices") or []:
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if content:
                        yield str(content)


__all__ = [
    "OpenAICompletionsClient",
    "apply_openai_thinking_cache",
    "reasoning_effort_from_budget",
]
