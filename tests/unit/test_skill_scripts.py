"""`spec.skill_scripts` -- `run_skill_script` runs an active skill's own scripts, and nothing else.

Against real subprocesses and the in-memory session store: what is under test is the line between
a model's arguments and the host, as in `test_shell_tool.py`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.manifests import builder
from felix.manifests.governance import GovernanceError, validate_for_write
from felix.manifests.loader import parse_manifest
from felix.manifests.schema import CommandScreening, SkillScriptsSpec
from felix.security.shell_policy import SKILL_SCRIPTS_DIR, skill_script_argv_issue
from felix.session.store import InMemorySessionStore
from felix.shell_runner import create_runner_app
from felix.skills.scripts import make_skill_script_tool
from felix.skills.tools import make_skill_tools
from felix.skills.types import Skill, SkillCatalog
from felix.tools import shell as shell_mod
from felix.tools.errors import ToolErrorCode, read_tool_error_code
from felix.tools.types import Tool, ToolInvocationCtx, is_wrapper_deny, tool_output_content

from tests.support.skill_threads import call_in_thread

pytestmark = pytest.mark.usefixtures("deployment_workspace_scope")

PY = sys.executable
TOKEN = "t" * 40
SH = """\
echo "args:$*"
echo "cwd:$(pwd)"
. "$(dirname "$0")/lib.sh"
"""


def _skill_dir(root: Path) -> Path:
    skill = root / "skills" / "dig"
    (skill / "scripts").mkdir(parents=True)
    (skill / "references").mkdir()
    (skill / "SKILL.md").write_text("---\nname: dig\ndescription: Dig.\n---\nRun scripts/run.sh.\n")
    (skill / "scripts" / "run.sh").write_text(SH)
    (skill / "scripts" / "lib.sh").write_text('echo "sibling:loaded"\n')
    (skill / "scripts" / "nap.sh").write_text("sleep 30\n")
    (skill / "scripts" / "run.rb").write_text("puts 1\n")
    (skill / "scripts" / "hello.py").write_text("import sys; print('py', *sys.argv[1:])\n")
    (skill / "references" / "x.sh").write_text("echo nope\n")
    return skill


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    return root


@pytest.fixture
def skill_dir(tmp_path: Path) -> Path:
    return _skill_dir(tmp_path)


def _catalog(skill_dir: Path, **kw: Any) -> SkillCatalog:
    skill = Skill(
        name="dig",
        description="Dig.",
        body="Run scripts/run.sh.",
        source="bundled",
        path=str(skill_dir / "SKILL.md"),
        **kw,
    )
    return SkillCatalog(skills={"dig": skill})


def _settings(ws: Path, interpreters: str = "bash", **kw: Any) -> Settings:
    return Settings(workspace_root=str(ws), skill_script_interpreters=interpreters, **kw)


def _spec(**kw: Any) -> SkillScriptsSpec:
    return SkillScriptsSpec(enabled=True, interpreters={".sh": "bash", ".rb": "ruby", ".py": PY}, **kw)


class _Thread:
    """A conversation: the skill tools and `run_skill_script` over one session store."""

    def __init__(
        self, catalog: SkillCatalog, settings: Settings, spec: SkillScriptsSpec | None = None
    ) -> None:
        self.sessions = InMemorySessionStore(tenant_id="t")
        self.settings = settings
        self.skills = {
            t.name: t
            for t in make_skill_tools(catalog, session_store=self.sessions, tenant_id="t", manifest_id="m")
        }
        self.tool = make_skill_script_tool(
            catalog,
            spec or _spec(),
            session_store=self.sessions,
            tenant_id="t",
            manifest_id="m",
            settings=settings,
        )

    async def activate(self, thread: str = "t:a") -> None:
        await call_in_thread(
            self.skills["activate_skill"], {"name": "dig"}, session_store=self.sessions, thread_id=thread
        )

    async def run(
        self, script: str, args: list[str] | None = None, thread: str = "t:a", tool: Tool | None = None
    ) -> Any:
        ctx = RequestContext(
            settings=self.settings, auth=AuthContext(tenant_id="t"), manifest_id="m", thread_id=thread
        )
        async with async_run_with_context(ctx):
            return await (tool or self.tool).executor.execute(
                {"name": "dig", "script": script, "args": args or []},
                ToolInvocationCtx(thread_id=thread, tool_call_id="r1"),
            )


def _ok(out: Any) -> dict[str, Any]:
    assert read_tool_error_code(out) is None, tool_output_content(out)
    return json.loads(tool_output_content(out))


def _error(out: Any) -> str:
    return json.loads(tool_output_content(out))["error"]


def _denied(out: Any) -> str:
    assert read_tool_error_code(out) == ToolErrorCode.PERMISSION_DENIED, tool_output_content(out)
    return tool_output_content(out)


# -- the run ---------------------------------------------------------------------------------------


async def test_a_script_runs_from_the_workspace_root_beside_its_own_files(ws: Path, skill_dir: Path) -> None:
    thread = _Thread(_catalog(skill_dir), _settings(ws))
    await thread.activate()
    result = _ok(await thread.run("scripts/run.sh", ["a b", "--flag", "; rm -rf /"]))

    assert result["exit_code"] == 0, result
    assert "args:a b --flag ; rm -rf /" in result["stdout"], "arguments are the script's, verbatim"
    assert f"cwd:{ws.resolve()}" in result["stdout"]
    assert "sibling:loaded" in result["stdout"], "the bundle is written beside the script"


async def test_the_written_bundle_is_gone_after_the_call_and_git_never_sees_it(
    ws: Path, skill_dir: Path
) -> None:
    thread = _Thread(_catalog(skill_dir), _settings(ws))
    await thread.activate()
    _ok(await thread.run("scripts/run.sh"))
    assert list((ws / SKILL_SCRIPTS_DIR / "dig").iterdir()) == []
    assert (ws / SKILL_SCRIPTS_DIR / ".gitignore").read_text() == "*\n"


async def test_a_python_script_runs_under_its_interpreter(ws: Path, skill_dir: Path) -> None:
    thread = _Thread(_catalog(skill_dir), _settings(ws, interpreters=f"bash,{PY}"))
    await thread.activate()
    assert _ok(await thread.run("scripts/hello.py", ["x"]))["stdout"].strip() == "py x"


async def test_a_script_is_killed_at_its_timeout(ws: Path, skill_dir: Path) -> None:
    thread = _Thread(_catalog(skill_dir), _settings(ws), _spec(timeout_ms=300))
    await thread.activate()
    assert _ok(await thread.run("scripts/nap.sh"))["timed_out"] is True


# -- what it refuses ----------------------------------------------------------------------------------


async def test_a_skill_not_active_here_runs_nothing(ws: Path, skill_dir: Path) -> None:
    """Active in another conversation is not active in this one."""
    thread = _Thread(_catalog(skill_dir), _settings(ws))
    assert _error(await thread.run("scripts/run.sh")) == "not_active"
    await thread.activate(thread="t:other")
    assert _error(await thread.run("scripts/run.sh")) == "not_active"
    assert not (ws / SKILL_SCRIPTS_DIR).exists(), "nothing was written"


async def test_an_imported_skill_nobody_adopted_runs_nothing(ws: Path, skill_dir: Path) -> None:
    thread = _Thread(_catalog(skill_dir, untrusted=True), _settings(ws))
    await thread.activate()
    assert _error(await thread.run("scripts/run.sh")) == "script_refused"


@pytest.mark.parametrize(
    ("script", "error"),
    [
        ("references/x.sh", "invalid_path"),
        ("scripts/../references/x.sh", "invalid_path"),
        ("/etc/passwd", "invalid_path"),
        ("scripts/missing.sh", "file_not_found"),
    ],
    ids=["outside-scripts", "dotdot", "absolute", "missing"],
)
async def test_only_a_script_of_the_bundle_runs(ws: Path, skill_dir: Path, script: str, error: str) -> None:
    thread = _Thread(_catalog(skill_dir), _settings(ws))
    await thread.activate()
    assert _error(await thread.run(script)) == error


async def test_an_extension_the_manifest_maps_to_nothing_runs_nothing(ws: Path, skill_dir: Path) -> None:
    thread = _Thread(
        _catalog(skill_dir), _settings(ws), SkillScriptsSpec(enabled=True, interpreters={".py": "python3"})
    )
    await thread.activate()
    assert "no interpreter" in _denied(await thread.run("scripts/run.sh"))


async def test_an_interpreter_the_operator_does_not_list_runs_nothing(ws: Path, skill_dir: Path) -> None:
    """`.rb` maps to `ruby` in the manifest; the host lists only `bash`. Read per call."""
    thread = _Thread(_catalog(skill_dir), _settings(ws))
    await thread.activate()
    assert "FELIX_SKILL_SCRIPT_INTERPRETERS" in _denied(await thread.run("scripts/run.rb"))
    thread.settings = _settings(ws, interpreters="")
    assert "FELIX_SKILL_SCRIPT_INTERPRETERS" in _denied(await thread.run("scripts/run.sh"))


async def test_off_a_development_box_without_a_runner_nothing_runs(ws: Path, skill_dir: Path) -> None:
    thread = _Thread(_catalog(skill_dir), _settings(ws, environment="production", auth_mode="api_key"))
    await thread.activate()
    assert "shell runner" in _denied(await thread.run("scripts/run.sh"))
    assert not (ws / SKILL_SCRIPTS_DIR).exists()


async def test_a_symlinked_scripts_directory_is_not_followed(
    ws: Path, skill_dir: Path, tmp_path: Path
) -> None:
    """A shell tool could leave `.felix-skills` pointing anywhere; nothing is written through it."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (ws / SKILL_SCRIPTS_DIR).symlink_to(elsewhere)
    thread = _Thread(_catalog(skill_dir), _settings(ws))
    await thread.activate()
    assert _error(await thread.run("scripts/run.sh")) == "workspace_unavailable"
    assert list(elsewhere.iterdir()) == []


