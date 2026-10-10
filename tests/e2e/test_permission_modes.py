"""Permission modes through the real stack: plan, the plan approval, accept_edits and bypass.

Each test drives `POST /chat/mode` and then a turn, and asserts on what actually happened to the
workspace or the approvals store -- a mode that only changed what the model was *told* would pass
any assertion about the reply.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

THREAD = "e2e-modes"
ADMIN = "sk-admin-not-a-secret"
DRIVER = "sk-driver-not-a-secret"


def _agent(*, approvals: list[dict[str, Any]] | None = None, **spec: Any) -> Any:
    base: dict[str, Any] = {
        "pattern": "react",
        "auth": {"inbound": {"allow_anonymous": True}},
        "tools": ["read_file", "write_file", "calculator"],
    }
    if approvals:
        base["approvals"] = approvals
    base.update(spec)
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "e2e-modes"}, "spec": base}
    )


def _gate(tool: str, ttl: int = 1) -> dict[str, Any]:
    return {"id": f"{tool}-gate", "tools": [tool], "ttl_seconds": ttl, "allow_unattended": True}


def _write(call_id: str = "w1", path: str = "out.txt") -> ScriptedTurn:
    return ScriptedTurn(
        tool_calls=[ToolCall(id=call_id, name="write_file", args={"path": path, "content": "x"})]
    )


def _calc(call_id: str = "c1") -> ScriptedTurn:
    return ScriptedTurn(tool_calls=[ToolCall(id=call_id, name="calculator", args={"expression": "1+1"})])


def _exit_plan(call_id: str = "p1") -> ScriptedTurn:
    return ScriptedTurn(
        tool_calls=[ToolCall(id=call_id, name="exit_plan_mode", args={"plan": "1. write out.txt"})]
    )


def _text(messages: list[Any]) -> str:
    return "\n".join(str(getattr(m, "content", "")) for m in messages)


def _written(root: Path) -> list[str]:
    return sorted(p.name for p in root.rglob("out.txt"))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "ws"
    path.mkdir()
    return path


async def _mode(app: Any, mode: str, token: str | None = None) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return await app.client.post("/chat/mode", json={"thread_id": THREAD, "mode": mode}, headers=headers)


async def _chat(app: Any, token: str | None = None) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return await app.client.post(
        "/chat",
        json={"manifest": "e2e-modes", "thread_id": THREAD, "messages": [{"role": "user", "content": "go"}]},
        headers=headers,
    )


async def _pending(app: Any, token: str | None = None) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    for _ in range(100):
        await asyncio.sleep(0.05)
        listing = (
            await app.client.get("/approvals", params={"thread_id": f"default:{THREAD}"}, headers=headers)
        ).json()
        pending = listing.get("approvals") or listing.get("items") or []
        if pending:
            return pending
    return []


# --- plan ------------------------------------------------------------------------------------


async def test_plan_mode_refuses_a_change_and_lets_reads_run(boot: Any, root: Path) -> None:
    script = [_write(), ScriptedTurn(content="I will propose it instead.")]
    async with boot(
        script, env={"FELIX_WORKSPACE_ROOT": str(root)}, manifests={"e2e-modes": _agent()}
    ) as app:
        assert (await _mode(app, "plan")).status_code == 200
        assert (await _chat(app)).status_code == 200
        assert "[plan mode] write_file can change things" in _text(app.spy.prompts[1])
        assert _written(root) == []
        snapshot = (await app.client.get(f"/chat/sessions/{THREAD}")).json()
        assert snapshot["permissionMode"] == "plan"


async def test_an_approved_plan_ends_plan_mode_and_the_run_carries_on(boot: Any, root: Path) -> None:
    """The plan goes through the approvals flow; once approved the same run may write."""
    script = [_exit_plan(), _write(), ScriptedTurn(content="done")]
    async with boot(
        script, env={"FELIX_WORKSPACE_ROOT": str(root)}, manifests={"e2e-modes": _agent()}
    ) as app:
        assert (await _mode(app, "plan")).status_code == 200
        chat = asyncio.create_task(_chat(app))
        [approval] = await _pending(app)
        assert approval["tool_name"] == "exit_plan_mode" and approval["rule_id"] == "plan-approval"
        decided = await app.client.post(f"/approvals/{approval['id']}/decide", json={"decision": "approved"})
        assert decided.status_code == 200, decided.text
        resp = await asyncio.wait_for(chat, timeout=30)
        assert resp.status_code == 200, resp.text
        assert "Plan approved." in _text(app.spy.prompts[1])
        assert _written(root) == ["out.txt"]
        snapshot = (await app.client.get(f"/chat/sessions/{THREAD}")).json()
        assert snapshot["permissionMode"] == "default"


async def test_a_rejected_plan_stays_in_plan_mode(boot: Any, root: Path) -> None:
    script = [_exit_plan(), _write(), ScriptedTurn(content="revising")]
    async with boot(
        script, env={"FELIX_WORKSPACE_ROOT": str(root)}, manifests={"e2e-modes": _agent()}
    ) as app:
        assert (await _mode(app, "plan")).status_code == 200
        chat = asyncio.create_task(_chat(app))
        [approval] = await _pending(app)
        await app.client.post(f"/approvals/{approval['id']}/decide", json={"decision": "denied"})
        assert (await asyncio.wait_for(chat, timeout=30)).status_code == 200
        assert "[plan mode] write_file" in _text(app.spy.prompts[2])
        assert _written(root) == []


async def test_plan_mode_binds_even_an_agent_that_does_not_offer_it(boot: Any, root: Path) -> None:
    """`plan` only narrows, so every agent honours it; `allowed_modes` only decides whether the
    agent offers a way out. Such a thread leaves plan mode through `POST /chat/mode`."""
    agent = _agent(permissions={"allowed_modes": ["default"]})
    script = [_write(), ScriptedTurn(content="could not")]
    async with boot(script, env={"FELIX_WORKSPACE_ROOT": str(root)}, manifests={"e2e-modes": agent}) as app:
        assert (await _mode(app, "plan")).status_code == 200
        assert (await _chat(app)).status_code == 200
        assert "which this agent cannot leave: ask the person driving it" in _text(app.spy.prompts[1])
        assert _written(root) == []
        assert "exit_plan_mode" not in app.spy.tools[0]


# --- accept_edits ----------------------------------------------------------------------------


EDITS = {"allowed_modes": ["default", "plan", "accept_edits"]}


async def test_accept_edits_waives_the_approval_on_a_workspace_edit_and_audits_it(
    boot: Any, root: Path
) -> None:
    from felix.approvals import store as approvals_store
    from felix.audit import store as audit_store
    from felix.flush import flush_all

    agent = _agent(approvals=[_gate("write_file")], permissions=EDITS)
    async with boot(
        [_write(), ScriptedTurn(content="ok")],
        env={"FELIX_WORKSPACE_ROOT": str(root)},
        manifests={"e2e-modes": agent},
    ) as app:
        assert (await _mode(app, "accept_edits")).status_code == 200
        assert (await _chat(app)).status_code == 200
        assert _written(root) == ["out.txt"]
        rows = await approvals_store.list_approvals(app.settings, "default", thread_id=f"default:{THREAD}")
        assert rows == [], "no approval was asked for"
        await flush_all(app.settings)
        events, _ = await audit_store.query(app.settings, "default", limit=50)
        waived = [e for e in events if e["event_type"] == "approval_waived"]
        assert [(e["status"], e["payload_json"]["tool"]) for e in waived] == [("accept_edits", "write_file")]


async def test_accept_edits_does_not_waive_a_gate_on_anything_else(boot: Any, root: Path) -> None:
    agent = _agent(approvals=[_gate("calculator")], permissions=EDITS)
    async with boot(
        [_calc(), ScriptedTurn(content="ok")],
        env={"FELIX_WORKSPACE_ROOT": str(root)},
        manifests={"e2e-modes": agent},
    ) as app:
        assert (await _mode(app, "accept_edits")).status_code == 200
        resp = await _chat(app)
        [approval] = resp.json()["approvals"]
        assert approval["tool_name"] == "calculator" and approval["status"] == "expired"


async def test_the_default_mode_asks_as_the_manifest_says(boot: Any, root: Path) -> None:
    agent = _agent(approvals=[_gate("write_file")])
    async with boot(
        [_write(), ScriptedTurn(content="ok")],
        env={"FELIX_WORKSPACE_ROOT": str(root)},
        manifests={"e2e-modes": agent},
    ) as app:
        resp = await _chat(app)
        [approval] = resp.json()["approvals"]
        assert approval["tool_name"] == "write_file" and approval["status"] == "expired"
        assert _written(root) == []


# --- bypass ----------------------------------------------------------------------------------


def _keys() -> dict[str, str]:
    return {
        "FELIX_AUTH_MODE": "api_key",
        "FELIX_AUTH_API_KEYS": json.dumps(
            {
                ADMIN: {"tenant_id": "default", "sub": "admin", "scopes": ["admin"]},
                DRIVER: {"tenant_id": "default", "sub": "driver", "scopes": ["approvals:read"]},
            }
        ),
    }


async def test_bypass_needs_the_scope_to_set(boot: Any, root: Path) -> None:
    agent = _agent(permissions={"allowed_modes": ["default", "bypass"]})
    async with boot(
        [], env={"FELIX_WORKSPACE_ROOT": str(root), **_keys()}, manifests={"e2e-modes": agent}
    ) as app:
        refused = await _mode(app, "bypass", DRIVER)
        assert refused.status_code == 403, refused.text
        assert "approvals:bypass" in refused.json()["detail"]
        assert (await _mode(app, "bypass", ADMIN)).status_code == 200


async def test_bypass_waives_every_approval_for_a_caller_holding_the_scope(boot: Any, root: Path) -> None:
    agent = _agent(approvals=[_gate("calculator")], permissions={"allowed_modes": ["default", "bypass"]})
    env = {"FELIX_WORKSPACE_ROOT": str(root), **_keys()}
    async with boot([_calc(), ScriptedTurn(content="ok")], env=env, manifests={"e2e-modes": agent}) as app:
        assert (await _mode(app, "bypass", ADMIN)).status_code == 200
        resp = await _chat(app, ADMIN)
        assert resp.status_code == 200, resp.text
        assert resp.json()["approvals"] == []
        assert "2" in _text(app.spy.prompts[1])


async def test_a_thread_left_in_bypass_is_not_bypassed_for_the_next_caller(boot: Any, root: Path) -> None:
    """The scope is checked on every run, not only when the mode was set."""
    agent = _agent(approvals=[_gate("calculator")], permissions={"allowed_modes": ["default", "bypass"]})
    env = {"FELIX_WORKSPACE_ROOT": str(root), **_keys()}
    async with boot([_calc(), ScriptedTurn(content="ok")], env=env, manifests={"e2e-modes": agent}) as app:
        assert (await _mode(app, "bypass", ADMIN)).status_code == 200
        resp = await _chat(app, DRIVER)
        assert resp.status_code == 200, resp.text
        [approval] = resp.json()["approvals"]
        assert approval["tool_name"] == "calculator"


async def test_bypass_the_manifest_does_not_allow_waives_nothing(boot: Any, root: Path) -> None:
    agent = _agent(approvals=[_gate("calculator")])
    env = {"FELIX_WORKSPACE_ROOT": str(root), **_keys()}
    async with boot([_calc(), ScriptedTurn(content="ok")], env=env, manifests={"e2e-modes": agent}) as app:
        assert (await _mode(app, "bypass", ADMIN)).status_code == 200
        [approval] = (await _chat(app, ADMIN)).json()["approvals"]
        assert approval["tool_name"] == "calculator"


# --- the schema ------------------------------------------------------------------------------


def test_bypass_cannot_be_a_default() -> None:
    from felix.manifests.loader import ManifestParseError

    with pytest.raises(ManifestParseError, match="cannot be bypass"):
        _agent(permissions={"default_mode": "bypass", "allowed_modes": ["default", "bypass"]})


# --- delegation ------------------------------------------------------------------------------


def _child(**spec: Any) -> Any:
    base: dict[str, Any] = {
        "pattern": "react",
        "auth": {"inbound": {"allow_anonymous": True}},
        "tools": ["calculator"],
    }
    base.update(spec)
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "e2e-child"}, "spec": base}
    )


def _task(background: bool = False) -> ScriptedTurn:
    args: dict[str, Any] = {"agent": "e2e-child", "prompt": "add"}
    if background:
        args["background"] = True
    return ScriptedTurn(tool_calls=[ToolCall(id="k1", name="task", args=args)])


async def test_a_parent_in_bypass_does_not_waive_its_childs_approvals(boot: Any, root: Path) -> None:
    """The child shares the run's mode, but a waiver needs the child's own manifest to allow it."""
    lead = _agent(
        permissions={"allowed_modes": ["default", "bypass"]},
        delegation={"agents": [{"name": "e2e-child"}]},
    )
    child = _child(approvals=[_gate("calculator")])
    env = {"FELIX_WORKSPACE_ROOT": str(root), **_keys()}
    script = [_task(), _calc(), ScriptedTurn(content="child done"), ScriptedTurn(content="lead done")]
    async with boot(script, env=env, manifests={"e2e-modes": lead, "e2e-child": child}) as app:
        assert (await _mode(app, "bypass", ADMIN)).status_code == 200
        resp = await _chat(app, ADMIN)
        assert resp.status_code == 200, resp.text
        assert [a["tool_name"] for a in resp.json()["approvals"]] == ["calculator"]


async def test_plan_mode_refuses_a_background_child(boot: Any, root: Path) -> None:
    """A worker run would not inherit plan mode, so it may not be started from one."""
    from felix.durability import fibers as F

    lead = _agent(delegation={"background": True, "agents": [{"name": "e2e-child"}]})
    script = [_task(background=True), ScriptedTurn(content="ok")]
    async with boot(
        script, env={"FELIX_WORKSPACE_ROOT": str(root)}, manifests={"e2e-modes": lead, "e2e-child": _child()}
    ) as app:
        assert (await _mode(app, "plan")).status_code == 200
        assert (await _chat(app)).status_code == 200
        assert "[task] plan mode" in _text(app.spy.prompts[1])
        assert F._memory_fibers == {}


# --- review findings -------------------------------------------------------------------------


async def test_accept_edits_is_opt_in(boot: Any, root: Path) -> None:
    """Each waiver mode waives approvals an author wrote, so a manifest must list it."""
    agent = _agent(approvals=[_gate("write_file")])
    async with boot(
        [_write(), ScriptedTurn(content="ok")],
        env={"FELIX_WORKSPACE_ROOT": str(root)},
        manifests={"e2e-modes": agent},
    ) as app:
        assert (await _mode(app, "accept_edits")).status_code == 200
        [approval] = (await _chat(app)).json()["approvals"]
        assert approval["tool_name"] == "write_file"
        assert _written(root) == []


async def test_edit_tools_and_read_only_tools_globs_count(boot: Any, root: Path) -> None:
    agent = _agent(
        approvals=[_gate("calculator")],
        permissions={**EDITS, "edit_tools": ["calc*"], "read_only_tools": ["calc*"]},
    )
    env = {"FELIX_WORKSPACE_ROOT": str(root)}
    async with boot([_calc(), ScriptedTurn(content="ok")], env=env, manifests={"e2e-modes": agent}) as app:
        assert (await _mode(app, "accept_edits")).status_code == 200
        assert (await _chat(app)).json()["approvals"] == [], "edit_tools waived the gate"
    plan_only = _agent(permissions={"read_only_tools": ["calc*"]})
    async with boot(
        [_calc(), ScriptedTurn(content="ok")], env=env, manifests={"e2e-modes": plan_only}
    ) as app:
        assert (await _mode(app, "plan")).status_code == 200
        assert (await _chat(app)).status_code == 200
        shown = _text(app.spy.prompts[1])
        assert "[plan mode]" not in shown and "2" in shown, "read_only_tools let it run"


async def test_an_approved_plan_is_not_a_key_for_another_thread(boot: Any, root: Path) -> None:
    """The grant is bound to the thread, the person and one use: the same plan text on another
    thread asks again rather than finding an approval waiting."""
    script = [_exit_plan(), ScriptedTurn(content="ok"), _exit_plan("p2"), ScriptedTurn(content="ok")]
    async with boot(
        script, env={"FELIX_WORKSPACE_ROOT": str(root)}, manifests={"e2e-modes": _agent()}
    ) as app:
        assert (await _mode(app, "plan")).status_code == 200
        chat = asyncio.create_task(_chat(app))
        [approval] = await _pending(app)
        await app.client.post(f"/approvals/{approval['id']}/decide", json={"decision": "approved"})
        assert (await asyncio.wait_for(chat, timeout=30)).status_code == 200

        other = "e2e-modes-other"
        await app.client.post("/chat/mode", json={"thread_id": other, "mode": "plan"})
        second = asyncio.create_task(
            app.client.post(
                "/chat",
                json={
                    "manifest": "e2e-modes",
                    "thread_id": other,
                    "messages": [{"role": "user", "content": "go"}],
                },
            )
        )
        # The same plan text on another thread found nothing to reuse: it is waiting on a person.
        asked: list[dict[str, Any]] = []
        for _ in range(100):
            await asyncio.sleep(0.05)
            listing = (await app.client.get("/approvals", params={"thread_id": f"default:{other}"})).json()
            asked = [
                a
                for a in (listing.get("approvals") or listing.get("items") or [])
                if a.get("status") == "pending"
            ]
            if asked:
                break
        assert [a["tool_name"] for a in asked] == ["exit_plan_mode"]
        await app.client.post(f"/approvals/{asked[0]['id']}/decide", json={"decision": "denied"})
        assert (await asyncio.wait_for(second, timeout=30)).status_code == 200
        snapshot = (await app.client.get(f"/chat/sessions/{other}")).json()
        assert snapshot["permissionMode"] == "plan"


async def test_a_router_child_that_does_not_offer_plan_mode_is_still_held_to_it(
    boot: Any, root: Path
) -> None:
    """The run caches only the stored mode; each agent resolves its own. A child not offering plan
    used to cache its default for the whole run, and plan mode failed open for everyone after it."""
    child = parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "e2e-writer"},
            "spec": {
                "pattern": "react",
                "auth": {"inbound": {"allow_anonymous": True}},
                "tools": ["write_file"],
                "permissions": {"allowed_modes": ["default"]},
            },
        }
    )
    router = _agent(pattern="router", sub_agents=["e2e-writer"], tools=[])
    script = [ScriptedTurn(content="e2e-writer"), _write(), ScriptedTurn(content="blocked")]
    env = {"FELIX_WORKSPACE_ROOT": str(root)}
    async with boot(script, env=env, manifests={"e2e-modes": router, "e2e-writer": child}) as app:
        assert (await _mode(app, "plan")).status_code == 200
        assert (await _chat(app)).status_code == 200
        assert _written(root) == []
        assert "[plan mode] write_file" in _text(app.spy.prompts[-1])


async def test_an_unreadable_mode_fails_closed(
    boot: Any, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from felix.governance import permission_mode

    async def broken(**_: Any) -> Any:
        raise RuntimeError("meta store down")

    async with boot(
        [_write(), ScriptedTurn(content="ok")],
        env={"FELIX_WORKSPACE_ROOT": str(root)},
        manifests={"e2e-modes": _agent()},
    ) as app:
        # Only the mode's read: the rest of the turn reads thread meta as usual.
        monkeypatch.setattr(permission_mode, "get_thread_meta", broken)
        assert (await _chat(app)).status_code == 200
        assert _written(root) == []
        assert "[plan mode] write_file" in _text(app.spy.prompts[1])


async def test_a_scoped_caller_may_set_a_mode_other_than_bypass_and_bypass_is_audited(
    boot: Any, root: Path
) -> None:
    from felix.audit import store as audit_store
    from felix.flush import flush_all

    agent = _agent(permissions={"allowed_modes": ["default", "plan", "bypass"]})
    async with boot(
        [], env={"FELIX_WORKSPACE_ROOT": str(root), **_keys()}, manifests={"e2e-modes": agent}
    ) as app:
        assert (await _mode(app, "plan", DRIVER)).status_code == 200
        assert (await _mode(app, "bypass", ADMIN)).status_code == 200
        await flush_all(app.settings)
        events, _ = await audit_store.query(app.settings, "default", limit=50)
        changes = [
            (e["status"], e["principal_subj"]) for e in events if e["event_type"] == "permission_mode_change"
        ]
        assert sorted(changes) == [("bypass", "admin"), ("plan", "driver")]


async def test_a_durable_run_is_held_to_plan_mode(boot: Any, root: Path) -> None:
    """The worker rebuilds the run; the mode is read off the thread there too."""
    from felix.durability import fibers as F

    agent = _agent(execution={"mode": "durable"})
    async with boot(
        [_write(), ScriptedTurn(content="ok")],
        env={"FELIX_WORKSPACE_ROOT": str(root)},
        manifests={"e2e-modes": agent},
    ) as app:
        assert (await _mode(app, "plan")).status_code == 200
        assert (await _chat(app)).status_code == 202
        await F.resume_due_fibers(app.settings)
        assert _written(root) == []
        assert "[plan mode] write_file" in _text(app.spy.prompts[1])


async def test_a_plan_grant_is_bound_to_its_thread() -> None:
    """Defence in depth beside one_shot and bind_principal: the same plan text signs differently on
    two threads, so a grant left unconsumed on one -- a run that died after its approval -- is not
    found by the other."""
    from felix.config import Settings
    from felix.context import AuthContext, RequestContext, async_run_with_context
    from felix.governance.permission_mode import make_exit_plan_tool, plan_approval_rule
    from felix.manifests.schema import PermissionsSpec

    tool = make_exit_plan_tool(PermissionsSpec())
    assert tool.approval_binding is not None
    settings = Settings(
        database_url="memory://pm", object_store="memory", allow_insecure=True, auth_mode="none"
    )
    seen = []
    for thread in ("default:a", "default:b"):
        ctx = RequestContext(settings=settings, auth=AuthContext(tenant_id="default"), thread_id=thread)
        async with async_run_with_context(ctx):
            seen.append(await tool.approval_binding({"plan": "Proceed."}))
    assert seen == ["default:a", "default:b"]
    rule = plan_approval_rule()
    assert (rule.one_shot, rule.bind_principal, rule.ttl_seconds) == (True, True, 1800)
