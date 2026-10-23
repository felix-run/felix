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

# Variables that make an allowlisted command run or load code the manifest chose, with no file of
# the attacker's on the host. A manifest's `env` may set anything else -- a server's own settings,
# `GIT_AUTHOR_NAME` -- which is why these are named rather than whole families banned.
_FORBIDDEN_ENV = frozenset(
    {
        # Dynamic loaders, beyond the `LD_` / `DYLD_` prefixes below.
        "GCONV_PATH",
        # Shells: a startup file, a field separator, and bash's own hooks into every command.
        "BASH_ENV",
        "BASHOPTS",
        "ENV",
        "IFS",
        "PROMPT_COMMAND",
        "PS4",
        "SHELLOPTS",
        # Interpreters: an option string or a search path is code.
        "JAVA_TOOL_OPTIONS",
        "JDK_JAVA_OPTIONS",
        "_JAVA_OPTIONS",
        "NODE_OPTIONS",
        "NODE_PATH",
        "PERL5DB",
        "PERL5LIB",
        "PERL5OPT",
        "PERLLIB",
        "PYTHONBREAKPOINT",
        # `PYTHONWARNINGS=ignore::antigravity.X` imports the module it names before checking the
        # class, and `antigravity` opens a browser -- which `BROWSER` names as a command.
        "PYTHONWARNINGS",
        "BROWSER",
        "PYTHONINSPECT",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
        "RUBYLIB",
        "RUBYOPT",
        # Mercurial's hooks, from an rc file the variable names (`/proc/self/environ` will do).
        "HGRCPATH",
        # git runs a program each of these names, before any config file is read; a fixed git
        # MCP server would run it on its first `git status`. `GIT_CONFIG_*` is a prefix below.
        # Re-enables `ext::` (a URL that runs a command), outranking every `protocol.*.allow`.
        "GIT_ALLOW_PROTOCOL",
        "GIT_ASKPASS",
        "GIT_CONFIG",
        "GIT_EDITOR",
        "GIT_EXEC_PATH",
        "GIT_EXTERNAL_DIFF",
        "GIT_PAGER",
        "GIT_PROXY_COMMAND",
        "GIT_SEQUENCE_EDITOR",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_TEMPLATE_DIR",
        "SSH_ASKPASS",
        # Where an allowlisted command finds what it runs: `npx` and `uvx` start their interpreter
        # through `#!/usr/bin/env node`, which searches PATH. The child gets the API's own.
        "PATH",
    }
)
# Whole families: the dynamic loaders' (`LD_PRELOAD`, `LD_AUDIT`, `DYLD_INSERT_LIBRARIES`), bash's
# exported functions (`BASH_FUNC_dirname%%` replaces `dirname` in a launcher script), git's
# config-by-environment (`GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_0=core.fsmonitor` runs a program),
# and git's trace targets.
# `GIT_TRACE*` (and `GIT_TRACE2*`) append to any file the API user can write, lines whose tails
# the caller controls -- a `~/.bashrc` is code at the next login.
_FORBIDDEN_ENV_PREFIXES = ("LD_", "DYLD_", "BASH_FUNC_", "GIT_CONFIG_", "GIT_TRACE")


def env_key_refused(name: str) -> bool:
    """Whether a manifest may not set the child environment variable ``name``. Compared
    upper-cased, so a case-insensitive environment (`Path` on Windows) is no way round it."""
    key = str(name).upper()
    return key in _FORBIDDEN_ENV or key.startswith(_FORBIDDEN_ENV_PREFIXES)


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


# A repository's `.git/config` and `.git/hooks` are the repository's to write -- a shell tool's
# workspace, which the agent writes, or a repository a stdio MCP server is pointed at -- and a
# setting there that makes git run a program turns an allowlisted command into running anything. These
# override it for every git the command starts -- named in argv or run by a script -- through
# git's own environment (`GIT_CONFIG_COUNT`, git 2.31+), which outranks every config file. As
# `github_publish._GIT_PRELUDE` and the gateway helper do for the git they run.
#
# Not covered: a `filter.<name>.clean`/`smudge` driver the repository names in `.gitattributes`,
# since its name is the repository's choice and cannot be switched off by a fixed key. A
# repository that needs no filters can be held to that by the operator's `.gitattributes`; see
# `deploy/GOVERNANCE.md`.
GIT_DISARMED: tuple[tuple[str, str], ...] = (
    ("core.hooksPath", "/dev/null"),
    ("core.fsmonitor", "false"),
    ("core.pager", "cat"),
    ("core.editor", "true"),
    ("sequence.editor", "true"),
    ("core.sshCommand", "ssh"),
    ("credential.helper", ""),
    ("commit.gpgSign", "false"),
    ("tag.gpgSign", "false"),
    # `git commit -S` signs whatever `commit.gpgSign` says, and `-S` is an argument the model
    # chooses: the programs themselves are pinned to the system's, not the repository's.
    ("gpg.program", "gpg"),
    ("gpg.ssh.program", "ssh-keygen"),
    ("gpg.x509.program", "gpgsm"),
    ("log.showSignature", "false"),
    ("diff.external", ""),
    # A URL whose transport is a command line, wherever the URL came from.
    ("protocol.ext.allow", "never"),
    # A repository-named askpass program runs on an HTTPS auth prompt; with it empty and
    # `GIT_TERMINAL_PROMPT=0` below, git fails the prompt instead.
    ("core.askPass", ""),
)


def git_disarmed(env: dict[str, str]) -> dict[str, str]:
    """`env` with every git it starts unable to run a program the repository's config names."""
    out = {k: v for k, v in env.items() if not k.startswith(("GIT_CONFIG_", "GIT_EXTERNAL_DIFF"))}
    out["GIT_CONFIG_COUNT"] = str(len(GIT_DISARMED))
    for n, (key, value) in enumerate(GIT_DISARMED):
        out[f"GIT_CONFIG_KEY_{n}"] = key
        out[f"GIT_CONFIG_VALUE_{n}"] = value
    # The environment's own names for three of them, which outrank even `GIT_CONFIG_*`.
    out["GIT_EDITOR"] = "true"
    out["GIT_SEQUENCE_EDITOR"] = "true"
    out["GIT_PAGER"] = "cat"
    out["GIT_TERMINAL_PROMPT"] = "0"
    return out


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
        if env_key_refused(name):
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
    "GIT_DISARMED",
    "StdioNotAllowedError",
    "absolute_path_entries",
    "allowed_commands",
    "assert_stdio_command_allowed",
    "describe_allowlist",
    "env_key_refused",
    "git_disarmed",
    "resolve_stdio_command",
    "stdio_child_env",
]
