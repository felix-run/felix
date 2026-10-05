"""Session leases: one exclusive holder and read-only observers, the same on every arm.

A second tab on a thread another tab drives asks for a `shared` hold and becomes an observer.
It used to get `409 lease_held` like an exclusive request, so it could not even watch — and
where an observer *was* admitted (a `shared` lease) it was handed the lease's own token and
its renewals extended the whole lease, keeping a closed tab's hold alive.

Three arms run the one contract:

* `memory` — the in-process fallback, everywhere.
* `fakeredis` — the Redis arm's code (`WATCH`/`MULTI`, the JSON payload) against an
  in-process server, everywhere, so the arm that runs across replicas is not one that only
  CI exercises.
* `redis` — a real Redis or Valkey (`FELIX_CONFORMANCE_REDIS_URL`). CI sets
  `FELIX_CONFORMANCE_REQUIRE_REDIS`, which turns a missing one into a failure.

The clock is the module's `_now`, moved by hand, so expiry is asserted rather than slept for.
On the Redis arms the key's own TTL is real time and long; the payload's expiries are what
lapse.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest

REDIS_URL_ENV = "FELIX_CONFORMANCE_REDIS_URL"
REQUIRE_REDIS_ENV = "FELIX_CONFORMANCE_REQUIRE_REDIS"
ARMS = ["memory", "fakeredis", "redis"]

T0 = 1_900_000_000.0


class _Conn:
    """`RedisConnection`'s two methods the lease module calls, over a given client.

    `fallback` fails the test: the module answers from the in-process arm when a Redis
    command raises, so a broken Redis arm would otherwise pass on the memory arm's answers.
    """

    def __init__(self, client: Any) -> None:
        self.client = client

    async def get(self) -> Any:
        return self.client

    async def fallback(self, what: str) -> None:
        raise AssertionError(f"the Redis arm fell back to memory: {what}")


@dataclass
class Arm:
    name: str
    client: Any | None
    clock: list[float]

    def at(self, seconds: float) -> None:
        self.clock[0] = T0 + seconds

    async def store_raw(self, thread: str, payload: dict[str, Any]) -> None:
        """Write a payload as an earlier release stored it, bypassing the transitions."""
        from felix.session import lease

        if self.client is None:
            lease._leases[thread] = payload
        else:
            await self.client.set(lease._redis_key(thread), json.dumps(payload), ex=600)


async def _redis_client(name: str) -> Any:
    if name == "fakeredis":
        import fakeredis

        return fakeredis.FakeAsyncRedis(decode_responses=True)
    url = os.environ.get(REDIS_URL_ENV, "").strip()
    if not url:
        if os.environ.get(REQUIRE_REDIS_ENV):
            pytest.fail(f"{REQUIRE_REDIS_ENV} is set but {REDIS_URL_ENV} is empty")
        pytest.skip(f"{REDIS_URL_ENV} not set — the real Redis arm did not run")
    import redis.asyncio as redis

    client = redis.from_url(url, decode_responses=True, socket_connect_timeout=1.0)
    try:
        await client.ping()
    except Exception as exc:  # pragma: no cover - environment
        if os.environ.get(REQUIRE_REDIS_ENV):
            raise
        pytest.skip(f"redis at {url} unreachable: {exc}")
    return client


@pytest.fixture(params=ARMS)
async def arm(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Arm]:
    from felix.session import lease

    clock = [T0]
    monkeypatch.setattr(lease, "_now", lambda: clock[0])
    monkeypatch.setattr(lease, "_leases", {})
    if request.param == "memory":
        monkeypatch.setattr(lease, "_force_memory", True)
        yield Arm("memory", None, clock)
        return
    client = await _redis_client(request.param)
    monkeypatch.setattr(lease, "_force_memory", False)
    monkeypatch.setattr(lease, "_conn", _Conn(client))
    try:
        yield Arm(request.param, client, clock)
    finally:
        await client.aclose()


def _thread() -> str:
    return f"conformance:lease-{uuid.uuid4().hex}"


async def _acquire(thread: str, holder: str, mode: str, ttl: float = 60.0, **kw: Any) -> dict[str, Any]:
    from felix.session.lease import acquire_lease

    return await acquire_lease(thread, holder_id=holder, mode=mode, ttl_seconds=ttl, **kw)


async def _status(thread: str) -> dict[str, Any]:
    from felix.session.lease import lease_status

    return await lease_status(thread)


def _observers(status: dict[str, Any]) -> list[str]:
    return [o["holder_id"] for o in status["observer_holds"]]


# --- acquiring -------------------------------------------------------------------------------


async def test_a_shared_acquire_observes_a_thread_another_holds_exclusively(arm: Arm) -> None:
    thread = _thread()
    a = await _acquire(thread, "tab-a", "exclusive")
    b = await _acquire(thread, "tab-b", "shared")

    assert b["ok"] is True, b
    assert (b["mode"], b["held_by_other"], b["renewed"]) == ("shared", True, False)
    assert b["token"] and b["token"] != a["token"], "an observer must not be handed the holder's token"
    assert a["held_by_other"] is False

    status = await _status(thread)
    assert (status["locked"], status["holder_id"], status["mode"]) == (True, "tab-a", "exclusive")
    assert _observers(status) == ["tab-b"]
    assert status["observers"] == 1


async def test_an_exclusive_acquire_by_another_holder_is_still_refused(arm: Arm) -> None:
    thread = _thread()
    await _acquire(thread, "tab-a", "exclusive")
    await _acquire(thread, "tab-b", "shared")

    for holder in ("tab-b", "tab-c"):
        refused = await _acquire(thread, holder, "exclusive")
        assert (refused["ok"], refused["error"]) == (False, "lease_held"), refused
    assert (await _status(thread))["holder_id"] == "tab-a"


async def test_the_same_holder_reacquiring_renews_its_own_hold(arm: Arm) -> None:
    thread = _thread()
    a1 = await _acquire(thread, "tab-a", "exclusive", ttl=30)
    b1 = await _acquire(thread, "tab-b", "shared", ttl=30)
    arm.at(20)
    a2 = await _acquire(thread, "tab-a", "exclusive", ttl=30, token=a1["token"])
    b2 = await _acquire(thread, "tab-b", "shared", ttl=30, token=b1["token"])

    assert (a2["renewed"], a2["token"]) == (True, a1["token"])
    assert (b2["renewed"], b2["token"]) == (True, b1["token"])
    assert (await _status(thread))["expires_at"] == int((T0 + 50) * 1000)


async def test_a_shared_acquire_on_a_free_thread_holds_it_without_locking_it(arm: Arm) -> None:
    """An observer-only lease: attached, not locked, and no bar to anyone taking it exclusively."""
    thread = _thread()
    b = await _acquire(thread, "tab-b", "shared")
    assert (b["ok"], b["mode"], b["held_by_other"]) == (True, "shared", False)
    status = await _status(thread)
    assert (status["attached"], status["locked"], status["holder_id"]) == (True, False, None)
    assert _observers(status) == ["tab-b"]

    c = await _acquire(thread, "tab-c", "exclusive")
    assert c["ok"] is True, c
    status = await _status(thread)
    assert (status["holder_id"], _observers(status)) == ("tab-c", ["tab-b"])


# --- each hold on its own clock --------------------------------------------------------------


async def test_an_observer_renewal_does_not_extend_the_exclusive_hold(arm: Arm) -> None:
    """The reason a client could not renew an observer hold: it kept a closed tab's lease alive."""
    thread = _thread()
    await _acquire(thread, "tab-a", "exclusive", ttl=30)
    await _acquire(thread, "tab-b", "shared", ttl=30)
    arm.at(20)
    await _acquire(thread, "tab-b", "shared", ttl=300)

    status = await _status(thread)
    assert status["expires_at"] == int((T0 + 30) * 1000), "the exclusive hold's expiry moved"
    arm.at(31)
    status = await _status(thread)
    assert (status["locked"], status["holder_id"]) == (False, None), status


