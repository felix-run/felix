"""Model client — chat/stream with fallbacks and confidence escalation."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

import httpx

from felix.config import DEFAULT_MODEL_ROUTES, Settings, get_settings
from felix.context import try_get_context
from felix.observability.metrics import record_counter
from felix.patterns.capabilities import capabilities_for, clamp_effort
from felix.patterns.model_registry import (
    get_model_provider,
    list_model_providers,
    register_model_provider,
)
from felix.patterns.types import ChatMessage, ToolCall
from felix.tools.types import Tool

logger = logging.getLogger("felix.patterns.model")

# `refusal` and `pause_turn` are real API outcomes. They were absent here, and the
# providers' stop_reason was never read at all — it was synthesised from whether the
# turn contained tool calls — so a truncated answer, a safety refusal, and a paused
# server-tool turn all presented to the agent loop as a normal completion.
StopReason = Literal[
    "end_turn",
    "tool_use",
    "max_tokens",
    "stop_sequence",
    "pause_turn",
    "refusal",
    "unknown",
]


@dataclass(slots=True)
class ModelRoute:
    provider: str
    model: str


@dataclass(slots=True)
class TokenUsage:
    input: int = 0
    output: int = 0
    cache_creation: int = 0
    cache_read: int = 0


@dataclass(slots=True)
class ModelChatOptions:
    temperature: float | None = None
    max_tokens: int | None = None
    signal: Any | None = None


@dataclass(slots=True)
class ModelChatResult:
    message: ChatMessage
    stop_reason: StopReason = "end_turn"
    usage: TokenUsage | None = None


@runtime_checkable
class ModelProvider(Protocol):
    """Protocol for provider-backed model clients."""

    model_id: str
    route: ModelRoute

    async def chat(
        self,
        messages: list[ChatMessage],
        tools: list[Tool],
        opts: ModelChatOptions | None = None,
    ) -> ModelChatResult: ...

    def stream(
        self,
        messages: list[ChatMessage],
        tools: list[Tool],
        opts: ModelChatOptions | None = None,
    ) -> AsyncIterator[str]: ...


# Alias used throughout patterns
ModelClient = ModelProvider


class ModelGatewayError(Exception):
    """An upstream model provider returned an error response.

    ``str(exc)`` is relayed to API clients verbatim by both `/chat` and
    `/v1/chat/completions`, so the provider's response body is deliberately kept **out**
    of the message: it can carry provider request ids, organization identifiers, quota
    and billing detail, and echoed request content. The body is retained on ``.body``
    for server-side logging only.
    """

    def __init__(self, label: str, status: int, body: str) -> None:
        super().__init__(f"{label} provider returned HTTP {status}")
        self.status = status
        self.label = label
        self.body = (body or "")[:2000]
        self.name = "ModelGatewayError"


def parse_model_routes(settings: Settings | None = None) -> dict[str, ModelRoute]:
    settings = settings or get_settings()
    routes = {k: ModelRoute(**v) for k, v in DEFAULT_MODEL_ROUTES.items()}
    if settings.model_routes.strip():
        try:
            override = json.loads(settings.model_routes)
            for k, v in override.items():
                routes[k] = ModelRoute(provider=v["provider"], model=v["model"])
        except json.JSONDecodeError, KeyError, TypeError:
            logger.warning("invalid FELIX_MODEL_ROUTES; using defaults")
    return routes


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
) -> None:
    """Attach thinking budget + prompt cache hints to an OpenAI-style request."""
    budget = getattr(spec, "thinking_budget", None) if spec is not None else None
    if budget:
        n = int(budget)
        body["reasoning_effort"] = reasoning_effort_from_budget(n)
        # LiteLLM / Anthropic-via-OpenAI also honor this block.
        body["thinking"] = {"type": "enabled", "budget_tokens": n}
    if spec is not None and getattr(spec, "cache", False):
        key = cache_key
        if not key:
            key = "felix"
            try:
                from felix.context import try_get_context

                ctx = try_get_context()
                tid = getattr(ctx, "thread_id", None) if ctx is not None else None
                if tid:
                    key = f"felix:{tid}"
            except Exception:
                pass
        body["prompt_cache_key"] = key


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


def apply_anthropic_thinking_cache(body: dict[str, Any], spec: Any, model: str = "") -> None:
    """Attach thinking + ephemeral cache_control in the shape this model accepts.

    The previous version emitted one shape for every Claude model:
    ``thinking: {"type": "enabled", "budget_tokens": N}`` plus ``temperature: 1``. Both
    are **removed** on the current generation and return HTTP 400, so the manifest's
    thinking levels hard-failed against Opus 5, Sonnet 5, Fable 5, and Opus 4.7/4.8.
    """
    caps = capabilities_for(model or str(body.get("model") or ""))

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
        body["max_tokens"] = min(requested, caps.max_output_tokens)

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
    if getattr(spec, "cache", False):
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

# OpenAI names the same outcomes differently.
_OPENAI_STOP: dict[str, StopReason] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}


def _map_stop(raw: Any, table: dict[str, StopReason], *, had_tool_calls: bool) -> StopReason:
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


# Retried statuses: rate limiting and transient upstream failures. 4xx other than these
# will not succeed on a retry, so retrying them just burns latency.
_RETRY_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504})
MODEL_MAX_RETRIES = 2  # three attempts total
_BASE_BACKOFF_S = 0.5
_MAX_BACKOFF_S = 20.0


# A 429 has two very different causes. Transient overload clears on its own and is worth
# a retry; a spent quota or a billing problem does not clear within a request, so retrying
# only adds latency to a failure the caller is going to see anyway.
_HARD_LIMIT_MARKERS = (
    "insufficient_quota",
    "insufficient quota",
    "billing",
    "payment",
    "credit balance",
    "exceeded your current quota",
    "monthly usage limit",
    "spending limit",
    "account is not active",
)


def _is_exhausted_quota(resp: Any) -> bool:
    """True when a rate-limit response reflects a spent budget rather than backpressure."""
    try:
        body = (resp.text or "").lower()
    except Exception:
        return False
    return any(marker in body for marker in _HARD_LIMIT_MARKERS)


def _retry_after_seconds(resp: Any) -> float | None:
    """Honour the provider's own Retry-After, in seconds or as an HTTP date."""
    raw = ""
    try:
        raw = (resp.headers.get("retry-after") or "").strip()
    except Exception:
        return None
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(raw)
        if when is None:
            return None
        delta = when.timestamp() - time.time()
        return max(0.0, delta)
    except Exception:
        return None


