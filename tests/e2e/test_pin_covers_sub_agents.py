"""`pin_compile` covers the sub-agents a router compiles, not the router alone.

Children resolve from the tenant's store since #317, so they can be edited mid-thread. A pin
that hashed only the parent let a pinned thread pick up an edited child's tools and policies on
its next turn. These drive two turns on one pinned thread through the real stack, with and
without an edit to the child in between.
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


ROUTER = _agent(
    "e2e-pinned",
    pattern="router",
    sub_agents=["e2e-child"],
    system_prompt={"inline": "Route it."},
    governance={"pin_compile": True},
)


async def _turn(app: Any, text: str) -> Any:
    return await app.client.post(
        "/chat",
        json={
            "manifest": "e2e-pinned",
            "thread_id": "e2e-pin",
            "messages": [{"role": "user", "content": text}],
        },
    )


def _script() -> list[ScriptedTurn]:
    return [ScriptedTurn(content="e2e-child"), ScriptedTurn(content="one")] * 2


async def test_a_pinned_thread_refuses_a_turn_after_its_child_was_edited(boot: Any) -> None:
    from felix.manifests.store import activate_version, put_version

    manifests = {"e2e-child": _agent("e2e-child", system_prompt={"inline": "v1"}), "e2e-pinned": ROUTER}
    async with boot(_script(), manifests=manifests) as app:
        assert (await _turn(app, "first")).status_code == 200
        await put_version(
            app.settings, "default", "e2e-child", _agent("e2e-child", system_prompt={"inline": "v2"})
        )
        # Published, not only stored: a new version is inactive until activated.
        await activate_version(app.settings, "default", "e2e-child", version=2)
        resp = await _turn(app, "second")
        assert resp.status_code == 409, resp.text


async def test_a_pinned_thread_with_an_unchanged_child_carries_on(boot: Any) -> None:
    manifests = {"e2e-child": _agent("e2e-child", system_prompt={"inline": "v1"}), "e2e-pinned": ROUTER}
    async with boot(_script(), manifests=manifests) as app:
        assert (await _turn(app, "first")).status_code == 200
        assert (await _turn(app, "second")).status_code == 200


async def test_the_compile_runs_the_children_the_pin_checked(boot: Any, monkeypatch: Any) -> None:
    """The race, forced: a new child version is activated the moment the pin check has
    resolved the old one. The compile used to resolve again and run the new one, unchecked,
    for that turn; it now runs the child the pin verified."""
    from felix import runtime
    from felix.manifests.store import activate_version, put_version

    manifests = {"e2e-child": _agent("e2e-child", system_prompt={"inline": "v1"}), "e2e-pinned": ROUTER}
    async with boot(_script(), manifests=manifests) as app:
        real = runtime.resolve_tenant_manifest
        raced: list[bool] = []

        async def racing(settings: Any, tenant_id: str, name: str, **kw: Any) -> Any:
            resolved = await real(settings, tenant_id, name, **kw)
            if name == "e2e-child" and not raced:
                raced.append(True)
                v2 = _agent("e2e-child", system_prompt={"inline": "v2"})
                await put_version(settings, tenant_id, "e2e-child", v2)
                await activate_version(settings, tenant_id, "e2e-child", version=2)
            return resolved

        monkeypatch.setattr(runtime, "resolve_tenant_manifest", racing)
        assert (await _turn(app, "first")).status_code == 200
        assert raced, "the pin check resolved the child"
        [_classify, answer] = app.spy.prompts
        assert "v1" in answer[0].content and "v2" not in answer[0].content


async def test_a_child_missing_at_check_time_is_refused_even_if_it_appears(
    boot: Any, monkeypatch: Any
) -> None:
    """Found nowhere by the pin check stays found nowhere for that turn's compile."""
    from felix import runtime
    from felix.manifests.store import put_version

    router = _agent(
        "e2e-pinned",
        pattern="router",
        sub_agents=["e2e-late"],
        system_prompt={"inline": "Route it."},
        governance={"pin_compile": True},
    )
    async with boot(manifests={"e2e-pinned": router}) as app:
        real = runtime.resolve_tenant_manifest
        published: list[bool] = []

        async def racing(settings: Any, tenant_id: str, name: str, **kw: Any) -> Any:
            if name == "e2e-late" and not published:
                published.append(True)
                await put_version(settings, tenant_id, "e2e-late", _agent("e2e-late"))
                raise LookupError("Unknown manifest: e2e-late")
            return await real(settings, tenant_id, name, **kw)

        monkeypatch.setattr(runtime, "resolve_tenant_manifest", racing)
        with pytest.raises(LookupError, match="e2e-late"):
            await _turn(app, "first")
        assert app.spy.prompts == []
