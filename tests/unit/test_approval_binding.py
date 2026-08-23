"""`bind_principal` and `one_shot` were declared in the schema and enforced nowhere.

`find_approved` matched only (tenant, manifest, tool, call_signature, status) and never
consumed the grant, so:

* principal A's approval auto-approved principal B's byte-identical call, and
* one approval authorized unlimited replays until it expired.

A manifest author reading `ApprovalRule` had every reason to believe otherwise.
"""

from __future__ import annotations

import pytest
from felix.approvals import store as approvals_store
from felix.config import Settings


def _settings() -> Settings:
    return Settings(
        database_url="memory://approvals",
        object_store="memory",
        allow_insecure=True,
        auth_mode="none",
        environment="development",
    )


@pytest.fixture(autouse=True)
def _clean() -> None:
    approvals_store._memory_approvals.clear()


async def _grant(s: Settings, *, principal: str, sig: str = "abc") -> dict:
    row = await approvals_store.create_pending(
        s,
        "t1",
        manifest_id="m",
        tool_name="shell",
        call_signature=sig,
        args={"command": "deploy"},
        principal_subj=principal,
        rule_id="r1",
    )
    await approvals_store.decide(s, "t1", str(row["id"]), decision="approved", decided_by="approver")
    return row


# --- bind_principal -------------------------------------------------------------


@pytest.mark.asyncio
async def test_grant_is_reusable_across_principals_when_unbound() -> None:
    """Documented behaviour when bind_principal is false — unchanged."""
    s = _settings()
    await _grant(s, principal="alice")
    found = await approvals_store.find_approved(
        s, "t1", manifest_id="m", tool_name="shell", call_signature="abc"
    )
    assert found is not None


@pytest.mark.asyncio
async def test_bind_principal_blocks_a_different_principal() -> None:
    """The privilege escalation: B replaying A's approved call."""
    s = _settings()
    await _grant(s, principal="alice")
    found = await approvals_store.find_approved(
        s,
        "t1",
        manifest_id="m",
        tool_name="shell",
        call_signature="abc",
        principal_subj="bob",
    )
    assert found is None


@pytest.mark.asyncio
async def test_bind_principal_allows_the_original_principal() -> None:
    s = _settings()
    await _grant(s, principal="alice")
    found = await approvals_store.find_approved(
        s,
        "t1",
        manifest_id="m",
        tool_name="shell",
        call_signature="abc",
        principal_subj="alice",
    )
    assert found is not None


# --- one_shot -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_shot_grant_is_spent_after_use() -> None:
    s = _settings()
    row = await _grant(s, principal="alice")

    first = await approvals_store.find_approved(
        s, "t1", manifest_id="m", tool_name="shell", call_signature="abc", unconsumed_only=True
    )
    assert first is not None
    assert await approvals_store.consume_approval(s, "t1", str(row["id"])) is True

    second = await approvals_store.find_approved(
        s, "t1", manifest_id="m", tool_name="shell", call_signature="abc", unconsumed_only=True
    )
    assert second is None, "a one_shot grant must not authorize a replay"


@pytest.mark.asyncio
async def test_consume_is_single_winner() -> None:
    """Two concurrent identical calls must not both spend one grant."""
    s = _settings()
    row = await _grant(s, principal="alice")
    first = await approvals_store.consume_approval(s, "t1", str(row["id"]))
    second = await approvals_store.consume_approval(s, "t1", str(row["id"]))
    assert (first, second) == (True, False)


@pytest.mark.asyncio
async def test_consumed_grant_still_visible_without_the_flag() -> None:
    """Consumption only gates one_shot rules; ordinary grants are unaffected."""
    s = _settings()
    row = await _grant(s, principal="alice")
    await approvals_store.consume_approval(s, "t1", str(row["id"]))
    found = await approvals_store.find_approved(
        s, "t1", manifest_id="m", tool_name="shell", call_signature="abc"
    )
    assert found is not None


# --- command screening `require_approval` ---------------------------------------


@pytest.mark.asyncio
async def test_command_require_approval_creates_a_real_approval() -> None:
    """It used to return a deny string that named an approval nobody ever created.

    The bundled default rule for `sudo` therefore told the model to go ask a human who
    was never asked. Now a pending row exists and the run blocks on a real decision.
    """
    import asyncio
    from dataclasses import dataclass

    from felix.approvals.interrupt import signal_decision
    from felix.context import AuthContext, RequestContext, async_run_with_context
    from felix.manifests.builder import apply_command_screening
    from felix.manifests.schema import CommandRule, CommandScreening
    from felix.tools.types import Tool, ToolInput, ToolInvocationCtx, ToolOutput

    @dataclass
    class _Exec:
        @property
        def transport(self) -> str:
            return "sandbox"

        async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
            return f"ran:{args.get('command')}"

    s = _settings()
    tools = apply_command_screening(
        [Tool(name="shell", description="run", args_schema={}, executor=_Exec())],
        CommandScreening(
            enabled=True,
            include_defaults=False,
            rules=[CommandRule(pattern=r"\bsudo\b", decision="require_approval", reason="privileged")],
            approval_ttl_seconds=5,
        ),
        "m",
    )

    ctx = RequestContext(settings=s, auth=AuthContext(), thread_id="t1")

    async def _run() -> ToolOutput:
        async with async_run_with_context(ctx):
            return await tools[0].executor.execute({"command": "sudo reboot"}, None)

    task = asyncio.create_task(_run())
    # a pending approval must appear
    for _ in range(50):
        await asyncio.sleep(0.02)
        rows = [r for r in approvals_store._memory_approvals.values() if r["status"] == "pending"]
        if rows:
            break
    assert rows, "require_approval must create a pending approval"
    await approvals_store.decide(
        s, rows[0]["tenant_id"], rows[0]["id"], decision="approved", decided_by="ops"
    )
    await signal_decision(rows[0]["id"], "approved")
    out = await asyncio.wait_for(task, timeout=5)
    assert "ran:sudo reboot" in str(out)
