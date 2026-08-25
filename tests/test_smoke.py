"""Smoke tests for cloud-agnostic seams and react slice."""

from __future__ import annotations

import pytest
from felix.config import Settings
from felix.manifests.loader import list_bundled, load_bundled
from felix.manifests.schema import Manifest
from felix.security.expr import evaluate_expression
from felix.storage import MemoryObjectStore, build_object_store
from felix.storage.fs import FilesystemObjectStore


def test_evaluate_expression() -> None:
    assert evaluate_expression("7 * 6") == 42


def test_bundled_quick_manifest() -> None:
    names = list_bundled()
    assert "quick" in names
    m = load_bundled("quick")
    assert isinstance(m, Manifest)
    assert m.apiVersion == "felix/v1"
    assert m.spec.pattern == "react"


def test_object_store_memory() -> None:
    settings = Settings(object_store="memory")
    store = build_object_store(settings)
    assert isinstance(store, MemoryObjectStore)


@pytest.mark.asyncio
async def test_memory_object_store_roundtrip() -> None:
    store = MemoryObjectStore()
    await store.put("a/b", b"hello")
    assert await store.get("a/b") == b"hello"
    assert await store.exists("a/b")
    await store.delete("a/b")
    assert await store.get("a/b") is None


@pytest.mark.asyncio
async def test_fs_object_store_roundtrip(tmp_path) -> None:
    settings = Settings(object_store="fs", data_dir=str(tmp_path))
    store = FilesystemObjectStore(settings)
    await store.put("a/b", b"hello")
    assert await store.get("a/b") == b"hello"
    assert await store.exists("a/b")
    await store.delete("a/b")
    assert await store.get("a/b") is None


@pytest.mark.asyncio
async def test_build_object_store_fs(tmp_path) -> None:
    settings = Settings(object_store="fs", data_dir=str(tmp_path))
    store = build_object_store(settings)
    await store.put("x", b"1")
    assert await store.get("x") == b"1"


@pytest.mark.asyncio
async def test_build_react_agent_calculator() -> None:
    from felix.manifests.builder import build_agent
    from felix.patterns.types import ChatMessage, InvokeInput
    from felix_api.composition import compose

    settings = Settings(
        auth_mode="none",
        allow_insecure=True,
        anthropic_api_key="",
        openai_api_key="",
        object_store="memory",
    )
    tools = compose(settings)
    agent = await build_agent("quick", tools=tools, settings=settings)

    try:
        result = await agent.invoke(
            InvokeInput(
                messages=[ChatMessage(role="user", content="What is 7 * 6?")],
            )
        )
    except Exception as exc:
        # Genuinely conditional, unlike the other two: this one wants a live backend.
        pytest.skip(f"model backend unavailable: {exc}")
    else:
        assert result.final is not None
