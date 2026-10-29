"""Operator allowlist for `spec.shell_tools` — argv prefixes a deployment may exec.

Same posture as `stdio_policy`: a manifest is tenant-writable, so what it may run is bounded
by the operator, and the default bound is nothing. Two gates, checked in this order at every
call — the manifest's own `commands` (the tool narrows), then `FELIX_SHELL_ALLOWED_COMMANDS`
(the operator bounds) — and at manifest write the manifest's prefixes must each be *covered*
by an operator prefix, so a stored manifest cannot name a command the host will refuse.

A prefix is whitespace-split tokens matched one for one from `argv[0]`. `git status` covers
`git status --short` and not `git -c core.pager=x status`; the option would have to be the
second token, and it is not. That is the whole grammar — no globs, no shell — because every
extra shape is a way for a listed command to run something that was not listed.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from felix.manifests.schema import ShellToolRef, SkillScriptsSpec


class ShellNotAllowedError(ValueError):
    """argv (or a manifest prefix) is outside what the operator allows."""


def split_prefix(text: str) -> tuple[str, ...]:
    return tuple(text.split())


def allowed_prefixes(settings: Any) -> tuple[tuple[str, ...], ...]:
    """Parse `FELIX_SHELL_ALLOWED_COMMANDS`. Empty (the default) disables shell tools."""
    raw = getattr(settings, "shell_allowed_commands", "") or ""
    return tuple(split_prefix(part) for part in raw.split(",") if part.strip())


def prefix_covers(prefix: Sequence[str], argv: Sequence[str]) -> bool:
    """Does `argv` begin with every token of `prefix`, in order?"""
    return bool(prefix) and len(argv) >= len(prefix) and tuple(argv[: len(prefix)]) == tuple(prefix)


def assert_argv_allowed(
    argv: Sequence[str],
    manifest_prefixes: Iterable[Sequence[str]],
    settings: Any,
) -> None:
    """Refuse an argv outside the tool's own prefixes or the operator's."""
    if not argv or not str(argv[0]).strip():
        raise ShellNotAllowedError("argv must name a command")
    operator = allowed_prefixes(settings)
    if not operator:
        raise ShellNotAllowedError(
            "shell tools are disabled. Set FELIX_SHELL_ALLOWED_COMMANDS to the argv prefixes "
            "this deployment may exec."
        )
    if not any(prefix_covers(p, argv) for p in manifest_prefixes):
        raise ShellNotAllowedError(f"{argv[0]!r} is not under any of this tool's commands")
    if not any(prefix_covers(p, argv) for p in operator):
        raise ShellNotAllowedError(f"{argv[0]!r} is not under any prefix in FELIX_SHELL_ALLOWED_COMMANDS")


def exec_is_isolated(settings: Any, *, scope: str | None = None) -> bool:
    """Would a shell command run somewhere other than this process?

    A shell tool runs repository code — `make test` imports what the agent just wrote — so where
    it execs is the boundary. Two places are not this process: the separate shell runner
    (`FELIX_SHELL_RUNNER_URL`) and the hosted workspace backend's sandbox. The hosted backend
    serves a `deployment` scope from this host, though, so for that scope only the runner counts.
    `scope=None` is the boot check, which cannot know the manifests it will serve.
    """
    if str(getattr(settings, "shell_runner_url", "") or "").strip():
        return True
    return getattr(settings, "workspace_backend", "local") == "hosted" and scope != "deployment"


def local_exec_allowed(settings: Any) -> bool:
    """Only on a single person's development box may a shell command run as a child of the API.

    There, the code it runs can read the API's environment through `/proc` — the model keys, the
    GitHub token. That is a laptop's trade, not a deployment's, and `FELIX_ENVIRONMENT` alone does
    not say which this is: Compose defaults it to development. So, as for skill import's token,
    development *and* `FELIX_AUTH_MODE=none` — `make dev` — and nothing else.
    """
    return (
        getattr(settings, "environment", "production") == "development"
        and getattr(settings, "auth_mode", "api_key") == "none"
    )


_ISOLATION_HELP = (
    "shell tools need a shell runner here: set FELIX_SHELL_RUNNER_URL — deploy/docker/compose.self.yml "
    "runs one — or use FELIX_WORKSPACE_BACKEND=hosted (not with `workspace.scope: deployment`, which "
    "it serves from this host). Only a development box (FELIX_ENVIRONMENT=development with "
    "FELIX_AUTH_MODE=none) runs a shell tool in the API's own process."
)


def isolation_refusal(settings: Any, *, scope: str | None = None) -> str | None:
    """Why a shell tool may not run on this host, or None when it may."""
    if exec_is_isolated(settings, scope=scope) or local_exec_allowed(settings):
        return None
    return _ISOLATION_HELP


def local_exec_refusal(settings: Any) -> str | None:
    """For the call itself, about to exec in this process: allowed only on a development box.

    Not `isolation_refusal`: by the time this is asked, the exec is local whatever the settings
    say about runners or backends — the hosted backend's `deployment` scope reaches here.
    """
    return None if local_exec_allowed(settings) else _ISOLATION_HELP


def assert_shell_commands_allowed(
    refs: Iterable[ShellToolRef], settings: Any, *, scope: str | None = None
) -> None:
    """Every manifest prefix is covered by an operator prefix.

    Checked at manifest write and at compile, like sandbox images: a manifest that names
    `git push` on a host allowing `git status` is refused with a 400 once, not compiled into
    a tool that fails on every call.
    """
    refs = list(refs)
    if not refs:
        return
    operator = allowed_prefixes(settings)
    errors: list[str] = []
    refusal = isolation_refusal(settings, scope=scope) if operator else None
    for ref in refs:
        if refusal is not None:
            errors.append(f"shell_tools[{ref.name}]: {refusal}")
            continue
        for command in ref.commands:
            wanted = split_prefix(command)
            if not operator:
                disabled = (
                    f"shell_tools[{ref.name}]: shell tools are disabled (no FELIX_SHELL_ALLOWED_COMMANDS)"
                )
                if isolation_refusal(settings, scope=scope) is not None:
                    # Setting the allowlist alone would not be enough here; say so up front.
                    disabled += "; outside development they also need a shell runner (FELIX_SHELL_RUNNER_URL)"
                errors.append(disabled)
                break
            if not any(prefix_covers(op, wanted) for op in operator):
                errors.append(
                    f"shell_tools[{ref.name}]: {command!r} is not covered by any prefix in "
                    "FELIX_SHELL_ALLOWED_COMMANDS"
                )
    if errors:
        raise ShellNotAllowedError("; ".join(errors))


# What an interpreter may not contain: it becomes `argv[0]` on its own, and the operator list that
# names it is comma-separated, the manifest's map is `ext: interpreter`.
_INTERPRETER_FORBIDDEN = frozenset(" \t\r\n\v\f,=\0")


def interpreter_issue(token: str) -> str | None:
    """Why ``token`` cannot be an interpreter, or None. One argv word that is not an option."""
    if not token:
        return "an interpreter must name a command"
    if any(c in _INTERPRETER_FORBIDDEN for c in token):
        return f"{token!r} must be one word, without whitespace, ',', '=' or NUL"
    if token.startswith("-"):
        return f"{token!r} reads as an option, not a command"
    return None


def allowed_interpreters(settings: Any) -> frozenset[str]:
    """Parse `FELIX_SKILL_SCRIPT_INTERPRETERS`. Empty (the default) disables skill scripts; an entry
    `interpreter_issue` refuses is dropped here and refused at boot."""
    raw = getattr(settings, "skill_script_interpreters", "") or ""
    tokens = (part.strip() for part in raw.split(","))
    return frozenset(t for t in tokens if t and interpreter_issue(t) is None)


def malformed_interpreters(settings: Any) -> list[str]:
    """The `FELIX_SKILL_SCRIPT_INTERPRETERS` entries `interpreter_issue` refuses, for the boot check."""
    raw = getattr(settings, "skill_script_interpreters", "") or ""
    issues = (interpreter_issue(part.strip()) for part in raw.split(",") if part.strip())
    return [issue for issue in issues if issue is not None]


def assert_skill_scripts_allowed(
    spec: SkillScriptsSpec, settings: Any, *, scope: str | None = None, has_skills: bool = True
) -> None:
    """`spec.skill_scripts`, against what this host runs: checked at manifest write and at compile,
    as shell tools are, so a stored manifest never names an interpreter the host will refuse."""
    if not spec.enabled:
        return
    errors: list[str] = []
    operator = allowed_interpreters(settings)
    if not has_skills:
        errors.append("skill_scripts: the manifest has no skills whose scripts could run")
    if not operator:
        errors.append("skill_scripts: skill scripts are disabled (no FELIX_SKILL_SCRIPT_INTERPRETERS)")
    else:
        for ext, interpreter in sorted(spec.interpreters.items()):
            if interpreter not in operator:
                errors.append(
                    f"skill_scripts.interpreters[{ext}]: {interpreter!r} is not in "
                    "FELIX_SKILL_SCRIPT_INTERPRETERS"
                )
    refusal = isolation_refusal(settings, scope=scope)
    if refusal is not None:
        # The same places a shell command may run: a script is code, as a listed command is.
        errors.append(f"skill_scripts: {refusal}")
    if errors:
        raise ShellNotAllowedError("; ".join(errors))


# Where a skill script is written for one call, under the scope's workspace root:
# `.felix-skills/<skill>/<call token>/<bundle path>`. The file tools refuse the directory
# (`workspace_backend.PANE_HIDDEN_DIRS`), so a model cannot write a script there itself.
SKILL_SCRIPTS_DIR = ".felix-skills"
_SCRIPT_PATH_RE = re.compile(
    r"\.felix-skills/[a-z0-9]+(?:-[a-z0-9]+)*/[0-9a-f]{32}/scripts(?:/[A-Za-z0-9._-]{1,128})+"
)


def skill_script_argv_issue(argv: Sequence[str], settings: Any) -> str | None:
    """Why ``argv`` is not a skill script this host runs, or None: an interpreter the operator
    lists, then a script written where `run_skill_script` writes one, then its arguments.

    The shell runner asks this instead of the shell allowlist: listing `python3` there would hand
    every shell tool `python3 -c`. The script is `argv[1]`, so nothing after the interpreter can
    be one of its options.
    """
    if len(argv) < 2:
        return "a skill script needs an interpreter and a script"
    interpreters = allowed_interpreters(settings)
    if not interpreters:
        return "skill scripts are disabled (no FELIX_SKILL_SCRIPT_INTERPRETERS)"
    if argv[0] not in interpreters:
        return f"{argv[0]!r} is not in FELIX_SKILL_SCRIPT_INTERPRETERS"
    path = str(argv[1])
    if not _SCRIPT_PATH_RE.fullmatch(path) or any(seg in {".", ".."} for seg in path.split("/")[1:]):
        return "the script is not one run_skill_script wrote"
    return None


def describe_interpreters(settings: Any) -> str:
    """Human-readable summary for `felix doctor`."""
    interpreters = allowed_interpreters(settings)
    if not interpreters:
        return "disabled (no FELIX_SKILL_SCRIPT_INTERPRETERS)"
    return ", ".join(sorted(interpreters))


def describe_allowlist(settings: Any) -> str:
    """Human-readable summary for `felix doctor`."""
    prefixes = allowed_prefixes(settings)
    if not prefixes:
        return "disabled (no FELIX_SHELL_ALLOWED_COMMANDS)"
    return ", ".join(shlex.quote(" ".join(p)) for p in prefixes)


__all__ = [
    "SKILL_SCRIPTS_DIR",
    "ShellNotAllowedError",
    "allowed_interpreters",
    "allowed_prefixes",
    "assert_argv_allowed",
    "assert_shell_commands_allowed",
    "assert_skill_scripts_allowed",
    "describe_allowlist",
    "describe_interpreters",
    "exec_is_isolated",
    "interpreter_issue",
    "isolation_refusal",
    "local_exec_allowed",
    "local_exec_refusal",
    "malformed_interpreters",
    "prefix_covers",
    "skill_script_argv_issue",
    "split_prefix",
]
