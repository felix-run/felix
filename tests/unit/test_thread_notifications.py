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


def _settings() -> Settings:
    return Settings(database_url="memory://notify", redis_url="")


@pytest.fixture(autouse=True)
async def _clean(monkeypatch: pytest.MonkeyPatch):
    """Reset the module globals, and take the ambient Redis out of the picture.

    `scripts/test.sh` does not set `FELIX_REDIS_URL`, and `Settings.redis_url` defaults
    to `redis://localhost:6379/0` -- so on a developer machine with the Compose stack up,
    these tests took the Redis path, and in CI they took the in-process one. Twenty-eight
    statements of `notify.py` executed only when Docker happened to be running, asserted
    by nothing either way, and `test_a_cancelled_reader_also_releases` cancelled inside
    `client.ping()` locally versus inside `event.wait()` in CI.

    Worse, the negative tests were defeatable from outside the process: a stray
    `PUBLISH felix:thread:t:other` on db 0 -- `make dev` in another terminal, a second
    worktree -- makes `test_only_the_named_thread_is_woken` fail deterministically.

    So the default here is no Redis, and the Redis-shaped behaviour is covered by tests
    that arrange it explicitly rather than by whatever is listening on 6379.
    """
    monkeypatch.setattr("felix.config.get_settings", _settings)
    await notify.reset_notifications()
    yield
    await notify.reset_notifications()


async def _registered(tenant_id: str, thread_id: str, *, timeout: float = 2.0) -> None:
    """Block until a `wait_for_events` task has registered its waiter.

    `wait_for_events` registers synchronously before its first `await`, so a single
    `asyncio.sleep(0)` does in fact suffice today -- but only while that ordering holds.
    Waiting on the actual condition means a change to that ordering fails here, fast and
    by name, instead of turning eight tests into two-second `wait_for` timeouts.
    """
    channel = notify._channel(tenant_id, thread_id)
    deadline = asyncio.get_running_loop().time() + timeout
    while channel not in notify._waiters:
        assert asyncio.get_running_loop().time() < deadline, f"no waiter registered on {channel}"
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_an_append_wakes_a_waiting_reader() -> None:
    async def _wait() -> notify.Wake:
        return await notify.wait_for_events("t", "th", timeout=5.0)

    waiter = asyncio.create_task(_wait())
    await _registered("t", "th")
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
async def test_a_failed_connection_is_retried_rather_than_latched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Redis blip must not degrade a worker to polling for the life of the process.

    The first version latched a boolean, so one failed connect meant that worker never
    tried again — and the poll underneath kept everything correct, which is exactly why
    nobody would have noticed the wake had stopped working.
    """
    import time

    from felix import config as config_mod

    attempts: list[float] = []

    def _unreachable() -> Settings:
        attempts.append(time.monotonic())
        # Port 1 is reserved and refuses immediately, so this fails fast rather than
        # spending the connect timeout.
        return Settings(database_url="memory://notify", redis_url="redis://127.0.0.1:1/0")

    monkeypatch.setattr(config_mod, "get_settings", _unreachable)
    monkeypatch.setattr(notify, "_RETRY_AFTER_SECONDS", 60.0)

    assert await notify._get_redis() is None
    assert len(attempts) == 1, "the first call should try"
    assert notify._redis_failed_until > time.monotonic(), "no cooldown was recorded"

    assert await notify._get_redis() is None
    assert len(attempts) == 1, "a call inside the cooldown should not retry"

    notify._redis_failed_until = time.monotonic() - 1  # cooldown elapsed
    assert await notify._get_redis() is None
    assert len(attempts) == 2, "a call after the cooldown should retry"
    assert notify._redis_failed_until > time.monotonic(), "the retry did not re-arm"


@pytest.mark.asyncio
async def test_only_the_named_thread_is_woken() -> None:
    """Both halves, because the negative alone passes against a `wait_for_events` that
    wakes nobody — which is a thing this module could plausibly regress into."""
    other = asyncio.create_task(notify.wait_for_events("t", "other", timeout=0.3))
    await _registered("t", "other")
    named = asyncio.create_task(notify.wait_for_events("t", "th", timeout=5.0))
    await _registered("t", "th")

    await notify.notify_appended("t", "th")

    assert (await asyncio.wait_for(named, timeout=2.0)).woken is True
    assert (await other).woken is False, "an append to one thread woke a reader of another"


@pytest.mark.asyncio
async def test_tenants_do_not_share_a_channel() -> None:
    """The channel carries the tenant, so one tenant's writes cannot wake another's
    reader — a cross-tenant wake would leak the timing of another tenant's activity."""
    reader = asyncio.create_task(notify.wait_for_events("tenant-a", "th", timeout=0.3))
    await _registered("tenant-a", "th")
    owner = asyncio.create_task(notify.wait_for_events("tenant-b", "th", timeout=5.0))
    await _registered("tenant-b", "th")

    await notify.notify_appended("tenant-b", "th")

    assert (await asyncio.wait_for(owner, timeout=2.0)).woken is True  # control
    assert (await reader).woken is False


