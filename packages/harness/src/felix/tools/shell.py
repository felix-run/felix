"""`spec.shell_tools` — exec an allowlisted argv in the workspace checkout.

What this tool must not be able to do, each pinned in `tests/unit/test_shell_tool.py`:
interpret a shell (argv is exec'd; `&&`, `|`, `;` are arguments), leave the workspace (`cwd`
resolves under `FELIX_WORKSPACE_ROOT`), see the API's secrets (the child gets the same
five-variable environment an MCP stdio child gets, with relative `PATH` entries dropped), run
an unlisted command (manifest prefix ∩ operator prefix, checked per call), outlive its budget
(the whole process *group* is killed at `timeout_ms`, when its output passes the byte budget,
and when the calling task is cancelled), or flood the API (output is read in bounded chunks
and only a tail is kept — `MAX_OUTPUT_BYTES` bounds memory, not just the transcript).

What it cannot prevent: an allowlisted command runs repository code — `./scripts/test.sh`
imports whatever the agent just wrote, and a relative `argv[0]` resolves against the `cwd` the
model chose. Where that code runs is the boundary. With `FELIX_SHELL_RUNNER_URL` unset it is a
child of this process, as this process's user, and can read this process's environment through
`/proc` — so only on a development box; anywhere else the call is refused (`isolation_refusal`), as the
boot and the manifest write already were. With it set, every check above still runs here and
the exec happens in `felix.shell_runner`, a separate process (on the builder stack, a separate
container sharing only the workspace volume and holding no secrets). A runner that cannot be reached fails the
call: there is no fallback to a local exec. `deploy/GOVERNANCE.md` "Shell tools" carries the
full list.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from felix.context import try_get_context
from felix.manifests.schema import MAX_INTEGRATION_TIMEOUT_MS, ShellToolRef
from felix.observability.metrics import record_counter
from felix.security.shell_policy import (
    ShellNotAllowedError,
    assert_argv_allowed,
    assert_shell_commands_allowed,
    local_exec_refusal,
    split_prefix,
)
from felix.security.stdio_policy import stdio_child_env
from felix.timeouts import DEFAULT_CONNECT_TIMEOUT_S, timeout_seconds
from felix.tools.errors import ToolErrorCode, tool_error_output
from felix.tools.types import Tool, ToolInput, ToolInvocationCtx, ToolOutput, define_tool_with_executor
from felix.tools.workspace import open_workspace_dir, workspace_root

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
# Remote mode: how long past the command's own timeout the API waits for the runner's answer.
# The runner kills at `timeout_ms` and then drains for up to `_DRAIN_AFTER_KILL_S`, so this
# only fires when the runner itself is wedged.
RUNNER_GRACE_S = 30.0
# The most of a runner response the API reads. Two output tails, argv and JSON escaping (a
# control byte is six bytes escaped) fit well inside it; anything larger is not a runner.
MAX_RUNNER_RESPONSE_BYTES = 2 * 1024 * 1024
# The largest `duration_ms` a runner can truthfully report: the longest timeout a manifest may
# set, the drain after the kill, and the grace this side waits.
MAX_RUNNER_DURATION_MS = MAX_INTEGRATION_TIMEOUT_MS + int((_DRAIN_AFTER_KILL_S + RUNNER_GRACE_S) * 1000)


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


# `.git/config` and `.git/hooks` are in the workspace, which the agent writes; a setting there
# that makes git run a program turns an allowlisted `git commit` into running anything. These
# override it for every git the command starts -- named in argv or run by a script -- through
# git's own environment (`GIT_CONFIG_COUNT`, git 2.31+), which outranks every config file. As
# `github_publish._GIT_PRELUDE` and the gateway helper do for the git they run.
#
# Not covered: a `filter.<name>.clean`/`smudge` driver the repository names in `.gitattributes`,
# since its name is the repository's choice and cannot be switched off by a fixed key. A
# repository that needs no filters can be held to that by the operator's `.gitattributes`; see
# `deploy/GOVERNANCE.md`.
_GIT_DISARMED: tuple[tuple[str, str], ...] = (
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
)


def _git_disarmed(env: dict[str, str]) -> dict[str, str]:
    """`env` with every git it starts unable to run a program the repository's config names."""
    out = {k: v for k, v in env.items() if not k.startswith(("GIT_CONFIG_", "GIT_EXTERNAL_DIFF"))}
    out["GIT_CONFIG_COUNT"] = str(len(_GIT_DISARMED))
    for n, (key, value) in enumerate(_GIT_DISARMED):
        out[f"GIT_CONFIG_KEY_{n}"] = key
        out[f"GIT_CONFIG_VALUE_{n}"] = value
    # The environment's own names for three of them, which outrank even `GIT_CONFIG_*`.
    out["GIT_EDITOR"] = "true"
    out["GIT_SEQUENCE_EDITOR"] = "true"
    out["GIT_PAGER"] = "cat"
    return out


