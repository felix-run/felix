"""Thread notifications across processes, against a real Redis.

`session/notify.py` exists for one reason: a reader on replica B has to learn that
replica A appended. Every other test of that module runs in one process, where the
in-process waiter path answers first and Redis is never consulted — so the pub/sub
fan-out, the shared subscriber connection and the pump task have been covered by a
fake and by nothing else. A single-replica deployment never reaches them.

Two halves, because the mechanism has two and a fake can hide either:

- **receiving** — something publishes on the channel and a local waiter wakes. Uses a
  raw client rather than `notify_appended`, so `_wake_local` cannot be what answers.
- **sending and receiving** — a subprocess stands in for the other replica and calls
  `notify_appended`. Nothing is shared but Redis, which is the actual claim.

Needs a reachable Redis (`FELIX_CONFORMANCE_REDIS_URL`). CI sets
`FELIX_CONFORMANCE_REQUIRE_REDIS` so a missing one fails rather than skipping: a
silently skipped arm looks exactly like a pass, which is the failure mode this whole
suite exists to remove.
"""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap

import pytest

REDIS_URL_ENV = "FELIX_CONFORMANCE_REDIS_URL"
REQUIRE_REDIS_ENV = "FELIX_CONFORMANCE_REQUIRE_REDIS"


def _redis_url() -> str:
    url = os.environ.get(REDIS_URL_ENV)
    if not url:
        if os.environ.get(REQUIRE_REDIS_ENV):
            pytest.fail(f"{REQUIRE_REDIS_ENV} is set but {REDIS_URL_ENV} is not")
        pytest.skip(f"{REDIS_URL_ENV} unset — the cross-replica arm did not run")
    return url


@pytest.fixture
async def notify_against_redis(monkeypatch: pytest.MonkeyPatch):
    """`notify` pointed at the conformance Redis, and reset around the test."""
    from felix.config import Settings
    from felix.session import notify

    url = _redis_url()
    settings = Settings(database_url="memory://xrep", redis_url=url)
    monkeypatch.setattr("felix.config.get_settings", lambda: settings)

    await notify.reset_notifications()
    yield notify, url
    await notify.reset_notifications()


@pytest.mark.asyncio
async def test_a_publish_from_outside_this_process_wakes_a_reader(notify_against_redis) -> None:
    """The receiving half, with `_wake_local` taken out of the picture.

    Publishing through a raw client rather than `notify_appended` is the point: in one
    process the local waiter set would answer first, and the test would pass with the
    Redis path entirely broken.
    """
    notify, url = notify_against_redis
    import redis.asyncio as redis

    async with notify.thread_watch("xrep", "inbound") as watch:
        assert watch.delivering, "not subscribed — this test would prove nothing"

        publisher = redis.from_url(url, decode_responses=True)
        try:
            # Give SUBSCRIBE time to be established server-side. Redis pub/sub is
            # at-most-once and drops anything published before the subscribe lands,
            # which would look like a broken fan-out rather than a race.
            for _ in range(50):
                if (await publisher.pubsub_numsub(notify._channel("xrep", "inbound")))[0][1]:
                    break
                await asyncio.sleep(0.02)
            await publisher.publish(notify._channel("xrep", "inbound"), "1")
        finally:
            await publisher.aclose()

        wake = await watch.wait(timeout=5.0)

    assert wake.woken is True, "a publish on the channel did not reach the waiter"
    assert wake.by_notification is True


@pytest.mark.asyncio
async def test_an_append_on_another_replica_wakes_this_one(notify_against_redis) -> None:
    """The claim itself: two processes sharing nothing but Redis.

    The subprocess is the other replica. It runs `notify_appended` — the same call the
    session store makes after a commit — so this covers the publish half as well as the
    receive half, and it covers them through the real API rather than a raw PUBLISH.
    """
    notify, url = notify_against_redis

    other_replica = textwrap.dedent(f"""
        import asyncio, os
        os.environ["FELIX_DATABASE_URL"] = "memory://xrep"
        os.environ["FELIX_REDIS_URL"] = {url!r}
        from felix.session.notify import notify_appended
        asyncio.run(notify_appended("xrep", "from-b"))
    """)

    async with notify.thread_watch("xrep", "from-b") as watch:
        assert watch.delivering, "not subscribed — this test would prove nothing"

        import redis.asyncio as redis

        probe = redis.from_url(url, decode_responses=True)
        try:
            for _ in range(50):
                if (await probe.pubsub_numsub(notify._channel("xrep", "from-b")))[0][1]:
                    break
                await asyncio.sleep(0.02)
        finally:
            await probe.aclose()

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            other_replica,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _out, err = await asyncio.wait_for(proc.communicate(), timeout=60.0)
        assert proc.returncode == 0, f"the other replica failed: {err.decode()[-800:]}"

        wake = await watch.wait(timeout=5.0)

    assert wake.woken is True, "an append on another process did not wake this reader"
    assert wake.by_notification is True
