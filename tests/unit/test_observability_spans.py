"""Telemetry that reaches a backend, rather than telemetry that merely exists.

Every test here fails against the tree before this module landed. The three defects being
pinned, in order of how quietly they failed:

1. Spans were never parented. `tracer.start_span` does not touch the active context, so a
   chat produced three unrelated single-span traces instead of one tree.
2. The model call had no span at all, so the one operation carrying token usage, model
   name and cost was the one operation a tracing backend could not see.
3. `stream_turn` is detected with `getattr(model, "stream_turn", None)`. Any wrapper that
   defines it unconditionally pushes every non-streaming provider into the streaming path
   and silently unmeters the turn.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from felix.config import Settings
from felix.observability import metrics as metrics_mod
from felix.observability.tracing import _parse_otel_headers, make_span
from felix.patterns.model import _traced, _TracedClient, _TracedStreamingClient
from felix_ai.types import ChatMessage, ModelChatResult, ModelRoute, TokenUsage
from prometheus_client import CollectorRegistry

from tests.optional_deps import require_optional

ROUTE = ModelRoute(provider="anthropic", model="claude-sonnet-4-6")


class _NoStream:
    """A provider that implements only `chat` — the Protocol's minimum."""

    model_id = "sonnet"
    route = ROUTE

    def __init__(self, result: ModelChatResult | None = None) -> None:
        self.result = result or ModelChatResult(
            message=ChatMessage(role="assistant", content="hi"),
            stop_reason="end_turn",
            usage=TokenUsage(input=11, output=7, cache_creation=3, cache_read=5),
        )
        self.calls = 0

    async def chat(self, messages: Any, tools: Any, opts: Any = None) -> ModelChatResult:
        self.calls += 1
        return self.result


class _WithStream(_NoStream):
    async def stream_turn(self, messages: Any, tools: Any, opts: Any = None):
        yield self.result


def test_a_non_streaming_provider_does_not_gain_stream_turn() -> None:
    """The footgun: callers probe for `stream_turn` with getattr.

    A wrapper that always defines it would route a provider that cannot report usage into
    `_stream_one_turn`, and `record_usage` is the sole feed for every token and cost limit.
    """
    wrapped = _traced(_NoStream())
    assert isinstance(wrapped, _TracedClient)
    assert not isinstance(wrapped, _TracedStreamingClient)
    assert getattr(wrapped, "stream_turn", None) is None


def test_a_streaming_provider_keeps_stream_turn() -> None:
    wrapped = _traced(_WithStream())
    assert isinstance(wrapped, _TracedStreamingClient)
    assert getattr(wrapped, "stream_turn", None) is not None


def test_the_wrapper_forwards_unknown_attributes() -> None:
    """`wire_model_id` and the patterns read provider attributes by name."""
    inner = _NoStream()
    inner.some_provider_extra = "kept"  # type: ignore[attr-defined]
    wrapped = _traced(inner)
    assert wrapped.model_id == "sonnet"
    assert wrapped.route.model == "claude-sonnet-4-6"
    assert wrapped.some_provider_extra == "kept"


@pytest.mark.asyncio
async def test_a_model_call_carries_gen_ai_attributes() -> None:
    """The attribute names are the contract: an OTLP backend keys generations off them."""
    wrapped = _traced(_NoStream())
    spans: list[Any] = []
    result = await _capture_spans(spans, lambda: wrapped.chat([], []))
    assert result.message.content == "hi"
    attrs = spans[0].attributes
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.system"] == "anthropic"
    assert attrs["gen_ai.request.model"] == "claude-sonnet-4-6"
    assert attrs["gen_ai.usage.input_tokens"] == 11
    assert attrs["gen_ai.usage.output_tokens"] == 7
    assert attrs["felix.usage.cache_creation"] == 3
    assert attrs["felix.usage.cache_read"] == 5
    # The logical route name is what operators configure; it is not the wire model id.
    assert attrs["felix.model.route"] == "sonnet"


@pytest.mark.asyncio
async def test_an_unmetered_turn_is_visible_on_the_span() -> None:
    """A turn with no usage cannot be capped by limits. That belongs in the trace."""
    inner = _NoStream(ModelChatResult(message=ChatMessage(role="assistant", content="x"), usage=None))
    spans: list[Any] = []
    await _capture_spans(spans, lambda: _traced(inner).chat([], []))
    assert spans[0].attributes["felix.usage.unmetered"] is True
    assert "gen_ai.usage.input_tokens" not in spans[0].attributes


@pytest.mark.asyncio
async def test_a_failing_model_call_marks_the_span_and_propagates() -> None:
    class _Boom(_NoStream):
        async def chat(self, messages: Any, tools: Any, opts: Any = None) -> ModelChatResult:
            raise RuntimeError("upstream")

    spans: list[Any] = []
    with pytest.raises(RuntimeError, match="upstream"):
        await _capture_spans(spans, lambda: _traced(_Boom()).chat([], []))
    assert spans[0].attributes["error"] is True


