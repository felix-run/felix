"""Repository checkouts under the hosted workspace backend (`WORKSPACE.md` phase 3c).

The clone runs in the thread's sandbox through the gateway's `clone` op, and everything that reads
the repository afterwards -- the workspace tools, the listing, `describe` -- goes to that sandbox,
with git run there by the helper's port of the harness's own `_git_exec`. The fake gateway
(`tests/support/workspace_gateway_fake.py`) clones from the `git_server` fixture, adding the token the way
the Worker's GitHub intercept does, so these tests see what the harness sends and what it keeps.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.repos import checkouts
from felix.tools import workspace_hosted
from felix.tools.builtins import default_tool_provider
from felix.tools.types import ToolInvocationCtx, tool_output_content
from felix.tools.workspace_scope import thread_key

from tests.support.git_server import _Server
from tests.support.workspace_gateway_fake import TOKEN as GATEWAY_TOKEN
from tests.support.workspace_gateway_fake import URL, FakeGateway

TOKEN = "ghu_person_token_0123456789"
REPO = {"full_name": "acme/widgets", "default_branch": "main", "size": 12, "private": True}
TENANT, THREAD = "acme", "acme:t1"


@pytest.fixture
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_server: _Server) -> FakeGateway:
    fake = FakeGateway(root=tmp_path / "sandboxes", clone_base=git_server.base)
    monkeypatch.setattr(workspace_hosted, "gateway_client", lambda settings: fake.client())
    return fake


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    shared = tmp_path / "workspace"
    shared.mkdir()
    return Settings(
        database_url="memory://hosted-checkouts",
        data_dir=str(tmp_path / "data"),
        workspace_root=str(shared),
        workspace_backend="hosted",
        workspace_gateway_url=URL,
        workspace_gateway_token=GATEWAY_TOKEN,
    )


def _sandbox(gateway: FakeGateway, thread: str = THREAD) -> Path:
    return gateway.sandbox(f"{TENANT}/{thread_key(TENANT, thread)}")


async def _open(settings: Settings, repo: dict[str, Any] | None = None) -> dict[str, Any]:
    await checkouts.open_checkout(
        settings, TENANT, THREAD, repo=repo or REPO, github_user_id=42, opened_by="github:42", token=TOKEN
    )
    await checkouts.wait_for_clones()
    state = checkouts.read_checkout(settings, TENANT, THREAD)
    assert state is not None
    return state


def _ctx(settings: Settings, thread: str | None = THREAD) -> RequestContext:
    return RequestContext(settings=settings, auth=AuthContext(tenant_id=TENANT), thread_id=thread)


async def _call(settings: Settings, tool: str, args: dict[str, Any]) -> str:
    async with async_run_with_context(_ctx(settings)):
        out = (
            await default_tool_provider()
            .get(tool)
            .executor.execute(args, ToolInvocationCtx(thread_id=THREAD))
        )
    return tool_output_content(out)


async def test_the_clone_happens_in_the_sandbox_and_the_token_is_kept_nowhere(
    settings: Settings, gateway: FakeGateway
) -> None:
    state = await _open(settings)
    assert state["state"] == "ready" and state["hosted"] is True, state
    sandbox = _sandbox(gateway)
    assert (sandbox / "app.py").read_text() == "print(1)\n"
    assert state["head"] and len(state["head"]) == 40
    # The token went to the gateway for the clone, and the clone was backed up straight after.
    assert gateway.clone_tokens == [TOKEN]
    assert ("acme/" + thread_key(TENANT, THREAD), "checkpoint") in gateway.calls
    # Nothing of the repository is on the host, and the token is in neither place.
    directory = checkouts.thread_dir(settings, TENANT, THREAD)
    assert not (directory / "repo").exists()
    basic = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
    for root in (directory, sandbox):
        for path in root.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                assert TOKEN.encode() not in data and basic.encode() not in data, path


async def test_the_tools_and_the_publish_root_reach_the_repository_in_the_sandbox(
    settings: Settings, gateway: FakeGateway
) -> None:
    await _open(settings)
    assert "print(1)" in await _call(settings, "read_file", {"path": "app.py"})
    await _call(settings, "write_file", {"path": "notes.txt", "content": "n\n"})
    assert (_sandbox(gateway) / "notes.txt").read_text() == "n\n"
    async with async_run_with_context(_ctx(settings)):
        root = workspace_hosted.hosted_checkout_root()
    assert root == workspace_hosted.HostedRoot(settings, f"{TENANT}/{thread_key(TENANT, THREAD)}")
    # Another thread has no checkout, so no repository to publish from.
    async with async_run_with_context(_ctx(settings, "acme:t2")):
        assert workspace_hosted.hosted_checkout_root() is None


async def test_describe_and_the_listing_read_git_in_the_sandbox(
    settings: Settings, gateway: FakeGateway
) -> None:
    await _open(settings)
    sandbox = _sandbox(gateway)
    (sandbox / "app.py").write_text("print(2)\n# more\n")
    (sandbox / "README.md").unlink()
    (sandbox / "src").mkdir()
    (sandbox / "src" / "new.py").write_text("x = 1\n")
    described = await checkouts.describe(settings, TENANT, THREAD)
    assert described is not None
    assert (described["branch"], described["ahead"], described["dirty"]) == ("main", 0, True)
    listed = await checkouts.list_files(settings, TENANT, THREAD)
    assert listed is not None and listed["state"] == "ready"
    rows = {f["path"]: f for f in listed["files"]}
    assert rows["app.py"] == {"path": "app.py", "kind": "file", "size": 16, "status": "modified"}
    assert rows["README.md"]["kind"] == "missing" and rows["README.md"]["status"] == "deleted"
    assert rows["src/new.py"]["status"] == "untracked"
    assert not any(p.startswith(".git/") for p in rows)


async def test_a_workspace_that_already_has_files_is_not_cloned_over(
    settings: Settings, gateway: FakeGateway
) -> None:
    await _call(settings, "write_file", {"path": "draft.md", "content": "mine\n"})
    state = await _open(settings)
    assert state["state"] == "failed"
    assert "already has files" in state["error"]
    assert (_sandbox(gateway) / "draft.md").read_text() == "mine\n"
    assert not (checkouts.thread_dir(settings, TENANT, THREAD) / checkouts.LOCK_FILE).exists()


async def test_a_branch_that_does_not_exist_fails_and_leaves_the_sandbox_empty(
    settings: Settings, gateway: FakeGateway
) -> None:
    state = await _open(settings, repo={**REPO, "default_branch": "nope"})
    assert state["state"] == "failed"
    assert state["error"] == "that branch does not exist"
    assert TOKEN not in state["error"]
    assert list(_sandbox(gateway).iterdir()) == []


async def test_removing_a_hosted_checkout_destroys_its_sandbox(
    settings: Settings, gateway: FakeGateway
) -> None:
    await _open(settings)
    assert await checkouts.remove_checkout(settings, TENANT, THREAD) is True
    assert checkouts.read_checkout(settings, TENANT, THREAD) is None
    assert not _sandbox(gateway).exists()
    # A new repository can be opened in the emptied workspace.
    assert (await _open(settings))["state"] == "ready"


async def test_a_removal_the_gateway_cannot_carry_out_keeps_the_checkout(
    settings: Settings, gateway: FakeGateway
) -> None:
    await _open(settings)
    gateway.down = True
    with pytest.raises(checkouts.CheckoutRefused) as refused:
        await checkouts.remove_checkout(settings, TENANT, THREAD)
    assert refused.value.code == "workspace_unavailable"
    state = checkouts.read_checkout(settings, TENANT, THREAD)
    assert state is not None and state["state"] == "ready"


async def test_a_long_listing_is_sized_in_batches_the_gateway_takes(
    settings: Settings, gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _open(settings)
    sandbox = _sandbox(gateway)
    names = [f"{'d' * 60}/{i:02}.txt" for i in range(7)]
    for name in names:
        (sandbox / name).parent.mkdir(exist_ok=True)
        (sandbox / name).write_text("x" * len(name))
    monkeypatch.setattr(workspace_hosted, "_LSTAT_BATCH_PATHS", 3)
    monkeypatch.setattr(workspace_hosted, "_LSTAT_BATCH_BYTES", 150)  # two of these paths
    gateway.calls.clear()
    root = workspace_hosted.HostedRoot(settings, f"{TENANT}/{thread_key(TENANT, THREAD)}")
    stats = await root.lstat([*names, "gone"])
    assert stats == [*({"kind": "file", "size": len(n)} for n in names), None]
    assert [op for _, op in gateway.calls] == ["lstat"] * 4


async def test_an_unused_hosted_checkout_expires_with_its_sandbox(
    settings: Settings, gateway: FakeGateway
) -> None:
    import os
    import time

    state = await _open(settings)
    assert state["sandbox"] == f"{TENANT}/{thread_key(TENANT, THREAD)}"
    directory = checkouts.thread_dir(settings, TENANT, THREAD)
    old = time.time() - (settings.repo_checkout_ttl_days + 1) * 86_400
    os.utime(directory / checkouts.USED_FILE, (old, old))

    gateway.down = True  # not cleared now: left for the next sweep, not marked expired
    assert await checkouts.sweep_expired(settings) == 0
    assert checkouts.read_checkout(settings, TENANT, THREAD)["state"] == "ready"  # type: ignore[index]

    gateway.down = False
    assert await checkouts.sweep_expired(settings) == 1
    assert checkouts.read_checkout(settings, TENANT, THREAD)["state"] == "expired"  # type: ignore[index]
    assert not _sandbox(gateway).exists()
    # Opened again, it clones into the emptied workspace.
    assert (await _open(settings))["state"] == "ready"
