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
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from felix_ai.providers import ProviderSpec, builtin_provider_specs
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
from felix.observability.tracing import SpanContext, timed_span
from felix.patterns.model_registry import (
    get_model_provider,
    list_model_providers,
    register_model_provider,
)

logger = logging.getLogger("felix.patterns.model")


@lru_cache(maxsize=32)
def _parse_routes_cached(raw: str) -> dict[str, ModelRoute]:
    """Routes for one `FELIX_MODEL_ROUTES` string.

    Keyed on the string rather than on `Settings`, which is neither hashable nor stable —
    and the string is the whole input. `parse_model_routes` went from 3 call sites to 7 in
    this branch (context-window sizing, handoff family, cost measurability, startup
    validation, the request allowlist), so a single agent compile re-ran `json.loads` and
    rebuilt the dict several times over.
    """
    routes = {
        k: ModelRoute(provider=v["provider"], model=v["model"]) for k, v in DEFAULT_MODEL_ROUTES.items()
    }
    if raw.strip():
        try:
            override = json.loads(raw)
            for k, v in override.items():
                routes[k] = ModelRoute(provider=v["provider"], model=v["model"])
        except Exception:
            logger.warning("invalid FELIX_MODEL_ROUTES; using defaults")
    return routes


def parse_model_routes(settings: Settings | None = None) -> dict[str, ModelRoute]:
    """Logical model id -> route, with `FELIX_MODEL_ROUTES` overlaid on the defaults."""
    settings = settings or get_settings()
    # A copy per call: the cached dict is shared, and a caller that mutated it would
    # silently reconfigure routing for every other caller in the process.
    return dict(_parse_routes_cached(settings.model_routes or ""))


def wire_model_id(client: Any) -> str:
    """The provider's own model id, which is what the catalog and the price table key on.

    `client.model_id` is the *logical* route name — `fast`, `claude-sonnet`, whatever the
    operator called it in `FELIX_MODEL_ROUTES` — and feeding that to `entry_for` matched
    nothing, so every custom route fell to the catalog default. Reporting still uses the
    logical name, because that is what an operator configured and recognises.
    """
    route = getattr(client, "route", None)
    return str(getattr(route, "model", "") or getattr(client, "model_id", "") or "")


def _metered_usage(result: ModelChatResult, *, manifest_id: str, model_id: str | None) -> TokenUsage | None:
    """This turn's usage, or `None` after saying loudly that there wasn't any.

    Returns the usage rather than a bool so the caller keeps the non-`None` narrowing —
    an earlier version answered `is_unmetered()` and moved every `result.usage` access
    below it back into `TokenUsage | None`.

    Not a no-op worth passing over quietly. `record_usage` is the only feed for
    `limits.max_input_tokens`, `max_output_tokens` and `max_cost_usd`, so a turn that
    reports nothing is a turn that cannot be capped — the budgets fail *open*. Most often
    this is a provider whose streamed response omits usage: the OpenAI wire format needs
    `stream_options.include_usage`, and an implementation that forgets it makes the whole
    run free as far as limits are concerned.
    """
    usage = result.usage
    if usage and (usage.input or usage.output or usage.cache_read or usage.cache_creation):
        return usage
    logger.warning(
        "model turn reported no usage; this turn is unmetered and cannot count "
        "against limits.max_cost_usd (model=%s)",
        model_id or "default",
    )
    record_counter("felix_model_unmetered", {"manifest_id": manifest_id, "model": model_id or "default"})
    return None


def record_usage(
    result: ModelChatResult,
    *,
    manifest_id: str,
    model_id: str | None = None,
    wire_model_id: str | None = None,
) -> None:
    """Meter one turn: run budgets, Prometheus, the usage store, and the plugin sink.

    `model_id` is the logical route name and is what gets *reported*. `wire_model_id` is
    the provider's own id and is what gets *priced*, because that is what the catalog keys
    on. When they were the same argument, every custom route priced at the catalog default.
    """
    usage = _metered_usage(result, manifest_id=manifest_id, model_id=model_id)
    if usage is None:
        return
    labels = {"manifest_id": manifest_id, "model": model_id or "default"}
    ctx = try_get_context()
    tenant_id = "default"
    if ctx is not None:
        u = usage
        ctx.limit_state.tokens_input += u.input + u.cache_creation + u.cache_read
        ctx.limit_state.tokens_output += u.output
        # Accumulate spend so `limits.max_cost_usd` has something to measure.
        try:
            from felix.usage.pricing import usage_with_cost

            priced = usage_with_cost(u, model_id=wire_model_id or model_id or "")
            ctx.limit_state.cost_usd += float((priced.get("cost") or {}).get("total") or 0.0)
        except Exception:
            logger.debug("usage pricing unavailable", exc_info=True)
        tenant_id = getattr(ctx.auth, "tenant_id", None) or "default"
        settings = ctx.settings
    else:
        settings = get_settings()
    record_counter("felix_tokens", {**labels, "kind": "input"}, usage.input)
    record_counter("felix_tokens", {**labels, "kind": "output"}, usage.output)
    try:
        from felix.usage.store import record_tokens

        record_tokens(
            settings,
            tenant_id=tenant_id,
            manifest_id=manifest_id,
            model_id=model_id or "",
            tokens_input=usage.input,
            tokens_output=usage.output,
            cache_creation=usage.cache_creation,
            cache_read=usage.cache_read,
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
                    usage=usage,
                )
    except Exception:
        logger.debug("usage_sink_failed", exc_info=True)


