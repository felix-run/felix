"""`delete_file` and `rename_file`: the agent's half of the file pane's delete and rename.

The tools sit on the backend operations the pane's routes use (`WorkspaceBackend.delete_file`,
`rename_file`), with no digest from the model, and answer the way the other workspace tools do:
a JSON result, or a `[tool error/<code>]` refusal the model can act on. The same calls run
against `local` and against `hosted` (a fake gateway serving the real `felix-fs` helper) and must
give the same answers word for word.

The manifest half is the rule that keeps this safe to ship: a delete is never gated less than a
write. Every bundled manifest that binds or gates a file writer -- the server's `write_file` /
`edit_file`, a client's `local_write` / `local_edit` -- binds and gates the delete and the rename
the same way.
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
from felix.manifests.tool_match import matches_any
from felix.session.compaction import extract_file_ops_from_events
from felix.session.types import SessionEvent
from felix.tools import workspace_hosted
from felix.tools.builtins import default_tool_provider
from felix.tools.types import ToolInvocationCtx, tool_output_content
from felix.tools.workspace import WORKSPACE_TOOL_NAMES
from felix.tools.workspace_scope import SCOPES_DIR, thread_key
from felix.usage.catalog import workspace_summary

from tests.workspace_gateway_fake import TOKEN, URL, FakeGateway

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
    (here / "a.txt").write_text("alpha")
    (here / "b.txt").write_text("bravo")
    (here / "gone.txt").write_text("bye")
    (here / ".git").mkdir()
    (here / ".git" / "config").write_text("[core]")
    os.symlink(outside / "secret.txt", here / "link.txt")
    os.symlink(outside, here / "escape")


async def _session(settings: Settings, here: Path, outside: Path) -> list[str]:
    """Every answer, in order: the happy paths, then each refusal."""
    _seed(here, outside)
    out: list[str] = []
    async with _request(settings):
        out.append(await _call("delete_file", {"path": "gone.txt"}))
        out.append(await _call("rename_file", {"path": "notes/plan.md", "to_path": "archive/2026/plan.md"}))
        # Refusals: missing, a directory, the root, a symlink, through a symlink, escaping,
        # absolute, reserved.
        out.append(await _call("delete_file", {"path": "gone.txt"}))
        out.append(await _call("delete_file", {"path": "notes"}))
        out.append(await _call("delete_file", {"path": "."}))
        out.append(await _call("delete_file", {"path": "link.txt"}))
        out.append(await _call("delete_file", {"path": "escape/secret.txt"}))
        out.append(await _call("delete_file", {"path": "../outside/secret.txt"}))
        out.append(await _call("delete_file", {"path": "/etc/passwd"}))
        out.append(await _call("delete_file", {"path": ".git/config"}))
        # A rename never replaces: the other file, a directory, the file itself.
        out.append(await _call("rename_file", {"path": "a.txt", "to_path": "b.txt"}))
        out.append(await _call("rename_file", {"path": "a.txt", "to_path": "notes"}))
        out.append(await _call("rename_file", {"path": "a.txt", "to_path": "a.txt"}))
        out.append(await _call("rename_file", {"path": "missing.txt", "to_path": "c.txt"}))
        out.append(await _call("rename_file", {"path": "notes", "to_path": "notes2"}))
        out.append(await _call("rename_file", {"path": "link.txt", "to_path": "c.txt"}))
        out.append(await _call("rename_file", {"path": "a.txt", "to_path": "escape/a.txt"}))
        out.append(await _call("rename_file", {"path": "a.txt", "to_path": "../outside/a.txt"}))
        out.append(await _call("rename_file", {"path": "a.txt", "to_path": "b.txt/c.txt"}))
        out.append(await _call("rename_file", {"path": "a.txt", "to_path": ".git/hooks/pre-commit"}))
        out.append(await _call("rename_file", {"path": ".git/config", "to_path": "config"}))
    return out


EXPECTED = [
    {"path": "gone.txt", "deleted": True},
    {"path": "notes/plan.md", "to_path": "archive/2026/plan.md", "bytes": 4},
    "[tool error/invalid_arguments] no such file: gone.txt",
    "[tool error/invalid_arguments] not a file: notes (delete_file removes files only, never a directory)",
    "[tool error/invalid_arguments] not a file: . (delete_file removes files only, never a directory)",
    "[tool error/invalid_arguments] symlinks are not followed in workspace paths: link.txt",
    "[tool error/invalid_arguments] symlinks are not followed in workspace paths: escape",
    "[tool error/invalid_arguments] path escapes workspace root",
    "[tool error/invalid_arguments] absolute paths are not allowed",
    "[tool error/invalid_arguments] reserved path: .git/config "
    "(inside .git or .felix-scopes, or an edit's temporary file)",
    "[tool error/invalid_arguments] b.txt already exists; rename_file never replaces anything -- "
    "choose another to_path, or delete the file there first",
    "[tool error/invalid_arguments] notes already exists; rename_file never replaces anything -- "
    "choose another to_path, or delete the file there first",
    "[tool error/invalid_arguments] a.txt already exists; rename_file never replaces anything -- "
    "choose another to_path, or delete the file there first",
    "[tool error/invalid_arguments] no such file: missing.txt",
    "[tool error/invalid_arguments] not a file: notes (rename_file moves files only, never a directory)",
    "[tool error/invalid_arguments] symlinks are not followed in workspace paths: link.txt",
    "[tool error/invalid_arguments] symlinks are not followed in workspace paths: escape",
    "[tool error/invalid_arguments] path escapes workspace root",
    "[tool error/invalid_arguments] the destination's directory is a file",
    "[tool error/invalid_arguments] reserved path: .git/hooks/pre-commit "
    "(inside .git or .felix-scopes, or an edit's temporary file)",
    "[tool error/invalid_arguments] reserved path: .git/config "
    "(inside .git or .felix-scopes, or an edit's temporary file)",
]


@pytest.mark.parametrize("backend", ["local", "hosted"])
async def test_both_backends_delete_and_rename_and_refuse_the_same_way(
    tmp_path: Path, gateway: FakeGateway, backend: str
) -> None:
    here = _scope_dir(tmp_path, backend, gateway)
    outside = tmp_path / "outside"
    transcript = await _session(_settings(tmp_path, backend), here, outside)

    answers: list[Any] = [json.loads(t) for t in transcript[:2]] + transcript[2:]
    assert answers == EXPECTED

    # What happened: one file gone, one moved into folders the rename made.
    assert not (here / "gone.txt").exists()
    assert not (here / "notes" / "plan.md").exists() and (here / "notes").is_dir()
    assert (here / "archive" / "2026" / "plan.md").read_text() == "plan"
    # What every refusal left alone: both ends of each refused rename, the links, the outside.
    assert ((here / "a.txt").read_text(), (here / "b.txt").read_text()) == ("alpha", "bravo")
    assert (here / ".git" / "config").read_text() == "[core]"
    assert not (here / ".git" / "hooks").exists()
    assert os.path.islink(here / "link.txt") and os.path.islink(here / "escape")
    assert (outside / "secret.txt").read_text() == "not yours"
    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]
    for never in ("c.txt", "notes2", "config"):
        assert not (here / never).exists()


async def test_a_hosted_delete_and_rename_are_checkpointed(tmp_path: Path, gateway: FakeGateway) -> None:
    here = _scope_dir(tmp_path, "hosted", gateway)
    (here / "a.md").write_text("a")
    (here / "b.md").write_text("b")
    async with _request(_settings(tmp_path, "hosted")):
        await _call("delete_file", {"path": "a.md"})
        await _call("rename_file", {"path": "b.md", "to_path": "c.md"})
    assert gateway.checkpoints == [f"{TENANT}/{thread_key(TENANT, THREAD)}"]


async def test_a_refusal_before_the_backend_never_reaches_the_gateway(
    tmp_path: Path, gateway: FakeGateway
) -> None:
    _scope_dir(tmp_path, "hosted", gateway)
    async with _request(_settings(tmp_path, "hosted")):
        await _call("delete_file", {"path": ".git/config"})
        await _call("delete_file", {"path": "."})
        await _call("rename_file", {"path": "a.txt", "to_path": ".felix-scopes/x"})
    # The scope is prepared (the workspace is judged before the arguments); nothing is changed.
    assert {op for _, op in gateway.calls} <= {"prepare"}


def _schema(tool: str) -> dict[str, Any]:
    schema = default_tool_provider().get(tool).args_schema
    return schema if isinstance(schema, dict) else schema.model_json_schema()  # type: ignore[union-attr]


def test_the_model_cannot_send_a_digest_or_anything_else() -> None:
    provider = default_tool_provider()
    delete, rename = _schema("delete_file"), _schema("rename_file")
    assert set(delete["properties"]) == {"path"} and delete["required"] == ["path"]
    assert set(rename["properties"]) == {"path", "to_path"}
    assert sorted(rename["required"]) == ["path", "to_path"]
    assert delete.get("additionalProperties") is False and rename.get("additionalProperties") is False
    # Neither is replayed on a durable resume: each changes the workspace.
    assert not provider.get("delete_file").replay_safe
    assert not provider.get("rename_file").replay_safe


def test_both_are_workspace_tools_in_the_catalog() -> None:
    assert {"delete_file", "rename_file"} <= WORKSPACE_TOOL_NAMES
    assert set(default_tool_provider().list()) >= WORKSPACE_TOOL_NAMES

    class _Spec:
        tools = ["delete_file"]
        shell_tools: list[str] = []
        client_tools: list[Any] = []
        workspace = None

    class _Manifest:
        spec = _Spec()

    assert workspace_summary(_Manifest()) == {"tools": "server", "scope": "thread"}


def test_compaction_counts_a_delete_and_both_ends_of_a_rename_as_changed() -> None:
    calls = [
        {"id": "1", "name": "delete_file", "args": {"path": "old.md"}},
        {"id": "2", "name": "local_rename", "args": {"path": "a.md", "to_path": "docs/a.md"}},
        {"id": "3", "name": "read_file", "args": {"path": "kept.md"}},
    ]
    ops = extract_file_ops_from_events(
        [SessionEvent(seq=1, ts=0.0, kind="message", role="assistant", content="", tool_calls=calls)]
    )
    assert ops == {"readFiles": ["kept.md"], "modifiedFiles": ["old.md", "a.md", "docs/a.md"]}


# --- every bundled manifest: a delete is never gated less than a write ------------------

SERVER_WRITERS = ("write_file", "edit_file")
SERVER_CHANGERS = ("delete_file", "rename_file")
CLIENT_WRITERS = ("local_write", "local_edit")
CLIENT_CHANGERS = ("local_delete", "local_rename")


def _bundled() -> list[Path]:
    return sorted((ROOT / "manifests").rglob("*.yaml"))


def _gating_rules(manifest: Manifest, tool: str) -> list[str]:
    return [rule.id for rule in manifest.spec.approvals if matches_any(rule.tools, tool)]


def _parity_gaps(manifest: Manifest) -> list[str]:
    """Each way `manifest` lets a delete or a rename through that it would not let a write through."""
    spec = manifest.spec
    bound = {str(t) for t in spec.tools} | {t.name for t in spec.client_tools}
    gaps: list[str] = []
    for writers, changers in ((SERVER_WRITERS, SERVER_CHANGERS), (CLIENT_WRITERS, CLIENT_CHANGERS)):
        if not any(w in bound for w in writers):
            continue
        gaps += [f"binds {writers} but not {c}" for c in changers if c not in bound]
        gated_writer = any(_gating_rules(manifest, w) for w in writers if w in bound)
        if gated_writer:
            gaps += [f"gates a writer but not {c}" for c in changers if not _gating_rules(manifest, c)]
    return gaps


def test_the_bundled_manifests_are_found_and_all_validate() -> None:
    paths = _bundled()
    names = {p.stem for p in paths}
    assert {"cowork", "contributor", "triage", "quick"} <= names
    for path in paths:
        assert isinstance(load_manifest_file(path), Manifest), path


@pytest.mark.parametrize("path", _bundled(), ids=lambda p: p.stem)
def test_no_bundled_manifest_gates_a_delete_less_than_a_write(path: Path) -> None:
    gaps = _parity_gaps(load_manifest_file(path))
    assert not gaps, f"{path.relative_to(ROOT)}: {gaps}"


def test_the_parity_check_catches_a_manifest_that_gates_only_the_write() -> None:
    """The check above is only worth something if it fails: a write gated and a delete not."""
    cowork = load_manifest_file(ROOT / "manifests" / "cowork.yaml")
    assert _parity_gaps(cowork) == []
    data = cowork.model_dump(mode="json", by_alias=True, exclude_none=True)
    for rule in data["spec"]["approvals"]:
        rule["tools"] = [t for t in rule["tools"] if t != "local_delete"]
    weakened = Manifest.model_validate(data)
    assert _parity_gaps(weakened) == ["gates a writer but not local_delete"]

    data["spec"]["client_tools"] = [t for t in data["spec"]["client_tools"] if t["name"] != "local_rename"]
    assert "binds ('local_write', 'local_edit') but not local_rename" in _parity_gaps(
        Manifest.model_validate(data)
    )

    server = Manifest.model_validate(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "w"},
            "spec": {
                "tools": ["write_file", "delete_file", "rename_file"],
                "approvals": [{"id": "w", "tools": ["write_file", "rename_file"], "ttl_seconds": 60}],
            },
        }
    )
    assert _parity_gaps(server) == ["gates a writer but not delete_file"]


def test_cowork_declares_and_gates_the_delete_and_the_rename() -> None:
    cowork = load_manifest_file(ROOT / "manifests" / "cowork.yaml")
    declared = {t.name: t for t in cowork.spec.client_tools}
    delete, rename = declared["local_delete"], declared["local_rename"]
    assert delete.args_schema["required"] == ["path"]
    assert set(delete.args_schema["properties"]) == {"path"}
    assert rename.args_schema["required"] == ["path", "to_path"]
    assert set(rename.args_schema["properties"]) == {"path", "to_path"}
    assert "no undo" in delete.description.lower()
    assert "never overwrites" in rename.description.lower()
    # One rule for every change to the user's folder, so a delete waits exactly as a write does.
    assert (
        _gating_rules(cowork, "local_delete") == _gating_rules(cowork, "local_write") == ["workspace-write"]
    )
    assert _gating_rules(cowork, "local_rename") == ["workspace-write"]
    prompt = cowork.spec.system_prompt.inline or ""
    assert "local_delete" in prompt and "local_rename" in prompt
    assert workspace_summary(cowork)["tools"] == "client"