def _child_env() -> dict[str, str]:
    """The stdio child environment with git disarmed (`_GIT_DISARMED`). Its `PATH` keeps only
    absolute entries (`stdio_policy.absolute_path_entries`): a relative one resolves against the
    child's cwd, which the model chose."""
    return _git_disarmed(stdio_child_env({}))


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


Refuse = Callable[[str, str], ToolOutput]


def shell_refusal(tool: str, reason: str, message: str) -> ToolOutput:
    """A refused shell call. A tool error, not a wrapper deny -- no governance wrapper produced it --
    but counted, so a model probing the allowlist shows up on the same dashboards a policy deny does
    rather than as a quiet string in the transcript."""
    record_counter("felix_shell_denied", {"tool": tool, "reason": reason})
    return tool_error_output(ToolErrorCode.PERMISSION_DENIED, f"[shell denied] {message}")


@dataclass(frozen=True, slots=True)
class HostedPlace:
    """The scope's sandbox, through the hosted workspace gateway."""

    settings: Any
    scope: Any


@dataclass(frozen=True, slots=True)
class RunnerPlace:
    """`felix-shell-runner`, told the scope's directory relative to its own workspace root."""

    url: str
    token: str
    root: Path
    scope: str


@dataclass(frozen=True, slots=True)
class LocalPlace:
    """This process: a development box only."""

    root: Path


ShellPlace = HostedPlace | RunnerPlace | LocalPlace


def shell_place(settings: Any, refuse: Refuse) -> ShellPlace | ToolOutput:
    """Where a shell call runs, or the tool error saying it runs nowhere. The one choice: a
    command, a background start and that command's reads and end all ask it, so an id is looked up
    where it was made.

    Under the hosted workspace backend, the scope's sandbox, where its files are; it never falls
    back to this host. With a runner, the runner, which repeats the allowlist and the cwd against
    its own configuration; a runner that cannot be reached fails the call, never the branch below.
    Otherwise this process, whatever the settings say about runners or backends (the hosted
    backend's `deployment` scope reaches here), so only a development box may. Checked at boot and
    compile too; this holds for settings that never went through either."""
    hosted = _hosted_scope(settings)
    if hosted is not None:
        return HostedPlace(settings, hosted)
    try:
        root = workspace_root()
    except ValueError as exc:
        return refuse("cwd", str(exc))
    url = str(getattr(settings, "shell_runner_url", "") or "").strip()
    if url:
        if _is_thread_checkout(root, settings):
            # The runner resolves `cwd` under its own FELIX_WORKSPACE_ROOT and has no thread's
            # checkout: sent there, the command would run in the shared workspace while the
            # model believes it is in the repository.
            return tool_error_output(
                ToolErrorCode.TRANSPORT_UNAVAILABLE,
                "[shell] this thread works in its own repository checkout, which the remote shell "
                "runner cannot reach; shell commands are unavailable here",
            )
        return RunnerPlace(url, _runner_token(settings), root, _scope_on_runner(root, settings))
    refusal = local_exec_refusal(settings)
    if refusal is not None:
        return refuse("isolation", refusal)
    return LocalPlace(root)


