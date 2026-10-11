"""A fake workspace gateway at the transport, serving the real `felix-fs` helper.

The gateway Worker (deploy/cloudflare/workspace-gateway) authenticates, names the scope's sandbox and
runs the helper in it; this does the same in-process, with a directory per scope standing in for each
sandbox and the helper module itself doing the operations. So what `HostedBackend` is tested against
is the wire contract the Worker's own tests hold it to, and the file code the sandbox really runs.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

HELPER = Path(__file__).resolve().parents[2] / "deploy/cloudflare/workspace-gateway/helper/felix_fs.py"
URL = "https://gateway.test"
TOKEN = "g" * 40

_STATUS = {
    "bad_request": 400,
    "unauthorized": 401,
    "permission_denied": 403,
    "not_found": 404,
    "not_a_directory": 409,
    "not_a_file": 409,
    "invalid_path": 422,
    "edit_refused": 422,
    "io_error": 500,
    "conflict": 409,
    "workspace_changed": 409,
    "target_exists": 409,
    "not_a_folder": 409,
    "reserved_path": 422,
    "too_many_entries": 409,
    "too_deep": 409,
    "clone_failed": 502,
    "unavailable": 503,
    "timeout": 504,
}


def _helper() -> Any:
    if "felix_fs" in sys.modules:
        return sys.modules["felix_fs"]
    spec = importlib.util.spec_from_file_location("felix_fs", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["felix_fs"] = module
    spec.loader.exec_module(module)
    return module


@dataclass
class FakeGateway:
    """Sandboxes are directories under `root`, one per `tenant/key`, made on first use."""

    root: Path
    calls: list[tuple[str, str]] = field(default_factory=list)  # (scope, op)
    checkpoints: list[str] = field(default_factory=list)
    down: bool = False
    # Where `clone` fetches `{repo}.git` from, standing in for github.com (the `git_server` fixture).
    clone_base: str = ""
    clone_tokens: list[str] = field(default_factory=list)

    def sandbox(self, scope: str) -> Path:
        return self.root / scope

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("gateway down", request=request)
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(
                401, json={"error": "unauthorized", "message": "a valid bearer is required"}
            )
        parts = request.url.path.split("/")
        # /v1/workspaces/{tenant}/{key}/{op}
        assert parts[:3] == ["", "v1", "workspaces"] and len(parts) == 6, request.url.path
        scope, op = f"{parts[3]}/{parts[4]}", parts[5]
        self.calls.append((scope, op))
        body = json.loads(request.content or b"{}")
        if op == "checkpoint":
            self.checkpoints.append(scope)
            return httpx.Response(200, json={"result": {"backed_up": True, "size": 1}})
        if op == "destroy":
            shutil.rmtree(self.sandbox(scope), ignore_errors=True)
            return httpx.Response(200, json={"result": {}})
        directory = self.sandbox(scope)
        directory.mkdir(parents=True, exist_ok=True)
        if op == "clone":
            return await self._clone(directory, body)
        helper = _helper()
        previous, helper.ROOT = helper.ROOT, directory
        try:
            # Off the event loop: `exec` runs its own (`asyncio.run`), as it does in the sandbox.
            answer = await asyncio.to_thread(helper.run, {"op": op, **body})
        finally:
            helper.ROOT = previous
        if answer["ok"]:
            return httpx.Response(200, json={"result": answer["result"]})
        refusal = {
            k: answer[k] for k in ("error", "message", "kind", "sha256", "bytes", "count") if k in answer
        }
        return httpx.Response(_STATUS[answer["error"]], json=refusal)

    async def _clone(self, directory: Path, body: dict[str, Any]) -> httpx.Response:
        """The Worker's `clone`: refused into a non-empty workspace, and the token added to the
        request outside the clone's config (the Worker's GitHub intercept). Run through the
        helper's own `_git_exec`, whose environment is built from nothing -- the sandbox's git."""
        if any(directory.iterdir()):
            return httpx.Response(409, json={"error": "conflict", "message": "the workspace is not empty"})
        self.clone_tokens.append(body["token"])
        basic = base64.b64encode(f"x-access-token:{body['token']}".encode()).decode()
        helper = _helper()
        args = ("-c", f"http.extraHeader=Authorization: Basic {basic}", "-c", "credential.helper=",
                "clone", "-q", "--branch", body["branch"], "--origin", "origin", "--",
                f"{self.clone_base}/{body['repo']}.git", ".")  # fmt: skip
        cloned = await helper._git_exec(directory, *args, stdin=None, limit=65_536)
        if cloned.code != 0:
            for child in directory.iterdir():
                shutil.rmtree(child) if child.is_dir() else child.unlink()
            tail = " ".join(cloned.err.strip().splitlines()[-3:])
            return httpx.Response(502, json={"error": "clone_failed", "message": f"git clone failed: {tail}"})
        head = await helper._git_exec(directory, "rev-parse", "HEAD", stdin=None, limit=1024)
        result = {"repo": body["repo"], "branch": body["branch"], "head": head.out.decode().strip()}
        return httpx.Response(200, json={"result": result})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
