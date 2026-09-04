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


def make_span(name: str, attributes: dict[str, SpanAttributeValue] | None = None) -> SpanContext:
    span = SpanContext(name=name, attributes=dict(attributes or {}))
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
    "make_span",
    "manifest_span",
    "setup_observability",
    "shutdown_observability",
    "span_cm",
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

        return HttpExporter(endpoint=endpoint, headers=headers or None)
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


def _instrument_fastapi(app: Any) -> bool:
    """Open a root span per HTTP request and honour an inbound `traceparent`.

    `opentelemetry-instrumentation-fastapi` was declared in the `otel` extra and never
    imported, which is why Felix's spans had no request to hang from.
    """
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        logger.warning("otel extra installed without instrumentation-fastapi; no request spans")
        return False
    try:
        FastAPIInstrumentor.instrument_app(app)
    except Exception:
        logger.warning("fastapi instrumentation failed; spans will not be parented", exc_info=True)
        return False
    return True


def setup_observability(settings: Any, app: Any | None = None) -> bool:
    """Bootstrap OTLP tracing when FELIX_OTEL_ENABLED=true.

    Returns True when the SDK was configured. Safe no-op without the otel extra.

    Pass `app` from the API to also instrument FastAPI; the worker calls this without one.
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
    if app is not None:
        _instrument_fastapi(app)
    logger.info("otel_enabled endpoint=%s protocol=%s", endpoint, getattr(settings, "otel_protocol", "grpc"))
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
    _otel_tracer = None
    _otel_checked = False
