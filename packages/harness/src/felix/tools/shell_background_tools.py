"""The model's side of background shell commands: `run_in_background`, `shell_output`, `shell_kill`.

`felix.tools.shell_background` is the supervisor and its files, standard library only so the hosted
sandbox can carry it. This is everything around it that the harness owns: the arguments the model
is offered, where each call goes (`felix.tools.shell.shell_place`, the choice a foreground command
makes too, so an id is always looked up where it was made), the bounds an answer from a runner or a
sandbox is held to, and how a failure is worded.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from felix.context import try_get_context
from felix.manifests.schema import SHELL_BACKGROUND_TOOLS
from felix.tools import shell_background
from felix.tools.errors import ToolErrorCode, tool_error_output
from felix.tools.shell import (
    _MAX_ARGV_ITEMS,
    MAX_OUTPUT_BYTES,
    MAX_TOTAL_OUTPUT_BYTES,
    RUNNER_GRACE_S,
    HostedPlace,
    LocalPlace,
    Refuse,
    RunnerPlace,
    ShellArgs,
    ShellPlace,
    _child_env,
    request_settings,
    runner_detail,
    runner_post,
    runner_unavailable,
    shell_place,
    shell_refusal,
)
from felix.tools.types import Tool, ToolInput, ToolInvocationCtx, ToolOutput, define_tool_with_executor

logger = logging.getLogger("felix.tools.shell_background_tools")

# pydantic's Rust regex: `$` is the end of the string, with no trailing-newline allowance.
_BACKGROUND_ID = r"^[0-9a-f]{32}$"
# On top of the call's own wait: a cold sandbox start, as a foreground `exec` allows for.
HOSTED_GRACE_S = 90.0

Op = Literal["start", "output", "list", "kill"]


class ShellBackgroundArgs(ShellArgs):
    """A shell tool with `background: true`: the same arguments, and the choice to come back later."""

    run_in_background: bool = Field(
        default=False,
        description=(
            "Start the command and answer at once with an id, instead of waiting for it to finish. "
            "Read what it writes with shell_output and end it with shell_kill. For a server, a watcher "
            "or a long build; it still stops at this tool's timeout."
        ),
    )


class ShellOutputArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(
        default=None,
        pattern=_BACKGROUND_ID,
        description="The id run_in_background answered. Omit it to list every background command here.",
    )
    wait_ms: int = Field(
        default=0,
        ge=0,
        le=shell_background.MAX_WAIT_MS,
        description="While the command runs, wait up to this long for it to write something new.",
    )


class ShellKillArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=_BACKGROUND_ID, description="The id run_in_background answered.")


class BackgroundProcess(BaseModel):
    """A background command as a runner or a sandbox describes it (`shell_background._describe`),
    held to bounds so a hostile answer is a validation failure, closed like any malformed result."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=_BACKGROUND_ID)
    argv: list[str] = Field(max_length=_MAX_ARGV_ITEMS)
    cwd: str = Field(max_length=4096)
    status: Literal["running", "exited", "killed", "timed_out", "output_exceeded", "failed", "lost"]
    exit_code: int | None = Field(ge=-255, le=255)
    error: str | None = Field(default=None, max_length=1000)
    truncated: bool
    duration_ms: int = Field(ge=0, le=2**53)


class BackgroundOutput(BackgroundProcess):
    stdout: str = Field(max_length=MAX_OUTPUT_BYTES)
    stderr: str = Field(max_length=MAX_OUTPUT_BYTES)
    stdout_skipped: int = Field(ge=0, le=MAX_TOTAL_OUTPUT_BYTES)
    stderr_skipped: int = Field(ge=0, le=MAX_TOTAL_OUTPUT_BYTES)


class BackgroundList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    processes: list[BackgroundProcess] = Field(max_length=shell_background.MAX_KEPT)


# Each op's answer, and what a failure means for it.
_MODEL: dict[Op, type[BaseModel]] = {
    "start": BackgroundProcess,
    "output": BackgroundOutput,
    "list": BackgroundList,
    "kill": BackgroundProcess,
}
_CONSEQUENCE: dict[Op, str] = {
    # Not "did not start": a start the answer was lost for may still be starting.
    "start": "it may or may not have started; shell_output lists what is running",
    "output": "nothing was read",
    "list": "nothing was read",
    "kill": "nothing was ended",
}
_HOSTED_OP: dict[Op, str] = {
    "start": "exec_start",
    "output": "exec_output",
    "list": "exec_output",
    "kill": "exec_kill",
}
_RUNNER_PATH: dict[Op, str] = {"start": "/start", "output": "/output", "list": "/output", "kill": "/kill"}


