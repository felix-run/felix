"""Retention contract: one sweep, every growing table, both backends.

Rows are written through the real stores with a controllable clock, the clock is moved
past the TTL, a second set is written, and the sweep must remove exactly the first set.
The clock lives in the modules under test (`retention.now_ms`, `fibers.now_ms`,
`a2a.tasks.now_ms`) so no row is backdated by SQL the production path never runs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from felix.a2a import tasks as a2a_store
from felix.audit import store as audit_store
from felix.durability import fibers as fiber_store
from felix.jobs import retention
from felix.memory import store as memory_store
from felix.plans import store as plans_store
from felix.session import thread_state
from felix.session.store import get_session_store
from felix.session.types import AppendableEvent
from felix.usage import store as usage_store

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("retention_settings", BACKENDS, indirect=True)

TENANT = "conformance"
DAY = retention.DAY_MS


@dataclass
class Clock:
    ms: int

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for module in (retention, fiber_store, a2a_store, memory_store):
            monkeypatch.setattr(module, "now_ms", lambda: self.ms)
        # `persist_leaf` stamps `thread_state.updated_at` from the wall clock.
        monkeypatch.setattr(thread_state, "time", SimpleNamespace(time=lambda: self.ms / 1000))


def _retention(settings: Any, **days: int) -> Any:
    return settings.model_copy(
        update={
            "audit_retention_days": days.get("audit", 1),
            "usage_retention_days": days.get("usage", 1),
            "fiber_retention_days": days.get("fiber", 1),
            "session_retention_days": days.get("session", 1),
        }
    )


async def _seed_audit(settings: Any, clock: Clock, *, manifest: str = "m", tenant: str = TENANT) -> str:
    event_id = uuid.uuid4().hex
    await audit_store._write_batch(
        settings,
        [
            {
                "tenant_id": tenant,
                "id": event_id,
                "ts": clock.ms,
                "event_type": "tool_call",
                "manifest_id": manifest,
                "status": "ok",
                "payload_json": {},
            }
        ],
    )
    return event_id


async def _audit_ids(settings: Any, tenant: str = TENANT) -> set[str]:
    rows, _ = await audit_store.query(settings, tenant, limit=500)
    return {r["id"] for r in rows}


async def _seed_usage(settings: Any, clock: Clock) -> str:
    event_id = uuid.uuid4().hex
    await usage_store._write_batch(
        settings,
        [{"tenant_id": TENANT, "id": event_id, "ts": clock.ms, "manifest_id": "m", "tokens_input": 1}],
    )
    return event_id


async def _usage_ids(settings: Any) -> set[str]:
    rows, _ = await usage_store.query(settings, TENANT, limit=500)
    return {r["id"] for r in rows}


async def _seed_fiber(settings: Any, *, status: str) -> str:
    row = await fiber_store.create_fiber(
        settings, TENANT, state={"auth": {"scopes": ["chat"]}}, status=status
    )
    return str(row["id"])


async def _seed_task(settings: Any, *, state: str) -> str:
    task_id = uuid.uuid4().hex
    await a2a_store.put_task(settings, TENANT, {"id": task_id, "status": {"state": state}, "manifest": "m"})
    return task_id


async def _seed_thread(settings: Any, clock: Clock) -> str:
    thread_id = uuid.uuid4().hex
    store = get_session_store(settings, tenant_id=TENANT)
    session = store.open(thread_id)
    await session.append_batch(
        [
            AppendableEvent(kind="message", role="user", content="hi", ts=clock.ms / 1000 - 5),
            AppendableEvent(kind="message", role="assistant", content="hello", ts=clock.ms / 1000),
        ]
    )
    await thread_state.persist_leaf(
        settings=settings, tenant_id=TENANT, thread_id=thread_id, leaf_event_id="e1"
    )
    return thread_id


async def _thread_len(settings: Any, thread_id: str) -> int:
    return len(await get_session_store(settings, tenant_id=TENANT).open(thread_id).get_events())


async def _thread_meta_ids(settings: Any) -> set[str]:
    if settings.database_url.startswith("memory://"):
        return set(thread_state._meta_by_thread)  # the memory lister keys on a tenant prefix threads lack
    return {
        str(m["id"]) for m in await thread_state.list_thread_metadata(settings=settings, tenant_id=TENANT)
    }


async def _seed_memory(settings: Any, *, superseded: bool) -> str:
    row = await memory_store.put_memory(settings, TENANT, content=uuid.uuid4().hex, origin_seq=1)
    if superseded:
        await memory_store.supersede(settings, TENANT, row["id"], 2, source="operator")
    return str(row["id"])


@parametrized
@pytest.mark.asyncio
async def test_sweep_removes_only_rows_older_than_each_ttl(
    retention_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _retention(retention_settings)
    clock = Clock(ms=1_800_000_000_000)
    clock.install(monkeypatch)

    # The superseded-memory grace is a week and not a setting, so those rows go in first.
    old_superseded_memory = await _seed_memory(settings, superseded=True)
    old_active_memory = await _seed_memory(settings, superseded=False)
    clock.ms += 7 * DAY

    old_audit = await _seed_audit(settings, clock)
    old_usage = await _seed_usage(settings, clock)
    old_done_fiber = await _seed_fiber(settings, status="completed")
    old_failed_fiber = await _seed_fiber(settings, status="failed")
    old_expired_fiber = await _seed_fiber(settings, status="expired")
    old_pending_fiber = await _seed_fiber(settings, status="pending")
    old_sleeping_fiber = await _seed_fiber(settings, status="sleeping")
    old_done_task = await _seed_task(settings, state="completed")
    old_unknown_task = await _seed_task(settings, state="unknown")
    old_working_task = await _seed_task(settings, state="working")
    old_waiting_task = await _seed_task(settings, state="input-required")
    old_thread = await _seed_thread(settings, clock)
    await plans_store.put_plan(settings, TENANT, "expired", plan={}, expires_at=clock.ms + DAY)

    clock.ms += 2 * DAY

    new_audit = await _seed_audit(settings, clock)
    new_usage = await _seed_usage(settings, clock)
    new_done_fiber = await _seed_fiber(settings, status="completed")
    new_done_task = await _seed_task(settings, state="completed")
    new_thread = await _seed_thread(settings, clock)
    await plans_store.put_plan(settings, TENANT, "live", plan={}, expires_at=clock.ms + DAY)

    counts = await retention.run_retention_sweep(settings)

    assert counts == {
        "audit_events": 1,
        "plans": 1,
        "memory_vectors": 1,
        "fibers": 3,
        "usage_events": 1,
        "a2a_tasks": 2,
        "session_events": 2,
    }
    assert await _audit_ids(settings) == {new_audit}, f"{old_audit=} should be gone"
    assert await _usage_ids(settings) == {new_usage}, f"{old_usage=} should be gone"
    for fiber_id in (old_done_fiber, old_failed_fiber, old_expired_fiber):
        assert await fiber_store.get_fiber(settings, TENANT, fiber_id) is None
    for fiber_id in (old_pending_fiber, old_sleeping_fiber, new_done_fiber):
        assert await fiber_store.get_fiber(settings, TENANT, fiber_id) is not None, (
            "live or fresh fiber swept"
        )
    for task_id in (old_done_task, old_unknown_task):
        assert await a2a_store.get_task(settings, TENANT, task_id) is None
    for task_id in (old_working_task, old_waiting_task, new_done_task):
        assert await a2a_store.get_task(settings, TENANT, task_id) is not None, "live or fresh task swept"
    assert await _thread_len(settings, old_thread) == 0
    assert await _thread_len(settings, new_thread) == 2, "a live thread lost events"
    assert await _thread_meta_ids(settings) == {new_thread}, "thread_state must go with its thread"
    remaining = await memory_store.get_many(settings, TENANT, [old_superseded_memory, old_active_memory])
    assert set(remaining) == {old_active_memory}
    assert await plans_store.get_plan(settings, TENANT, "expired") is None
    assert await plans_store.get_plan(settings, TENANT, "live") is not None


@parametrized
@pytest.mark.asyncio
async def test_zero_days_keeps_the_table(retention_settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """``0`` is keep-forever on every setting, and is the session default."""
    settings = _retention(retention_settings, audit=0, usage=0, fiber=0, session=0)
    clock = Clock(ms=1_800_000_000_000)
    clock.install(monkeypatch)

    audit_id = await _seed_audit(settings, clock)
    usage_id = await _seed_usage(settings, clock)
    fiber_id = await _seed_fiber(settings, status="completed")
    task_id = await _seed_task(settings, state="completed")
    thread_id = await _seed_thread(settings, clock)
    clock.ms += 400 * DAY

    counts = await retention.run_retention_sweep(settings)

    assert not any(
        counts[t] for t in ("audit_events", "usage_events", "fibers", "a2a_tasks", "session_events")
    )
    assert await _audit_ids(settings) == {audit_id}
    assert await _usage_ids(settings) == {usage_id}
    assert await fiber_store.get_fiber(settings, TENANT, fiber_id) is not None
    assert await a2a_store.get_task(settings, TENANT, task_id) is not None
    assert await _thread_len(settings, thread_id) == 2


@parametrized
@pytest.mark.asyncio
async def test_a_thread_is_dropped_whole_or_not_at_all(
    retention_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One recent event keeps every older event in its thread: `seq` must stay dense."""
    settings = _retention(retention_settings, session=1)
    clock = Clock(ms=1_800_000_000_000)
    clock.install(monkeypatch)

    thread_id = await _seed_thread(settings, clock)
    clock.ms += 2 * DAY
    session = get_session_store(settings, tenant_id=TENANT).open(thread_id)
    await session.append(
        AppendableEvent(kind="message", role="user", content="still here", ts=clock.ms / 1000)
    )

    counts = await retention.run_retention_sweep(settings)

    assert counts["session_events"] == 0
    assert await _thread_len(settings, thread_id) == 3


