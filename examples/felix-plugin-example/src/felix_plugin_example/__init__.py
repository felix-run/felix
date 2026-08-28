"""Reference third-party Felix plugin.

Install it (``uv pip install -e examples/felix-plugin-example``) and core will
discover it through the ``felix.plugins`` entry point declared in
``pyproject.toml`` — no edit to Felix is required, and core never imports this
package by name.

What this demonstrates, one seam per section below:

* a tool the model can call
* an HTTP route
* a periodic worker task
* an agent-loop hook
* a pattern
* an object-store backend
* a session strategy
* manifest config via ``spec.extensions``
"""

from __future__ import annotations

from typing import Any

PLUGIN_NAMESPACE = "example"


# --------------------------------------------------------------------------- tools
def _build_greet_tool() -> Any:
    from felix.tools.types import define_tool

    async def handler(args: dict[str, Any], ctx: Any = None) -> str:
        _ = ctx
        return f"hello, {args.get('name', 'world')}"

    return define_tool(
        name="example__greet",
        description="Greet someone by name.",
        handler=handler,
        replay_safe=True,
        # Leave `transport` at its default ("local") only for in-process work.
        # Anything that returns remote content should name its own transport, which
        # content screening then treats as untrusted by default.
    )


# --------------------------------------------------------------------------- pattern
async def _build_example_pattern(ctx: dict[str, Any]) -> Any:
    """A pattern builder. `ctx` is the PatternBuildContext dict.

    `ctx["extensions"]` carries this plugin's manifest block, namespaced by name:

        spec:
          extensions:
            example:
              greeting: "hei"
    """
    from felix.patterns.react import build_react_agent

    config = (ctx.get("extensions") or {}).get(PLUGIN_NAMESPACE) or {}
    greeting = str(config.get("greeting") or "hello")
    ctx = {**ctx, "system_prompt": f"{ctx.get('system_prompt', '')}\n\nOpen replies with {greeting!r}."}
    return await build_react_agent(ctx)


# --------------------------------------------------------------------------- backends
def _build_null_object_store(settings: Any) -> Any:
    """An object-store backend selected by ``FELIX_OBJECT_STORE=example-null``."""
    from felix.storage import MemoryObjectStore

    _ = settings
    return MemoryObjectStore()


def _build_pairs_session_strategy(arg: str, **budget: Any) -> Any:
    """Selected by ``spec.session.strategy: example-pairs`` (or ``example-pairs:4``)."""
    from felix.session.strategies import WindowedSessionStrategy

    _ = budget
    return WindowedSessionStrategy(int(arg or "2") * 2)


# --------------------------------------------------------------------------- hooks
async def _before_tool(**kwargs: Any) -> dict[str, Any] | None:
    """Return ``{"deny": True, "reason": ...}`` to block a call; None to allow.

    Runs at the tool-runner boundary, outside the governance wrapper stack — it is
    a global interceptor, not a governance slot. Order-sensitive controls belong in
    the wrapper stack in core.
    """
    _ = kwargs
    return None


# --------------------------------------------------------------------------- plugin
class ExamplePlugin:
    """Core reads these members with ``getattr``, so implement only what you need."""

    name = PLUGIN_NAMESPACE

    def register_tools(self, register: Any) -> None:
        register("example__greet", _build_greet_tool)

    def routes(self, app: Any, *, tools: Any) -> None:
        from fastapi import APIRouter

        _ = tools
        router = APIRouter()

        @router.get("/example/ping")
        async def ping() -> dict[str, str]:
            return {"plugin": PLUGIN_NAMESPACE, "status": "ok"}

        app.include_router(router)

    @property
    def self_authenticating_mounts(self) -> tuple[str, ...]:
        """Paths that carry their own auth and bypass AuthMiddleware."""
        return ()

    @property
    def body_limit_bytes(self) -> int | None:
        """Raise the request body cap; core takes the max across plugins."""
        return None

    def rate_limit_key(self, request: Any) -> str | None:
        """Return a bucket key, or None to fall through to core's."""
        _ = request
        return None

    @property
    def cron_tasks(self) -> tuple[Any, ...]:
        from felix.plugins import PluginCronTask

        async def heartbeat() -> None:
            pass

        return (PluginCronTask(name=f"{PLUGIN_NAMESPACE}_heartbeat", run=heartbeat),)


def register(registry: Any) -> None:
    """Entry point. Called once at startup with the process-wide PluginRegistry."""
    from felix.patterns.registry import register_pattern
    from felix.session.strategies import register_session_strategy
    from felix.storage import register_object_store

    registry.register_plugin(ExamplePlugin())
    registry.register_before_tool(_before_tool)

    # Open registries live in core, not on the registry object.
    register_pattern("example-echo", _build_example_pattern)
    register_object_store("example-null", _build_null_object_store)
    register_session_strategy("example-pairs", _build_pairs_session_strategy)


__all__ = ["ExamplePlugin", "register"]