async def test_when_the_holder_lapses_the_observer_is_not_promoted_and_keeps_its_own_ttl(arm: Arm) -> None:
    from felix.session.lease import lease_write_refusal

    thread = _thread()
    await _acquire(thread, "tab-a", "exclusive", ttl=30)
    b = await _acquire(thread, "tab-b", "shared", ttl=100)

    arm.at(31)
    status = await _status(thread)
    assert (status["locked"], status["holder_id"], status["mode"]) == (False, None, "shared"), status
    assert _observers(status) == ["tab-b"]
    assert status["observer_holds"][0]["expires_at"] == int((T0 + 100) * 1000)
    assert await lease_write_refusal(thread, b["token"]) == "lease_read_only"

    arm.at(99)
    assert _observers(await _status(thread)) == ["tab-b"]
    arm.at(101)
    assert (await _status(thread))["attached"] is False


# --- releasing -------------------------------------------------------------------------------


async def test_an_observer_release_removes_only_that_observer(arm: Arm) -> None:
    from felix.session.lease import release_lease

    thread = _thread()
    await _acquire(thread, "tab-a", "exclusive")
    b = await _acquire(thread, "tab-b", "shared")
    await _acquire(thread, "tab-c", "shared")

    released = await release_lease(thread, holder_id="tab-b", token=b["token"])
    assert released["ok"] and released["released"], released
    status = await _status(thread)
    assert (status["holder_id"], _observers(status)) == ("tab-a", ["tab-c"])


