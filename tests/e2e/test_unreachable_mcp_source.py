"""A scheduled run whose MCP server is down says so, rather than reporting a quiet day.

Through the production app: a job on a manifest with two MCP servers, one of which cannot be
reached when the firing compiles, fired by `POST /jobs/{name}/run` -- the path cron takes. The
server is faked at the discovery call, the one place a compile dials it. Before this, the
unreachable server bound no tools and nothing else changed: the model was never told a source
was missing, so "what is new in the tracker" came back "nothing new", and the run recorded `ok`
with nothing to tell an outage from a quiet day.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix.mcp.client import UNAVAILABLE_SOURCES_TAG
from felix_ai.providers.scripted import ScriptedTurn

KEYS = {"sk-e2e-alice": {"tenant_id": "default", "sub": "alice", "scopes": ["jobs:write", "jobs:read"]}}
ENV = {"FELIX_AUTH_MODE": "api_key", "FELIX_AUTH_API_KEYS": json.dumps(KEYS)}
ALICE = {"Authorization": "Bearer sk-e2e-alice"}
TAG = f"<{UNAVAILABLE_SOURCES_TAG}>"


def _manifest(name: str = "e2e-brief", servers: tuple[str, ...] = ("tracker", "channel"), **spec: Any) -> Any:
    refs = [{"name": s, "url": f"https://{s}.example.com/mcp", "transport": "http"} for s in servers]
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": name},
            "spec": {"pattern": "react", "mcp_servers": refs, **spec},
        }
    )


def _servers(monkeypatch: pytest.MonkeyPatch, *, down: set[str]) -> None:
    from felix.mcp import client

    async def fake_list(ref: Any, *, allow_http: bool = False, handshake: Any = None) -> list[Any]:
        if ref.name in down:
            raise ConnectionError(f"{ref.name} is down")
        return [{"name": "search", "inputSchema": {"type": "object", "properties": {}}}]

    client.clear_discovery_cache()
    monkeypatch.setattr(client, "list_remote_tools", fake_list)


async def _fire(app: Any) -> dict[str, Any]:
    job = {"schedule": "", "manifest_id": "e2e-brief", "payload": {"prompt": "Write today's brief."}}
    written = await app.client.put("/jobs/brief", json=job, headers=ALICE)
    assert written.status_code == 200, written.text
    fired = await app.client.post("/jobs/brief/run", headers=ALICE)
    assert fired.status_code == 200, fired.text
    return fired.json()


def _system(app: Any) -> str:
    (prompt,) = app.spy.prompts
    return next(str(m.content) for m in prompt if getattr(m, "role", "") == "system")


async def test_a_source_that_is_down_is_named_to_the_model_and_on_the_run(
    boot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _servers(monkeypatch, down={"tracker"})
    async with boot([ScriptedTurn(content="Tracker unavailable this run.")], env=ENV) as app:
        from felix.manifests.store import put_version

        await put_version(app.settings, "default", "e2e-brief", _manifest())
        run = await _fire(app)
        system = _system(app)
        offered = app.spy.tools[0]

    assert run["status"] == "ok", run
    assert run["result"]["unavailable_sources"] == {"e2e-brief": ["tracker"]}
    note = system[system.index(TAG) :]
    assert "`tracker`" in note and "`channel`" not in note, note
    assert "channel__search" in offered and not any(t.startswith("tracker__") for t in offered), offered


async def test_with_every_source_up_nothing_is_said(boot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _servers(monkeypatch, down=set())
    async with boot([ScriptedTurn(content="Nothing new today.")], env=ENV) as app:
        from felix.manifests.store import put_version

        await put_version(app.settings, "default", "e2e-brief", _manifest())
        run = await _fire(app)
        system = _system(app)

    assert run["status"] == "ok", run
    assert "unavailable_sources" not in run["result"]
    assert TAG not in system


async def test_a_run_that_fails_still_names_the_source(boot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The missing source may be why it failed, so the failed run's record is the one that most
    needs to say so. An empty script: the model has nothing to answer with, and the firing raises."""
    _servers(monkeypatch, down={"tracker"})
    async with boot([], env=ENV) as app:
        from felix.manifests.store import put_version

        await put_version(app.settings, "default", "e2e-brief", _manifest())
        run = await _fire(app)

    assert run["status"] == "error", run
    assert run["result"]["unavailable_sources"] == {"e2e-brief": ["tracker"]}


async def test_a_childs_source_is_recorded_under_the_child(
    boot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delegating agent compiles its child for the same run. The child's lost server is the
    child's: the record names it under the child, and the parent's own prompt says nothing."""
    _servers(monkeypatch, down={"ledger"})
    parent = _manifest(servers=("channel",), delegation={"agents": [{"name": "e2e-billing"}]})
    child = _manifest(name="e2e-billing", servers=("ledger",))
    async with boot([ScriptedTurn(content="Done.")], env=ENV) as app:
        from felix.manifests.store import put_version

        await put_version(app.settings, "default", "e2e-billing", child)
        await put_version(app.settings, "default", "e2e-brief", parent)
        run = await _fire(app)
        system = _system(app)

    assert run["status"] == "ok", run
    assert run["result"]["unavailable_sources"] == {"e2e-billing": ["ledger"]}
    assert TAG not in system
