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

from tests.support.factories import make_settings


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


async def test_grant_is_reusable_across_principals_when_unbound() -> None:
    """Documented behaviour when bind_principal is false — unchanged."""
    s = make_settings()
    await _grant(s, principal="alice")
    found = await approvals_store.find_approved(
        s, "t1", manifest_id="m", tool_name="shell", call_signature="abc"
    )
    assert found is not None


async def test_bind_principal_blocks_a_different_principal() -> None:
    """The privilege escalation: B replaying A's approved call."""
    s = make_settings()
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


async def test_bind_principal_allows_the_original_principal() -> None:
    s = make_settings()
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


async def test_one_shot_grant_is_spent_after_use() -> None:
    s = make_settings()
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


async def test_consume_is_single_winner() -> None:
    """Two concurrent identical calls must not both spend one grant."""
    s = make_settings()
    row = await _grant(s, principal="alice")
    first = await approvals_store.consume_approval(s, "t1", str(row["id"]))
    second = await approvals_store.consume_approval(s, "t1", str(row["id"]))
    assert (first, second) == (True, False)


async def test_consumed_grant_still_visible_without_the_flag() -> None:
    """Consumption only gates one_shot rules; ordinary grants are unaffected."""
    s = make_settings()
    row = await _grant(s, principal="alice")
    await approvals_store.consume_approval(s, "t1", str(row["id"]))
    found = await approvals_store.find_approved(
        s, "t1", manifest_id="m", tool_name="shell", call_signature="abc"
    )
    assert found is not None


# --- command screening `require_approval` ---------------------------------------


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

    s = make_settings()
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


# --- bind_principal on the waiting path ----------------------------------------


async def test_a_second_principal_cannot_join_the_first_ones_pending_request() -> None:
    """`create_pending` shares a live row between identical calls, keyed on the signature alone.

    Under `bind_principal` that let B join A's request: the approver read A, granted it, and B's
    waiting call ran on A's approval -- with `one_shot`, spending it and refusing A. B is now
    refused at once and A's approval is A's.
    """
    import asyncio

    from felix.approvals.interrupt import signal_decision
    from felix.context import AuthContext, RequestContext, async_run_with_context
    from felix.manifests.builder import apply_approvals
    from felix.manifests.schema import ApprovalRule
    from felix.tools.types import ToolInvocationCtx, define_tool, tool_output_content

    ran: list[str] = []

    async def handler(args: dict, ctx: object = None) -> str:
        ran.append(str(args.get("command")))
        return "deployed"

    rule = ApprovalRule(id="r1", tools=["deploy"], ttl_seconds=10, bind_principal=True, one_shot=True)
    (gated,) = apply_approvals([define_tool(name="deploy", description="d", handler=handler)], [rule], "m")
    s = make_settings()

    def req(principal: str) -> RequestContext:
        return RequestContext(
            settings=s,
            auth=AuthContext(principal_sub=principal, tenant_id="t1"),
            manifest_id="m",
            thread_id="t1:x",
        )

    async def call(principal: str) -> str:
        async with async_run_with_context(req(principal)):
            out = await gated.executor.execute(
                {"command": "deploy"}, ToolInvocationCtx(tool_call_id=principal)
            )
        return tool_output_content(out)

    alice = asyncio.create_task(call("alice"))
    for _ in range(200):
        pending = await approvals_store.list_approvals(s, "t1", status="pending")
        if pending:
            break
        await asyncio.sleep(0.01)
    (row,) = pending
    assert row["principal_subj"] == "alice"

    bob = await call("bob")

    assert "another caller is already pending" in bob, bob
    await approvals_store.decide(s, "t1", row["id"], decision="approved", decided_by="op")
    await signal_decision(row["id"], "approved")
    assert await alice == "deployed"
    assert ran == ["deploy"], "the approved call ran exactly once, as the principal approved"


async def test_a_preview_error_is_redacted_before_it_is_cut() -> None:
    """Cut first and a secret straddling the cut survived as an unredacted prefix.

    The failure text goes back to the model, and the approvals deny never passes through the
    masking wrapper, so this is the only redaction it gets.
    """
    from felix.manifests.builder import _approval_preview, _PreviewFailed
    from felix.secrets import register_resolved_secret
    from felix.tools.types import define_tool

    secret = "sk-STRADDLE-" + "q" * 40
    register_resolved_secret(secret)

    async def preview(args: dict) -> str:
        # "RuntimeError: " is 14 characters; the secret starts at 190 and crosses 200.
        raise RuntimeError("x" * 176 + secret)

    async def handler(args: dict, ctx: object = None) -> str:
        return "ok"

    from dataclasses import replace

    tool = replace(define_tool(name="probe", description="d", handler=handler), approval_preview=preview)

    with pytest.raises(_PreviewFailed) as failed:
        await _approval_preview(tool, {})

    assert "sk-STRADDLE" not in str(failed.value), str(failed.value)[-60:]
