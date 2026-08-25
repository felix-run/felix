"""Waking a reader when a thread moves, instead of asking every second.

Query volume on the resume stream grows with *connected users* rather than with turns,
which is the line that crosses first at scale: a thousand reattached tabs is a sustained
query rate that learns nothing.

The design rests on one property, and most of these tests are about it: **the
notification is a hint, never the source of truth.** Redis pub/sub is at-most-once and
drops messages on reconnect, so a lost wake must cost latency and nothing else. Every
wake runs the same `get_events` query it ran before — which is what lets the poll
ceiling relax rather than disappear.

These cover the in-process layer, which is complete for a single-replica deployment
because the writer and the reader are the same process. The Redis layer needs a server;
its behaviour under a real Redis is recorded in the PR.
"""

from __future__ import annotations

import asyncio

import pytest
from felix.config import Settings
from felix.session import notify
from felix.session.store import get_session_store
from felix.session.types import AppendableEvent


@pytest.fixture(autouse=True)
async def _clean():
    await notify.reset_notifications()
    yield
    await notify.reset_notifications()


def _settings() -> Settings:
    return Settings(database_url="memory://notify", redis_url="")


@pytest.mark.asyncio
async def test_an_append_wakes_a_waiting_reader() -> None:
    async def _wait() -> notify.Wake:
        return await notify.wait_for_events("t", "th", timeout=5.0)

    waiter = asyncio.create_task(_wait())
    await asyncio.sleep(0)  # let it register before the append
    await notify.notify_appended("t", "th")

    wake = await asyncio.wait_for(waiter, timeout=2.0)
    assert wake.woken is True


@pytest.mark.asyncio
async def test_a_timeout_is_not_an_error() -> None:
    """The caller polls on the way out either way, so a quiet thread must return
    normally rather than raise."""
    wake = await notify.wait_for_events("t", "quiet", timeout=0.05)
    assert wake.woken is False


@pytest.mark.asyncio
async def test_without_redis_nothing_claims_to_be_notified(monkeypatch: pytest.MonkeyPatch) -> None:
    """`by_notification` is what tells the caller it may wait longer. Claiming it
    without a cross-process channel would make a multi-replica deployment miss writes
    from other replicas for a full minute.

    Redis is forced unavailable rather than assumed absent: a developer with the
    Compose stack up has one running, and this passed or failed depending on that.
    """

    async def _no_redis() -> None:
        return None

    monkeypatch.setattr(notify, "_get_redis", _no_redis)
    wake = await notify.wait_for_events("t", "th", timeout=0.05)
    assert wake.by_notification is False


@pytest.mark.asyncio
async def test_only_the_named_thread_is_woken() -> None:
    other = asyncio.create_task(notify.wait_for_events("t", "other", timeout=0.3))
    await asyncio.sleep(0)
    await notify.notify_appended("t", "th")
    assert (await other).woken is False, "an append to one thread woke a reader of another"


@pytest.mark.asyncio
async def test_tenants_do_not_share_a_channel() -> None:
    """The channel carries the tenant, so one tenant's writes cannot wake another's
    reader — a cross-tenant wake would leak the timing of another tenant's activity."""
    reader = asyncio.create_task(notify.wait_for_events("tenant-a", "th", timeout=0.3))
    await asyncio.sleep(0)
    await notify.notify_appended("tenant-b", "th")
    assert (await reader).woken is False


@pytest.mark.asyncio
async def test_every_reader_of_a_thread_is_woken() -> None:
    """Two tabs on one conversation is ordinary. A single-consumer primitive — which is
    what the existing `waiters` module is — would wake one and strand the other."""
    readers = [asyncio.create_task(notify.wait_for_events("t", "shared", timeout=5.0)) for _ in range(3)]
    await asyncio.sleep(0)
    await notify.notify_appended("t", "shared")
    results = await asyncio.wait_for(asyncio.gather(*readers), timeout=2.0)
    assert all(w.woken for w in results), [w.woken for w in results]


@pytest.mark.asyncio
async def test_a_notification_with_nobody_listening_is_harmless() -> None:
    await notify.notify_appended("t", "nobody")  # must not raise


@pytest.mark.asyncio
async def test_waiters_are_released_when_a_reader_goes_away() -> None:
    """A stream per tab, and tabs close. Leaking a waiter per stream would be a slow
    leak in exactly the component added to help at scale."""
    for _ in range(20):
        await notify.wait_for_events("t", "churn", timeout=0.01)
    assert notify._waiters == {}, f"left {len(notify._waiters)} channels registered"


@pytest.mark.asyncio
async def test_a_cancelled_reader_also_releases() -> None:
    task = asyncio.create_task(notify.wait_for_events("t", "cancelled", timeout=5.0))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert notify._waiters == {}


# --- the append path is what publishes ------------------------------------------------


@pytest.mark.asyncio
async def test_appending_through_the_store_wakes_a_reader() -> None:
    """Wired into the store rather than a route, so it covers every writer — the agent
    loop, steering, tool results, the management API — not only the ones an endpoint
    knows about."""
    settings = _settings()
    session = get_session_store(settings, tenant_id="default").open("wired")

    waiter = asyncio.create_task(notify.wait_for_events("default", "wired", timeout=5.0))
    await asyncio.sleep(0)
    await session.append_batch([AppendableEvent(kind="message", role="user", content="hi")])

    wake = await asyncio.wait_for(waiter, timeout=2.0)
    assert wake.woken is True


@pytest.mark.asyncio
async def test_a_failing_notifier_does_not_fail_the_append(monkeypatch: pytest.MonkeyPatch) -> None:
    """The append already succeeded. Losing a wake costs a reader some latency; raising
    here would lose the event."""

    async def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("redis exploded")

    monkeypatch.setattr(notify, "notify_appended", _boom)
    settings = _settings()
    session = get_session_store(settings, tenant_id="default").open("resilient")

    seqs = await session.append_batch([AppendableEvent(kind="message", role="user", content="hi")])
    assert seqs == [0]
    assert len(await session.get_events()) == 1
