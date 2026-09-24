"""`edit_file` — the in-place edit that `write_file` cannot express.

`write_file` writes whole files, so changing one paragraph of a large file means sending the
whole file back. That is how a stray docstring edit reached a Felix-authored branch, and under
a `CHANGELOG.md` every pull request appends to it is not merely wasteful: the file is larger
than a model should be asked to reproduce. These tests hold the edit to an exact match.
"""

from __future__ import annotations

import ast
import json
import os
import stat
from pathlib import Path

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.tools import workspace
from felix.tools.builtins import default_tool_provider
from felix.tools.types import ToolInvocationCtx
from felix.tools.workspace import _MAX_EDIT_FILE_BYTES, _MAX_WRITE_BYTES


async def _edit(ws: Path, **args: object) -> str:
    settings = Settings(
        allow_insecure=True,
        auth_mode="none",
        environment="development",
        workspace_root=str(ws),
    )
    provider = default_tool_provider()
    ctx = RequestContext(settings=settings, auth=AuthContext(), thread_id="t1")
    async with async_run_with_context(ctx):
        return str(
            await provider.get("edit_file").executor.execute(dict(args), ToolInvocationCtx(thread_id="t1"))
        )


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.mark.asyncio
async def test_it_replaces_the_match_and_leaves_every_other_byte(ws: Path) -> None:
    (ws / "CHANGELOG.md").write_text("## [Unreleased]\n\n### Added\n\n- old entry\n", encoding="utf-8")

    out = await _edit(
        ws,
        path="CHANGELOG.md",
        old_string="### Added\n",
        new_string="### Added\n\n- **New thing.** What it does.\n",
    )

    assert json.loads(out) == {"path": "CHANGELOG.md", "replacements": 1, "bytes": 72}, (
        "path stays relative to the workspace root — an absolute one would put it in a prompt"
    )
    assert (ws / "CHANGELOG.md").read_text(encoding="utf-8") == (
        "## [Unreleased]\n\n### Added\n\n- **New thing.** What it does.\n\n- old entry\n"
    )


@pytest.mark.asyncio
async def test_a_string_that_is_not_there_changes_nothing(ws: Path) -> None:
    (ws / "a.txt").write_text("alpha\n", encoding="utf-8")

    out = await _edit(ws, path="a.txt", old_string="beta", new_string="gamma")

    assert "not found" in out
    assert (ws / "a.txt").read_text(encoding="utf-8") == "alpha\n"


@pytest.mark.asyncio
async def test_an_ambiguous_match_is_refused_until_replace_all_says_otherwise(ws: Path) -> None:
    (ws / "a.txt").write_text("x\nx\n", encoding="utf-8")

    refused = await _edit(ws, path="a.txt", old_string="x", new_string="y")
    assert "appears 2 times" in refused
    assert (ws / "a.txt").read_text(encoding="utf-8") == "x\nx\n", "a refused edit writes nothing"

    done = await _edit(ws, path="a.txt", old_string="x", new_string="y", replace_all=True)
    assert json.loads(done)["replacements"] == 2
    assert (ws / "a.txt").read_text(encoding="utf-8") == "y\ny\n"


@pytest.mark.asyncio
async def test_it_cannot_edit_outside_the_workspace(ws: Path) -> None:
    outside = ws.parent / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")

    out = await _edit(ws, path="../outside.txt", old_string="secret", new_string="leaked")

    assert "escapes workspace root" in out
    assert outside.read_text(encoding="utf-8") == "secret\n"


@pytest.mark.asyncio
async def test_an_edit_that_changes_nothing_is_refused(ws: Path) -> None:
    (ws / "a.txt").write_text("same\n", encoding="utf-8")

    out = await _edit(ws, path="a.txt", old_string="same", new_string="same")

    assert "identical" in out
    assert (ws / "a.txt").read_text(encoding="utf-8") == "same\n"


