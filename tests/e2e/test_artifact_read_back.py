"""A spilled tool result, read back by the model through a real request.

The chain no unit test holds: `spec.artifacts` → the builder binding `read_artifact` beside the
spill → the governance stack wrapping both → the object store the API was booted with → the
model offered the reader and shown what it returns. Before the reader existed the model saw a
preview and a marker, and every call it could make stopped there.
"""

from __future__ import annotations

import re
import types
from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

ARTIFACT_ID = "a" * 32
# Distinct lines, so a window that came from the wrong offset cannot match by accident.
FILE = "".join(f"row {i:05d}\n" for i in range(800))
LAST_ROW = "row 00799"
# `read_file` answers in JSON, so the spilled text is escaped file plus a wrapper: about 9K
# characters here. One 16K read (the default `max_window_chars`) from this offset reaches the
# end with room to spare, so the test does not hinge on the exact shape of that JSON.
TAIL_OFFSET = 2_000


def _manifest(name: str, *, artifacts: bool, **spec: Any) -> Any:
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": name},
            "spec": {
                "pattern": "react",
                "tools": ["read_file"],
                "auth": {"inbound": {"allow_anonymous": True}},
                "artifacts": {"enabled": artifacts, "threshold_chars": 2000, "preview_chars": 100},
                **spec,
            },
        }
    )


@pytest.fixture
def workspace(tmp_path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    (tmp_path / "big.txt").write_text(FILE)
    # The spill names its object with uuid4, which a script written in advance cannot know.
    # Only this module's reference is replaced; the object is still written and read for real.
    monkeypatch.setattr(
        "felix.artifacts.uuid", types.SimpleNamespace(uuid4=lambda: types.SimpleNamespace(hex=ARTIFACT_ID))
    )
    return {"FELIX_WORKSPACE_ROOT": str(tmp_path)}


async def _chat(app: Any, name: str) -> Any:
    return await app.client.post(
        "/v1/chat/completions",
        json={"model": name, "messages": [{"role": "user", "content": "What is the last row of big.txt?"}]},
    )


async def test_the_model_reads_past_the_preview(boot: Any, workspace: dict[str, str]) -> None:
    script = [
        ScriptedTurn(tool_calls=[ToolCall(id="c1", name="read_file", args={"path": "big.txt"})]),
        ScriptedTurn(
            tool_calls=[
                ToolCall(
                    id="c2",
                    name="read_artifact",
                    args={"artifact_id": ARTIFACT_ID, "offset": TAIL_OFFSET, "length": 16_000},
                )
            ]
        ),
        ScriptedTurn(content=LAST_ROW),
    ]
    async with boot(
        script, env=workspace, manifests={"e2e-spill": _manifest("e2e-spill", artifacts=True)}
    ) as app:
        response = await _chat(app, "e2e-spill")

    assert response.status_code == 200, response.text
    assert all("read_artifact" in offered for offered in app.spy.tools), "every step offers the reader"
    after_read_file, after_read_artifact = (
        "\n".join(str(getattr(m, "content", "") or "") for m in prompt) for prompt in app.spy.prompts[1:3]
    )
    # The preview is what the spill leaves: a preview and the marker, not the file.
    assert f"[artifact:{ARTIFACT_ID}" in after_read_file
    assert re.search(rf"\[artifact:{ARTIFACT_ID} key=\S+ chars=\d+ spilled_at=\d+\]", after_read_file)
    assert LAST_ROW not in after_read_file, "the spill kept the file out of the transcript"
    # The reader reached the stored object and returned the window the model asked for.
    assert f"[artifact-window:{ARTIFACT_ID} chars {TAIL_OFFSET}-" in after_read_artifact
    assert "; end]" in after_read_artifact
    assert LAST_ROW in after_read_artifact


async def test_without_artifacts_there_is_no_reader(boot: Any, workspace: dict[str, str]) -> None:
    script = [ScriptedTurn(content="ok")]
    async with boot(
        script, env=workspace, manifests={"e2e-plain": _manifest("e2e-plain", artifacts=False)}
    ) as app:
        response = await _chat(app, "e2e-plain")

    assert response.status_code == 200, response.text
    assert app.spy.tools and all("read_artifact" not in offered for offered in app.spy.tools)


async def test_a_policy_on_the_reader_is_enforced(boot: Any, workspace: dict[str, str]) -> None:
    # The reader is bound before the governance stack, so it is governed like the call that
    # produced the artifact. Bound after it, this policy would wrap nothing and the window
    # would come back — which is what this asserts does not happen.
    script = [
        ScriptedTurn(tool_calls=[ToolCall(id="c1", name="read_file", args={"path": "big.txt"})]),
        ScriptedTurn(
            tool_calls=[
                ToolCall(
                    id="c2", name="read_artifact", args={"artifact_id": ARTIFACT_ID, "offset": TAIL_OFFSET}
                )
            ]
        ),
        ScriptedTurn(content="denied"),
    ]
    policy = {"id": "artifacts-need-scope", "required_scopes": ["artifacts:read"], "tools": ["read_artifact"]}
    manifest = _manifest("e2e-gated", artifacts=True, policies=[policy])
    async with boot(script, env=workspace, manifests={"e2e-gated": manifest}) as app:
        response = await _chat(app, "e2e-gated")

    assert response.status_code == 200, response.text
    after_read_artifact = "\n".join(str(getattr(m, "content", "") or "") for m in app.spy.prompts[2])
    assert "[policy denied] missing scopes for read_artifact" in after_read_artifact
    assert f"[artifact-window:{ARTIFACT_ID}" not in after_read_artifact
    assert LAST_ROW not in after_read_artifact


async def test_a_second_conversation_cannot_read_the_first_ones_spill(
    boot: Any, workspace: dict[str, str]
) -> None:
    # The security review's case: every caller of a manifest shares its tenant/manifest prefix,
    # so an id alone would reach another user's spilled output. Two requests, two
    # conversations; the second knows the id (it is fixed here, as a leak would make it) and
    # is refused.
    script = [
        ScriptedTurn(tool_calls=[ToolCall(id="c1", name="read_file", args={"path": "big.txt"})]),
        ScriptedTurn(content="spilled"),
        ScriptedTurn(
            tool_calls=[
                ToolCall(
                    id="c2", name="read_artifact", args={"artifact_id": ARTIFACT_ID, "offset": TAIL_OFFSET}
                )
            ]
        ),
        ScriptedTurn(content="refused"),
    ]
    async with boot(
        script, env=workspace, manifests={"e2e-two": _manifest("e2e-two", artifacts=True)}
    ) as app:
        first = await _chat(app, "e2e-two")
        second = await _chat(app, "e2e-two")

    assert first.status_code == 200 and second.status_code == 200
    after_read_artifact = "\n".join(str(getattr(m, "content", "") or "") for m in app.spy.prompts[3])
    assert f"no artifact '{ARTIFACT_ID}'" in after_read_artifact
    assert LAST_ROW not in after_read_artifact
