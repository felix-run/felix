"""`spec.shell_tools` — exec an allowlisted argv in the workspace checkout.

What this tool must not be able to do, each pinned in `tests/unit/test_shell_tool.py`:
interpret a shell (argv is exec'd; `&&`, `|`, `;` are arguments), leave the workspace (`cwd`
resolves under `FELIX_WORKSPACE_ROOT`), see the API's secrets (the child gets the same
five-variable environment an MCP stdio child gets, with relative `PATH` entries dropped), run
an unlisted command (manifest prefix ∩ operator prefix, checked per call), outlive its budget
(the whole process *group* is killed at `timeout_ms`, when its output passes the byte budget,
and when the calling task is cancelled), or flood the API (output is read in bounded chunks
and only a tail is kept — `MAX_OUTPUT_BYTES` bounds memory, not just the transcript).

What it cannot prevent: an allowlisted command runs repository code as the API's user —
`./scripts/test.sh` imports whatever the agent just wrote, and a relative `argv[0]` resolves
against the `cwd` the model chose. The host is that boundary, which is why the builder
deployment holds no cloud credentials, no Docker socket, no deployment secrets in the
checkout, and one tenant. `deploy/GOVERNANCE.md` carries the full list.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from felix.context import try_get_context
from felix.manifests.schema import ShellToolRef
from felix.observability.metrics import record_counter
from felix.security.shell_policy import (
    ShellNotAllowedError,
    assert_argv_allowed,
    assert_shell_commands_allowed,
    split_prefix,
)
from felix.security.stdio_policy import stdio_child_env
from felix.timeouts import timeout_seconds
from felix.tools.errors import ToolErrorCode, tool_error_output
from felix.tools.types import Tool, ToolInput, ToolInvocationCtx, ToolOutput, define_tool_with_executor
from felix.tools.workspace import resolve_under_root, workspace_root

DEFAULT_SHELL_TIMEOUT_S = 300.0
# The tail of stdout and of stderr that reaches the model. A test suite's last lines are
# what it needs; the first ten thousand are not.
MAX_OUTPUT_BYTES = 64_000
# Total bytes a command may write across both streams before it is killed. This is what
# bounds the API process, since only the tail above is ever held.
MAX_TOTAL_OUTPUT_BYTES = 8_000_000
_READ_CHUNK = 65_536
_MAX_ARGV_ITEMS = 256
_MAX_ARGV_BYTES = 64_000
_MAX_STDIN_CHARS = 256_000
# How long to wait for a killed group's pipes to close before giving up on its output.
_DRAIN_AFTER_KILL_S = 5.0


class ShellArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    argv: list[str] = Field(
        min_length=1, max_length=_MAX_ARGV_ITEMS, description="Command and arguments, exec'd without a shell."
    )
    cwd: str = Field(default=".", description="Working directory, relative to the workspace root.")
    stdin: str | None = Field(
        default=None, max_length=_MAX_STDIN_CHARS, description="Optional text piped to the command's stdin."
    )

    @field_validator("argv")
    @classmethod
    def _bounded(cls, v: list[str]) -> list[str]:
        if sum(len(a.encode("utf-8")) for a in v) > _MAX_ARGV_BYTES:
            raise ValueError(f"argv exceeds {_MAX_ARGV_BYTES} bytes")
        return v


def _child_env() -> dict[str, str]:
    """The stdio child environment, minus any `PATH` entry that is not absolute.

    A relative entry — `.`, `node_modules/.bin`, `.venv/bin`, all common on a developer host
    — resolves against the child's cwd, which the model chose. An absolute one does not.
    """
    env = stdio_child_env({})
    if "PATH" in env:
        env["PATH"] = os.pathsep.join(p for p in env["PATH"].split(os.pathsep) if os.path.isabs(p))
    return env


class _Stream:
    """A bounded tail of one output stream, filled by `_drain`."""

    def __init__(self) -> None:
        self.tail = bytearray()
        self.truncated = False


class _Budget:
    """Bytes written across both streams, shared so the kill fires once."""

    def __init__(self) -> None:
        self.total = 0
        self.exceeded = False


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the child's whole process group, not only the child.

    `start_new_session=True` made the child a group leader, so grandchildren — the pytest
    a test script spawns — die with it instead of holding the pipes open forever.
    """
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)