def _backoff_delay(attempt: int, retry_after: float | None) -> float:
    """Exponential backoff with jitter, never shorter than the provider asked for."""
    if retry_after is not None:
        return min(retry_after, _MAX_BACKOFF_S)
    base = min(_BASE_BACKOFF_S * (2**attempt), _MAX_BACKOFF_S)
    # Jitter spreads retries from concurrent runs; not a security decision.
    return base + random.uniform(0, base / 2)


async def _post_with_retry(
    client: Any,
    url: str,
    *,
    label: str,
    json: dict[str, Any],
    headers: dict[str, str],
    max_retries: int = MODEL_MAX_RETRIES,
) -> Any:
    """POST, retrying rate limits and transient upstream failures.

    There was no retry anywhere in this layer: `_is_provider_error` existed but was only
    consulted by `_FallbackClient` to advance to the next *model*, and with no
    `spec.fallbacks` configured — the default in every bundled manifest — a single 429
    failed the whole run.
    """
    last: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = await client.post(url, json=json, headers=headers)
        except httpx.HTTPError as exc:
            last = exc
            if attempt >= max_retries:
                raise
            await asyncio.sleep(_backoff_delay(attempt, None))
            continue
        if resp.status_code in _RETRY_STATUSES and attempt < max_retries:
            if resp.status_code == 429 and _is_exhausted_quota(resp):
                logger.warning("%s rate limit is a spent quota, not backpressure; not retrying", label)
                record_counter("felix_model_retry_skipped", {"provider": label, "reason": "quota"})
                return resp
            delay = _backoff_delay(attempt, _retry_after_seconds(resp))
            record_counter("felix_model_retry", {"provider": label, "status": str(resp.status_code)})
            logger.warning(
                "%s returned %s; retrying in %.1fs (attempt %d/%d)",
                label,
                resp.status_code,
                delay,
                attempt + 1,
                max_retries,
            )
            await asyncio.sleep(delay)
            continue
        return resp
    raise last if last is not None else RuntimeError("unreachable")


def _openai_usage(usage_raw: dict[str, Any]) -> TokenUsage:
    # prompt_tokens already includes cached tokens.
    return TokenUsage(
        input=int(usage_raw.get("prompt_tokens") or 0),
        output=int(usage_raw.get("completion_tokens") or 0),
    )


