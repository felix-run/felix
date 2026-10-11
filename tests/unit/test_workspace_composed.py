"""`glob` and `multi_edit`: workspace tools composed from operations every backend has.

The same calls run against `local` and against `hosted` (a fake gateway serving the real
`felix-fs` helper) and must give the same answers word for word -- the reason the tools are built
from existing backend operations rather than new gateway ones.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.governance.permission_mode import WORKSPACE_EDIT_TOOLS
from felix.manifests.delete_gate import FAMILIES
from felix.tools import workspace_composed, workspace_hosted
from felix.tools.builtins import default_tool_provider
from felix.tools.types import ToolInvocationCtx, tool_output_content
from felix.tools.workspace import _MAX_WRITE_BYTES, WORKSPACE_TOOL_NAMES
from felix.tools.workspace_backend import EditRefused
from felix.tools.workspace_composed import StringEdit, apply_edits, glob_match
from felix.tools.workspace_scope import SCOPES_DIR, thread_key

from tests.support.workspace_gateway_fake import TOKEN, URL, FakeGateway

TENANT = "acme"
THREAD = "acme:t1"
BACKENDS = ["local", "hosted"]
REFUSED = "[tool error/invalid_arguments]"


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


def _edit(old: str, new: str, **kw: Any) -> dict[str, Any]:
    return {"old_string": old, "new_string": new, **kw}


def _seed(here: Path, outside: Path) -> None:
    outside.mkdir(exist_ok=True)
    (outside / "secret.py").write_text("x = 1\n")
    (here / "src" / "pkg").mkdir(parents=True)
    (here / "src" / "pkg" / "a.py").write_text("def a():\n    return 1\n")
    (here / "src" / "pkg" / "test_a.py").write_text("from pkg import a\n")
    (here / "src" / "main.py").write_text("import pkg\nprint('hi')\nprint('hi')\n")
    # Outside `src`, its folder as long as `src/`: a filter that sliced `src/` off every path read
    # this as `main.py`, matching `*.py` under `src`.
    (here / "lib").mkdir()
    (here / "lib" / "main.py").write_text("x = 0\n")
    (here / "README.md").write_text("readme\r\nline two\r\n")
    (here / "big.txt").write_bytes(b"x" * 600_000)
    (here / "latin1.txt").write_bytes(b"caf\xe9\n")
    (here / ".git").mkdir()
    (here / ".git" / "hook.py").write_text("x\n")
    os.symlink(outside, here / "escape")


GLOBS: dict[str, dict[str, Any]] = {
    "all_py": {"pattern": "**/*.py"},
    "under_src": {"pattern": "*.py", "path": "src"},
    "tests_under_src": {"pattern": "**/test_*.py", "path": "src"},
    "capped": {"pattern": "**/*.py", "max_results": 2},
    "exactly_the_cap": {"pattern": "**/*.py", "path": "src", "max_results": 3},
    "md": {"pattern": "*.md"},
    "files_only": {"pattern": "src/*"},
    "climbs": {"pattern": "../*.py"},
    "absolute": {"pattern": "/etc/*"},
    "outside": {"pattern": "*", "path": "../outside"},
    "missing_folder": {"pattern": "*", "path": "nope"},
    "file_as_folder": {"pattern": "*", "path": "src/main.py"},
    "too_wild": {"pattern": "*a" * 9 + "b"},
}

EDITS: dict[str, dict[str, Any]] = {
    "two": {
        "path": "src/main.py",
        "edits": [
            _edit("import pkg\n", "import pkg\nimport sys\n"),
            _edit("print('hi')", "print('bye')", replace_all=True),
        ],
    },
    "crlf": {"path": "README.md", "edits": [_edit("readme", "README")]},
    "second_refused": {
        "path": "src/pkg/a.py",
        "edits": [_edit("return 1", "return 2"), _edit("nowhere", "x")],
    },
    "chained": {
        "path": "src/pkg/a.py",
        "edits": [_edit("return 1", "return 2"), _edit("return 2", "return 3")],
    },
    "ambiguous": {"path": "src/main.py", "edits": [_edit("import", "from")]},
    "identical": {"path": "src/main.py", "edits": [_edit("sys", "sys")]},
    "missing": {"path": "missing.py", "edits": [_edit("a", "b")]},
    "through_link": {"path": "escape/secret.py", "edits": [_edit("1", "2")]},
    "folder": {"path": "src", "edits": [_edit("a", "b")]},
    "too_big": {"path": "big.txt", "edits": [_edit("x", "y")]},
    "not_utf8": {"path": "latin1.txt", "edits": [_edit("caf", "CAF")]},
    "grows_past_cap": {"path": "lib/main.py", "edits": [_edit("x", "y" * (_MAX_WRITE_BYTES - 1))]},
    "no_edits": {"path": "lib/main.py", "edits": []},
    "too_many_edits": {"path": "lib/main.py", "edits": [_edit("x", "y")] * 51},
    "empty_old": {"path": "lib/main.py", "edits": [_edit("", "y", replace_all=True)]},
}


async def _session(settings: Settings, here: Path, outside: Path) -> dict[str, str]:
    _seed(here, outside)
    out: dict[str, str] = {}
    async with _request(settings):
        for name, args in GLOBS.items():
            out[f"glob:{name}"] = await _call("glob", args)
        for name, args in EDITS.items():
            target = here / args["path"]
            before = target.read_bytes() if target.is_file() else None
            out[f"edit:{name}"] = await _call("multi_edit", args)
            after = target.read_bytes() if target.is_file() else None
            out[f"file:{name}"] = repr(after) if after != before else "unchanged"
    return out


async def test_glob_and_multi_edit_answer_alike_on_both_backends(
    tmp_path: Path, gateway: FakeGateway
) -> None:
    local = await _session(
        _settings(tmp_path / "l", "local"), _here(tmp_path / "l", "local", gateway), tmp_path / "lo"
    )
    hosted = await _session(
        _settings(tmp_path / "h", "hosted"), _here(tmp_path / "h", "hosted", gateway), tmp_path / "ho"
    )
    assert hosted == local

    def files(name: str) -> tuple[list[str], bool]:
        body = json.loads(local[f"glob:{name}"])
        return body["files"], body["truncated"]

    assert files("all_py") == (["lib/main.py", "src/main.py", "src/pkg/a.py", "src/pkg/test_a.py"], False)
    assert files("under_src") == (["src/main.py"], False), "`*` stays within one folder, under `path`"
    assert files("tests_under_src") == (["src/pkg/test_a.py"], False)
    assert files("capped") == (["lib/main.py", "src/main.py"], True)
    assert files("exactly_the_cap")[1] is False, "exactly max_results matches is not truncated"
    assert files("md") == (["README.md"], False)
    assert files("files_only") == (["src/main.py"], False), "`src/pkg` is a folder"
    for name in ("climbs", "absolute"):
        assert local[f"glob:{name}"].startswith(f"{REFUSED} pattern must be relative to path"), name
    assert local["glob:outside"].startswith(REFUSED)
    assert local["glob:missing_folder"] == f"{REFUSED} not found: nope"
    assert local["glob:file_as_folder"] == f"{REFUSED} not a folder: src/main.py"
    assert local["glob:too_wild"].startswith(f"{REFUSED} pattern has more than 8")

    assert json.loads(local["edit:two"]) == {
        "path": "src/main.py",
        "edits": 2,
        "replacements": 3,
        "bytes": 48,
    }
    assert local["file:two"] == repr(b"import pkg\nimport sys\nprint('bye')\nprint('bye')\n")
    assert local["file:crlf"] == repr(b"README\r\nline two\r\n"), "CRLF untouched where no edit reached"
    assert local["edit:second_refused"] == (
        f"{REFUSED} edit 2: old_string not found in src/pkg/a.py (no edit was applied)"
    )
    assert local["file:second_refused"] == "unchanged", "all or none"
    assert json.loads(local["edit:chained"])["replacements"] == 2
    assert local["file:chained"] == repr(b"def a():\n    return 3\n"), "each edit sees the one before"
    assert "edit 1: old_string appears 2 times" in local["edit:ambiguous"]
    assert local["edit:identical"].startswith(
        f"{REFUSED} edit 1: old_string and new_string are identical in src/main.py"
    )
    assert local["edit:missing"] == f"{REFUSED} not a file: missing.py"
    assert local["edit:through_link"].startswith(REFUSED)
    assert local["edit:folder"] == f"{REFUSED} not a file: src"
    assert local["edit:too_big"].startswith(f"{REFUSED} big.txt is over 512000 bytes")
    assert local["edit:not_utf8"] == f"{REFUSED} not UTF-8 text: latin1.txt"
    assert "over the 512000 limit" in local["edit:grows_past_cap"]
    for name in ("no_edits", "too_many_edits", "empty_old"):
        assert local[f"edit:{name}"].startswith("[invalid args for multi_edit]"), name
    for name in ("ambiguous", "identical", "too_big", "not_utf8", "grows_past_cap", "no_edits", "empty_old"):
        assert local[f"file:{name}"] == "unchanged", name


@pytest.mark.parametrize("backend", BACKENDS)
async def test_glob_says_when_the_walk_stopped_short(
    tmp_path: Path, gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    """A big `node_modules` cut short must not read as "no such files"."""
    here = _here(tmp_path, backend, gateway)
    for n in range(6):
        (here / f"f{n}.txt").write_text("x")
    (here / "zz_target.py").write_text("x")
    monkeypatch.setattr(workspace_composed, "_GLOB_SCAN_LIMIT", 3)
    async with _request(_settings(tmp_path, backend)):
        body = json.loads(await _call("glob", {"pattern": "*.py"}))
    assert body == {"pattern": "*.py", "path": ".", "files": [], "truncated": True}


async def test_a_pattern_built_to_backtrack_answers_at_once(tmp_path: Path, gateway: FakeGateway) -> None:
    """`PurePath.full_match` ran a pattern like this against a long name for minutes on the loop."""
    here = _here(tmp_path, "local", gateway)
    (here / ("a" * 250)).write_text("x")
    started = time.monotonic()
    async with _request(_settings(tmp_path, "local")):
        body = json.loads(await _call("glob", {"pattern": "*a*a*a*a*a*a*a*b"}))
    assert body["files"] == []
    assert time.monotonic() - started < 2.0
    assert not glob_match(["*a*a*a*a*a*a*a*b"], ["a" * 250])


@pytest.mark.parametrize(
    ("pattern", "path", "matches"),
    [
        ("**/*.py", "a/b/c.py", True),
        ("**/*.py", "c.py", True),
        ("a/**/c.py", "a/c.py", True),
        ("a/**/c.py", "a/x/y/c.py", True),
        ("a/*/c.py", "a/x/y/c.py", False),
        ("*.py", "a/c.py", False),
        ("**", "a/b", True),
        ("a/**", "a", True),
        ("?.py", "ab.py", False),
        ("[ab].py", "b.py", True),
        ("*.PY", "c.py", False),
    ],
)
def test_glob_match_reads_globs_as_folders_and_names(pattern: str, path: str, matches: bool) -> None:
    assert glob_match(pattern.split("/"), path.split("/")) is matches


# --- `apply_edits` holds edit_file's rules ------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "old", "new", "replace_all"),
    [
        ("one two", "two", "three", False),
        ("one two", "four", "x", False),
        ("one two", "two", "two", False),
        ("a a a", "a", "b", False),
        ("a a a", "a", "b", True),
        ("line\r\nnext\r\n", "line", "LINE", False),
    ],
)
async def test_apply_edits_decides_as_edit_file_does(
    tmp_path: Path, gateway: FakeGateway, text: str, old: str, new: str, replace_all: bool
) -> None:
    """`apply_edits` re-states `edit_file`'s rules, which cannot be shared without drifting from
    the gateway's copy of them; this holds the two to the same decisions, text and words."""
    here = _here(tmp_path, "local", gateway)
    (here / "f.txt").write_bytes(text.encode())
    async with _request(_settings(tmp_path, "local")):
        theirs = await _call(
            "edit_file", {"path": "f.txt", "old_string": old, "new_string": new, "replace_all": replace_all}
        )
    their_text = (here / "f.txt").read_bytes().decode()
    try:
        ours_text, _ = apply_edits(
            text, [StringEdit(old_string=old, new_string=new, replace_all=replace_all)], "f.txt"
        )
        refused = None
    except EditRefused as exc:
        refused, ours_text = str(exc).removeprefix("edit 1: "), text
    if theirs.startswith(REFUSED):
        assert refused is not None and theirs == f"{REFUSED} {refused}"
    else:
        assert refused is None
    assert ours_text == their_text


