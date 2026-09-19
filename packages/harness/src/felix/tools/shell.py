"""`spec.shell_tools` — exec an allowlisted argv in the workspace checkout.

What this tool must not be able to do, each pinned in `tests/unit/test_shell_tool.py`:
interpret a shell (argv is exec'd; `&&`, `|`, `;` are arguments), leave the workspace (`cwd`
resolves under `FELIX_WORKSPACE_ROOT`), see the API's secrets (the child gets the same
five-variable environment an MCP stdio child gets), run an unlisted command (manifest prefix
∩ operator prefix, checked per call), run forever (killed at `timeout_ms`), or flood the
context (output capped and marked truncated).

What it cannot prevent: an allowlisted command runs repository code as the API's user —
`./scripts/test.sh` imports whatever the agent just wrote. The host is that boundary, which
is why the builder deployment holds no cloud credentials and no Docker socket.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from felix.context import try_get_context
from felix.manifests.schema import ShellToolRef
from felix.security.shell_policy import (
    ShellNotAllowedError,
    assert_argv_allowed,
    assert_shell_commands_allowed,
    split_prefix,
)
from felix.security.stdio_policy import stdio_child_env
from felix.timeouts import timeout_seconds
from felix.tools.types import Tool, ToolInput, ToolInvocationCtx, ToolOutput, define_tool_with_executor
from felix.tools.workspace import _workspace_root, resolve_under_root

DEFAULT_SHELL_TIMEOUT_S = 300.0
# stdout and stderr each. A test suite's tail is what the model needs; the head is not.
MAX_OUTPUT_BYTES = 64_000
_MAX_STDIN_BYTES = 256_000


class ShellArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    argv: list[str] = Field(min_length=1, description="Command and arguments, exec'd without a shell.")
    cwd: str = Field(default=".", description="Working directory, relative to the workspace root.")
    stdin: str | None = Field(default=None, description="Optional text piped to the command's stdin.")


def _cap(data: bytes) -> tuple[str, bool]:
    truncated = len(data) > MAX_OUTPUT_BYTES
    kept = data[-MAX_OUTPUT_BYTES:] if truncated else data
    return kept.decode("utf-8", errors="replace"), truncated


class _ShellExecutor:
    transport = "shell"

    def __init__(self, *, prefixes: list[tuple[str, ...]], timeout_s: float, settings: Any | None) -> None:
        self._prefixes = prefixes
        self._timeout_s = timeout_s
        self._settings = settings

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        _ = ctx
        argv = [str(a) for a in (args.get("argv") or [])]
        # The operator allowlist is read per call from the live settings, not the copy the
        # tool was bound with: "checked per call" has to mean against what the host allows now.
        req = try_get_context()
        settings = req.settings if req is not None and req.settings is not None else self._settings
        try:
            assert_argv_allowed(argv, self._prefixes, settings)
            root = _workspace_root()
            cwd = resolve_under_root(root, str(args.get("cwd") or "."))
        except (ShellNotAllowedError, ValueError) as exc:
            return f"shell_error: {exc}"
        if not cwd.is_dir():
            return f"shell_error: not a directory: {args.get('cwd')}"
        stdin = args.get("stdin")
        stdin_bytes = str(stdin).encode("utf-8")[:_MAX_STDIN_BYTES] if stdin is not None else None
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                env=stdio_child_env({}),
                stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return f"shell_error: {exc}"
        timed_out = False
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin_bytes), timeout=self._timeout_s)
        except TimeoutError:
            timed_out = True
            proc.kill()
            out, err = await proc.communicate()
        stdout, out_trunc = _cap(out)
        stderr, err_trunc = _cap(err)
        return json.dumps(
            {
                "argv": argv,
                "cwd": str(cwd.relative_to(root)),
                "exit_code": proc.returncode,
                "timed_out": timed_out,
                "stdout": stdout,
                "stderr": stderr,
                "truncated": out_trunc or err_trunc,
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        )


def tools_from_shell_refs(refs: list[ShellToolRef], *, settings: Any | None = None) -> list[Tool]:
    assert_shell_commands_allowed(refs, settings)
    out: list[Tool] = []
    for ref in refs:
        timeout_s = timeout_seconds(ref.timeout_ms, default_s=DEFAULT_SHELL_TIMEOUT_S)
        allowed = ", ".join(ref.commands)
        out.append(
            define_tool_with_executor(
                name=ref.name,
                description=ref.description
                or f"Run a command in the workspace checkout, without a shell. Allowed: {allowed}.",
                args=ShellArgs,
                executor=_ShellExecutor(
                    prefixes=[split_prefix(c) for c in ref.commands],
                    timeout_s=timeout_s,
                    settings=settings,
                ),
                source="shell",
                fatal=ref.fatal,
            )
        )
    return out


__all__ = ["DEFAULT_SHELL_TIMEOUT_S", "MAX_OUTPUT_BYTES", "ShellArgs", "tools_from_shell_refs"]
