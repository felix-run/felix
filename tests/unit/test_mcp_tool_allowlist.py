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

# `report[2024]` is a remote name that is itself glob-shaped; the binder must match it literally.
REMOTE_TOOLS = ("issue_read", "issue_write", "create_pull_request", "report[2024]")


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


async def _bound_names(tools: list[str]) -> list[str]:
    """A list, not a set: a filter that binds a tool once per matching pattern must show."""
    ref = McpServerRef(name="gh", url="https://mcp.example.com/mcp", transport="http", tools=tools)
    bound = await tools_from_mcp_servers([ref], allow_http=False, manifest_id="m")
    return [t.name for t in bound]


@pytest.mark.asyncio
async def test_empty_allowlist_binds_every_remote_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default every existing manifest was written against."""
    _install_fake_server(monkeypatch)
    assert await _bound_names([]) == [f"gh__{n}" for n in REMOTE_TOOLS]


@pytest.mark.asyncio
async def test_allowlist_binds_only_what_it_names(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_server(monkeypatch)
    assert await _bound_names(["issue_read"]) == ["gh__issue_read"]


@pytest.mark.asyncio
async def test_allowlist_patterns_are_globs_over_remote_names(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_server(monkeypatch)
    assert await _bound_names(["issue_*"]) == ["gh__issue_read", "gh__issue_write"]


@pytest.mark.asyncio
async def test_overlapping_patterns_bind_a_tool_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_server(monkeypatch)
    assert await _bound_names(["issue_*", "issue_read"]) == ["gh__issue_read", "gh__issue_write"]


@pytest.mark.asyncio
async def test_a_glob_shaped_remote_name_listed_literally_binds_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`fnmatch` reads `[2024]` as a character class; `tool_match` treats a pattern with no
    wildcard as a name. The binder must go through `tool_match`, not raw `fnmatch`."""
    _install_fake_server(monkeypatch)
    assert await _bound_names(["report[2024]"]) == ["gh__report[2024]"]


@pytest.mark.asyncio
async def test_the_bound_spelling_is_accepted_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every other `tools:` list in a manifest is written over the bound name, `gh__issue_read`.

    An author copying a name from the approval block into the allowlist is the likely case, so
    the server's own prefix is stripped before matching rather than binding nothing.
    """
    _install_fake_server(monkeypatch)
    assert await _bound_names(["gh__issue_read"]) == ["gh__issue_read"]
    # Another server's prefix is not this server's, and stays a literal that matches nothing.
    assert await _bound_names(["other__issue_read"]) == []


@pytest.mark.asyncio
async def test_a_pattern_the_server_no_longer_serves_is_logged_not_fatal(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A renamed remote tool must not take the rest of the server down with it."""
    _install_fake_server(monkeypatch)
    counted: list[tuple[str, dict[str, str]]] = []
    import felix.mcp.client as client_mod

    monkeypatch.setattr(
        client_mod, "record_counter", lambda name, labels: counted.append((name, dict(labels)))
    )
    with caplog.at_level(logging.WARNING, logger="felix.mcp.client"):
        assert await _bound_names(["issue_read", "renamed_upstream"]) == ["gh__issue_read"]
    assert any("renamed_upstream" in r.getMessage() for r in caplog.records)
    # The same counter `builder.py` uses for an approval or policy rule that gates nothing, so
    # an operator alerting on it sees a renamed remote tool without a second alert.
    assert counted == [
        ("felix_rule_targets_nothing", {"manifest_id": "m", "kind": "mcp_allowlist", "rule": "gh"})
    ]


@pytest.mark.asyncio
async def test_secret_resolution_keeps_the_allowlist() -> None:
    """`resolve_mcp_ref` sits between the manifest and the binder. A rewrite that rebuilt the
    ref field by field would drop `tools` with every binder test green — the silent-default shape."""
    from felix.manifests.secret_refs import resolve_mcp_ref

    class _NoSecrets:
        async def get(self, name: str) -> str | None:
            return None

    ref = McpServerRef(name="gh", url="https://mcp.example.com/mcp", transport="http", tools=["issue_read"])
    assert (await resolve_mcp_ref(ref, _NoSecrets())).tools == ["issue_read"]
