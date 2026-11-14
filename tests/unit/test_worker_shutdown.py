"""What a stopping worker owes: its buffered rows written, and the time to write them."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from felix.audit import store as audit_store
from felix.usage import store as usage_store
from felix_worker.main import worker_args

from tests.support.compose_yaml import load_compose
from tests.support.factories import make_settings

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


async def test_shutdown_writes_what_the_worker_still_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The minute cron was the worker's only flush, so a restart dropped what it had buffered
    since: the usage and audit rows of durable runs and jobs, the spend no one is watching as it
    happens. The rows here are recorded by the drain itself, because that is the order that
    matters: a flush ahead of the drain would miss what the last runs wrote."""
    import felix_worker.tasks as tasks

    settings = make_settings()
    monkeypatch.setattr(tasks, "_settings", settings)

    async def _last_run_finishes() -> None:
        usage_store.record_tokens(
            settings, tenant_id="default", manifest_id="quick", model_id="m", tokens_input=7, tokens_output=3
        )
        audit_store.record_event(settings, "default", "tool_call", payload={"tool": "calculator"})

    monkeypatch.setattr(tasks, "_stop_fiber_loop", _last_run_finishes)

    await tasks._on_worker_shutdown(None)

    assert (usage_store.pending_count(), len(audit_store.pending_buffer())) == (0, 0)
    usage, _ = await usage_store.query(settings, "default")
    assert [(r["tokens_input"], r["tokens_output"]) for r in usage] == [(7, 3)]
    audit, _ = await audit_store.query(settings, "default")
    assert [e["event_type"] for e in audit] == ["tool_call"]


async def test_a_fiber_loop_that_died_does_not_stop_the_flush(monkeypatch: pytest.MonkeyPatch) -> None:
    import felix_worker.tasks as tasks

    settings = make_settings()
    monkeypatch.setattr(tasks, "_settings", settings)

    async def _died() -> None:
        raise RuntimeError("the loop's own failure")

    monkeypatch.setattr(tasks, "_fiber_loop", asyncio.ensure_future(_died()))
    monkeypatch.setattr(tasks, "_fiber_loop_stop", asyncio.Event())
    usage_store.record_tokens(
        settings, tenant_id="default", manifest_id="quick", model_id="m", tokens_input=1
    )

    await tasks._on_worker_shutdown(None)

    assert usage_store.pending_count() == 0


def _compose_worker_grace() -> int:
    """Compose kills a container 10s after `stop` unless told otherwise."""
    grace = str(
        load_compose(DEPLOY / "docker" / "compose.yml")["services"]["worker"].get("stop_grace_period", "10s")
    )
    seconds = re.fullmatch(r"(\d+)s", grace)
    assert seconds, f"stop_grace_period {grace!r} is not whole seconds"
    return int(seconds.group(1))


def _helm_worker_grace() -> int:
    """Kubernetes' default is 30s."""
    values = yaml.safe_load((DEPLOY / "helm" / "felix" / "values.yaml").read_text(encoding="utf-8"))
    return int(values["worker"].get("terminationGracePeriodSeconds", 30))


@pytest.mark.parametrize("grace", [_compose_worker_grace, _helm_worker_grace], ids=["compose", "helm"])
def test_the_worker_is_given_longer_than_its_drain_and_flush(grace: Callable[[], int]) -> None:
    """The worker gives running durable runs `FIBER_LOOP_DRAIN_S` to finish and then flushes;
    killed before that, the runs are lost and so are their rows. Compose had no grace period at all.

    Measured against what the worker asks Taskiq for (`felix_worker.shutdown`), which is the
    drain, the stop, the flush and a margin: a container killed inside the time its own worker
    was told it had is the same loss by another route.
    """
    args = worker_args()
    assert grace() > args.wait_tasks_timeout + args.shutdown_timeout


# --- the cron tasks still running when the worker stops ----------------------------------------

T0 = 1_800_000_000_000