def test_the_file_tools_may_not_write_where_scripts_are_written() -> None:
    """Else a model could put its own file beside a skill's script and have it run as the skill's."""
    from felix.tools.workspace import writable_file

    with pytest.raises(ValueError, match="reserved path"):
        writable_file(f"{SKILL_SCRIPTS_DIR}/dig/scripts/run.sh")


# -- governance -------------------------------------------------------------------------------------


def test_the_tool_is_an_execution_transport(ws: Path, skill_dir: Path) -> None:
    """So command screening reads every argument, and content screening its output."""
    tool = _Thread(_catalog(skill_dir), _settings(ws)).tool
    assert tool.executor.transport == "shell"
    assert tool.executor.transport in builder._EXECUTION_TRANSPORTS
    assert tool.executor.transport not in builder._TRUSTED_TRANSPORTS


async def test_a_command_screening_rule_sees_the_script_s_arguments(ws: Path, skill_dir: Path) -> None:
    thread = _Thread(_catalog(skill_dir), _settings(ws))
    await thread.activate()
    screening = CommandScreening(enabled=True, rules=[{"pattern": "drop table", "decision": "deny"}])
    [screened] = builder.apply_command_screening([thread.tool], screening, "m")
    assert is_wrapper_deny(await thread.run("scripts/run.sh", ["DROP TABLE users"], tool=screened))


