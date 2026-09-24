"""`edit_file` — the in-place edit that `write_file` cannot express.

`write_file` writes whole files, so changing one paragraph of a large file means sending the
whole file back. That is how a stray docstring edit reached a Felix-authored branch, and under
a `CHANGELOG.md` every pull request appends to it is not merely wasteful: the file is larger
than a model should be asked to reproduce. These tests hold the edit to an exact match.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.tools.builtins import default_tool_provider
from felix.tools.types import ToolInvocationCtx
from felix.tools.workspace import _MAX_WRITE_BYTES


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

    assert json.loads(out)["replacements"] == 1
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

    assert "exceeds" in out
    assert (ws / "a.txt").read_text(encoding="utf-8") == "seed\n"
