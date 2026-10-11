"""`glob` and `multi_edit`: workspace tools composed from operations every backend has.

The same calls run against `local` and against `hosted` (a fake gateway serving the real
`felix-fs` helper), and must give the same answers word for word -- the reason the tools are built
from existing backend operations rather than new gateway ones.
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
from felix.governance.permission_mode import WORKSPACE_EDIT_TOOLS
from felix.manifests.delete_gate import FAMILIES
from felix.tools import workspace_hosted
from felix.tools.builtins import default_tool_provider
from felix.tools.types import ToolInvocationCtx, tool_output_content
from felix.tools.workspace import WORKSPACE_TOOL_NAMES
from felix.tools.workspace_scope import SCOPES_DIR, thread_key

from tests.support.workspace_gateway_fake import TOKEN, URL, FakeGateway

TENANT = "acme"
THREAD = "acme:t1"
BACKENDS = ["local", "hosted"]


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


def _here(base: Path, backend: str, gateway: FakeGateway) -> Path:
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
    (outside / "secret.py").write_text("x = 1\n")
    (here / "src" / "pkg").mkdir(parents=True)
    (here / "src" / "pkg" / "a.py").write_text("def a():\n    return 1\n")
    (here / "src" / "pkg" / "test_a.py").write_text("from pkg import a\n")
    (here / "src" / "main.py").write_text("import pkg\nprint('hi')\nprint('hi')\n")
    (here / "README.md").write_text("readme\r\nline two\r\n")
    # Outside `src`, its folder as long as `src/`: a filter that sliced `src/` off every path read
    # this as `main.py`, matching `*.py` under `src`.
    (here / "lib").mkdir()
    (here / "lib" / "main.py").write_text("x = 0\n")
    (here / ".git").mkdir()
    (here / ".git" / "hook.py").write_text("x\n")
    os.symlink(outside, here / "escape")


async def _session(settings: Settings, here: Path, outside: Path) -> list[str]:
    _seed(here, outside)
    out: list[str] = []
    async with _request(settings):
        for args in (
            {"pattern": "**/*.py"},
            {"pattern": "*.py", "path": "src"},
            {"pattern": "**/test_*.py", "path": "src"},
            {"pattern": "**/*.py", "max_results": 2},
            {"pattern": "*.md"},
            {"pattern": "src/*"},  # `src/pkg` is a folder: files only
            {"pattern": "../*.py"},
            {"pattern": "/etc/*"},
            {"pattern": "*", "path": "../outside"},
        ):
            out.append(await _call("glob", args))
        edits = [
            {"old_string": "import pkg\n", "new_string": "import pkg\nimport sys\n"},
            {"old_string": "print('hi')", "new_string": "print('bye')", "replace_all": True},
        ]
        out.append(await _call("multi_edit", {"path": "src/main.py", "edits": edits}))
        out.append((here / "src" / "main.py").read_text())
        # CRLF untouched where no edit reached.
        out.append(
            await _call(
                "multi_edit",
                {"path": "README.md", "edits": [{"old_string": "readme", "new_string": "README"}]},
            )
        )
        out.append(repr((here / "README.md").read_bytes()))
        # All or none: the second edit is refused, so the first is not written.
        before = (here / "src" / "pkg" / "a.py").read_text()
        bad = [
            {"old_string": "return 1", "new_string": "return 2"},
            {"old_string": "nowhere", "new_string": "x"},
        ]
        out.append(await _call("multi_edit", {"path": "src/pkg/a.py", "edits": bad}))
        out.append(str((here / "src" / "pkg" / "a.py").read_text() == before))
        # Edits see the result of the ones before them.
        chained = [
            {"old_string": "return 1", "new_string": "return 2"},
            {"old_string": "return 2", "new_string": "return 3"},
        ]
        out.append(await _call("multi_edit", {"path": "src/pkg/a.py", "edits": chained}))
        out.append(
            await _call(
                "multi_edit",
                {"path": "src/main.py", "edits": [{"old_string": "import", "new_string": "from"}]},
            )
        )
        out.append(
            await _call(
                "multi_edit", {"path": "missing.py", "edits": [{"old_string": "a", "new_string": "b"}]}
            )
        )
        out.append(
            await _call(
                "multi_edit", {"path": "escape/secret.py", "edits": [{"old_string": "1", "new_string": "2"}]}
            )
        )
        out.append(
            await _call("multi_edit", {"path": "src", "edits": [{"old_string": "a", "new_string": "b"}]})
        )
    return out


@pytest.mark.parametrize("backend", BACKENDS)
async def test_glob_and_multi_edit_answer_alike_on_both_backends(
    tmp_path: Path, gateway: FakeGateway, backend: str
) -> None:
    local = await _session(
        _settings(tmp_path / "l", "local"), _here(tmp_path / "l", "local", gateway), tmp_path / "lo"
    )
    other = await _session(
        _settings(tmp_path / "h", backend), _here(tmp_path / "h", backend, gateway), tmp_path / "ho"
    )
    assert other == local
    globs = [json.loads(o) if o.startswith("{") else o for o in local[:9]]
    assert globs[0]["files"] == ["lib/main.py", "src/main.py", "src/pkg/a.py", "src/pkg/test_a.py"]
    assert globs[0]["truncated"] is False
    assert globs[1]["files"] == ["src/main.py"], "`*` stays within one folder, and under `path`"
    assert globs[2]["files"] == ["src/pkg/test_a.py"]
    assert globs[3]["files"] == ["lib/main.py", "src/main.py"] and globs[3]["truncated"] is True
    assert globs[4]["files"] == ["README.md"]
    assert globs[5]["files"] == ["src/main.py"], "files only: `src/pkg` is a folder"
    assert all("tool error" in g for g in local[6:9]), local[6:9]
    edits = local[9:]
    assert json.loads(edits[0]) == {"path": "src/main.py", "edits": 2, "replacements": 3, "bytes": 48}
    assert edits[1] == "import pkg\nimport sys\nprint('bye')\nprint('bye')\n"
    assert edits[3] == repr(b"README\r\nline two\r\n")
    assert "edit 2: old_string not found" in edits[4] and "no edit was applied" in edits[4]
    assert edits[5] == "True"
    assert json.loads(edits[6])["replacements"] == 2
    assert "edit 1: old_string appears 2 times" in edits[7]
    assert all("tool error" in o for o in edits[8:11]), edits[8:11]


async def test_a_file_changed_after_the_read_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gateway: FakeGateway
) -> None:
    from felix.tools.workspace_local import LocalBackend

    settings = _settings(tmp_path, "local")
    here = _here(tmp_path, "local", gateway)
    (here / "f.py").write_text("one\n")
    real = LocalBackend.read_file

    async def read_then_someone_writes(self: Any, *a: Any, **kw: Any) -> Any:
        result = await real(self, *a, **kw)
        (here / "f.py").write_text("someone else's\n")
        return result

    monkeypatch.setattr(LocalBackend, "read_file", read_then_someone_writes)
    async with _request(settings):
        out = await _call(
            "multi_edit", {"path": "f.py", "edits": [{"old_string": "one", "new_string": "two"}]}
        )
    assert "changed while the edits were applied" in out
    assert (here / "f.py").read_text() == "someone else's\n"


def test_the_new_tools_join_every_list_the_workspace_tools_are_named_in() -> None:
    """A writer missing from these would escape plan mode, accept_edits and the delete gate."""
    assert {"glob", "multi_edit"} <= WORKSPACE_TOOL_NAMES
    assert "multi_edit" in WORKSPACE_EDIT_TOOLS
    [server] = [writers for family, writers, _ in FAMILIES if family == "server"]
    assert "multi_edit" in server
    provider = default_tool_provider()
    assert provider.get("glob").read_only is True
    assert provider.get("multi_edit").read_only is False