async def test_the_holder_release_leaves_its_observers_observing(arm: Arm) -> None:
    from felix.session.lease import release_lease

    thread = _thread()
    a = await _acquire(thread, "tab-a", "exclusive")
    await _acquire(thread, "tab-b", "shared")

    released = await release_lease(thread, holder_id="tab-a", token=a["token"])
    assert released["ok"], released
    status = await _status(thread)
    assert (status["locked"], status["holder_id"], _observers(status)) == (False, None, ["tab-b"])


async def test_an_observer_token_cannot_release_or_drive_as_the_holder(arm: Arm) -> None:
    from felix.session.lease import lease_write_refusal, release_lease

    thread = _thread()
    a = await _acquire(thread, "tab-a", "exclusive")
    b = await _acquire(thread, "tab-b", "shared")

    assert await lease_write_refusal(thread, a["token"]) is None
    assert await lease_write_refusal(thread, b["token"]) == "lease_read_only"
    assert await lease_write_refusal(thread, "not-a-token") == "lease_held"

    refused = await release_lease(thread, holder_id="tab-a", token=b["token"])
    assert (refused["ok"], refused["error"]) == (False, "token_mismatch"), refused
    by_token = await release_lease(thread, token=b["token"])
    assert by_token["ok"], by_token
    assert (await _status(thread))["holder_id"] == "tab-a", "an observer's token released the holder"


async def test_strict_enforcement_refuses_no_token_only_while_a_thread_is_held_exclusively(arm: Arm) -> None:
    """`driving_refusal` with no token: `lease_held` under strict while someone drives, else None."""
    from felix.session.lease import driving_refusal, release_lease

    held, watched = _thread(), _thread()
    a = await _acquire(held, "tab-a", "exclusive")
    b = await _acquire(held, "tab-b", "shared")
    await _acquire(watched, "tab-c", "shared")

    assert await driving_refusal(held, None, enforce="strict") == "lease_held"
    assert await driving_refusal(held, "", enforce="strict") == "lease_held"
    assert await driving_refusal(held, None, enforce="advisory") is None
    assert await driving_refusal(held, a["token"], enforce="strict") is None
    assert await driving_refusal(held, b["token"], enforce="strict") == "lease_read_only"
    assert await driving_refusal(held, b["token"], enforce="advisory") == "lease_read_only"
    # Observers never block a driver, so an observer-only thread -- and an unheld one -- passes.
    assert await driving_refusal(watched, None, enforce="strict") is None
    assert await driving_refusal(_thread(), None, enforce="strict") is None

    assert (await release_lease(held, token=a["token"]))["ok"]
    assert await driving_refusal(held, None, enforce="strict") is None


# --- a renewal proves itself with the token -------------------------------------------------
#
# The holder id is published: every status carries the exclusive holder's, `GET …/lease` lists
# every observer's, and a duplicated browser tab copies its own. A renewal keyed on it alone
# handed the exclusive token to anyone who read it.


async def test_the_holder_id_alone_neither_renews_nor_reveals_the_exclusive_hold(arm: Arm) -> None:
    thread = _thread()
    a = await _acquire(thread, "tab-a", "exclusive", ttl=30)
    await _acquire(thread, "tab-b", "shared")
    arm.at(10)

    for token in (None, "not-the-token"):
        posing = await _acquire(thread, "tab-a", "exclusive", ttl=300, token=token)
        assert (posing["ok"], posing["error"]) == (False, "lease_held"), posing
        assert "token" not in posing, "a refused renewal handed out the exclusive token"
    status = await _status(thread)
    assert status["expires_at"] == int((T0 + 30) * 1000), "a refused renewal extended the hold"
    assert status["token_hint"] == a["token"][:6], "a refused renewal replaced the token"

    renewed = await _acquire(thread, "tab-a", "exclusive", ttl=30, token=a["token"])
    assert (renewed["ok"], renewed["renewed"], renewed["token"]) == (True, True, a["token"])


async def test_an_observer_renewal_needs_that_observers_token(arm: Arm) -> None:
    thread = _thread()
    await _acquire(thread, "tab-a", "exclusive")
    b = await _acquire(thread, "tab-b", "shared", ttl=30)
    arm.at(10)

    for token in (None, "not-the-token"):
        posing = await _acquire(thread, "tab-b", "shared", ttl=300, token=token)
        assert (posing["ok"], posing["error"]) == (False, "lease_held"), posing
        assert "token" not in posing
    assert (await _status(thread))["observer_holds"][0]["expires_at"] == int((T0 + 30) * 1000)

    renewed = await _acquire(thread, "tab-b", "shared", ttl=30, token=b["token"])
    assert (renewed["ok"], renewed["renewed"], renewed["token"]) == (True, True, b["token"])


