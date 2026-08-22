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
            self._otel_span.end()


def make_span(name: str, attributes: dict[str, SpanAttributeValue] | None = None) -> SpanContext:
    span = SpanContext(name=name, attributes=dict(attributes or {}))
    tracer = _get_otel_tracer()
    if tracer is not None:
        span._otel_span = tracer.start_span(name, attributes=span.attributes)
    return span


def manifest_span(name: str, version: str) -> SpanContext:
    return make_span("manifest", {"manifest_name": name, "manifest_version": version})


async def with_span(
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


def setup_observability(settings: Any) -> bool:
    """Bootstrap OTLP tracing when FELIX_OTEL_ENABLED=true.

    Returns True when the SDK was configured. Safe no-op without the otel extra.
    """
    global _otel_tracer, _otel_checked
    if not getattr(settings, "otel_enabled", False):
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            "FELIX_OTEL_ENABLED=true but otel extra is not installed "
            "(uv sync --extra otel)"
        )
        return False

    endpoint = getattr(settings, "otel_endpoint", "") or "http://localhost:4317"
    resource = Resource.create(
        {
            "service.name": "felix",
            "service.version": "0.1.0",
            "deployment.environment": getattr(settings, "environment", "development"),
        }
    )
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _otel_tracer = trace.get_tracer("felix")
    _otel_checked = True
    logger.info("otel_enabled endpoint=%s", endpoint)
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
