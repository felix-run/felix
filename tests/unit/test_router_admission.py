"""An anonymous composite may not name a child whose door it cannot read as open.

The e2e case (`tests/e2e/test_sub_agents.py`) covers a child that says it is closed. These cover
the child the build cannot read: one whose compiled agent carries another name — a bundled file
whose `metadata.name` is not its file name — which the check used to skip, leaving the router an
anonymous door to it.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.manifests.builder import BuildDeps, build_agent
from felix.manifests.loader import parse_manifest
from felix.tools.builtins import default_tool_provider


def _manifest(name: str, *, anonymous: bool, **spec: Any) -> Any:
    body: dict[str, Any] = {"pattern": "react", "auth": {"inbound": {"allow_anonymous": anonymous}}}
    body.update(spec)
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": name}, "spec": body}
    )


def _router(children: list[str]) -> Any:
    return _manifest(
        "anon-router",
        anonymous=True,
        pattern="router",
        sub_agents=children,
        system_prompt={"inline": "Route."},
    )


async def _compile(router: Any, children: dict[str, Any]) -> Any:
    deps = BuildDeps(tools=default_tool_provider())

    async def builder(name: str) -> Any:
        return await build_agent(children[name], deps=deps)

    deps.sub_agent_builder = builder
    return await build_agent(router, deps=deps)


async def test_a_child_compiled_under_another_name_counts_as_closed() -> None:
    # Named `ops` by the router, compiled from a manifest calling itself `ops-internal`.
    children = {"ops": _manifest("ops-internal", anonymous=False)}
    with pytest.raises(ValueError, match="sub_agents ops do not"):
        await _compile(_router(["ops"]), children)


async def test_an_open_child_under_its_own_name_compiles() -> None:
    children = {"ops": _manifest("ops", anonymous=True)}
    agent = await _compile(_router(["ops"]), children)
    assert set(agent.sub_agents) == {"ops"}  # type: ignore[attr-defined]


async def test_a_closed_router_may_name_a_closed_child() -> None:
    router = _manifest(
        "closed-router",
        anonymous=False,
        pattern="router",
        sub_agents=["ops"],
        system_prompt={"inline": "Route."},
    )
    agent = await _compile(router, {"ops": _manifest("ops", anonymous=False)})
    assert set(agent.sub_agents) == {"ops"}  # type: ignore[attr-defined]
