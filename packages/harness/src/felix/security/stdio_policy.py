"""Operator allowlist for MCP stdio subprocess execution.

``McpServerRef.command`` / ``args`` / ``cwd`` / ``env`` are manifest-supplied and reach
``asyncio.create_subprocess_exec`` at *compile* time, so any principal holding the
tenant-level ``manifests:write`` scope could otherwise execute arbitrary code as the API
process. stdio is therefore **disabled unless the operator names the commands allowed**,
and the child never inherits the parent environment.
"""

from __future__ import annotations

import os
import shlex
from typing import Any

# Loader/interpreter variables that turn an allowlisted command into arbitrary code.
_FORBIDDEN_ENV = frozenset(
    {
        "BASH_ENV",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "GCONV_PATH",
        "IFS",
        "LD_AUDIT",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "NODE_OPTIONS",
        "PERL5OPT",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
    }
)

# Handed to every stdio child so an allowlisted binary can still resolve itself.
_BASE_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")


class StdioNotAllowedError(ValueError):
    """Raised when a manifest asks to spawn a command the operator has not allowed."""


def allowed_commands(settings: Any) -> frozenset[str]:
    """Parse ``FELIX_MCP_STDIO_ALLOWED_COMMANDS``. Empty (the default) disables stdio."""
    raw = getattr(settings, "mcp_stdio_allowed_commands", "") or ""
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def assert_stdio_command_allowed(command: str, settings: Any) -> None:
    """Raise :class:`StdioNotAllowedError` unless ``command`` is explicitly allowed.

    Matching is on the exact string the manifest supplied *and* on its resolved absolute
    path, so neither ``/usr/bin/uvx`` nor ``uvx`` can stand in for the other unless the
    operator listed it.
    """
    allowed = allowed_commands(settings)
    if not allowed:
        raise StdioNotAllowedError(
            "MCP stdio transport is disabled. Set FELIX_MCP_STDIO_ALLOWED_COMMANDS to the "
            "exact commands this deployment may spawn."
        )
    candidate = (command or "").strip()
    if not candidate:
        raise StdioNotAllowedError("stdio MCP servers require command")
    if candidate in allowed:
        return
    resolved = os.path.realpath(candidate) if os.path.sep in candidate else ""
    if resolved and resolved in allowed:
        return
    raise StdioNotAllowedError(f"MCP stdio command {candidate!r} is not in FELIX_MCP_STDIO_ALLOWED_COMMANDS")


def stdio_child_env(ref_env: dict[str, str] | None) -> dict[str, str]:
    """Build a minimal child environment.

    The parent environment holds model API keys, the Postgres URL, and cloud
    credentials, so it is never copied wholesale — only a small base plus the keys the
    manifest declared (with resolved ``secret:NAME`` values already substituted).
    """
    env = {k: os.environ[k] for k in _BASE_ENV_KEYS if k in os.environ}
    for key, value in (ref_env or {}).items():
        name = str(key)
        if name in _FORBIDDEN_ENV:
            raise StdioNotAllowedError(f"MCP stdio env may not set {name}")
        env[name] = str(value)
    return env


def describe_allowlist(settings: Any) -> str:
    """Human-readable summary for ``felix doctor``."""
    allowed = allowed_commands(settings)
    if not allowed:
        return "disabled (no FELIX_MCP_STDIO_ALLOWED_COMMANDS)"
    return " ".join(shlex.quote(c) for c in sorted(allowed))


__all__ = [
    "StdioNotAllowedError",
    "allowed_commands",
    "assert_stdio_command_allowed",
    "describe_allowlist",
    "stdio_child_env",
]