def _manifest(**spec: Any) -> Any:
    base = {"pattern": "react", "skills": [{"name": "dig"}], "skill_scripts": {"enabled": True}, **spec}
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "m"}, "spec": base}
    )


@pytest.mark.parametrize(
    ("interpreters", "match"),
    [("", "disabled"), ("python3", "'bash' is not in")],
    ids=["no-operator-list", "interpreter-outside-it"],
)
def test_a_manifest_the_host_would_refuse_is_refused_at_write(
    ws: Path, interpreters: str, match: str
) -> None:
    with pytest.raises(GovernanceError, match=match):
        validate_for_write(_manifest(), _settings(ws, interpreters=interpreters))


def test_a_manifest_without_skills_cannot_ask_for_their_scripts(ws: Path) -> None:
    with pytest.raises(GovernanceError, match="no skills"):
        validate_for_write(_manifest(skills=[]), _settings(ws, interpreters="python3,bash,node"))


def test_skill_scripts_may_not_be_reached_anonymously_outside_development(ws: Path) -> None:
    settings = _settings(
        ws,
        interpreters="python3,bash,node",
        environment="production",
        shell_runner_url="http://shell:8080",
        shell_runner_token=TOKEN,
    )
    with pytest.raises(GovernanceError, match="authenticated caller"):
        validate_for_write(_manifest(auth={"inbound": {"allow_anonymous": True}}), settings)


def test_an_interpreter_must_be_one_word() -> None:
    with pytest.raises(ValueError, match="one word"):
        SkillScriptsSpec(interpreters={".py": "python3 -c"})
    with pytest.raises(ValueError, match="extension"):
        SkillScriptsSpec(interpreters={"py": "python3"})


@pytest.mark.parametrize(
    ("env", "match"),
    [
        ({"skill_script_interpreters": "python3,bash=x"}, "one word"),
        ({"skill_script_interpreters": "python3", "environment": "production"}, "shell runner"),
    ],
    ids=["malformed", "no-isolation"],
)
def test_the_process_refuses_to_boot_on_an_unsafe_interpreter_list(env: dict[str, str], match: str) -> None:
    with pytest.raises(RuntimeError, match=match):
        Settings(**env).validate_runtime()  # type: ignore[arg-type]


