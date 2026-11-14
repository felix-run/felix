"""A run that kills its worker is charged for it, and buried when it keeps doing so.

`attempts` moves when a step raises. A step that takes its worker down -- an OOM kill, a
SIGKILL -- raises nothing: the lease lapses, the next claim runs the same step, and that went on
for as long as the run's `expires_at` allowed, which a manifest may set to a day. Nearly three
hundred reclaims of a turn that kills whatever runs it, with `FELIX_FIBER_MAX_ATTEMPTS` never
consulted.

The claim is where it shows: a fiber found `running` under a lease that lapsed was let go of by
nobody. Each such claim is counted in the fiber's state, and at the limit the fiber is `dead`.

Counting alone buried healthy runs: one parked on an approval through five worker restarts, and
every fiber sharing a worker with one that kept killing it. So the limit is on lost workers with
nothing done between them -- the thread's log is where a run's work lands -- with a larger bound
on every loss whatever the log did, for a step that logs its way to the same death each time.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import Settings
from felix.durability import fibers as F
from felix.durability.runs import run_view
from felix.plugins import RunFinished, get_registry

from tests.support.factories import make_settings

TENANT = "t"


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    at = {"now": F.now_ms()}
    monkeypatch.setattr(F, "now_ms", lambda: at["now"])
    return at


def _settings(**over: Any) -> Settings:
    return make_settings(**{"fiber_max_attempts": 3, **over})


async def _fiber(settings: Settings, **over: Any) -> str:
    row = await F.create_fiber(
        settings,
        TENANT,
        **{"status": "pending", "state": {"steps": [{"op": "invoke"}], "cursor": 0, "stash": {}}, **over},
    )
    return str(row["id"])


async def _worker_dies(settings: Settings, clock: dict[str, int]) -> list[dict[str, Any]]:
    """The holder is gone and its lease runs out; then the next claim."""
    clock["now"] += F.FIBER_LEASE_MS + 1
    return await F._claim_due(settings)


def _lapses(row: dict[str, Any]) -> int:
    return int(row["state_json"].get(F.FIBER_LAPSES_KEY) or 0)


async def test_each_claim_after_a_lost_worker_is_counted(clock: dict[str, int]) -> None:
    settings = _settings()
    fiber_id = await _fiber(settings)

    (first,) = await F._claim_due(settings)
    assert _lapses(first) == 0, "a fresh run was charged"
    (second,) = await _worker_dies(settings, clock)
    (third,) = await _worker_dies(settings, clock)

    assert (_lapses(second), _lapses(third)) == (1, 2)
    stored = await F.get_fiber(settings, TENANT, fiber_id)
    assert stored is not None and _lapses(stored) == 2, "the count was not written to the row"


async def test_a_run_that_keeps_killing_its_worker_is_buried(clock: dict[str, int]) -> None:
    settings = _settings()
    fiber_id = await _fiber(settings)
    await F._claim_due(settings)
    await _worker_dies(settings, clock)
    await _worker_dies(settings, clock)

    assert await _worker_dies(settings, clock) == [], "the third lost worker's fiber was handed out again"

    stored = await F.get_fiber(settings, TENANT, fiber_id)
    assert stored is not None
    assert (stored["status"], stored["lease_owner"], stored["lease_until"]) == ("dead", "", None)
    assert run_view(stored)["error"] == "step lost its worker 3 times; not run again"
    assert await _worker_dies(settings, clock) == [], "a dead fiber was claimed"


async def test_a_buried_run_is_announced_to_whoever_started_it(clock: dict[str, int]) -> None:
    """Burial happens in the claim, where no step loop follows to say the run is over."""
    seen: list[RunFinished] = []

    async def hook(settings: Any, finished: RunFinished) -> None:
        seen.append(finished)

    get_registry().register_run_finished(hook, origin="p")
    settings = _settings(fiber_max_attempts=1)
    fiber_id = await _fiber(
        settings,
        kind="durable_chat",
        state={"steps": [{"op": "invoke"}], "cursor": 0, "origin": {"plugin": "p"}},
    )
    await F._claim_due(settings)
    await _worker_dies(settings, clock)

    assert [(f.run_id, f.status, f.error) for f in seen] == [
        (fiber_id, "dead", "step lost its worker 1 times; not run again")
    ]


@pytest.mark.parametrize("status", ["pending", "sleeping"])
async def test_a_fiber_nobody_was_running_is_not_charged(clock: dict[str, int], status: str) -> None:
    settings = _settings(fiber_max_attempts=1)
    wake_at = clock["now"] + 1000 if status == "sleeping" else None
    await _fiber(settings, status=status, wake_at=wake_at)

    claimed = await _worker_dies(settings, clock)

    assert [(row["status"], _lapses(row)) for row in claimed] == [("running", 0)]


THREAD = "t:thread-1"


async def _the_run_does_some_work(settings: Settings) -> None:
    from felix.session.store import get_session_store
    from felix.session.types import AppendableEvent

    session = get_session_store(settings, tenant_id=TENANT).open(THREAD)
    await session.append(AppendableEvent(kind="message", role="assistant", content="a step further"))


async def test_a_run_whose_log_moves_between_lost_workers_is_not_buried(clock: dict[str, int]) -> None:
    """Parked on an approval through a morning of deploys, or beside a fiber that keeps killing
    the worker they share: it loses its worker again and again, and it is not the cause."""
    settings = _settings(fiber_max_attempts=2)
    await _fiber(settings, thread_id=THREAD)
    await F._claim_due(settings)

    for _ in range(2 * settings.fiber_max_attempts + 1):
        await _the_run_does_some_work(settings)
        claimed = await _worker_dies(settings, clock)
        assert [(row["status"], _lapses(row)) for row in claimed] == [("running", 1)]


async def test_a_run_whose_log_stopped_moving_is_buried_after_all(clock: dict[str, int]) -> None:
    settings = _settings(fiber_max_attempts=2)
    fiber_id = await _fiber(settings, thread_id=THREAD)
    await F._claim_due(settings)
    await _the_run_does_some_work(settings)
    await _worker_dies(settings, clock)
    await _the_run_does_some_work(settings)
    (claimed,) = await _worker_dies(settings, clock)
    assert _lapses(claimed) == 1

    assert await _worker_dies(settings, clock) == [], "two lost workers with nothing done between them"

    stored = await F.get_fiber(settings, TENANT, fiber_id)
    assert stored is not None and stored["status"] == "dead"
    assert run_view(stored)["error"] == "step lost its worker 2 times; not run again"


async def test_a_run_that_logs_its_way_to_the_same_death_is_still_bounded(clock: dict[str, int]) -> None:
    """Movement is not progress. A step that kills its worker inside a tool call logs the call
    again on every attempt, and would otherwise be claimed until its `expires_at`."""
    settings = _settings(fiber_max_attempts=2)
    fiber_id = await _fiber(settings, thread_id=THREAD)
    await F._claim_due(settings)
    bound = settings.fiber_max_attempts * F.FIBER_LAPSE_BACKSTOP

    for lost in range(1, bound):
        await _the_run_does_some_work(settings)
        assert len(await _worker_dies(settings, clock)) == 1, f"buried at {lost} lost workers"
    await _the_run_does_some_work(settings)
    assert await _worker_dies(settings, clock) == []

    stored = await F.get_fiber(settings, TENANT, fiber_id)
    assert stored is not None
    assert run_view(stored)["error"] == f"step lost its worker {bound} times; not run again"


async def test_a_log_that_does_not_answer_does_not_hold_the_claim(
    clock: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claimed fibers' leases are running while the claim reads a log, and none has a
    heartbeat yet. A read that does not come back is no evidence of progress: the lapse counts."""
    import asyncio

    import felix.session.store as session_store

    settings = _settings(fiber_max_attempts=2)
    fiber_id = await _fiber(settings, thread_id=THREAD)
    await F._claim_due(settings)
    await _the_run_does_some_work(settings)
    await _worker_dies(settings, clock)
    await _the_run_does_some_work(settings)  # progress, if only the log could be read

    class _Hung:
        def open(self, _thread: str) -> _Hung:
            return self

        async def head(self) -> dict[str, int]:
            await asyncio.Event().wait()
            return {}

    monkeypatch.setattr(session_store, "get_session_store", lambda *_a, **_k: _Hung())
    monkeypatch.setattr(F, "FIBER_LOG_HEAD_TIMEOUT_S", 0.05)

    assert await asyncio.wait_for(_worker_dies(settings, clock), timeout=2) == []
    stored = await F.get_fiber(settings, TENANT, fiber_id)
    assert stored is not None and stored["status"] == "dead"


async def test_a_claim_that_was_let_go_ends_the_streak(clock: dict[str, int]) -> None:
    """Consecutive, like `attempts`: a worker that finished its step and released the fiber was
    not lost, so a long run is not buried by losses spread across its life."""
    settings = _settings()
    await _fiber(settings)
    await F._claim_due(settings)
    (lost,) = await _worker_dies(settings, clock)
    assert _lapses(lost) == 1

    # This holder's step lands and it releases, leaving the fiber `running` for its next step.
    await F._save_fiber(settings, lost, hold_claim=True)
    await F._release_fiber(settings, lost)
    (again,) = await F._claim_due(settings)

    assert _lapses(again) == 0
