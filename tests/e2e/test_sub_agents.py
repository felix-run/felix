"""`spec.sub_agents` resolved the way a request resolves a manifest: store, objects, bundled.

Found in a real run: a router whose children were stored manifests compiled every child as an
empty `You are <name>.` agent with no tools, because children were looked up in bundled YAML
only and a miss became an empty manifest. Nothing failed — the router routed, the blank child
answered. These pin that the child is the tenant's own, and that a child that resolves nowhere,
or a cycle of routers, fails the compile rather than the conversation.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn


def _agent(name: str, **spec: Any) -> Any:
    base: dict[str, Any] = {"pattern": "react", "auth": {"inbound": {"allow_anonymous": True}}}
    base.update(spec)
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": name}, "spec": base}
    )


def _router(name: str, children: list[str]) -> Any:
    return _agent(name, pattern="router", sub_agents=children, system_prompt={"inline": "Route it."})


async def _chat(app: Any, name: str) -> Any:
    return await app.client.post(
        "/v1/chat/completions",
        json={"model": name, "messages": [{"role": "user", "content": "what is DNA?"}]},
    )


async def test_a_stored_child_is_the_tenants_agent_not_a_blank_one(boot: Any) -> None:
    manifests = {
        "e2e-bio": _agent("e2e-bio", system_prompt={"inline": "You do biology."}),
        "e2e-router": _router("e2e-router", ["e2e-bio"]),
    }
    script = [ScriptedTurn(content="e2e-bio"), ScriptedTurn(content="DNA is a molecule.")]
    async with boot(script, manifests=manifests) as app:
        resp = await _chat(app, "e2e-router")
        assert resp.status_code == 200, resp.text
        [_classify, answer] = app.spy.prompts
        assert "You do biology." in answer[0].content


async def test_a_child_that_resolves_nowhere_fails_the_compile(boot: Any) -> None:
    async with boot(manifests={"e2e-router": _router("e2e-router", ["e2e-nowhere"])}) as app:
        with pytest.raises(LookupError, match="e2e-nowhere"):
            await _chat(app, "e2e-router")
        assert app.spy.prompts == []


async def test_a_cycle_of_routers_fails_the_compile(boot: Any) -> None:
    manifests = {
        "e2e-a": _router("e2e-a", ["e2e-b"]),
        "e2e-b": _router("e2e-b", ["e2e-a"]),
    }
    async with boot(manifests=manifests) as app:
        with pytest.raises(ValueError, match="e2e-a -> e2e-b -> e2e-a"):
            await _chat(app, "e2e-a")


async def test_a_bundled_parent_reaches_a_bundled_child_without_a_tenant() -> None:
    """The path with no runtime around it — the CLI and plugins calling `build_agent` directly."""
    from felix.manifests.builder import BuildDeps, build_agent
    from felix.tools.builtins import default_tool_provider

    deps = BuildDeps(tools=default_tool_provider())
    router = await build_agent("router", deps=deps)
    assert set(router.sub_agents) == {"quick", "deep", "support"}  # type: ignore[attr-defined]

    missing = _router("e2e-router", ["e2e-nowhere"])
    with pytest.raises(LookupError, match="e2e-nowhere"):
        await build_agent(missing, deps=BuildDeps(tools=default_tool_provider()))


async def test_a_shared_child_compiles_once_and_nesting_is_bounded(
    boot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tenant can write A → [B, C], both naming D; D compiles once. And a chain deeper than
    `MAX_SUB_AGENT_DEPTH` is refused, so one chat cannot trigger an unbounded compile."""
    from felix import runtime

    resolved: list[str] = []
    real = runtime.resolve_tenant_manifest

    async def counting(settings: Any, tenant_id: str, name: str, **kw: Any) -> Any:
        resolved.append(name)
        return await real(settings, tenant_id, name, **kw)

    monkeypatch.setattr(runtime, "resolve_tenant_manifest", counting)
    shared = {
        "e2e-a": _router("e2e-a", ["e2e-b", "e2e-c"]),
        "e2e-b": _router("e2e-b", ["e2e-d"]),
        "e2e-c": _router("e2e-c", ["e2e-d"]),
        "e2e-d": _agent("e2e-d"),
    }
    async with boot(
        [ScriptedTurn(content="e2e-b"), ScriptedTurn(content="e2e-d"), ScriptedTurn(content="ok")],
        manifests=shared,
    ) as app:
        resp = await _chat(app, "e2e-a")
        assert resp.status_code == 200, resp.text
    assert resolved.count("e2e-d") == 1

    deep = {f"e2e-l{i}": _router(f"e2e-l{i}", [f"e2e-l{i + 1}"]) for i in range(6)}
    deep["e2e-l6"] = _agent("e2e-l6")
    async with boot(manifests=deep) as app:
        with pytest.raises(ValueError, match="nest deeper than 4"):
            await _chat(app, "e2e-l0")
