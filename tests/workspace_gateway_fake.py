"""A fake workspace gateway at the transport, serving the real `felix-fs` helper.

The gateway Worker (deploy/cloudflare/workspace-gateway) authenticates, names the scope's sandbox and
runs the helper in it; this does the same in-process, with a directory per scope standing in for each
sandbox and the helper module itself doing the operations. So what `HostedBackend` is tested against
is the wire contract the Worker's own tests hold it to, and the file code the sandbox really runs.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

HELPER = Path(__file__).resolve().parents[1] / "deploy/cloudflare/workspace-gateway/helper/felix_fs.py"
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
        helper = _helper()
        previous, helper.ROOT = helper.ROOT, directory
        try:
            # Off the event loop: `exec` runs its own (`asyncio.run`), as it does in the sandbox.
            answer = await asyncio.to_thread(helper.run, {"op": op, **body})
        finally:
            helper.ROOT = previous
        if answer["ok"]:
            return httpx.Response(200, json={"result": answer["result"]})
        refusal = {k: answer[k] for k in ("error", "message", "kind") if k in answer}
        return httpx.Response(_STATUS[answer["error"]], json=refusal)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
