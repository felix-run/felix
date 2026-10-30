"""An MCP server a compile cannot reach is named, not dropped in silence.

The end-to-end case (a job firing, its prompt and its run record) is
`tests/e2e/test_unreachable_mcp_source.py`; these pin the two halves it rests on.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.manifests.schema import McpServerRef


async def test_a_stdio_server_that_cannot_start_is_reported_unreachable() -> None:
    """A stdio server that fails to start used to be swallowed inside `list_remote_tools` as an
    empty tool list -- indistinguishable from a server with no tools, so nothing named it."""
    from felix.mcp import client

    client.clear_discovery_cache()
    ref = McpServerRef(name="git", transport="stdio", command="/nonexistent/felix-mcp-server")
    unavailable: list[str] = []
    assert await client.tools_from_mcp_servers([ref], unavailable=unavailable) == []
    assert unavailable == ["git"]


async def test_only_the_server_that_failed_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.mcp import client

    async def fake_list(ref: McpServerRef, *, allow_http: bool = False, handshake: Any = None) -> list:
        if ref.name == "tracker":
            raise ConnectionError("down")
        return [{"name": "search"}]

    client.clear_discovery_cache()
    monkeypatch.setattr(client, "list_remote_tools", fake_list)
    refs = [McpServerRef(name=n, url=f"https://{n}.example.com/mcp") for n in ("tracker", "docs")]
    unavailable: list[str] = []
    tools = await client.tools_from_mcp_servers(refs, unavailable=unavailable)
    assert [t.name for t in tools] == ["docs__search"]
    assert unavailable == ["tracker"]


def test_each_server_is_recorded_once_under_the_manifest_that_named_it() -> None:
    from felix.context import AuthContext, RequestContext, run_with_context
    from felix.mcp.client import UNAVAILABLE_SOURCES_EXTRA, record_unavailable_sources

    from tests.support.factories import make_settings

    ctx = RequestContext(settings=make_settings(), auth=AuthContext(tenant_id="default"))
    with run_with_context(ctx):
        record_unavailable_sources("router", ["tracker"])
        record_unavailable_sources("router", ["tracker", "calendar"])
        # A child compiled for the same run: its server is its own, not the router's.
        record_unavailable_sources("billing", ["ledger"])
    assert ctx.extras[UNAVAILABLE_SOURCES_EXTRA] == {"router": ["tracker", "calendar"], "billing": ["ledger"]}


def test_the_note_names_the_servers_and_records_nothing() -> None:
    """The wording only: recording is `record_unavailable_sources`, so a caller that wants the
    text alone cannot add to a run's record by asking for it."""
    from felix.context import AuthContext, RequestContext, run_with_context
    from felix.mcp.client import UNAVAILABLE_SOURCES_TAG, unavailable_sources_note

    from tests.support.factories import make_settings

    ctx = RequestContext(settings=make_settings(), auth=AuthContext(tenant_id="default"))
    with run_with_context(ctx):
        note = unavailable_sources_note(["tracker", "calendar"])
    assert note.startswith(f"<{UNAVAILABLE_SOURCES_TAG}>")
    assert "`tracker`" in note and "`calendar`" in note and "unknown, not empty" in note
    assert ctx.extras == {}
