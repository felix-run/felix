"""Sandbox and container tools from manifest refs."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from felix.manifests.schema import ContainerRef, SandboxRef
from felix.security.ssrf import assert_safe_outbound_url
from felix.tools.transports import SandboxExecutor
from felix.tools.types import (
    Tool,
    ToolInput,
    ToolInvocationCtx,
    ToolOutput,
    define_tool_with_executor,
)


class SandboxArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(default="", description="Python source to run in the sandbox.")
    path: str | None = Field(default=None, description="Optional workspace path the snippet may touch.")
    stdin: dict[str, Any] | None = Field(
        default=None, description="Optional JSON payload written to the container stdin."
    )


class ContainerArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload: dict[str, Any] = Field(
        default_factory=dict, description="JSON body forwarded to the container gateway."
    )


class _SandboxToolExecutor:
    transport = "sandbox"

    def __init__(
        self,
        *,
        image: str,
        path_prefix: str,
        timeout_s: float,
        network_disabled: bool = True,
        mem_limit: str = "256m",
    ) -> None:
        self._image = image
        self._path_prefix = path_prefix
        self._timeout_s = timeout_s
        self._network_disabled = network_disabled
        self._mem_limit = mem_limit

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        path = str(args.get("path") or "")
        if self._path_prefix and path and not path.startswith(self._path_prefix):
            return f"sandbox_error: path must start with {self._path_prefix!r}"
        code = str(args.get("code") or "")
        command = ["python", "-c", code] if code else ["python", "-c", "import sys; print(sys.stdin.read())"]
        runner = SandboxExecutor(
            image=self._image,
            command=command,
            network_disabled=self._network_disabled,
            mem_limit=self._mem_limit,
        )
        stdin = args.get("stdin")
        payload: dict[str, Any]
        if isinstance(stdin, dict):
            payload = dict(stdin)
        elif stdin is not None:
            payload = {"stdin": stdin}
        else:
            payload = {k: v for k, v in args.items() if k not in {"code", "path"}}
        try:
            return await asyncio.wait_for(runner.execute(payload, ctx), timeout=self._timeout_s)
        except TimeoutError:
            return "sandbox_error: timed out"
        except RuntimeError as exc:
            return f"sandbox_error: {exc}"


class _ContainerExecutor:
    transport = "container"

    def __init__(
        self,
        *,
        gateway_url: str,
        image: str,
        timeout_ms: int | None,
        auth: str,
        allow_http: bool,
    ) -> None:
        assert_safe_outbound_url(gateway_url, allow_http=allow_http)
        self._url = gateway_url
        self._image = image
        self._timeout = (timeout_ms or 30_000) / 1000.0
        self._auth = auth

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if not self._auth:
            return headers
        if self._auth.lower().startswith("bearer ") or self._auth.lower().startswith("basic "):
            headers["authorization"] = self._auth
        else:
            headers["authorization"] = f"Bearer {self._auth}"
        return headers

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        _ = ctx
        body = {
            "image": self._image,
            "payload": args.get("payload") if "payload" in args else args,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as client:
                resp = await client.post(self._url, json=body, headers=self._headers())
                resp.raise_for_status()
                return resp.text
        except Exception as exc:
            return f"container_error: {exc}"


DEFAULT_SANDBOX_IMAGE = "python:3.14-slim"


class SandboxImageNotAllowed(ValueError):
    """A manifest asked for a container image the operator has not allowed."""


def allowed_sandbox_images(settings: Any | None = None) -> frozenset[str]:
    """Images sandbox tools may run. Always includes the built-in default."""
    if settings is None:
        from felix.config import get_settings

        settings = get_settings()
    raw = getattr(settings, "sandbox_allowed_images", "") or ""
    extra = {part.strip() for part in raw.split(",") if part.strip()}
    return frozenset({DEFAULT_SANDBOX_IMAGE, *extra})


def assert_sandbox_image_allowed(image: str, settings: Any | None = None) -> None:
    allowed = allowed_sandbox_images(settings)
    if image not in allowed:
        raise SandboxImageNotAllowed(
            f"sandbox image {image!r} is not in FELIX_SANDBOX_ALLOWED_IMAGES "
            f"(allowed: {', '.join(sorted(allowed))})"
        )


def tools_from_sandboxes(refs: list[SandboxRef], *, settings: Any | None = None) -> list[Tool]:
    out: list[Tool] = []
    for ref in refs:
        image = ref.binding or DEFAULT_SANDBOX_IMAGE
        # `binding` is manifest-supplied and reaches `docker run`, so an unrestricted
        # value is arbitrary image pull-and-run on the host holding the Docker socket.
        assert_sandbox_image_allowed(image, settings)
        timeout_s = max(1.0, int(ref.timeout_ms or 30_000) / 1000)
        name = ref.sandbox_tool_name or ref.name
        out.append(
            define_tool_with_executor(
                name=name,
                description=ref.description
                or f"Run Python in a short-lived sandbox ({image}, {int(timeout_s)}s).",
                args=SandboxArgs,
                executor=_SandboxToolExecutor(
                    image=image,
                    path_prefix=ref.path_prefix,
                    timeout_s=timeout_s,
                ),
                source="sandbox",
                fatal=ref.fatal,
            )
        )
    return out


def tools_from_containers(
    refs: list[ContainerRef],
    *,
    allow_http: bool = False,
) -> list[Tool]:
    out: list[Tool] = []
    for ref in refs:
        name = ref.container_tool_name or ref.name
        out.append(
            define_tool_with_executor(
                name=name,
                description=ref.description or f"Invoke container image {ref.image} via {ref.gateway_url}.",
                args=ContainerArgs,
                executor=_ContainerExecutor(
                    gateway_url=ref.gateway_url,
                    image=ref.image,
                    timeout_ms=ref.timeout_ms,
                    auth=ref.auth,
                    allow_http=allow_http,
                ),
                source="container",
                fatal=ref.fatal,
            )
        )
    return out


__all__ = ["tools_from_containers", "tools_from_sandboxes"]
