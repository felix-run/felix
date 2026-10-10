"""The operator's file pane over HTTP: `GET /chat/workspace/tree`, `GET /chat/workspace/file` and
`POST /chat/workspace/write`, against the workspace the thread's agent actually works in.

What is pinned is the promise a client builds on: the pane reads from and writes back to the same
directory the agent's next turn uses -- decided by the manifest the thread ran under, overridden by
its checkout -- that nothing is followed out of it, and that a save over a file the agent changed
since it was read is refused rather than lost. The backends' own walk and compare are held to each
other in `tests/unit/test_workspace_pane.py`.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn

TENANT = "default"


def _scope_dir(root: Path, suffix: str, scope: str = "thread") -> Path:
    from felix.config import get_settings
    from felix.tools.workspace_scope import scope_relpath

    rel = scope_relpath(get_settings(), TENANT, f"{TENANT}:{suffix}", scope)  # type: ignore[arg-type]
    path = root.joinpath(*rel.split("/")) if rel else root
    path.mkdir(parents=True, exist_ok=True)
    return path


def _manifest(name: str, scope: str, **spec: Any) -> Any:
    body = {
        "pattern": "react",
        "workspace": {"scope": scope},
        "auth": {"inbound": {"allow_anonymous": True}},
        **spec,
    }
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": name}, "spec": body}
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


def _env(root: Path, **extra: str) -> dict[str, str]:
    return {"FELIX_WORKSPACE_ROOT": str(root), **extra}


async def _run(app: Any, thread: str, manifest: str = "quick") -> None:
    app.push(ScriptedTurn(content="ok"))
    resp = await app.client.post(
        "/chat",
        json={"manifest": manifest, "thread_id": thread, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200, resp.text


# --- the tree ---------------------------------------------------------------------------------


async def test_a_workspace_nothing_has_written_lists_empty(boot: Any, root: Path) -> None:
    async with boot([], env=_env(root)) as app:
        resp = await app.client.get("/chat/workspace/tree", params={"thread_id": "pane-fresh"})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "root_kind": "scoped",
            "scope": "thread",
            "manifest": "quick",
            "entries": [],
            "truncated": False,
        }


async def test_the_tree_is_recursive_sorted_and_leaves_out_git_and_links(
    boot: Any, root: Path, tmp_path: Path
) -> None:
    thread = "pane-tree"
    async with boot([], env=_env(root)) as app:
        here = _scope_dir(root, thread)
        (here / "src" / "deep").mkdir(parents=True)
        (here / "src" / "deep" / "x.py").write_text("x = 1\n")
        (here / "README.md").write_text("hi")
        (here / "b.txt").write_text("bb")
        (here / ".git").mkdir()
        (here / ".git" / "config").write_text("[core]")
        (here / ".felix-edit-0123456789abcdef").write_text("half a write")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("not yours")
        os.symlink(outside, here / "escape")
        os.symlink(outside / "secret.txt", here / "secret-link.txt")

        resp = await app.client.get("/chat/workspace/tree", params={"thread_id": thread})
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out["entries"] == [
            {"path": "b.txt", "type": "file", "bytes": 2},
            {"path": "README.md", "type": "file", "bytes": 2},
            {"path": "src", "type": "dir"},
            {"path": "src/deep", "type": "dir"},
            {"path": "src/deep/x.py", "type": "file", "bytes": 6},
        ]
        assert out["truncated"] is False


async def test_the_limit_cuts_the_tree_and_says_so(boot: Any, root: Path) -> None:
    thread = "pane-limit"
    async with boot([], env=_env(root)) as app:
        here = _scope_dir(root, thread)
        for i in range(6):
            (here / f"f{i}.txt").write_text(str(i))
        cut = (await app.client.get("/chat/workspace/tree", params={"thread_id": thread, "limit": 4})).json()
        assert [e["path"] for e in cut["entries"]] == ["f0.txt", "f1.txt", "f2.txt", "f3.txt"]
        assert cut["truncated"] is True
        whole = (
            await app.client.get("/chat/workspace/tree", params={"thread_id": thread, "limit": 6})
        ).json()
        assert len(whole["entries"]) == 6
        assert whole["truncated"] is False
        for bad in (0, 5_001):
            resp = await app.client.get("/chat/workspace/tree", params={"thread_id": thread, "limit": bad})
            assert resp.status_code == 422, resp.text


async def test_an_unconfigured_workspace_is_a_503_without_host_paths(boot: Any) -> None:
    async with boot([], env={"FELIX_WORKSPACE_ROOT": ""}) as app:
        resp = await app.client.get("/chat/workspace/tree", params={"thread_id": "pane-none"})
        assert resp.status_code == 503, resp.text
        assert resp.json() == {"detail": "workspace_not_configured"}


async def test_a_malformed_thread_id_is_refused(boot: Any, root: Path) -> None:
    async with boot([], env=_env(root)) as app:
        resp = await app.client.get("/chat/workspace/tree", params={"thread_id": "other:thread"})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "invalid_thread_id"


# --- which workspace ----------------------------------------------------------------------------


async def test_the_manifest_the_thread_ran_under_decides_the_scope(boot: Any, root: Path) -> None:
    """A thread that ran under a `tenant`-scope agent is shown the tenant's shared directory, and a
    `manifest` the caller names cannot move it anywhere else once the thread has run."""
    thread = "pane-ran"
    manifests = {
        "e2e-shared": _manifest("e2e-shared", "tenant"),
        "e2e-deploy": _manifest("e2e-deploy", "deployment"),
    }
    async with boot([], env=_env(root), manifests=manifests) as app:
        await _run(app, thread, "e2e-shared")
        (_scope_dir(root, thread, "tenant") / "shared.md").write_text("team notes")
        (_scope_dir(root, thread, "thread") / "mine.md").write_text("only this thread")

        for params in ({}, {"manifest": "e2e-deploy"}, {"manifest": "no-such-agent"}):
            out = (
                await app.client.get("/chat/workspace/tree", params={"thread_id": thread, **params})
            ).json()
            assert (out["root_kind"], out["scope"], out["manifest"]) == ("scoped", "tenant", "e2e-shared"), (
                params
            )
            assert [e["path"] for e in out["entries"]] == ["shared.md"], params


async def test_a_thread_that_never_ran_uses_the_manifest_named(boot: Any, root: Path) -> None:
    """At `deployment` scope the root itself is listed, minus the scopes directory, whose contents
    are every other scope's files."""
    manifests = {"e2e-deploy": _manifest("e2e-deploy", "deployment")}
    async with boot([], env=_env(root), manifests=manifests) as app:
        (root / "AGENTS.md").write_text("rules")
        _scope_dir(root, "someone-else")
        (_scope_dir(root, "someone-else") / "private.md").write_text("theirs")

        resp = await app.client.get(
            "/chat/workspace/tree", params={"thread_id": "pane-new", "manifest": "e2e-deploy"}
        )
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert (out["scope"], out["manifest"]) == ("deployment", "e2e-deploy")
        assert [e["path"] for e in out["entries"]] == ["AGENTS.md"]

        unknown = await app.client.get(
            "/chat/workspace/tree", params={"thread_id": "pane-new", "manifest": "no-such-agent"}
        )
        assert unknown.status_code == 404
        assert unknown.json() == {"detail": "unknown_manifest"}