# --- ordering ----------------------------------------------------------------------------------


async def test_a_parallel_edit_on_hosted_is_never_lost(tmp_path: Path, gateway: FakeGateway) -> None:
    """Hosted writes and edits take the checked write's lock, keyed by the normalised path: one
    lands after the other, or multi_edit refuses because the file changed -- never a silent
    overwrite of the edit."""
    here = _here(tmp_path, "hosted", gateway)
    (here / "f.py").write_text("alpha\nbeta\n")
    async with _request(_settings(tmp_path, "hosted")):
        # The scope prepared once, so the race below is between the two writes alone.
        await _call("list_dir", {"path": "."})
        multi, single = await asyncio.gather(
            _call("multi_edit", {"path": "f.py", "edits": [_edit("alpha", "ALPHA")]}),
            _call("edit_file", {"path": "./f.py", "old_string": "beta", "new_string": "BETA"}),
        )
    final = (here / "f.py").read_text()
    # Whatever said it landed, did: a success whose change is gone is the silent overwrite.
    if not single.startswith("[tool error"):
        assert "BETA" in final, (multi, single, final)
    if not multi.startswith("[tool error"):
        assert "ALPHA" in final, (multi, single, final)
    assert not (multi.startswith("[tool error") and single.startswith("[tool error")), (multi, single)


