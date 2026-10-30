"""Plugin seam — core never imports optional feature packages.

Optional packages register routes, tools, auth modes, cron tasks, agent-loop
hooks, audit/usage sinks, and rate-limit keys via the registry (or the
``felix.plugins`` entry-point group).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from fastapi import FastAPI

    from felix.config import Settings
    from felix.destinations import DestinationAdapter
    from felix.hooks import (
        AfterModelHook,
        AfterToolHook,
        BeforeCompactHook,
        BeforeModelHook,
        BeforeToolHook,
        BeforeTurnHook,
        CompactFailedHook,
        FilterHistoryHook,
    )
    from felix.tools.types import Tool

logger = logging.getLogger("felix.plugins")

AuthenticatorBuilder = Callable[["Settings"], Any]
ToolFactory = Callable[[], "Tool"]
CronRunner = Callable[..., Awaitable[None]]


@dataclass
class PluginCronTask:
    name: str
    run: CronRunner


#: The statuses a durable run ends in (`durability.fibers.FIBER_TERMINAL_STATUSES`).
RunEnd = Literal["completed", "failed", "expired", "dead"]


@dataclass(frozen=True, slots=True, kw_only=True)
class RunFinished:
    """A durable run that reached a terminal status, as a `register_run_finished` hook sees it.

    `origin` is what the plugin that started the run attached to it
    (`start_durable_chat(origin={"plugin": <name>, ...})`): its way back to the conversation that
    asked. `answer` and `error` are empty when there is none. Keyword-only, so a field added here
    breaks no plugin.
    """

    tenant_id: str
    thread_id: str
    run_id: str
    status: RunEnd
    answer: str
    error: str
    origin: Mapping[str, str]


RunFinishedHook = Callable[["Settings", RunFinished], Awaitable[None]]


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
    _plugins: list[Any] = field(default_factory=list)
    _audit_sink_factory: Callable[[], Any] | None = None
    _audit_sink: Any | None = None
    _usage_sink_factory: Callable[[Settings], Any] | None = None
    _startup_hooks: list[Callable[..., Awaitable[Any]]] = field(default_factory=list)
    _run_finished_hooks: list[tuple[str, RunFinishedHook]] = field(default_factory=list)
    _manifest_dirs: list[Path] = field(default_factory=list)
    _loaded: bool = False

    def register_plugin(self, plugin: Any) -> None:
        self._plugins.append(plugin)

    def register_manifest_dir(self, path: str | Path) -> None:
        """A directory of manifests the plugin ships -- example agents built on its tools -- served
        beside the bundled ones (`manifests.loader`), so installing the plugin is what lists them
        and an install without it never offers an agent whose tools it lacks."""
        root = Path(path)
        if not root.is_absolute():
            # A relative path would resolve against whichever process looks it up -- for the
            # CLI, whatever checkout it runs in.
            raise ValueError(f"a plugin's manifest directory must be an absolute path, not {str(path)!r}")
        root = root.resolve()
        if root not in self._manifest_dirs:
            self._manifest_dirs.append(root)

    def manifest_dirs(self) -> list[Path]:
        return list(self._manifest_dirs)

    def register_authenticator(self, mode: str, builder: AuthenticatorBuilder) -> None:
        self._authenticators[mode] = builder

    def authenticator_builder(self, mode: str) -> AuthenticatorBuilder | None:
        return self._authenticators.get(mode)

    @property
    def plugins(self) -> list[Any]:
        return list(self._plugins)

    def register_audit_sink(self, factory: Callable[[], Any]) -> None:
        self._audit_sink_factory = factory
        self._audit_sink = None

    def audit_sink_factory(self) -> Callable[[], Any] | None:
        return self._audit_sink_factory

    def audit_sink(self) -> Any | None:
        """The constructed sink, built once.

        `record_event` runs per tool call and per turn, so building the sink per
        event would hand a plugin that opens an HTTP client in its factory one
        client per audit event, on the event loop.
        """
        if self._audit_sink is None and self._audit_sink_factory is not None:
            self._audit_sink = self._audit_sink_factory()
        return self._audit_sink

    def register_usage_sink(self, factory: Callable[[Settings], Any]) -> None:
        """`factory(settings)` returns an object whose `record(*, tenant_id, manifest_id,
        model_id, usage)` is called once per metered model call. A `record` that also names
        `thread_id` (or takes `**kwargs`) is passed the call's `{tenant}:{suffix}` thread, `''`
        outside one; a sink that does not is called exactly as before."""
        self._usage_sink_factory = factory

    def usage_sink_factory(self) -> Callable[[Settings], Any] | None:
        return self._usage_sink_factory

    def register_model_provider(self, name: str, factory: Callable[..., Any]) -> None:
        """Register a model provider, selected by `provider` in `FELIX_MODEL_ROUTES`.

        Forwarded to the model registry rather than stored here, like the agent-loop hooks
        below. It exists because a plugin previously had to reach into
        `felix.patterns.model_registry` itself: the registry was open, but it was the one
        open registry with no seam on this object and no example in the reference plugin,
        so the documented way to add a provider was to read the source.
        """
        from felix.patterns.model_registry import register_model_provider

        register_model_provider(name, factory)

    def register_decision_provider(self, name: str, factory: Callable[..., Any]) -> None:
        """Register a decision provider, selected by `provider` in `FELIX_DECISION_ROUTES`.

        Forwarded to `felix_ai.decide`'s registry, like `register_model_provider` above. The
        factory takes `(model_id, wire_model, options, settings)`, where `options` is the
        provider's entry in `FELIX_MODEL_PROVIDER_OPTIONS`.
        """
        from felix.decisions import register_decision_provider

        register_decision_provider(name, factory)

    def register_destination_kind(self, kind: str, adapter: DestinationAdapter) -> None:
        """Register a destination kind, selected by `kind` in `FELIX_DESTINATIONS`: `adapter` has
        `validate(settings, tenant_id, dest)` and an async `deliver(settings, tenant_id, dest, post)
        -> Delivery`, `dest` the typed `felix.destinations.Destination`, within
        `felix.destinations.DELIVERY_BUDGET_S` (`felix.destinations.DestinationAdapter`). An HTTP
        kind classifies with `delivery_for_status` / `delivery_for_send_error`.

        Forwarded to the destination registry, like `register_model_provider` above: `post`
        (`felix.destinations.tool`) reads it when a manifest naming the destination compiles and
        again when a post is delivered."""
        from felix.destinations import register_destination_kind

        register_destination_kind(kind, adapter)

    def register_startup_hook(self, hook: Callable[..., Awaitable[Any]]) -> None:
        self._startup_hooks.append(hook)

    @property
    def startup_hooks(self) -> list[Callable[..., Awaitable[Any]]]:
        return list(self._startup_hooks)

    def register_run_finished(self, hook: RunFinishedHook, *, origin: str) -> None:
        """`hook(settings, finished)` runs in the worker once a durable run that `origin` started --
        one whose `start_durable_chat(origin=...)` named `{"plugin": origin}` -- reaches a terminal
        status, after that status is saved. Only those: a hook never sees another plugin's runs, or
        a run nobody tagged, so it never holds an answer it was not owed.

        At most once today, so a worker that dies between the save and the hook does not call it;
        write a hook to be idempotent on `run_id`, since a retried delivery may come later. A hook
        that raises or outlives `fibers.RUN_FINISHED_HOOK_TIMEOUT_S` is logged and the next runs."""
        if not origin:
            raise ValueError("register_run_finished needs the origin its runs are tagged with")
        self._run_finished_hooks.append((origin, hook))

    def run_finished_hooks(self, origin: str) -> list[RunFinishedHook]:
        """The hooks registered for runs `origin` started."""
        return [hook for owner, hook in self._run_finished_hooks if owner == origin]

    def register_before_turn(self, hook: BeforeTurnHook) -> None:
        from felix.hooks import get_agent_hooks

        get_agent_hooks().register_before_turn(hook)

    def register_filter_history(self, hook: FilterHistoryHook) -> None:
        from felix.hooks import get_agent_hooks

        get_agent_hooks().register_filter_history(hook)

    def register_before_compact(self, hook: BeforeCompactHook) -> None:
        from felix.hooks import get_agent_hooks

        get_agent_hooks().register_before_compact(hook)

    def register_before_tool(self, hook: BeforeToolHook) -> None:
        from felix.hooks import get_agent_hooks

        get_agent_hooks().register_before_tool(hook)

    def register_after_tool(self, hook: AfterToolHook) -> None:
        from felix.hooks import get_agent_hooks

        get_agent_hooks().register_after_tool(hook)

    def register_compact_failed(self, hook: CompactFailedHook) -> None:
        from felix.hooks import get_agent_hooks

        get_agent_hooks().register_compact_failed(hook)

    def register_before_model(self, hook: BeforeModelHook) -> None:
        from felix.hooks import get_agent_hooks

        get_agent_hooks().register_before_model(hook)

    def register_after_model(self, hook: AfterModelHook) -> None:
        from felix.hooks import get_agent_hooks

        get_agent_hooks().register_after_model(hook)


_registry = PluginRegistry()
_load_lock = threading.RLock()


def get_registry() -> PluginRegistry:
    return _registry


def load_optional_plugins(reg: PluginRegistry | None = None) -> bool:
    """Load optional plugins from ``felix.plugins`` entry points if installed.

    Core never hard-imports third-party packages. Install a package that
    declares an entry point in group ``felix.plugins``; its ``register(registry)``
    callable is invoked once at startup.
    """
    registry = reg or _registry
    # Held while plugins register: the manifest loader loads them lazily, and a second thread
    # arriving mid-load must not read a half-filled registry. Re-entrant, so a plugin's
    # `register` reaching back here finds `_loaded` and returns.
    with _load_lock:
        return _load(registry)


def _load(registry: PluginRegistry) -> bool:
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
        if not callable(register):
            continue
        try:
            register(registry)
        except Exception:
            # Guarding `ep.load()` but not `register()` left the front door of the
            # seam as its one undefended call site, while every consumer downstream
            # (tool registration, agent hooks, sinks, startup hooks) is defensive.
            # One misconfigured plugin would take down whichever process loaded it,
            # including `Settings.validate_runtime`, whose job is a legible error.
            logger.exception("plugin entry point %s failed during register()", ep.name)
            continue
        loaded = True
        logger.info("loaded optional plugin entry point %s", ep.name)
    return loaded
