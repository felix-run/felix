"""`pin_compile` covers the sub-agents a router compiles, not the router alone.

Children resolve from the tenant's store since #317, so they can be edited mid-thread. A pin
that hashed only the parent let a pinned thread pick up an edited child's tools and policies on
its next turn. These drive two turns on one pinned thread through the real stack, with and
without an edit to the child in between.
"""

from __future__ import annotations

from typing import Any

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