# Prompts grow without bound; a span attribute should not. Truncation is preferable to a
# span the exporter refuses or the backend rejects.
MAX_SPAN_CONTENT_CHARS = 32_000


def _content_capture_enabled() -> bool:
    """`FELIX_OTEL_CAPTURE_CONTENT`, read from the active request's settings.

    Off by default. A tracing backend is an egress destination like any other, and span
    attributes do not pass through the governance content screening that guards tool
    output — so the prompt and the completion only leave the process when an operator has
    said so.
    """
    ctx = try_get_context()
    settings = getattr(ctx, "settings", None) if ctx is not None else None
    if settings is None:
        settings = get_settings()
    return bool(getattr(settings, "otel_capture_content", False))


def _redacted_json(value: Any) -> str | None:
    """Serialise for a span attribute, with the audit store's redaction applied first.

    `redact_json` is what the audit path already uses, so content on a span is masked to
    the same standard as content in an audit row — and to no better one. It replaces
    values Felix *knows* are secrets (those hydrated from the secrets backend) by
    substring match; it is not pattern detection. A credential a user types into a chat is
    therefore exported verbatim. That boundary is why `FELIX_OTEL_CAPTURE_CONTENT`
    defaults to off.
    """
    try:
        from felix.secrets import redact_json

        return json.dumps(redact_json(value), default=str)[:MAX_SPAN_CONTENT_CHARS]
    except Exception:
        logger.debug("span content redaction failed; dropping content", exc_info=True)
        return None


def _record_input_on_span(span: SpanContext, messages: list[ChatMessage]) -> None:
    if not _content_capture_enabled():
        return
    payload = _redacted_json([_message_dict(m) for m in messages])
    if payload is not None:
        span.set_attribute("gen_ai.input.messages", payload)


def _record_output_on_span(span: SpanContext, result: ModelChatResult) -> None:
    if not _content_capture_enabled():
        return
    payload = _redacted_json([_message_dict(result.message)])
    if payload is not None:
        span.set_attribute("gen_ai.output.messages", payload)


def _message_dict(message: Any) -> dict[str, Any]:
    """The parts of a message worth tracing, without dragging in provider internals."""
    content = getattr(message, "content", None)
    out: dict[str, Any] = {"role": getattr(message, "role", "") or "", "content": content}
    calls = getattr(message, "tool_calls", None)
    if calls:
        out["tool_calls"] = [
            {"name": getattr(c, "name", ""), "args": getattr(c, "args", None)} for c in calls
        ]
    return out


def _record_result_on_span(span: SpanContext, result: ModelChatResult, wire_model: str) -> None:
    """Attach the response half of a generation: stop reason, tokens, cost."""
    span.set_attribute("gen_ai.response.model", wire_model)
    span.set_attribute("gen_ai.response.finish_reasons", str(result.stop_reason))
    _record_output_on_span(span, result)
    usage = result.usage
    if usage is None:
        # Same failure `_metered_usage` warns about — a turn that cannot be capped.
        # Worth seeing in a trace, not only in the log.
        span.set_attribute("felix.usage.unmetered", True)
        return
    span.set_attribute("gen_ai.usage.input_tokens", usage.input)
    span.set_attribute("gen_ai.usage.output_tokens", usage.output)
    # Cache tokens under the GenAI-semconv names as well as Felix's own. Only the former
    # are read by backends, and dropping them is not cosmetic: Felix's `usage_with_cost`
    # counts cache reads and creations, so a backend computing cost from input/output
    # alone silently disagrees with Felix's own number as soon as prompt caching is on.
    span.set_attribute("gen_ai.usage.cache_creation_input_tokens", usage.cache_creation)
    span.set_attribute("gen_ai.usage.cache_read_input_tokens", usage.cache_read)
    span.set_attribute("felix.usage.cache_creation", usage.cache_creation)
    span.set_attribute("felix.usage.cache_read", usage.cache_read)
    try:
        from felix.usage.pricing import usage_with_cost

        priced = usage_with_cost(usage, model_id=wire_model)
        span.set_attribute("felix.cost_usd", float((priced.get("cost") or {}).get("total") or 0.0))
    except Exception:
        logger.debug("span pricing unavailable", exc_info=True)


