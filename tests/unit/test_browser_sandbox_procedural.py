"""Browser, sandbox/container tools, and procedural memory."""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import Settings
from felix.manifests.builder import BuildDeps, apply_content_screening, build_agent
from felix.manifests.schema import (
    BrowserToolRef,
    ContainerRef,
    ContentScreening,
    ProceduralSpec,
    SandboxRef,
)
from felix.memory.procedural import (
    make_remember_procedure_tool,
    rank_procedures,
    retrieve_procedures,
)
from felix.memory.store import put_memory
from felix.patterns.types import ChatMessage
from felix.tools.browser import tools_from_browser_refs
from felix.tools.sandboxes import tools_from_containers, tools_from_sandboxes
from felix.tools.types import ToolInvocationCtx


def _settings() -> Settings:
    return Settings(
        database_url="memory://browser-sandbox-proc",
        object_store="memory",
        allow_insecure=True,
        environment="development",
    )


@pytest.mark.asyncio
async def test_browser_missing_playwright(monkeypatch: pytest.MonkeyPatch) -> None:
    import felix.tools.browser as browser_mod

    def _boom() -> Any:
        raise ImportError("no playwright")

    monkeypatch.setattr(browser_mod, "_load_playwright", _boom)
    tools = tools_from_browser_refs([BrowserToolRef(name="browse", binding="chromium", op="content")])
    assert tools[0].executor.transport == "browser"
    out = await tools[0].executor.execute({"url": "https://example.com"}, ToolInvocationCtx())
    text = out if isinstance(out, str) else out.content
    assert "Playwright is not installed" in text


@pytest.mark.asyncio
async def test_browser_content_and_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    import felix.tools.browser as browser_mod

    class _Page:
        def __init__(self) -> None:
            self.routes: list[tuple[str, Any]] = []

        async def route(self, pattern: str, handler: Any) -> None:
            # Real Playwright pages always have this; the egress guard registers here to
            # re-validate redirects and subresources, which page.goto() otherwise follows
            # straight past the initial SSRF check.
            self.routes.append((pattern, handler))

        async def goto(self, url: str, **_kwargs: Any) -> None:
            self.url = url

        async def inner_text(self, _sel: str) -> str:
            return "hello from page"

        async def title(self) -> str:
            return "Example"

        async def eval_on_selector_all(self, _sel: str, _js: str) -> list[str]:
            return ["https://example.com/a"]

        async def content(self) -> str:
            return "<html></html>"

        async def screenshot(self, full_page: bool = False) -> bytes:
            return b"png-bytes"

        async def pdf(self) -> bytes:
            return b"%PDF"

    class _Browser:
        async def new_page(self) -> _Page:
            return _Page()

        async def close(self) -> None:
            return None

    class _Chromium:
        async def launch(self, headless: bool = True) -> _Browser:
            return _Browser()

    class _PW:
        chromium = _Chromium()

        async def __aenter__(self) -> _PW:
            return self

        async def __aexit__(self, *a: object) -> bool:
            return False

    monkeypatch.setattr(browser_mod, "_load_playwright", lambda: _PW)
    tools = tools_from_browser_refs(
        [
            BrowserToolRef(
                name="browse",
                binding="chromium",
                op="content",
                path_prefix="https://example.com",
            )
        ]
    )
    out = await tools[0].executor.execute({"url": "https://example.com/docs"}, ToolInvocationCtx())
    assert "hello from page" in (out if isinstance(out, str) else out.content)

    blocked = await tools[0].executor.execute({"url": "https://evil.example/x"}, ToolInvocationCtx())
    assert "must start with" in (blocked if isinstance(blocked, str) else blocked.content)

    loopback = await tools[0].executor.execute({"url": "http://127.0.0.1/"}, ToolInvocationCtx())
    assert "browser_error" in (loopback if isinstance(loopback, str) else loopback.content)


def test_browser_screening_transport() -> None:
    tools = tools_from_browser_refs([BrowserToolRef(name="browse", binding="chromium", op="links")])
    wrapped = apply_content_screening(tools, ContentScreening(enabled=True), "wired")
    assert wrapped[0].executor.transport == "browser"


@pytest.mark.asyncio
async def test_sandbox_tool_runs_via_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    import felix.tools.sandboxes as sand_mod

    seen: dict[str, Any] = {}

    class _FakeBox:
        def __init__(self, **kwargs: Any) -> None:
            seen.update(kwargs)

        async def execute(self, args: dict[str, Any], ctx: Any = None) -> str:
            return f"ok:{seen.get('command')}:{args}"

    monkeypatch.setattr(sand_mod, "SandboxExecutor", _FakeBox)
    tools = tools_from_sandboxes(
        [
            SandboxRef(
                name="pybox",
                binding="python:3.14-slim",
                sandbox_tool_name="run_python",
                path_prefix="/workspace",
            )
        ]
    )
    assert tools[0].name == "run_python"
    assert tools[0].executor.transport == "sandbox"
    denied = await tools[0].executor.execute({"code": "print(1)", "path": "/tmp/x"}, ToolInvocationCtx())
    assert "path must start" in (denied if isinstance(denied, str) else denied.content)
    out = await tools[0].executor.execute(
        {"code": "print(1)", "path": "/workspace/app.py"}, ToolInvocationCtx()
    )
    text = out if isinstance(out, str) else out.content
    assert "print(1)" in text
    assert seen["image"] == "python:3.14-slim"