async def test_a_duplicated_tab_with_the_same_holder_id_falls_back_to_observing(arm: Arm) -> None:
    """chat-ui keeps its holder id in `sessionStorage`, which duplicating a tab copies."""
    from felix.session.lease import lease_write_refusal

    thread = _thread()
    original = await _acquire(thread, "tab-x", "exclusive")

    duplicate = await _acquire(thread, "tab-x", "exclusive")
    assert (duplicate["ok"], duplicate["error"]) == (False, "lease_held"), duplicate
    watching = await _acquire(thread, "tab-x", "shared")
    assert (watching["ok"], watching["mode"], watching["held_by_other"]) == (True, "shared", True), watching
    assert watching["token"] != original["token"]
    assert await lease_write_refusal(thread, watching["token"]) == "lease_read_only"

    status = await _status(thread)
    assert (status["holder_id"], _observers(status)) == ("tab-x", ["tab-x"])
    kept = await _acquire(thread, "tab-x", "exclusive", token=original["token"])
    assert (kept["ok"], kept["renewed"]) == (True, True), "the original tab lost its hold"


async def test_a_release_needs_the_holds_token(arm: Arm) -> None:
    from felix.session.lease import release_lease

    thread = _thread()
    await _acquire(thread, "tab-a", "exclusive")
    await _acquire(thread, "tab-b", "shared")

    for kwargs in ({"holder_id": "tab-a"}, {"holder_id": "tab-b"}, {}):
        refused = await release_lease(thread, **kwargs)
        assert (refused["ok"], refused["error"]) == (False, "token_required"), (kwargs, refused)
    status = await _status(thread)
    assert (status["holder_id"], _observers(status)) == ("tab-a", ["tab-b"]), (
        "a token-less release dropped a hold"
    )


# --- the stored shape ------------------------------------------------------------------------


async def test_a_lease_stored_before_observer_holds_reads_as_what_it_meant(arm: Arm) -> None:
    """A Redis key an earlier release wrote can be live across the upgrade."""
    exclusive, shared = _thread(), _thread()
    await arm.store_raw(
        exclusive,
        {
            "holder_id": "tab-a",
            "token": "tok-a",
            "mode": "exclusive",
            "acquired_at": T0,
            "expires_at": T0 + 60,
            "observers": [],
        },
    )
    await arm.store_raw(
        shared,
        {
            "holder_id": "tab-x",
            "token": "tok-x",
            "mode": "shared",
            "acquired_at": T0,
            "expires_at": T0 + 60,
            "observers": ["tab-x", "tab-y"],
        },
    )

    status = await _status(exclusive)
    assert (status["holder_id"], status["locked"]) == ("tab-a", True)
    renewed = await _acquire(exclusive, "tab-a", "exclusive", token="tok-a")
    assert (renewed["renewed"], renewed["token"]) == (True, "tok-a")

    status = await _status(shared)
    assert (status["holder_id"], status["locked"], _observers(status)) == (None, False, ["tab-x", "tab-y"])
    assert (await _acquire(shared, "tab-z", "exclusive"))["ok"] is True


class _Yielding:
    """A client whose reads hand the loop over before returning, so concurrent writers interleave.

    Without it the in-process server answers each command without suspending, `gather` runs the
    acquires one after another, and a read-then-write with no `WATCH` passes the race below.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        import asyncio

        value = await self._inner.get(*args, **kwargs)
        for _ in range(3):
            await asyncio.sleep(0)
        return value

    def pipeline(self, *args: Any, **kwargs: Any) -> Any:
        return _YieldingPipeline(self._inner.pipeline(*args, **kwargs))


class _YieldingPipeline(_Yielding):
    async def __aenter__(self) -> _YieldingPipeline:
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._inner.__aexit__(*exc)


async def test_concurrent_exclusive_acquires_have_one_winner(
    arm: Arm, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Redis this is the `WATCH`: an observer-only lease is no longer a `SET NX` away from free."""
    import asyncio

    from felix.session import lease

    if arm.client is not None:
        monkeypatch.setattr(lease, "_conn", _Conn(_Yielding(arm.client)))
    thread = _thread()
    await _acquire(thread, "watcher", "shared")
    results = await asyncio.gather(*(_acquire(thread, f"tab-{i}", "exclusive") for i in range(5)))
    assert sorted(bool(r["ok"]) for r in results) == [False, False, False, False, True], results
    winner = next(r for r in results if r["ok"])
    assert (await _status(thread))["token_hint"] == winner["token"][:6]
