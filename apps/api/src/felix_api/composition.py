"""Compose — wiring root for tools and plugins.

``installed_plugins()`` is the ONLY core-side line that may list plugins.
Removing a feature is deleting its entry (or uninstalling the optional package
that ``load_optional_plugins`` discovers). Plugin tool registration is shared
with ``felix.tools.builtins.register_plugin_tools`` so every entry point — API,
fibers, scheduled jobs, eval, CLI — resolves the same tool set; the worker
registers plugin ``cron_tasks`` on startup.
"""

from __future__ import annotations

from typing import Any

from felix.config import Settings
from felix.plugins import get_registry, load_optional_plugins
from felix.tools.builtins import register_builtin_tools, register_plugin_tools
from felix.tools.provider import InMemoryToolProvider, ToolProvider


def installed_plugins() -> list[Any]:
    """Return installed feature plugins after optional-package discovery.

    This is the composition seat — the only place core names plugins.
    """
    load_optional_plugins()
    return list(get_registry().plugins)


def compose(settings: Settings) -> ToolProvider:
    """Build the process-wide ToolProvider and register builtins + plugin tools."""
    _ = settings  # reserved for env-gated tool wiring
    provider = InMemoryToolProvider()
    register_builtin_tools(provider)
    # Shared with `default_tool_provider`, so fibers, scheduled jobs, eval, and the
    # CLI resolve the same tool set this process does.
    register_plugin_tools(provider)

    return provider
