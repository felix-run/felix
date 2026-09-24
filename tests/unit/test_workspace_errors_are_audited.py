"""A workspace tool that fails is audited as failing.

The workspace tools used to return failures as plain `error: …` text. That text carries no
error marker, so the tool runner wrote the audit row as `tool_call` / `ok`, the metrics counted a
success, and the eval trajectory — which only has the text, and reads it against
`FAILURE_CONTENT_PREFIXES` — did not count a failure either. It was found on the reference
deployment on 2026-09-24: an approved `write_file` failed with `Errno 13` twice and both rows
said `ok`.

These drive the real `write_file` / `edit_file` / `read_file` through the real `ToolRunner`, with
only the audit sink replaced (patched where the runner looks it up, as
`test_audit_deny_control.py` does), and assert all three readers agree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.patterns import tool_runner as runner_mod
from felix.patterns.tool_runner import ToolRunner
from felix.patterns.types import ToolCall
from felix.tools.builtins import default_tool_provider
from felix.tools.types import is_failure_content


async def _run(
    monkeypatch: pytest.MonkeyPatch, *, root: str, tool: str, args: dict[str, Any]
) -> tuple[list[tuple[str, str]], str]:
    """Run one call; return the audit rows as (kind, status) and the tool message the model sees."""
    audited: list[tuple[str, str]] = []
    monkeypatch.setattr(
        runner_mod,
        "emit_agent_audit",
        lambda kind, **kw: audited.append((kind, str(kw.get("status")))),
    )
    settings = Settings(
        allow_insecure=True,
        auth_mode="none",
        environment="development",
        workspace_root=root,
    )
    ctx = RequestContext(
        settings=settings,
        auth=AuthContext(principal_sub="p", tenant_id="t"),
        manifest_id="m",
        thread_id="th",
    )
    provider = default_tool_provider()
    async with async_run_with_context(ctx):
        msgs, _, _ = await ToolRunner(tool_map={tool: provider.get(tool)}, manifest_id="m").run_batch(
            [ToolCall(id="1", name=tool, args=args)], thread_id="th", tenant_id="t"
        )
    return audited, str(msgs[0].content)


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.mark.asyncio
async def test_a_write_the_filesystem_refuses_is_audited_as_an_error(
    monkeypatch: pytest.MonkeyPatch, ws: Path
) -> None:
    """The production case: the mount is owned by someone else and the write gets `Errno 13`."""

    def _refused(self: Path, data: bytes) -> int:
        raise PermissionError(13, "Permission denied", str(self))

    # Patched rather than chmod'd, so the test means the same thing when the suite runs as root.
    monkeypatch.setattr(Path, "write_bytes", _refused)

    audited, content = await _run(
        monkeypatch, root=str(ws), tool="write_file", args={"path": "a.txt", "content": "x"}
    )

    assert audited == [("tool_call", "error")], audited
    assert content.startswith("[tool error/permission_denied]"), content
    assert "Permission denied" in content
    assert is_failure_content(content), "the eval trajectory would not count this as a failure"


@pytest.mark.asyncio
async def test_a_refused_edit_is_an_error_the_model_can_fix(
    monkeypatch: pytest.MonkeyPatch, ws: Path
) -> None:
    """A bad argument is still an error — distinct code, same wording the model already reads."""
    (ws / "a.txt").write_text("alpha\n", encoding="utf-8")

    audited, content = await _run(
        monkeypatch,
        root=str(ws),
        tool="edit_file",
        args={"path": "a.txt", "old_string": "beta", "new_string": "gamma"},
    )

    assert audited == [("tool_call", "error")], audited
    assert content.startswith("[tool error/invalid_arguments]"), content
    assert "old_string not found in a.txt" in content
    assert (ws / "a.txt").read_text(encoding="utf-8") == "alpha\n"


@pytest.mark.asyncio
async def test_a_path_that_escapes_is_refused_and_audited(monkeypatch: pytest.MonkeyPatch, ws: Path) -> None:
    audited, content = await _run(monkeypatch, root=str(ws), tool="read_file", args={"path": "../outside"})

    assert audited == [("tool_call", "error")], audited
    assert content.startswith("[tool error/invalid_arguments]"), content
    assert "escapes workspace root" in content


@pytest.mark.asyncio
async def test_a_missing_workspace_is_the_transport_not_the_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No workspace at all is not something the model can fix by asking differently."""
    audited, content = await _run(
        monkeypatch, root=str(tmp_path / "does-not-exist"), tool="list_dir", args={"path": "."}
    )

    assert audited == [("tool_call", "error")], audited
    assert content.startswith("[tool error/transport_unavailable]"), content


@pytest.mark.asyncio
async def test_a_write_that_works_is_still_ok(monkeypatch: pytest.MonkeyPatch, ws: Path) -> None:
    """The guard against over-correcting: success keeps its status and its JSON body."""
    audited, content = await _run(
        monkeypatch, root=str(ws), tool="write_file", args={"path": "a.txt", "content": "x"}
    )

    assert audited == [("tool_call", "ok")], audited
    assert not is_failure_content(content)
    assert (ws / "a.txt").read_text(encoding="utf-8") == "x"