async def test_a_file_changed_after_the_read_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gateway: FakeGateway
) -> None:
    from felix.tools.workspace_local import LocalBackend

    here = _here(tmp_path, "local", gateway)
    (here / "f.py").write_text("one\n")
    real = LocalBackend.read_file

    async def read_then_someone_writes(self: Any, *a: Any, **kw: Any) -> Any:
        result = await real(self, *a, **kw)
        (here / "f.py").write_text("someone else's\n")
        return result

    monkeypatch.setattr(LocalBackend, "read_file", read_then_someone_writes)
    async with _request(_settings(tmp_path, "local")):
        out = await _call("multi_edit", {"path": "f.py", "edits": [_edit("one", "two")]})
    assert out == f"{REFUSED} f.py changed while the edits were applied; read it again (no edit was applied)"
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


def test_one_file_is_one_lock_however_its_path_is_written(tmp_path: Path) -> None:
    """`a.py` and `./a.py` are one file: two locks would let a write slip between a checked
    write's compare and its write."""
    from felix.tools.workspace_backend import WorkspaceScope

    settings = _settings(tmp_path, "hosted")
    scope = WorkspaceScope(tenant_id=TENANT, thread_id=THREAD, scope="thread")
    lock = workspace_hosted._write_lock(settings, scope, "src/a.py")
    assert workspace_hosted._write_lock(settings, scope, "./src/a.py") is lock
    assert workspace_hosted._write_lock(settings, scope, "src//a.py") is lock
    assert workspace_hosted._write_lock(settings, scope, "src/b.py") is not lock


