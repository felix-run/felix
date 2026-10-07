"""The workspace backend seam (WORKSPACE.md phase 2b): the tools judge, a backend does the I/O.

The five workspace tools keep their argument models, limits, regex screen and every message; the
file I/O moved behind `WorkspaceBackend`, with `LocalBackend` doing what the tools did in-process.
These pin the seam itself -- that every operation reaches the backend with the call's scope, that
the order a caller sees refusals in did not change, and that the tool handlers no longer touch the
filesystem, which is what lets a backend that is not this host's filesystem stand behind them.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.tools import workspace, workspace_backend
from felix.tools.builtins import default_tool_provider
from felix.tools.types import ToolInvocationCtx, tool_output_content
from felix.tools.workspace_backend import (
    EditRefused,
    EditResult,
    ListResult,
    ReadResult,
    SearchResult,
    WorkspaceBackend,
    WorkspaceScope,
    WriteResult,
)
from felix.tools.workspace_local import LocalBackend
from felix.tools.workspace_scope import bound_scope


class Recording:
    """A backend that serves canned answers and records what it was asked."""

    def __init__(self, *, unusable: bool = False, refuse_edit: str = "") -> None:
        self.calls: list[tuple[str, WorkspaceScope | None, tuple[Any, ...]]] = []
        self.unusable = unusable
        self.refuse_edit = refuse_edit

    async def prepare(self, scope: WorkspaceScope | None) -> None:
        self.calls.append(("prepare", scope, ()))
        if self.unusable:
            raise ValueError("workspace_root is not configured (set FELIX_WORKSPACE_ROOT)")

    async def list_dir(self, scope: WorkspaceScope | None, path: str) -> ListResult:
        self.calls.append(("list_dir", scope, (path,)))
        return ListResult(path=path, entries=[{"path": "a.txt", "type": "file", "size": 1}])

    async def read_file(self, scope: WorkspaceScope | None, path: str, offset: int, limit: int) -> ReadResult:
        self.calls.append(("read_file", scope, (path, offset, limit)))
        return ReadResult(path=path, size=5, data=b"hello")

    async def write_file(
        self, scope: WorkspaceScope | None, path: str, data: bytes, append: bool
    ) -> WriteResult:
        self.calls.append(("write_file", scope, (path, data, append)))
        return WriteResult(path=path, bytes=len(data))

    async def edit_file(
        self, scope: WorkspaceScope | None, path: str, old: str, new: str, replace_all: bool
    ) -> EditResult:
        self.calls.append(("edit_file", scope, (path, old, new, replace_all)))
        if self.refuse_edit:
            raise EditRefused(self.refuse_edit)
        return EditResult(path=path, replacements=1, bytes=3)

    async def search(
        self, scope: WorkspaceScope | None, path: str, query: str, regex: bool, max_hits: int
    ) -> SearchResult:
        self.calls.append(("search", scope, (path, query, regex, max_hits)))
        return SearchResult(hits=[{"path": "a.txt", "line": 1, "text": query}])


@pytest.fixture
def recording(monkeypatch: pytest.MonkeyPatch) -> Recording:
    backend = Recording()
    monkeypatch.setattr(workspace_backend, "get_workspace_backend", lambda settings: backend)
    return backend


async def _call(
    tool: str, args: dict[str, Any], *, tenant: str = "acme", thread: str | None = "acme:t1"
) -> str:
    ctx = RequestContext(settings=Settings(), auth=AuthContext(tenant_id=tenant), thread_id=thread)
    async with async_run_with_context(ctx):
        with bound_scope("tenant"):
            out = (
                await default_tool_provider()
                .get(tool)
                .executor.execute(args, ToolInvocationCtx(thread_id=thread))
            )
    return tool_output_content(out)


SCOPE = WorkspaceScope(tenant_id="acme", thread_id="acme:t1", scope="tenant")


async def test_every_operation_reaches_the_backend_with_the_calls_scope(recording: Recording) -> None:
    assert json.loads(await _call("list_dir", {"path": "."}))["entries"][0]["path"] == "a.txt"
    assert json.loads(await _call("read_file", {"path": "a.txt"}))["content"] == "hello"
    assert (
        json.loads(await _call("write_file", {"path": "a.txt", "content": "hi", "append": True}))["bytes"]
        == 2
    )
    edit = {"path": "a.txt", "old_string": "x", "new_string": "y", "replace_all": True}
    assert json.loads(await _call("edit_file", edit))["replacements"] == 1
    found = json.loads(await _call("search_files", {"query": "needle", "path": "src", "max_hits": 7}))
    assert found["hits"][0]["text"] == "needle"

    operations = [(name, args) for name, scope, args in recording.calls if name != "prepare"]
    assert operations == [
        ("list_dir", (".",)),
        ("read_file", ("a.txt", 0, 512_000)),
        ("write_file", ("a.txt", b"hi", True)),
        ("edit_file", ("a.txt", "x", "y", True)),
        ("search", ("src", "needle", False, 7)),
    ]
    assert {scope for _, scope, _ in recording.calls} == {SCOPE}


async def test_an_unusable_workspace_is_reported_before_anything_wrong_with_the_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The order a caller saw before the seam: the workspace first, then the arguments."""
    backend = Recording(unusable=True)
    monkeypatch.setattr(workspace_backend, "get_workspace_backend", lambda settings: backend)
    too_big = "x" * 600_000
    for tool, args in (
        ("write_file", {"path": "a.txt", "content": too_big}),
        ("edit_file", {"path": "a.txt", "old_string": "x", "new_string": too_big}),
        ("search_files", {"query": "(a+)+", "regex": True}),
    ):
        out = await _call(tool, args)
        assert out.startswith("[tool error/transport_unavailable] workspace_root"), (tool, out)
    assert [name for name, _, _ in backend.calls] == ["prepare"] * 3


