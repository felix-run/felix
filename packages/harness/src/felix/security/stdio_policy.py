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
import shutil
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
        # Where an allowlisted command finds what it runs: `npx` and `uvx` start their interpreter
        # through `#!/usr/bin/env node`, which searches PATH. The child gets the API's own.
        "PATH",
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
    _allowed_entry(command, settings)


def _allowed_entry(command: str, settings: Any) -> str | None:
    """The realpath the allowlist matched ``command`` through, or None for an exact-string match.
    Raises :class:`StdioNotAllowedError` when neither matches."""
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
        return None
    resolved = os.path.realpath(candidate) if os.path.sep in candidate else ""
    if resolved and resolved in allowed:
        return resolved
    raise StdioNotAllowedError(f"MCP stdio command {candidate!r} is not in FELIX_MCP_STDIO_ALLOWED_COMMANDS")


def resolve_stdio_command(command: str, settings: Any) -> str:
    """The absolute path an allowed ``command`` runs, resolved on the *API's* own PATH.

    The allowlist approves a string. Spawning a bare name lets the exec look it up on the child's
    PATH, so the binary that ran was whichever ``uvx`` came first in directories the manifest could
    choose. Resolving here, against the operator's environment, and spawning the result means the
    allowlist's ``uvx`` is the API host's ``uvx``. A path is taken as written, made absolute.

    An entry the operator listed is spawned as listed, symlinks and all: following a virtualenv's
    ``python`` to the base interpreter loses the environment's packages. A manifest path that only
    matched through its realpath spawns that realpath -- the file the operator listed -- not the
    manifest's link to it, whose location can change what runs (a `pyvenv.cfg` beside it, a
    multi-call binary dispatching on its name).
    """
    through = _allowed_entry(command, settings)
    candidate = command.strip()
    if through is not None:
        resolved = through
    elif os.path.sep in candidate:
        resolved = os.path.abspath(candidate)
    else:
        resolved = os.path.abspath(found) if (found := shutil.which(candidate, path=_api_path())) else ""
    if not resolved or not os.path.isfile(resolved) or not os.access(resolved, os.X_OK):
        raise StdioNotAllowedError(f"MCP stdio command {candidate!r} is not an executable on the API's PATH")
    return resolved


def _api_path() -> str:
    return absolute_path_entries(os.environ.get("PATH", os.defpath))


def absolute_path_entries(path: str) -> str:
    """``path`` without its relative entries. A relative entry -- `.`, an empty one, a
    `node_modules/.bin` or `.venv/bin` common on a developer host -- resolves against the child's
    cwd, which a manifest (or a model, for a shell tool) chose; an absolute one does not."""
    return os.pathsep.join(p for p in path.split(os.pathsep) if os.path.isabs(p))


def stdio_child_env(ref_env: dict[str, str] | None) -> dict[str, str]:
    """Build a minimal child environment.

    The parent environment holds model API keys, the Postgres URL, and cloud
    credentials, so it is never copied wholesale — only a small base plus the keys the
    manifest declared (with resolved ``secret:NAME`` values already substituted).
    """
    env = {k: os.environ[k] for k in _BASE_ENV_KEYS if k in os.environ}
    if "PATH" in env:
        env["PATH"] = absolute_path_entries(env["PATH"])
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
    "absolute_path_entries",
    "allowed_commands",
    "assert_stdio_command_allowed",
    "describe_allowlist",
    "resolve_stdio_command",
    "stdio_child_env",
]
