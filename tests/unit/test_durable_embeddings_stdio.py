"""Durable runs, embeddings ranking, and MCP stdio."""

from __future__ import annotations

import pytest
from felix.config import Settings
from felix.durability.fibers import create_fiber, get_fiber, resume_due_fibers
from felix.durability.runs import get_durable_run, start_durable_chat
from felix.embeddings import cosine_similarity, rank_indices_by_query
from felix.manifests.schema import ExecutionSpec, McpServerRef, ToolsRetrievalSpec
from felix.mcp.client import tools_from_mcp_servers
from felix.patterns.types import ChatMessage
from felix.tools.retrieval import select_tools
from felix.tools.types import ToolInvocationCtx, define_tool
from pydantic import ValidationError


def _settings(**kwargs: object) -> Settings:
    body = {
        "database_url": "memory://durable-embed-stdio",
        "object_store": "memory",
        "allow_insecure": True,
        "hibernate_after_seconds": 120,
    }
    body.update(kwargs)
    return Settings(**body)  # type: ignore[arg-type]


def test_mcp_stdio_schema_requires_command() -> None:
    with pytest.raises(ValidationError):
        McpServerRef(name="fs", transport="stdio")
    ref = McpServerRef(name="fs", transport="stdio", command="npx", args=["-y", "pkg"])
    assert ref.command == "npx"
    assert ref.url == ""


def test_mcp_http_still_requires_url() -> None:
    with pytest.raises(ValidationError):
        McpServerRef(name="docs", transport="http")


def test_cosine_and_rank(monkeypatch: pytest.MonkeyPatch) -> None:
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def fake_encode(texts: list[str], model: str = "") -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            low = t.lower()
            if "search" in low or "web" in low:
                out.append([1.0, 0.0])
            elif "calc" in low:
                out.append([0.0, 1.0])
            else:
                out.append([0.1, 0.1])
        return out

    monkeypatch.setattr("felix.embeddings.encode_texts", fake_encode)
    order = rank_indices_by_query("search the web", ["calculator math", "web search"], "bge")
    assert order is not None
    assert order[0] == 1


def test_select_tools_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_encode(texts: list[str], model: str = "") -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            low = t.lower()
            if "search" in low or "web" in low:
                out.append([1.0, 0.0])
            else:
                out.append([0.0, 1.0])
        return out

    monkeypatch.setattr("felix.embeddings.encode_texts", fake_encode)

    async def _h(_a=None, _c=None):
        return "ok"

    calc = define_tool(name="calculator", description="arithmetic", handler=_h)
    search = define_tool(name="web_search", description="search the public web", handler=_h)
    extra = define_tool(name="unused_blob", description="zzzz", handler=_h)
    selected = select_tools(
        [calc, search, extra],
        [ChatMessage(role="user", content="search the web")],
        ToolsRetrievalSpec(enabled=True, top_k=1, model="bge-base-en-v1.5"),
    )
    assert [t.name for t in selected] == ["web_search"]


@pytest.mark.asyncio
async def test_durable_chat_enqueue_and_poll() -> None:
    settings = _settings()
    started = await start_durable_chat(
        settings,
        "t-dur",
        manifest_id="quick",
        messages=[ChatMessage(role="user", content="hi")],
        thread_id="t-dur:t",
        model_id=None,
        execution=ExecutionSpec(mode="durable", resume_token_ttl_seconds=60),
    )
    assert started["status"] == "accepted"
    token = started["resume_token"]
    row = await get_durable_run(settings, "t-dur", token)
    assert row is not None
    assert row["status"] in {"pending", "running"}
    assert row["resume_token"] == token


@pytest.mark.asyncio
async def test_durable_ttl_expires() -> None:
    settings = _settings()
    fiber = await create_fiber(
        settings,
        "t-ttl",
        status="pending",
        state={"steps": [{"op": "complete"}], "cursor": 0, "expires_at": 1},
    )
    await resume_due_fibers(settings)
    got = await get_fiber(settings, "t-ttl", fiber["id"])
    assert got is not None
    assert got["status"] == "expired"


@pytest.mark.asyncio
async def test_temporal_start_marks_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(durability="temporal")
    called: list[str] = []

    async def fake_start(_settings: Settings, fiber: dict) -> str:
        called.append(fiber["id"])
        return "wf"

    monkeypatch.setattr("felix.durability.temporal.start_fiber_workflow", fake_start)
    started = await start_durable_chat(
        settings,
        "t-tmp",
        manifest_id="quick",
        messages=[ChatMessage(role="user", content="hi")],
        thread_id=None,
        model_id=None,
        execution=ExecutionSpec(mode="durable"),
    )
    assert called
    row = await get_fiber(settings, "t-tmp", started["resume_token"])
    assert row is not None
    assert (row.get("state_json") or {}).get("backend") == "temporal"
    await resume_due_fibers(settings)
    row2 = await get_fiber(settings, "t-tmp", started["resume_token"])
    assert row2 is not None
    assert row2["status"] in {"pending", "running"}


@pytest.mark.asyncio
async def test_mcp_stdio_tool_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_stdio(ref: McpServerRef, method: str, params: dict | None = None, **_k):
        if method == "tools/list":
            return {
                "tools": [
                    {
                        "name": "ping",
                        "description": "Ping",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ]
            }
        if method == "tools/call":
            return {"content": [{"type": "text", "text": "pong"}]}
        return {}

    monkeypatch.setattr("felix.mcp.stdio.stdio_rpc", fake_stdio)
    ref = McpServerRef(name="local", transport="stdio", command="mcp-fake")
    tools = await tools_from_mcp_servers([ref])
    assert len(tools) == 1
    assert tools[0].name == "local__ping"
    assert tools[0].executor.transport == "mcp"
    out = await tools[0].executor.execute({}, ToolInvocationCtx())
    text = out if isinstance(out, str) else out.content
    assert "pong" in text