async def test_the_tools_judge_their_arguments_before_the_backend_is_asked(recording: Recording) -> None:
    out = await _call("write_file", {"path": "a.txt", "content": "x" * 600_000})
    assert "content exceeds" in out
    out = await _call("search_files", {"query": "(a+)+", "regex": True})
    assert "nests a quantifier" in out
    out = await _call("search_files", {"query": "(", "regex": True})
    assert "invalid regex" in out
    assert [name for name, _, _ in recording.calls] == ["prepare"] * 3


async def test_an_edit_the_backend_refuses_is_an_error_the_model_can_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = Recording(refuse_edit="old_string not found in a.txt")
    monkeypatch.setattr(workspace_backend, "get_workspace_backend", lambda settings: backend)
    out = await _call("edit_file", {"path": "a.txt", "old_string": "x", "new_string": "y"})
    assert out == "[tool error/invalid_arguments] old_string not found in a.txt"


def test_the_local_backend_is_a_workspace_backend() -> None:
    backend: WorkspaceBackend = LocalBackend(Settings())
    assert isinstance(backend, LocalBackend)


def test_the_tool_handlers_do_no_file_io_of_their_own() -> None:
    """Structural, deliberately: a handler that opened a file itself would work on `local` and be
    wrong on every other backend, and no behavioural test against `local` could tell."""
    tree = ast.parse(Path(workspace.__file__).read_text(encoding="utf-8"))
    handlers = {"_list_dir", "_read_file", "_write_file", "_edit_file", "_search_files"}
    file_io = {
        "open_workspace_parent",
        "open_workspace_dir",
        "open_regular",
        "open_at",
        "workspace_root",
        "_pread",
        "_write_all",
        "_dir_batch",
        "to_thread",
    }
    found: dict[str, set[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name in handlers:
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            attrs = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
            used = (names | attrs) & file_io
            used |= {
                "os." + a
                for a in attrs
                if any(isinstance(n, ast.Name) and n.id == "os" for n in ast.walk(node))
            }
            found[node.name] = used
    assert set(found) == handlers
    assert found == {name: set() for name in handlers}, found
