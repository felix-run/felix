"""`Tool.egress_hosts`: a tool that names the hosts it reaches is held to its agent's allow_hosts.

Plugin tools are not handed the agent's egress bound when they run, so before this the bound
covered `http_tools` alone and a plugin tool reached its host under any `network.allow_hosts`.
These compile through `build_agent` with a provider holding one such tool, as a plugin would.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.manifests.builder import BuildDeps, build_agent
from felix.manifests.loader import parse_manifest
from felix.tools.provider import InMemoryToolProvider
from felix.tools.types import define_tool_with_executor


class _Executor:
    transport = "plugin"

    async def execute(self, args: Any, ctx: Any = None) -> str:
        return "ok"


def _provider(hosts: frozenset[str]) -> InMemoryToolProvider:
    provider = InMemoryToolProvider()
    provider.register(
        "forecast",
        lambda: define_tool_with_executor(
            name="forecast", description="A forecast.", executor=_Executor(), egress_hosts=hosts
        ),
    )
    return provider


def _manifest(allow_hosts: list[str] | None, name: str = "weather-agent") -> Any:
    spec: dict[str, Any] = {"pattern": "react", "tools": ["forecast"]}
    if allow_hosts is not None:
        spec["network"] = {"allow_hosts": allow_hosts}
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": name}, "spec": spec}
    )


async def test_a_tool_reaching_a_host_outside_allow_hosts_is_refused() -> None:
    deps = BuildDeps(tools=_provider(frozenset({"api.open-meteo.com"})))
    with pytest.raises(ValueError, match=r"forecast \(api\.open-meteo\.com\)"):
        await build_agent(_manifest(["example.com"]), deps=deps)


async def test_a_tool_whose_hosts_are_allowed_compiles() -> None:
    deps = BuildDeps(tools=_provider(frozenset({"api.open-meteo.com"})))
    agent = await build_agent(_manifest(["*.open-meteo.com"]), deps=deps)
    assert "forecast" in {t.name for t in agent.tools}  # type: ignore[attr-defined]


async def test_without_allow_hosts_nothing_is_checked() -> None:
    deps = BuildDeps(tools=_provider(frozenset({"api.open-meteo.com"})))
    agent = await build_agent(_manifest(None), deps=deps)
    assert "forecast" in {t.name for t in agent.tools}  # type: ignore[attr-defined]


async def test_a_parents_list_bounds_a_child_that_sets_none() -> None:
    # The child's own manifest says nothing about hosts; the router above it allows one other.
    deps = BuildDeps(tools=_provider(frozenset({"api.open-meteo.com"})))
    child = _manifest(None, name="forecaster")

    async def builder(name: str) -> Any:
        return await build_agent(child, deps=deps)

    deps.sub_agent_builder = builder
    router = parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "bounded-router"},
            "spec": {
                "pattern": "router",
                "sub_agents": ["forecaster"],
                "system_prompt": {"inline": "Route."},
                "network": {"allow_hosts": ["example.com"]},
            },
        }
    )
    with pytest.raises(ValueError, match="manifest 'forecaster' binds tools that reach hosts"):
        await build_agent(router, deps=deps)


async def test_every_host_a_tool_declares_is_checked() -> None:
    deps = BuildDeps(tools=_provider(frozenset({"api.open-meteo.com", "evil.example"})))
    with pytest.raises(ValueError, match=r"forecast \(evil\.example\)$") as refused:
        await build_agent(_manifest(["*.open-meteo.com"]), deps=deps)
    assert "open-meteo" not in str(refused.value).rsplit("allow: ", 1)[1]


async def test_a_tool_that_declares_nothing_is_not_refused() -> None:
    # "Empty claims nothing": the bound does not reach an undeclared plugin tool (docs/ROADMAP.md).
    deps = BuildDeps(tools=_provider(frozenset()))
    agent = await build_agent(_manifest(["example.com"]), deps=deps)
    assert "forecast" in {t.name for t in agent.tools}  # type: ignore[attr-defined]
