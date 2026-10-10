"""The hosted workspace backend, and the two backends held to one behaviour.

`FELIX_WORKSPACE_BACKEND=hosted` sends the workspace tools' file operations to the gateway Worker,
which runs them in the scope's sandbox with the `felix-fs` helper. The conformance half runs the
same tool calls against `local` and against `hosted` (a fake gateway serving the real helper,
`tests/support/workspace_gateway_fake.py`) and requires the same answers, word for word: a model, and the
audit log, cannot tell which backend served a call. The rest pins what is hosted-only -- the
scope-to-sandbox naming, the end-of-run checkpoint, the gateway failing, and the consumers that
need a directory on the host being refused.
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
from felix.tools import workspace_hosted
from felix.tools.builtins import default_tool_provider
from felix.tools.types import ToolInvocationCtx, tool_output_content
from felix.tools.workspace_scope import SCOPES_DIR, bound_scope, thread_key

from tests.support.workspace_gateway_fake import TOKEN, URL, FakeGateway

TENANT = "acme"
THREAD = "acme:t1"


@pytest.fixture
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGateway:
    fake = FakeGateway(root=tmp_path / "sandboxes")
    monkeypatch.setattr(workspace_hosted, "gateway_client", lambda settings: fake.client())
    return fake


def _settings(tmp_path: Path, backend: str, **kw: Any) -> Settings:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    extra: dict[str, Any] = {}
    if backend == "hosted":
        extra = {
            "workspace_backend": "hosted",
            "workspace_gateway_url": URL,
            "workspace_gateway_token": TOKEN,
        }
    return Settings(workspace_root=str(ws), **extra, **kw)


def _scope_dir(tmp_path: Path, backend: str, tenant: str = TENANT, thread: str = THREAD) -> Path:
    """Where the scope's files are: under the local root, or in the fake sandbox."""
    key = thread_key(tenant, thread)
    if backend == "local":
        return tmp_path / "workspace" / SCOPES_DIR / tenant / key
    return tmp_path / "sandboxes" / tenant / key


@asynccontextmanager
async def _request(
    settings: Settings, tenant: str = TENANT, thread: str | None = THREAD
) -> AsyncIterator[None]:
    ctx = RequestContext(settings=settings, auth=AuthContext(tenant_id=tenant), thread_id=thread)
    async with async_run_with_context(ctx):
        yield


async def _call(tool: str, args: dict[str, Any], thread: str | None = THREAD) -> str:
    out = await default_tool_provider().get(tool).executor.execute(args, ToolInvocationCtx(thread_id=thread))
    return tool_output_content(out)


# --- conformance: one script, both backends, the same answers ---------------------------


async def _session(settings: Settings, scope_dir: Path) -> list[str]:
    """A cowork-shaped turn, refusals included. Every answer goes in the transcript."""
    out: list[str] = []
    async with _request(settings):
        out.append(await _call("write_file", {"path": "notes/plan.md", "content": "step one\r\nstep two\n"}))
        out.append(
            await _call("write_file", {"path": "notes/plan.md", "content": "step three\n", "append": True})
        )
        out.append(await _call("read_file", {"path": "notes/plan.md"}))
        out.append(await _call("read_file", {"path": "notes/plan.md", "offset": 5, "limit": 3}))
        out.append(await _call("list_dir", {"path": "notes"}))
        out.append(
            await _call("edit_file", {"path": "notes/plan.md", "old_string": "one", "new_string": "1"})
        )
        out.append(await _call("search_files", {"query": "step", "path": "."}))
        out.append(await _call("search_files", {"query": r"step\s+t\w+", "regex": True}))
        # Refusals, which must be worded the same.
        out.append(
            await _call("edit_file", {"path": "notes/plan.md", "old_string": "nope", "new_string": "x"})
        )
        out.append(
            await _call("edit_file", {"path": "notes/plan.md", "old_string": "step", "new_string": "s"})
        )
        out.append(await _call("read_file", {"path": "../outside.txt"}))
        out.append(await _call("read_file", {"path": "/etc/passwd"}))
        out.append(await _call("read_file", {"path": "missing.txt"}))
        out.append(await _call("list_dir", {"path": "missing"}))
        out.append(await _call("list_dir", {"path": "notes/plan.md"}))
        out.append(await _call("write_file", {"path": "notes", "content": "x"}))
        os.symlink("/etc", scope_dir / "link")
        out.append(await _call("read_file", {"path": "link/passwd"}))
        out.append(await _call("list_dir", {"path": "."}))
    return out