def record_usage(result: ModelChatResult, *, manifest_id: str, model_id: str | None = None) -> None:
    if not result.usage:
        return
    labels = {"manifest_id": manifest_id, "model": model_id or "default"}
    ctx = try_get_context()
    tenant_id = "default"
    if ctx is not None:
        u = result.usage
        ctx.limit_state.tokens_input += u.input + u.cache_creation + u.cache_read
        ctx.limit_state.tokens_output += u.output
        # Accumulate spend so `limits.max_cost_usd` has something to measure.
        try:
            from felix.usage.pricing import usage_with_cost

            priced = usage_with_cost(u, model_id=model_id or "")
            ctx.limit_state.cost_usd += float((priced.get("cost") or {}).get("total") or 0.0)
        except Exception:
            logger.debug("usage pricing unavailable", exc_info=True)
        tenant_id = getattr(ctx.auth, "tenant_id", None) or "default"
        settings = ctx.settings
    else:
        settings = get_settings()
    record_counter("felix_tokens", {**labels, "kind": "input"}, result.usage.input)
    record_counter("felix_tokens", {**labels, "kind": "output"}, result.usage.output)
    try:
        from felix.usage.store import record_tokens

        record_tokens(
            settings,
            tenant_id=tenant_id,
            manifest_id=manifest_id,
            model_id=model_id or "",
            tokens_input=result.usage.input,
            tokens_output=result.usage.output,
            cache_creation=result.usage.cache_creation,
            cache_read=result.usage.cache_read,
        )
    except Exception:
        logger.debug("usage_record_failed", exc_info=True)
    try:
        from felix.plugins import get_registry

        factory = get_registry()._usage_sink_factory
        if factory is not None:
            sink = factory(settings)
            record = getattr(sink, "record", None)
            if callable(record):
                record(
                    tenant_id=tenant_id,
                    manifest_id=manifest_id,
                    model_id=model_id or "",
                    usage=result.usage,
                )
    except Exception:
        logger.debug("usage_sink_failed", exc_info=True)


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


def _tool_json_schema(tool: Tool) -> dict[str, Any]:
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