def _wait_s(op: Op, body: dict[str, Any]) -> float:
    """How long the other side may take before it answers: a start waits for the supervisor to
    detach, a read for new output, an end for the supervisor to report."""
    if op == "start":
        return shell_background.DETACH_WAIT_S
    if op == "kill":
        return shell_background.KILL_WAIT_S
    return int(body.get("wait_ms") or 0) / 1000


def _answer(op: Op, out: Any, argv: list[str] | None = None) -> ToolOutput:
    """`out` as the model reads it, once it has the shape `op` answers; a start's argv is what this
    process asked for, not what the other side says it ran."""
    try:
        result = _MODEL[op].model_validate(out).model_dump()
    except ValidationError:
        # Closed, and not "it did not run": a start whose answer is malformed may have started.
        return tool_error_output(
            ToolErrorCode.TRANSPORT_UNAVAILABLE,
            "[shell] the answer about the background command was malformed; "
            "shell_output lists what is running",
        )
    return json.dumps({**result, "argv": argv} if argv is not None else result)


def _failure(op: Op, exc: Exception, refuse: Refuse) -> ToolOutput:
    """A background call's failure. Only text written for the model is relayed -- the limit's, an
    unknown id's -- and anything else is logged and answered in fixed words."""
    if isinstance(exc, shell_background.BackgroundLimitReached):
        return refuse("background", str(exc))
    if isinstance(exc, shell_background.UnknownBackgroundId):
        return tool_error_output(ToolErrorCode.INVALID_ARGUMENTS, f"[shell] {exc}")
    logger.warning("shell background %s failed", op, exc_info=exc)
    return tool_error_output(
        ToolErrorCode.TRANSPORT_UNAVAILABLE, f"[shell] the {op} failed; {_CONSEQUENCE[op]}"
    )


async def _local(op: Op, call: Callable[[], dict[str, Any]], refuse: Refuse) -> ToolOutput:
    """A background call on this host. Off the loop: a read may wait, and a start waits for the
    supervisor to detach. Anything raised -- a state file a command rewrote included -- is a tool
    error, never an escape."""
    try:
        out = await asyncio.to_thread(call)
    except Exception as exc:
        return _failure(op, exc, refuse)
    return _answer(op, out)


async def _hosted(
    place: HostedPlace, op: Op, body: dict[str, Any], refuse: Refuse, argv: list[str] | None = None
) -> ToolOutput:
    from felix.tools.workspace_hosted import GatewayUnavailable, HostedBackend

    try:
        out = await HostedBackend(place.settings).background(
            place.scope, _HOSTED_OP[op], body, _wait_s(op, body) + HOSTED_GRACE_S
        )
    except GatewayUnavailable as exc:
        # The harness's own words about the gateway, as a foreground `exec` reports them.
        return tool_error_output(ToolErrorCode.TRANSPORT_UNAVAILABLE, f"[shell] {exc}; {_CONSEQUENCE[op]}")
    except ValueError as exc:
        if op != "start":
            return _failure(op, exc, refuse)
        # The sandbox resolved `cwd` as `resolve_cwd` does here.
        return refuse("cwd", str(exc))
    except Exception as exc:
        return _failure(op, exc, refuse)
    return _answer(op, out, argv)


async def _remote(
    place: RunnerPlace, op: Op, body: dict[str, Any], refuse: Refuse, argv: list[str] | None = None
) -> ToolOutput:
    if place.scope:
        body = {**body, "scope": place.scope}
    consequence = _CONSEQUENCE[op]
    answer = await runner_post(
        place.url, place.token, _RUNNER_PATH[op], body, _wait_s(op, body) + RUNNER_GRACE_S, consequence
    )
    if not isinstance(answer, tuple):
        return answer
    status, raw = answer
    detail = runner_detail(raw)
    if status == 200:
        try:
            out = json.loads(raw)
        except ValueError:
            out = None
        return _answer(op, out, argv)
    if status == 403:
        return refuse("runner", f"the shell runner refused it: {detail}")
    if status == 404 and detail:
        return tool_error_output(ToolErrorCode.INVALID_ARGUMENTS, f"[shell] {detail}")
    if status == 401:
        return runner_unavailable("rejected the token (FELIX_SHELL_RUNNER_TOKEN must match)", consequence)
    if status == 422 and detail:
        return tool_error_output(ToolErrorCode.TRANSPORT_UNAVAILABLE, f"[shell] {detail}")
    return runner_unavailable(f"answered HTTP {status}", consequence)


