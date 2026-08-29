"""Model client — the harness half of the model layer.

The wire formats, the catalog and the neutral types moved to `felix_ai`, which may not
import `felix`. What stays here is everything that needs the harness: resolving
`FELIX_MODEL_ROUTES` against `Settings`, metering a turn against `ctx.limit_state` and the
usage store, the fallback/escalation composites, and the factories that adapt `Settings`
into the explicit configuration a wire client takes.

Every public name this module used to export is re-exported below, so existing imports of
`felix.patterns.model` keep working.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

from felix_ai.types import (
    ChatMessage,
    ModelChatOptions,
    ModelChatResult,
    ModelClient,
    ModelConfig,
    ModelProvider,
    ModelRoute,
    StopReason,
    StreamDelta,
    TokenUsage,
    ToolCall,
    ToolSchema,
)
from felix_ai.wire import (
    MODEL_MAX_RETRIES,
    AnthropicMessagesClient,
    HttpModelClient,
    ModelGatewayError,
    OpenAICompletionsClient,
    apply_anthropic_thinking_cache,
    apply_openai_thinking_cache,
    post_with_retry,
    reasoning_effort_from_budget,
)

from felix.config import DEFAULT_MODEL_ROUTES, Settings, get_settings
from felix.context import try_get_context
from felix.observability.metrics import record_counter
from felix.patterns.model_registry import (
    get_model_provider,
    list_model_providers,
    register_model_provider,
)

logger = logging.getLogger("felix.patterns.model")


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

        factory = get_registry().usage_sink_factory()
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
            turn = getattr(client, "stream_turn", None)
            if turn is None:
                continue
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
        return await self.escalate_to.chat(messages, tools, opts)

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
        text = result.message.content or ""
        step = 48
        for i in range(0, len(text), step):
            yield StreamDelta(kind="text", text=text[i : i + step])
        yield result

    async def stream(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
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
    return AnthropicMessagesClient(
        model_id=model_id,
        route=route,
        settings=settings,
        spec=spec,
        base_url="https://api.anthropic.com",
        api_key=settings.anthropic_api_key,
    )


def _make_openai(model_id: str, route: ModelRoute, spec: Any, settings: Settings) -> ModelClient:
    base = settings.litellm_base_url or "https://api.openai.com/v1"
    return OpenAICompletionsClient(
        model_id=model_id,
        route=route,
        settings=settings,
        spec=spec,
        base_url=base if base.endswith("/v1") else f"{base.rstrip('/')}/v1",
        api_key=settings.openai_api_key,
    )


def _make_ollama(model_id: str, route: ModelRoute, spec: Any, settings: Settings) -> ModelClient:
    base = settings.ollama_base_url.rstrip("/") + "/v1"
    return OpenAICompletionsClient(
        model_id=model_id,
        route=route,
        settings=settings,
        spec=spec,
        base_url=base,
        api_key="ollama",
    )


def register_builtin_providers() -> None:
    register_model_provider("anthropic", _make_anthropic)
    register_model_provider("openai", _make_openai)
    register_model_provider("ollama", _make_ollama)


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


# Transitional aliases for the names that moved to `felix_ai`. Kept so the ~40 call sites
# across core, the API and the tests did not have to change in the same commit as the move.
_HttpModelClient = HttpModelClient
_OpenAIClient = OpenAICompletionsClient
_AnthropicClient = AnthropicMessagesClient
_post_with_retry = post_with_retry


__all__ = [
    "MODEL_MAX_RETRIES",
    "AnthropicMessagesClient",
    "ChatMessage",
    "HttpModelClient",
    "ModelChatOptions",
    "ModelChatResult",
    "ModelClient",
    "ModelConfig",
    "ModelGatewayError",
    "ModelProvider",
    "ModelRoute",
    "OpenAICompletionsClient",
    "StopReason",
    "StreamDelta",
    "TokenUsage",
    "ToolCall",
    "ToolSchema",
    "apply_anthropic_thinking_cache",
    "apply_openai_thinking_cache",
    "build_model",
    "build_one_model",
    "parse_model_routes",
    "post_with_retry",
    "reasoning_effort_from_budget",
    "record_usage",
    "register_builtin_providers",
]
