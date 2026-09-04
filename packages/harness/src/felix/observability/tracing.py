"""Lightweight spans with optional OpenTelemetry export."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, TypeVar

logger = logging.getLogger("felix.observability.tracing")

SpanAttributeValue = str | int | float | bool
T = TypeVar("T")

_otel_tracer: Any | None = None
_otel_checked = False
_log_handler: logging.Handler | None = None


def _attach_span(otel_span: Any) -> Any | None:
    """Make `otel_span` the current span so spans opened inside it become children.

    `tracer.start_span` deliberately does *not* touch the active context, so without this
    every Felix span was a separate root: a single chat produced one `manifest` trace, one
    `chat` trace and one `tool.call` trace with nothing tying them together. Attaching here
    (and detaching in `SpanContext.end`) is what turns them into one tree, and it is also
    what lets an inbound `traceparent` parent the whole request.
    """
    try:
        from opentelemetry import context as otel_context
        from opentelemetry import trace

        return otel_context.attach(trace.set_span_in_context(otel_span))
    except Exception:
        logger.debug("otel context attach failed", exc_info=True)
        return None


def _detach_span(token: Any) -> None:
    try:
        from opentelemetry import context as otel_context

        otel_context.detach(token)
    except Exception:
        # A detach out of order raises rather than corrupting the context. Spans are
        # ended in `finally` blocks so the order is LIFO in practice; log and move on.
        logger.debug("otel context detach failed", exc_info=True)


def _get_otel_tracer() -> Any | None:
    global _otel_tracer, _otel_checked
    if _otel_checked:
        return _otel_tracer
    _otel_checked = True
    try:
        from opentelemetry import trace

        _otel_tracer = trace.get_tracer("felix")
    except ImportError:
        _otel_tracer = None
    return _otel_tracer


@dataclass
class SpanContext:
    name: str
    attributes: dict[str, SpanAttributeValue] = field(default_factory=dict)
    _started_at: float = field(default_factory=time.perf_counter)
    _ended: bool = False
    _otel_span: Any | None = None
    _otel_token: Any | None = None

    def set_attribute(self, key: str, value: SpanAttributeValue) -> None:
        self.attributes[key] = value
        if self._otel_span is not None:
            self._otel_span.set_attribute(key, value)

    def end(self) -> None:
        if self._ended:
            return
        self._ended = True
        duration_ms = int((time.perf_counter() - self._started_at) * 1000)
        sanitized = {k: v for k, v in self.attributes.items() if v is not None}
        logger.info(
            "span=%s duration_ms=%s attributes=%s",
            self.name,
            duration_ms,
            sanitized,
        )
        if self._otel_span is not None:
            if self._otel_token is not None:
                _detach_span(self._otel_token)
                self._otel_token = None
            self._otel_span.end()


def _identity_attrs() -> dict[str, SpanAttributeValue]:
    """Session and caller identity for the active request.

    Applied by `make_span`, so it is true of every span rather than of whichever call
    sites remember. It lived in `patterns/model.py` and reached only `chat` spans, while
    the docs claimed every span carried it — the claim is cheaper to make true than to
    keep correcting.

    `thread_id` is an opaque id Felix already generates, and it is what turns a multi-turn
    conversation into one session. The principal and tenant are gated: a tracing backend is
    an egress destination, and `principal_sub` can be a real person's subject claim.
    """
    attrs: dict[str, SpanAttributeValue] = {}
    try:
        from felix.context import try_get_context

        ctx = try_get_context()
    except Exception:  # pragma: no cover - context module always importable
        return attrs
    if ctx is None:
        return attrs

    thread_id = getattr(ctx, "thread_id", None)
    tenant = getattr(getattr(ctx, "auth", None), "tenant_id", "") or ""
    if thread_id:
        # Used as-is. `threads.effective_thread_id` already returns `{tenant}:{suffix}`
        # unconditionally and rejects a suffix containing the delimiter, so the id is
        # tenant-scoped before it reaches here. Prefixing again produced
        # `default:default:my-thread` in a real export.
        attrs["session.id"] = str(thread_id)
        # Two names for two readers; neither is universal.
        attrs["gen_ai.conversation.id"] = str(thread_id)

    settings = getattr(ctx, "settings", None)
    if not bool(getattr(settings, "otel_capture_identity", True)):
        return attrs
    auth = getattr(ctx, "auth", None)
    principal = getattr(auth, "principal_sub", "") if auth is not None else ""
    # `anonymous` is the default subject, not an identity — recording it would fill a
    # backend's Users view with one meaningless entry.
    if principal and principal != "anonymous":
        attrs["user.id"] = str(principal)
        attrs["gen_ai.user.id"] = str(principal)
    if tenant:
        attrs["felix.tenant.id"] = str(tenant)
    return attrs


def _annotate_enclosing_span(attrs: dict[str, SpanAttributeValue]) -> None:
    """Copy identity onto the span already open around this one — the HTTP root.

    The request span is created by the FastAPI instrumentation, which runs before auth, so
    it cannot carry identity at creation time. That matters more than it looks: a backend
    reading a trace's session and user from its *root* span — the correct place to read
    them, since the root is the request — would find none, while every child had them.

    Idempotent and cheap: nested Felix spans re-set values their parent already holds.
    """
    if not attrs:
        return
    try:
        from opentelemetry import trace

        current = trace.get_current_span()
        if current is None or not current.is_recording():
            return
        for key, value in attrs.items():
            current.set_attribute(key, value)
    except Exception:  # pragma: no cover - identity must never break a span
        logger.debug("could not annotate the enclosing span with identity", exc_info=True)


def make_span(name: str, attributes: dict[str, SpanAttributeValue] | None = None) -> SpanContext:
    identity = _identity_attrs()
    _annotate_enclosing_span(identity)
    merged = dict(identity)
    merged.update(attributes or {})
    span = SpanContext(name=name, attributes=merged)
    tracer = _get_otel_tracer()
    if tracer is not None:
        span._otel_span = tracer.start_span(name, attributes=span.attributes)
        span._otel_token = _attach_span(span._otel_span)
    return span


def manifest_span(name: str, version: str) -> SpanContext:
    return make_span("manifest", {"manifest_name": name, "manifest_version": version})


async def with_span[T](
    name: str,
    fn: Callable[[SpanContext], Awaitable[T]],
    attributes: dict[str, SpanAttributeValue] | None = None,
) -> T:
    span = make_span(name, attributes)
    try:
        return await fn(span)
    except Exception:
        span.set_attribute("error", True)
        raise
    finally:
        span.end()


@asynccontextmanager
async def timed_span(
    name: str,
    attributes: dict[str, SpanAttributeValue] | None = None,
    *,
    metric: str,
    labels: dict[str, str] | None = None,
    counter: str | None = None,
) -> AsyncIterator[SpanContext]:
    """A span, a latency observation, and one status convention for both.

    Four call sites hand-rolled this — two model paths, the tool runner and the worker —
    and had already drifted at birth: three histograms with three different status
    conventions, so `{status="error"}` was a valid filter on one of them and silently
    matched nothing on the others. A telemetry vocabulary that is not the same everywhere
    is not a vocabulary, which is the whole point of the module it lives in.

    `counter` additionally records a `{**labels, status}` counter, for call sites that want
    a rate as well as a duration.
    """
    span = make_span(name, attributes)
    started = time.perf_counter()
    status = "ok"
    try:
        yield span
    except Exception:
        status = "error"
        span.set_attribute("error", True)
        raise
    finally:
        from felix.observability.metrics import record_counter, record_histogram

        tagged = {**(labels or {}), "status": status}
        record_histogram(metric, time.perf_counter() - started, tagged)
        if counter:
            record_counter(counter, tagged)
        span.end()


@asynccontextmanager
async def span_cm(
    name: str,
    attributes: dict[str, SpanAttributeValue] | None = None,
) -> AsyncIterator[SpanContext]:
    span = make_span(name, attributes)
    try:
        yield span
    except Exception:
        span.set_attribute("error", True)
        raise
    finally:
        span.end()


__all__ = [
    "SpanAttributeValue",
    "SpanContext",
    "instrument_fastapi",
    "make_span",
    "manifest_span",
    "setup_log_export",
    "setup_observability",
    "shutdown_observability",
    "span_cm",
    "timed_span",
    "with_span",
]


def _parse_otel_headers(raw: str) -> dict[str, str]:
    """Parse the W3C-style `k=v,k2=v2` header list OTLP exporters take.

    Values may contain `=` (base64 credentials routinely do), so split once only.
    """
    headers: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        key = key.strip()
        if key:
            headers[key] = value.strip()
    return headers


def _signal_endpoint(endpoint: str, signal: str) -> str:
    """Per-signal path for OTLP/HTTP. gRPC needs none; HTTP does.

    `FELIX_OTEL_ENDPOINT` is one base URL shared by traces and logs, so the signal path
    has to be derived rather than configured — there is only one setting for two signals.
    The Python OTLP/HTTP exporters treat `endpoint` as the complete URL for their signal
    and append nothing, so passing the base straight through POSTed to it verbatim and got
    a 404 from every collector and backend alike.

    An endpoint that already carries the suffix is left alone, so an operator can still pin
    an exact URL.
    """
    base = endpoint.rstrip("/")
    suffix = f"/v1/{signal}"
    return base if base.endswith(suffix) else base + suffix


def _build_exporter(settings: Any, endpoint: str, headers: dict[str, str]) -> Any:
    """OTLP exporter for the configured protocol.

    `http` is not a nicety: an OTLP/HTTP endpoint behind TLS with an `Authorization`
    header is how hosted collectors are reached, and the gRPC-only exporter could not
    talk to one at all.
    """
    protocol = (getattr(settings, "otel_protocol", "grpc") or "grpc").lower()
    if protocol == "http":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter as HttpExporter,
        )

        return HttpExporter(endpoint=_signal_endpoint(endpoint, "traces"), headers=headers or None)
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter as GrpcExporter,
    )

    insecure = bool(getattr(settings, "otel_insecure", True))
    return GrpcExporter(endpoint=endpoint, insecure=insecure, headers=headers or None)


def _build_sampler(settings: Any) -> Any | None:
    """Ratio sampler, or None to keep the SDK default.

    Every model call is a span now, so an unsampled high-traffic deployment ships a lot
    more than it used to. 1.0 keeps the previous behaviour.
    """
    ratio = float(getattr(settings, "otel_sample_ratio", 1.0) or 1.0)
    if ratio >= 1.0:
        return None
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    return ParentBased(root=TraceIdRatioBased(max(ratio, 0.0)))


# Probe and scrape endpoints. Docker healthchecks and a Prometheus scrape both run every
# 10-15s forever, so tracing them buried real traffic: 270 of 372 traces in the first local
# run were `GET /health` or `GET /metrics`, against 3 chats. None of them describe agent
# behaviour, and all of them cost export bandwidth and retention.
_EXCLUDED_URLS = "health,live,ready,metrics"

# The ASGI instrumentation emits a child span per `http.receive` / `http.send` event, which
# was 552 of 936 observations — four plumbing spans inside every chat trace, between the
# manifest span and the model call. The request span still measures the whole request.
_EXCLUDED_SPANS = ["send", "receive"]


def instrument_fastapi(app: Any) -> bool:
    """Open a root span per HTTP request and honour an inbound `traceparent`.

    `opentelemetry-instrumentation-fastapi` was declared in the `otel` extra and never
    imported, which is why Felix's spans had no request to hang from.

    **Call this while the app is being constructed, not from its lifespan.** It installs
    ASGI middleware, and Starlette finalises its middleware stack before the lifespan
    runs — so instrumenting there is accepted, reports success, and adds nothing. The
    symptom is subtle: every Felix span still exports, each one as its own single-span
    trace, which looks like tracing working rather than tracing broken.
    """
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        logger.warning("otel extra installed without instrumentation-fastapi; no request spans")
        return False
    try:
        FastAPIInstrumentor.instrument_app(
            app,
            excluded_urls=_EXCLUDED_URLS,
            exclude_spans=_EXCLUDED_SPANS,
        )
    except Exception:
        logger.warning("fastapi instrumentation failed; spans will not be parented", exc_info=True)
        return False
    return True


def setup_observability(settings: Any) -> bool:
    """Bootstrap OTLP tracing when FELIX_OTEL_ENABLED=true.

    Returns True when the SDK was configured. Safe no-op without the otel extra.

    Installs the exporter only. FastAPI instrumentation is `instrument_fastapi`, which has
    to run at construction time — see the note there.
    """
    global _otel_tracer, _otel_checked
    if not getattr(settings, "otel_enabled", False):
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning("FELIX_OTEL_ENABLED=true but otel extra is not installed (uv sync --extra otel)")
        return False

    from felix import __version__ as harness_version

    endpoint = getattr(settings, "otel_endpoint", "") or "http://localhost:4317"
    headers = _parse_otel_headers(getattr(settings, "otel_headers", "") or "")
    resource = Resource.create(
        {
            "service.name": getattr(settings, "otel_service_name", "") or "felix",
            # Was hardcoded "0.1.0" while the packages shipped 0.2.2, so every span
            # claimed a version that had not existed for some time.
            "service.version": harness_version,
            "deployment.environment": getattr(settings, "environment", "development"),
        }
    )
    try:
        exporter = _build_exporter(settings, endpoint, headers)
    except ImportError:
        logger.warning(
            "otel exporter for protocol %r is not installed",
            getattr(settings, "otel_protocol", "grpc"),
        )
        return False
    sampler = _build_sampler(settings)
    provider = (
        TracerProvider(resource=resource, sampler=sampler) if sampler else TracerProvider(resource=resource)
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _otel_tracer = trace.get_tracer("felix")
    _otel_checked = True
    logger.info("otel_enabled endpoint=%s protocol=%s", endpoint, getattr(settings, "otel_protocol", "grpc"))
    return True


def _build_log_exporter(settings: Any, endpoint: str, headers: dict[str, str]) -> Any:
    """OTLP log exporter matching the configured protocol."""
    protocol = (getattr(settings, "otel_protocol", "grpc") or "grpc").lower()
    if protocol == "http":
        from opentelemetry.exporter.otlp.proto.http._log_exporter import (
            OTLPLogExporter as HttpLogExporter,
        )

        return HttpLogExporter(endpoint=_signal_endpoint(endpoint, "logs"), headers=headers or None)
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
        OTLPLogExporter as GrpcLogExporter,
    )

    insecure = bool(getattr(settings, "otel_insecure", True))
    return GrpcLogExporter(endpoint=endpoint, insecure=insecure, headers=headers or None)


def setup_log_export(settings: Any) -> bool:
    """Ship logs over OTLP, correlated with the trace that produced them.

    The obvious alternative — pointing a collector's `filelog` receiver at
    `/var/lib/docker/containers` — was tried and rejected. It needs a read-only mount of
    every container's logs on the host and a collector running as root to read files that
    are `root:root 0640`, and it fails *silently* when it cannot (the receiver simply opens
    zero files). It is also Linux/Docker-shaped: nothing about it survives Kubernetes,
    systemd, or a process started by hand.

    Emitting from the process needs none of that, and gets something file tailing cannot:
    the SDK stamps `trace_id` and `span_id` onto each record from the active context, so a
    log line links to the exact span it came from instead of being matched by a regex over
    the text.

    Off by default — log volume is a real cost, and it is the operator's to opt into.
    """
    if not (getattr(settings, "otel_enabled", False) and getattr(settings, "otel_logs", False)):
        return False
    try:
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import Resource
    except ImportError:
        logger.warning("FELIX_OTEL_LOGS=true but the otel extra is not installed")
        return False

    from felix import __version__ as harness_version

    endpoint = getattr(settings, "otel_endpoint", "") or "http://localhost:4317"
    headers = _parse_otel_headers(getattr(settings, "otel_headers", "") or "")
    resource = Resource.create(
        {
            "service.name": getattr(settings, "otel_service_name", "") or "felix",
            "service.version": harness_version,
            "deployment.environment": getattr(settings, "environment", "development"),
        }
    )
    provider = LoggerProvider(resource=resource)
    try:
        exporter = _build_log_exporter(settings, endpoint, headers)
    except ImportError:
        logger.warning("otel log exporter for the configured protocol is not installed")
        return False
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    set_logger_provider(provider)

    global _log_handler
    handler = LoggingHandler(level=logging.NOTSET, logger_provider=provider)
    # Marked so `shutdown_observability` removes exactly this handler and re-running setup
    # cannot stack duplicates — the same reason `configure_logging` marks its own.
    handler._felix_otel = True  # type: ignore[attr-defined]
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, "_felix_otel", False):
            root.removeHandler(existing)
    root.addHandler(handler)
    _log_handler = handler
    logger.info("otel_logs_enabled endpoint=%s", endpoint)
    return True


def shutdown_observability() -> None:
    global _otel_tracer, _otel_checked
    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        shutdown = getattr(provider, "shutdown", None)
        if callable(shutdown):
            shutdown()
    except Exception:
        logger.debug("otel shutdown failed", exc_info=True)
    global _log_handler
    if _log_handler is not None:
        logging.getLogger().removeHandler(_log_handler)
        _log_handler = None
    try:
        from opentelemetry._logs import get_logger_provider

        log_shutdown = getattr(get_logger_provider(), "shutdown", None)
        if callable(log_shutdown):
            log_shutdown()
    except Exception:
        logger.debug("otel log provider shutdown failed", exc_info=True)
    _otel_tracer = None
    _otel_checked = False