def _tools_to_openai(tools: list[Tool]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": _tool_json_schema(t),
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
class _HttpModelClient:
    model_id: str
    route: ModelRoute
    settings: Settings
    spec: Any
    base_url: str
    api_key: str
    style: Literal["openai", "anthropic"]

    async def chat(
        self,
        messages: list[ChatMessage],
        tools: list[Tool],
        opts: ModelChatOptions | None = None,
    ) -> ModelChatResult:
        opts = opts or ModelChatOptions()
        temperature = (
            opts.temperature if opts.temperature is not None else getattr(self.spec, "temperature", 0)
        )
        max_tokens = opts.max_tokens or getattr(self.spec, "max_tokens", None)

        if self.style == "anthropic":
            return await self._chat_anthropic(messages, tools, temperature, max_tokens)
        return await self._chat_openai(messages, tools, temperature, max_tokens)

    async def _chat_openai(
        self,
        messages: list[ChatMessage],
        tools: list[Tool],
        temperature: float,
        max_tokens: int | None,
    ) -> ModelChatResult:
        body: dict[str, Any] = {
            "model": self.route.model,
            "messages": _messages_to_openai(messages),
            "temperature": temperature,
        }
        if max_tokens:
            body["max_tokens"] = max_tokens
        if tools:
            body["tools"] = _tools_to_openai(tools)
        apply_openai_thinking_cache(body, self.spec)
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await _post_with_retry(
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
        stop = _map_stop(choice.get("finish_reason"), _OPENAI_STOP, had_tool_calls=bool(tool_calls))
        return ModelChatResult(
            message=ChatMessage(
                role="assistant",
                content=str(msg.get("content") or ""),
                tool_calls=tool_calls,
            ),
            stop_reason=stop,
            usage=_openai_usage(usage_raw),
        )

    async def _chat_anthropic(
        self,
        messages: list[ChatMessage],
        tools: list[Tool],
        temperature: float,
        max_tokens: int | None,
    ) -> ModelChatResult:
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
                    "input_schema": _tool_json_schema(t),
                }
                for t in tools
            ]
        apply_anthropic_thinking_cache(body, self.spec, self.route.model)
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await _post_with_retry(
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
        stop = _map_stop(data.get("stop_reason"), _ANTHROPIC_STOP, had_tool_calls=bool(tool_calls))
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

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[Tool],
        opts: ModelChatOptions | None = None,
    ) -> AsyncIterator[str]:
        opts = opts or ModelChatOptions()
        temperature = (
            opts.temperature if opts.temperature is not None else getattr(self.spec, "temperature", 0)
        )
        max_tokens = opts.max_tokens or getattr(self.spec, "max_tokens", None)
        if self.style == "anthropic":
            async for chunk in self._stream_anthropic(messages, tools, temperature, max_tokens):
                yield chunk
            return
        async for chunk in self._stream_openai(messages, tools, temperature, max_tokens):
            yield chunk

    async def _stream_openai(
        self,
        messages: list[ChatMessage],
        tools: list[Tool],
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
            httpx.AsyncClient(timeout=120.0) as client,
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

    async def _stream_anthropic(
        self,
        messages: list[ChatMessage],
        tools: list[Tool],
        temperature: float,
        max_tokens: int | None,
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
        apply_anthropic_thinking_cache(body, self.spec, self.route.model)
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        async with (
            httpx.AsyncClient(timeout=120.0) as client,
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


@dataclass
class _FallbackClient:
    primary: ModelClient
    fallbacks: list[ModelClient]
    model_id: str
    route: ModelRoute

    async def chat(
        self,
        messages: list[ChatMessage],
        tools: list[Tool],
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

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[Tool],
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
        tools: list[Tool],
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
        return await self.escalate_to.chat(messages, tools, opts)

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: list[Tool],
        opts: ModelChatOptions | None = None,
    ) -> AsyncIterator[str]:
        # Confidence check needs the full reply; stream escalate path when needed.
        result = await self.chat(messages, tools, opts)
        if result.message.content:
            # Chunk for smoother SSE when escalation used the chat path.
            text = result.message.content
            step = 48
            for i in range(0, len(text), step):
                yield text[i : i + step]


def _is_provider_error(err: object) -> bool:
    if isinstance(err, ModelGatewayError):
        return err.status >= 500 or err.status == 429
    status = getattr(err, "status", None) or getattr(err, "status_code", None)
    return bool(isinstance(status, int) and (status >= 500 or status == 429))


def _make_anthropic(model_id: str, route: ModelRoute, spec: Any, settings: Settings) -> ModelClient:
    return _HttpModelClient(
        model_id=model_id,
        route=route,
        settings=settings,
        spec=spec,
        base_url="https://api.anthropic.com",
        api_key=settings.anthropic_api_key,
        style="anthropic",
    )


def _make_openai(model_id: str, route: ModelRoute, spec: Any, settings: Settings) -> ModelClient:
    base = settings.litellm_base_url or "https://api.openai.com/v1"
    return _HttpModelClient(
        model_id=model_id,
        route=route,
        settings=settings,
        spec=spec,
        base_url=base if base.endswith("/v1") else f"{base.rstrip('/')}/v1",
        api_key=settings.openai_api_key,
        style="openai",
    )


def _make_ollama(model_id: str, route: ModelRoute, spec: Any, settings: Settings) -> ModelClient:
    base = settings.ollama_base_url.rstrip("/") + "/v1"
    return _HttpModelClient(
        model_id=model_id,
        route=route,
        settings=settings,
        spec=spec,
        base_url=base,
        api_key="ollama",
        style="openai",
    )


def register_builtin_providers() -> None:
    register_model_provider(
        "anthropic", lambda mid, route, spec, settings: _make_anthropic(mid, route, spec, settings)
    )
    register_model_provider(
        "openai", lambda mid, route, spec, settings: _make_openai(mid, route, spec, settings)
    )
    register_model_provider(
        "ollama", lambda mid, route, spec, settings: _make_ollama(mid, route, spec, settings)
    )


def build_one_model(settings: Settings, spec: Any, logical_id: str) -> ModelClient:
    routes = parse_model_routes(settings)
    route = routes.get(logical_id)
    if route is None:
        raise ValueError(f"Model '{logical_id}' is not in MODEL_ROUTES")
    factory = get_model_provider(route.provider)
    if factory is None:
        raise ValueError(
            f"Unknown model provider '{route.provider}' — registered: "
            f"{', '.join(list_model_providers()) or '(none)'}"
        )
    return factory(logical_id, route, spec, settings)


def build_model(settings: Settings | None, spec: Any) -> ModelClient:
    settings = settings or get_settings()
    if not list_model_providers():
        register_builtin_providers()
    primary_id = getattr(spec, "id", None) or settings.default_model_id
    client = build_one_model(settings, spec, primary_id)
    fallbacks_ids = list(getattr(spec, "fallbacks", None) or [])
    if fallbacks_ids:
        fallbacks = [build_one_model(settings, spec, fid) for fid in fallbacks_ids]
        client = _FallbackClient(
            primary=client,
            fallbacks=fallbacks,
            model_id=client.model_id,
            route=client.route,
        )
    esc = getattr(spec, "confidence_escalation", None)
    if esc is not None and getattr(esc, "enabled", False) and getattr(esc, "escalate_to", ""):
        escalate = build_one_model(settings, spec, esc.escalate_to)
        client = _EscalationClient(
            primary=client,
            escalate_to=escalate,
            markers=list(esc.low_confidence_markers),
            min_response_chars=esc.min_response_chars,
            model_id=client.model_id,
            route=client.route,
        )
    return client


__all__ = [
    "ModelChatOptions",
    "ModelChatResult",
    "ModelClient",
    "ModelGatewayError",
    "ModelProvider",
    "ModelRoute",
    "TokenUsage",
    "apply_anthropic_thinking_cache",
    "apply_openai_thinking_cache",
    "build_model",
    "parse_model_routes",
    "reasoning_effort_from_budget",
    "record_usage",
    "register_builtin_providers",
]
