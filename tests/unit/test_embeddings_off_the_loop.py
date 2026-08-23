"""Embedding must not run on the event loop.

`encode_texts` is a blocking, CPU-bound call, and the first call for a model also
loads it from disk. Every caller sits on the request path — tool retrieval runs up to
four times per loop step, procedural recall runs once per turn, and the `semantic:N`
session strategy runs on every render — so a synchronous encode stalled every other
request the worker was serving, not just the one that asked for it.

These tests assert the property directly: a coroutine ticking alongside the encode
must keep making progress. If the encode moves back onto the loop, the tick count
collapses to zero and they fail.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from felix.manifests.schema import ToolsRetrievalSpec
from felix.patterns.types import ChatMessage
from felix.tools.retrieval import select_tools, select_tools_from_ctx_async, will_embed
from felix.tools.types import define_tool

# Long enough that an inline call is unmistakable, short enough to keep the suite fast.
_ENCODE_SECONDS = 0.2
_TICK_SECONDS = 0.005


async def _h(_a=None, _c=None):
    return "ok"


def _tools(n: int) -> list:
    return [define_tool(name=f"tool_{i}", description=f"does thing number {i}", handler=_h) for i in range(n)]


def _slow_encode(texts: list[str], model: str = "") -> list[list[float]]:
    """Stands in for sentence-transformers: blocking, and slow enough to notice."""
    time.sleep(_ENCODE_SECONDS)
    return [[float(len(t)), 1.0] for t in texts]


async def _ticks_during(coro) -> tuple[int, object]:
    """Run `coro`, counting how many times the event loop got control meanwhile."""
    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(_TICK_SECONDS)
            ticks += 1

    task = asyncio.create_task(_ticker())
    await asyncio.sleep(0)  # let the ticker reach its first await
    result = await coro
    task.cancel()
    return ticks, result


@pytest.mark.asyncio
async def test_tool_retrieval_encode_does_not_block_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression: this path runs up to four times per loop step."""
    monkeypatch.setattr("felix.embeddings.encode_texts", _slow_encode)
    spec = ToolsRetrievalSpec(enabled=True, top_k=2, model="bge")
    messages = [ChatMessage(role="user", content="thing number 3")]

    ticks, selected = await _ticks_during(select_tools_from_ctx_async(_tools(6), messages, spec))

    assert len(selected) == 2, "sanity: retrieval actually ran"
    assert ticks > 0, "the event loop was blocked for the whole encode"


@pytest.mark.asyncio
async def test_rank_indices_by_query_async_does_not_block_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from felix.embeddings import rank_indices_by_query_async

    monkeypatch.setattr("felix.embeddings.encode_texts", _slow_encode)

    ticks, order = await _ticks_during(rank_indices_by_query_async("query", ["a", "bb", "ccc"], "bge"))

    assert order is not None and len(order) == 3
    assert ticks > 0, "the event loop was blocked for the whole encode"


@pytest.mark.asyncio
async def test_async_ranking_matches_the_sync_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """Moving off the loop must not change the answer."""
    from felix.embeddings import rank_indices_by_query, rank_indices_by_query_async

    monkeypatch.setattr("felix.embeddings.encode_texts", _slow_encode)
    blobs = ["a", "bbbb", "cc"]

    assert await rank_indices_by_query_async("q", blobs, "bge") == rank_indices_by_query("q", blobs, "bge")


@pytest.mark.asyncio
async def test_no_executor_hop_when_retrieval_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default path is inline: `tools_retrieval` is off in every bundled manifest."""
    hops = 0
    real = asyncio.to_thread

    async def _counting(fn, /, *a, **kw):
        nonlocal hops
        hops += 1
        return await real(fn, *a, **kw)

    monkeypatch.setattr(asyncio, "to_thread", _counting)
    tools = _tools(6)
    messages = [ChatMessage(role="user", content="hi")]

    assert await select_tools_from_ctx_async(tools, messages, None) == tools
    assert await select_tools_from_ctx_async(tools, messages, ToolsRetrievalSpec()) == tools
    assert hops == 0


# --- the guard must not drift from the code it guards --------------------------
#
# `will_embed` mirrors the early returns in `select_tools` plus its `if model` branch.
# A predicate that drifts is worse than none, because it silently puts a blocking
# encode back on the event loop. So assert the two agree, rather than trusting them to.

_CASES = [
    ("no spec", None, 6),
    ("disabled", ToolsRetrievalSpec(enabled=False, top_k=2, model="bge"), 6),
    ("fewer tools than top_k", ToolsRetrievalSpec(enabled=True, top_k=9, model="bge"), 6),
    ("exactly top_k", ToolsRetrievalSpec(enabled=True, top_k=6, model="bge"), 6),
    ("no model", ToolsRetrievalSpec(enabled=True, top_k=2, model=""), 6),
    ("embeds", ToolsRetrievalSpec(enabled=True, top_k=2, model="bge"), 6),
]


@pytest.mark.parametrize(("label", "spec", "count"), _CASES, ids=[c[0] for c in _CASES])
def test_will_embed_agrees_with_select_tools(
    monkeypatch: pytest.MonkeyPatch, label: str, spec, count: int
) -> None:
    reached = False

    def _spy(texts: list[str], model: str = "") -> list[list[float]]:
        nonlocal reached
        reached = True
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr("felix.embeddings.encode_texts", _spy)
    select_tools(_tools(count), [ChatMessage(role="user", content="thing number 3")], spec)

    assert reached == will_embed(_tools(count), spec), f"will_embed disagrees with select_tools for: {label}"


def test_concurrent_encoders_load_the_model_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Threading the encode is what makes this race reachable; each model is ~GBs."""
    from concurrent.futures import ThreadPoolExecutor

    import felix.embeddings as emb

    loads = 0

    class _FakeEncoder:
        def encode(self, texts, convert_to_numpy=True):
            return [[1.0, 0.0] for _ in texts]

    class _FakeModule:
        @staticmethod
        def SentenceTransformer(name: str):
            nonlocal loads
            loads += 1
            time.sleep(0.05)  # widen the window a real load would leave open
            return _FakeEncoder()

    monkeypatch.setitem(__import__("sys").modules, "sentence_transformers", _FakeModule)
    monkeypatch.setattr(emb, "_models", {})

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: emb.encode_texts(["x"], "same-model"), range(8)))

    assert all(r is not None for r in results)
    assert loads == 1, f"loaded the same model {loads} times concurrently"
