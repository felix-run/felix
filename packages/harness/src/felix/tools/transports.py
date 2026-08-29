"""Tool transports — HTTP (SSRF-guarded) and local echo.

Governance may label tools as ``sandbox`` / ``container``; a Docker sandbox
executor is available behind ``felix-harness[sandbox]``. Core remains HTTP + local.
"""

from __future__ import annotations

import asyncio
from typing import Any

from felix.security.ssrf import assert_safe_outbound_url
from felix.tools.types import ToolInput, ToolInvocationCtx, ToolOutput


class HttpExecutor:
    """Outbound HTTPS tool transport (SSRF-guarded)."""

    transport = "http"

    def __init__(self, url: str, *, method: str = "POST", allow_http: bool = False) -> None:
        assert_safe_outbound_url(url, allow_http=allow_http)
        self._url = url
        self._method = method.upper()
        self._allow_http = allow_http

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        import httpx

        _ = ctx
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            resp = await client.request(self._method, self._url, json=args)
            resp.raise_for_status()
            return resp.text


class EchoExecutor:
    """Dev transport that echoes args — useful for ladder tests."""

    transport = "local"

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        _ = ctx
        return str(args)


class SandboxExecutor:
    """Optional Docker sandbox rung (requires ``felix-harness[sandbox]``).

    Runs a short-lived container with the JSON args in the ``FELIX_SANDBOX_INPUT``
    environment variable. Not stdin: ``containers.run()`` has no way to write to a
    container's stdin, and passing ``input=`` there — which is ``subprocess.run``'s
    signature, not docker-py's — failed every call against a real daemon.
    Disabled unless the docker SDK is importable.
    """

    transport = "sandbox"

    def __init__(
        self,
        *,
        image: str = "python:3.14-slim",
        command: list[str] | None = None,
        network_disabled: bool = True,
        mem_limit: str = "256m",
        user: str = "65534:65534",
        read_only: bool = True,
        pids_limit: int = 128,
        nano_cpus: int = 1_000_000_000,
    ) -> None:
        self._image = image
        self._command = command or [
            "python",
            "-c",
            "import sys, os, json; print(json.loads(os.environ['FELIX_SANDBOX_INPUT']))",
        ]
        self._network_disabled = network_disabled
        self._mem_limit = mem_limit
        self._user = user
        self._read_only = read_only
        self._pids_limit = pids_limit
        self._nano_cpus = nano_cpus

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        import json

        _ = ctx
        try:
            import docker
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "SandboxExecutor requires felix-harness[sandbox] (uv sync --extra sandbox)"
            ) from exc

        client = docker.from_env()
        payload = json.dumps(args)

        def _run() -> Any:
            return client.containers.run(
                self._image,
                self._command,
                environment={"FELIX_SANDBOX_INPUT": payload},
                network_disabled=self._network_disabled,
                mem_limit=self._mem_limit,
                remove=True,
                stdout=True,
                stderr=True,
                # Confinement. Without these the sandbox was network-off and
                # memory-capped and nothing else: it ran as root, could write anywhere in
                # its filesystem, fork without limit, and keep every Linux capability.
                user=self._user,
                read_only=self._read_only,
                pids_limit=self._pids_limit,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                nano_cpus=self._nano_cpus,
                # A read-only rootfs still needs somewhere to write; keep it small,
                # in-memory, and non-executable.
                tmpfs={"/tmp": "rw,noexec,nosuid,size=64m"},
            )

        try:
            # docker-py is synchronous. Calling it directly from a coroutine meant the
            # asyncio.wait_for timeout around this executor could never fire — nothing
            # yields — and the whole API event loop stalled for the container's lifetime,
            # so a model emitting `while True: pass` froze every concurrent request.
            out = await asyncio.to_thread(_run)
        except Exception as exc:
            return f"sandbox_error: {exc}"
        if isinstance(out, bytes):
            return out.decode("utf-8", errors="replace")
        return str(out)


__all__ = ["EchoExecutor", "HttpExecutor", "SandboxExecutor"]