# -- the shell runner -----------------------------------------------------------------------------------


def _runner_settings(ws: Path, interpreters: str = "bash", allowed: str = "") -> Settings:
    return Settings(
        workspace_root=str(ws),
        shell_allowed_commands=allowed,
        skill_script_interpreters=interpreters,
        shell_runner_url="http://shell:8080",
        shell_runner_token=TOKEN,
        environment="production",
        auth_mode="api_key",
    )


async def test_through_the_runner_the_script_runs_there(
    ws: Path, skill_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_runner_app(_runner_settings(ws))
    seen: list[dict[str, Any]] = []

    async def _spy(request: httpx.Request) -> None:
        seen.append(json.loads(request.content))

    def _client(timeout_s: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), timeout=timeout_s, event_hooks={"request": [_spy]}
        )

    async def _no_local(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a runner call fell back to a local exec")

    monkeypatch.setattr(shell_mod, "_runner_client", _client)
    monkeypatch.setattr(shell_mod, "exec_argv", _no_local)
    thread = _Thread(_catalog(skill_dir), _runner_settings(ws))
    await thread.activate()
    result = _ok(await thread.run("scripts/run.sh", ["x"]))
    assert "args:x" in result["stdout"] and "sibling:loaded" in result["stdout"]
    assert seen[0]["kind"] == "skill_script" and seen[0]["cwd"] == "."


async def _post(ws: Path, body: dict[str, Any], *, path: str = "/run", **kw: Any) -> httpx.Response:
    app = create_runner_app(_runner_settings(ws, **kw))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://shell") as client:
        return await client.post(path, json=body, headers={"authorization": f"Bearer {TOKEN}"})


def _written(ws: Path) -> str:
    """A script where `run_skill_script` writes one, as the runner would find it."""
    rel = f"{SKILL_SCRIPTS_DIR}/dig/{'0' * 32}/scripts/ok.sh"
    (ws / rel).parent.mkdir(parents=True)
    (ws / rel).write_text("echo ran\n")
    return rel


async def test_the_runner_admits_a_written_script_under_its_own_interpreter(ws: Path) -> None:
    rel = _written(ws)
    response = await _post(ws, {"kind": "skill_script", "argv": ["bash", rel], "cwd": "."})
    assert response.status_code == 200 and response.json()["stdout"] == "ran\n"


@pytest.mark.parametrize(
    "body",
    [
        {"argv": ["python3", "{rel}"]},  # an interpreter the runner does not list
        {"argv": ["bash", "-c", "echo pwned"]},  # an option, not a written script
        {"argv": ["bash", "scripts/ok.sh"]},  # not where run_skill_script writes
        {"argv": ["bash", ".felix-skills/dig/" + "0" * 32 + "/scripts/../../x.sh"]},
        {"argv": ["bash", "{rel}"], "cwd": ".felix-skills"},  # a script runs from the root
    ],
    ids=["interpreter", "option", "elsewhere", "dotdot", "cwd"],
)
async def test_the_runner_refuses_anything_else_as_a_skill_script(ws: Path, body: dict[str, Any]) -> None:
    rel = _written(ws)
    argv = [a.replace("{rel}", rel) for a in body["argv"]]
    response = await _post(ws, {"kind": "skill_script", "cwd": ".", **body, "argv": argv})
    assert response.status_code == 403, response.text


async def test_the_interpreter_list_does_not_widen_the_shell_allowlist(ws: Path) -> None:
    """Listing `bash` for scripts must not hand a shell tool `bash -c`."""
    response = await _post(ws, {"argv": ["bash", "-c", "echo pwned"], "cwd": "."}, allowed="git status")
    assert response.status_code == 403


async def test_a_skill_script_does_not_start_in_the_background(ws: Path) -> None:
    rel = _written(ws)
    response = await _post(ws, {"kind": "skill_script", "argv": ["bash", rel], "cwd": "."}, path="/start")
    assert response.status_code == 403


def test_the_argv_shape_is_held_to_what_the_tool_writes() -> None:
    settings = Settings(skill_script_interpreters="bash")
    good = f".felix-skills/dig/{'a' * 32}/scripts/sub/run.sh"
    assert skill_script_argv_issue(["bash", good, "-c", "x"], settings) is None
    assert skill_script_argv_issue(["bash", good.replace("dig", "Dig")], settings) is not None
    assert (
        skill_script_argv_issue(["bash", f".felix-skills/dig/{'a' * 32}/scripts/./run.sh"], settings)
        is not None
    )