@pytest.mark.asyncio
async def test_a_streamed_turn_records_usage_from_the_terminal_result() -> None:
    """Usage rides on the final ModelChatResult, not on the deltas."""
    spans: list[Any] = []
    wrapped = _traced(_WithStream())
    from felix.patterns import model as model_mod

    made: list[Any] = []
    original = model_mod.make_span

    def _spy(name: str, attributes: Any = None) -> Any:
        span = original(name, attributes)
        made.append(span)
        return span

    model_mod.make_span = _spy  # type: ignore[assignment]
    try:
        items = [item async for item in wrapped.stream_turn([], [])]
    finally:
        model_mod.make_span = original  # type: ignore[assignment]
    spans.extend(made)
    assert len(items) == 1
    assert spans[0].attributes["gen_ai.usage.output_tokens"] == 7
    assert spans[0].attributes["felix.streamed"] is True


async def _capture_spans(sink: list[Any], call: Any) -> Any:
    from felix.patterns import model as model_mod

    original = model_mod.make_span

    def _spy(name: str, attributes: Any = None) -> Any:
        span = original(name, attributes)
        sink.append(span)
        return span

    model_mod.make_span = _spy  # type: ignore[assignment]
    try:
        return await call()
    finally:
        model_mod.make_span = original  # type: ignore[assignment]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", {}),
        ("a=1,b=2", {"a": "1", "b": "2"}),
        # Base64 credentials end in padding. Splitting on every `=` truncated them, which
        # is the shape of "auth silently stopped working" rather than a visible failure.
        ("authorization=Basic cGs6c2s=", {"authorization": "Basic cGs6c2s="}),
        ("  spaced = value  ", {"spaced": "value"}),
        ("junk,,x=1", {"x": "1"}),
    ],
)
def test_otel_headers_parse(raw: str, expected: dict[str, str]) -> None:
    assert _parse_otel_headers(raw) == expected


def test_metrics_port_defaults_to_off() -> None:
    """The worker endpoint is unauthenticated, so it must be opt-in."""
    assert Settings(database_url="memory://obs", object_store="memory").metrics_port == 0


def test_a_full_buffer_counts_the_drop() -> None:
    """Losing audit rows was logged and nothing else, so it was invisible to a scrape."""
    from felix.buffers import DurableBuffer

    registry = CollectorRegistry()
    calls: list[tuple[str, Any, float]] = []
    original = metrics_mod.record_counter

    def _spy(name: str, labels: Any = None, value: float = 1, **kw: Any) -> None:
        calls.append((name, labels, value))
        original(name, labels, value, registry=registry)

    metrics_mod.record_counter = _spy  # type: ignore[assignment]
    try:
        buf = DurableBuffer("audit", max_pending=2)
        for i in range(5):
            buf.append({"i": i})
    finally:
        metrics_mod.record_counter = original  # type: ignore[assignment]

    drops = [c for c in calls if c[0] == "felix_buffer_dropped"]
    assert drops, "a full buffer dropped rows without recording a counter"
    assert drops[0][1] == {"buffer": "audit"}
    assert sum(c[2] for c in drops) == 3


def test_spans_nest_into_one_trace() -> None:
    """The defect this pins: three spans, three traces, nothing relating them.

    `tracer.start_span` deliberately leaves the active context alone, so before
    `_attach_span` a chat produced a `manifest` root, a `chat` root and a `tool.call` root
    with no shared trace id — every backend showed three unrelated one-span traces.
    """
    require_optional("opentelemetry.sdk", "otel")
    from felix.observability import tracing as tracing_mod
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    previous_tracer, previous_checked = tracing_mod._otel_tracer, tracing_mod._otel_checked
    tracing_mod._otel_tracer = provider.get_tracer("felix-test")
    tracing_mod._otel_checked = True
    try:
        outer = make_span("manifest", {"manifest_name": "quick"})
        inner = make_span("chat claude-sonnet-4-6", {"gen_ai.system": "anthropic"})
        inner.end()
        outer.end()
    finally:
        tracing_mod._otel_tracer, tracing_mod._otel_checked = previous_tracer, previous_checked

    finished = {s.name: s for s in exporter.get_finished_spans()}
    child, parent = finished["chat claude-sonnet-4-6"], finished["manifest"]
    assert child.parent is not None, "the model span was still an orphan root"
    assert child.parent.span_id == parent.context.span_id
    assert child.context.trace_id == parent.context.trace_id


def test_fastapi_is_instrumented_at_construction_not_in_the_lifespan() -> None:
    """The trap that produced one-span traces in a real deployment.

    `FastAPIInstrumentor.instrument_app` installs ASGI middleware, and Starlette finalises
    its middleware stack before the lifespan runs. Calling it from the lifespan is
    accepted and reports success while adding nothing — and the symptom looks like tracing
    *working*: every Felix span still exports, each as its own single-span trace, because
    no request span exists to parent it. Unit tests of `make_span` cannot see this; only
    where the call sits can.
    """
    source = (Path(__file__).resolve().parents[2] / "apps/api/src/felix_api/app.py").read_text()
    tree = ast.parse(source)

    lifespan_calls: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan":
            lifespan_calls = {
                n.func.id for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            }
    assert lifespan_calls, "no lifespan found in create_app; this test needs rewriting"
    assert "instrument_fastapi" not in lifespan_calls, (
        "instrument_fastapi is called from the lifespan, where installing middleware has "
        "no effect: spans will export as unparented single-span traces"
    )
    assert "instrument_fastapi" in source, "FastAPI is never instrumented, so there is no trace root"
