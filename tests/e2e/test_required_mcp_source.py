"""A run whose `required: true` MCP server cannot be used stops, instead of running without it.

Through the production app, as `test_unreachable_mcp_source.py` stages it: the server is faked at
the discovery call, the one place a compile dials it. An optional server that is down is named to
the model and the run goes on (#732); a required one stops the compile before any model call, and
every surface answers with the same written refusal -- a job's run is an error that counts toward
the health watch's `failing` streak, a request is a 503, a durable run fails -- with the sources
still on the job run's record.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix.run_sources import UNAVAILABLE_SOURCES_TAG
from felix_ai.providers.scripted import ScriptedTurn

from tests.support.sse import sse_payloads

KEYS = {"sk-e2e-alice": {"tenant_id": "default", "sub": "alice", "scopes": ["jobs:write", "jobs:read"]}}
JOBS_ENV = {"FELIX_AUTH_MODE": "api_key", "FELIX_AUTH_API_KEYS": json.dumps(KEYS)}
ALICE = {"Authorization": "Bearer sk-e2e-alice"}


def _names_tracker(message: str) -> None:
    """The refusal, by its fixed opening and the server it names. The whole sentence is pinned once,
    in `tests/unit/test_mcp_unreachable_sources.py`; a surface only has to carry it."""
    assert message.startswith("required tool sources unavailable for `e2e-brief`"), message
    assert message.endswith("so the run did not start: `tracker`"), message


CODE = "required_source_unavailable"
ASK = [{"role": "user", "content": "What is new in the tracker?"}]


def _manifest(*, required: bool = True, **spec: Any) -> Any:
    refs = [
        {
            "name": "tracker",
            "url": "https://tracker.example.com/mcp",
            "transport": "http",
            "required": required,
        },
        {"name": "channel", "url": "https://channel.example.com/mcp", "transport": "http"},
    ]
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "e2e-brief"},
            "spec": {
                "pattern": "react",
                "mcp_servers": refs,
                "auth": {"inbound": {"allow_anonymous": True}},
                **spec,
            },
        }
    )


@pytest.fixture(autouse=True)
def _tracker_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.mcp import client

    async def fake_list(ref: Any, *, allow_http: bool = False, handshake: Any = None) -> list[Any]:
        if "tracker.example.com" in ref.url:
            raise ConnectionError("dial tcp 10.0.0.7:443: connection refused")
        return [{"name": "search", "inputSchema": {"type": "object", "properties": {}}}]

    client.clear_discovery_cache()
    monkeypatch.setattr(client, "list_remote_tools", fake_list)


async def _write_job(app: Any) -> None:
    job = {"schedule": "", "manifest_id": "e2e-brief", "payload": {"prompt": "Write today's brief."}}
    written = await app.client.put("/jobs/brief", json=job, headers=ALICE)
    assert written.status_code == 200, written.text


async def _fire(app: Any) -> dict[str, Any]:
    fired = await app.client.post("/jobs/brief/run", headers=ALICE)
    assert fired.status_code == 200, fired.text
    return fired.json()


async def test_a_job_firing_is_an_error_run_with_no_model_call(boot: Any) -> None:
    async with boot([ScriptedTurn(content="Nothing new in the tracker.")], env=JOBS_ENV) as app:
        from felix.manifests.store import put_version

        await put_version(app.settings, "default", "e2e-brief", _manifest())
        await _write_job(app)
        run = await _fire(app)
        prompts = app.spy.prompts

    assert run["status"] == "error", run
    _names_tracker(run["result"]["error"])
    assert run["result"]["unavailable_sources"] == {"e2e-brief": ["tracker"]}
    assert prompts == [], "the run stopped before any model call"


async def test_repeated_refusals_open_the_watchs_failing_incident(boot: Any) -> None:
    env = {**JOBS_ENV, "FELIX_JOB_FAILURE_THRESHOLD": "3"}
    async with boot([], env=env) as app:
        from felix.jobs.watch import run_job_health_sweep_all_tenants
        from felix.manifests.store import put_version

        await put_version(app.settings, "default", "e2e-brief", _manifest())
        await _write_job(app)
        for _ in range(3):
            assert (await _fire(app))["status"] == "error"
        await run_job_health_sweep_all_tenants(app.settings)
        job = await app.client.get("/jobs/brief", headers=ALICE)

    assert job.status_code == 200, job.text
    failing = job.json()["health"]["failing"]
    assert failing["error_class"] == "required_source_unavailable", failing


async def test_a_chat_turn_is_a_written_503(boot: Any) -> None:
    async with boot([ScriptedTurn(content="Nothing new.")], manifests={"e2e-brief": _manifest()}) as app:
        answer = await app.client.post("/chat", json={"manifest": "e2e-brief", "messages": ASK})
        prompts = app.spy.prompts

    assert answer.status_code == 503, answer.text
    _names_tracker(answer.json()["detail"])
    assert prompts == []


async def test_a_streamed_chat_turn_is_a_typed_error_frame(boot: Any) -> None:
    async with boot([ScriptedTurn(content="Nothing new.")], manifests={"e2e-brief": _manifest()}) as app:
        answer = await app.client.post("/chat/stream", json={"manifest": "e2e-brief", "messages": ASK})
        prompts = app.spy.prompts

    assert answer.status_code == 200, answer.text
    (error,) = [f["error"] for f in sse_payloads(answer.text) if "error" in f]
    assert error["type"] == CODE, error
    _names_tracker(error["message"])
    assert prompts == []


@pytest.mark.parametrize("stream", [False, True])
async def test_an_openai_compatible_call_is_refused_the_same_way(boot: Any, stream: bool) -> None:
    async with boot([ScriptedTurn(content="Nothing new.")], manifests={"e2e-brief": _manifest()}) as app:
        answer = await app.client.post(
            "/v1/chat/completions", json={"model": "e2e-brief", "messages": ASK, "stream": stream}
        )
        prompts = app.spy.prompts

    if stream:
        assert answer.status_code == 200, answer.text
        (error,) = [f["error"] for f in sse_payloads(answer.text) if "error" in f]
    else:
        assert answer.status_code == 503, answer.text
        error = answer.json()["error"]
    assert error["code"] == CODE, error
    _names_tracker(error["message"])
    assert prompts == []


async def test_an_a2a_task_fails_with_the_written_refusal(boot: Any) -> None:
    send = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/send",
        "params": {
            "manifest": "e2e-brief",
            "taskId": "t1",
            "message": {"role": "user", "parts": [{"kind": "text", "text": "What is new?"}]},
        },
    }
    async with boot([ScriptedTurn(content="Nothing new.")], manifests={"e2e-brief": _manifest()}) as app:
        answer = await app.client.post("/a2a", json=send)
        prompts = app.spy.prompts

    assert answer.status_code == 200, answer.text
    status = answer.json()["result"]["status"]
    assert status["state"] == "failed", status
    _names_tracker(status["message"])
    assert prompts == []


async def test_an_mcp_client_listing_the_agents_tools_is_told(boot: Any) -> None:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"manifest": "e2e-brief"}}
    async with boot([], manifests={"e2e-brief": _manifest()}) as app:
        answer = await app.client.post(
            "/mcp", json=body, headers={"Accept": "application/json, text/event-stream"}
        )

    assert answer.status_code == 200, answer.text
    error = answer.json()["error"]
    # JSON-RPC's internal error, the code `mcp/server.py` answers any failed compile with.
    assert error["code"] == -32603, error
    _names_tracker(error["message"])


async def test_a_durable_run_fails_with_the_written_refusal(boot: Any) -> None:
    manifest = _manifest(execution={"mode": "durable"})
    async with boot([ScriptedTurn(content="Nothing new.")], manifests={"e2e-brief": manifest}) as app:
        from felix.durability.fibers import resume_due_fibers

        enqueued = await app.client.post("/chat", json={"manifest": "e2e-brief", "messages": ASK})
        assert enqueued.status_code == 202, enqueued.text
        token = enqueued.json()["resume_token"]
        await resume_due_fibers(app.settings)
        run = (await app.client.get(f"/chat/runs/{token}")).json()
        prompts = app.spy.prompts

    assert run["status"] == "failed", run
    _names_tracker(run["error"])
    assert prompts == []


async def test_an_optional_server_that_is_down_still_runs_with_the_note(boot: Any) -> None:
    script = [ScriptedTurn(content="The tracker could not be read.")]
    async with boot(script, manifests={"e2e-brief": _manifest(required=False)}) as app:
        answer = await app.client.post("/chat", json={"manifest": "e2e-brief", "messages": ASK})
        (prompt,) = app.spy.prompts

    assert answer.status_code == 200, answer.text
    system = next(str(m.content) for m in prompt if getattr(m, "role", "") == "system")
    assert f"<{UNAVAILABLE_SOURCES_TAG}>" in system and "`tracker`" in system


async def test_a_childs_required_source_stops_the_parents_run_under_the_childs_name(boot: Any) -> None:
    """A delegating agent compiles its child for the same run; the child's required server down
    stops the whole run, named and recorded as the child's, not the parent's."""
    refs = [
        {"name": "ledger", "url": "https://tracker.example.com/mcp", "transport": "http", "required": True}
    ]
    child = parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "e2e-billing"},
            "spec": {"pattern": "react", "mcp_servers": refs},
        }
    )
    # `tracker.example.com` is down in this file's fake, whatever the server is called.
    parent = parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "e2e-brief"},
            "spec": {"pattern": "react", "delegation": {"agents": [{"name": "e2e-billing"}]}},
        }
    )
    async with boot([ScriptedTurn(content="Done.")], env=JOBS_ENV) as app:
        from felix.manifests.store import put_version

        await put_version(app.settings, "default", "e2e-billing", child)
        await put_version(app.settings, "default", "e2e-brief", parent)
        await _write_job(app)
        run = await _fire(app)
        prompts = app.spy.prompts

    assert run["status"] == "error", run
    assert run["result"]["error"].startswith("required tool sources unavailable for `e2e-billing`"), run
    assert run["result"]["error"].endswith(": `ledger`"), run
    assert run["result"]["unavailable_sources"] == {"e2e-billing": ["ledger"]}
    assert prompts == []