@pytest.fixture
def job_worker(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The worker's task module on the memory stores, with one job due and the clock at `T0`."""
    import felix_worker.tasks as tasks
    from felix.durability import fibers
    from felix.jobs import scheduler
    from felix.jobs import store as jobs_store

    settings = make_settings()
    monkeypatch.setattr(tasks, "_settings", settings)
    monkeypatch.setattr(scheduler, "now_ms", lambda: T0)
    jobs_store.reset_jobs_for_tests()
    fibers.reset_memory_fibers()
    yield settings
    jobs_store.reset_jobs_for_tests()
    fibers.reset_memory_fibers()


async def _job_row(settings: Any) -> dict[str, Any]:
    from felix.jobs import store as jobs_store

    row = await jobs_store.get_job(settings, "default", "a")
    assert row is not None
    return row


async def _a_job_mid_run(
    settings: Any, monkeypatch: pytest.MonkeyPatch, until: asyncio.Event
) -> asyncio.Task[None]:
    """`run_scheduled_jobs`, as Taskiq runs it, inside a job's turn until `until` is set."""
    import felix_worker.tasks as tasks
    from felix.jobs import scheduler
    from felix.jobs import store as jobs_store

    await jobs_store.put_job(settings, "default", "a", schedule="* * * * *", manifest_id="m", payload={})
    entered = asyncio.Event()

    async def _turn(_settings: Any, *, tenant_id: str, job: dict[str, Any]) -> dict[str, Any]:
        entered.set()
        await until.wait()
        return {"status": "ok"}

    monkeypatch.setattr(scheduler, "_invoke_job_manifest", _turn)
    task = asyncio.create_task(tasks.run_scheduled_jobs.original_func())
    await asyncio.wait_for(entered.wait(), timeout=5)
    assert (await _job_row(settings))["last_status"] == "running"
    return task


async def test_a_job_in_flight_at_shutdown_is_recorded_cancelled_and_frees_its_job(
    job_worker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Taskiq does not cancel a task still running when the worker stops: it stops waiting and
    the task dies with the process. So the job's own way out -- a cancelled run on the record,
    its `running` mark cleared -- never ran, and the mark held the job for six hours. With
    production rolling hourly and jobs a minute apart, that was most restarts."""
    import felix_worker.shutdown as budget
    import felix_worker.tasks as tasks
    from felix.jobs import store as jobs_store

    monkeypatch.setattr(budget, "CRON_DRAIN_S", 0.05, raising=False)
    task = await _a_job_mid_run(job_worker, monkeypatch, asyncio.Event())

    await asyncio.wait_for(tasks._on_worker_shutdown(None), timeout=5)

    row = await _job_row(job_worker)
    assert (row["last_status"], row["last_error"]) == ("error", "cancelled"), "the mark outlived the worker"
    (run,) = await jobs_store.list_runs(job_worker, "default", "a")
    assert (run["status"], run["error"]) == ("error", "cancelled")
    assert task.cancelled()
    assert jobs_store._free(row, T0 + 60_000), "the job is held against its next firing"


async def test_a_cron_task_that_finishes_inside_the_window_is_left_to(
    job_worker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job a few seconds from done completes and records its run, as it could before."""
    import felix_worker.tasks as tasks
    from felix.jobs import store as jobs_store

    done = asyncio.Event()
    task = await _a_job_mid_run(job_worker, monkeypatch, done)
    asyncio.get_running_loop().call_later(0.05, done.set)

    await asyncio.wait_for(tasks._on_worker_shutdown(None), timeout=5)

    assert task.done() and not task.cancelled()
    assert (await _job_row(job_worker))["last_status"] == "ok"
    (run,) = await jobs_store.list_runs(job_worker, "default", "a")
    assert run["status"] == "ok"


async def test_what_a_stopped_job_recorded_is_flushed_with_the_rest(
    job_worker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cron tasks are ended before the flush, not left running behind it."""
    import felix_worker.shutdown as budget
    import felix_worker.tasks as tasks
    from felix.flush import flush_all

    monkeypatch.setattr(budget, "CRON_DRAIN_S", 0.05, raising=False)
    task = await _a_job_mid_run(job_worker, monkeypatch, asyncio.Event())
    job_ended_before_the_flush: list[bool] = []

    async def _flush(settings: Any) -> tuple[int, int]:
        job_ended_before_the_flush.append(task.done())
        return await flush_all(settings)

    monkeypatch.setattr("felix.flush.flush_all", _flush)

    await asyncio.wait_for(tasks._on_worker_shutdown(None), timeout=5)

    assert job_ended_before_the_flush == [True]


async def _a_fiber_mid_step(settings: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A durable run in a step that only a cancel ends; counts the cancels delivered to it."""
    from felix.durability import fibers

    await fibers.create_fiber(settings, "default", status="pending", state={"steps": [{"op": "invoke"}]})
    seen: dict[str, Any] = {"started": asyncio.Event(), "cancels": 0}

    async def _step(_settings: Any, row: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        seen["started"].set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            seen["cancels"] += 1
            raise
        return row

    monkeypatch.setattr(fibers, "_run_fiber_step", _step)
    return seen


async def test_the_sweep_is_left_to_end_with_its_fibers(
    job_worker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`fiber_scheduler` is a cron task too, and it is the one the hook does not cancel: the fiber
    drain stops its fibers, once each, and it ends when they have. Cancelled as well, it would
    cancel them again -- on top of the release each is making."""
    import felix_worker.tasks as tasks
    from felix.durability import fibers

    monkeypatch.setattr(fibers, "FIBER_LOOP_DRAIN_S", 0.05)
    seen = await _a_fiber_mid_step(job_worker, monkeypatch)
    sweep = asyncio.create_task(tasks.fiber_scheduler.original_func())
    await asyncio.wait_for(seen["started"].wait(), timeout=5)

    await asyncio.wait_for(tasks._on_worker_shutdown(None), timeout=5)

    assert sweep.done() and not sweep.cancelled() and sweep.exception() is None
    assert seen["cancels"] == 1
    (stored,) = fibers._memory_fibers.values()
    assert (stored["status"], stored["lease_until"]) == ("running", None)


async def test_a_job_and_a_durable_run_share_one_window(
    job_worker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Side by side, not one after the other: the budget the worker asks Taskiq for is the longer
    of the two drains, and a job that waited for the fibers' window to end first would be
    cancelled by the handler's own timeout instead of by the handler."""
    import felix_worker.shutdown as budget
    import felix_worker.tasks as tasks
    from felix.durability import fibers

    window = 0.5
    monkeypatch.setattr(budget, "CRON_DRAIN_S", window, raising=False)
    monkeypatch.setattr(fibers, "FIBER_LOOP_DRAIN_S", window)
    job = await _a_job_mid_run(job_worker, monkeypatch, asyncio.Event())
    seen = await _a_fiber_mid_step(job_worker, monkeypatch)
    sweep = asyncio.create_task(tasks.fiber_scheduler.original_func())
    await asyncio.wait_for(seen["started"].wait(), timeout=5)
    began = asyncio.get_running_loop().time()

    await asyncio.wait_for(tasks._on_worker_shutdown(None), timeout=5)

    took = asyncio.get_running_loop().time() - began
    assert job.cancelled() and seen["cancels"] == 1 and sweep.done()
    assert window <= took < 1.7 * window, f"the two windows ran one after the other: {took:.2f}s"


async def test_a_loop_stuck_in_a_claim_does_not_hold_the_drain_or_the_flush(
    job_worker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handler waited for the loop to leave its claim before it drained anything. A claim
    reads thread logs and runs burial hooks, with nothing bounding the sum, and the handler's
    own timeout then landed in the drain: fibers cancelled twice, and no flush."""
    import felix_worker.shutdown as budget
    import felix_worker.tasks as tasks
    from felix.durability import fibers

    monkeypatch.setattr(budget, "WORK_DRAIN_BUDGET_S", 0.1, raising=False)
    in_the_claim = asyncio.Event()

    async def _never_returns(*_a: Any, **_k: Any) -> Any:
        in_the_claim.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(fibers, "_claim_and_settle", _never_returns)
    settings = make_settings(fiber_poll_seconds=0.01)
    monkeypatch.setattr(tasks, "_settings", settings)
    stop = asyncio.Event()
    loop = asyncio.create_task(fibers.run_fiber_loop(settings, stop))
    monkeypatch.setattr(tasks, "_fiber_loop", loop)
    monkeypatch.setattr(tasks, "_fiber_loop_stop", stop)
    await asyncio.wait_for(in_the_claim.wait(), timeout=5)
    usage_store.record_tokens(settings, tenant_id="default", manifest_id="m", model_id="m", tokens_input=1)
    try:
        await asyncio.wait_for(tasks._on_worker_shutdown(None), timeout=2)
        assert usage_store.pending_count() == 0, "the flush never ran"
    finally:
        loop.cancel()
