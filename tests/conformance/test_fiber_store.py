"""Fiber store contract: the retry count survives the round trip on both backends.

The unit tests prove the scheduler's arithmetic on the memory twin; this proves the
Postgres row carries `attempts` through claim, failure, backoff and burial the same way.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.durability import fibers

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("fiber_settings", BACKENDS, indirect=True)
TENANT = "conformance"


@parametrized
@pytest.mark.asyncio
async def test_attempts_persist_through_backoff_to_dead(
    fiber_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = fiber_settings.model_copy(update={"fiber_max_attempts": 2})
    clock = {"ms": 1_800_000_000_000}
    monkeypatch.setattr(fibers, "now_ms", lambda: clock["ms"])

    async def boom(settings: Any, row: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("store down")

    monkeypatch.setattr(fibers, "_run_fiber_step", boom)
    created = await fibers.create_fiber(settings, TENANT, state={"steps": [{"op": "complete"}], "cursor": 0})
    fiber_id = str(created["id"])
    stored = await fibers.get_fiber(settings, TENANT, fiber_id)
    assert stored is not None and stored["attempts"] == 0, "the column's default did not round-trip"

    assert await fibers.resume_due_fibers(settings) == 1
    row = await fibers.get_fiber(settings, TENANT, fiber_id)
    assert row is not None
    assert (row["status"], row["attempts"], row["lease_until"]) == ("sleeping", 1, None)
    assert row["wake_at"] == clock["ms"] + fibers.retry_delay_ms(1)

    clock["ms"] = int(row["wake_at"])
    assert await fibers.resume_due_fibers(settings) == 1
    row = await fibers.get_fiber(settings, TENANT, fiber_id)
    assert row is not None
    assert (row["status"], row["attempts"], row["wake_at"]) == ("dead", 2, None)

    clock["ms"] += fibers.FIBER_RETRY_MAX_MS
    assert await fibers.resume_due_fibers(settings) == 0, "dead fibers are never claimed"


@parametrized
@pytest.mark.asyncio
async def test_attempts_persist_when_the_save_itself_fails(
    fiber_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bookkeeping write that avoids `state_json` lands on both backends."""
    settings = fiber_settings.model_copy(update={"fiber_max_attempts": 2})
    clock = {"ms": 1_800_000_000_000}
    monkeypatch.setattr(fibers, "now_ms", lambda: clock["ms"])

    async def unsaveable(settings: Any, row: dict[str, Any], **kw: Any) -> None:
        raise RuntimeError("state_json is not JSON serialisable")

    created = await fibers.create_fiber(
        settings, TENANT, state={"steps": [{"op": "stash", "data": {}}, {"op": "complete"}], "cursor": 0}
    )
    monkeypatch.setattr(fibers, "_save_fiber", unsaveable)
    fiber_id = str(created["id"])

    assert await fibers.resume_due_fibers(settings) == 1
    row = await fibers.get_fiber(settings, TENANT, fiber_id)
    assert row is not None
    assert (row["status"], row["attempts"], row["lease_until"]) == ("sleeping", 1, None)
    clock["ms"] = int(row["wake_at"])
    assert await fibers.resume_due_fibers(settings) == 1
    row = await fibers.get_fiber(settings, TENANT, fiber_id)
    assert row is not None
    assert (row["status"], row["attempts"]) == ("dead", 2)


# --- completion webhooks: delivery state on the run's own row ---------------------------


