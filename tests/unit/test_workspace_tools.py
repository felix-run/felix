"""Workspace path sandbox and file tools."""

from __future__ import annotations

from pathlib import Path

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.tools.builtins import default_tool_provider
from felix.tools.types import ToolInvocationCtx
from felix.tools.workspace import resolve_under_root


def test_resolve_under_root_rejects_escape(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "ok.txt").write_text("hi", encoding="utf-8")
    assert resolve_under_root(root, "ok.txt").name == "ok.txt"
    with pytest.raises(ValueError, match="escapes"):
        resolve_under_root(root, "../outside.txt")
    with pytest.raises(ValueError, match="absolute"):
        resolve_under_root(root, str(tmp_path / "abs.txt"))


@pytest.mark.asyncio
async def test_workspace_read_write_list_search(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "notes.txt").write_text("alpha beta gamma", encoding="utf-8")
    settings = Settings(
        allow_insecure=True,
        auth_mode="none",
        environment="development",
        workspace_root=str(ws),
    )
    provider = default_tool_provider()
    ctx = RequestContext(settings=settings, auth=AuthContext(), thread_id="t1")
    async with async_run_with_context(ctx):
        listed = await provider.get("list_dir").executor.execute(
            {"path": "."}, ToolInvocationCtx(thread_id="t1")
        )
        assert "notes.txt" in str(listed)

        written = await provider.get("write_file").executor.execute(
            {"path": "out.txt", "content": "hello workspace"},
            ToolInvocationCtx(thread_id="t1"),
        )
        assert "out.txt" in str(written)
        assert (ws / "out.txt").read_text(encoding="utf-8") == "hello workspace"

        read = await provider.get("read_file").executor.execute(
            {"path": "notes.txt"}, ToolInvocationCtx(thread_id="t1")
        )
        assert "alpha beta" in str(read)

        found = await provider.get("search_files").executor.execute(
            {"query": "beta", "path": "."}, ToolInvocationCtx(thread_id="t1")
        )
        assert "notes.txt" in str(found)
