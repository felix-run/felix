"""`felix-shell-runner` — exec `spec.shell_tools` argv somewhere that holds no secrets.

The shell tool runs repository code: `make test` imports whatever the agent just wrote. As a
child of the API that code is the API's user in the API's container, so it can read
`/proc/<api pid>/environ` — the GitHub token, model keys, database credentials. This process is
the other place it can run: a separate container sharing only the workspace volume, started
with nothing in its environment worth reading. The API keeps every check it makes today and
sends the argv here (`FELIX_SHELL_RUNNER_URL`); this process checks it again and execs it
through the same code the local path uses (`felix.tools.shell.exec_argv`), so the scrubbed
environment, the process-group kill and the bounded output are one implementation.

Two endpoints. `GET /health` answers anyone. `POST /run` requires
`Authorization: Bearer $FELIX_SHELL_RUNNER_TOKEN`, compared in constant time, before the body
is parsed; then the argv must sit under this process's own `FELIX_SHELL_ALLOWED_COMMANDS` and
`cwd` under its own `FELIX_WORKSPACE_ROOT`. A refusal is a 403 carrying the reason; a command
that cannot be spawned is a 422; the result is the same JSON object the local path returns.

What the token is for: keeping other containers on the network from running commands here. It
is not a secret from the code this process runs — that code can read this process's environment
exactly as it could the API's — and holding it grants only what that code already has.

When the caller disconnects (the API's turn was cancelled, or its HTTP timeout fired) the
command's process group is killed, as it would be locally.

Imports nothing that opens a database, a cache or a model client: `Settings` is read for four
fields and `validate_runtime` is never called, so the container needs none of their variables.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field, ValidationError

from felix.config import MIN_SHELL_RUNNER_TOKEN_CHARS
from felix.manifests.schema import MAX_INTEGRATION_TIMEOUT_MS
from felix.security.shell_policy import ShellNotAllowedError, allowed_prefixes, assert_argv_allowed
from felix.tools.shell import DEFAULT_SHELL_TIMEOUT_S, ShellArgs, exec_argv, resolve_cwd

if TYPE_CHECKING:
    from felix.config import Settings

logger = logging.getLogger("felix.shell_runner")

# argv (64 KB) plus stdin (256 K characters, up to four bytes each) plus JSON escaping.
MAX_REQUEST_BYTES = 2 * 1024 * 1024
# How often a running command checks whether its caller is still there.
_DISCONNECT_POLL_S = 0.5


class RunRequest(ShellArgs):
    """`ShellArgs` — the same bounds the tool's own arguments carry — plus the deadline."""

    timeout_ms: int = Field(default=int(DEFAULT_SHELL_TIMEOUT_S * 1000), gt=0, le=MAX_INTEGRATION_TIMEOUT_MS)


# What the caller is told. Fixed text, never an exception's: the detail is logged here, where an
# operator reads it, and the reply carries only the stable reason the API maps to a tool error.
_DENIED_MESSAGES = {
    "argv": "the command is not on this runner's allowlist",
    "cwd": "the working directory is not inside this runner's workspace",
}


def _denied(reason: str, detail: str) -> JSONResponse:
    logger.warning("shell_runner_denied reason=%s detail=%s", reason, detail)
    return JSONResponse({"reason": reason, "message": _DENIED_MESSAGES[reason]}, status_code=403)


def _authorized(request: Request, token: str) -> bool:
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return False
    return hmac.compare_digest(presented.strip().encode("utf-8"), token.encode("utf-8"))


async def _read_body(request: Request) -> bytes | None:
    """The body, or None past `MAX_REQUEST_BYTES` — read in chunks, never whole first."""
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > MAX_REQUEST_BYTES:
            return None
    return bytes(buf)