@dataclass
class _TracedClient:
    """One span and one latency sample per provider call.

    Wrapped around the leaf client in `build_one_model`, which every model the harness
    builds passes through — primary, each fallback, and an escalation target alike. That
    placement is deliberate: a fallback chain that tries two providers produces two spans,
    which is what a trace should show. Wrapping the composite instead would have collapsed
    them into one and hidden the retry.

    Only `chat` and `stream_turn` are instrumented, because they are the two that report
    usage. `stream` cannot report any, so it is passed through untouched rather than
    given a span that would claim a generation with no tokens.
    """

    inner: ModelClient
    model_id: str
    route: ModelRoute

    def _span_attrs(self) -> dict[str, Any]:
        wire_model = self._wire_model()
        ctx = try_get_context()
        attrs: dict[str, Any] = {
            "gen_ai.operation.name": "chat",
            "gen_ai.system": str(getattr(self.route, "provider", "") or "unknown"),
            "gen_ai.request.model": wire_model,
            # The logical route name, which is what operators configure and what the
            # `felix_tokens` counter reports. Not the same string as the wire model.
            "felix.model.route": self.model_id,
        }
        if ctx is not None:
            manifest_id = getattr(ctx, "manifest_id", None)
            if manifest_id:
                attrs["felix.manifest.id"] = str(manifest_id)
        return attrs

    def _wire_model(self) -> str:
        return str(getattr(self.route, "model", "") or self.model_id)

    async def chat(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        opts: ModelChatOptions | None = None,
    ) -> ModelChatResult:
        async with timed_span(
            f"chat {self._wire_model()}",
            self._span_attrs(),
            metric="felix_model_call_seconds",
            labels={"model": self.model_id},
        ) as span:
            _record_input_on_span(span, messages)
            result = await self.inner.chat(messages, tools, opts)
            _record_result_on_span(span, result, self._wire_model())
            return result

    def stream(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        opts: ModelChatOptions | None = None,
    ) -> AsyncIterator[str]:
        return self.inner.stream(messages, tools, opts)

    def __getattr__(self, name: str) -> Any:
        # Providers carry extra attributes the harness reads by name (`wire_model_id`
        # walks `route`/`model_id`, patterns probe for capabilities). Forward anything
        # this wrapper does not define — but see `_TracedStreamingClient`: `stream_turn`
        # is resolved by `getattr(model, "stream_turn", None)`, so it must NOT be
        # forwarded here or every client would claim to support it.
        # `inner` guards against infinite recursion when it is unset (an instance built
        # by copy/pickle before __init__ runs); without it the forward looks itself up.
        if name in ("inner", "stream_turn"):
            raise AttributeError(name)
        return getattr(self.inner, name)


@dataclass
class _TracedStreamingClient(_TracedClient):
    """`_TracedClient` for providers that implement `stream_turn`.

    The capability is detected with `getattr(model, "stream_turn", None)`, so it has to be
    absent on the wrapper when it is absent on the client — a wrapper that always defined
    it would push every non-streaming provider into the streaming path.
    """

    async def stream_turn(
        self,
        messages: list[ChatMessage],
        tools: Sequence[ToolSchema],
        opts: ModelChatOptions | None = None,
    ) -> AsyncIterator[StreamDelta | ModelChatResult]:
        async with timed_span(
            f"chat {self._wire_model()}",
            {**self._span_attrs(), "felix.streamed": True},
            metric="felix_model_call_seconds",
            labels={"model": self.model_id},
        ) as span:
            _record_input_on_span(span, messages)
            async for item in self.inner.stream_turn(messages, tools, opts):
                # Usage rides on the terminal ModelChatResult, not on the deltas.
                if isinstance(item, ModelChatResult):
                    _record_result_on_span(span, item, self._wire_model())
                yield item


def _traced(client: ModelClient) -> ModelClient:
    cls = _TracedStreamingClient if getattr(client, "stream_turn", None) is not None else _TracedClient
    return cls(inner=client, model_id=client.model_id, route=client.route)


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


