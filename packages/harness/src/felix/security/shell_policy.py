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

import shlex
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from felix.manifests.schema import ShellToolRef


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


def assert_shell_commands_allowed(refs: Iterable[ShellToolRef], settings: Any) -> None:
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
    for ref in refs:
        for command in ref.commands:
            wanted = split_prefix(command)
            if not operator:
                errors.append(
                    f"shell_tools[{ref.name}]: shell tools are disabled (no FELIX_SHELL_ALLOWED_COMMANDS)"
                )
                break
            if not any(prefix_covers(op, wanted) for op in operator):
                errors.append(
                    f"shell_tools[{ref.name}]: {command!r} is not covered by any prefix in "
                    "FELIX_SHELL_ALLOWED_COMMANDS"
                )
    if errors:
        raise ShellNotAllowedError("; ".join(errors))


def describe_allowlist(settings: Any) -> str:
    """Human-readable summary for `felix doctor`."""
    prefixes = allowed_prefixes(settings)
    if not prefixes:
        return "disabled (no FELIX_SHELL_ALLOWED_COMMANDS)"
    return ", ".join(shlex.quote(" ".join(p)) for p in prefixes)


__all__ = [
    "ShellNotAllowedError",
    "allowed_prefixes",
    "assert_argv_allowed",
    "assert_shell_commands_allowed",
    "describe_allowlist",
    "prefix_covers",
    "split_prefix",
]