async def _drain(
    reader: asyncio.StreamReader | None, into: _Stream, budget: _Budget, proc: asyncio.subprocess.Process
) -> None:
    if reader is None:
        return
    while True:
        chunk = await reader.read(_READ_CHUNK)
        if not chunk:
            return
        budget.total += len(chunk)
        into.tail += chunk
        if len(into.tail) > MAX_OUTPUT_BYTES:
            del into.tail[: len(into.tail) - MAX_OUTPUT_BYTES]
            into.truncated = True
        if budget.total > MAX_TOTAL_OUTPUT_BYTES and not budget.exceeded:
            budget.exceeded = True
            _kill_group(proc)


async def _feed(proc: asyncio.subprocess.Process, data: bytes | None) -> None:
    if proc.stdin is None:
        return
    try:
        if data:
            proc.stdin.write(data)
            await proc.stdin.drain()
    except BrokenPipeError, ConnectionResetError:
        pass  # the command exited without reading; its exit code says so
    finally:
        proc.stdin.close()


class _ShellExecutor:
    transport = "shell"

    def __init__(
        self, *, name: str, prefixes: list[tuple[str, ...]], timeout_s: float, settings: Any
    ) -> None:
        self._name = name
        self._prefixes = prefixes
        self._timeout_s = timeout_s
        self._settings = settings

    def _refuse(self, reason: str, message: str) -> ToolOutput:
        # A refusal is a tool error, not a wrapper deny — no governance wrapper produced it —
        # but it is counted, so a model probing the allowlist shows up on the same dashboards
        # a policy deny does rather than as a quiet string in the transcript.
        record_counter("felix_shell_denied", {"tool": self._name, "reason": reason})
        return tool_error_output(ToolErrorCode.PERMISSION_DENIED, f"[shell denied] {message}")

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        _ = ctx
        argv = [str(a) for a in (args.get("argv") or [])]
        # The operator allowlist is read per call from the request's settings, not the copy
        # the tool was bound with: "checked per call" has to mean against what the host allows now.
        req = try_get_context()
        settings = req.settings if req is not None and req.settings is not None else self._settings
        try:
            assert_argv_allowed(argv, self._prefixes, settings)
        except ShellNotAllowedError as exc:
            return self._refuse("argv", str(exc))
        try:
            root = workspace_root()
            cwd = resolve_under_root(root, str(args.get("cwd") or "."))
        except ValueError as exc:
            return self._refuse("cwd", str(exc))
        if not cwd.is_dir():
            return self._refuse("cwd", f"not a directory: {args.get('cwd')}")
        stdin = args.get("stdin")
        stdin_bytes = str(stdin)[:_MAX_STDIN_CHARS].encode("utf-8") if stdin is not None else None
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                env=_child_env(),
                stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            return tool_error_output(ToolErrorCode.TRANSPORT_UNAVAILABLE, f"[shell] {exc}")

        out, err, budget = _Stream(), _Stream(), _Budget()
        tasks = [
            asyncio.create_task(_feed(proc, stdin_bytes)),
            asyncio.create_task(_drain(proc.stdout, out, budget, proc)),
            asyncio.create_task(_drain(proc.stderr, err, budget, proc)),
        ]
        timed_out = False
        try:
            try:
                await asyncio.wait_for(proc.wait(), timeout=self._timeout_s)
            except TimeoutError:
                timed_out = True
                _kill_group(proc)
                await proc.wait()
            # The group is dead or exited; give its pipes a bounded moment to close.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), timeout=_DRAIN_AFTER_KILL_S
                )
        finally:
            # Cancellation of the calling task lands here too: nothing it spawned survives it.
            _kill_group(proc)
            for task in tasks:
                task.cancel()
            if proc.returncode is None:
                await proc.wait()
        return json.dumps(
            {
                "argv": argv,
                "cwd": str(cwd.relative_to(root)),
                "exit_code": proc.returncode,
                "timed_out": timed_out,
                "output_exceeded": budget.exceeded,
                "stdout": out.tail.decode("utf-8", errors="replace"),
                "stderr": err.tail.decode("utf-8", errors="replace"),
                "truncated": out.truncated or err.truncated or budget.exceeded,
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        )


def tools_from_shell_refs(refs: list[ShellToolRef], *, settings: Any | None = None) -> list[Tool]:
    if settings is None:
        from felix.config import get_settings

        settings = get_settings()
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
                    name=ref.name,
                    prefixes=[split_prefix(c) for c in ref.commands],
                    timeout_s=timeout_s,
                    settings=settings,
                ),
                source="shell",
                fatal=ref.fatal,
            )
        )
    return out


__all__ = [
    "DEFAULT_SHELL_TIMEOUT_S",
    "MAX_OUTPUT_BYTES",
    "MAX_TOTAL_OUTPUT_BYTES",
    "ShellArgs",
    "tools_from_shell_refs",
]