@pytest.mark.asyncio
async def test_every_reader_of_a_thread_is_woken() -> None:
    """Two tabs on one conversation is ordinary. A single-consumer primitive — which is
    what the existing `waiters` module is — would wake one and strand the other."""
    readers = [asyncio.create_task(notify.wait_for_events("t", "shared", timeout=5.0)) for _ in range(3)]
    await _registered("t", "shared")
    await notify.notify_appended("t", "shared")
    results = await asyncio.wait_for(asyncio.gather(*readers), timeout=2.0)
    assert all(w.woken for w in results), [w.woken for w in results]


@pytest.mark.asyncio
async def test_a_notification_with_nobody_listening_registers_nothing() -> None:
    """Was assertion-free, and `notify_appended` catches `Exception` around everything
    that can fail — so it could only have failed on a bare `SyntaxError`. The real
    untested property is that announcing does not create the channel it announces on;
    otherwise every append to an unwatched thread leaks an entry."""
    await notify.notify_appended("t", "nobody")
    assert notify._waiters == {}
    assert notify._subscribed == {}


@pytest.mark.asyncio
async def test_waiters_are_released_when_a_reader_goes_away() -> None:
    """A stream per tab, and tabs close. Leaking a waiter per stream would be a slow
    leak in exactly the component added to help at scale."""
    for _ in range(3):
        await notify.wait_for_events("t", "churn", timeout=0.01)
    assert notify._waiters == {}, f"left {len(notify._waiters)} channels registered"
    # The ref-count is the one that can drift without `_waiters` showing anything.
    assert notify._subscribed == {}, f"left {notify._subscribed} subscribed"


@pytest.mark.asyncio
async def test_a_cancelled_reader_also_releases() -> None:
    task = asyncio.create_task(notify.wait_for_events("t", "cancelled", timeout=5.0))
    await _registered("t", "cancelled")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert notify._waiters == {}
    assert notify._subscribed == {}


# --- the append path is what publishes ------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant", ["default", "tenant-b"])
async def test_appending_through_the_store_wakes_that_tenants_reader(tenant: str) -> None:
    """Wired into the store rather than a route, so it covers every writer — the agent
    loop, steering, tool results, the management API — not only the ones an endpoint
    knows about.

    Parametrised over the tenant because the single hard-coded ``"default"`` this
    replaces was the one value that could not fail: the in-memory session had no tenant
    to pass, ``_announce`` defaulted to ``"default"``, and so every ``memory://`` append
    announced on that channel whoever wrote it. A real tenant's reader was never woken.
    """
    thread = f"wired-{tenant}"
    session = get_session_store(_settings(), tenant_id=tenant).open(thread)

    waiter = asyncio.create_task(notify.wait_for_events(tenant, thread, timeout=5.0))
    await _registered(tenant, thread)
    await session.append_batch([AppendableEvent(kind="message", role="user", content="hi")])

    wake = await asyncio.wait_for(waiter, timeout=2.0)
    assert wake.woken is True


@pytest.mark.asyncio
async def test_one_tenants_append_does_not_wake_another_on_the_same_thread_id() -> None:
    """The other direction, and the one with a disclosure shape.

    Thread ids are not globally unique, so two tenants holding the same id is ordinary.
    A wake crossing between them leaks the timing of one tenant's activity to another —
    which ``test_tenants_do_not_share_a_channel`` asserts of ``notify`` directly, one
    screen above where the store path used to violate it.
    """
    session = get_session_store(_settings(), tenant_id="tenant-b").open("same-id")

    stranger = asyncio.create_task(notify.wait_for_events("default", "same-id", timeout=0.3))
    await _registered("default", "same-id")
    owner = asyncio.create_task(notify.wait_for_events("tenant-b", "same-id", timeout=5.0))
    await _registered("tenant-b", "same-id")

    await session.append_batch([AppendableEvent(kind="message", role="user", content="hi")])

    # The positive half is the control: without it this passes against a store that
    # notifies nobody at all.
    assert (await asyncio.wait_for(owner, timeout=2.0)).woken is True
    assert (await stranger).woken is False, "a tenant-b append woke a 'default' reader"


