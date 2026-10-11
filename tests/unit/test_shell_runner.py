"""`felix-shell-runner` and the shell tool's remote mode.

The finding this closes: a shell tool's command is a child of the API, so code it runs can read
`/proc/<api pid>/environ`. With `FELIX_SHELL_RUNNER_URL` set the exec moves to a separate
process (a separate container on the builder stack). Three things have to hold for that to mean
anything, and each is pinned here:

* the API still makes every check before anything leaves it, and a runner that is down, broken
  or slow fails the call — it never quietly execs locally instead;
* the runner refuses a caller without the token, and re-checks argv and cwd against its own
  configuration rather than trusting the API's;
* the runner execs through the same hardened core as the local path, so timeouts, output
  bounds and process-group kills are not a second, weaker implementation.

The runner is exercised for real — its ASGI app in-process, real subprocesses — and the API's
client against `tests/support/loopback_http.py` where the test needs a runner that misbehaves.
"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.manifests.schema import ShellToolRef
from felix.shell_runner import create_runner_app, run_until_disconnected
from felix.tools import shell as shell_mod
from felix.tools import shell_background
from felix.tools.errors import ToolErrorCode, read_tool_error_code
from felix.tools.shell import MAX_OUTPUT_BYTES, exec_argv, tools_from_shell_refs
from felix.tools.types import Tool, ToolInvocationCtx, tool_output_content

from tests.support.background_state import isolate_background_state
from tests.support.git_fixture import git
from tests.support.loopback_http import Request, respond, serve

# Written against files at the workspace root; scope selection is test_workspace_scopes.py.
pytestmark = pytest.mark.usefixtures("deployment_workspace_scope")

PY = sys.executable
# A placeholder, long enough for the 32-character floor and obviously not a credential.
TOKEN = "test-runner-token-" + "0" * 24
RESULT_KEYS = {
    "argv",
    "cwd",
    "exit_code",
    "timed_out",
    "output_exceeded",
    "stdout",
    "stderr",
    "truncated",
    "duration_ms",
}
_HEARTBEAT = "import time, pathlib, sys; p = pathlib.Path(sys.argv[1]); [p.write_text(str(time.time())) or time.sleep(0.05) for _ in iter(int, 1)]"


@pytest.fixture(autouse=True)
def _background_state(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Background commands keep their state in a directory of this test's own."""
    isolate_background_state(monkeypatch, tmp_path_factory)


def _settings(ws: Path, allowed: str, *, url: str = "", token: str = TOKEN) -> Settings:
    return Settings(
        workspace_root=str(ws),
        shell_allowed_commands=allowed,
        shell_runner_url=url,
        shell_runner_token=token,
    )


def _tool(settings: Settings, commands: list[str], *, timeout_ms: int | None = None) -> Tool:
    ref = ShellToolRef(name="run", commands=commands, timeout_ms=timeout_ms)
    return tools_from_shell_refs([ref], settings=settings)[0]


async def _call(tool: Tool, settings: Settings, args: dict[str, Any]) -> Any:
    ctx = RequestContext(settings=settings, auth=AuthContext(tenant_id="t"), manifest_id="m", thread_id="th")
    async with async_run_with_context(ctx):
        return await tool.executor.execute(args, ToolInvocationCtx())


def _ok(out: Any) -> dict[str, Any]:
    assert read_tool_error_code(out) is None, tool_output_content(out)
    return json.loads(tool_output_content(out))


def _closed_port_url() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