async def test_glob_stops_its_own_work_at_the_deadline(
    tmp_path: Path, gateway: FakeGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`to_thread` cannot be cancelled: the filter checks the deadline itself, so the work stops
    when the answer does rather than running on after a timeout the caller already returned."""
    here = _here(tmp_path, "local", gateway)
    (here / "a.py").write_text("x")
    monkeypatch.setattr(workspace_composed, "_GLOB_BUDGET_S", -1.0)
    async with _request(_settings(tmp_path, "local")):
        out = await _call("glob", {"pattern": "*.py"})
    assert out.startswith("[tool error/timeout] glob exceeded")
    entries = [{"path": f"d/{n}.py", "type": "file"} for n in range(1000)]
    assert workspace_composed._filter(entries, "", ["**", "*.py"], 500, deadline=0.0) == ([], False, True)


def test_a_pattern_with_too_many_segments_is_refused() -> None:
    """Literal segments count toward no wildcard cap, and each one is a row of the match table."""
    assert (
        workspace_composed._glob_pattern_refusal("/".join(["a"] * 33)) == "pattern has more than 32 segments"
    )
    assert workspace_composed._glob_pattern_refusal("/".join(["a"] * 32)) is None


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize(
    "path", [".git/config", ".git/hooks/pre-commit", ".gitattributes", "src/.gitattributes"]
)
async def test_no_model_write_reaches_what_decides_what_git_runs(
    tmp_path: Path, gateway: FakeGateway, backend: str, path: str
) -> None:
    here = _here(tmp_path, backend, gateway)
    (here / ".git" / "hooks").mkdir(parents=True)
    (here / "src").mkdir()
    for existing in (".git/config", ".gitattributes", "src/.gitattributes"):
        (here / existing).write_text("x\n")
    async with _request(_settings(tmp_path, backend)):
        writes = [
            await _call("write_file", {"path": path, "content": "planted\n"}),
            await _call("edit_file", {"path": path, "old_string": "x", "new_string": "planted"}),
            await _call("multi_edit", {"path": path, "edits": [_edit("x", "planted")]}),
        ]
        ordinary = await _call("write_file", {"path": "src/ok.txt", "content": "fine\n"})
    for out in writes:
        assert out.startswith(f"{REFUSED} reserved path: {path}"), out
    assert "planted" not in "".join(
        p.read_text() for p in here.rglob("*") if p.is_file() and p.name != "ok.txt"
    )
    assert json.loads(ordinary)["path"] == "src/ok.txt"


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("to_path", [".gitattributes", "src/.gitattributes", ".GitAttributes", ".GIT/config"])
async def test_a_rename_cannot_land_on_what_decides_what_git_runs(
    tmp_path: Path, gateway: FakeGateway, backend: str, to_path: str
) -> None:
    """The one check every write, delete and rename uses: a rename was the way round the write's."""
    here = _here(tmp_path, backend, gateway)
    (here / "src").mkdir()
    (here / "notes.txt").write_text("x\n")
    async with _request(_settings(tmp_path, backend)):
        out = await _call("rename_file", {"path": "notes.txt", "to_path": to_path})
    assert out.startswith(f"{REFUSED} reserved path: {to_path}"), out
    assert (here / "notes.txt").read_text() == "x\n"


@pytest.mark.parametrize("path", [".GIT/config", ".Git/hooks/pre-commit", ".GITATTRIBUTES"])
async def test_case_does_not_get_round_the_reserved_names(
    tmp_path: Path, gateway: FakeGateway, path: str
) -> None:
    """A case-insensitive filesystem (macOS's default) serves `.GIT/config` as `.git/config`."""
    _here(tmp_path, "local", gateway)
    async with _request(_settings(tmp_path, "local")):
        out = await _call("write_file", {"path": path, "content": "x\n"})
    assert out.startswith(f"{REFUSED} reserved path: {path}"), out
