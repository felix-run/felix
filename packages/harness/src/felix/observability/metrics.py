"""Prometheus counters + histograms for the Felix harness."""

from __future__ import annotations

import logging
from typing import Any

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Histogram

logger = logging.getLogger("felix.observability.metrics")

MetricLabels = dict[str, str | int | float | None]

_counters: dict[str, Counter] = {}
_histograms: dict[str, Histogram] = {}


def _label_keys(labels: MetricLabels) -> tuple[str, ...]:
    return tuple(sorted(k for k, v in labels.items() if v is not None))


def _label_values(labels: MetricLabels, keys: tuple[str, ...]) -> list[str]:
    return [str(labels[k]) for k in keys]


def record_counter(
    name: str,
    labels: MetricLabels | None = None,
    value: float = 1,
    *,
    registry: CollectorRegistry = REGISTRY,
) -> None:
    """Increment a Prometheus counter (creates it on first use)."""
    labels = labels or {}
    keys = _label_keys(labels)
    cache_key = f"{name}|{','.join(keys)}"
    counter = _counters.get(cache_key)
    if counter is None:
        try:
            counter = Counter(name, f"Felix counter {name}", labelnames=keys, registry=registry)
        except ValueError:
            # Already registered under another label set — fall back to logging.
            logger.debug("metric %s already registered; logging instead", name)
            logger.info("metric=%s kind=counter value=%s labels=%s", name, value, labels)
            return
        _counters[cache_key] = counter
    if keys:
        counter.labels(**dict(zip(keys, _label_values(labels, keys), strict=True))).inc(value)
    else:
        counter.inc(value)


def record_histogram(
    name: str,
    value: float,
    labels: MetricLabels | None = None,
    *,
    registry: CollectorRegistry = REGISTRY,
) -> None:
    labels = labels or {}
    keys = _label_keys(labels)
    cache_key = f"{name}|{','.join(keys)}"
    hist = _histograms.get(cache_key)
    if hist is None:
        try:
            hist = Histogram(name, f"Felix histogram {name}", labelnames=keys, registry=registry)
        except ValueError:
            logger.info("metric=%s kind=histogram value=%s labels=%s", name, value, labels)
            return
        _histograms[cache_key] = hist
    if keys:
        hist.labels(**dict(zip(keys, _label_values(labels, keys), strict=True))).observe(value)
    else:
        hist.observe(value)


def record_counter_detached(
    _env: Any,
    name: str,
    labels: MetricLabels | None = None,
    value: float = 1,
) -> None:
    """Env-taking variant for call sites outside RequestContext."""
    record_counter(name, labels, value)


__all__ = [
    "MetricLabels",
    "record_counter",
    "record_counter_detached",
    "record_histogram",
]