@parametrized
@pytest.mark.asyncio
async def test_webhook_delivery_state_round_trips_and_is_claimed_once(fiber_settings: Any) -> None:
    """Migration 0019's columns, the terminal-only claim, and the delivery write — on both arms.

    The claim pushes `webhook_due_at` forward, so a second sweep in the same window finds nothing:
    that is what keeps two workers from announcing one run twice.
    """
    import json

    from felix.durability import webhooks

    from tests.support.webhook_receiver import SECRET, receiver

    finished = await fibers.create_fiber(
        fiber_settings, TENANT, state={"steps": [{"op": "complete"}], "cursor": 0}, webhooks=["ops"]
    )
    running = await fibers.create_fiber(
        fiber_settings,
        TENANT,
        state={"steps": [{"op": "sleep", "ms": 3_600_000}], "cursor": 0},
        webhooks=["ops"],
    )
    stored = await fibers.get_fiber(fiber_settings, TENANT, str(finished["id"]))
    assert stored is not None and stored["webhook_status"] == "pending"
    assert stored["webhook_state"]["endpoints"]["ops"]["status"] == "pending"

    await fibers.resume_due_fibers(fiber_settings)  # `finished` completes, `running` sleeps
    async with receiver([200]) as (url, seen):
        settings = fiber_settings.model_copy(
            update={"webhook_endpoints": json.dumps({"ops": {"url": url, "secret": SECRET, "tenants": "*"}})}
        )
        claimed = await webhooks._claim_due(settings, fibers.now_ms())
        assert [row["id"] for row in claimed] == [finished["id"]], "only the finished run"
        assert await webhooks._claim_due(settings, fibers.now_ms()) == [], "claimed once"

        registry = webhooks.parse_webhook_endpoints(settings)
        from felix.secrets import build_secrets

        await webhooks._deliver_row(settings, claimed[0], registry, build_secrets(settings))
    assert len(seen) == 1

    done = await fibers.get_fiber(fiber_settings, TENANT, str(finished["id"]))
    assert done is not None
    assert (done["status"], done["webhook_status"]) == ("completed", "delivered")
    assert done["webhook_state"]["endpoints"]["ops"]["status"] == "delivered"
    still = await fibers.get_fiber(fiber_settings, TENANT, str(running["id"]))
    assert still is not None and still["webhook_status"] == "pending"


@parametrized
@pytest.mark.asyncio
async def test_a_mid_step_checkpoint_lands_under_the_claims_version_and_only_there(
    fiber_settings: Any,
) -> None:
    """The invoke's resume marker is written before the model is called. It must persist
    without advancing `version` — the step's closing save is what does that, and what the
    lease loop checks — and must be refused once another writer has moved the row on."""
    created = await fibers.create_fiber(fiber_settings, TENANT, state={"steps": [{"op": "complete"}]})
    row = await fibers.get_fiber(fiber_settings, TENANT, str(created["id"]))
    assert row is not None
    version = int(row["version"] or 0)

    row["state_json"] = {**row["state_json"], "invoke_began": {"cursor": 0, "seq": 3}}
    assert await fibers._checkpoint_state(fiber_settings, row) is True
    stored = await fibers.get_fiber(fiber_settings, TENANT, str(created["id"]))
    assert stored is not None
    assert stored["state_json"]["invoke_began"] == {"cursor": 0, "seq": 3}
    assert int(stored["version"] or 0) == version, "a checkpoint does not advance the row"

    stale = {**row, "version": version - 1, "state_json": {"invoke_began": {"cursor": 0, "seq": 9}}}
    assert await fibers._checkpoint_state(fiber_settings, stale) is False
    stored = await fibers.get_fiber(fiber_settings, TENANT, str(created["id"]))
    assert stored is not None and stored["state_json"]["invoke_began"]["seq"] == 3


