"""`spec.skill_scripts` through the real app: the model activates a skill, then runs its script.

The compile is what binds `run_skill_script` and wraps it in the governance stack; a manifest
that asks for scripts on a host that lists no interpreter is refused rather than compiled without.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall


@pytest.fixture
def skills_dir(tmp_path: Path) -> Path:
    skill = tmp_path / "skills" / "tally"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: tally\ndescription: Count things.\n---\nRun scripts/count.sh.\n"
    )
    (skill / "scripts" / "count.sh").write_text('echo "counted:$#:$1"\n')
    return tmp_path / "skills"


def _manifests(**spec: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "pattern": "react",
        "auth": {"inbound": {"allow_anonymous": True}},
        "skills": [{"name": "tally"}],
        "skill_scripts": {"enabled": True, "interpreters": {".sh": "bash"}},
        **spec,
    }
    return {
        "e2e-scripts": parse_manifest(
            {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "e2e-scripts"}, "spec": base}
        )
    }


def _env(skills_dir: Path, workspace: Path, interpreters: str = "bash") -> dict[str, str]:
    return {
        "FELIX_SKILLS_DIR": str(skills_dir),
        "FELIX_WORKSPACE_ROOT": str(workspace),
        "FELIX_SKILL_SCRIPT_INTERPRETERS": interpreters,
    }


async def _chat(app: Any) -> Any:
    return await app.client.post(
        "/chat",
        json={
            "manifest": "e2e-scripts",
            "thread_id": "e2e-s1",
            "messages": [{"role": "user", "content": "count"}],
        },
    )


def _tool_result(prompt: list[Any], name: str) -> str:
    [message] = [m for m in prompt if getattr(m, "role", "") == "tool" and getattr(m, "name", "") == name][
        -1:
    ]
    return str(message.content)


async def test_the_model_runs_an_active_skill_s_script(boot: Any, skills_dir: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    script = [
        ScriptedTurn(tool_calls=[ToolCall(id="a1", name="activate_skill", args={"name": "tally"})]),
        ScriptedTurn(
            tool_calls=[
                ToolCall(
                    id="r1",
                    name="run_skill_script",
                    args={"name": "tally", "script": "scripts/count.sh", "args": ["x", "y"]},
                )
            ]
        ),
        ScriptedTurn(content="2."),
    ]
    async with boot(script, env=_env(skills_dir, workspace), manifests=_manifests()) as app:
        response = await _chat(app)
        assert response.status_code == 200, response.text
        result = json.loads(_tool_result(app.spy.prompts[-1], "run_skill_script"))
    assert result["exit_code"] == 0 and result["stdout"] == "counted:2:x\n"


async def test_a_host_without_the_interpreter_refuses_the_compile(
    boot: Any, skills_dir: Path, tmp_path: Path
) -> None:
    """Refused, not compiled without the tool: an agent told "I can't run that" would hide the cause."""
    from felix.manifests.governance import GovernanceError

    workspace = tmp_path / "ws"
    workspace.mkdir()
    async with boot(
        [ScriptedTurn(content="never")], env=_env(skills_dir, workspace, "python3"), manifests=_manifests()
    ) as app:
        with pytest.raises(GovernanceError, match="FELIX_SKILL_SCRIPT_INTERPRETERS"):
            await _chat(app)
    assert app.spy.prompts == [], "no model call was made"


async def test_the_compiled_tool_is_governed(boot: Any, skills_dir: Path, tmp_path: Path) -> None:
    """Bound before the wrapper stack: a command-screening deny on an argument stops the script."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    marker = tmp_path / "ran"
    (skills_dir / "tally" / "scripts" / "count.sh").write_text(f'touch "{marker}"\n')
    script = [
        ScriptedTurn(tool_calls=[ToolCall(id="a1", name="activate_skill", args={"name": "tally"})]),
        ScriptedTurn(
            tool_calls=[
                ToolCall(
                    id="r1",
                    name="run_skill_script",
                    args={"name": "tally", "script": "scripts/count.sh", "args": ["DROP TABLE x"]},
                )
            ]
        ),
        ScriptedTurn(content="no."),
    ]
    screening = {"enabled": True, "rules": [{"pattern": "drop table", "decision": "deny"}]}
    async with boot(
        script, env=_env(skills_dir, workspace), manifests=_manifests(command_screening=screening)
    ) as app:
        response = await _chat(app)
        assert response.status_code == 200, response.text
        result = _tool_result(app.spy.prompts[-1], "run_skill_script")
    assert "[command denied]" in result
    assert not marker.exists()