async def run_until_disconnected(work: Any, is_disconnected: Any) -> tuple[bool, Any]:
    """Await `work` unless `is_disconnected()` turns true first; then cancel it.

    Returns `(completed, result)`. Cancelling `exec_argv` is what kills the process group —
    its `finally` does — so a caller that goes away takes its command with it.
    """
    run = asyncio.ensure_future(work)

    async def _watch() -> None:
        # A poll, not an event: `Request.is_disconnected` is the portable question across
        # Granian and uvicorn, and a raw `receive()` loop's behaviour after the body differs.
        while True:
            if await is_disconnected():
                return
            await asyncio.sleep(_DISCONNECT_POLL_S)

    watch = asyncio.ensure_future(_watch())
    try:
        await asyncio.wait({run, watch}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        watch.cancel()
        if not run.done():
            run.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await run
    if run.cancelled():
        return False, None
    return True, run.result()


def create_runner_app(settings: Settings | None = None) -> FastAPI:
    """The runner's ASGI app. Refuses to build without a usable token."""
    if settings is None:
        from felix.config import get_settings

        settings = get_settings()
    token = settings.shell_runner_token.strip()
    if len(token) < MIN_SHELL_RUNNER_TOKEN_CHARS:
        raise RuntimeError(
            f"felix-shell-runner needs FELIX_SHELL_RUNNER_TOKEN (at least {MIN_SHELL_RUNNER_TOKEN_CHARS} "
            "characters); it will not serve an unauthenticated exec endpoint."
        )
    if not allowed_prefixes(settings):
        logger.warning("FELIX_SHELL_ALLOWED_COMMANDS is empty; every /run will be refused")

    app = FastAPI(title="felix-shell-runner", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/run")
    async def run(request: Request) -> Response:
        # Authenticate before reading a byte of the body: an unauthenticated caller learns
        # nothing about what this endpoint would accept.
        if not _authorized(request, token):
            return JSONResponse({"message": "unauthorized"}, status_code=401)
        raw = await _read_body(request)
        if raw is None:
            return JSONResponse({"message": "request too large"}, status_code=413)
        try:
            req = RunRequest.model_validate_json(raw)
        except ValidationError as exc:
            return JSONResponse(
                {"message": f"invalid request: {exc.error_count()} error(s)"}, status_code=400
            )
        # Defence in depth: this process's own allowlist, as both sides of the tool and the
        # operator. The API already checked its manifest's prefixes; this checks the host's.
        try:
            assert_argv_allowed(req.argv, allowed_prefixes(settings), settings)
        except ShellNotAllowedError as exc:
            return _denied("argv", str(exc))
        try:
            root = _workspace_root(settings)
            cwd = resolve_cwd(root, req.cwd)
        except ValueError as exc:
            return _denied("cwd", str(exc))
        timeout_s = req.timeout_ms / 1000
        try:
            completed, result = await run_until_disconnected(
                exec_argv(req.argv, cwd=cwd, root=root, stdin=req.stdin, timeout_s=timeout_s),
                request.is_disconnected,
            )
        except OSError:
            # Logged, not returned: the text names paths inside this container.
            logger.warning("shell_runner_exec_failed", exc_info=True)
            return JSONResponse({"message": "the command could not be started"}, status_code=422)
        if not completed:
            # Nobody is reading this; the process group is already dead.
            return Response(status_code=499)
        return JSONResponse(result)

    return app


def _workspace_root(settings: Settings) -> Path:
    raw = (settings.workspace_root or "").strip()
    if not raw:
        raise ValueError("FELIX_WORKSPACE_ROOT is not configured on the runner")
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"FELIX_WORKSPACE_ROOT is not a directory on the runner: {root}")
    return root


def create_application() -> FastAPI:
    """ASGI factory for granian/uvicorn ``felix.shell_runner:create_application``."""
    return create_runner_app()


def main() -> None:
    """Console script: serve the runner on FELIX_HOST:FELIX_PORT, Granian first, like the API."""
    from felix.config import get_settings

    settings = get_settings()
    # Fail here, in the foreground, rather than inside a Granian worker that respawns forever.
    create_runner_app(settings)
    try:
        from granian.constants import Interfaces
        from granian.server import Server

        Server(
            "felix.shell_runner:create_application",
            address=settings.host,
            port=settings.port,
            interface=Interfaces.ASGI,
            workers=1,
            factory=True,
        ).serve()
    except ImportError:
        import uvicorn

        uvicorn.run(
            "felix.shell_runner:create_application",
            host=settings.host,
            port=settings.port,
            factory=True,
            workers=1,
        )


__all__ = ["MAX_REQUEST_BYTES", "RunRequest", "create_application", "create_runner_app", "main"]
