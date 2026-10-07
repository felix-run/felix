"""No workspace tool follows a symlink, at any component of the path.

The workspace is writable by the code the agent runs — on the builder stack from the `shell`
container, by a process that can outlive the tool call. A check that resolves the path and an
open that follows it by name lose the race between them: swap `src` for a link to `/proc/self`
after the check, and the API reads its own `environ`. So the tools walk the path by descriptor
with `O_NOFOLLOW` and refuse a symlink wherever it sits, pointing out of the workspace or back
into it — the deterministic cases below, and a race against a real swapping thread after them.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.context_files import _read_local
from felix.tools import workspace
from felix.tools.errors import ToolErrorCode, read_tool_error_code
from felix.tools.shell import resolve_cwd
from felix.tools.types import output_text

# Written against files at the workspace root; scope selection is test_workspace_scopes.py.
pytestmark = pytest.mark.usefixtures("deployment_workspace_scope")

SECRET = "SECRET-not-in-the-workspace"
REFUSED = "symlinks are not followed"


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path]:
    """`ws` with a real dir and file, and `outside` holding the secret, plus links of every shape."""
    ws, outside = tmp_path / "ws", tmp_path / "outside"
    ws.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text(SECRET, encoding="utf-8")
    (ws / "real").mkdir()
    (ws / "real" / "inner.txt").write_text("inner text\n", encoding="utf-8")
    (ws / "out_dir").symlink_to(outside, target_is_directory=True)
    (ws / "in_dir").symlink_to(ws / "real", target_is_directory=True)
    (ws / "out_file").symlink_to(outside / "secret.txt")
    (ws / "in_file").symlink_to(ws / "real" / "inner.txt")
    (ws / "real" / "nested_out").symlink_to(outside, target_is_directory=True)
    return ws.resolve(), outside.resolve()


async def _call(ws: Path, handler: Callable[..., Awaitable[object]], args: object) -> object:
    settings = Settings(
        allow_insecure=True, auth_mode="none", environment="development", workspace_root=str(ws)
    )
    async with async_run_with_context(RequestContext(settings=settings, auth=AuthContext(), thread_id="t")):
        return await handler(args)


def _assert_refused(out: object) -> None:
    assert read_tool_error_code(out) == ToolErrorCode.INVALID_ARGUMENTS, out
    assert REFUSED in output_text(out), out
    assert SECRET not in output_text(out)


# Every shape: a link as an intermediate directory and as the final component, out and in.
DIR_PATHS = ["out_dir/secret.txt", "in_dir/inner.txt", "real/nested_out/secret.txt"]
FILE_PATHS = ["out_file", "in_file"]


@pytest.mark.parametrize("path", DIR_PATHS + FILE_PATHS)
async def test_read_refuses_a_symlink_component(layout: tuple[Path, Path], path: str) -> None:
    ws, _ = layout
    _assert_refused(await _call(ws, workspace._read_file, workspace.ReadFileArgs(path=path)))


@pytest.mark.parametrize("path", [*DIR_PATHS, *FILE_PATHS, "out_dir/new.txt", "in_dir/new/deeper.txt"])
@pytest.mark.parametrize("append", [False, True])
async def test_write_refuses_a_symlink_component(layout: tuple[Path, Path], path: str, append: bool) -> None:
    ws, outside = layout
    args = workspace.WriteFileArgs(path=path, content="overwritten", append=append)
    _assert_refused(await _call(ws, workspace._write_file, args))
    assert (outside / "secret.txt").read_text(encoding="utf-8") == SECRET
    assert (ws / "real" / "inner.txt").read_text(encoding="utf-8") == "inner text\n"
    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]
    assert not (ws / "real" / "new").exists()


@pytest.mark.parametrize("path", DIR_PATHS + FILE_PATHS)
async def test_edit_refuses_a_symlink_component(layout: tuple[Path, Path], path: str) -> None:
    ws, outside = layout
    for old in (SECRET, "inner text"):
        args = workspace.EditFileArgs(path=path, old_string=old, new_string="edited")
        _assert_refused(await _call(ws, workspace._edit_file, args))
    assert (outside / "secret.txt").read_text(encoding="utf-8") == SECRET
    assert (ws / "real" / "inner.txt").read_text(encoding="utf-8") == "inner text\n"


@pytest.mark.parametrize("path", ["out_dir", "in_dir", "real/nested_out", "in_dir/."])
async def test_list_refuses_a_symlinked_directory(layout: tuple[Path, Path], path: str) -> None:
    ws, _ = layout
    _assert_refused(await _call(ws, workspace._list_dir, workspace.PathArgs(path=path)))


async def test_list_reports_a_link_as_a_link(layout: tuple[Path, Path]) -> None:
    ws, _ = layout
    listed = json.loads(output_text(await _call(ws, workspace._list_dir, workspace.PathArgs(path="."))))
    kinds = {e["path"]: e["type"] for e in listed["entries"]}
    assert kinds == {
        "in_dir": "symlink",
        "in_file": "symlink",
        "out_dir": "symlink",
        "out_file": "symlink",
        "real": "dir",
    }


@pytest.mark.parametrize("path", [*DIR_PATHS, *FILE_PATHS, "out_dir", "in_dir"])
async def test_search_refuses_a_symlink_component(layout: tuple[Path, Path], path: str) -> None:
    ws, _ = layout
    args = workspace.SearchFilesArgs(query="e", path=path)
    _assert_refused(await _call(ws, workspace._search_files, args))


async def test_a_tree_search_does_not_descend_into_or_read_through_a_link(layout: tuple[Path, Path]) -> None:
    ws, _ = layout
    for query in ("SECRET", "inner text"):
        out = await _call(ws, workspace._search_files, workspace.SearchFilesArgs(query=query, max_hits=50))
        hits = json.loads(output_text(out))["hits"]
        # The real file is found once, by its real name, and never through `in_dir`/`in_file`.
        assert [h["path"] for h in hits] == (["real/inner.txt"] if query == "inner text" else [])


async def test_ordinary_nested_paths_still_work(layout: tuple[Path, Path]) -> None:
    ws, _ = layout
    wrote = await _call(
        ws, workspace._write_file, workspace.WriteFileArgs(path="a/b/../b/c.txt", content="deep")
    )
    assert json.loads(output_text(wrote))["path"] == "a/b/c.txt"
    assert (ws / "a" / "b" / "c.txt").read_text(encoding="utf-8") == "deep"
    read = await _call(ws, workspace._read_file, workspace.ReadFileArgs(path="./a/b/c.txt"))
    assert json.loads(output_text(read))["content"] == "deep"


def test_context_files_and_shell_cwd_refuse_links_too(layout: tuple[Path, Path]) -> None:
    ws, _ = layout
    assert _read_local(ws, "out_file") is None
    assert _read_local(ws, "out_dir/secret.txt") is None
    assert _read_local(ws, "real/inner.txt") == "inner text\n"
    for cwd in ("out_dir", "in_dir", "real/nested_out"):
        with pytest.raises(ValueError, match=REFUSED):
            resolve_cwd(ws, cwd)
    assert resolve_cwd(ws, "real") == ws / "real"


async def test_a_directory_swapped_for_a_link_mid_call_never_leaks(tmp_path: Path) -> None:
    """A thread swaps `ws/d` between a real directory and a link to `outside` as fast as it
    can, while the tool reads `d/f.txt`. Check-then-open loses this race; the descriptor walk
    either finds the real directory or refuses the link, and never returns the outside file."""
    ws, outside = tmp_path / "ws", tmp_path / "outside"
    for d in (ws / "d", outside):
        d.mkdir(parents=True)
    (ws / "d" / "f.txt").write_text("public", encoding="utf-8")
    (outside / "f.txt").write_text(SECRET, encoding="utf-8")
    ws = ws.resolve()
    real, hold = ws / "d", ws / "hold"

    stop = threading.Event()
    errors: list[BaseException] = []

    def swap() -> None:
        try:
            while not stop.is_set():
                os.rename(real, hold)
                os.symlink(outside, real)
                os.unlink(real)
                os.rename(hold, real)
        except BaseException as exc:  # recorded and asserted on: a dead swapper races nothing
            errors.append(exc)

    # A short switch interval makes the interpreter interleave the two threads between the
    # syscalls of one tool call, which is where the window is.
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    swapper = threading.Thread(target=swap, daemon=True)
    outcomes: dict[str, int] = {"public": 0, "refused": 0, "missing": 0}
    swapper_alive = False
    try:
        swapper.start()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            out = await _call(ws, workspace._read_file, workspace.ReadFileArgs(path="d/f.txt"))
            text = output_text(out)
            assert SECRET not in text, "read the file outside the workspace through a swapped link"
            outcomes[_classify_read(out)] += 1
        swapper_alive = swapper.is_alive()
    finally:
        stop.set()
        swapper.join(timeout=5)
        sys.setswitchinterval(previous)
    assert errors == [], errors
    assert swapper_alive, "the swapper stopped early, so the race was not run for the whole window"
    # The race was actually run: the reader saw the directory both as itself and as a link.
    assert outcomes["public"] > 0, outcomes
    assert outcomes["refused"] + outcomes["missing"] > 0, outcomes


def _classify_read(out: object) -> str:
    """Exactly one of the three outcomes the race may produce, or the test fails on the output."""
    text = output_text(out)
    if read_tool_error_code(out) is None:
        assert json.loads(text)["content"] == "public", text
        return "public"
    assert read_tool_error_code(out) == ToolErrorCode.INVALID_ARGUMENTS, text
    if REFUSED in text:
        return "refused"  # `d` was the link when the walk reached it
    assert "not a file: d/f.txt" in text, text  # `d` was mid-rename: not there at all
    return "missing"


@pytest.mark.parametrize("append", [False, True])
async def test_a_dangling_link_is_not_created_through(layout: tuple[Path, Path], append: bool) -> None:
    """`O_CREAT` follows a dangling link and creates its target; `O_NOFOLLOW` refuses it."""
    ws, outside = layout
    (ws / "dangling").symlink_to(outside / "new.txt")
    args = workspace.WriteFileArgs(path="dangling", content="planted", append=append)
    _assert_refused(await _call(ws, workspace._write_file, args))
    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]


@pytest.mark.parametrize("append", [False, True])
async def test_a_file_swapped_for_a_link_mid_write_is_never_written_through(
    tmp_path: Path, append: bool
) -> None:
    """The leaf alternates, by atomic rename, between a regular file and a link to the secret.
    Every write either lands in the regular file or is refused; the secret never changes."""
    ws, outside = tmp_path / "ws", tmp_path / "outside"
    ws.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text(SECRET, encoding="utf-8")
    ws = ws.resolve()
    leaf, link_tmp, file_tmp = ws / "f.txt", ws / "link.tmp", ws / "file.tmp"
    leaf.write_text("public", encoding="utf-8")

    stop = threading.Event()
    errors: list[BaseException] = []

    def swap() -> None:
        try:
            while not stop.is_set():
                os.symlink(outside / "secret.txt", link_tmp)
                os.rename(link_tmp, leaf)
                file_tmp.write_text("public", encoding="utf-8")
                os.rename(file_tmp, leaf)
        except BaseException as exc:
            errors.append(exc)

    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    swapper = threading.Thread(target=swap, daemon=True)
    outcomes = {"written": 0, "refused": 0}
    swapper_alive = False
    try:
        swapper.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            args = workspace.WriteFileArgs(path="f.txt", content="x", append=append)
            out = await _call(ws, workspace._write_file, args)
            text = output_text(out)
            if read_tool_error_code(out) is None:
                assert json.loads(text)["path"] == "f.txt", text
                outcomes["written"] += 1
            else:
                _assert_refused(out)
                outcomes["refused"] += 1
        swapper_alive = swapper.is_alive()
    finally:
        stop.set()
        swapper.join(timeout=5)
        sys.setswitchinterval(previous)
    assert errors == [], errors
    assert swapper_alive
    assert (outside / "secret.txt").read_text(encoding="utf-8") == SECRET
    assert sorted(p.name for p in outside.iterdir()) == ["secret.txt"]
    assert outcomes["written"] > 0 and outcomes["refused"] > 0, outcomes


async def test_a_symlinked_workspace_root_is_refused(layout: tuple[Path, Path]) -> None:
    """The root is opened `O_NOFOLLOW` too. Not a check that it is a mountpoint: a dev or test
    root is a plain directory, and only a link in its *last* component is refused."""
    ws, _ = layout
    alias = ws.parent / "ws_alias"
    alias.symlink_to(ws, target_is_directory=True)
    out = await _call(alias, workspace._read_file, workspace.ReadFileArgs(path="real/inner.txt"))
    assert read_tool_error_code(out) == ToolErrorCode.TRANSPORT_UNAVAILABLE, out
    assert "workspace_root is a symlink" in output_text(out)
    # A caller handing the walk a root it did not get from `workspace_root()` is refused at the open.
    with (
        pytest.raises(ValueError, match="workspace_root is a symlink"),
        workspace.open_workspace_parent(alias, "real/inner.txt"),
    ):
        pass
    assert _read_local(alias, "real/inner.txt") is None
    assert _read_local(ws, "real/inner.txt") == "inner text\n"
