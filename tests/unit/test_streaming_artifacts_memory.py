"""Tests for streaming helpers, artifact spill, and memory capture."""

from __future__ import annotations

import pytest
from felix.config import Settings
from felix.manifests.schema import ArtifactsSpec, MemoryCapture
from felix.memory.capture import _heuristic_facts, active_facts_prompt, capture_from_turn
from felix.memory.store import list_active
from felix.patterns.types import ChatMessage
from felix.storage import MemoryObjectStore
from felix.tools.types import ToolInvocationCtx, define_tool


@pytest.mark.asyncio
async def test_openai_stream_parses_sse_deltas(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.patterns.model import ModelRoute, _HttpModelClient

    class _FakeResp:
        status_code = 200

        async def aiter_lines(self):
            chunks = [
                'data: {"choices":[{"delta":{"content":"Hel"}}]}',
                'data: {"choices":[{"delta":{"content":"lo"}}]}',
                "data: [DONE]",
            ]
            for c in chunks:
                yield c

        async def aread(self) -> bytes:
            return b""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, *a, **k):
            return _FakeResp()

    import felix.patterns.model as model_mod

    monkeypatch.setattr(model_mod.httpx, "AsyncClient", _FakeClient)

    client = _HttpModelClient(
        model_id="gpt-4.1",
        route=ModelRoute(provider="openai", model="gpt-4.1"),
        settings=Settings(database_url="memory://t", object_store="memory", allow_insecure=True),
        spec=None,
        base_url="https://api.openai.com/v1",
        api_key="test",
        style="openai",
    )
    parts = [
        c
        async for c in client.stream(
            [ChatMessage(role="user", content="hi")],
            [],
        )
    ]
    assert "".join(parts) == "Hello"


@pytest.mark.asyncio
async def test_artifact_spill() -> None:
    from felix.artifacts import apply_artifact_spill

    store = MemoryObjectStore()

    async def big(_args, _ctx=None):
        return "x" * 100

    tool = define_tool(name="big", description="big", handler=big)
    wrapped = apply_artifact_spill(
        [tool],
        ArtifactsSpec(enabled=True, threshold_chars=20, preview_chars=5),
        object_store=store,
        tenant_id="t",
        manifest_id="quick",
    )[0]
    out = await wrapped.executor.execute({}, ToolInvocationCtx())
    text = out if isinstance(out, str) else out.content
    assert "artifact:" in text
    assert text.startswith("xxxxx")
    # Something was stored
    keys = [k for k in store._data if k.startswith("artifacts/")]
    assert keys


@pytest.mark.asyncio
async def test_memory_capture_heuristic() -> None:
    settings = Settings(database_url="memory://cap", object_store="memory", allow_insecure=True)
    facts = await capture_from_turn(
        settings,
        "default",
        manifest_id="quick",
        user_text="Remember this.",
        assistant_text="The API base URL is https://example.com/v1.\nMaybe later.",
        capture=MemoryCapture(enabled=True, max_facts=5, min_chars=10),
        model=None,
    )
    assert any("API base URL" in f for f in facts)
    active = await list_active(settings, "default", manifest_id="quick", kind="fact")
    assert active

    block = await active_facts_prompt(settings, "default", manifest_id="quick")
    # Fenced and labelled: recalled facts are model-extracted from earlier turns and can
    # carry text that originated in tool output, so they are reference material rather
    # than part of the system prompt.
    assert "<known_facts" in block
    assert "</known_facts>" in block
    assert "not instructions" in block


def test_heuristic_facts_filters_questions() -> None:
    out = _heuristic_facts(
        "What is your name?\nThe deploy target is us-east-1.\nmaybe tomorrow",
        max_facts=5,
        min_chars=10,
    )
    assert any("deploy target" in f for f in out)
    assert not any("?" in f for f in out)