@pytest.mark.asyncio
async def test_a_missing_match_is_reported_before_the_no_op(ws: Path) -> None:
    """Order matters for what the model does next: told the strings are identical, it goes
    looking for a string that was never in the file."""
    (ws / "a.txt").write_text("alpha\n", encoding="utf-8")

    out = await _edit(ws, path="a.txt", old_string="zzz", new_string="zzz")

    assert "not found" in out


@pytest.mark.asyncio
async def test_a_binary_file_is_refused_rather_than_mangled(ws: Path) -> None:
    (ws / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")

    out = await _edit(ws, path="blob.bin", old_string="a", new_string="b")

    assert "not UTF-8" in out
    assert (ws / "blob.bin").read_bytes() == b"\xff\xfe\x00\x01"


@pytest.mark.asyncio
async def test_a_missing_file_is_not_created(ws: Path) -> None:
    out = await _edit(ws, path="nope.txt", old_string="a", new_string="b")

    assert "not a file" in out
    assert not (ws / "nope.txt").exists()


@pytest.mark.asyncio
async def test_it_edits_a_file_too_large_for_write_file_to_send(ws: Path) -> None:
    """The reason the tool exists: the file is bigger than a whole-file write may carry."""
    big = ws / "CHANGELOG.md"
    body = "## [Unreleased]\n\n### Added\n\n" + ("- an entry that is already here\n" * 20_000)
    big.write_text(body, encoding="utf-8")
    assert big.stat().st_size > _MAX_WRITE_BYTES

    out = await _edit(
        ws,
        path="CHANGELOG.md",
        old_string="### Added\n\n",
        new_string="### Added\n\n- **Pricing.** New rates.\n",
    )

    assert json.loads(out)["replacements"] == 1
    assert big.read_text(encoding="utf-8").startswith(
        "## [Unreleased]\n\n### Added\n\n- **Pricing.** New rates.\n- an entry"
    )


@pytest.mark.asyncio
async def test_a_new_string_larger_than_the_write_cap_is_refused(ws: Path) -> None:
    (ws / "a.txt").write_text("seed\n", encoding="utf-8")

    out = await _edit(ws, path="a.txt", old_string="seed", new_string="z" * (_MAX_WRITE_BYTES + 1))

    assert "new_string exceeds" in out, "the file-size ceiling must not answer for this one"
    assert (ws / "a.txt").read_text(encoding="utf-8") == "seed\n"


@pytest.mark.asyncio
async def test_it_keeps_the_line_endings_it_did_not_edit(ws: Path) -> None:
    """`read_text` would translate every `\r\n` to `\n` and the write-back would keep the
    translation — one edited line silently rewriting every line in the file, which is the
    damage this tool exists to prevent."""
    crlf = ws / "notes.md"
    crlf.write_bytes(b"line one\r\nline two\r\nline three\r\n")

    out = await _edit(ws, path="notes.md", old_string="line two", new_string="line 2")

    assert json.loads(out)["replacements"] == 1
    assert crlf.read_bytes() == b"line one\r\nline 2\r\nline three\r\n"


@pytest.mark.asyncio
async def test_an_old_string_spanning_a_crlf_matches(ws: Path) -> None:
    """`read_file` hands the model bytes, so the model quotes `\r\n` back. If the edit read
    through universal newlines, what it was shown would not match what it searches."""
    crlf = ws / "notes.md"
    crlf.write_bytes(b"alpha\r\nbeta\r\n")

    out = await _edit(ws, path="notes.md", old_string="alpha\r\nbeta", new_string="gamma")

    assert json.loads(out)["replacements"] == 1
    assert crlf.read_bytes() == b"gamma\r\n"


@pytest.mark.asyncio
async def test_a_file_over_the_ceiling_is_refused_before_it_is_read(ws: Path) -> None:
    big = ws / "huge.txt"
    big.write_bytes(b"x")
    os.truncate(big, _MAX_EDIT_FILE_BYTES + 1)  # sparse: the guard reads stat, never content

    out = await _edit(ws, path="huge.txt", old_string="x", new_string="y")

    assert "huge.txt exceeds" in out
    assert big.stat().st_size == _MAX_EDIT_FILE_BYTES + 1


@pytest.mark.asyncio
async def test_replace_all_cannot_inflate_a_file_past_the_ceiling(ws: Path) -> None:
    """Both caps bound an input; replace_all multiplies them. 1 MB of `x` and a five-byte
    replacement is 5 MB out of inputs that are each individually allowed."""
    grower = ws / "a.txt"
    grower.write_bytes(b"x" * 1_000_000)

    out = await _edit(ws, path="a.txt", old_string="x", new_string="xxxxx", replace_all=True)

    assert "would make a.txt 5000000 bytes" in out
    assert grower.stat().st_size == 1_000_000, "refused before str.replace built it"


@pytest.mark.asyncio
async def test_an_empty_new_string_deletes_the_match(ws: Path) -> None:
    """`new_string` documents this, and it is the one case where an edit shrinks a file to
    nothing — `old_string` still has to be reproduced exactly for that to happen."""
    (ws / "a.txt").write_text("keep\nDROP ME\nkeep\n", encoding="utf-8")

    out = await _edit(ws, path="a.txt", old_string="DROP ME\n", new_string="")

    assert json.loads(out)["replacements"] == 1
    assert (ws / "a.txt").read_text(encoding="utf-8") == "keep\nkeep\n"


@pytest.mark.asyncio
async def test_an_edited_file_keeps_its_mode(ws: Path) -> None:
    """The write goes through a temp file and a rename, so the mode has to be carried across:
    `scripts/test.sh` coming back without its executable bit would break the gates."""
    script = ws / "run.sh"
    script.write_text("#!/bin/sh\nold\n", encoding="utf-8")
    script.chmod(0o755)

    await _edit(ws, path="run.sh", old_string="old", new_string="new")

    assert stat.S_IMODE(script.stat().st_mode) == 0o755
    assert script.read_text(encoding="utf-8") == "#!/bin/sh\nnew\n"
    assert not list(ws.glob(".*.felix-edit")), "the temp file does not outlive the rename"


def test_the_read_and_the_write_are_both_inside_the_lock() -> None:
    """Structural, not behavioural, and deliberately so: there is no `await` between the read
    and the write, so within one event loop the body is atomic and a concurrency test would
    pass with the lock removed — a test that cannot fail, wearing the look of proof. This one
    goes red when the `async with` is dropped, and the control below shows it can tell the
    difference."""
    source = Path(workspace.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    bodies = {
        node.name: node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
    }

    def takes_the_lock(name: str) -> bool:
        return any(
            isinstance(node, ast.AsyncWith)
            and any(
                isinstance(item.context_expr, ast.Call)
                and getattr(item.context_expr.func, "id", "") == "_write_lock"
                for item in node.items
            )
            for node in ast.walk(bodies[name])
        )

    assert takes_the_lock("_edit_file")
    assert takes_the_lock("_write_file")
    assert not takes_the_lock("_read_file"), "positive control: the scan discriminates"


@pytest.mark.asyncio
async def test_a_write_that_fails_leaves_the_original_where_it_was(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A truncating write would leave a half-file whose prior contents exist nowhere: unlike
    a whole-file write, the edit's arguments do not carry the pre-image to retry from."""
    target = ws / "a.txt"
    target.write_text("original\n", encoding="utf-8")
    real_write = Path.write_bytes

    def full_disk(self: Path, data: bytes) -> int:
        if self.name.endswith(".felix-edit"):
            raise OSError(28, "No space left on device")
        return real_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", full_disk)

    out = await _edit(ws, path="a.txt", old_string="original", new_string="edited")

    assert "No space left on device" in out
    assert target.read_text(encoding="utf-8") == "original\n"
    assert not list(ws.glob(".*.felix-edit"))