@pytest.mark.asyncio
async def test_container_gateway_post(monkeypatch: pytest.MonkeyPatch) -> None:
    import felix.tools.sandboxes as sand_mod

    class _Resp:
        status_code = 200
        text = "container-ok"

        def raise_for_status(self) -> None:
            return None

    class _Client:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *a: object) -> bool:
            return False

        async def post(self, url: str, json: dict | None = None, headers: dict | None = None):
            assert url == "https://gateway.example.com/run"
            assert json is not None
            assert json["image"] == "busybox:latest"
            assert json["payload"]["x"] == 1
            assert headers is not None
            assert headers["authorization"] == "Bearer tok"
            return _Resp()

    monkeypatch.setattr(sand_mod.httpx, "AsyncClient", _Client)
    tools = tools_from_containers(
        [
            ContainerRef(
                name="job",
                gateway_url="https://gateway.example.com/run",
                image="busybox:latest",
                container_tool_name="run_job",
                auth="tok",
            )
        ]
    )
    assert tools[0].name == "run_job"
    assert tools[0].executor.transport == "container"
    out = await tools[0].executor.execute({"payload": {"x": 1}}, ToolInvocationCtx())
    assert (out if isinstance(out, str) else out.content) == "container-ok"


def test_rank_procedures_prefers_overlap() -> None:
    rows = [
        {"content": "reset the database with felix migrate"},
        {"content": "how to rotate api keys in production"},
        {"content": "unrelated cooking recipe"},
    ]
    picked = rank_procedures(rows, "rotate api keys", top_k=1)
    assert picked[0]["content"].startswith("how to rotate")


@pytest.mark.asyncio
async def test_procedural_remember_and_retrieve() -> None:
    settings = _settings()
    tool = make_remember_procedure_tool(settings=settings, tenant_id="t-proc", manifest_id="wired")
    out = await tool.executor.execute(
        {"title": "rotate keys", "body": "Use the dashboard to rotate api keys yearly."},
        ToolInvocationCtx(),
    )
    assert "remembered_procedure:" in (out if isinstance(out, str) else out.content)
    block = await retrieve_procedures(
        settings,
        "t-proc",
        manifest_id="wired",
        query="how do I rotate api keys",
        spec=ProceduralSpec(enabled=True, top_k=2),
    )
    assert "[known procedures]" in block
    assert "rotate" in block.lower()


@pytest.mark.asyncio
async def test_build_agent_binds_browser_sandbox_container_procedural() -> None:
    from felix.tools.provider import InMemoryToolProvider

    settings = _settings()
    agent = await build_agent(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "wired-tools"},
            "spec": {
                "pattern": "react",
                "tools": [],
                "browser_tools": [
                    {
                        "name": "browse",
                        "binding": "chromium",
                        "op": "content",
                    }
                ],
                "sandboxes": [
                    {
                        "name": "pybox",
                        "binding": "python:3.14-slim",
                        "sandbox_tool_name": "run_python",
                    }
                ],
                "containers": [
                    {
                        "name": "job",
                        "gateway_url": "https://gateway.example.com/run",
                        "image": "busybox",
                        "container_tool_name": "run_job",
                    }
                ],
                "procedural_memory": {"enabled": True, "top_k": 2},
            },
        },
        deps=BuildDeps(
            tools=InMemoryToolProvider(),
            settings=settings,
            tenant_id="t-proc",
        ),
        settings=settings,
    )
    names = {t.name for t in agent.tools}
    assert "browse" in names
    assert "run_python" in names
    assert "run_job" in names
    assert "remember_procedure" in names
    by_name = {t.name: t for t in agent.tools}
    assert by_name["browse"].executor.transport == "browser"
    assert by_name["run_python"].executor.transport == "sandbox"
    assert by_name["run_job"].executor.transport == "container"


@pytest.mark.asyncio
async def test_react_injects_procedures() -> None:
    settings = _settings()
    await put_memory(
        settings,
        "t-react",
        content="To restart the worker, run felix worker restart.",
        kind="procedure",
        manifest_id="wired-tools",
    )
    from felix.manifests.schema import ModelSpec
    from felix.patterns.react import build_react_agent
    from felix.patterns.types import InvokeInput

    class _Result:
        message = ChatMessage(role="assistant", content="ok")
        usage = None

    class _Model:
        model_id = "test"

        async def chat(self, messages: list[ChatMessage], tools: list) -> _Result:
            assert any(m.role == "system" and "known procedures" in (m.content or "") for m in messages)
            return _Result()

    agent = build_react_agent(
        {
            "tools": [],
            "system_prompt": "You are a test.",
            "model_spec": ModelSpec(),
            "manifest_id": "wired-tools",
            "manifest_version": "1.0.0",
            "settings": settings,
            "tenant_id": "t-react",
            "procedural_memory": ProceduralSpec(enabled=True, top_k=3),
        }
    )
    agent._resolve_model = lambda _input: _Model()  # type: ignore[method-assign]
    result = await agent.invoke(
        InvokeInput(
            messages=[ChatMessage(role="user", content="how do I restart the worker")],
            tenant_id="t-react",
        )
    )
    assert result.final.content == "ok"