@pytest.mark.asyncio
async def test_two_tenants_do_not_share_a_thread_id_in_the_store() -> None:
    """The bug above had a storage half as well: ``get_session_store`` took a tenant and
    handed back one process-wide store that ignored it, so the same thread id was the
    same event list for everyone."""
    settings = _settings()
    a = get_session_store(settings, tenant_id="tenant-a").open("collide")
    b = get_session_store(settings, tenant_id="tenant-b").open("collide")

    await a.append_batch([AppendableEvent(kind="message", role="user", content="a's message")])

    assert len(await a.get_events()) == 1
    assert await b.get_events() == [], "tenant-b can read tenant-a's events"


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


# --- the Redis fan-out, arranged rather than ambient ---------------------------------
#
# `notify.py`'s second stated design property is "one subscriber connection per process,
# ref-counted". Nothing asserted it. It ran only when a developer's Compose stack
# happened to be up, and the ref-count branch that keeps a channel alive for a second
# reader was uncovered even then. A fake gives it a subscribe latency, which is what the
# interesting failures need.


class _FakePubSub:
    """One connection, so commands queue behind each other -- as they do on a real one.

    This detail is the whole test. A fake that lets two SUBSCRIBEs overlap makes the
    ref-count race unreproducible: both round trips finish together, the increments
    serialize cleanly, and the buggy code passes. Queueing them is what puts a departing
    reader's UNSUBSCRIBE behind an arriving reader's SUBSCRIBE, which is the ordering
    that strands the arrival.
    """

    def __init__(self, log: list[tuple[str, str]], delay: float) -> None:
        self.log = log
        self.delay = delay
        self.channels: set[str] = set()
        self.queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self._wire = asyncio.Lock()

    async def subscribe(self, channel: str) -> None:
        async with self._wire:
            await asyncio.sleep(self.delay)  # a round trip, which is where the race lives
            self.log.append(("sub", channel))
            self.channels.add(channel)

    async def unsubscribe(self, channel: str) -> None:
        async with self._wire:
            await asyncio.sleep(self.delay)
            self.log.append(("unsub", channel))
            self.channels.discard(channel)

    async def listen(self):
        while True:
            yield await self.queue.get()

    async def aclose(self) -> None:
        return None


class _FakeRedis:
    def __init__(self, *, delay: float = 0.0) -> None:
        self.log: list[tuple[str, str]] = []
        self.delay = delay
        self.pubsubs: list[_FakePubSub] = []

    async def ping(self) -> bool:
        return True

    def pubsub(self, **_kw: object) -> _FakePubSub:
        ps = _FakePubSub(self.log, self.delay)
        self.pubsubs.append(ps)
        return ps

    async def publish(self, channel: str, data: str) -> None:
        for ps in self.pubsubs:
            if channel in ps.channels:
                await ps.queue.put({"type": "message", "channel": channel, "data": data})

    async def aclose(self) -> None:
        return None


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch):
    def _make(delay: float = 0.0) -> _FakeRedis:
        client = _FakeRedis(delay=delay)

        async def _get() -> _FakeRedis:
            return client

        monkeypatch.setattr(notify, "_get_redis", _get)
        return client

    return _make


@pytest.mark.asyncio
async def test_a_watch_subscribes_once_however_many_times_it_waits(fake_redis) -> None:
    """The unit is the reader, not the wait.

    Subscribing per wait meant a SUBSCRIBE and an UNSUBSCRIBE every poll interval of
    every stream, serialized through the one connection this module exists to conserve.
    """
    client = fake_redis()
    async with notify.thread_watch("t", "held") as watch:
        for _ in range(4):
            await watch.wait(timeout=0.01)
        assert client.log == [("sub", notify._channel("t", "held"))], client.log
    assert client.log[-1] == ("unsub", notify._channel("t", "held"))


@pytest.mark.asyncio
async def test_two_readers_of_one_thread_share_a_single_subscription(fake_redis) -> None:
    """The ref-count branch that keeps a channel alive for the second reader."""
    client = fake_redis()
    channel = notify._channel("t", "two")

    async with notify.thread_watch("t", "two"):
        async with notify.thread_watch("t", "two"):
            assert notify._subscribed[channel] == 2
            assert client.log == [("sub", channel)], "subscribed twice for one channel"
        # The inner reader left; the outer one still needs the channel.
        assert channel in client.pubsubs[0].channels, "unsubscribed while a reader remained"
        assert notify._subscribed[channel] == 1
    assert client.log == [("sub", channel), ("unsub", channel)]
    assert notify._subscribed == {}


