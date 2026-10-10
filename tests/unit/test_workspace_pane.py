"""The file pane's two backend operations, `tree` and `write_file_checked`, held to one behaviour.

The routes (`tests/e2e/test_workspace_files.py`) run on the local backend. Here the same walk and
the same compare-then-write run against `local` and against `hosted` -- a fake gateway serving the
real `felix-fs` helper (`tests/workspace_gateway_fake.py`) -- and must give the same answers, so a
pane cannot tell which backend served it. Plus what only a unit can reach: the depth and batch
bounds of the local walk, and the SDK's three methods on the wire.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from felix.config import Settings
from felix.tools import workspace_hosted, workspace_local
from felix.tools.workspace_backend import WorkspaceChanged, WorkspaceScope, get_workspace_backend
from felix.tools.workspace_scope import SCOPES_DIR, thread_key
from felix.usage.catalog import workspace_summary
from felix_client import FelixClient

from tests.unit.test_sdk_interrupts import _bind
from tests.workspace_gateway_fake import TOKEN, URL, FakeGateway

TENANT = "acme"
THREAD = "acme:t1"
SCOPE = WorkspaceScope(TENANT, THREAD, "thread")


@pytest.fixture
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGateway:
    fake = FakeGateway(root=tmp_path / "sandboxes")
    monkeypatch.setattr(workspace_hosted, "gateway_client", lambda settings: fake.client())
    return fake


def _settings(tmp_path: Path, backend: str) -> Settings:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    extra: dict[str, Any] = {}
    if backend == "hosted":
        extra = {
            "workspace_backend": "hosted",
            "workspace_gateway_url": URL,
            "workspace_gateway_token": TOKEN,
        }
    return Settings(workspace_root=str(ws), **extra)


def _scope_dir(tmp_path: Path, backend: str) -> Path:
    key = thread_key(TENANT, THREAD)
    path = (
        tmp_path / "workspace" / SCOPES_DIR / TENANT / key
        if backend == "local"
        else tmp_path / "sandboxes" / TENANT / key
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def _populate(here: Path, outside: Path) -> None:
    (here / "src" / "deep").mkdir(parents=True)
    (here / "src" / "deep" / "x.py").write_text("x = 1\n")
    (here / "Readme.md").write_text("hi")
    (here / "a.txt").write_text("aa")
    (here / ".git").mkdir()
    (here / ".git" / "HEAD").write_text("ref")
    (here / "src" / ".git").mkdir()
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("not yours")
    os.symlink(outside, here / "escape")
    os.symlink(outside / "secret.txt", here / "src" / "secret.txt")


@pytest.mark.parametrize("backend", ["local", "hosted"])
async def test_both_backends_walk_the_same_tree(tmp_path: Path, gateway: FakeGateway, backend: str) -> None:
    settings = _settings(tmp_path, backend)
    _populate(_scope_dir(tmp_path, backend), tmp_path / "outside")
    tree = await get_workspace_backend(settings).tree(SCOPE, 100)
    assert tree.entries == [
        {"path": "a.txt", "type": "file", "bytes": 2},
        {"path": "Readme.md", "type": "file", "bytes": 2},
        {"path": "src", "type": "dir"},
        {"path": "src/deep", "type": "dir"},
        {"path": "src/deep/x.py", "type": "file", "bytes": 6},
    ]
    assert tree.truncated is False
    cut = await get_workspace_backend(settings).tree(SCOPE, 3)
    assert (cut.entries, cut.truncated) == (tree.entries[:3], True)


@pytest.mark.parametrize("backend", ["local", "hosted"])
async def test_both_backends_compare_before_they_write(
    tmp_path: Path, gateway: FakeGateway, backend: str
) -> None:
    settings = _settings(tmp_path, backend)
    here = _scope_dir(tmp_path, backend)
    backend_ = get_workspace_backend(settings)

    with pytest.raises(WorkspaceChanged) as missing:
        await backend_.write_file_checked(SCOPE, "notes/plan.md", b"x", "0" * 64)
    assert (missing.value.sha256, missing.value.bytes) == (None, None)
    assert not (here / "notes").exists()

    first = await backend_.write_file_checked(SCOPE, "notes/plan.md", b"one\n", None)
    assert (first.path, first.bytes, first.sha256) == (
        "notes/plan.md",
        4,
        hashlib.sha256(b"one\n").hexdigest(),
    )

    (here / "notes" / "plan.md").write_bytes(b"agent wrote this\n")
    with pytest.raises(WorkspaceChanged) as stale:
        await backend_.write_file_checked(SCOPE, "notes/plan.md", b"two\n", first.sha256)
    assert stale.value.sha256 == hashlib.sha256(b"agent wrote this\n").hexdigest()
    assert (here / "notes" / "plan.md").read_bytes() == b"agent wrote this\n"

    current = hashlib.sha256(b"agent wrote this\n").hexdigest()
    second = await backend_.write_file_checked(SCOPE, "notes/plan.md", b"two\n", current)
    assert second.sha256 == hashlib.sha256(b"two\n").hexdigest()
    assert (here / "notes" / "plan.md").read_bytes() == b"two\n"

    (here / "big.bin").write_bytes(b"b" * 512_001)
    with pytest.raises(WorkspaceChanged) as big:
        await backend_.write_file_checked(SCOPE, "big.bin", b"small", "0" * 64)
    assert (big.value.sha256, big.value.bytes) == (None, 512_001)


async def test_a_hosted_write_from_the_pane_is_checkpointed(tmp_path: Path, gateway: FakeGateway) -> None:
    from felix.context import AuthContext, RequestContext, async_run_with_context

    settings = _settings(tmp_path, "hosted")
    ctx = RequestContext(settings=settings, auth=AuthContext(tenant_id=TENANT), thread_id=THREAD)
    async with async_run_with_context(ctx):
        await get_workspace_backend(settings).write_file_checked(SCOPE, "a.md", b"saved", None)
    assert gateway.checkpoints == [f"{TENANT}/{thread_key(TENANT, THREAD)}"]


async def test_the_local_walk_stops_at_its_depth_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_local, "_MAX_SEARCH_DEPTH", 2)
    here = _scope_dir(tmp_path, "local")
    (here / "a" / "b" / "c" / "d").mkdir(parents=True)
    (here / "a" / "b" / "c" / "d" / "deep.txt").write_text("deep")
    tree = await get_workspace_backend(_settings(tmp_path, "local")).tree(SCOPE, 100)
    assert [e["path"] for e in tree.entries] == ["a", "a/b", "a/b/c"]
    assert tree.truncated is True


async def test_a_directory_past_one_batch_marks_the_tree_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_local, "_MAX_DIR_BATCH", 3)
    monkeypatch.setattr("felix.tools.workspace._MAX_DIR_BATCH", 3)
    here = _scope_dir(tmp_path, "local")
    for i in range(5):
        (here / f"f{i}").write_text("x")
    tree = await get_workspace_backend(_settings(tmp_path, "local")).tree(SCOPE, 100)
    assert len(tree.entries) == 3
    assert tree.truncated is True


# --- the catalog's `workspace` ------------------------------------------------------------------


def _manifest(**spec: Any) -> Any:
    from felix.manifests.loader import parse_manifest

    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "m"},
            "spec": {"pattern": "react", **spec},
        }
    )


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ({"tools": ["read_file"], "workspace": {"scope": "tenant"}}, {"tools": "server", "scope": "tenant"}),
        (
            {"shell_tools": [{"name": "run_tests", "commands": ["pytest"]}]},
            {"tools": "server", "scope": "thread"},
        ),
        ({"client_tools": [{"name": "local_write"}]}, {"tools": "client", "scope": "thread"}),
        (
            {"tools": ["edit_file"], "client_tools": [{"name": "local_read"}]},
            {"tools": "both", "scope": "thread"},
        ),
        (
            {"tools": ["calculator"], "client_tools": [{"name": "pick_color"}]},
            {"tools": "none", "scope": "thread"},
        ),
    ],
    ids=["server", "shell-is-server", "client", "both", "none"],
)
def test_the_catalog_names_where_an_agent_keeps_files(spec: dict[str, Any], expected: dict[str, str]) -> None:
    assert workspace_summary(_manifest(**spec)) == expected


def test_an_unresolved_manifest_lists_no_workspace() -> None:
    from felix.usage.catalog import catalog_from_manifest

    assert catalog_from_manifest("broken", None)["felix"]["workspace"] is None


# --- the SDK --------------------------------------------------------------------------------------


def _record(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    monkeypatch.setattr(httpx, "AsyncClient", _bind(httpx.MockTransport(handler)))
    return seen


async def test_the_sdk_asks_for_the_tree_and_a_file(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _record(monkeypatch)
    client = FelixClient(base_url="http://felix")
    client.set_thread("t1")

    await client.workspace_tree(limit=50)
    await client.workspace_read("notes/plan.md", manifest="cowork")

    tree, read = seen
    assert (tree.method, tree.url.path, dict(tree.url.params)) == (
        "GET",
        "/chat/workspace/tree",
        {"thread_id": "t1", "limit": "50"},
    )
    assert (read.url.path, dict(read.url.params)) == (
        "/chat/workspace/file",
        {"thread_id": "t1", "path": "notes/plan.md", "manifest": "cowork"},
    )


async def test_the_sdk_writes_with_its_hash_and_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _record(monkeypatch)
    await FelixClient(base_url="http://felix").workspace_write(
        "a.md", "text", expected_sha256="f" * 64, thread_id="t2", lease_token="tok"
    )
    (request,) = seen
    assert (request.method, request.url.path) == ("POST", "/chat/workspace/write")
    assert json.loads(request.content) == {
        "thread_id": "t2",
        "path": "a.md",
        "content": "text",
        "expected_sha256": "f" * 64,
    }
    assert request.headers["x-felix-lease-token"] == "tok"


async def test_the_sdk_needs_a_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    _record(monkeypatch)
    with pytest.raises(ValueError, match="thread_id required"):
        await FelixClient(base_url="http://felix").workspace_tree()
