"""`open_file`: the agent asking the client to show the user a workspace file.

The harness opens nothing. The tool answers whether the file is there to open, and the client
opens what a successful result names, in its file pane. That is the whole design, because the
result reaches every client on every path (`tool_end` live, `session_event` on a durable run)
while a side event never leaves the API process. So what is pinned here is the answer: a
result naming the file the pane's own route would serve, and the same refusals `read_file`
gives -- on both backends, word for word -- so a client is never asked to open what it cannot
then read.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.manifests.loader import load_manifest_file
from felix.manifests.schema import Manifest
from felix.tools import workspace_hosted
from felix.tools.builtins import default_tool_provider
from felix.tools.types import ToolInvocationCtx, tool_output_content
from felix.tools.workspace import WORKSPACE_TOOL_NAMES
from felix.tools.workspace_scope import SCOPES_DIR, thread_key
from felix.usage.catalog import workspace_summary

from tests.support.workspace_gateway_fake import TOKEN, URL, FakeGateway

ROOT = Path(__file__).resolve().parents[2]
TENANT = "acme"
THREAD = "acme:t1"


@pytest.fixture
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGateway:
    fake = FakeGateway(root=tmp_path / "sandboxes")
    monkeypatch.setattr(workspace_hosted, "gateway_client", lambda settings: fake.client())
    return fake


def _settings(base: Path, backend: str) -> Settings:
    ws = base / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    extra: dict[str, Any] = {}
    if backend == "hosted":
        extra = {
            "workspace_backend": "hosted",
            "workspace_gateway_url": URL,
            "workspace_gateway_token": TOKEN,
        }
    return Settings(workspace_root=str(ws), **extra)


def _scope_dir(base: Path, backend: str, gateway: FakeGateway) -> Path:
    key = thread_key(TENANT, THREAD)
    path = (
        base / "workspace" / SCOPES_DIR / TENANT / key if backend == "local" else gateway.root / TENANT / key
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


@asynccontextmanager
async def _request(settings: Settings) -> AsyncIterator[None]:
    ctx = RequestContext(settings=settings, auth=AuthContext(tenant_id=TENANT), thread_id=THREAD)
    async with async_run_with_context(ctx):
        yield


async def _call(tool: str, args: dict[str, Any]) -> str:
    out = await default_tool_provider().get(tool).executor.execute(args, ToolInvocationCtx(thread_id=THREAD))
    return tool_output_content(out)


def _seed(here: Path, outside: Path) -> None:
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("not yours")
    (here / "notes").mkdir()
    (here / "notes" / "plan.md").write_text("plan")
    (here / "README.md").write_text("# hi\n")
    (here / ".felix-edit-abc123").write_text("half an edit")
    (here / ".git").mkdir()
    (here / ".git" / "config").write_text("[core]")
    (here / "big.log").write_bytes(b"x" * 512_001)
    os.symlink(outside / "secret.txt", here / "link.txt")
    os.symlink(outside, here / "escape")


OPENS = ["README.md", "notes/plan.md", "./notes/plan.md"]
REFUSES = [
    "missing.md",
    "notes",
    ".",
    "link.txt",
    "escape/secret.txt",
    "../outside/secret.txt",
    "/etc/passwd",
    # Readable by `read_file`, refused by the pane's own route -- so refused here.
    ".felix-edit-abc123",
    ".git/config",
    ".GIT/config",
    "big.log",
]


@pytest.mark.parametrize("backend", ["local", "hosted"])
async def test_both_backends_open_and_refuse_the_same_way(
    tmp_path: Path, gateway: FakeGateway, backend: str
) -> None:
    here = _scope_dir(tmp_path, backend, gateway)
    _seed(here, tmp_path / "outside")
    async with _request(_settings(tmp_path, backend)):
        opened = [json.loads(await _call("open_file", {"path": p})) for p in OPENS]
        refused = [await _call("open_file", {"path": p}) for p in REFUSES]
        # What `read_file` says about the same paths: the pane reads through the same backend,
        # so an open it would refuse is one the client could not then show.
        read = [await _call("read_file", {"path": p}) for p in REFUSES[:7]]

    assert opened == [
        {"opened": "README.md", "kind": "file", "in": "side_pane", "size": 5},
        {"opened": "notes/plan.md", "kind": "file", "in": "side_pane", "size": 4},
        {"opened": "notes/plan.md", "kind": "file", "in": "side_pane", "size": 4},
    ]
    assert refused == [
        "[tool error/invalid_arguments] not a file: missing.md",
        "[tool error/invalid_arguments] not a file: notes",
        "[tool error/invalid_arguments] not a file: .",
        "[tool error/invalid_arguments] symlinks are not followed in workspace paths: link.txt",
        "[tool error/invalid_arguments] symlinks are not followed in workspace paths: escape",
        "[tool error/invalid_arguments] path escapes workspace root",
        "[tool error/invalid_arguments] absolute paths are not allowed",
        "[tool error/invalid_arguments] reserved path: .felix-edit-abc123 (the file pane does not show it)",
        "[tool error/invalid_arguments] reserved path: .git/config (the file pane does not show it)",
        "[tool error/invalid_arguments] reserved path: .GIT/config (the file pane does not show it)",
        "[tool error/invalid_arguments] too large to show: big.log (512001 bytes; the pane shows up to 512000)",
    ]
    assert refused[:7] == read
    # Opening changes nothing.
    assert (here / "README.md").read_text() == "# hi\n"


def test_it_is_a_read_only_workspace_tool() -> None:
    tool = default_tool_provider().get("open_file")
    assert tool.read_only and tool.replay_safe
    # So an agent binding it is reported as one whose files live on the harness, and chat-ui
    # draws the harness file tree the opened file belongs to.
    assert "open_file" in WORKSPACE_TOOL_NAMES
    m = Manifest.model_validate(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "x"},
            "spec": {"tools": ["open_file"]},
        }
    )
    assert workspace_summary(m)["tools"] == "server"


def test_contributor_binds_it_and_triage_does_not() -> None:
    # The contributor works with a person beside it; triage runs on a webhook with nobody to show.
    contributor = load_manifest_file(ROOT / "manifests" / "self" / "contributor.yaml")
    triage = load_manifest_file(ROOT / "manifests" / "self" / "triage.yaml")
    assert "open_file" in contributor.spec.tools
    assert "open_file" not in triage.spec.tools