class _ShellExecutor:
    transport = "shell"

    def __init__(
        self,
        *,
        name: str,
        prefixes: list[tuple[str, ...]],
        timeout_s: float,
        settings: Any,
        background: bool = False,
    ) -> None:
        self._name = name
        self._prefixes = prefixes
        self._timeout_s = timeout_s
        self._settings = settings
        self._background = background
        self._refuse: Refuse = functools.partial(shell_refusal, name)

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        _ = ctx
        argv = [str(a) for a in (args.get("argv") or [])]
        # The operator allowlist is read per call from the request's settings, not the copy
        # the tool was bound with: "checked per call" has to mean against what the host allows now.
        settings = request_settings(self._settings)
        try:
            assert_argv_allowed(argv, self._prefixes, settings)
        except ShellNotAllowedError as exc:
            return self._refuse("argv", str(exc))
        stdin = args.get("stdin")
        stdin_text = str(stdin)[:_MAX_STDIN_CHARS] if stdin is not None else None
        place = shell_place(settings, self._refuse)
        if not isinstance(place, HostedPlace | RunnerPlace | LocalPlace):
            return place
        cwd_arg = str(args.get("cwd") or ".")
        cwd: Path | None = None
        if not isinstance(place, HostedPlace):
            # The sandbox resolves its own, the same way.
            try:
                cwd = resolve_cwd(place.root, cwd_arg)
            except ValueError as exc:
                return self._refuse("cwd", str(exc))
            cwd_arg = str(cwd.relative_to(place.root))
        # Only a tool bound with `background: true` was offered the argument.
        if self._background and args.get("run_in_background"):
            from felix.tools.shell_background_tools import start_in_background

            return await start_in_background(
                place,
                self._refuse,
                argv=argv,
                cwd=cwd,
                cwd_rel=cwd_arg,
                stdin=stdin_text,
                timeout_s=self._timeout_s,
            )
        match place:
            case HostedPlace():
                return await self._run_hosted(place.settings, place.scope, argv, cwd_arg, stdin_text)
            case RunnerPlace():
                return await self._run_remote(place.url, place.token, argv, cwd_arg, stdin_text, place.scope)
        assert cwd is not None
        try:
            result = await exec_argv(
                argv, cwd=cwd, root=place.root, stdin=stdin_text, timeout_s=self._timeout_s
            )
        except OSError as exc:
            return tool_error_output(ToolErrorCode.TRANSPORT_UNAVAILABLE, f"[shell] {exc}")
        return json.dumps(result)

    async def _run_hosted(
        self, settings: Any, scope: Any, argv: list[str], cwd: str, stdin: str | None
    ) -> ToolOutput:
        from felix.tools.workspace_hosted import HostedBackend

        try:
            out = await HostedBackend(settings).exec(
                scope, argv, cwd, stdin, max(1, int(self._timeout_s * 1000))
            )
        except ValueError as exc:
            if "workspace_root" in str(exc):
                return tool_error_output(
                    ToolErrorCode.TRANSPORT_UNAVAILABLE, f"[shell] {exc}; the command did not run"
                )
            # The sandbox resolved `cwd` as `resolve_cwd` does here: escaping, absolute, a symlink.
            return self._refuse("cwd", str(exc))
        except OSError as exc:
            # The command could not be spawned in the sandbox, as `exec_argv` reports it here.
            return tool_error_output(ToolErrorCode.TRANSPORT_UNAVAILABLE, f"[shell] {exc}")
        try:
            result = RunnerResult.model_validate(out)
        except ValidationError:
            return runner_unavailable("returned a malformed result")
        # argv is what this process asked for, not what the sandbox says it ran.
        return json.dumps({**result.model_dump(), "argv": argv})

    async def _run_remote(
        self, url: str, token: str, argv: list[str], cwd: str, stdin: str | None, scope: str
    ) -> ToolOutput:
        body: dict[str, Any] = {"argv": argv, "cwd": cwd, "timeout_ms": max(1, int(self._timeout_s * 1000))}
        if scope:
            # The runner shares the volume, not this process's request: it is told which scope's
            # directory `cwd` is relative to, and checks the shape before using it.
            body["scope"] = scope
        if stdin is not None:
            body["stdin"] = stdin
        answer = await runner_post(url, token, "/run", body, self._timeout_s + RUNNER_GRACE_S)
        if not isinstance(answer, tuple):
            return answer
        return self._map_runner_response(*answer, argv, cwd)

    def _map_runner_response(self, status: int, raw: bytes, argv: list[str], cwd: str) -> ToolOutput:
        if status == 200:
            try:
                result = RunnerResult.model_validate_json(raw)
            except ValidationError:
                return runner_unavailable("returned a malformed result")
            # argv and cwd are what this process asked for, not what the runner says it ran.
            return json.dumps({**result.model_dump(), "argv": argv, "cwd": cwd})
        detail = runner_detail(raw)
        if status == 403:
            return self._refuse("runner", f"the shell runner refused it: {detail}")
        if status == 401:
            return runner_unavailable("rejected the token (FELIX_SHELL_RUNNER_TOKEN must match)")
        if status == 422 and detail:
            return tool_error_output(ToolErrorCode.TRANSPORT_UNAVAILABLE, f"[shell] {detail}")
        return runner_unavailable(f"answered HTTP {status}")


