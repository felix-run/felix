"""An approval decision reaching the run that is waiting for it.

Approvals ride on `felix.waiters`, which uses a Redis list so a decision made on one
replica reaches a run paused on another — and so a decision that arrives *before* the
run starts waiting is not lost, which pub/sub could not promise.

None of that was tested against a real Redis, and the gap hid a bug that made approvals
wrong on a single replica too. `RedisConnection` sets `socket_timeout=2.0`; BLPOP blocks
server-side while the client sits in a socket read; so a wait longer than two seconds
raised `TimeoutError` on a perfectly healthy connection. The handler read that as "Redis
is unusable" and fell back to an in-process future. The decision then went to Redis while
the run waited on a local future nobody would resolve, and the run was told
`denied / timeout` — after a human had clicked Approve and been told it worked.

The unit suite could not see it: those tests run with `redis_url=""`, so they exercise
the fallback path that works. Every wait here is deliberately longer than the socket
timeout, because a shorter one passes against the bug.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap

import pytest

from tests.conformance.test_cross_replica_notify import _redis_url

# Comfortably past `RedisConnection`'s 2 s socket timeout. A wait shorter than that
# proves nothing: it returns before the socket read can time out.
DECIDE_AFTER_SECONDS = 3.0
WAIT_TIMEOUT_SECONDS = 20.0


@pytest.fixture
async def waiters_against_redis(monkeypatch: pytest.MonkeyPatch):
    from felix import waiters
    from felix.config import Settings

    url = _redis_url()
    monkeypatch.setattr(
        "felix.config.get_settings", lambda: Settings(database_url="memory://appr", redis_url=url)
    )
    await waiters._conn.aclose()
    yield waiters, url
    await waiters._conn.aclose()


@pytest.mark.asyncio
async def test_a_decision_after_the_socket_timeout_still_reaches_the_run(waiters_against_redis) -> None:
    """The regression. A human takes longer than two seconds to read the prompt."""
    from felix.approvals.interrupt import signal_decision, wait_for_decision

    run = asyncio.create_task(wait_for_decision("late", timeout=WAIT_TIMEOUT_SECONDS))
    await asyncio.sleep(DECIDE_AFTER_SECONDS)
    assert await signal_decision("late", "approved") is True

    decision = await asyncio.wait_for(run, timeout=WAIT_TIMEOUT_SECONDS)
    assert decision.decision == "approved", (
        f"a human approved and the run was told {decision.decision!r} ({decision.note!r})"
    )


@pytest.mark.asyncio
async def test_a_decision_made_on_another_replica_reaches_the_run(waiters_against_redis) -> None:
    """The two-replica assertion approvals never had.

    The approver's browser talks to whichever replica the origin picked; the paused run
    is on another. Nothing is shared but Redis.
    """
    _waiters, url = waiters_against_redis
    from felix.approvals.interrupt import wait_for_decision

    run = asyncio.create_task(wait_for_decision("cross", timeout=WAIT_TIMEOUT_SECONDS))
    await asyncio.sleep(DECIDE_AFTER_SECONDS)

    other_replica = textwrap.dedent(f"""
        import asyncio, os
        os.environ["FELIX_DATABASE_URL"] = "memory://appr"
        os.environ["FELIX_REDIS_URL"] = {url!r}
        from felix.approvals.interrupt import signal_decision
        assert asyncio.run(signal_decision("cross", "approved")) is True
    """)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        other_replica,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _out, err = await asyncio.wait_for(proc.communicate(), timeout=60.0)
    assert proc.returncode == 0, f"the other replica failed: {err.decode()[-800:]}"

    decision = await asyncio.wait_for(run, timeout=WAIT_TIMEOUT_SECONDS)
    assert decision.decision == "approved", (
        f"a decision on another replica did not reach the run: {decision.decision!r}"
    )


@pytest.mark.asyncio
async def test_a_decision_that_arrives_before_the_wait_is_not_lost(waiters_against_redis) -> None:
    """Why this is a list and not a pub/sub channel.

    A fast approver, or a run slow to reach its interrupt, means the decision can land
    first. Pub/sub is at-most-once and would drop it; a list holds it until someone pops.
    """
    from felix.approvals.interrupt import signal_decision, wait_for_decision

    assert await signal_decision("early", "denied", note="not this time") is True

    decision = await asyncio.wait_for(
        wait_for_decision("early", timeout=WAIT_TIMEOUT_SECONDS), timeout=WAIT_TIMEOUT_SECONDS
    )
    assert decision.decision == "denied"
    assert decision.note == "not this time"


@pytest.mark.asyncio
async def test_an_undecided_approval_still_times_out(waiters_against_redis) -> None:
    """The slicing loop must not turn a bounded wait into an unbounded one: nobody
    decides, and the run has to stop waiting and deny."""
    from felix.approvals.interrupt import wait_for_decision

    loop = asyncio.get_running_loop()
    started = loop.time()
    decision = await asyncio.wait_for(wait_for_decision("nobody", timeout=3.0), timeout=30.0)
    elapsed = loop.time() - started

    assert decision.decision == "denied"
    assert decision.note == "timeout"
    assert elapsed < 15.0, f"waited {elapsed:.1f}s for a 3s timeout"
