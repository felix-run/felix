"""Felix observability — Prometheus metrics + optional OTel spans."""

from __future__ import annotations

from felix.observability.metrics import record_counter, record_counter_detached, record_histogram
from felix.observability.tracing import make_span, manifest_span, with_span

__all__ = [
    "make_span",
    "manifest_span",
    "record_counter",
    "record_counter_detached",
    "record_histogram",
    "with_span",
]
