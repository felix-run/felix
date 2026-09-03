"""Two correctness bugs in the fiber scheduler, both about a claim's boundaries.

Neither is about *what* a step does. They are about how long a worker's claim on a fiber lasts,
and which tenant the database thinks is asking while a step is being set up — the two things a
hand-rolled scheduler has to get right itself, and the two it got wrong.
"""

from __future__ import annotations

import pytest
from felix.config import Settings
from felix.durability import fibers as F


def _settings() -> Settings:
    return Settings(
        database_url="memory://ci",
        object_store="memory",
        auth_mode="none",
        allow_insecure=True,
        host="127.0.0.1",
    )


async def _pending(settings: Settings) -> dict:
    return await F.create_fiber(
        settings,
        "t",
        kind="durable_chat",
        status="pending",
        state={"steps": [{"op": "complete", "result": "x"}], "cursor": 0, "stash": {}},
    )


# --------------------------------------------------------------------------
# The lease bounds worker liveness, not step duration.
# --------------------------------------------------------------------------


def test_the_lease_is_longer_than_the_interval_it_is_renewed_on() -> None:
    """Two missed renewals of slack. Equal values would make every renewal a race."""
    assert F.FIBER_LEASE_RENEW_MS < F.FIBER_LEASE_MS
    assert F.FIBER_LEASE_MS // F.FIBER_LEASE_RENEW_MS >= 3


@pytest.mark.asyncio
async def test_a_step_still_running_keeps_its_claim_past_the_original_horizon(monkeypatch) -> None:
    """The collision, in the shape that caused it.

    `FIBER_LEASE_MS` was 300s and `approvals/interrupt.py:DEFAULT_TIMEOUT_SECONDS` is 300.0 —
    the same number. An approval rule may set `ttl_seconds` up to 3600, so a run parked on an
    approval outlived its own claim and the next sweep re-claimed it, re-running an invoke
    whose tool side effects had already happened, then losing the write to the `version` CAS.
    Up to twelve times.
    """
    settings = _settings()
    row = await _pending(settings)
    t0 = F.now_ms()

    claimed = await F._claim_due_memory(settings, t0)
    assert len(claimed) == 1

    # The step is still running one renewal interval in, so it heartbeats.
    monkeypatch.setattr(F, "now_ms", lambda: t0 + F.FIBER_LEASE_RENEW_MS)
    await F._renew_lease(settings, claimed[0])
    monkeypatch.undo()

    at_old_horizon = await F._claim_due_memory(settings, t0 + F.FIBER_LEASE_MS + 1)

    assert at_old_horizon == [], "a running step was re-claimed at its original lease horizon"
    stored = F._memory_fibers[("t", row["id"])]
    assert stored["lease_until"] > t0 + F.FIBER_LEASE_MS


@pytest.mark.asyncio
async def test_a_worker_that_stops_renewing_releases_the_fiber() -> None:
    """The other direction. A lease that only ever extends is a fiber stranded by a crash."""
    settings = _settings()
    row = await _pending(settings)
    t0 = F.now_ms()
    await F._claim_due_memory(settings, t0)

    # No renewals — the worker died.
    reclaimed = await F._claim_due_memory(settings, t0 + F.FIBER_LEASE_MS + 1)

    assert len(reclaimed) == 1
    assert reclaimed[0]["id"] == row["id"]


@pytest.mark.asyncio
async def test_a_lease_already_lost_is_not_stolen_back(monkeypatch) -> None:
    """A worker whose lease lapsed and was taken must not renew its way back in mid-step."""
    settings = _settings()
    row = await _pending(settings)
    claimed = await F._claim_due_memory(settings, F.now_ms())

    stored = F._memory_fibers[("t", row["id"])]
    stored["lease_owner"] = "another-replica"
    taken_until = stored["lease_until"]

    # Advance the clock, or a renewal writes the same millisecond back and the assertion below
    # cannot tell "refused" from "renewed to an identical value".
    monkeypatch.setattr(F, "now_ms", lambda: taken_until + 60_000)
    await F._renew_lease(settings, claimed[0])
    monkeypatch.undo()

    assert stored["lease_until"] == taken_until, "renewed a claim this worker no longer holds"
    assert stored["lease_owner"] == "another-replica"


# --------------------------------------------------------------------------
# Manifest resolution happens inside the tenant context.
# --------------------------------------------------------------------------


def test_the_manifest_is_resolved_inside_the_tenant_context() -> None:
    """`async_run_with_context` is what sets the `app.tenant_id` GUC, and the worker installs
    no ambient context. With resolution above it, `FELIX_DATABASE_RLS=true` meant the FORCE'd
    policy filtered every row, `get_active` returned None, and `_read_tenant_postgres` fell
    through to the **bundled** manifest of the same name. Operators are told to fork
    `governed`, so a durable run could silently execute a different, ungoverned manifest — and
    `ensure_thread_pin` was equally blind, so the drift check could not see the stored pin.

    Asserted structurally because the failure is a database visibility rule: reproducing it
    needs a real Postgres with RLS forced, which the unit suite deliberately does not have.
    The ordering is the whole fix, so the ordering is what is pinned.
    """
    import ast
    import inspect
    import textwrap

    source = textwrap.dedent(inspect.getsource(F._run_fiber_step))
    tree = ast.parse(source)

    inside: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncWith):
            continue
        if "async_run_with_context" not in ast.unparse(node.items[0].context_expr):
            continue
        for call in ast.walk(node):
            if isinstance(call, ast.Call):
                func = call.func
                inside.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))

    for required in ("resolve_tenant_manifest", "prepare_tenant_invoke", "assert_pin_matches"):
        assert required in inside, (
            f"{required} runs outside async_run_with_context, so it queries with no "
            "app.tenant_id and RLS returns nothing"
        )


@pytest.mark.asyncio
async def test_resume_renews_the_lease_while_a_step_is_actually_running(monkeypatch) -> None:
    """The wiring, not the helper.

    The tests above call `_renew_lease` directly, so removing the heartbeat from
    `resume_due_fibers` left them all green — the same defect as testing a governance helper
    without ever driving `build_agent`. This drives `resume_due_fibers` against a step that
    outlasts the renewal interval and asserts the claim moved.
    """
    import asyncio

    settings = _settings()
    row = await _pending(settings)

    monkeypatch.setattr(F, "FIBER_LEASE_RENEW_MS", 10)

    async def _slow_step(_settings, _row):
        await asyncio.sleep(0.08)  # several renewal intervals

    monkeypatch.setattr(F, "_run_fiber_step", _slow_step)

    # The discriminator has to be the lease *moving*. Comparing it to a horizon is satisfied
    # by the original claim, because the whole step takes milliseconds — the first version of
    # this test did exactly that and stayed green with the heartbeat removed.
    at_claim: list[int] = []
    real_claim = F._claim_due_memory

    async def _record(settings_, ts):
        claimed = await real_claim(settings_, ts)
        at_claim.extend(c["lease_until"] for c in claimed)
        return claimed

    monkeypatch.setattr(F, "_claim_due_memory", _record)

    stored = F._memory_fibers[("t", row["id"])]
    await F.resume_due_fibers(settings)

    assert at_claim, "the fiber was never claimed"
    assert stored["lease_until"] > at_claim[0], (
        "the lease did not move while the step ran; a step outlasting the lease loses its claim"
    )