async def runner_post(
    url: str,
    token: str,
    path: str,
    body: dict[str, Any],
    deadline_s: float,
    consequence: str = "the command did not run",
) -> tuple[int, bytes] | ToolOutput:
    """One call to the shell runner: its status and capped body, or the tool error saying why
    there is none, and `consequence` of that."""
    try:
        # httpx's timeout is per operation — connect, each read — so a runner that sends a
        # byte every few seconds never trips it. This bounds the whole exchange: the API's
        # tool call ends at the command's own budget plus the grace, whatever the runner does.
        async with (
            asyncio.timeout(deadline_s),
            _runner_client(deadline_s) as client,
            client.stream(
                "POST", url.rstrip("/") + path, json=body, headers={"authorization": f"Bearer {token}"}
            ) as resp,
        ):
            return resp.status_code, await _read_capped(resp, MAX_RUNNER_RESPONSE_BYTES)
    except httpx.TimeoutException, TimeoutError:
        return runner_unavailable("did not answer in time", consequence)
    except (httpx.HTTPError, _ResponseTooLarge) as exc:
        # The class name only: an httpx message can carry the URL, and the URL is the
        # operator's to read in config, not the model's to read in a transcript.
        return runner_unavailable(f"is unreachable ({type(exc).__name__})", consequence)


def request_settings(bound: Any) -> Any:
    """The request's settings, or the ones a tool was bound with outside a request."""
    req = try_get_context()
    return req.settings if req is not None and req.settings is not None else bound


def _runner_token(settings: Any) -> str:
    return str(getattr(settings, "shell_runner_token", "") or "")


class RunnerResult(BaseModel):
    """What `POST /run` answers — the same fields the local exec returns, and nothing else."""

    model_config = ConfigDict(extra="forbid")

    argv: list[str]
    cwd: str
    # Bounded so a hostile runner's 10**5000 is a validation failure here — closed, like any
    # malformed result — and not a ValueError when something later renders or stores it. A
    # POSIX exit status is 0-255 and a signal death is its negative; the duration cannot
    # exceed the longest timeout a manifest may set plus the grace.
    exit_code: int | None = Field(ge=-255, le=255)
    timed_out: bool
    output_exceeded: bool
    # Characters, not bytes, but a decoded tail never has more characters than bytes.
    stdout: str = Field(max_length=MAX_OUTPUT_BYTES)
    stderr: str = Field(max_length=MAX_OUTPUT_BYTES)
    truncated: bool
    duration_ms: int = Field(ge=0, le=MAX_RUNNER_DURATION_MS)


class _ResponseTooLarge(Exception):
    pass