async def start_in_background(
    place: ShellPlace,
    refuse: Refuse,
    *,
    argv: list[str],
    cwd: Path | None,
    cwd_rel: str,
    stdin: str | None,
    timeout_s: float,
) -> ToolOutput:
    """`argv`, checked as a foreground command is, started at `place` under a supervisor."""
    body: dict[str, Any] = {"argv": argv, "cwd": cwd_rel, "timeout_ms": max(1, int(timeout_s * 1000))}
    if stdin is not None:
        body["stdin"] = stdin
    match place:
        case HostedPlace():
            return await _hosted(place, "start", body, refuse, argv)
        case RunnerPlace():
            return await _remote(place, "start", body, refuse, argv)
    assert cwd is not None
    start = functools.partial(
        shell_background.start_background,
        argv,
        cwd=cwd,
        root=place.root,
        stdin=stdin,
        timeout_s=timeout_s,
        env=_child_env(),
        owner=_tenant(),
    )
    return await _local("start", start, refuse)


def _tenant() -> str:
    """The call's tenant, whose share of this host a local start counts against."""
    req = try_get_context()
    auth = req.auth if req is not None else None
    return str(getattr(auth, "tenant_id", "") or "")


class _BackgroundExecutor:
    """`shell_output` and `shell_kill`: a background command read or ended where `shell_place`
    says shell calls run -- the hosted sandbox, the shell runner or, on a development box, here."""

    transport = "shell"

    def __init__(self, *, name: str, action: Literal["output", "kill"], settings: Any) -> None:
        self._action = action
        self._settings = settings
        self._refuse: Refuse = functools.partial(shell_refusal, name)

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        _ = ctx
        process_id = args.get("id")
        op: Op = "kill" if self._action == "kill" else "output" if process_id is not None else "list"
        body: dict[str, Any] = {"id": str(process_id) if process_id is not None else None}
        if self._action == "output":
            body["wait_ms"] = int(args.get("wait_ms") or 0)
        place = shell_place(request_settings(self._settings), self._refuse)
        match place:
            case HostedPlace():
                return await _hosted(place, op, body, self._refuse)
            case RunnerPlace():
                return await _remote(place, op, body, self._refuse)
            case LocalPlace():
                return await _local(op, _local_call(op, place.root, body), self._refuse)
        return place


def _local_call(op: Op, root: Path, body: dict[str, Any]) -> Callable[[], dict[str, Any]]:
    if op == "kill":
        return functools.partial(shell_background.kill_background, root, body["id"])
    if op == "list":
        return functools.partial(shell_background.list_background, root)
    return functools.partial(shell_background.read_background, root, body["id"], body["wait_ms"])


def background_tools(settings: Any) -> list[Tool]:
    """`shell_output` and `shell_kill`, bound once beside every shell tool that may start one."""
    output_name, kill_name = SHELL_BACKGROUND_TOOLS
    return [
        define_tool_with_executor(
            name=output_name,
            description=(
                "Read a background command started with run_in_background: its status and what it "
                "wrote since the last read (at most the last 64 KB of each stream). Without an id, "
                "list every background command here."
            ),
            args=ShellOutputArgs,
            executor=_BackgroundExecutor(name=output_name, action="output", settings=settings),
            source="shell",
            read_only=True,
        ),
        define_tool_with_executor(
            name=kill_name,
            description="End a background command started with run_in_background, and its children.",
            args=ShellKillArgs,
            executor=_BackgroundExecutor(name=kill_name, action="kill", settings=settings),
            source="shell",
        ),
    ]


__all__ = [
    "HOSTED_GRACE_S",
    "BackgroundList",
    "BackgroundOutput",
    "BackgroundProcess",
    "ShellBackgroundArgs",
    "ShellKillArgs",
    "ShellOutputArgs",
    "background_tools",
    "start_in_background",
]
