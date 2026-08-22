"""Compose — wiring root for tools and plugins.

``installed_plugins()`` is the ONLY core-side line that may list plugins.
Removing a feature is deleting its entry (or uninstalling the optional package
that ``load_optional_plugins`` discovers). Tool registration for plugins
happens here; route mounting and cron tasks consume the same list.
"""

from __future__ import annotations

from typing import Any

from felix.config import Settings
from felix.plugins import get_registry, load_optional_plugins
from felix.tools.builtins import register_builtin_tools
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

    for plugin in installed_plugins():
        register_tools = getattr(plugin, "register_tools", None)
        if callable(register_tools):
            register_tools(provider.register)

    return provider