@pytest.fixture
def no_local_exec(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make the local exec path explode if anything reaches it, and record that it tried."""
    reached: list[str] = []

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        reached.append("exec_argv")
        raise AssertionError("remote mode fell back to a local exec")

    monkeypatch.setattr(shell_mod, "exec_argv", _boom)
    return reached


def _runner(ws: Path, allowed: str, *, token: str = TOKEN) -> httpx.AsyncClient:
    app = create_runner_app(_settings(ws, allowed, token=token))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shell")


@pytest.fixture
def route_to_runner(monkeypatch: pytest.MonkeyPatch):
    """Point the tool's runner client at a real runner app in-process."""

    def _install(ws: Path, allowed: str) -> None:
        app = create_runner_app(_settings(ws, allowed))

        def _client(timeout_s: float) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), timeout=timeout_s)

        monkeypatch.setattr(shell_mod, "_runner_client", _client)

    return _install


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_a_runner_url_without_a_token_is_refused_at_startup(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="FELIX_SHELL_RUNNER_TOKEN is required"):
        _settings(tmp_path, "git status", url="http://shell:8080", token="").validate_runtime()
    with pytest.raises(RuntimeError, match="FELIX_SHELL_RUNNER_TOKEN is required"):
        _settings(tmp_path, "git status", url="http://shell:8080", token="short").validate_runtime()
    with pytest.raises(RuntimeError, match="http"):
        _settings(tmp_path, "git status", url="shell:8080").validate_runtime()
    _settings(tmp_path, "git status", url="http://shell:8080").validate_runtime()
    # Unset is today's behaviour and needs nothing.
    _settings(tmp_path, "git status", token="").validate_runtime()


def test_the_runner_will_not_build_without_a_token(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="FELIX_SHELL_RUNNER_TOKEN"):
        create_runner_app(_settings(tmp_path, "git status", token=""))


def test_the_runner_imports_no_database_cache_or_model_client() -> None:
    """Its container carries none of their settings, and must not need them to start."""
    code = (
        "import sys, felix.shell_runner; "
        "heavy = ('sqlalchemy', 'psycopg', 'redis', 'taskiq', 'anthropic', 'openai'); "
        "print(sorted(m for m in sys.modules if m.split('.')[0] in heavy or m.startswith('felix.db')))"
    )
    done = subprocess.run([PY, "-c", code], capture_output=True, text=True, check=True)
    assert done.stdout.strip() == "[]", done.stdout


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"authorization": "Bearer wrong-" + "0" * 30},
        {"authorization": TOKEN},
        {"authorization": "Basic x"},
    ],
    ids=["missing", "wrong", "no-scheme", "basic"],
)
async def test_the_runner_requires_the_token_and_runs_nothing_without_it(
    tmp_path: Path, headers: dict[str, str]
) -> None:
    marker = tmp_path / "touched"
    async with _runner(tmp_path, "touch") as client:
        resp = await client.post("/run", json={"argv": ["touch", str(marker)]}, headers=headers)
    assert resp.status_code == 401
    assert not marker.exists()


async def test_the_runner_checks_the_token_before_the_body(tmp_path: Path) -> None:
    async with _runner(tmp_path, "touch") as client:
        resp = await client.post("/run", content=b"not json")
    assert resp.status_code == 401


async def test_the_runner_rechecks_argv_against_its_own_allowlist(tmp_path: Path) -> None:
    """Whatever the API allowed, the runner's host allows only what its own operator lists."""
    marker = tmp_path / "touched"
    async with _runner(tmp_path, "git status") as client:
        resp = await client.post(
            "/run", json={"argv": ["touch", str(marker)]}, headers={"authorization": f"Bearer {TOKEN}"}
        )
    assert resp.status_code == 403
    assert resp.json()["reason"] == "argv"
    assert not marker.exists()


async def test_the_runner_with_no_allowlist_runs_nothing(tmp_path: Path) -> None:
    marker = tmp_path / "touched"
    async with _runner(tmp_path, "") as client:
        resp = await client.post(
            "/run", json={"argv": ["touch", str(marker)]}, headers={"authorization": f"Bearer {TOKEN}"}
        )
    assert resp.status_code == 403
    assert not marker.exists()


