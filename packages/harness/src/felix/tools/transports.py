"""Tool transports — HTTP (SSRF-guarded) and local echo.

Governance may label tools as ``sandbox`` / ``container``; a Docker sandbox
executor is available behind ``felix-harness[sandbox]``. Core remains HTTP + local.
"""

from __future__ import annotations

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

    async def execute(
        self, args: ToolInput, ctx: ToolInvocationCtx | None = None
    ) -> ToolOutput:
        import httpx

        _ = ctx
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            resp = await client.request(self._method, self._url, json=args)
            resp.raise_for_status()
            return resp.text


class EchoExecutor:
    """Dev transport that echoes args — useful for ladder tests."""

    transport = "local"

    async def execute(
        self, args: ToolInput, ctx: ToolInvocationCtx | None = None
    ) -> ToolOutput:
        _ = ctx
        return str(args)


class SandboxExecutor:
    """Optional Docker sandbox rung (requires ``felix-harness[sandbox]``).

    Runs a short-lived container with JSON args as stdin. Disabled unless the
    docker SDK is importable.
    """

    transport = "sandbox"

    def __init__(
        self,
        *,
        image: str = "python:3.14-slim",
        command: list[str] | None = None,
        network_disabled: bool = True,
        mem_limit: str = "256m",
    ) -> None:
        self._image = image
        self._command = command or ["python", "-c", "import sys; print(sys.stdin.read())"]
        self._network_disabled = network_disabled
        self._mem_limit = mem_limit

    async def execute(
        self, args: ToolInput, ctx: ToolInvocationCtx | None = None
    ) -> ToolOutput:
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
        try:
            out = client.containers.run(
                self._image,
                self._command,
                input=payload.encode("utf-8"),
                network_disabled=self._network_disabled,
                mem_limit=self._mem_limit,
                remove=True,
                stdout=True,
                stderr=True,
            )
        except Exception as exc:
            return f"sandbox_error: {exc}"
        if isinstance(out, bytes):
            return out.decode("utf-8", errors="replace")
        return str(out)


__all__ = ["EchoExecutor", "HttpExecutor", "SandboxExecutor"]
