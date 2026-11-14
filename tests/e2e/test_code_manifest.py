"""The bundled coding agent through authenticated HTTP, approvals and durable execution."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

import pytest
from felix.durability.fibers import resume_due_fibers
from felix.tools import client_requests
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

KEY = "sk-code-e2e-not-a-secret"
AUTH = {"Authorization": f"Bearer {KEY}"}
ENV = {
    "FELIX_AUTH_MODE": "api_key",
    "FELIX_AUTH_API_KEYS": json.dumps(
        {KEY: {"tenant_id": "default", "sub": "developer", "scopes": ["admin"]}}
    ),
}


async def test_code_is_discoverable_as_a_client_workspace(boot: Any) -> None:
    async with boot([], env=ENV) as app:
        response = await app.client.get("/v1/models", headers=AUTH)
    assert response.status_code == 200, response.text
    entry = next(item for item in response.json()["data"] if item["id"] == "code")
    assert entry["felix"]["workspace"]["tools"] == "client"
    assert entry["felix"]["greeting"]["headline"] == "What should we build or fix?"
    assert any(item["title"] == "Fix a failing test" for item in entry["felix"]["starters"])


async def test_code_refuses_anonymous_execution(boot: Any) -> None:
    async with boot([], env={"FELIX_AUTH_MODE": "none"}) as app:
        response = await app.client.post(
            "/chat", json={"manifest": "code", "messages": [{"role": "user", "content": "run tests"}]}
        )
        assert response.status_code == 401, response.text
        assert app.spy.calls == []


@pytest.mark.parametrize(
    ("decision", "client_error"), [("approved", False), ("approved", True), ("denied", False)]
)
async def test_code_shell_waits_for_approval_and_client_result(
    boot: Any, decision: str, client_error: bool
) -> None:
    call = ToolCall(id="code-test-command", name="local_shell", args={"command": "npm test", "cwd": "src"})
    script = [
        ScriptedTurn(content="", tool_calls=[call], stop_reason="tool_use"),
        ScriptedTurn(content="Done."),
    ]
    async with boot(script, env=ENV) as app:
        response = await app.client.post(
            "/chat",
            headers=AUTH,
            json={
                "manifest": "code",
                "thread_id": "coding",
                "messages": [{"role": "user", "content": "run tests"}],
            },
        )
        assert response.status_code == 202, response.text
        token = response.json()["resume_token"]
        worker = asyncio.create_task(resume_due_fibers(app.settings))
        try:
            pending: list[dict[str, Any]] = []
            for _ in range(300):
                listing = await app.client.get(
                    "/approvals", headers=AUTH, params={"thread_id": "default:coding"}
                )
                assert listing.status_code == 200, listing.text
                pending = listing.json().get("approvals") or listing.json().get("items") or []
                if pending:
                    break
                await asyncio.sleep(0.01)
            assert pending, "the bundled code shell did not ask for approval"
            assert not await client_requests.pending("default:coding"), (
                "the command escaped its approval gate"
            )
            assert pending[0]["tool_name"] == "local_shell"
            response = await app.client.post(
                f"/approvals/{pending[0]['id']}/decide", headers=AUTH, json={"decision": decision}
            )
            assert response.status_code == 200, response.text
            if decision == "approved":
                requests = []
                for _ in range(300):
                    requests = await client_requests.pending("default:coding")
                    if requests:
                        break
                    await asyncio.sleep(0.01)
                assert requests, "approved command never reached the client bridge"
                assert requests[0]["name"] == "local_shell"
                assert requests[0]["args"] == call.args
                response = await app.client.post(
                    "/chat/tool_result",
                    headers=AUTH,
                    json={
                        "thread_id": "coding",
                        "tool_call_id": requests[0]["id"],
                        "content": "build failed" if client_error else "1 test passed",
                        "error": client_error,
                    },
                )
                assert response.status_code == 200, response.text
                assert response.json()["signaled"]
            await asyncio.wait_for(worker, timeout=10)
            run = await app.client.get(f"/chat/runs/{token}", headers=AUTH)
            assert run.status_code == 200, run.text
            assert run.json()["status"] == "completed", run.text
            assert run.json()["final"]["content"] == "Done."
            assert not await client_requests.pending("default:coding")
            if client_error:
                assert "build failed" in str(app.spy.prompts[-1])
                assert "provider_error" in str(app.spy.prompts[-1]), (
                    "a failed command reached the model as success"
                )
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker


async def test_code_recovers_after_a_worker_claim_lapses(boot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The agent's real expiry leaves enough authority for the worker lease to lapse."""
    from felix.durability import fibers as F

    async with boot([ScriptedTurn(content="Recovered.")], env=ENV) as app:
        response = await app.client.post(
            "/chat",
            headers=AUTH,
            json={
                "manifest": "code",
                "thread_id": "recover-code",
                "messages": [{"role": "user", "content": "inspect the project"}],
            },
        )
        assert response.status_code == 202, response.text
        token = response.json()["resume_token"]
        clock = {"now": F.now_ms()}
        monkeypatch.setattr(F, "now_ms", lambda: clock["now"])
        (stale,) = await F._claim_due(app.settings, 1)
        assert stale["id"] == token
        clock["now"] += F.FIBER_LEASE_MS + 1
        await F.resume_due_fibers(app.settings)
        response = await app.client.get(f"/chat/runs/{token}", headers=AUTH)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "completed", response.text
        assert response.json()["final"]["content"] == "Recovered."
        assert len(app.spy.prompts) == 1
        # A stale worker returning after recovery cannot run it a second time.
        await F._advance_claimed(app.settings, stale)
        assert len(app.spy.prompts) == 1
