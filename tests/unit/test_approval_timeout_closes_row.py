"""A timed-out approval is closed on its row, and never reused by the next identical call.

`wait_for_decision` has always returned `denied`/`timeout` to the caller and written nothing
back, so the row read `pending` for the life of the deployment. Two things followed, both seen
on chat.felix.run on 2026-09-29:

* every client polling `/approvals` offered a decision the harness had already made -- a
  `write_file` twenty-nine hours past its ten-minute deadline, with nothing to click;
* `create_pending` reuses a pending row for a byte-identical call, so a re-ask joined that
  dead row, carrying its expired `expires_at`, while the harness waited a fresh ttl on it. A
  client reading the row's deadline showed a live approval as already denied.
"""

from __future__ import annotations

import pytest
from felix.approvals import store as approvals_store
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.tools.types import ToolInvocationCtx

TENANT = "default"


def _settings() -> Settings:
    return Settings(allow_insecure=True, auth_mode="none", environment="development")


@pytest.fixture(autouse=True)
def _clean_store():
    approvals_store.reset_approvals_for_tests()
    yield
    approvals_store.reset_approvals_for_tests()


async def _pending(settings: Settings, **kw) -> dict:
    return await approvals_store.create_pending(
        settings,
        TENANT,
        tool_name="write_file",
        call_signature="sig",
        manifest_id="cowork",
        ttl_seconds=kw.pop("ttl_seconds", 600),
        **kw,
    )


def _age(approval_id: str, *, by_ms: int) -> None:
    """Move a memory row into the past, as if it had been opened `by_ms` ago."""
    row = approvals_store._memory_approvals[(TENANT, approval_id)]
    row["created_at"] -= by_ms
    if row["expires_at"] is not None:
        row["expires_at"] -= by_ms


async def test_an_unanswered_approval_is_denied_on_its_row() -> None:
    """End to end through the gate: nobody answers, and the row stops reading `pending`."""
    from felix.manifests.builder import apply_approvals
    from felix.manifests.schema import ApprovalRule
    from felix.tools.types import define_tool

    ran: list[dict] = []

    async def _write(args: dict) -> str:
        ran.append(args)
        return "written"

    wrapped = apply_approvals(
        [define_tool(name="write_file", description="w", handler=_write)],
        [ApprovalRule(id="workspace-write", tools=["write_file"], ttl_seconds=1)],
        "cowork",
    )[0]
    settings = _settings()
    req = RequestContext(
        settings=settings,
        auth=AuthContext(tenant_id=TENANT),
        manifest_id="cowork",
        thread_id="default:t-timeout",
    )
    async with async_run_with_context(req):
        out = await wrapped.executor.execute({"path": "notes.txt"}, ToolInvocationCtx(tool_call_id="c1"))

    assert not ran, "the gated tool ran without a decision"
    assert "[approval timeout]" in str(getattr(out, "content", out))
    assert await approvals_store.list_approvals(settings, TENANT, status="pending") == [], (
        "a timed-out approval is still listed as pending"
    )
    (row,) = await approvals_store.list_approvals(settings, TENANT, status="denied")
    assert row["decision_note"] == "timeout"
    assert row["decided_at"] is not None


async def test_a_reask_after_the_deadline_gets_a_row_of_its_own() -> None:
    settings = _settings()
    stale = await _pending(settings)
    _age(stale["id"], by_ms=601_000)

    fresh = await _pending(settings)

    assert fresh["id"] != stale["id"], "the re-ask joined a row whose wait was already over"
    assert fresh["expires_at"] > approvals_store.now_ms()
    closed = await approvals_store.get_approval(settings, TENANT, stale["id"])
    assert closed is not None
    assert (closed["status"], closed["decision_note"]) == ("denied", "timeout")


async def test_a_row_with_no_ttl_lapses_at_the_default_wait() -> None:
    """`expires_at` is null when the rule sets no ttl, but the wait still ends -- at five
    minutes -- so the row has a deadline even though it is not written down."""
    settings = _settings()
    stale = await _pending(settings, ttl_seconds=None)
    assert stale["expires_at"] is None
    _age(stale["id"], by_ms=301_000)

    fresh = await _pending(settings, ttl_seconds=None)

    assert fresh["id"] != stale["id"]


async def test_a_live_row_is_still_shared_by_identical_calls() -> None:
    """The reuse is deliberate -- concurrent identical calls share one decision -- and only a
    lapsed row is excluded from it."""
    settings = _settings()
    first = await _pending(settings)
    second = await _pending(settings)
    assert second["id"] == first["id"]


async def test_closing_never_overwrites_a_decision_that_landed_first() -> None:
    settings = _settings()
    row = await _pending(settings)
    await approvals_store.decide(settings, TENANT, row["id"], decision="approved", decided_by="op")

    assert await approvals_store.close_timed_out(settings, TENANT, row["id"]) is False
    kept = await approvals_store.get_approval(settings, TENANT, row["id"])
    assert kept is not None
    assert kept["status"] == "approved"