@parametrized
@pytest.mark.asyncio
async def test_a_thread_written_to_after_the_cutoff_is_not_idle(
    retention_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fork copies its source's events with their timestamps, so a thread forked today
    from an old conversation has only old events. Its metadata row says it was just
    written; that keeps it."""
    settings = _retention(retention_settings, session=1)
    clock = Clock(ms=1_800_000_000_000)
    clock.install(monkeypatch)

    forked = await _seed_thread(settings, clock)
    abandoned = await _seed_thread(settings, clock)
    clock.ms += 2 * DAY
    await thread_state.persist_leaf(settings=settings, tenant_id=TENANT, thread_id=forked, leaf_event_id="e2")

    counts = await retention.run_retention_sweep(settings)

    assert counts["session_events"] == 2
    assert await _thread_len(settings, forked) == 2, "a freshly written thread was swept as idle"
    assert await _thread_len(settings, abandoned) == 0
    assert await _thread_meta_ids(settings) == {forked}


@parametrized
@pytest.mark.asyncio
async def test_manifest_retention_days_shortens_the_audit_ttl_for_its_own_rows(
    retention_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`governance.retention_days` on a stored manifest prunes that manifest's audit rows
    early, leaves every other manifest's rows to the operator TTL, and cannot extend it."""
    from felix.manifests import store as manifest_store
    from felix.manifests.loader import parse_manifest

    settings = _retention(retention_settings, audit=30)
    clock = Clock(ms=1_800_000_000_000)
    clock.install(monkeypatch)
    short = parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "short"},
            "spec": {"governance": {"retention_days": 2}},
        }
    )
    long = parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "long"},
            "spec": {"governance": {"retention_days": 90}},
        }
    )
    for manifest in (short, long):
        await manifest_store.put_version(settings, TENANT, manifest.metadata.name, manifest, created_by="t")

    short_old = await _seed_audit(settings, clock, manifest="short")
    long_old = await _seed_audit(settings, clock, manifest="long")
    other_old = await _seed_audit(settings, clock, manifest="other")
    neighbour_old = await _seed_audit(settings, clock, manifest="short", tenant="neighbour")
    clock.ms += 28 * DAY
    short_new = await _seed_audit(settings, clock, manifest="short")
    clock.ms += 1 * DAY  # first rows are 29 days old: inside the operator's 30, past short's 2

    counts = await retention.run_retention_sweep(settings)

    assert counts["audit_events"] == 1
    assert await _audit_ids(settings) == {long_old, other_old, short_new}, (
        f"{short_old=} should be the only loss"
    )
    assert await _audit_ids(settings, tenant="neighbour") == {neighbour_old}, (
        "a stored manifest's rule reaches its own tenant only, whatever another tenant names its manifest"
    )

    clock.ms += 5 * DAY  # 34 days: past the operator's 30, still inside long's 90

    counts = await retention.run_retention_sweep(settings)

    assert counts["audit_events"] == 4, "long's 90 days must not extend the operator's 30"
    assert await _audit_ids(settings) == set()
    assert await _audit_ids(settings, tenant="neighbour") == set()