@parametrized
@pytest.mark.asyncio
async def test_one_run_per_thread_on_both_backends(fiber_settings: Any) -> None:
    """felix-run/felix#529: the thread is recorded, a second run on it is refused, and the
    refusal ends with the run. On Postgres this is the advisory lock, the `thread_id` column
    and the partial index all at once -- none of which the memory twin has."""
    state = {"steps": [{"op": "complete"}], "cursor": 0, "expires_at": fibers.now_ms() + 60_000}
    first = await fibers.create_fiber(
        fiber_settings, TENANT, state=state, thread_id="conf:t", exclusive_on_thread=True
    )
    stored = await fibers.get_fiber(fiber_settings, TENANT, str(first["id"]))
    assert stored is not None and stored["thread_id"] == "conf:t"

    with pytest.raises(fibers.RunInProgress) as refused:
        await fibers.create_fiber(
            fiber_settings, TENANT, state=state, thread_id="conf:t", exclusive_on_thread=True
        )
    assert refused.value.resume_token == first["id"]
    active = await fibers.active_fiber_for_thread(fiber_settings, TENANT, "conf:t")
    assert active is not None and active["id"] == first["id"]

    assert await fibers.resume_due_fibers(fiber_settings) == 1  # `complete` ends it
    assert await fibers.active_fiber_for_thread(fiber_settings, TENANT, "conf:t") is None
    await fibers.create_fiber(
        fiber_settings, TENANT, state=state, thread_id="conf:t", exclusive_on_thread=True
    )


@parametrized
@pytest.mark.asyncio
async def test_concurrent_sends_start_exactly_one_run(fiber_settings: Any) -> None:
    """The check and the insert are one step: a pair racing past the route's pre-check must
    not both enqueue."""
    import asyncio

    state = {"steps": [{"op": "complete"}], "cursor": 0, "expires_at": fibers.now_ms() + 60_000}

    async def start() -> str:
        try:
            await fibers.create_fiber(
                fiber_settings, TENANT, state=state, thread_id="conf:race", exclusive_on_thread=True
            )
            return "started"
        except fibers.RunInProgress:
            return "refused"

    outcomes = await asyncio.gather(*(start() for _ in range(8)))
    assert sorted(outcomes) == ["refused"] * 7 + ["started"]


@parametrized
@pytest.mark.asyncio
async def test_background_children_are_capped_per_parent_thread(fiber_settings: Any) -> None:
    """Each child has its own thread, so one-run-per-thread never bounded how many a parent
    could start; `max_children` does, and a finished child frees its slot. The Postgres arm reads
    `state_json->>'parent_thread_id'`, which the memory twin reads as a dict."""

    def child(parent: str) -> dict[str, Any]:
        return {
            "steps": [{"op": "complete"}],
            "cursor": 0,
            "expires_at": fibers.now_ms() + 60_000,
            "parent_thread_id": parent,
        }

    for i in range(2):
        await fibers.create_fiber(
            fiber_settings, TENANT, state=child("conf:p"), thread_id=f"conf:p:task:{i}", max_children=2
        )
    with pytest.raises(fibers.TooManyChildren):
        await fibers.create_fiber(
            fiber_settings, TENANT, state=child("conf:p"), thread_id="conf:p:task:2", max_children=2
        )
    # Another parent's slots are its own.
    await fibers.create_fiber(
        fiber_settings, TENANT, state=child("conf:q"), thread_id="conf:q:task:0", max_children=2
    )

    assert await fibers.resume_due_fibers(fiber_settings) == 3  # `complete` ends each
    await fibers.create_fiber(
        fiber_settings, TENANT, state=child("conf:p"), thread_id="conf:p:task:3", max_children=2
    )


@parametrized
@pytest.mark.asyncio
async def test_concurrent_background_starts_respect_the_cap(fiber_settings: Any) -> None:
    import asyncio

    state = {
        "steps": [{"op": "complete"}],
        "cursor": 0,
        "expires_at": fibers.now_ms() + 60_000,
        "parent_thread_id": "conf:race-parent",
    }

    async def start(i: int) -> str:
        try:
            await fibers.create_fiber(
                fiber_settings, TENANT, state=state, thread_id=f"conf:race-parent:task:{i}", max_children=3
            )
            return "started"
        except fibers.TooManyChildren:
            return "refused"

    outcomes = await asyncio.gather(*(start(i) for i in range(8)))
    assert sorted(outcomes) == ["refused"] * 5 + ["started"] * 3
