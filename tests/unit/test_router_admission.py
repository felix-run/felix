"""What a composite may name as a child, decided at compile.

A router forwards the caller's turn to its child without the child's inbound check, so the
compile refuses an anonymous composite over a child that refuses anonymous callers. The e2e case
(`tests/e2e/test_sub_agents.py`) covers a child that says `allow_anonymous: false`. These cover the
rest of the door, and the one way the check could read the wrong door: a child found under one
name whose manifest calls itself another.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from felix.manifests.builder import BuildDeps, build_agent
from felix.manifests.loader import load_bundled, parse_manifest
from felix.tools.builtins import default_tool_provider

BUNDLED = Path(__file__).resolve().parents[2] / "manifests"


def _manifest(name: str, *, anonymous: bool, scopes: list[str] | None = None, **spec: Any) -> Any:
    inbound: dict[str, Any] = {"allow_anonymous": anonymous}
    if scopes:
        inbound["required_scopes"] = scopes
    body: dict[str, Any] = {"pattern": "react", "auth": {"inbound": inbound}}
    body.update(spec)
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": name}, "spec": body}
    )


def _router(
    name: str, children: list[str], *, anonymous: bool = True, scopes: list[str] | None = None
) -> Any:
    return _manifest(
        name,
        anonymous=anonymous,
        scopes=scopes,
        pattern="router",
        sub_agents=children,
        system_prompt={"inline": "Route."},
    )


async def _compile(root: Any, children: dict[str, Any]) -> Any:
    """`root` compiled with each child name resolved from `children`, as a tenant store would."""
    deps = BuildDeps(tools=default_tool_provider())

    async def builder(name: str) -> Any:
        return await build_agent(children[name], deps=deps)

    deps.sub_agent_builder = builder
    return await build_agent(root, deps=deps)


async def test_a_child_closed_by_its_scopes_is_closed() -> None:
    # `allow_anonymous: true` with a required scope: an anonymous caller asking `ops` by name is
    # refused, so a router may not hand it one.
    children = {"ops": _manifest("ops", anonymous=True, scopes=["ops:run"])}
    with pytest.raises(ValueError, match="sub_agents ops do not"):
        await _compile(_router("anon-router", ["ops"]), children)


async def test_only_the_closed_children_are_named() -> None:
    children = {"open": _manifest("open", anonymous=True), "shut": _manifest("shut", anonymous=False)}
    with pytest.raises(ValueError, match="sub_agents shut do not;"):
        await _compile(_router("anon-router", ["open", "shut"]), children)


async def test_a_child_under_another_name_is_refused_even_when_open() -> None:
    # Named `ops` by the router, compiled from a manifest calling itself `ops-internal`.
    children = {"ops": _manifest("ops-internal", anonymous=True)}
    with pytest.raises(ValueError, match="child agent 'ops' resolved to a manifest named 'ops-internal'"):
        await _compile(_router("anon-router", ["ops"]), children)


async def test_a_misnamed_grandchild_cannot_reopen_a_siblings_door() -> None:
    # `b` is closed. `q`, a closed router, names `a`, whose manifest calls itself `b` and is open:
    # compiled, it would replace `b`'s entry, and the anonymous root -- checking `b` after its
    # siblings compiled -- would read the impostor's open door and forward to the real, closed `b`.
    children = {
        "b": _manifest("b", anonymous=False),
        "q": _router("q", ["a"], anonymous=False),
        "a": _manifest("b", anonymous=True),
    }
    with pytest.raises(ValueError, match="names must match"):
        await _compile(_router("anon-root", ["b", "q"]), children)


async def test_an_open_child_under_its_own_name_compiles() -> None:
    children = {"ops": _manifest("ops", anonymous=True)}
    agent = await _compile(_router("anon-router", ["ops"]), children)
    assert set(agent.sub_agents) == {"ops"}  # type: ignore[attr-defined]


async def test_a_closed_router_may_name_a_closed_child() -> None:
    agent = await _compile(
        _router("closed-router", ["ops"], anonymous=False), {"ops": _manifest("ops", anonymous=False)}
    )
    assert set(agent.sub_agents) == {"ops"}  # type: ignore[attr-defined]


async def test_a_router_closed_by_its_scopes_may_name_a_closed_child() -> None:
    # The parent's door is read the same way: a scope requirement already keeps anonymous out.
    router = _router("scoped-router", ["ops"], anonymous=True, scopes=["ops:run"])
    agent = await _compile(router, {"ops": _manifest("ops", anonymous=False)})
    assert set(agent.sub_agents) == {"ops"}  # type: ignore[attr-defined]


_COMPOSITES = sorted(
    p.stem for p in BUNDLED.glob("*.yaml") if load_bundled(p.stem, bundled_dir=BUNDLED).spec.sub_agents
)


def test_the_bundled_composites_are_found() -> None:
    assert {"router", "hybrid-router"} <= set(_COMPOSITES)


@pytest.mark.parametrize("name", _COMPOSITES)
async def test_every_bundled_composite_compiles_over_its_bundled_children(name: str) -> None:
    # The check runs at request time, so a bundled child whose door drifts would break its parent
    # in production with nothing else failing.
    agent = await build_agent(
        load_bundled(name, bundled_dir=BUNDLED), deps=BuildDeps(tools=default_tool_provider())
    )
    assert set(agent.sub_agents) == set(load_bundled(name, bundled_dir=BUNDLED).spec.sub_agents)  # type: ignore[attr-defined]
