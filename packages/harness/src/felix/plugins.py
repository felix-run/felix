"""Plugin seam — core never imports optional feature packages.

Optional packages register routes, tools, auth modes, cron tasks, and
rate-limit keys via the registry (or ``felix.plugins`` entry points).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from fastapi import APIRouter, FastAPI

    from felix.config import Settings
    from felix.tools.types import Tool

logger = logging.getLogger("felix.plugins")

AuthenticatorBuilder = Callable[["Settings"], Any]
ToolFactory = Callable[[], "Tool"]
CronRunner = Callable[..., Awaitable[None]]


@dataclass
class PluginCronTask:
    name: str
    run: CronRunner


class FelixPlugin(Protocol):
    name: str

    def routes(self, app: FastAPI, *, tools: Any) -> None: ...
    def register_tools(self, register: Callable[[str, ToolFactory], None]) -> None: ...

    @property
    def self_authenticating_mounts(self) -> tuple[str, ...]: ...

    def rate_limit_key(self, request: Any) -> str | None: ...

    @property
    def body_limit_bytes(self) -> int | None: ...

    @property
    def cron_tasks(self) -> tuple[PluginCronTask, ...]: ...


@dataclass
class PluginRegistry:
    """Process-wide registry; optional packages populate at startup."""

    _authenticators: dict[str, AuthenticatorBuilder] = field(default_factory=dict)
    _routers: list[Any] = field(default_factory=list)
    _plugins: list[Any] = field(default_factory=list)
    _audit_sink_factory: Callable[[], Any] | None = None
    _usage_sink_factory: Callable[[Settings], Any] | None = None
    _startup_hooks: list[Callable[..., Awaitable[Any]]] = field(default_factory=list)
    _loaded: bool = False

    def register_plugin(self, plugin: Any) -> None:
        self._plugins.append(plugin)

    def register_authenticator(self, mode: str, builder: AuthenticatorBuilder) -> None:
        self._authenticators[mode] = builder

    def authenticator_builder(self, mode: str) -> AuthenticatorBuilder | None:
        return self._authenticators.get(mode)

    def register_router(self, router: APIRouter) -> None:
        self._routers.append(router)

    @property
    def routers(self) -> list[Any]:
        return list(self._routers)

    @property
    def plugins(self) -> list[Any]:
        return list(self._plugins)

    def register_audit_sink(self, factory: Callable[[], Any]) -> None:
        self._audit_sink_factory = factory

    def register_usage_sink(self, factory: Callable[[Settings], Any]) -> None:
        self._usage_sink_factory = factory

    def register_startup_hook(self, hook: Callable[..., Awaitable[Any]]) -> None:
        self._startup_hooks.append(hook)

    def register_before_turn(self, hook: Callable[..., Any]) -> None:
        from felix.hooks import get_agent_hooks

        get_agent_hooks().register_before_turn(hook)

    def register_filter_history(self, hook: Callable[..., Any]) -> None:
        from felix.hooks import get_agent_hooks

        get_agent_hooks().register_filter_history(hook)

    def register_before_compact(self, hook: Callable[..., Any]) -> None:
        from felix.hooks import get_agent_hooks

        get_agent_hooks().register_before_compact(hook)

    def register_before_tool(self, hook: Callable[..., Any]) -> None:
        from felix.hooks import get_agent_hooks

        get_agent_hooks().register_before_tool(hook)

    def register_after_tool(self, hook: Callable[..., Any]) -> None:
        from felix.hooks import get_agent_hooks

        get_agent_hooks().register_after_tool(hook)

    def register_compact_failed(self, hook: Callable[..., Any]) -> None:
        from felix.hooks import get_agent_hooks

        get_agent_hooks().register_compact_failed(hook)


_registry = PluginRegistry()


def get_registry() -> PluginRegistry:
    return _registry


def installed_plugins() -> list[Any]:
    """The ONLY core-side line that may list plugins (composition seat)."""
    return list(_registry.plugins)


def load_optional_plugins(reg: PluginRegistry | None = None) -> bool:
    """Load optional plugins from ``felix.plugins`` entry points if installed.

    Core never hard-imports third-party packages. Install a package that
    declares an entry point in group ``felix.plugins``; its ``register(registry)``
    callable is invoked once at startup.
    """
    registry = reg or _registry
    if registry._loaded:
        return bool(registry.plugins)
    registry._loaded = True
    loaded = False
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover
        return False

    eps = entry_points()
    if hasattr(eps, "select"):
        selected = eps.select(group="felix.plugins")
    else:  # pragma: no cover — Python <3.10 style
        selected = eps.get("felix.plugins", ())
    for ep in selected:
        try:
            register = ep.load()
        except Exception:
            logger.exception("failed loading plugin entry point %s", ep.name)
            continue
        if callable(register):
            register(registry)
            loaded = True
            logger.info("loaded optional plugin entry point %s", ep.name)
    return loaded
