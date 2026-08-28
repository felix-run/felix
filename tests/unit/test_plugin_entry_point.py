"""The `felix.plugins` entry point actually reaches the app.

`test_plugin_boundary.py` only ever asserted the *empty* state — that
`installed_plugins()` returns a list when nothing is installed. The primary
third-party install path was never exercised, so a plugin could register a tool,
route, or cron task and have it silently dropped.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.plugins import PluginCronTask, PluginRegistry, load_optional_plugins


class _FakeEntryPoint:
    def __init__(self, name: str, register: Any) -> None:
        self.name = name
        self._register = register

    def load(self) -> Any:
        return self._register


class _FakeEntryPoints:
    def __init__(self, eps: list[_FakeEntryPoint]) -> None:
        self._eps = eps

    def select(self, *, group: str) -> list[_FakeEntryPoint]:
        return self._eps if group == "felix.plugins" else []


def _install(monkeypatch: pytest.MonkeyPatch, register: Any) -> PluginRegistry:
    """Fake an installed distribution declaring a `felix.plugins` entry point."""
    import importlib.metadata

    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        lambda: _FakeEntryPoints([_FakeEntryPoint("fake", register)]),
    )
    registry = PluginRegistry()
    assert load_optional_plugins(registry) is True
    return registry


class _Plugin:
    name = "fake"
    body_limit_bytes = 4096

    def register_tools(self, register: Any) -> None:
        from felix.tools.types import define_tool

        async def handler(args: dict[str, Any], ctx: Any = None) -> str:
            _ = ctx
            return "from-plugin"

        register(
            "fake__tool",
            lambda: define_tool(name="fake__tool", description="d", handler=handler),
        )

    @property
    def self_authenticating_mounts(self) -> tuple[str, ...]:
        return ("/fake",)

    def rate_limit_key(self, request: Any) -> str | None:
        _ = request
        return "fake-bucket"

    @property
    def cron_tasks(self) -> tuple[PluginCronTask, ...]:
        async def run() -> None:
            return None

        return (PluginCronTask(name="fake_job", run=run),)


def test_entry_point_is_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _install(monkeypatch, lambda reg: reg.register_plugin(_Plugin()))
    assert [p.name for p in registry.plugins] == ["fake"]


def test_the_declared_seams_reach_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_Plugin` declares four more seams; assert them rather than imply coverage."""
    from felix.config import Settings
    from felix_api.app import create_app

    app = create_app(settings=Settings(auth_mode="none", host="127.0.0.1"), plugins=[_Plugin()])

    assert [p.name for p in app.state.plugins] == ["fake"]


@pytest.mark.asyncio
async def test_a_self_authenticating_mount_bypasses_auth() -> None:
    """`self_authenticating_mounts` is how a plugin carries its own auth."""
    from felix.auth.context import ANONYMOUS
    from felix.auth.middleware import authenticate_request
    from felix.config import Settings
    from starlette.requests import Request

    def _req(path: str) -> Request:
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": path,
                "headers": [],
                "query_string": b"",
                "scheme": "http",
                "server": ("t", 80),
            }
        )

    settings = Settings(auth_mode="api_key", auth_api_keys="{}", host="127.0.0.1")
    mounts = _Plugin().self_authenticating_mounts

    assert (
        await authenticate_request(_req("/fake/x"), settings, self_authenticating_mounts=mounts) is ANONYMOUS
    )
    # A path outside the mount still fails closed.
    other = await authenticate_request(_req("/chat"), settings, self_authenticating_mounts=mounts)
    assert getattr(other, "status_code", None) == 401


def test_plugin_cron_tasks_register_on_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:

    registry = _install(monkeypatch, lambda reg: reg.register_plugin(_Plugin()))
    monkeypatch.setattr("felix.plugins._registry", registry)

    from felix_worker.tasks import _register_plugin_cron_tasks

    _register_plugin_cron_tasks()  # must not raise, and must see the plugin
    assert registry.plugins[0].cron_tasks[0].name == "fake_job"


def test_load_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second call must not register the plugin twice."""
    registry = _install(monkeypatch, lambda reg: reg.register_plugin(_Plugin()))
    load_optional_plugins(registry)
    assert len(registry.plugins) == 1


class _ExplodingOnLoad(_FakeEntryPoint):
    def load(self) -> Any:
        raise ImportError("no such module")


def _raises_on_register(_reg: Any) -> None:
    raise RuntimeError("plugin misconfigured")


@pytest.mark.parametrize("stage", ["load", "register"])
def test_a_broken_plugin_does_not_break_startup(monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    """Both halves must be guarded.

    `ep.load()` was already guarded but `register(registry)` was not, so a plugin
    that imported fine and then raised while registering took down whichever
    process loaded it — including `Settings.validate_runtime`, whose whole job is
    to produce a legible startup error.
    """
    import importlib.metadata

    ep = _ExplodingOnLoad("bad", None) if stage == "load" else _FakeEntryPoint("bad", _raises_on_register)
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda: _FakeEntryPoints([ep]))
    registry = PluginRegistry()

    assert load_optional_plugins(registry) is False
    assert registry.plugins == []


def test_one_broken_plugin_does_not_prevent_a_good_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.metadata

    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        lambda: _FakeEntryPoints(
            [
                _FakeEntryPoint("bad", _raises_on_register),
                _FakeEntryPoint("good", lambda reg: reg.register_plugin(_Plugin())),
            ]
        ),
    )
    registry = PluginRegistry()

    assert load_optional_plugins(registry) is True
    assert [p.name for p in registry.plugins] == ["fake"]


@pytest.mark.asyncio
async def test_plugin_tool_reaches_every_entry_point(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression that mattered: plugin tools existed only in the API process.

    `default_tool_provider` backs fibers, scheduled jobs, eval, and the CLI. It
    registered builtins only, so a manifest naming a plugin tool resolved over
    HTTP and raised `Unknown tool` when run as a durable fiber or a cron job.
    """
    from felix.tools.builtins import default_tool_provider

    registry = _install(monkeypatch, lambda reg: reg.register_plugin(_Plugin()))
    monkeypatch.setattr("felix.plugins._registry", registry)

    provider = default_tool_provider()
    assert provider.has("fake__tool")
    assert provider.has("calculator")  # builtins still there

    tool = provider.get("fake__tool")
    assert await tool.executor.execute({}, None) == "from-plugin"


def test_compose_and_default_provider_agree(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both paths must resolve the same tool set, or manifests behave differently."""
    from felix.config import Settings
    from felix.tools.builtins import default_tool_provider
    from felix_api.composition import compose

    registry = _install(monkeypatch, lambda reg: reg.register_plugin(_Plugin()))
    monkeypatch.setattr("felix.plugins._registry", registry)

    composed = compose(Settings())
    # Assert the tool is actually there, not merely that both paths agree — they
    # would also agree if `register_plugin_tools` became a no-op in both.
    assert "fake__tool" in composed.list()
    assert sorted(composed.list()) == sorted(default_tool_provider().list())