@pytest.mark.parametrize("backend", ["local", "hosted"])
async def test_both_backends_answer_a_session_identically(
    tmp_path: Path, gateway: FakeGateway, backend: str
) -> None:
    base = tmp_path / backend
    scope_dir = (
        _scope_dir(base, "local")
        if backend == "local"
        else gateway.root / TENANT / thread_key(TENANT, THREAD)
    )
    transcript = await _session(_settings(base, backend), scope_dir)
    reference_dir = tmp_path / "reference"
    reference = await _session(_settings(reference_dir, "local"), _scope_dir(reference_dir, "local"))
    assert transcript == reference
    # The answers are real ones, not two identical failures.
    assert json.loads(transcript[2])["content"] == "step one\r\nstep two\nstep three\n"
    assert "symlinks are not followed" in transcript[16]


async def test_the_files_land_in_the_scopes_sandbox_not_on_the_host(
    tmp_path: Path, gateway: FakeGateway
) -> None:
    settings = _settings(tmp_path, "hosted")
    async with _request(settings):
        await _call("write_file", {"path": "a.txt", "content": "hosted"})
    assert (_scope_dir(tmp_path, "hosted") / "a.txt").read_text() == "hosted"
    assert not any((tmp_path / "workspace").rglob("a.txt"))
    assert {scope for scope, _ in gateway.calls} == {f"{TENANT}/{thread_key(TENANT, THREAD)}"}


async def test_threads_and_tenants_get_separate_sandboxes(tmp_path: Path, gateway: FakeGateway) -> None:
    settings = _settings(tmp_path, "hosted")
    async with _request(settings):
        await _call("write_file", {"path": "a.txt", "content": "t1"})
    async with _request(settings, thread="acme:t2"):
        assert "t1" not in await _call("read_file", {"path": "a.txt"}, thread="acme:t2")
    async with _request(settings, tenant="globex", thread="acme:t1"):
        assert "t1" not in await _call("read_file", {"path": "a.txt"})
    async with _request(settings), _tenant_scope():
        await _call("write_file", {"path": "shared.txt", "content": "s"})
    assert (gateway.root / TENANT / "shared" / "shared.txt").exists()


@asynccontextmanager
async def _tenant_scope() -> AsyncIterator[None]:
    with bound_scope("tenant"):
        yield


async def test_a_call_with_no_thread_is_refused_before_the_gateway_is_asked(
    tmp_path: Path, gateway: FakeGateway
) -> None:
    async with _request(_settings(tmp_path, "hosted"), thread=None):
        out = await _call("write_file", {"path": "a.txt", "content": "x"}, thread=None)
    assert out.startswith("[tool error/transport_unavailable]")
    assert "belongs to a thread" in out
    assert gateway.calls == []


async def test_the_deployment_scope_stays_on_the_host(tmp_path: Path, gateway: FakeGateway) -> None:
    settings = _settings(tmp_path, "hosted")
    (tmp_path / "workspace" / "README.md").write_text("the checkout")
    async with _request(settings, tenant="default", thread=None):
        with bound_scope("deployment"):
            out = await _call("read_file", {"path": "README.md"}, thread=None)
    assert json.loads(out)["content"] == "the checkout"
    assert gateway.calls == []