def _runner_client(timeout_s: float) -> httpx.AsyncClient:
    """The one client that reaches the shell runner.

    Not `safe_async_client`, deliberately: the runner is a private Compose hostname (`shell`)
    that the egress guard exists to refuse. The exemption holds because the URL comes from
    `Settings.shell_runner_url` and nowhere else — never a manifest field or a model argument.
    No redirects (a 3xx would carry the bearer to wherever it pointed) and no proxy from the
    environment.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_s, connect=DEFAULT_CONNECT_TIMEOUT_S),
        follow_redirects=False,
        trust_env=False,
    )


async def _read_capped(resp: httpx.Response, limit: int) -> bytes:
    buf = bytearray()
    async for chunk in resp.aiter_bytes():
        buf += chunk
        if len(buf) > limit:
            raise _ResponseTooLarge
    return bytes(buf)


def runner_detail(raw: bytes) -> str:
    try:
        data = json.loads(raw)
    except ValueError:
        return ""
    message = data.get("message") if isinstance(data, dict) else None
    return str(message)[:500] if message else ""


def runner_unavailable(what: str, consequence: str = "the command did not run") -> ToolOutput:
    # Fail closed, and say what that meant: for `/run`, that the command did not run anywhere.
    return tool_error_output(
        ToolErrorCode.TRANSPORT_UNAVAILABLE, f"[shell] the shell runner {what}; {consequence}"
    )


def resolve_cwd(root: Path, raw: str) -> Path:
    """`raw` under `root`, or `ValueError` — escapes, absolute paths, symlinks, non-directories.

    Walked the way the workspace tools open a path, so a symlinked component is refused here
    as it is there. The answer is still a name the exec then `chdir`s to, so this is a check,
    not a confinement: where the command runs is the boundary, not where it starts.
    """
    try:
        with open_workspace_dir(root, raw or ".") as (_fd, rel):
            pass
    except OSError:
        raise ValueError(f"not a directory: {raw}") from None
    return root if rel == "." else root.joinpath(*rel.split("/"))


async def exec_argv(
    argv: list[str], *, cwd: Path, root: Path, stdin: str | None, timeout_s: float
) -> dict[str, Any]:
    """Exec `argv` in `cwd` and return the result the model sees. The one exec path.

    Shared by the in-process tool and `felix.shell_runner`, so the scrubbed environment, the
    process-group kill and the bounded tail are the same code on both sides. Callers have
    already checked the allowlist and resolved `cwd` under `root`. Raises `OSError` when the
    command cannot be spawned.
    """
    stdin_bytes = stdin[:_MAX_STDIN_CHARS].encode("utf-8") if stdin is not None else None
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=_child_env(),
        stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )

    out, err, budget = _Stream(), _Stream(), _Budget()
    tasks = [
        asyncio.create_task(_feed(proc, stdin_bytes)),
        asyncio.create_task(_drain(proc.stdout, out, budget, proc)),
        asyncio.create_task(_drain(proc.stderr, err, budget, proc)),
    ]
    timed_out = False
    try:
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_s)
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
    return {
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


def tools_from_shell_refs(
    refs: list[ShellToolRef], *, settings: Any | None = None, scope: str | None = None
) -> list[Tool]:
    if settings is None:
        from felix.config import get_settings

        settings = get_settings()
    assert_shell_commands_allowed(refs, settings, scope=scope)
    out: list[Tool] = []
    for ref in refs:
        timeout_s = timeout_seconds(ref.timeout_ms, default_s=DEFAULT_SHELL_TIMEOUT_S)
        allowed = ", ".join(ref.commands)
        out.append(
            define_tool_with_executor(
                name=ref.name,
                description=ref.description
                or f"Run a command in the workspace checkout, without a shell. Allowed: {allowed}.",
                args=background_args() if ref.background else ShellArgs,
                executor=_ShellExecutor(
                    name=ref.name,
                    prefixes=[split_prefix(c) for c in ref.commands],
                    timeout_s=timeout_s,
                    settings=settings,
                    background=ref.background,
                ),
                source="shell",
                fatal=ref.fatal,
            )
        )
    if any(ref.background for ref in refs):
        from felix.tools.shell_background_tools import background_tools

        out.extend(background_tools(settings))
    return out


def background_args() -> type[ShellArgs]:
    from felix.tools.shell_background_tools import ShellBackgroundArgs

    return ShellBackgroundArgs


__all__ = [
    "DEFAULT_SHELL_TIMEOUT_S",
    "MAX_OUTPUT_BYTES",
    "MAX_RUNNER_DURATION_MS",
    "MAX_RUNNER_RESPONSE_BYTES",
    "MAX_TOTAL_OUTPUT_BYTES",
    "HostedPlace",
    "LocalPlace",
    "Refuse",
    "RunnerPlace",
    "RunnerResult",
    "ShellArgs",
    "ShellPlace",
    "exec_argv",
    "request_settings",
    "resolve_cwd",
    "runner_detail",
    "runner_post",
    "runner_unavailable",
    "shell_place",
    "shell_refusal",
    "tools_from_shell_refs",
]


def _hosted_scope(settings: Any) -> Any | None:
    """The call's workspace scope when the hosted backend serves it, else None (run locally)."""
    if getattr(settings, "workspace_backend", "local") != "hosted":
        return None
    from felix.tools.workspace_backend import current_workspace_scope

    _, scope = current_workspace_scope()
    return scope if scope is not None and scope.scope != "deployment" else None


def _is_thread_checkout(root: Path, settings: Any) -> bool:
    """Whether `root` is a thread's checkout rather than a scope of the operator's workspace.

    A scope's directory is the workspace root or one under it; a checkout is never under it
    (`checkout_root` refuses that configuration), which is what tells the two apart.
    """
    shared = str(getattr(settings, "workspace_root", "") or "").strip()
    if not shared:
        return True
    try:
        base = Path(shared).expanduser().resolve()
    except OSError:
        return True
    return root != base and base not in root.parents


def _scope_on_runner(root: Path, settings: Any) -> str:
    """`root` relative to the workspace root, as the runner is told it: `""` for the root itself."""
    base = Path(str(getattr(settings, "workspace_root", "") or "")).expanduser().resolve()
    rel = root.relative_to(base).as_posix()
    return "" if rel == "." else rel