async def test_a_thread_with_a_checkout_is_shown_the_checkout(boot: Any, root: Path, tmp_path: Path) -> None:
    from felix.config import get_settings
    from felix.repos.checkouts import REPO_DIR, STATE_FILE, thread_dir

    thread = "pane-repo"
    checkouts = tmp_path / "checkouts"
    async with boot([], env=_env(root, FELIX_REPO_CHECKOUT_ROOT=str(checkouts))) as app:
        directory = thread_dir(get_settings(), TENANT, f"{TENANT}:{thread}")
        (directory / REPO_DIR / "pkg").mkdir(parents=True)
        (directory / REPO_DIR / "pkg" / "mod.py").write_text("print('repo')\n")
        (directory / STATE_FILE).write_text(json.dumps({"state": "ready", "repo": "acme/widgets"}))
        (_scope_dir(root, thread) / "scoped.md").write_text("not the repo")

        out = (await app.client.get("/chat/workspace/tree", params={"thread_id": thread})).json()
        assert (out["root_kind"], out["scope"]) == ("checkout", None)
        assert [e["path"] for e in out["entries"]] == ["pkg", "pkg/mod.py"]

        read = (
            await app.client.get("/chat/workspace/file", params={"thread_id": thread, "path": "pkg/mod.py"})
        ).json()
        assert read["content"] == "print('repo')\n"

        resp = await app.client.post(
            "/chat/workspace/write", json={"thread_id": thread, "path": "pkg/new.py", "content": "y = 2\n"}
        )
        assert resp.status_code == 200, resp.text
        assert (directory / REPO_DIR / "pkg" / "new.py").read_text() == "y = 2\n"
        assert not (_scope_dir(root, thread) / "pkg").exists()

        (directory / STATE_FILE).write_text(json.dumps({"state": "cloning", "repo": "acme/widgets"}))
        cloning = await app.client.get("/chat/workspace/tree", params={"thread_id": thread})
        assert cloning.status_code == 409
        assert cloning.json() == {"detail": "workspace_unavailable"}


