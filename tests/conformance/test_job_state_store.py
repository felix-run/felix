"""One contract for a scheduled job's own state, run against both backends.

What the state tools rely on, and what a dict and a locked transaction could quietly disagree on:
that a job's keys are its own per tenant and per job; that the key cap is exact even when new keys
race; that of racing compare-and-sets naming one expected value exactly one lands; that a write is
refused once the job is gone or another writer holds it; and that deleting the job, or a re-save by
another writer, leaves no row behind -- while the same writer's re-save keeps them all.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from felix.jobs import state as job_state
from felix.jobs import store as jobs
from felix.jobs.state import RunHolder, StateWrite

BACKENDS = ["memory", "postgres"]
parametrized = pytest.mark.parametrize("store_settings", BACKENDS, indirect=True)

TENANT = "conformance"
OTHER = "other-tenant"
JOB = "nightly-digest"


async def _job(settings: Any, name: str = JOB, *, tenant: str = TENANT, writer: str | None = "alice") -> None:
    await jobs.put_job(settings, tenant, name, schedule="", manifest_id="quick", created_by=writer)


async def _set(
    settings: Any,
    key: str,
    value: str = "v",
    *,
    job: str = JOB,
    tenant: str = TENANT,
    writer: str | None = "alice",
    max_keys: int = 50,
    **kw: Any,
) -> job_state.SetResult:
    run = kw.pop("run", None) or await _holder(settings, job, tenant=tenant, writer=writer)
    write = StateWrite(key=key, value=value, run=run, max_keys=max_keys, **kw)
    return await job_state.set_value(settings, tenant, job, write)


async def _holder(
    settings: Any, job: str = JOB, *, tenant: str = TENANT, writer: str | None = "alice"
) -> RunHolder:
    """The job as a firing starting now reads it, for `writer`: its clear stamp as it stands."""
    row = await jobs.get_job(settings, tenant, job) or {}
    return RunHolder(writer=writer, state_cleared_at=row.get("state_cleared_at"))


async def _keys(settings: Any, job: str = JOB, tenant: str = TENANT) -> list[tuple[str, str]]:
    return [(r["key"], r["value"]) for r in await job_state.list_state(settings, tenant, job)]


@parametrized
async def test_a_value_is_read_back_and_overwritten(store_settings: Any) -> None:
    await _job(store_settings)
    first = await _set(store_settings, "bookmark", "item 1", thread_id="conformance:job:x")
    again = await _set(store_settings, "bookmark", "item 2")
    row = await job_state.get(store_settings, TENANT, JOB, "bookmark")

    assert (first.status, first.created, again.status, again.created) == ("saved", True, "saved", False)
    assert row is not None
    assert (row["value"], row["bytes"], row["updated_by_thread"]) == ("item 2", 6, "")
    assert await job_state.get(store_settings, TENANT, JOB, "missing") is None


@parametrized
async def test_keys_are_listed_in_code_point_order(store_settings: Any) -> None:
    await _job(store_settings)
    for key in ("b", "a:2", "a.1", "a-0", "0"):
        await _set(store_settings, key)
    assert [k for k, _ in await _keys(store_settings)] == ["0", "a-0", "a.1", "a:2", "b"]


@parametrized
async def test_a_jobs_state_is_its_own_per_tenant_and_per_job(store_settings: Any) -> None:
    await _job(store_settings)
    await _job(store_settings, "other-job")
    await _job(store_settings, tenant=OTHER)
    await _set(store_settings, "k", "mine")
    await _set(store_settings, "k", "other job's", job="other-job")
    await _set(store_settings, "k", "other tenant's", tenant=OTHER)

    assert await _keys(store_settings) == [("k", "mine")]
    assert await _keys(store_settings, "other-job") == [("k", "other job's")]
    assert await _keys(store_settings, tenant=OTHER) == [("k", "other tenant's")]
    # One job's cap, one tenant's delete and clear: never another's. The other tenant's job holds
    # one key, so a cap of two still has room there, whatever this tenant's job holds.
    assert (await _set(store_settings, "k2", max_keys=1)).status == "too_many_keys"
    assert (await _set(store_settings, "k2", tenant=OTHER, max_keys=2)).status == "saved"
    assert (await _set(store_settings, "k2", tenant=OTHER, max_keys=1)).status == "saved", "an overwrite"
    assert await job_state.delete_key(store_settings, OTHER, JOB, "k2")
    assert await job_state.delete_key(store_settings, OTHER, JOB, "k")
    assert await job_state.clear(store_settings, TENANT, "other-job") == 1
    assert await _keys(store_settings) == [("k", "mine")]


@parametrized
async def test_the_key_cap_is_exact_when_new_keys_race(store_settings: Any) -> None:
    await _job(store_settings)
    await _set(store_settings, "held")
    results = await asyncio.gather(*(_set(store_settings, f"k{i}", max_keys=3) for i in range(6)))

    assert sorted(r.status for r in results) == ["saved"] * 2 + ["too_many_keys"] * 4
    assert len(await _keys(store_settings)) == 3
    # At the cap, an existing key still takes a new value.
    assert (await _set(store_settings, "held", "new", max_keys=3)).status == "saved"


@parametrized
async def test_racing_compare_and_sets_land_exactly_once(store_settings: Any) -> None:
    await _job(store_settings)
    created = await asyncio.gather(*(_set(store_settings, "b", f"first-{i}", expected="") for i in range(5)))
    (won,) = [i for i, r in enumerate(created) if r.status == "saved"]
    assert {(r.status, r.current_exists) for r in created if r.status != "saved"} == {("conflict", True)}

    moved = await asyncio.gather(
        *(_set(store_settings, "b", f"second-{i}", expected=f"first-{won}") for i in range(5))
    )
    assert sorted(r.status for r in moved) == ["conflict"] * 4 + ["saved"]
    (row,) = await _keys(store_settings)
    assert row[1].startswith("second-")


@parametrized
async def test_compare_and_set_against_a_missing_key(store_settings: Any) -> None:
    await _job(store_settings)
    stale = await _set(store_settings, "b", expected="anything")
    assert (stale.status, stale.current_exists) == ("conflict", False)
    assert (await _set(store_settings, "b", expected=None)).status == "saved"
    assert (await _set(store_settings, "b", "x", expected="v")).status == "saved"
    assert await _keys(store_settings) == [("b", "x")]


@parametrized
async def test_a_write_is_refused_for_a_writer_who_no_longer_holds_the_job(store_settings: Any) -> None:
    await _job(store_settings, writer="alice")
    await _set(store_settings, "k", writer="alice")
    assert (await _set(store_settings, "k", writer="bob")).status == "job_changed"
    bob = await _holder(store_settings, writer="bob")
    assert await job_state.delete_key_as_run(store_settings, TENANT, JOB, "k", bob) == "job_changed"
    alice = await _holder(store_settings)
    assert await job_state.delete_key_as_run(store_settings, TENANT, JOB, "nope", alice) == "missing"
    assert await _keys(store_settings) == [("k", "v")]


@parametrized
async def test_a_jobless_or_writerless_mismatch_is_refused(store_settings: Any) -> None:
    assert (await _set(store_settings, "k", job="no-such-job")).status == "job_changed"
    await _job(store_settings, "legacy", writer=None)
    assert (await _set(store_settings, "k", job="legacy", writer=None)).status == "saved"
    assert (await _set(store_settings, "k", job="legacy", writer="alice")).status == "job_changed"
    assert await _keys(store_settings, "no-such-job") == []


@parametrized
async def test_the_same_writers_resave_keeps_the_state_and_anothers_clears_it(store_settings: Any) -> None:
    await _job(store_settings, writer="alice")
    await _job(store_settings, "untouched", writer="alice")
    await _set(store_settings, "k")
    await _set(store_settings, "k", job="untouched")

    await _job(store_settings, writer="alice")
    assert await _keys(store_settings) == [("k", "v")]

    await _job(store_settings, writer="bob")
    assert await _keys(store_settings) == []
    assert (await _set(store_settings, "k", writer="alice")).status == "job_changed"
    assert (await _set(store_settings, "k", "bob's", writer="bob")).status == "saved"
    assert await _keys(store_settings, "untouched") == [("k", "v")]


@parametrized
async def test_deleting_the_job_deletes_its_state_and_refuses_a_late_write(store_settings: Any) -> None:
    await _job(store_settings)
    await _job(store_settings, "kept")
    await _set(store_settings, "a")
    await _set(store_settings, "b")
    await _set(store_settings, "a", job="kept")

    assert await jobs.delete_job(store_settings, TENANT, JOB)
    assert await _keys(store_settings) == []
    assert (await _set(store_settings, "late")).status == "job_changed"
    await _job(store_settings)
    assert await _keys(store_settings) == [], "a job written again under the name starts empty"
    assert await _keys(store_settings, "kept") == [("a", "v")]


@parametrized
async def test_a_persons_delete_and_clear(store_settings: Any) -> None:
    await _job(store_settings)
    for key in ("a", "b", "c"):
        await _set(store_settings, key)
    assert await job_state.delete_key(store_settings, TENANT, JOB, "b")
    assert not await job_state.delete_key(store_settings, TENANT, JOB, "b")
    assert await job_state.clear(store_settings, TENANT, JOB) == 2
    assert await job_state.clear(store_settings, TENANT, JOB) == 0


@parametrized
async def test_a_shared_subjects_resave_refuses_a_firing_that_read_the_job_before_it(
    store_settings: Any,
) -> None:
    """Every holder of a sub-less API key is `api_key`, so the writer's subject cannot tell the
    re-saver from the firing's writer; the clear's stamp (`state_cleared_at`) can."""
    await _job(store_settings, writer="api_key")
    before = await _holder(store_settings, writer="api_key")
    await _set(store_settings, "k", run=before)
    await _job(store_settings, writer="api_key")

    assert await _keys(store_settings) == [], "a shared subject's re-save is a new writer's"
    assert (await _set(store_settings, "k", run=before)).status == "job_changed"
    assert await job_state.delete_key_as_run(store_settings, TENANT, JOB, "k", before) == "job_changed"
    assert (await _set(store_settings, "k", "new holder's", writer="api_key")).status == "saved"


@parametrized
async def test_a_named_writers_own_resave_keeps_its_firings_writing(store_settings: Any) -> None:
    await _job(store_settings, writer="alice")
    before = await _holder(store_settings)
    await _job(store_settings, writer="alice")
    assert (await _set(store_settings, "k", run=before)).status == "saved"


async def _write_held_open(settings: Any, job: str = JOB) -> tuple[Any, Any]:
    """A run's write that has taken the job's lock and inserted its row, not yet committed: what a
    racing `delete_job` or `put_job` meets mid-write. The session and its context, to commit by hand."""
    from felix.db.models import JobStateRow
    from felix.db.session import tenant_session

    holder = await _holder(settings, job)
    session = tenant_session(settings, TENANT)
    db = await session.__aenter__()
    assert await job_state._lock_job(db, TENANT, job, holder)
    db.add(JobStateRow(tenant_id=TENANT, job_name=job, key="late", value="v", updated_at=1))
    await db.flush()
    return session, db


async def _race(settings: Any, job_write: Any) -> None:
    """Start `job_write` while a run's write holds the job's lock, let it run as far as it can, then
    commit the run's write and let `job_write` finish."""
    session, db = await _write_held_open(settings)
    try:
        task = asyncio.ensure_future(job_write)
        # Long enough for every statement `job_write` can run without the lock to have run.
        await asyncio.sleep(0.5)
        assert not task.done(), "the job write waits for the run's lock"
        await db.commit()
    finally:
        await session.__aexit__(None, None, None)
    await task


@pytest.mark.parametrize("store_settings", ["postgres"], indirect=True)
async def test_a_delete_racing_a_runs_write_leaves_no_row(store_settings: Any) -> None:
    """`delete_job` deletes the job's row -- waiting on the lock a run's write holds -- before its
    state, so the DELETE of the state reads after that write committed. The other order reads first,
    misses the write still in flight, and leaves its row for the next job of the name."""
    await _job(store_settings)
    await _race(store_settings, jobs.delete_job(store_settings, TENANT, JOB))
    assert await _keys(store_settings) == []


@pytest.mark.parametrize("store_settings", ["postgres"], indirect=True)
async def test_a_new_writers_resave_racing_a_runs_write_leaves_no_row(store_settings: Any) -> None:
    """`put_job` by a new writer flushes the job row's UPDATE -- waiting on the run's lock -- before
    it deletes the state (`store._reassign_writer`), for the same reason."""
    await _job(store_settings, writer="alice")
    await _race(store_settings, _job(store_settings, writer="bob"))
    assert await _keys(store_settings) == []
