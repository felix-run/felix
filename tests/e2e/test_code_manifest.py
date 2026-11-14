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


async def test_code_persists_and_recovers_its_progress(boot: Any) -> None:
    from felix.plans import store as plans_store

    calls = [
        ToolCall(
            id="create",
            name="plan_create",
            args={
                "plan_id": "coding-plan",
                "title": "Fix the build",
                "steps": [{"id": "check", "title": "Run relevant checks"}],
            },
        ),
        ToolCall(
            id="update",
            name="plan_update_step",
            args={
                "plan_id": "coding-plan",
                "step_id": "check",
                "status": "done",
                "note": "Tests passed",
            },
        ),
        ToolCall(id="recover", name="plan_get", args={}),
    ]
    script = [ScriptedTurn(content="", tool_calls=[call], stop_reason="tool_use") for call in calls]
    script.insert(2, ScriptedTurn(content="Checks finished. We can continue later."))
    script.append(ScriptedTurn(content="Verified."))
    async with boot(script, env=ENV) as app:
        response = await app.client.post(
            "/chat",
            headers=AUTH,
            json={
                "manifest": "code",
                "thread_id": "code-plan",
                "messages": [{"role": "user", "content": "Fix the build"}],
            },
        )
        assert response.status_code == 202, response.text
        await asyncio.wait_for(resume_due_fibers(app.settings), timeout=10)
        run = await app.client.get(f"/chat/runs/{response.json()['resume_token']}", headers=AUTH)
        assert run.json()["status"] == "completed", run.text
        # A fresh durable run must recover persisted progress, rather than relying
        # on the first run's in-memory agent or its tool results.
        response = await app.client.post(
            "/chat",
            headers=AUTH,
            json={
                "manifest": "code",
                "thread_id": "code-plan",
                "messages": [{"role": "user", "content": "Continue from the saved plan"}],
            },
        )
        assert response.status_code == 202, response.text
        await asyncio.wait_for(resume_due_fibers(app.settings), timeout=10)
        run = await app.client.get(f"/chat/runs/{response.json()['resume_token']}", headers=AUTH)
        assert run.json()["status"] == "completed", run.text
        assert run.json()["final"]["content"] == "Verified."
        plan = await plans_store.get_plan(app.settings, "default", "coding-plan")
        assert plan is not None
        assert plan["thread_id"] == "default:code-plan"
        assert plan["plan"]["steps"][0]["status"] == "done"
        assert "Tests passed" in str(app.spy.prompts[-1])
        assert {call.name for call in calls} <= set(app.spy.tools[0])


@pytest.mark.parametrize(
    "name", ["local_info", "local_instructions", "local_list", "local_read", "local_search"]
)
async def test_code_can_inspect_files_in_plan_mode(boot: Any, name: str) -> None:
    args = {
        "local_info": {},
        "local_instructions": {"path": "src/new.py"},
        "local_list": {},
        "local_read": {"path": "AGENTS.md"},
        "local_search": {"query": "TODO"},
    }[name]
    script = [
        ScriptedTurn(
            content="", tool_calls=[ToolCall(id="inspect", name=name, args=args)], stop_reason="tool_use"
        ),
        ScriptedTurn(content="Proposed plan."),
    ]
    async with boot(script, env=ENV) as app:
        mode = await app.client.post(
            "/chat/mode", headers=AUTH, json={"thread_id": "plan-code", "mode": "plan"}
        )
        assert mode.status_code == 200, mode.text
        response = await app.client.post(
            "/chat",
            headers=AUTH,
            json={
                "manifest": "code",
                "thread_id": "plan-code",
                "messages": [{"role": "user", "content": "Inspect and propose"}],
            },
        )
        assert response.status_code == 202, response.text
        worker = asyncio.create_task(resume_due_fibers(app.settings))
        try:
            for _ in range(500):
                pending = await client_requests.pending("default:plan-code")
                if pending:
                    break
                await asyncio.sleep(0.01)
            assert pending, "plan-mode inspection never reached the client"
            assert pending[0]["name"] == name
            result = await app.client.post(
                "/chat/tool_result",
                headers=AUTH,
                json={
                    "thread_id": "plan-code",
                    "tool_call_id": pending[0]["id"],
                    "content": "project instructions",
                },
            )
            assert result.status_code == 200, result.text
            await asyncio.wait_for(worker, timeout=10)
            assert "project instructions" in str(app.spy.prompts[-1])
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker


async def test_code_is_discoverable_as_a_client_workspace(boot: Any) -> None:
    async with boot([], env=ENV) as app:
        response = await app.client.get("/v1/models", headers=AUTH)
    assert response.status_code == 200, response.text
    entry = next(item for item in response.json()["data"] if item["id"] == "code")
    assert entry["felix"]["workspace"]["tools"] == "client"
    assert entry["felix"]["greeting"]["headline"] == "What should we build or fix?"
    assert any(item["title"] == "Fix a failing test" for item in entry["felix"]["starters"])


@pytest.mark.parametrize(
    ("name", "args"),
    [("local_write", {"path": "new.txt", "content": "changed"}), ("local_shell", {"command": "npm test"})],
)
async def test_code_plan_mode_blocks_changes_before_client_dispatch(
    boot: Any, name: str, args: dict[str, Any]
) -> None:
    script = [
        ScriptedTurn(
            content="", tool_calls=[ToolCall(id="change", name=name, args=args)], stop_reason="tool_use"
        ),
        ScriptedTurn(content="Awaiting plan approval."),
    ]
    async with boot(script, env=ENV) as app:
        mode = await app.client.post(
            "/chat/mode", headers=AUTH, json={"thread_id": "blocked-code", "mode": "plan"}
        )
        assert mode.status_code == 200, mode.text
        response = await app.client.post(
            "/chat",
            headers=AUTH,
            json={
                "manifest": "code",
                "thread_id": "blocked-code",
                "messages": [{"role": "user", "content": "Propose a change"}],
            },
        )
        assert response.status_code == 202, response.text
        await asyncio.wait_for(resume_due_fibers(app.settings), timeout=10)
        assert f"[plan mode] {name} can change things" in str(app.spy.prompts[-1])
        assert not await client_requests.pending("default:blocked-code")


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
