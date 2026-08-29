"""Optional metrics sink.

`felix.observability.metrics.record_counter` cannot be imported here — this package may not
import the harness. The harness installs itself as the sink once at import time; without it
the counters are simply dropped, which is the right behaviour for a library.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping
from typing import Any

# The labels bag is `Any` on purpose: the harness sink accepts a wider value type than
# this package ever passes, and `dict` invariance makes the narrower spelling unassignable
# across the boundary. This module's own callers stay checked through `record_counter`.
CounterSink = Callable[[str, Any], None]

_sink: CounterSink | None = None


def set_counter_sink(sink: CounterSink | None) -> None:
    """Install the process-wide counter sink. Called once by `felix.patterns`."""
    global _sink
    _sink = sink


def record_counter(name: str, labels: Mapping[str, str]) -> None:
    """Record a counter through the installed sink, or drop it if there is none."""
    if _sink is None:
        return
    # A metrics sink must never fail a model call.
    with contextlib.suppress(Exception):
        _sink(name, labels)


__all__ = ["CounterSink", "record_counter", "set_counter_sink"]
