"""Workspace scopes: which directory a tool call reaches, and that no call reaches another's.

Before scopes, FELIX_WORKSPACE_ROOT was one directory for every tenant and thread on the host: a
tenant's agent read and overwrote every other tenant's files. These pin the boundary that replaced
it (`felix.tools.workspace_scope`): a thread's files are its own by default, a tenant's shared
scope is the tenant's alone, the whole root is the operator's, and the remote shell runner is held
to the same scope as the API that sent it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.manifests.builder import apply_workspace_scope
from felix.manifests.schema import ShellToolRef
from felix.shell_runner import create_runner_app
from felix.tools import shell as shell_mod
from felix.tools.builtins import default_tool_provider
from felix.tools.errors import ToolErrorCode, read_tool_error_code
from felix.tools.shell import tools_from_shell_refs
from felix.tools.types import ToolInvocationCtx, tool_output_content
from felix.tools.workspace import workspace_root
from felix.tools.workspace_scope import (
    SCOPES_DIR,
    bound_scope,
    is_scope_relpath,
    migrate_legacy_files,
    scope_relpath,
    thread_key,
)

TOKEN = "test-runner-token-" + "0" * 24


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def _settings(ws: Path, **kw: Any) -> Settings:
    return Settings(workspace_root=str(ws), **kw)


async def _call(
    settings: Settings,
    tenant: str,
    thread: str | None,
    tool: str,
    args: dict[str, Any],
    scope: str | None = None,
) -> str:
    provider = default_tool_provider()
    ctx = RequestContext(settings=settings, auth=AuthContext(tenant_id=tenant), thread_id=thread)
    async with async_run_with_context(ctx):
        execute = provider.get(tool).executor.execute
        if scope is None:
            out = await execute(args, ToolInvocationCtx(thread_id=thread))
        else:
            with bound_scope(scope):  # type: ignore[arg-type]
                out = await execute(args, ToolInvocationCtx(thread_id=thread))
    return tool_output_content(out)


async def _write(
    settings: Settings, tenant: str, thread: str | None, path: str, body: str, scope: str | None = None
) -> str:
    return await _call(settings, tenant, thread, "write_file", {"path": path, "content": body}, scope)


async def _read(
    settings: Settings, tenant: str, thread: str | None, path: str, scope: str | None = None
) -> str:
    return await _call(settings, tenant, thread, "read_file", {"path": path}, scope)


# --- thread scope, the default ------------------------------------------------------------


async def test_a_threads_files_are_its_own(ws: Path) -> None:
    settings = _settings(ws)
    await _write(settings, "acme", "acme:t1", "notes.txt", "from t1")
    assert "from t1" in await _read(settings, "acme", "acme:t1", "notes.txt")
    other = await _read(settings, "acme", "acme:t2", "notes.txt")
    assert "from t1" not in other
    assert not (ws / "notes.txt").exists()
    key = thread_key("acme", "acme:t1")
    assert (ws / SCOPES_DIR / "acme" / key / "notes.txt").read_text() == "from t1"


async def test_two_tenants_with_the_same_thread_id_do_not_share(ws: Path) -> None:
    settings = _settings(ws)
    await _write(settings, "acme", "t1", "notes.txt", "acme's")
    assert "acme's" not in await _read(settings, "globex", "t1", "notes.txt")


async def test_a_call_with_no_thread_is_refused_not_given_a_shared_directory(ws: Path) -> None:
    out = await _write(_settings(ws), "acme", None, "notes.txt", "x")
    assert out.startswith("[tool error/transport_unavailable]")
    assert "belongs to a thread" in out
    assert not any(ws.rglob("notes.txt"))


async def test_a_path_cannot_climb_out_of_a_scope(ws: Path) -> None:
    settings = _settings(ws)
    await _write(settings, "acme", "acme:t1", "secret.txt", "t1's secret")
    key = thread_key("acme", "acme:t1")
    for path in (f"../{key}/secret.txt", "../../acme/shared/x", "../../../secret.txt"):
        out = await _read(settings, "acme", "acme:t2", path)
        assert "t1's secret" not in out
        assert out.startswith("[tool error/"), out


async def test_a_thread_id_of_any_shape_is_a_safe_directory(ws: Path) -> None:
    settings = _settings(ws)
    for thread in ("acme:a/../../b", "acme:..", "acme:with space", "acme:é"):
        await _write(settings, "acme", thread, "f.txt", thread)
        assert json.loads(await _read(settings, "acme", thread, "f.txt"))["content"] == thread
    assert sorted(p.name for p in (ws / SCOPES_DIR / "acme").iterdir()) == sorted(
        thread_key("acme", t) for t in ("acme:a/../../b", "acme:..", "acme:with space", "acme:é")
    )


# --- tenant scope -------------------------------------------------------------------------


async def test_a_tenant_scope_is_shared_by_its_threads_and_no_one_else(ws: Path) -> None:
    settings = _settings(ws)
    await _write(settings, "acme", "acme:t1", "plan.md", "acme plan", scope="tenant")
    assert "acme plan" in await _read(settings, "acme", "acme:t2", "plan.md", scope="tenant")
    assert "acme plan" in await _read(settings, "acme", None, "plan.md", scope="tenant")
    assert "acme plan" not in await _read(settings, "globex", "globex:t1", "plan.md", scope="tenant")
    # A tenant scope and a thread scope of the same tenant are different directories.
    assert "acme plan" not in await _read(settings, "acme", "acme:t1", "plan.md")


# --- deployment scope ---------------------------------------------------------------------


async def test_the_deployment_scope_is_the_root_for_the_operators_tenant(ws: Path) -> None:
    (ws / "README.md").write_text("the checkout")
    settings = _settings(ws)
    assert "the checkout" in await _read(settings, "default", None, "README.md", scope="deployment")


async def test_the_deployment_scope_is_refused_for_any_other_tenant(ws: Path) -> None:
    (ws / "README.md").write_text("the checkout")
    out = await _read(_settings(ws), "gh-4242", "gh-4242:t1", "README.md", scope="deployment")
    assert "the checkout" not in out
    assert out.startswith("[tool error/transport_unavailable]")
    assert "FELIX_WORKSPACE_DEPLOYMENT_TENANTS" in out


async def test_the_operator_names_which_tenants_get_the_deployment_scope(ws: Path) -> None:
    (ws / "README.md").write_text("the checkout")
    settings = _settings(ws, workspace_deployment_tenants="ops, builders")
    assert "the checkout" in await _read(settings, "builders", None, "README.md", scope="deployment")
    out = await _read(settings, "default", None, "README.md", scope="deployment")
    assert out.startswith("[tool error/transport_unavailable]")


# --- the scope directory itself -----------------------------------------------------------


async def test_a_symlinked_scope_directory_is_refused(ws: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (ws / SCOPES_DIR).mkdir()
    os.symlink(elsewhere, ws / SCOPES_DIR / "acme")
    out = await _write(_settings(ws), "acme", "acme:t1", "x.txt", "x")
    assert out.startswith("[tool error/transport_unavailable]")
    assert "symlink" in out
    assert not any(elsewhere.iterdir())


async def test_scope_directories_are_private_to_the_process_user(ws: Path) -> None:
    await _write(_settings(ws), "acme", "acme:t1", "x.txt", "x")
    for d in (ws / SCOPES_DIR, ws / SCOPES_DIR / "acme"):
        assert d.stat().st_mode & 0o077 == 0, oct(d.stat().st_mode)


def test_workspace_root_outside_a_request_is_the_deployment_root(ws: Path) -> None:
    from felix.tools.workspace import deployment_workspace

    assert deployment_workspace(str(ws)) == ws.resolve()


async def test_workspace_root_follows_the_bound_scope(ws: Path) -> None:
    settings = _settings(ws)
    ctx = RequestContext(settings=settings, auth=AuthContext(tenant_id="acme"), thread_id="acme:t1")
    async with async_run_with_context(ctx):
        assert workspace_root() == ws.resolve() / scope_relpath(settings, "acme", "acme:t1", "thread")
        with bound_scope("tenant"):
            assert workspace_root() == ws.resolve() / SCOPES_DIR / "acme" / "shared"


# --- the builder binds the manifest's scope -----------------------------------------------


async def test_the_builder_wrapper_binds_the_manifests_scope_around_the_call(ws: Path) -> None:
    settings = _settings(ws)
    write = default_tool_provider().get("write_file")
    (wrapped,) = apply_workspace_scope([write], "tenant")
    ctx = RequestContext(settings=settings, auth=AuthContext(tenant_id="acme"), thread_id="acme:t1")
    async with async_run_with_context(ctx):
        await wrapped.executor.execute(
            {"path": "x.txt", "content": "shared"}, ToolInvocationCtx(thread_id="acme:t1")
        )
    assert (ws / SCOPES_DIR / "acme" / "shared" / "x.txt").read_text() == "shared"


def test_a_manifest_defaults_to_the_thread_scope_and_rejects_an_unknown_one() -> None:
    from felix.manifests.schema import Spec
    from pydantic import ValidationError

    assert Spec().workspace.scope == "thread"
    with pytest.raises(ValidationError):
        Spec.model_validate({"workspace": {"scope": "everyone"}})


def test_the_self_build_manifests_ask_for_the_deployment_scope_and_cowork_does_not() -> None:
    from felix.manifests.loader import load_manifest_file

    repo = Path(__file__).resolve().parents[2]
    scopes = {
        name: load_manifest_file(repo / "manifests" / f"{name}.yaml").spec.workspace.scope
        for name in ("contributor", "triage", "cowork")
    }
    assert scopes == {"contributor": "deployment", "triage": "deployment", "cowork": "thread"}


# --- the remote shell runner --------------------------------------------------------------


@pytest.mark.parametrize(
    ("rel", "ok"),
    [
        ("", True),
        (f"{SCOPES_DIR}/acme/shared", True),
        (f"{SCOPES_DIR}/acme/{'a' * 40}", True),
        (f"{SCOPES_DIR}/acme/{'A' * 40}", False),
        (f"{SCOPES_DIR}/acme/other", False),
        (f"{SCOPES_DIR}/../shared", False),
        (f"{SCOPES_DIR}/acme/shared/..", False),
        ("acme/shared", False),
        ("/etc", False),
    ],
)
def test_the_runner_accepts_only_scope_shaped_paths(rel: str, ok: bool) -> None:
    assert is_scope_relpath(rel) is ok


async def test_the_runner_runs_in_the_scope_the_api_sent(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    settings = _settings(
        ws, shell_allowed_commands=sys.executable, shell_runner_url="http://runner", shell_runner_token=TOKEN
    )
    app = create_runner_app(settings)
    monkeypatch.setattr(
        shell_mod,
        "_runner_client",
        lambda timeout_s: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), timeout=timeout_s),
    )
    (tool,) = tools_from_shell_refs([ShellToolRef(name="py", commands=[sys.executable])], settings=settings)
    ctx = RequestContext(settings=settings, auth=AuthContext(tenant_id="acme"), thread_id="acme:t1")
    async with async_run_with_context(ctx):
        out = await tool.executor.execute(
            {"argv": [sys.executable, "-c", "import os; print(os.getcwd())"]},
            ToolInvocationCtx(thread_id="acme:t1"),
        )
    assert read_tool_error_code(out) is None, tool_output_content(out)
    expected = (ws / SCOPES_DIR / "acme" / thread_key("acme", "acme:t1")).resolve()
    assert str(expected) in tool_output_content(out)


async def test_the_runner_refuses_a_scope_it_did_not_produce(ws: Path) -> None:
    import sys

    settings = _settings(ws, shell_allowed_commands=sys.executable, shell_runner_token=TOKEN)
    app = create_runner_app(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shell") as client:
        resp = await client.post(
            "/run",
            headers={"authorization": f"Bearer {TOKEN}"},
            json={"argv": [sys.executable, "-c", "print(1)"], "cwd": ".", "scope": "../../etc"},
        )
    assert resp.status_code == 403
    assert resp.json()["reason"] == "scope"
    _ = ToolErrorCode


# --- migrating files written before scopes ------------------------------------------------


def test_migrate_moves_legacy_files_into_the_default_tenants_shared_scope(ws: Path) -> None:
    (ws / "notes.txt").write_text("old")
    (ws / "sub").mkdir()
    (ws / "sub" / "deep.txt").write_text("deeper")
    (ws / "AGENTS.md").write_text("operator instructions")
    result = migrate_legacy_files(ws, keep=frozenset({"AGENTS.md"}))
    target = ws / SCOPES_DIR / "default" / "shared"
    assert result.moved == ("notes.txt", "sub")
    assert result.kept == ("AGENTS.md",)
    assert (target / "notes.txt").read_text() == "old"
    assert (target / "sub" / "deep.txt").read_text() == "deeper"
    assert (ws / "AGENTS.md").exists()
    assert sorted(p.name for p in ws.iterdir()) == [SCOPES_DIR, "AGENTS.md"]


def test_migrate_is_idempotent_and_never_overwrites(ws: Path) -> None:
    target = ws / SCOPES_DIR / "default" / "shared"
    (ws / "notes.txt").write_text("old")
    migrate_legacy_files(ws)
    assert migrate_legacy_files(ws).moved == ()
    (ws / "notes.txt").write_text("a second one")
    result = migrate_legacy_files(ws)
    assert result.collided == ("notes.txt",)
    assert (target / "notes.txt").read_text() == "old"
    assert (ws / "notes.txt").read_text() == "a second one"


def test_migrate_dry_run_moves_nothing(ws: Path) -> None:
    (ws / "notes.txt").write_text("old")
    result = migrate_legacy_files(ws, dry_run=True)
    assert result.moved == ("notes.txt",)
    assert (ws / "notes.txt").exists()
    assert not (ws / SCOPES_DIR).exists()


async def test_migrated_files_are_what_a_default_tenant_scope_agent_reads(ws: Path) -> None:
    (ws / "notes.txt").write_text("from before")
    migrate_legacy_files(ws)
    assert "from before" in await _read(_settings(ws), "default", "default:t9", "notes.txt", scope="tenant")