@parametrized
@pytest.mark.asyncio
async def test_manifest_retention_days_applies_to_whatever_governs_the_rows(
    retention_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule is read off the manifest a request would resolve: a bundled manifest's
    `retention_days` (governed.yaml: 30) prunes its rows even with the operator TTL off,
    and a tenant that overrides the bundled name with its own copy gets its own rule."""
    from felix.manifests import store as manifest_store
    from felix.manifests.loader import load_bundled, parse_manifest
    from felix.manifests.resolver import clear_resolver_cache

    settings = _retention(retention_settings, audit=0)
    clock = Clock(ms=1_800_000_000_000)
    clock.install(monkeypatch)
    assert load_bundled("governed").spec.governance.retention_days == 30, (
        "the fixture relies on governed.yaml"
    )
    override = parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "governed"}, "spec": {}}
    )
    await manifest_store.put_version(settings, "overrider", "governed", override, created_by="t")
    clear_resolver_cache()

    bundled_old = await _seed_audit(settings, clock, manifest="governed")
    overrider_old = await _seed_audit(settings, clock, manifest="governed", tenant="overrider")
    unknown_old = await _seed_audit(settings, clock, manifest="no-such-manifest")
    clock.ms += 31 * DAY

    counts = await retention.run_retention_sweep(settings)

    assert counts["audit_events"] == 1
    assert await _audit_ids(settings) == {unknown_old}, f"{bundled_old=} should be the only loss"
    assert await _audit_ids(settings, tenant="overrider") == {overrider_old}, (
        "the overriding tenant's copy sets no retention_days, so the bundled rule must not reach it"
    )


@pytest.mark.parametrize("retention_settings", ["postgres"], indirect=True)
@pytest.mark.asyncio
async def test_one_table_failing_does_not_stop_the_others(
    retention_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each table is its own transaction: a delete that cannot complete is logged and rolled
    back, and every other table is still swept and still counted. (Postgres only — the
    memory arm has no transactions to isolate.)"""
    settings = _retention(retention_settings)
    clock = Clock(ms=1_800_000_000_000)
    clock.install(monkeypatch)
    old_audit = await _seed_audit(settings, clock)
    old_usage = await _seed_usage(settings, clock)
    old_fiber = await _seed_fiber(settings, status="completed")
    clock.ms += 2 * DAY

    async def boom(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("canceling statement due to statement timeout")

    monkeypatch.setattr(retention, "_delete_audit", boom)

    counts = await retention.run_retention_sweep(settings)

    assert counts["audit_events"] == 0
    assert (counts["usage_events"], counts["fibers"]) == (1, 1), "a later table was not swept"
    assert await _audit_ids(settings) == {old_audit}, "the failed table's rows must be untouched"
    assert await _usage_ids(settings) == set(), f"{old_usage=} should be gone"
    assert await fiber_store.get_fiber(settings, TENANT, old_fiber) is None


@pytest.mark.parametrize("retention_settings", ["memory"], indirect=True)
@pytest.mark.asyncio
async def test_manifests_are_resolved_under_the_rls_bypass(
    retention_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep has no request context, so under `FELIX_DATABASE_RLS=true` every manifest
    read returns nothing unless the bypass is set — and then no manifest-level policy
    applies, silently. The conformance database's role owns its tables and so bypasses RLS
    anyway; this pins the one control that makes the Postgres path work, on the ContextVar
    the session listener reads."""
    from felix.db import session as db_session
    from felix.manifests import store as manifest_store

    settings = _retention(retention_settings)
    clock = Clock(ms=1_800_000_000_000)
    clock.install(monkeypatch)
    await _seed_audit(settings, clock, manifest="governed")
    seen: list[bool] = []
    real = manifest_store.PostgresManifestStore.get_active

    async def spy(self: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(db_session._rls_bypass.get())
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(manifest_store.PostgresManifestStore, "get_active", spy)
    # `list_manifests_with_events` opens its own bypass; the resolver relies on the caller's.
    monkeypatch.setattr(
        audit_store, "list_manifests_with_events", _no_bypass(audit_store.list_manifests_with_events)
    )

    await retention.run_retention_sweep(settings)

    assert seen and all(seen), "manifest resolution ran without the RLS bypass"


def _no_bypass(fn: Any) -> Any:
    """Call `fn` but report whether the *caller* had set the bypass, by asserting on entry."""
    from felix.db import session as db_session

    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        assert db_session._rls_bypass.get(), "the sweep must open the bypass before it reads manifests"
        return await fn(*args, **kwargs)

    return wrapped