@pytest.mark.asyncio
async def test_a_departing_reader_does_not_strand_one_still_subscribing(fake_redis) -> None:
    """The regression this file exists for.

    With the ref-count taken *after* the SUBSCRIBE await, two readers arriving together
    both saw a count of zero. The first finished its wait and released 1 -> 0, putting
    UNSUBSCRIBE last on the wire; the second was left holding a count over a connection
    subscribed to nothing -- and still reporting `by_notification=True`, which tells the
    caller it may stretch its poll interval to a minute. A stream that believes it is
    being woken, is not, and has stopped polling.
    """
    client = fake_redis(delay=0.02)
    channel = notify._channel("t", "stranded")

    async def reader(hold: float) -> notify.Wake:
        async with notify.thread_watch("t", "stranded") as watch:
            return await watch.wait(timeout=hold)

    leaving = asyncio.create_task(reader(0.001))
    staying = asyncio.create_task(reader(1.0))
    await asyncio.wait_for(leaving, timeout=2.0)

    assert channel in client.pubsubs[0].channels, "the remaining reader's channel was dropped"
    await client.publish(channel, "1")
    assert (await asyncio.wait_for(staying, timeout=2.0)).woken is True


@pytest.mark.asyncio
async def test_an_append_between_two_waits_is_not_missed(fake_redis) -> None:
    """A reader spends most of its time querying, not waiting. An append landing in that
    gap used to be lost with the per-wait event that recorded it."""
    fake_redis()
    async with notify.thread_watch("t", "gap") as watch:
        await notify.notify_appended("t", "gap")  # arrives while nobody is in `wait`
        assert (await watch.wait(timeout=0.01)).woken is True


def test_the_postgres_arm_announces_after_its_transaction() -> None:
    """Load-bearing ordering that no behavioural test can reach.

    A reader woken before the commit lands queries, finds nothing, and waits again on a
    notification already spent — so the append is invisible until the next poll fires.
    The in-memory twin cannot exercise it (no transaction), and the Postgres arm needs a
    database, so a reviewer tidying the call back inside the `async with` would get a
    green suite. `test_pooler_compatibility.py` sets the precedent for pinning a
    structural property this way when the behavioural route is closed.
    """
    import ast
    import inspect
    import textwrap

    from felix.session import store as store_mod

    # `textwrap.dedent`: a method's source is indented, which `ast.parse` rejects.
    tree = ast.parse(textwrap.dedent(inspect.getsource(store_mod._PostgresSession.append_batch)))
    inside: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncWith):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "_announce"
            ):
                inside.append(inner.lineno)

    assert not inside, f"_announce is inside the transaction block at line(s) {inside}"


def test_the_retry_cooldown_is_a_cooldown() -> None:
    """`test_a_failed_connection_is_retried_rather_than_latched` patches this constant,
    so it stays green if the real value were zero — which is the opposite failure: a
    connection attempt on every wait against a hard-down Redis."""
    assert notify._RETRY_AFTER_SECONDS > 0


@pytest.mark.asyncio
async def test_an_abandoned_connect_does_not_latch_the_module_off() -> None:
    """`_connecting` is a single-flight guard, and a guard that outlives its flight is a
    latch.

    The `finally` that clears it does not run if the loop closes with the connect still
    pending — a pytest session creates and destroys a loop per test, so this is ordinary
    there. Every later `_get_redis` then short-circuited on the stale guard and returned
    `_redis` without attempting a connect, for the life of the process. That is the
    indefinite degradation `_RETRY_AFTER_SECONDS` exists to prevent, arriving by another
    door.
    """
    stale = asyncio.get_running_loop().create_future()
    stale.set_result(None)
    notify._connecting = stale

    await notify._get_redis()
    assert notify._connecting is None, "a spent guard survived the call that observed it"


@pytest.mark.asyncio
async def test_reset_drops_the_in_flight_guard_too() -> None:
    """`reset_notifications` is documented as dropping every connection and waiter. It
    cleared six module globals and left this one, which is the one that latches."""
    notify._connecting = asyncio.get_running_loop().create_future()
    await notify.reset_notifications()
    assert notify._connecting is None