@pytest.mark.parametrize(("cwd", "why"), [("..", "escapes workspace root"), ("/", "absolute paths")])
async def test_the_runner_confines_cwd_to_its_own_workspace(
    tmp_path: Path, cwd: str, why: str, caplog: pytest.LogCaptureFixture
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    with caplog.at_level("WARNING", logger="felix.shell_runner"):
        async with _runner(ws, "pwd") as client:
            resp = await client.post(
                "/run", json={"argv": ["pwd"], "cwd": cwd}, headers={"authorization": f"Bearer {TOKEN}"}
            )
    assert resp.status_code == 403
    assert resp.json()["reason"] == "cwd"
    # The reply is fixed text (an exception's message never leaves the runner); the reason it
    # was refused is in the runner's own log, where an operator reads it.
    assert why not in resp.json()["message"]
    assert any(why in r.getMessage() for r in caplog.records), [r.getMessage() for r in caplog.records]


async def test_the_runner_kills_a_command_at_its_timeout(tmp_path: Path) -> None:
    async with _runner(tmp_path, PY) as client:
        resp = await client.post(
            "/run",
            json={"argv": [PY, "-c", "import time; time.sleep(30)"], "timeout_ms": 1000},
            headers={"authorization": f"Bearer {TOKEN}"},
        )
    res = resp.json()
    assert resp.status_code == 200
    assert res["timed_out"] is True
    assert res["duration_ms"] < 10_000


async def test_the_runner_bounds_output(tmp_path: Path) -> None:
    async with _runner(tmp_path, PY) as client:
        resp = await client.post(
            "/run",
            json={"argv": [PY, "-c", f"print('x' * {MAX_OUTPUT_BYTES * 3})"]},
            headers={"authorization": f"Bearer {TOKEN}"},
        )
    res = resp.json()
    assert res["truncated"] is True
    assert len(res["stdout"].encode()) <= MAX_OUTPUT_BYTES
    assert set(res) == RESULT_KEYS


async def test_the_runner_reports_a_command_it_cannot_spawn(tmp_path: Path) -> None:
    async with _runner(tmp_path, "./missing.sh") as client:
        resp = await client.post(
            "/run", json={"argv": ["./missing.sh"]}, headers={"authorization": f"Bearer {TOKEN}"}
        )
    assert resp.status_code == 422


async def test_health_needs_no_token(tmp_path: Path) -> None:
    async with _runner(tmp_path, "") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_a_disconnected_caller_takes_its_command_with_it(tmp_path: Path) -> None:
    """`run_until_disconnected` is what the endpoint wraps `exec_argv` in; cancelling it must
    reach the process group, as cancelling the local tool call does."""
    beat = tmp_path / "beat"
    gone = asyncio.Event()

    async def _is_disconnected() -> bool:
        return gone.is_set()

    work = exec_argv([PY, "-c", _HEARTBEAT, str(beat)], cwd=tmp_path, root=tmp_path, stdin=None, timeout_s=60)
    task = asyncio.create_task(run_until_disconnected(work, _is_disconnected))
    for _ in range(100):
        if beat.exists():
            break
        await asyncio.sleep(0.05)
    assert beat.exists(), "the child never started"
    gone.set()
    completed, _ = await asyncio.wait_for(task, timeout=10)
    assert completed is False
    first = beat.read_text()
    await asyncio.sleep(0.4)
    assert beat.read_text() == first, "the command outlived its caller"


# ---------------------------------------------------------------------------
# The tool in remote mode, against a runner that behaves
# ---------------------------------------------------------------------------


async def test_a_call_reaches_the_runner_with_the_bearer_and_the_checked_arguments(
    tmp_path: Path, no_local_exec: list[str]
) -> None:
    (tmp_path / "sub").mkdir()
    seen: list[Request] = []
    answer = {
        "argv": ["ignored"],
        "cwd": "ignored",
        "exit_code": 0,
        "timed_out": False,
        "output_exceeded": False,
        "stdout": "clean\n",
        "stderr": "",
        "truncated": False,
        "duration_ms": 12,
    }

    def _responder(req: Request, writer: asyncio.StreamWriter) -> None:
        seen.append(req)
        respond(writer, json.dumps(answer).encode(), ctype="application/json")

    async with serve(_responder) as url:
        settings = _settings(tmp_path, "git status", url=url)
        tool = _tool(settings, ["git status"], timeout_ms=5000)
        res = _ok(await _call(tool, settings, {"argv": ["git", "status"], "cwd": "sub", "stdin": "in"}))

    assert no_local_exec == []
    (req,) = seen
    assert (req.method, req.path) == ("POST", "/run")
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(req.body) == {
        "argv": ["git", "status"],
        "cwd": "sub",
        "timeout_ms": 5000,
        "stdin": "in",
    }
    # Same shape as a local result, with argv and cwd as this side asked rather than as claimed.
    assert set(res) == RESULT_KEYS
    assert res["argv"] == ["git", "status"]
    assert res["cwd"] == "sub"
    assert res["stdout"] == "clean\n"


async def test_the_api_allowlist_refuses_before_any_request(tmp_path: Path, no_local_exec: list[str]) -> None:
    marker = tmp_path / "touched"
    seen: list[Request] = []

    def _responder(req: Request, writer: asyncio.StreamWriter) -> None:
        seen.append(req)
        respond(writer, b"{}", ctype="application/json")

    async with serve(_responder) as url:
        settings = _settings(tmp_path, "git status", url=url)
        tool = _tool(settings, ["git status"])
        out = await _call(tool, settings, {"argv": ["touch", str(marker)]})
        cwd_out = await _call(tool, settings, {"argv": ["git", "status"], "cwd": ".."})

    assert read_tool_error_code(out) == ToolErrorCode.PERMISSION_DENIED
    assert read_tool_error_code(cwd_out) == ToolErrorCode.PERMISSION_DENIED
    assert seen == [], "a refused call must not reach the runner"
    assert no_local_exec == []
    assert not marker.exists()


async def test_a_runner_refusal_is_a_permission_denied(tmp_path: Path, no_local_exec: list[str]) -> None:
    def _responder(req: Request, writer: asyncio.StreamWriter) -> None:
        body = {"reason": "argv", "message": "not under any prefix in FELIX_SHELL_ALLOWED_COMMANDS"}
        respond(writer, json.dumps(body).encode(), status="403 Forbidden", ctype="application/json")

    async with serve(_responder) as url:
        settings = _settings(tmp_path, "git status", url=url)
        out = await _call(_tool(settings, ["git status"]), settings, {"argv": ["git", "status"]})

    assert read_tool_error_code(out) == ToolErrorCode.PERMISSION_DENIED
    assert "runner refused" in tool_output_content(out)
    assert no_local_exec == []


# ---------------------------------------------------------------------------
# ...and against one that does not: every failure fails closed, nothing runs locally
# ---------------------------------------------------------------------------


async def test_a_runner_that_is_down_fails_closed(tmp_path: Path, no_local_exec: list[str]) -> None:
    marker = tmp_path / "touched"
    settings = _settings(tmp_path, "touch", url=_closed_port_url())
    out = await _call(_tool(settings, ["touch"]), settings, {"argv": ["touch", str(marker)]})
    text = tool_output_content(out)
    assert read_tool_error_code(out) == ToolErrorCode.TRANSPORT_UNAVAILABLE, text
    assert "did not run" in text
    assert TOKEN not in text
    assert no_local_exec == []
    assert not marker.exists()


@pytest.mark.parametrize(
    ("status", "body"),
    [
        ("500 Internal Server Error", b"boom"),
        ("200 OK", b'{"exit_code": 0}'),
        ("200 OK", b"not json"),
        ("401 Unauthorized", b'{"message": "unauthorized"}'),
        ("302 Found", b""),
    ],
    ids=["500", "malformed-result", "not-json", "401", "redirect"],
)
async def test_a_runner_that_errors_fails_closed(
    tmp_path: Path, no_local_exec: list[str], status: str, body: bytes
) -> None:
    def _responder(req: Request, writer: asyncio.StreamWriter) -> None:
        respond(
            writer, body, status=status, ctype="application/json", extra="location: http://elsewhere/\r\n"
        )

    marker = tmp_path / "touched"
    async with serve(_responder) as url:
        settings = _settings(tmp_path, "touch", url=url)
        out = await _call(_tool(settings, ["touch"]), settings, {"argv": ["touch", str(marker)]})
    text = tool_output_content(out)
    assert read_tool_error_code(out) == ToolErrorCode.TRANSPORT_UNAVAILABLE, text
    assert TOKEN not in text
    assert no_local_exec == []
    assert not marker.exists()


async def test_a_runner_that_hangs_fails_closed(
    tmp_path: Path, no_local_exec: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "touched"
    monkeypatch.setattr(shell_mod, "RUNNER_GRACE_S", 0.5)
    release = asyncio.Event()

    async def _responder(req: Request, writer: asyncio.StreamWriter) -> None:
        await release.wait()

    async with serve(_responder) as url:
        settings = _settings(tmp_path, "touch", url=url)
        started = time.monotonic()
        out = await _call(
            _tool(settings, ["touch"], timeout_ms=500), settings, {"argv": ["touch", str(marker)]}
        )
        elapsed = time.monotonic() - started
        release.set()
    assert read_tool_error_code(out) == ToolErrorCode.TRANSPORT_UNAVAILABLE, tool_output_content(out)
    assert "did not answer in time" in tool_output_content(out)
    assert elapsed < 10
    assert no_local_exec == []
    assert not marker.exists()


async def test_a_runner_that_trickles_fails_closed_at_the_deadline(
    tmp_path: Path, no_local_exec: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A byte every 0.2s never trips httpx's per-read timeout; the call's own deadline must."""
    marker = tmp_path / "touched"
    monkeypatch.setattr(shell_mod, "RUNNER_GRACE_S", 0.5)

    async def _responder(req: Request, writer: asyncio.StreamWriter) -> None:
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: 100000\r\n\r\n")
        for _ in range(40):  # 8s of trickle, far past the 1s deadline
            writer.write(b" ")
            await writer.drain()
            await asyncio.sleep(0.2)

    async with serve(_responder) as url:
        settings = _settings(tmp_path, "touch", url=url)
        started = time.monotonic()
        out = await _call(
            _tool(settings, ["touch"], timeout_ms=500), settings, {"argv": ["touch", str(marker)]}
        )
        elapsed = time.monotonic() - started
    assert read_tool_error_code(out) == ToolErrorCode.TRANSPORT_UNAVAILABLE, tool_output_content(out)
    assert "did not answer in time" in tool_output_content(out)
    assert elapsed < 4, elapsed
    assert no_local_exec == []
    assert not marker.exists()


@pytest.mark.parametrize(
    "field",
    [
        {"exit_code": 10**40},
        {"exit_code": -(10**40)},
        {"exit_code": 256},
        {"duration_ms": 10**40},
        {"duration_ms": -1},
    ],
    ids=["huge-exit", "huge-negative-exit", "exit-256", "huge-duration", "negative-duration"],
)
async def test_an_out_of_range_number_from_the_runner_fails_closed(
    tmp_path: Path, no_local_exec: list[str], field: dict[str, int]
) -> None:
    marker = tmp_path / "touched"
    result = {
        "argv": ["touch", str(marker)],
        "cwd": ".",
        "exit_code": 0,
        "timed_out": False,
        "output_exceeded": False,
        "stdout": "",
        "stderr": "",
        "truncated": False,
        "duration_ms": 5,
    }

    def _responder(body: dict[str, Any]) -> Any:
        def _send(req: Request, writer: asyncio.StreamWriter) -> None:
            respond(writer, json.dumps(body).encode(), ctype="application/json")

        return _send

    async with serve(_responder(result)) as url:
        settings = _settings(tmp_path, "touch", url=url)
        ok = _ok(await _call(_tool(settings, ["touch"]), settings, {"argv": ["touch", str(marker)]}))
        assert ok["exit_code"] == 0
    async with serve(_responder({**result, **field})) as url:
        settings = _settings(tmp_path, "touch", url=url)
        out = await _call(_tool(settings, ["touch"]), settings, {"argv": ["touch", str(marker)]})
    assert read_tool_error_code(out) == ToolErrorCode.TRANSPORT_UNAVAILABLE, tool_output_content(out)
    assert "malformed result" in tool_output_content(out)
    assert not marker.exists()


async def test_an_oversized_runner_response_fails_closed(
    tmp_path: Path, no_local_exec: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "touched"
    monkeypatch.setattr(shell_mod, "MAX_RUNNER_RESPONSE_BYTES", 1024)

    def _responder(req: Request, writer: asyncio.StreamWriter) -> None:
        respond(writer, b"x" * 4096, ctype="application/json")

    async with serve(_responder) as url:
        settings = _settings(tmp_path, "touch", url=url)
        out = await _call(_tool(settings, ["touch"]), settings, {"argv": ["touch", str(marker)]})
    assert read_tool_error_code(out) == ToolErrorCode.TRANSPORT_UNAVAILABLE, tool_output_content(out)
    assert no_local_exec == []
    assert not marker.exists()


# ---------------------------------------------------------------------------
# End to end: the tool, the real runner app, a real command
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    git(ws, "init", "-q")
    (ws / "new.txt").write_text("hello\n")
    return ws


async def test_git_status_runs_through_the_real_runner(repo: Path, route_to_runner: Any) -> None:
    route_to_runner(repo, "git status")
    remote = _settings(repo, "git status", url="http://shell:8080")
    remote_res = _ok(
        await _call(_tool(remote, ["git status"]), remote, {"argv": ["git", "status", "--short"]})
    )

    local = _settings(repo, "git status")
    local_res = _ok(await _call(_tool(local, ["git status"]), local, {"argv": ["git", "status", "--short"]}))

    assert remote_res["exit_code"] == 0, remote_res["stderr"]
    assert remote_res["stdout"] == "?? new.txt\n"
    # The same command answers the same thing, in the same shape, either way it ran.
    assert set(remote_res) == set(local_res) == RESULT_KEYS
    for key in ("argv", "cwd", "exit_code", "stdout", "stderr", "timed_out", "truncated"):
        assert remote_res[key] == local_res[key], key


async def test_the_real_runner_refuses_what_its_operator_does_not_list(
    repo: Path, route_to_runner: Any
) -> None:
    """The API allows `git status` and `touch`; the runner's host allows only `git status`."""
    route_to_runner(repo, "git status")
    marker = repo / "touched"
    settings = _settings(repo, "git status, touch", url="http://shell:8080")
    out = await _call(_tool(settings, ["touch"]), settings, {"argv": ["touch", str(marker)]})
    assert read_tool_error_code(out) == ToolErrorCode.PERMISSION_DENIED, tool_output_content(out)
    assert not marker.exists()


# ---------------------------------------------------------------------------
# Background commands on the runner
# ---------------------------------------------------------------------------

BEARER = {"authorization": f"Bearer {TOKEN}"}


@pytest.mark.parametrize("path", ["/start", "/output", "/kill"])
async def test_every_background_endpoint_requires_the_token(tmp_path: Path, path: str) -> None:
    marker = tmp_path / "touched"
    body = {"argv": ["touch", str(marker)]} if path == "/start" else {"id": "0" * 32}
    async with _runner(tmp_path, "touch") as client:
        resp = await client.post(path, json=body)
    assert resp.status_code == 401
    await asyncio.sleep(0.3)
    assert not marker.exists()


async def test_a_background_start_rechecks_argv_against_the_runners_allowlist(tmp_path: Path) -> None:
    marker = tmp_path / "touched"
    async with _runner(tmp_path, "git status") as client:
        resp = await client.post("/start", json={"argv": ["touch", str(marker)]}, headers=BEARER)
    assert resp.status_code == 403 and resp.json()["reason"] == "argv"
    await asyncio.sleep(0.3)
    assert not marker.exists()


async def test_a_background_id_is_found_only_in_the_scope_that_made_it(tmp_path: Path) -> None:
    (tmp_path / ".felix-scopes" / "acme" / "shared").mkdir(parents=True)
    async with _runner(tmp_path, "sleep") as client:
        started = await client.post(
            "/start", json={"argv": ["sleep", "30"], "scope": ".felix-scopes/acme/shared"}, headers=BEARER
        )
        process_id = started.json()["id"]
        elsewhere = await client.post("/kill", json={"id": process_id}, headers=BEARER)
        ended = await client.post(
            "/kill", json={"id": process_id, "scope": ".felix-scopes/acme/shared"}, headers=BEARER
        )
    assert started.status_code == 200
    assert elsewhere.status_code == 404 and elsewhere.json() == {"message": "no such background command here"}
    assert ended.json()["status"] == "killed"


async def test_background_tools_run_on_the_runner_not_here(tmp_path: Path, route_to_runner: Any) -> None:
    api_ws, runner_ws = tmp_path / "api", tmp_path / "runner"
    api_ws.mkdir()
    runner_ws.mkdir()
    route_to_runner(runner_ws, "pwd, sleep")
    settings = _settings(api_ws, "pwd, sleep", url="http://shell")
    ref = ShellToolRef(name="run", commands=["pwd", "sleep"], timeout_ms=5000, background=True)
    run, output, kill = tools_from_shell_refs([ref], settings=settings)
    started = _ok(await _call(run, settings, {"argv": ["pwd"], "run_in_background": True}))
    assert started["argv"] == ["pwd"]
    for _ in range(20):
        read = _ok(await _call(output, settings, {"id": started["id"], "wait_ms": 500}))
        if read["status"] != "running":
            break
    assert (read["status"], read["stdout"]) == ("exited", f"{runner_ws.resolve()}\n")
    sleeper = _ok(await _call(run, settings, {"argv": ["sleep", "30"], "run_in_background": True}))
    assert _ok(await _call(kill, settings, {"id": sleeper["id"]}))["status"] == "killed"
    assert not shell_background.background_base(api_ws.resolve()).exists() or not any(
        shell_background.background_base(api_ws.resolve()).iterdir()
    ), "nothing was started on the API's side"


async def test_a_malformed_background_answer_is_closed(tmp_path: Path) -> None:
    answers = iter([b'{"id": "not-an-id", "status": "running"}', b'{"processes": "everything"}'])

    def _responder(req: Request, writer: asyncio.StreamWriter) -> None:
        respond(writer, next(answers), ctype="application/json")

    async with serve(_responder) as url:
        settings = _settings(tmp_path, "sleep", url=url)
        ref = ShellToolRef(name="run", commands=["sleep"], background=True)
        run, output, _ = tools_from_shell_refs([ref], settings=settings)
        started = await _call(run, settings, {"argv": ["sleep", "1"], "run_in_background": True})
        listed = await _call(output, settings, {})
    for out in (started, listed):
        assert tool_output_content(out).startswith(
            "[shell] the answer about the background command was malformed"
        )


async def test_a_thread_checkout_never_reaches_the_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner has no thread's checkout: every call there -- a command, a background start, a
    read, an end -- would act on the shared workspace while the model believes it is in its repo."""
    shared, checkout = tmp_path / "shared", tmp_path / "checkout"
    shared.mkdir()
    checkout.mkdir()
    monkeypatch.setattr(shell_mod, "workspace_root", lambda: checkout.resolve())
    seen: list[Request] = []

    def _responder(req: Request, writer: asyncio.StreamWriter) -> None:
        seen.append(req)
        respond(writer, b"{}", ctype="application/json")

    async with serve(_responder) as url:
        settings = _settings(shared, "ls", url=url)
        run, output, kill = tools_from_shell_refs(
            [ShellToolRef(name="run", commands=["ls"], background=True)], settings=settings
        )
        answers = [
            await _call(run, settings, {"argv": ["ls"]}),
            await _call(run, settings, {"argv": ["ls"], "run_in_background": True}),
            await _call(output, settings, {}),
            await _call(kill, settings, {"id": "0" * 32}),
        ]
    assert seen == []
    for out in answers:
        assert read_tool_error_code(out) is ToolErrorCode.TRANSPORT_UNAVAILABLE
        assert "its own repository checkout" in tool_output_content(out)


async def test_a_started_commands_argv_is_what_was_asked_not_what_the_runner_says(tmp_path: Path) -> None:
    claimed = {
        "id": "a" * 32, "argv": ["something", "else"], "cwd": ".", "status": "running",
        "exit_code": None, "error": None, "truncated": False, "duration_ms": 1,
    }  # fmt: skip

    def _responder(req: Request, writer: asyncio.StreamWriter) -> None:
        respond(writer, json.dumps(claimed).encode(), ctype="application/json")

    async with serve(_responder) as url:
        settings = _settings(tmp_path, "sleep", url=url)
        (run, *_) = tools_from_shell_refs(
            [ShellToolRef(name="run", commands=["sleep"], background=True)], settings=settings
        )
        started = _ok(await _call(run, settings, {"argv": ["sleep", "1"], "run_in_background": True}))
    assert started["argv"] == ["sleep", "1"] and started["id"] == "a" * 32


async def test_a_runner_start_hands_the_command_what_the_call_gave(
    tmp_path: Path, route_to_runner: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stdin, cwd and the tool's timeout reach the command on the runner, and the API's own
    environment does not."""
    monkeypatch.setenv("FELIX_SECRET_FOR_TEST", "leaked")
    (tmp_path / "sub").mkdir()
    route_to_runner(tmp_path, "sh")
    settings = _settings(tmp_path, "sh", url="http://shell")

    async def _done(timeout_ms: int, argv: list[str], **args: Any) -> dict[str, Any]:
        ref = ShellToolRef(name="run", commands=["sh"], timeout_ms=timeout_ms, background=True)
        run, output, _ = tools_from_shell_refs([ref], settings=settings)
        started = _ok(await _call(run, settings, {"argv": argv, "run_in_background": True, **args}))
        out = ""
        for _ in range(40):
            read = _ok(await _call(output, settings, {"id": started["id"], "wait_ms": 500}))
            out += read["stdout"]
            if read["status"] != "running":
                return {**read, "stdout": out}
        raise AssertionError("never finished")

    given = await _done(
        10_000,
        ["sh", "-c", 'cat; echo "[${FELIX_SECRET_FOR_TEST:-clean}]"; basename "$PWD"'],
        cwd="sub",
        stdin="piped\n",
    )
    assert (given["status"], given["stdout"]) == ("exited", "piped\n[clean]\nsub\n")
    assert (await _done(500, ["sh", "-c", "sleep 30"]))["status"] == "timed_out"


async def test_the_runner_counts_each_tenants_share_by_its_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shell_background, "MAX_RUNNING_OWNER", 1)
    for scope in ("acme/shared", "acme/" + "1" * 40, "globex/shared"):
        (tmp_path / ".felix-scopes" / scope).mkdir(parents=True)
    async with _runner(tmp_path, "sleep") as client:

        async def start(scope: str) -> httpx.Response:
            body = {"argv": ["sleep", "30"], "scope": f".felix-scopes/{scope}"}
            return await client.post("/start", json=body, headers=BEARER)

        first = await start("acme/shared")
        second = await start("acme/" + "1" * 40)
        other = await start("globex/shared")
        for answer in (first, other):
            await client.post(
                "/kill",
                json={
                    "id": answer.json()["id"],
                    "scope": answer.request.content and json.loads(answer.request.content)["scope"],
                },
                headers=BEARER,
            )
    assert first.status_code == 200 and other.status_code == 200
    assert second.status_code == 403 and second.json()["reason"] == "busy"