# --- the end-of-run checkpoint ------------------------------------------------------------


async def test_a_request_that_wrote_checkpoints_each_scope_it_wrote_once(
    tmp_path: Path, gateway: FakeGateway
) -> None:
    settings = _settings(tmp_path, "hosted")
    async with _request(settings):
        await _call("write_file", {"path": "a.txt", "content": "1"})
        await _call("edit_file", {"path": "a.txt", "old_string": "1", "new_string": "2"})
        assert gateway.checkpoints == [], "not before the request ends"
    assert gateway.checkpoints == [f"{TENANT}/{thread_key(TENANT, THREAD)}"]


async def test_a_request_that_only_read_checkpoints_nothing(tmp_path: Path, gateway: FakeGateway) -> None:
    settings = _settings(tmp_path, "hosted")
    async with _request(settings):
        await _call("list_dir", {"path": "."})
        await _call("read_file", {"path": "missing.txt"})
    assert gateway.checkpoints == []


async def test_a_failed_checkpoint_does_not_fail_the_request(
    tmp_path: Path, gateway: FakeGateway, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings(tmp_path, "hosted")
    async with _request(settings):
        await _call("write_file", {"path": "a.txt", "content": "1"})
        gateway.down = True
    assert "idle backup covers it" in caplog.text


# --- the gateway failing ------------------------------------------------------------------


async def test_an_unreachable_gateway_is_the_workspace_being_unavailable(
    tmp_path: Path, gateway: FakeGateway
) -> None:
    gateway.down = True
    async with _request(_settings(tmp_path, "hosted")):
        out = await _call("read_file", {"path": "a.txt"})
    assert out.startswith(
        "[tool error/transport_unavailable] workspace_root: the hosted workspace is unavailable"
    )


async def test_a_refused_token_says_which_setting(tmp_path: Path, gateway: FakeGateway) -> None:
    settings = _settings(tmp_path, "hosted").model_copy(update={"workspace_gateway_token": "x" * 40})
    async with _request(settings):
        out = await _call("read_file", {"path": "a.txt"})
    assert "FELIX_WORKSPACE_GATEWAY_TOKEN" in out
    assert out.startswith("[tool error/transport_unavailable]")


# --- what needs a directory on the host -----------------------------------------------------


async def test_a_consumer_that_needs_a_host_directory_is_refused_under_hosted(
    tmp_path: Path, gateway: FakeGateway
) -> None:
    from felix.tools.workspace import workspace_root

    settings = _settings(tmp_path, "hosted")
    async with _request(settings):
        with pytest.raises(ValueError, match="needs a directory on the host"):
            workspace_root()
    # The operator's own tenant, in `deployment` scope, still has the host's root.
    async with _request(settings, tenant="default", thread=None):
        with bound_scope("deployment"):
            assert workspace_root() == (tmp_path / "workspace").resolve()


async def test_a_thread_with_a_repository_checkout_is_refused_under_hosted(
    tmp_path: Path, gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    from felix.tools import workspace

    monkeypatch.setattr(
        workspace, "_thread_checkout_of", lambda settings, tenant, thread: tmp_path / "checkout"
    )
    async with _request(_settings(tmp_path, "hosted")):
        out = await _call("read_file", {"path": "a.txt"})
    assert "repository checkout on the host" in out
    assert gateway.calls == []


# --- settings -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        pytest.param({"workspace_gateway_url": ""}, "FELIX_WORKSPACE_GATEWAY_URL", id="no-url"),
        pytest.param({"workspace_gateway_url": "http://gateway.example.com"}, "https", id="plain-http"),
        pytest.param({"workspace_gateway_token": "short"}, "at least 32", id="short-token"),
    ],
)
def test_hosted_without_a_usable_gateway_does_not_start(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    settings = _settings(tmp_path, "hosted").model_copy(update=overrides)
    with pytest.raises(RuntimeError, match=message):
        settings.validate_runtime()


def test_hosted_on_localhost_may_be_plain_http(tmp_path: Path) -> None:
    _settings(tmp_path, "hosted").model_copy(
        update={"workspace_gateway_url": "http://127.0.0.1:8797"}
    ).validate_runtime()


def test_the_default_backend_is_local_and_needs_no_gateway(tmp_path: Path) -> None:
    from felix.tools.workspace_backend import get_workspace_backend
    from felix.tools.workspace_local import LocalBackend

    settings = _settings(tmp_path, "local")
    settings.validate_runtime()
    assert isinstance(get_workspace_backend(settings), LocalBackend)


# --- shell_tools in the sandbox (3b) -------------------------------------------------------

_COMMANDS = ["sh", "ls", "cat", "/no/such/binary"]


async def _shell(
    settings: Settings, argv: list[str], commands: list[str] = _COMMANDS, **args: Any
) -> dict[str, Any] | str:
    from felix.manifests.schema import ShellToolRef
    from felix.tools.shell import tools_from_shell_refs

    (tool,) = tools_from_shell_refs(
        [ShellToolRef(name="run", commands=commands, timeout_ms=1000)], settings=settings
    )
    out = tool_output_content(
        await tool.executor.execute({"argv": argv, **args}, ToolInvocationCtx(thread_id=THREAD))
    )
    try:
        result = json.loads(out)
    except ValueError:
        return out
    result.pop("duration_ms", None)  # the one field a run cannot reproduce
    return result


async def _shell_session(settings: Settings) -> list[Any]:
    out: list[Any] = []
    async with _request(settings):
        await _call("write_file", {"path": "sub/a.txt", "content": "a"})
        out.append(await _shell(settings, ["sh", "-c", "echo out; echo err >&2; exit 3"]))
        out.append(await _shell(settings, ["ls"], cwd="sub"))
        out.append(await _shell(settings, ["cat"], stdin="piped in"))
        out.append(await _shell(settings, ["sh", "-c", "echo made > made.txt"]))
        out.append(await _call("read_file", {"path": "made.txt"}))
        out.append(await _shell(settings, ["sh", "-c", "sleep 10"], cwd="."))
        out.append(await _shell(settings, ["ls"], cwd="../"))
        out.append(await _shell(settings, ["ls"], cwd="missing"))
        out.append(await _shell(settings, ["/no/such/binary"]))
        out.append(await _shell(settings, ["rm", "-rf", "/"]))
    return out


@pytest.mark.parametrize("backend", ["local", "hosted"])
async def test_shell_tools_answer_identically_on_both_backends(
    tmp_path: Path, gateway: FakeGateway, backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from felix.tools import shell

    # A one-second budget keeps the timeout case quick; the same on both backends.
    monkeypatch.setattr(shell, "_DRAIN_AFTER_KILL_S", 1.0)
    allowed = {"shell_allowed_commands": ", ".join(_COMMANDS)}
    transcript = await _shell_session(_settings(tmp_path / backend, backend, **allowed))
    reference = await _shell_session(_settings(tmp_path / "reference", "local", **allowed))
    assert transcript == reference
    first = transcript[0]
    assert isinstance(first, dict) and (first["exit_code"], first["stdout"], first["stderr"]) == (
        3,
        "out\n",
        "err\n",
    )
    assert json.loads(transcript[4])["content"] == "made\n"


async def test_a_hosted_command_runs_in_the_sandbox_and_is_checkpointed(
    tmp_path: Path, gateway: FakeGateway
) -> None:
    settings = _settings(tmp_path, "hosted", shell_allowed_commands=", ".join(_COMMANDS))
    async with _request(settings):
        await _shell(settings, ["sh", "-c", "echo made > made.txt"])
    assert (_scope_dir(tmp_path, "hosted") / "made.txt").read_text() == "made\n"
    assert not any((tmp_path / "workspace").rglob("made.txt"))
    assert ("acme/" + thread_key(TENANT, THREAD), "exec") in gateway.calls
    assert gateway.checkpoints == [f"{TENANT}/{thread_key(TENANT, THREAD)}"]


async def test_a_command_off_the_allowlist_never_reaches_the_sandbox(
    tmp_path: Path, gateway: FakeGateway
) -> None:
    settings = _settings(tmp_path, "hosted", shell_allowed_commands="ls")
    async with _request(settings):
        out = await _shell(settings, ["sh", "-c", "echo hi"], commands=["ls"])
    assert isinstance(out, str) and out.startswith("[shell denied]")
    assert gateway.calls == []


async def test_an_unreachable_gateway_runs_nothing_anywhere(tmp_path: Path, gateway: FakeGateway) -> None:
    gateway.down = True
    settings = _settings(tmp_path, "hosted", shell_allowed_commands=", ".join(_COMMANDS))
    async with _request(settings):
        out = await _shell(settings, ["sh", "-c", "echo made > made.txt"])
    # `tool_error_output` adds no `[tool error/...]` prefix to text that already starts with `[`.
    assert isinstance(out, str) and out.startswith(
        "[shell] workspace_root: the hosted workspace is unavailable"
    )
    assert "the command did not run" in out
    assert not any(tmp_path.rglob("made.txt"))


# --- carrying local scopes into their sandboxes ---------------------------------------------


async def test_upload_copies_each_local_scope_into_its_sandbox_and_backs_it_up(
    tmp_path: Path, gateway: FakeGateway
) -> None:
    from felix.tools.workspace_hosted import upload_local_scopes

    settings = _settings(tmp_path, "hosted")
    scopes = tmp_path / "workspace" / SCOPES_DIR
    shared = scopes / "default" / "shared"
    (shared / "notes").mkdir(parents=True)
    (shared / "README.md").write_text("read me")
    (shared / "notes" / "a.txt").write_text("a")
    os.symlink("/etc/passwd", shared / "passwd")
    (shared / "big.bin").write_bytes(b"x" * 600_000)
    thread = scopes / "acme" / thread_key("acme", "acme:t1")
    thread.mkdir(parents=True)
    (thread / "t.txt").write_text("thread file")
    (scopes / "acme" / "not-a-scope").mkdir()
    (scopes / "acme" / "not-a-scope" / "x.txt").write_text("ignored")

    reports = {r.scope: r for r in await upload_local_scopes(settings)}

    assert set(reports) == {"default/shared", f"acme/{thread_key('acme', 'acme:t1')}"}
    assert reports["default/shared"].uploaded == ["README.md", "notes/a.txt"]
    assert sorted(reports["default/shared"].skipped) == [
        ("big.bin", "over 512000 bytes"),
        ("passwd", "a symlink"),
    ]
    assert (gateway.root / "default" / "shared" / "notes" / "a.txt").read_text() == "a"
    assert not (gateway.root / "default" / "shared" / "passwd").exists()
    assert sorted(gateway.checkpoints) == sorted(reports)
    assert (shared / "README.md").exists(), "the local files stay"


async def test_upload_dry_run_and_tenant_filter(tmp_path: Path, gateway: FakeGateway) -> None:
    from felix.tools.workspace_hosted import upload_local_scopes

    settings = _settings(tmp_path, "hosted")
    for tenant in ("acme", "globex"):
        d = tmp_path / "workspace" / SCOPES_DIR / tenant / "shared"
        d.mkdir(parents=True)
        (d / "f.txt").write_text(tenant)
    dry = await upload_local_scopes(settings, dry_run=True)
    assert [r.uploaded for r in dry] == [["f.txt"], ["f.txt"]]
    assert gateway.calls == []
    only = await upload_local_scopes(settings, tenant="globex")
    assert [r.scope for r in only] == ["globex/shared"]
    assert {scope for scope, _ in gateway.calls} == {"globex/shared"}