# --- reading ------------------------------------------------------------------------------------


async def test_a_text_file_reads_as_utf8_and_a_binary_one_as_base64(boot: Any, root: Path) -> None:
    import base64

    thread = "pane-read"
    async with boot([], env=_env(root)) as app:
        here = _scope_dir(root, thread)
        (here / "notes").mkdir()
        (here / "notes" / "plan.md").write_bytes("café\r\nnext\n".encode())
        (here / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\x00")

        text = await app.client.get(
            "/chat/workspace/file", params={"thread_id": thread, "path": "notes/plan.md"}
        )
        assert text.status_code == 200, text.text
        raw = "café\r\nnext\n".encode()
        assert text.json() == {
            "path": "notes/plan.md",
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "encoding": "utf-8",
            "content": "café\r\nnext\n",
        }

        binary = (
            await app.client.get("/chat/workspace/file", params={"thread_id": thread, "path": "logo.png"})
        ).json()
        assert binary["encoding"] == "base64"
        assert base64.b64decode(binary["content"]) == b"\x89PNG\r\n\x1a\n\xff\x00"
        assert binary["sha256"] == hashlib.sha256(b"\x89PNG\r\n\x1a\n\xff\x00").hexdigest()


@pytest.mark.parametrize(
    ("path", "status", "detail"),
    [
        ("missing.md", 404, "not_found"),
        ("nowhere/missing.md", 404, "not_found"),
        ("big.txt", 413, "too_large"),
        ("docs", 400, "not_a_file"),
        (".", 400, "not_a_file"),
        ("../escape.md", 400, "invalid_path"),
        ("/etc/passwd", 400, "invalid_path"),
        ("link.md", 400, "invalid_path"),
        (".git/config", 400, "reserved_path"),
        ("sub/.git/HEAD", 400, "reserved_path"),
    ],
)
async def test_a_read_that_cannot_be_served_says_why(
    boot: Any, root: Path, tmp_path: Path, path: str, status: int, detail: str
) -> None:
    thread = "pane-refused"
    async with boot([], env=_env(root)) as app:
        here = _scope_dir(root, thread)
        (here / "docs").mkdir()
        (here / "big.txt").write_bytes(b"x" * 512_001)
        (tmp_path / "outside.md").write_text("not yours")
        os.symlink(tmp_path / "outside.md", here / "link.md")
        (here / ".git").mkdir()
        (here / ".git" / "config").write_text("[core]")

        resp = await app.client.get("/chat/workspace/file", params={"thread_id": thread, "path": path})
        assert resp.status_code == status, resp.text
        assert resp.json() == {"detail": detail}


async def test_a_file_at_the_read_cap_is_served(boot: Any, root: Path) -> None:
    thread = "pane-cap"
    async with boot([], env=_env(root)) as app:
        (_scope_dir(root, thread) / "edge.txt").write_bytes(b"y" * 512_000)
        resp = await app.client.get("/chat/workspace/file", params={"thread_id": thread, "path": "edge.txt"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["bytes"] == 512_000


# --- writing ------------------------------------------------------------------------------------


async def test_a_write_lands_in_the_workspace_and_the_next_turn_is_told(boot: Any, root: Path) -> None:
    from felix.flush import flush_all

    thread = "pane-write"
    async with boot([], env=_env(root)) as app:
        await _run(app, thread)
        body = "# Plan\n\nstep one\n"
        resp = await app.client.post(
            "/chat/workspace/write", json={"thread_id": thread, "path": "notes/./plan.md", "content": body}
        )
        assert resp.status_code == 200, resp.text
        out = resp.json()
        digest = hashlib.sha256(body.encode()).hexdigest()
        assert {k: out[k] for k in ("status", "path", "bytes", "sha256")} == {
            "status": "recorded",
            "path": "notes/plan.md",
            "bytes": len(body.encode()),
            "sha256": digest,
        }
        assert out["event_id"]
        assert (_scope_dir(root, thread) / "notes" / "plan.md").read_text() == body

        read = (
            await app.client.get(
                "/chat/workspace/file", params={"thread_id": thread, "path": "notes/plan.md"}
            )
        ).json()
        assert read["sha256"] == digest

        snap = (await app.client.get(f"/chat/sessions/{thread}")).json()
        entry = next(e for e in snap["transcript"] if e["id"] == out["event_id"])
        assert entry["metadata"]["type"] == "workspace_edit"
        assert (entry["metadata"]["path"], entry["metadata"]["op"], entry["metadata"]["bytes"]) == (
            "notes/plan.md",
            "write",
            len(body.encode()),
        )

        await flush_all(app.settings)
        audit = await app.client.get("/audit", params={"event_type": "workspace_write"})
        assert audit.status_code == 200, audit.text
        assert [(r["payload_json"], r["manifest_id"], r["status"]) for r in audit.json()["items"]] == [
            (
                {
                    "thread_id": f"{TENANT}:{thread}",
                    "path": "notes/plan.md",
                    "bytes": len(body.encode()),
                    "manifest": "quick",
                },
                "quick",
                "ok",
            )
        ]


async def test_a_write_during_a_run_is_queued_for_its_next_model_call(boot: Any, root: Path) -> None:
    from felix.workspace_notes import drain, mark_run_active, mark_run_idle

    thread = "pane-live"
    scoped = f"{TENANT}:{thread}"
    async with boot([], env=_env(root)) as app:
        await mark_run_active(TENANT, scoped)
        try:
            resp = await app.client.post(
                "/chat/workspace/write", json={"thread_id": thread, "path": "a.md", "content": "live"}
            )
            assert resp.status_code == 200, resp.text
            assert (resp.json()["status"], resp.json()["event_id"]) == ("queued", None)
            notes = await drain(TENANT, scoped)
            assert [(n.path, n.op, n.bytes) for n in notes] == [("a.md", "write", 4)]
        finally:
            await mark_run_idle(TENANT, scoped)


async def test_a_stale_hash_refuses_the_write_and_leaves_the_file(boot: Any, root: Path) -> None:
    thread = "pane-stale"
    async with boot([], env=_env(root)) as app:
        target = _scope_dir(root, thread) / "plan.md"
        target.write_text("what the operator read")
        read = (
            await app.client.get("/chat/workspace/file", params={"thread_id": thread, "path": "plan.md"})
        ).json()
        target.write_text("what the agent wrote since")

        resp = await app.client.post(
            "/chat/workspace/write",
            json={
                "thread_id": thread,
                "path": "plan.md",
                "content": "operator's edit",
                "expected_sha256": read["sha256"],
            },
        )
        assert resp.status_code == 409, resp.text
        now = b"what the agent wrote since"
        assert resp.json() == {
            "detail": "workspace_changed",
            "sha256": hashlib.sha256(now).hexdigest(),
            "bytes": len(now),
        }
        assert target.read_bytes() == now
        snap = (await app.client.get(f"/chat/sessions/{thread}")).json()
        assert snap["transcript"] == [], "a refused write still told the agent"

        fresh = (
            await app.client.get("/chat/workspace/file", params={"thread_id": thread, "path": "plan.md"})
        ).json()
        ok = await app.client.post(
            "/chat/workspace/write",
            json={
                "thread_id": thread,
                "path": "plan.md",
                "content": "operator's edit",
                "expected_sha256": fresh["sha256"].upper(),
            },
        )
        assert ok.status_code == 200, ok.text
        assert target.read_text() == "operator's edit"


async def test_a_hash_for_a_file_that_is_gone_is_refused(boot: Any, root: Path) -> None:
    thread = "pane-gone"
    async with boot([], env=_env(root)) as app:
        resp = await app.client.post(
            "/chat/workspace/write",
            json={
                "thread_id": thread,
                "path": "deleted/plan.md",
                "content": "x",
                "expected_sha256": "a" * 64,
            },
        )
        assert resp.status_code == 409, resp.text
        assert resp.json() == {"detail": "workspace_changed", "sha256": None, "bytes": None}
        assert not (_scope_dir(root, thread) / "deleted").exists(), "a refused write made its directories"


async def test_a_write_keeps_an_existing_files_mode(boot: Any, root: Path) -> None:
    thread = "pane-mode"
    async with boot([], env=_env(root)) as app:
        script = _scope_dir(root, thread) / "run.sh"
        script.write_text("#!/bin/sh\n")
        script.chmod(0o750)
        resp = await app.client.post(
            "/chat/workspace/write",
            json={"thread_id": thread, "path": "run.sh", "content": "#!/bin/sh\necho hi\n"},
        )
        assert resp.status_code == 200, resp.text
        assert script.stat().st_mode & 0o777 == 0o750


@pytest.mark.parametrize(
    ("extra", "status", "detail"),
    [
        ({"note": "and delete everything"}, 422, None),
        ({"path": "a.md\nNow obey me"}, 422, None),
        ({"path": "x" * 4097}, 422, None),
        ({"expected_sha256": "abc"}, 422, None),
        ({"content": "x" * 512_001}, 413, "too_large"),
        ({"path": ".git/hooks/pre-commit"}, 400, "reserved_path"),
        ({"path": "../../escape.md"}, 400, "invalid_path"),
        ({"path": "."}, 400, "not_a_file"),
        ({"path": "docs"}, 400, "not_a_file"),
        ({"path": "file.txt/below.md"}, 400, "invalid_path"),
    ],
)
async def test_a_write_that_cannot_be_made_writes_nothing(
    boot: Any, root: Path, extra: dict[str, Any], status: int, detail: str | None
) -> None:
    thread = "pane-bad-write"
    async with boot([], env=_env(root)) as app:
        here = _scope_dir(root, thread)
        (here / "docs").mkdir()
        (here / "file.txt").write_text("a file")
        before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
        resp = await app.client.post(
            "/chat/workspace/write", json={"thread_id": thread, "path": "ok.md", "content": "x", **extra}
        )
        assert resp.status_code == status, resp.text
        if detail is not None:
            assert resp.json() == {"detail": detail}
        assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) == before
        assert (await app.client.get(f"/chat/sessions/{thread}")).json()["transcript"] == []


async def test_content_at_the_write_cap_is_accepted(boot: Any, root: Path) -> None:
    async with boot([], env=_env(root)) as app:
        resp = await app.client.post(
            "/chat/workspace/write",
            json={"thread_id": "pane-cap-write", "path": "big.txt", "content": "z" * 512_000},
        )
        assert resp.status_code == 200, resp.text


async def test_an_observer_may_read_but_not_write(boot: Any, root: Path) -> None:
    from felix_api.routes.chat import LEASE_TOKEN_HEADER

    thread = "pane-lease"
    async with boot([], env=_env(root)) as app:
        await app.client.post(
            "/chat/sessions/lease", json={"thread_id": thread, "holder_id": "tab-a", "mode": "exclusive"}
        )
        observer = await app.client.post(
            "/chat/sessions/lease", json={"thread_id": thread, "holder_id": "tab-b", "mode": "shared"}
        )
        headers = {LEASE_TOKEN_HEADER: observer.json()["token"]}
        (_scope_dir(root, thread) / "a.md").write_text("as it was")

        refused = await app.client.post(
            "/chat/workspace/write",
            json={"thread_id": thread, "path": "a.md", "content": "taken"},
            headers=headers,
        )
        assert refused.status_code == 409
        assert refused.json()["detail"] == "lease_read_only"
        assert (_scope_dir(root, thread) / "a.md").read_text() == "as it was"

        read = await app.client.get(
            "/chat/workspace/file", params={"thread_id": thread, "path": "a.md"}, headers=headers
        )
        assert read.status_code == 200, read.text


# --- the catalog --------------------------------------------------------------------------------


async def test_the_catalog_says_where_each_agent_keeps_files(boot: Any, root: Path) -> None:
    local = [{"name": "local_read"}]
    manifests = {
        "e2e-ws-server": _manifest("e2e-ws-server", "tenant", tools=["read_file", "write_file"]),
        "e2e-ws-client": _manifest("e2e-ws-client", "thread", client_tools=local),
        "e2e-ws-both": _manifest("e2e-ws-both", "thread", tools=["list_dir"], client_tools=local),
        "e2e-ws-none": _manifest("e2e-ws-none", "thread", tools=["calculator"]),
    }
    async with boot([], env=_env(root), manifests=manifests) as app:
        listed = {
            m["id"]: m["felix"]["workspace"] for m in (await app.client.get("/v1/models")).json()["data"]
        }
        assert listed["e2e-ws-server"] == {"tools": "server", "scope": "tenant"}
        assert listed["e2e-ws-client"] == {"tools": "client", "scope": "thread"}
        assert listed["e2e-ws-both"] == {"tools": "both", "scope": "thread"}
        assert listed["e2e-ws-none"] == {"tools": "none", "scope": "thread"}
        assert listed["cowork"]["tools"] == "client"


@pytest.mark.parametrize(
    ("path", "status", "detail"),
    [
        ("x" * 256, 400, "invalid_path"),
        ("escape/secret.txt", 400, "invalid_path"),
        ("escape", 400, "invalid_path"),
        ("..\\..\\secret.txt", 404, "not_found"),
    ],
    ids=["segment-too-long", "through-a-root-symlink", "the-root-symlink", "backslashes-are-names"],
)
async def test_the_http_entry_cannot_reach_past_the_walk(
    boot: Any, root: Path, tmp_path: Path, path: str, status: int, detail: str
) -> None:
    """What the route passes in is what a tool call could: a name past NAME_MAX is a bad path
    (it was a 500), a symlink at the scope's root is refused whether named or walked through, and
    a backslash is part of a name, never a separator."""
    thread = "pane-walk"
    async with boot([], env=_env(root)) as app:
        here = _scope_dir(root, thread)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("not yours")
        os.symlink(outside, here / "escape")

        read = await app.client.get("/chat/workspace/file", params={"thread_id": thread, "path": path})
        assert (read.status_code, read.json()) == (status, {"detail": detail}), read.text

        write = await app.client.post(
            "/chat/workspace/write", json={"thread_id": thread, "path": path, "content": "x"}
        )
        if path.startswith(".."):
            assert write.status_code == 200, write.text
            assert (here / path).read_text() == "x"
        else:
            assert (write.status_code, write.json()) == (400, {"detail": "invalid_path"}), write.text
        assert (outside / "secret.txt").read_text() == "not yours"
        assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]


# --- deleting and renaming ----------------------------------------------------------------------


async def _note(app: Any, thread: str, event_id: str) -> dict[str, Any]:
    snap = (await app.client.get(f"/chat/sessions/{thread}")).json()
    return next(e for e in snap["transcript"] if e["id"] == event_id)


async def _audit(app: Any, event_type: str) -> list[tuple[Any, str, str]]:
    from felix.flush import flush_all

    await flush_all(app.settings)
    resp = await app.client.get("/audit", params={"event_type": event_type})
    assert resp.status_code == 200, resp.text
    return [(r["payload_json"], r["manifest_id"], r["status"]) for r in resp.json()["items"]]


async def test_a_delete_removes_the_file_and_the_next_turn_is_told(boot: Any, root: Path) -> None:
    thread = "pane-delete"
    async with boot([], env=_env(root)) as app:
        await _run(app, thread)
        target = _scope_dir(root, thread) / "notes" / "plan.md"
        target.parent.mkdir()
        target.write_text("old plan")
        resp = await app.client.post(
            "/chat/workspace/delete",
            json={
                "thread_id": thread,
                "path": "notes/./plan.md",
                "expected_sha256": hashlib.sha256(b"old plan").hexdigest(),
            },
        )
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert (out["status"], out["path"]) == ("recorded", "notes/plan.md")
        assert not target.exists()
        assert target.parent.is_dir()

        entry = await _note(app, thread, out["event_id"])
        assert (entry["metadata"]["type"], entry["metadata"]["op"], entry["metadata"]["path"]) == (
            "workspace_edit",
            "delete",
            "notes/plan.md",
        )
        assert entry["content"].startswith("The operator deleted `notes/plan.md` from the workspace.")
        assert await _audit(app, "workspace_delete") == [
            ({"thread_id": f"{TENANT}:{thread}", "path": "notes/plan.md", "manifest": "quick"}, "quick", "ok")
        ]


async def test_a_rename_moves_the_file_into_a_new_directory_and_the_next_turn_is_told(
    boot: Any, root: Path
) -> None:
    thread = "pane-rename"
    async with boot([], env=_env(root)) as app:
        await _run(app, thread)
        here = _scope_dir(root, thread)
        (here / "draft.md").write_text("draft")
        digest = hashlib.sha256(b"draft").hexdigest()
        resp = await app.client.post(
            "/chat/workspace/rename",
            json={
                "thread_id": thread,
                "path": "draft.md",
                "to_path": "docs/final/plan.md",
                "expected_sha256": digest.upper(),
            },
        )
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert {k: out[k] for k in ("status", "path", "to_path", "sha256")} == {
            "status": "recorded",
            "path": "draft.md",
            "to_path": "docs/final/plan.md",
            "sha256": digest,
        }
        assert not (here / "draft.md").exists()
        assert (here / "docs" / "final" / "plan.md").read_text() == "draft"

        read = (
            await app.client.get(
                "/chat/workspace/file", params={"thread_id": thread, "path": "docs/final/plan.md"}
            )
        ).json()
        assert read["sha256"] == out["sha256"]

        entry = await _note(app, thread, out["event_id"])
        md = entry["metadata"]
        assert (md["op"], md["path"], md["to_path"]) == ("rename", "draft.md", "docs/final/plan.md")
        assert entry["content"].startswith(
            "The operator renamed `draft.md` to `docs/final/plan.md` in the workspace."
        )
        assert await _audit(app, "workspace_rename") == [
            (
                {
                    "thread_id": f"{TENANT}:{thread}",
                    "path": "draft.md",
                    "to_path": "docs/final/plan.md",
                    "manifest": "quick",
                },
                "quick",
                "ok",
            )
        ]


async def test_a_rename_during_a_run_is_queued_for_its_next_model_call(boot: Any, root: Path) -> None:
    from felix.workspace_notes import drain, mark_run_active, mark_run_idle

    thread = "pane-rename-live"
    scoped = f"{TENANT}:{thread}"
    async with boot([], env=_env(root)) as app:
        (_scope_dir(root, thread) / "a.md").write_text("a")
        await mark_run_active(TENANT, scoped)
        try:
            resp = await app.client.post(
                "/chat/workspace/rename", json={"thread_id": thread, "path": "a.md", "to_path": "b.md"}
            )
            assert resp.status_code == 200, resp.text
            assert (resp.json()["status"], resp.json()["event_id"]) == ("queued", None)
            notes = await drain(TENANT, scoped)
            assert [(n.path, n.op, n.to_path) for n in notes] == [("a.md", "rename", "b.md")]
        finally:
            await mark_run_idle(TENANT, scoped)


@pytest.mark.parametrize(
    ("route", "extra", "status", "detail"),
    [
        ("delete", {"path": "missing.md"}, 404, "not_found"),
        ("delete", {"path": "file.txt/under"}, 404, "not_found"),
        ("delete", {"path": "docs"}, 400, "not_a_file"),
        ("delete", {"path": "link.txt"}, 400, "invalid_path"),
        ("delete", {"path": "escape/secret.txt"}, 400, "invalid_path"),
        ("delete", {"path": "../outside/secret.txt"}, 400, "invalid_path"),
        ("delete", {"path": "/etc/passwd"}, 400, "invalid_path"),
        ("delete", {"path": "x" * 256}, 400, "invalid_path"),
        ("delete", {"path": ".git/config"}, 400, "reserved_path"),
        ("delete", {"path": ".felix-scopes/x"}, 400, "reserved_path"),
        ("rename", {"path": "missing.md", "to_path": "new.md"}, 404, "not_found"),
        ("rename", {"path": "file.txt", "to_path": "other.txt"}, 409, "target_exists"),
        ("rename", {"path": "file.txt", "to_path": "docs"}, 409, "target_exists"),
        ("rename", {"path": "file.txt", "to_path": "./file.txt"}, 409, "target_exists"),
        ("rename", {"path": "docs", "to_path": "docs2"}, 400, "not_a_file"),
        ("rename", {"path": "link.txt", "to_path": "new.md"}, 400, "invalid_path"),
        ("rename", {"path": "file.txt", "to_path": "link.txt"}, 400, "invalid_path"),
        ("rename", {"path": "file.txt", "to_path": "escape/stolen.txt"}, 400, "invalid_path"),
        ("rename", {"path": "file.txt", "to_path": "../outside/stolen.txt"}, 400, "invalid_path"),
        ("rename", {"path": "file.txt", "to_path": "other.txt/inside.txt"}, 400, "invalid_path"),
        ("rename", {"path": "file.txt", "to_path": "y" * 256}, 400, "invalid_path"),
        ("rename", {"path": "file.txt", "to_path": ".git/hooks/pre-commit"}, 400, "reserved_path"),
        ("rename", {"path": ".git/config", "to_path": "config"}, 400, "reserved_path"),
        ("rename", {"path": "file.txt", "to_path": "a/.felix-edit-0123456789abcdef"}, 400, "reserved_path"),
    ],
)
async def test_a_delete_or_rename_that_cannot_be_made_changes_nothing(
    boot: Any, root: Path, tmp_path: Path, route: str, extra: dict[str, Any], status: int, detail: str
) -> None:
    thread = "pane-bad-change"
    async with boot([], env=_env(root)) as app:
        here = _scope_dir(root, thread)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("not yours")
        (here / "docs").mkdir()
        (here / "file.txt").write_text("a file")
        (here / "other.txt").write_text("another")
        (here / ".git").mkdir()
        (here / ".git" / "config").write_text("[core]")
        os.symlink(outside / "secret.txt", here / "link.txt")
        os.symlink(outside, here / "escape")
        before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
        resp = await app.client.post(f"/chat/workspace/{route}", json={"thread_id": thread, **extra})
        assert (resp.status_code, resp.json()) == (status, {"detail": detail}), resp.text
        assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == before
        assert (here / "file.txt").read_text() == "a file"
        assert (here / "other.txt").read_text() == "another"
        assert (outside / "secret.txt").read_text() == "not yours"
        assert (await app.client.get(f"/chat/sessions/{thread}")).json()["transcript"] == []


@pytest.mark.parametrize("route", ["delete", "rename"])
async def test_a_stale_hash_refuses_the_change_and_leaves_the_file(boot: Any, root: Path, route: str) -> None:
    thread = f"pane-stale-{route}"
    async with boot([], env=_env(root)) as app:
        here = _scope_dir(root, thread)
        target = here / "plan.md"
        target.write_text("what the operator read")
        read = (
            await app.client.get("/chat/workspace/file", params={"thread_id": thread, "path": "plan.md"})
        ).json()
        target.write_text("what the agent wrote since")
        body: dict[str, Any] = {"thread_id": thread, "path": "plan.md", "expected_sha256": read["sha256"]}
        if route == "rename":
            body["to_path"] = "moved/plan.md"
        resp = await app.client.post(f"/chat/workspace/{route}", json=body)
        now = b"what the agent wrote since"
        assert (resp.status_code, resp.json()) == (
            409,
            {"detail": "workspace_changed", "sha256": hashlib.sha256(now).hexdigest(), "bytes": len(now)},
        )
        assert target.read_bytes() == now
        assert not (here / "moved").exists()
        assert (await app.client.get(f"/chat/sessions/{thread}")).json()["transcript"] == []


@pytest.mark.parametrize(
    ("route", "body"),
    [
        ("delete", {"path": "a.md", "content": "x"}),
        ("delete", {"path": "a.md", "op": "delete"}),
        ("delete", {"path": "a.md", "expected_sha256": "nothex"}),
        ("delete", {"path": "a\nb.md"}),
        ("rename", {"path": "a.md"}),
        ("rename", {"path": "a.md", "to_path": "b.md", "overwrite": True}),
        ("rename", {"path": "a.md", "to_path": "b\n.md"}),
        ("rename", {"path": "a.md", "to_path": ""}),
    ],
)
async def test_a_malformed_change_is_a_422(boot: Any, root: Path, route: str, body: dict[str, Any]) -> None:
    thread = "pane-422"
    async with boot([], env=_env(root)) as app:
        (_scope_dir(root, thread) / "a.md").write_text("kept")
        resp = await app.client.post(f"/chat/workspace/{route}", json={"thread_id": thread, **body})
        assert resp.status_code == 422, resp.text
        assert (_scope_dir(root, thread) / "a.md").read_text() == "kept"


async def test_an_observer_may_not_delete_or_rename(boot: Any, root: Path) -> None:
    from felix_api.routes.chat import LEASE_TOKEN_HEADER

    thread = "pane-lease-change"
    async with boot([], env=_env(root)) as app:
        await app.client.post(
            "/chat/sessions/lease", json={"thread_id": thread, "holder_id": "tab-a", "mode": "exclusive"}
        )
        observer = await app.client.post(
            "/chat/sessions/lease", json={"thread_id": thread, "holder_id": "tab-b", "mode": "shared"}
        )
        headers = {LEASE_TOKEN_HEADER: observer.json()["token"]}
        here = _scope_dir(root, thread)
        (here / "a.md").write_text("as it was")
        for route, extra in (("delete", {}), ("rename", {"to_path": "b.md"})):
            refused = await app.client.post(
                f"/chat/workspace/{route}",
                json={"thread_id": thread, "path": "a.md", **extra},
                headers=headers,
            )
            assert (refused.status_code, refused.json()["detail"]) == (409, "lease_read_only"), route
        assert sorted(p.name for p in here.iterdir()) == ["a.md"]


async def test_a_thread_with_a_checkout_deletes_and_renames_in_the_checkout(
    boot: Any, root: Path, tmp_path: Path
) -> None:
    from felix.config import get_settings
    from felix.repos.checkouts import REPO_DIR, STATE_FILE, thread_dir

    thread = "pane-repo-change"
    checkouts = tmp_path / "checkouts"
    async with boot([], env=_env(root, FELIX_REPO_CHECKOUT_ROOT=str(checkouts))) as app:
        directory = thread_dir(get_settings(), TENANT, f"{TENANT}:{thread}")
        repo = directory / REPO_DIR
        (repo / "pkg").mkdir(parents=True)
        (repo / "pkg" / "old.py").write_text("old")
        (repo / "pkg" / "gone.py").write_text("gone")
        (directory / STATE_FILE).write_text(json.dumps({"state": "ready", "repo": "acme/widgets"}))
        scoped = _scope_dir(root, thread)
        (scoped / "pkg").mkdir()
        (scoped / "pkg" / "gone.py").write_text("the scoped copy")

        renamed = await app.client.post(
            "/chat/workspace/rename",
            json={"thread_id": thread, "path": "pkg/old.py", "to_path": "pkg/new.py"},
        )
        assert renamed.status_code == 200, renamed.text
        assert (repo / "pkg" / "new.py").read_text() == "old"
        assert not (repo / "pkg" / "old.py").exists()

        deleted = await app.client.post(
            "/chat/workspace/delete", json={"thread_id": thread, "path": "pkg/gone.py"}
        )
        assert deleted.status_code == 200, deleted.text
        assert not (repo / "pkg" / "gone.py").exists()
        assert (scoped / "pkg" / "gone.py").read_text() == "the scoped copy"