def parse_provider_options(settings: Settings | None = None) -> dict[str, dict[str, str]]:
    """`FELIX_MODEL_PROVIDER_OPTIONS` — per-provider endpoint and credential.

    The built-in providers have named `Settings` fields, but a plugin's cannot: `Settings`
    is `extra="ignore"`, so `FELIX_MYPROVIDER_API_KEY` never lands on it. Without this a
    registered third-party provider had no way to be given a key at all, which made the
    open registry a good deal less open than it looked.

    Malformed JSON degrades to no options with a warning rather than failing startup, the
    same way `parse_model_routes` treats a malformed route table.
    """
    settings = settings or get_settings()
    raw = (getattr(settings, "model_provider_options", "") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("invalid FELIX_MODEL_PROVIDER_OPTIONS; ignoring")
        return {}
    if not isinstance(parsed, dict):
        logger.warning("FELIX_MODEL_PROVIDER_OPTIONS must be an object; ignoring")
        return {}
    out: dict[str, dict[str, str]] = {}
    for name, opts in parsed.items():
        if isinstance(opts, dict):
            # `None` and booleans are dropped rather than stringified: `str(None)` is
            # `"None"`, which is truthy, so a `null` api_key suppressed the settings-field
            # fallback and the missing-credential warning and went out as `Bearer None`.
            out[str(name)] = {
                str(k): str(v) for k, v in opts.items() if v is not None and not isinstance(v, bool)
            }
    return out


# Providers already warned about a missing credential, so the notice is a startup-ish fact
# rather than per-turn noise.
_WARNED_NO_CREDENTIAL: set[str] = set()


def resolve_provider_config(spec: ProviderSpec, settings: Settings) -> tuple[str, str, dict[str, str]]:
    """The endpoint and credential for one provider, most specific source winning.

    An explicit `FELIX_MODEL_PROVIDER_OPTIONS` entry beats the provider's named `Settings`
    field, which beats the descriptor's default. That ordering is what lets an operator
    point a built-in provider somewhere else without a new setting, and lets a plugin
    provider be configured with no `Settings` field at all.
    """
    options = parse_provider_options(settings).get(spec.name, {})

    configured = options.get("base_url")
    if not configured and spec.base_url_config_key:
        configured = str(getattr(settings, spec.base_url_config_key, "") or "")
    base_url = spec.resolve_base_url(configured, options)

    api_key = options.get("api_key") or ""
    if not api_key and spec.api_key_config_key:
        api_key = str(getattr(settings, spec.api_key_config_key, "") or "")
    if not api_key and spec.api_key_literal:
        api_key = spec.api_key_literal
    if not api_key and spec.name not in _WARNED_NO_CREDENTIAL:
        # Once per provider, not once per turn. `resolve_provider_config` runs on the
        # per-request path — `build_model` from the react loop, from each sub-agent, and
        # from the inbound injection screener on every turn — so an unconditional warning
        # is unbounded log volume for a configuration that is legitimate (a local gateway
        # with no key), keyed to whatever rate a caller chooses. A guard that fires on every
        # request for a supported setup carries no signal.
        _WARNED_NO_CREDENTIAL.add(spec.name)
        logger.warning(
            "provider %r has no credential; requests will be sent unauthenticated. Set it "
            'in FELIX_MODEL_PROVIDER_OPTIONS, e.g. {"%s": {"api_key": "..."}}',
            spec.name,
            spec.name,
        )
    return base_url, api_key, spec.resolve_headers(options)


def provider_factory(spec: ProviderSpec) -> Callable[..., ModelClient]:
    """Adapt a `ProviderSpec` into the `(logical_id, route, spec, settings)` factory."""

    def factory(model_id: str, route: ModelRoute, model_spec: Any, settings: Settings) -> ModelClient:
        base_url, api_key, headers = resolve_provider_config(spec, settings)
        return spec.wire(
            model_id=model_id,
            route=route,
            settings=settings,
            spec=model_spec,
            base_url=base_url,
            api_key=api_key,
            extra_headers=headers,
        )

    factory.__name__ = f"_make_{spec.name}"
    factory.__qualname__ = factory.__name__
    return factory


def register_builtin_providers() -> None:
    """Register every built-in provider. Idempotent — registration is last-write-wins.

    This used to be guarded in `build_model` by `if not list_model_providers()`, a sentinel
    that could only be right while nothing else ever registered first: after a
    `reset_model_provider_registry()` it restored the three builtins and silently dropped
    every plugin provider, because `load_optional_plugins` had already run and would not
    run again.
    """
    for spec in builtin_provider_specs():
        register_model_provider(spec.name, provider_factory(spec))


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
    # Every model the harness builds comes through here — primary, fallbacks and
    # escalation targets alike — so this is the one place a span per provider call can be
    # added without touching each of the eight `record_usage` call sites.
    return _traced(factory(logical_id, route, spec, settings))


def build_model(settings: Settings | None, spec: Any) -> ModelClient:
    settings = settings or get_settings()
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
    "parse_provider_options",
    "post_with_retry",
    "provider_factory",
    "reasoning_effort_from_budget",
    "record_usage",
    "register_builtin_providers",
    "resolve_provider_config",
    "wire_model_id",
]
