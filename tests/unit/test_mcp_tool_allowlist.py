"""`McpServerRef.tools` — the allowlist that makes a remote server's surface enumerable.

Before this field the whole remote catalogue bound, so an approval rule could only gate the
tools its author happened to know about on the day. The allowlist inverts that: a tool the
manifest does not name does not exist for the agent, and a governance test can compare the
manifest to itself and mean it.
"""

from __future__ import annotations

import json
import logging

import pytest
from felix.manifests.schema import McpServerRef
from felix.mcp.client import tools_from_mcp_servers

REMOTE_TOOLS = ("issue_read", "issue_write", "create_pull_request")


def _install_fake_server(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        status_code = 200
        headers = {"content-type": "application/json"}

        def __init__(self, body: dict):
            self._body = body

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._body

        @property
        def text(self) -> str:
            return json.dumps(self._body)

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            method = (json or {}).get("method")
            if method == "initialize":
                return _Resp({"jsonrpc": "2.0", "id": 1, "result": {}})
            if method == "tools/list":
                tools = [
                    {"name": n, "description": n, "inputSchema": {"type": "object"}} for n in REMOTE_TOOLS
                ]
                return _Resp({"jsonrpc": "2.0", "id": 2, "result": {"tools": tools}})
            return _Resp({"jsonrpc": "2.0", "id": 0, "error": {"code": -1, "message": "nope"}})

    import felix.mcp.client as client_mod

    monkeypatch.setattr(client_mod.httpx, "AsyncClient", _Client)


async def _bound_names(tools: list[str]) -> set[str]:
    ref = McpServerRef(name="gh", url="https://mcp.example.com/mcp", transport="http", tools=tools)
    bound = await tools_from_mcp_servers([ref], allow_http=False)
    return {t.name for t in bound}


@pytest.mark.asyncio
async def test_empty_allowlist_binds_every_remote_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default every existing manifest was written against."""
    _install_fake_server(monkeypatch)
    assert await _bound_names([]) == {f"gh__{n}" for n in REMOTE_TOOLS}


@pytest.mark.asyncio
async def test_allowlist_binds_only_what_it_names(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_server(monkeypatch)
    assert await _bound_names(["issue_read"]) == {"gh__issue_read"}


@pytest.mark.asyncio
async def test_allowlist_patterns_are_globs_over_remote_names(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_server(monkeypatch)
    assert await _bound_names(["issue_*"]) == {"gh__issue_read", "gh__issue_write"}


@pytest.mark.asyncio
async def test_allowlist_matches_the_remote_name_not_the_prefixed_one(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`gh__issue_read` is the *local* name. Writing it in the allowlist binds nothing.

    That is the trap an author falls into after reading the approval rules, which do use the
    prefixed form. Nothing binds, and the log says which pattern matched nothing.
    """
    _install_fake_server(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="felix.mcp.client"):
        assert await _bound_names(["gh__issue_read"]) == set()
    assert any("gh__issue_read" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_pattern_the_server_no_longer_serves_is_logged_not_fatal(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A renamed remote tool must not take the rest of the server down with it."""
    _install_fake_server(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="felix.mcp.client"):
        assert await _bound_names(["issue_read", "renamed_upstream"]) == {"gh__issue_read"}
    assert any("renamed_upstream" in r.getMessage() for r in caplog.records)
