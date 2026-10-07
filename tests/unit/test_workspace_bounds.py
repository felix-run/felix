"""What one workspace tool call may cost, whatever the agent's code left in the workspace.

The workspace is writable by code the agent runs, so its contents are adversarial in size and
shape the way a tool argument is: a sparse file that reports 64 MiB (or 50 GiB) and costs
nothing on disk, an instruction file grown past any sane prompt, a directory planted under the
name an edit was about to use, a tree nested deeper than a recursive walk survives. Each is
bounded here at the call that would have paid for it.
"""

from __future__ import annotations

import json
import logging
import os
import tracemalloc
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.context_files import MAX_CONTEXT_FILE_BYTES, _read_local
from felix.tools import workspace
from felix.tools.errors import read_tool_error_code
from felix.tools.types import output_text

# Written against files at the workspace root; scope selection is test_workspace_scopes.py.
pytestmark = pytest.mark.usefixtures("deployment_workspace_scope")


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    return root.resolve()


async def _call(ws: Path, handler: Callable[..., Awaitable[object]], args: object) -> object:
    settings = Settings(
        allow_insecure=True, auth_mode="none", environment="development", workspace_root=str(ws)
    )
    async with async_run_with_context(RequestContext(settings=settings, auth=AuthContext(), thread_id="t")):
        return await handler(args)


def _ok(out: object) -> dict:
    assert read_tool_error_code(out) is None, out
    return json.loads(output_text(out))


SPARSE = 64 * 1024 * 1024


async def test_read_file_reads_only_the_window_of_a_sparse_file(ws: Path) -> None:
    """The whole file used to be read and then sliced. A 64 MiB sparse file is free on disk and
    64 MiB of `bytes` in the API; tracemalloc sees that allocation whichever thread makes it."""
    with (ws / "sparse.bin").open("wb") as fh:
        fh.truncate(SPARSE)
        fh.seek(SPARSE // 2)
        fh.write(b"WINDOW")
        fh.truncate(SPARSE)
    tracemalloc.start()
    try:
        out = await _call(
            ws, workspace._read_file, workspace.ReadFileArgs(path="sparse.bin", offset=SPARSE // 2, limit=6)
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # The output contract is unchanged: the window, at its offset, beside the whole file's size.
    assert _ok(out) == {"path": "sparse.bin", "offset": SPARSE // 2, "size": SPARSE, "content": "WINDOW"}
    assert peak < SPARSE // 8, f"peak {peak} bytes: the read allocated far more than its window"


async def test_read_file_past_the_end_is_an_empty_window(ws: Path) -> None:
    (ws / "a.txt").write_text("abc", encoding="utf-8")
    for offset in (3, 10, 10**30):  # 10**30 overflows off_t; the slice it replaced did not care
        out = await _call(ws, workspace._read_file, workspace.ReadFileArgs(path="a.txt", offset=offset))
        assert _ok(out) == {"path": "a.txt", "offset": offset, "size": 3, "content": ""}
    out = await _call(ws, workspace._read_file, workspace.ReadFileArgs(path="a.txt", offset=1, limit=1))
    assert _ok(out)["content"] == "b"


def test_a_context_file_over_the_cap_is_ignored_not_cut(ws: Path, caplog: pytest.LogCaptureFixture) -> None:
    (ws / "at.md").write_bytes(b"a" * MAX_CONTEXT_FILE_BYTES)
    (ws / "over.md").write_bytes(b"a" * (MAX_CONTEXT_FILE_BYTES + 1))
    assert _read_local(ws, "at.md") == "a" * MAX_CONTEXT_FILE_BYTES
    with caplog.at_level(logging.WARNING, logger="felix.context_files"):
        assert _read_local(ws, "over.md") is None
    assert "exceeds" in caplog.text


async def test_an_edit_is_not_blocked_by_a_directory_under_the_old_temp_name(ws: Path) -> None:
    """The temp name was `.{leaf}.felix-edit`: a directory planted there failed every edit of
    `leaf` — the unlink-and-retry removed a file or a link, never a directory."""
    (ws / "a.txt").write_text("old\n", encoding="utf-8")
    (ws / ".a.txt.felix-edit").mkdir()
    out = await _call(
        ws, workspace._edit_file, workspace.EditFileArgs(path="a.txt", old_string="old", new_string="new")
    )
    assert _ok(out)["replacements"] == 1
    assert (ws / "a.txt").read_text(encoding="utf-8") == "new\n"
    assert sorted(p.name for p in ws.iterdir()) == [".a.txt.felix-edit", "a.txt"]


async def test_an_edit_of_a_leaf_near_name_max_has_a_temp_name_that_fits(ws: Path) -> None:
    """`.{leaf}.felix-edit` is the leaf plus 12 bytes, so a 250-byte name had no temp name."""
    leaf = "x" * 246 + ".txt"
    (ws / leaf).write_text("old\n", encoding="utf-8")
    out = await _call(
        ws, workspace._edit_file, workspace.EditFileArgs(path=leaf, old_string="old", new_string="new")
    )
    assert _ok(out)["replacements"] == 1
    assert (ws / leaf).read_text(encoding="utf-8") == "new\n"
    assert [p.name for p in ws.iterdir()] == [leaf]


async def test_search_stops_at_the_depth_limit_and_returns_cleanly(ws: Path) -> None:
    """A recursive walk takes a stack frame and a descriptor per level of a tree the agent built."""
    deepest = workspace._MAX_SEARCH_DEPTH + 16
    (ws / "top.txt").write_text("needle\n", encoding="utf-8")
    here = ws
    for level in range(1, deepest + 1):
        here = here / "d"
        here.mkdir()
        if level in (10, deepest):
            (here / "hit.txt").write_text("needle\n", encoding="utf-8")
    out = await _call(ws, workspace._search_files, workspace.SearchFilesArgs(query="needle", max_hits=50))
    hits = [h["path"] for h in _ok(out)["hits"]]
    # Found within the limit, in the walk's name order; skipped past it — not an error.
    assert hits == ["/".join(["d"] * 10 + ["hit.txt"]), "top.txt"]


async def test_a_listing_reads_a_bounded_batch_and_sorts_it(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace, "_MAX_DIR_BATCH", 5)
    for i in range(20):
        (ws / f"f{i:02}.txt").write_text("x", encoding="utf-8")
    out = await _call(ws, workspace._list_dir, workspace.PathArgs(path="."))
    paths = [e["path"] for e in _ok(out)["entries"]]
    assert len(paths) == 5
    assert paths == sorted(paths, key=str.lower)
    assert set(paths) <= set(os.listdir(ws))
