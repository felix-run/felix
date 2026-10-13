"""Routes that act on a caller-named manifest without running a turn of it still admit the caller.

A turn of a manifest is admitted by `enforce_inbound_auth` -- its `auth.inbound` and a credential's
`felix_manifests` pin. The file pane, `/chat/style` and A2A's task methods reach a manifest's
workspace, its authored text and its answers without a turn, and each used to resolve the manifest
the caller named with no admission at all: a credential pinned to one agent could open the
deployment-scope workspace of another, or read the answer of a task it could not have started.

Over real HTTP with `api_key` auth, two principals in one tenant: `ops` (unpinned) and `kid`
(pinned to `quick`). Threads and tasks stay shared across the tenant; what is checked is only that
the caller is one the deciding manifest admits.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from felix.manifests.loader import parse_manifest

from tests.support.factories import app_client, make_settings

TENANT = "acme"
# Short, obviously-fake bearer values: anything key-shaped trips the full-history secret scan.
OPS = "k-ops"
KID = "k-kid"
NOSCOPE = "k-noscope"
KEYS = {
    OPS: {"tenant_id": TENANT, "sub": "ops", "scopes": ["ops:run"]},
    KID: {"tenant_id": TENANT, "sub": "kid", "scopes": ["ops:run"], "felix_manifests": ["quick"]},
    NOSCOPE: {"tenant_id": TENANT, "sub": "plain", "scopes": []},
}
GUARDED = "ops-deploy"


def _guarded() -> Any:
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": GUARDED},
            "spec": {
                "pattern": "react",
                "workspace": {"scope": "deployment"},
                "auth": {"inbound": {"allow_anonymous": False, "required_scopes": ["ops:run"]}},
                "output_styles": {"styles": [{"name": "terse", "prompt": "Be brief."}]},
            },
        }
    )


@pytest.fixture
async def settings(tmp_path: Path) -> Any:
    from felix.manifests.store import put_version

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "AGENTS.md").write_text("the operator's checkout")
    cfg = make_settings(
        auth_mode="api_key",
        auth_api_keys=json.dumps(KEYS),
        workspace_root=str(root),
        workspace_deployment_tenants=TENANT,
    )
    await put_version(cfg, TENANT, GUARDED, _guarded())
    return cfg


def _as(key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {key}"}


# --- the file pane -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "status", "detail"),
    [
        (KID, 403, f"this credential may not invoke manifest {GUARDED!r}"),
        (NOSCOPE, 403, "missing scopes: ops:run"),
    ],
    ids=["pinned-elsewhere", "missing-scope"],
)
async def test_the_pane_refuses_a_manifest_the_caller_may_not_invoke(
    settings: Any, key: str, status: int, detail: str
) -> None:
    async with app_client(settings) as client:
        tree = await client.get(
            "/chat/workspace/tree", params={"thread_id": "pane-1", "manifest": GUARDED}, headers=_as(key)
        )
        assert (tree.status_code, tree.json()) == (status, {"detail": detail})
        read = await client.get(
            "/chat/workspace/file",
            params={"thread_id": "pane-1", "manifest": GUARDED, "path": "AGENTS.md"},
            headers=_as(key),
        )
        assert read.status_code == status, read.text
        write = await client.post(
            "/chat/workspace/write",
            json={"thread_id": "pane-1", "manifest": GUARDED, "path": "planted.md", "content": "x"},
            headers=_as(key),
        )
        assert write.status_code == status, write.text
    assert not (Path(settings.workspace_root) / "planted.md").exists()


async def test_the_pane_serves_a_caller_the_manifest_admits(settings: Any) -> None:
    async with app_client(settings) as client:
        tree = await client.get(
            "/chat/workspace/tree", params={"thread_id": "pane-1", "manifest": GUARDED}, headers=_as(OPS)
        )
        assert tree.status_code == 200, tree.text
        assert (tree.json()["scope"], [e["path"] for e in tree.json()["entries"]]) == (
            "deployment",
            ["AGENTS.md"],
        )
        # The pinned credential still has its own agent's workspace.
        own = await client.get(
            "/chat/workspace/tree", params={"thread_id": "pane-2", "manifest": "quick"}, headers=_as(KID)
        )
        assert own.status_code == 200, own.text


async def test_the_pane_admits_the_caller_to_the_manifest_the_thread_ran_under(settings: Any) -> None:
    """Once a thread has run, its recorded manifest decides the scope; it is the one checked."""
    from felix.session.thread_state import LAST_MANIFEST_KEY, update_thread_meta

    await update_thread_meta(
        settings=settings, tenant_id=TENANT, thread_id=f"{TENANT}:ran", **{LAST_MANIFEST_KEY: GUARDED}
    )
    async with app_client(settings) as client:
        kid = await client.get(
            "/chat/workspace/tree", params={"thread_id": "ran", "manifest": "quick"}, headers=_as(KID)
        )
        assert kid.status_code == 403, kid.text
        ops = await client.get("/chat/workspace/tree", params={"thread_id": "ran"}, headers=_as(OPS))
        assert (ops.status_code, ops.json().get("scope")) == (200, "deployment"), ops.text


# --- output styles -----------------------------------------------------------------------------


async def test_a_style_cannot_be_set_from_a_manifest_the_caller_may_not_invoke(settings: Any) -> None:
    from felix.session.thread_state import get_thread_meta

    async with app_client(settings) as client:
        refused = await client.post(
            "/chat/style",
            json={"thread_id": "styled", "manifest": GUARDED, "style": "terse"},
            headers=_as(KID),
        )
        assert (refused.status_code, refused.json()) == (
            403,
            {"detail": f"this credential may not invoke manifest {GUARDED!r}"},
        )
        meta = await get_thread_meta(settings=settings, tenant_id=TENANT, thread_id=f"{TENANT}:styled")
        assert meta.get("output_style") is None, "a refused call set the style"
        # Refused before the style is looked up: probing names must not tell a 422 from a 403.
        probe = await client.post(
            "/chat/style",
            json={"thread_id": "styled", "manifest": GUARDED, "style": "no-such-style"},
            headers=_as(KID),
        )
        assert probe.status_code == 403, probe.text
        allowed = await client.post(
            "/chat/style",
            json={"thread_id": "styled", "manifest": GUARDED, "style": "terse"},
            headers=_as(OPS),
        )
        assert (allowed.status_code, allowed.json().get("style")) == (200, "terse"), allowed.text


# --- A2A tasks ---------------------------------------------------------------------------------


async def _rpc(client: Any, key: str, method: str, task_id: str) -> dict[str, Any]:
    resp = await client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {"id": task_id}},
        headers=_as(key),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.parametrize("method", ["tasks/get", "tasks/cancel", "tasks/resubscribe"])
async def test_a2a_task_methods_admit_the_caller_to_the_tasks_manifest(settings: Any, method: str) -> None:
    from felix.a2a import tasks as task_store

    answer = {"parts": [{"type": "text", "text": "the ops agent's answer"}]}
    await task_store.put_task(
        settings,
        TENANT,
        {"id": "t-ops", "status": {"state": "completed"}, "manifest": GUARDED, "artifacts": [answer]},
    )
    async with app_client(settings) as client:
        refused = await _rpc(client, KID, method, "t-ops")
        assert "result" not in refused
        assert refused["error"]["code"] == -32001
        assert refused["error"]["message"] == f"this credential may not invoke manifest {GUARDED!r}"
        stored = await task_store.get_task(settings, TENANT, "t-ops")
        assert stored is not None and stored["status"]["state"] == "completed", "a refused cancel ran"

        allowed = await _rpc(client, OPS, method, "t-ops")
        assert allowed["result"]["id"] == "t-ops", allowed
        assert allowed["result"]["artifacts"] == [answer]


async def test_a2a_tasks_of_a_manifest_that_no_longer_resolves_are_refused(settings: Any) -> None:
    from felix.a2a import tasks as task_store

    await task_store.put_task(
        settings,
        TENANT,
        {"id": "t-gone", "status": {"state": "completed"}, "manifest": "gone", "artifacts": []},
    )
    async with app_client(settings) as client:
        out = await _rpc(client, OPS, "tasks/get", "t-gone")
    assert out["error"] == {"code": -32001, "message": "the task's manifest no longer resolves"}, out


async def _send(client: Any, key: str, **params: Any) -> dict[str, Any]:
    resp = await client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": "hi", **params}},
        headers=_as(key),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.parametrize(
    ("params", "error"),
    [
        ({}, {"code": -32001, "message": f"this credential may not invoke manifest {GUARDED!r}"}),
        ({"manifest": "quick"}, {"code": -32602, "message": "taskId names a task of another manifest"}),
    ],
    ids=["same-manifest", "other-manifest"],
)
async def test_a2a_send_cannot_overwrite_a_task_its_manifest_refuses(
    settings: Any, params: dict[str, Any], error: dict[str, Any]
) -> None:
    """Continuing a task is admitted against the stored task first, and nothing is written until
    the caller is: writing first let a refused caller replace the answer, state and manifest."""
    from felix.a2a import tasks as task_store

    answer = {"parts": [{"type": "text", "text": "the ops agent's answer"}]}
    original = {"id": "t-ops", "status": {"state": "completed"}, "manifest": GUARDED, "artifacts": [answer]}
    await task_store.put_task(settings, TENANT, dict(original))
    async with app_client(settings) as client:
        out = await _send(client, KID, taskId="t-ops", **params)
    assert out == {"jsonrpc": "2.0", "id": 1, "error": error}
    stored = await task_store.get_task(settings, TENANT, "t-ops")
    assert stored is not None
    assert (stored["status"]["state"], stored["manifest"], stored["artifacts"]) == (
        "completed",
        GUARDED,
        [answer],
    )


async def test_a2a_send_refused_before_admission_writes_no_task(settings: Any) -> None:
    from felix.a2a import tasks as task_store

    async with app_client(settings) as client:
        out = await _send(client, KID, taskId="t-new", manifest=GUARDED)
    assert out["error"]["code"] == -32001, out
    assert await task_store.get_task(settings, TENANT, "t-new") is None
