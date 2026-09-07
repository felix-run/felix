"""`Idempotency-Key` on `POST /chat`: one turn per key per principal.

A client that times out on a turn and retries ran the turn twice — two model calls, two
usage rows, two session events — because nothing tied the retry to the first attempt.
The route is exercised the way production reaches it, with the agent stubbed at the
one seam that would otherwise need a model.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from felix.config import Settings
from felix.idempotency import (
    MAX_STORED_BODY_BYTES,
    MAX_TRACKED_KEYS,
    Claim,
    IdempotencyConflict,
    MemoryIdempotencyStore,
    RedisIdempotencyStore,
    StoredResponse,
    build_idempotency_store,
    once,
    request_fingerprint,
    valid_key,
)
from felix.manifests.loader import load_bundled
from felix.patterns.types import ChatMessage, InvokeOutput
from felix_api.app import create_app
from httpx import ASGITransport, AsyncClient

KEYS = json.dumps(
    {
        "sk-acme": {"tenant_id": "acme", "sub": "alice", "scopes": ["*"]},
        "sk-acme-2": {"tenant_id": "acme", "sub": "bob", "scopes": ["*"]},
        "sk-globex": {"tenant_id": "globex", "sub": "g", "scopes": ["*"]},
    }
)
TURN = {"manifest": "quick", "messages": [{"role": "user", "content": "hi"}]}


def _settings(name: str, **over: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": f"memory://{name}",
        "object_store": "memory",
        "allow_insecure": True,
        "auth_mode": "api_key",
        "auth_api_keys": KEYS,
        "host": "127.0.0.1",
        "environment": "development",
        "redis_url": "",
        "rate_limit": 100_000,
        "anthropic_api_key": "",
        "openai_api_key": "",
    }
    base.update(over)
    return Settings(**base)


class _Agent:
    """Counts turns; the response carries the count so a replay is distinguishable.
    `started`/`proceed` let a test hold a turn open deterministically."""

    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.proceed = asyncio.Event()
        self.proceed.set()

    async def invoke(self, inp: Any) -> InvokeOutput:
        self.calls += 1
        self.started.set()
        await self.proceed.wait()
        reply = ChatMessage(role="assistant", content=f"turn {self.calls}")
        return InvokeOutput(messages=[*inp.messages, reply], final=reply)


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> _Agent:
    import felix_api.routes.chat as chat_routes

    stub = _Agent()

    async def build(*args: Any, **kwargs: Any) -> _Agent:
        return stub

    monkeypatch.setattr(chat_routes, "build_tenant_agent", build)
    return stub


def _client(settings: Settings) -> AsyncClient:
    app = create_app(settings=settings, plugins=[])
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


def _h(key: str | None, token: str = "sk-acme") -> dict[str, str]:
    headers = {"authorization": f"Bearer {token}"}
    if key is not None:
        headers["idempotency-key"] = key
    return headers


# --- the route ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_retry_with_the_same_key_replays_the_first_response(agent: _Agent) -> None:
    async with _client(_settings("replay")) as client:
        first = await client.post("/chat", json=TURN, headers=_h("k1"))
        again = await client.post("/chat", json=TURN, headers=_h("k1"))
    assert first.status_code == 200 and "idempotent-replayed" not in first.headers
    assert again.status_code == 200 and again.headers["idempotent-replayed"] == "true"
    assert again.json() == first.json()
    assert agent.calls == 1, "the retry must not run a second turn"


@pytest.mark.asyncio
async def test_without_a_key_every_request_is_a_turn(agent: _Agent) -> None:
    async with _client(_settings("nokey")) as client:
        for _ in range(2):
            assert (await client.post("/chat", json=TURN, headers=_h(None))).status_code == 200
    assert agent.calls == 2


@pytest.mark.asyncio
async def test_the_same_key_with_a_different_body_is_refused(agent: _Agent) -> None:
    other = {"manifest": "quick", "messages": [{"role": "user", "content": "something else"}]}
    async with _client(_settings("mismatch")) as client:
        assert (await client.post("/chat", json=TURN, headers=_h("k1"))).status_code == 200
        reused = await client.post("/chat", json=other, headers=_h("k1"))
    assert reused.status_code == 422 and reused.json()["detail"] == "idempotency_key_reused"
    assert agent.calls == 1


@pytest.mark.asyncio
async def test_keys_are_scoped_to_the_principal_not_the_tenant(agent: _Agent) -> None:
    """A replay returns before the manifest's inbound auth runs, so a response one
    principal earned must not be handed to another in the same tenant — and two tenants
    choosing the same key must never see each other's."""
    async with _client(_settings("principals")) as client:
        alice = await client.post("/chat", json=TURN, headers=_h("shared", "sk-acme"))
        bob = await client.post("/chat", json=TURN, headers=_h("shared", "sk-acme-2"))
        globex = await client.post("/chat", json=TURN, headers=_h("shared", "sk-globex"))
    assert alice.status_code == bob.status_code == globex.status_code == 200
    assert "idempotent-replayed" not in bob.headers and "idempotent-replayed" not in globex.headers
    assert agent.calls == 3
    assert len({r.json()["final"]["content"] for r in (alice, bob, globex)}) == 3


@pytest.mark.asyncio
async def test_a_retry_while_the_first_attempt_runs_is_told_so(agent: _Agent) -> None:
    agent.proceed.clear()
    async with _client(_settings("inflight")) as client:
        first = asyncio.create_task(client.post("/chat", json=TURN, headers=_h("k1")))
        await asyncio.wait_for(agent.started.wait(), 5)
        second = await client.post("/chat", json=TURN, headers=_h("k1"))
        assert second.status_code == 409 and second.json()["detail"] == "idempotency_in_progress"
        agent.proceed.set()
        assert (await first).status_code == 200
        replay = await client.post("/chat", json=TURN, headers=_h("k1"))
    assert replay.headers.get("idempotent-replayed") == "true"
    assert agent.calls == 1


@pytest.mark.asyncio
async def test_a_failed_attempt_releases_the_key_so_the_retry_runs(agent: _Agent) -> None:
    """Storing a failure would make the retry the header exists for return the failure."""
    async with _client(_settings("release")) as client:
        bad = await client.post("/chat", json={"manifest": "no-such-manifest"}, headers=_h("k1"))
        assert bad.status_code == 404
        fixed = await client.post("/chat", json=TURN, headers=_h("k1"))
    assert fixed.status_code == 200 and "idempotent-replayed" not in fixed.headers


@pytest.mark.asyncio
async def test_a_malformed_key_is_refused_before_anything_runs(agent: _Agent) -> None:
    async with _client(_settings("badkey")) as client:
        for key in ("", "x" * 256, "has space", "tab\tkey", "{slot}1"):
            resp = await client.post("/chat", json=TURN, headers=_h(key))
            assert resp.status_code == 400 and resp.json()["detail"] == "invalid_idempotency_key", key
    assert agent.calls == 0


@pytest.mark.asyncio
async def test_two_apps_in_one_process_do_not_share_claims(agent: _Agent) -> None:
    async with _client(_settings("app-a")) as a, _client(_settings("app-b")) as b:
        assert (await a.post("/chat", json=TURN, headers=_h("k1"))).status_code == 200
        second = await b.post("/chat", json=TURN, headers=_h("k1"))
    assert "idempotent-replayed" not in second.headers
    assert agent.calls == 2


@pytest.mark.asyncio
async def test_a_durable_accept_replays_as_the_same_202(
    agent: _Agent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 202 carries the resume token; a retry that enqueued a second fiber would leave
    the client with two runs and one token."""
    import felix.durability.runs as runs

    assert load_bundled("cowork").spec.execution.mode == "durable", "the 202 path needs a durable manifest"
    starts = 0

    async def start(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal starts
        starts += 1
        return {"status": "accepted", "resume_token": f"tok-{starts}", "thread_id": "acme:t"}

    monkeypatch.setattr(runs, "start_durable_chat", start)
    body = {"manifest": "cowork", "messages": [{"role": "user", "content": "hi"}]}
    async with _client(_settings("durable")) as client:
        first = await client.post("/chat", json=body, headers=_h("k1"))
        again = await client.post("/chat", json=body, headers=_h("k1"))
    assert first.status_code == 202, first.text
    assert again.status_code == 202 and again.headers["idempotent-replayed"] == "true"
    assert again.json()["resume_token"] == first.json()["resume_token"] == "tok-1"
    assert starts == 1


# --- the store -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_store_contract_ttl_and_ownership() -> None:
    now = [0.0]
    store = MemoryIdempotencyStore(ttl_seconds=10, now=lambda: now[0])
    first = await store.claim("t", "k", "fp")
    assert first.kind == "new" and first.token
    assert (await store.claim("t", "k", "fp")).kind == "in_progress"
    assert (await store.claim("t", "k", "other")).kind == "mismatch"
    assert await store.finish("t", "k", "not-the-holder", StoredResponse(200, {"ok": True})) is False
    assert await store.finish("t", "k", first.token, StoredResponse(200, {"ok": True})) is True
    assert (await store.claim("t", "k", "fp")) == Claim("replay", StoredResponse(200, {"ok": True}))
    now[0] = 11.0
    second = await store.claim("t", "k", "fp")
    assert second.kind == "new", "past the TTL the key is free"
    # The first holder outlived its TTL; its finish/release must not touch the new claim.
    assert await store.finish("t", "k", first.token, StoredResponse(200, {"stale": True})) is False
    await store.release("t", "k", first.token)
    assert (await store.claim("t", "k", "fp")).kind == "in_progress"
    await store.release("t", "k", second.token)
    assert (await store.claim("t", "k", "fp")).kind == "new"


@pytest.mark.asyncio
async def test_memory_store_is_bounded_and_declines_oversized_bodies() -> None:
    store = MemoryIdempotencyStore(ttl_seconds=3600)
    for n in range(MAX_TRACKED_KEYS + 100):
        await store.claim("t", f"k{n}", "fp")
    assert len(store._records) <= MAX_TRACKED_KEYS
    assert (await store.claim("t", "k0", "fp")).kind == "new", "the oldest was evicted, not the newest"

    big = await store.claim("t", "big", "fp")
    huge = StoredResponse(200, {"final": "x" * (MAX_STORED_BODY_BYTES + 1)})
    assert await store.finish("t", "big", big.token, huge) is False
    assert (await store.claim("t", "big", "fp")).kind == "new", "declined, so the retry runs the turn"


class _FakeRedis:
    """Enough of redis.asyncio for the store: SET NX/EX, GET, and the two CAS scripts."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.down = False

    def _check(self) -> None:
        if self.down:
            raise ConnectionError("down")

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        self._check()
        if nx and key in self.data:
            return False
        self.data[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def get(self, key: str) -> str | None:
        self._check()
        return self.data.get(key)

    async def eval(self, script: str, numkeys: int, key: str, token: str, *args: Any) -> int:
        self._check()
        cur = self.data.get(key)
        if cur is None or json.loads(cur).get("token") != token:
            return 0
        if "'DEL'" in script:
            del self.data[key]
        else:
            self.data[key] = args[0]
            self.ttls[key] = int(args[1])
        return 1


class _Conn:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.closed = 0

    async def get(self) -> Any:
        return self.client

    async def aclose(self) -> None:
        self.closed += 1


@pytest.mark.asyncio
async def test_redis_store_claims_atomically_keys_by_scope_and_checks_ownership() -> None:
    fake = _FakeRedis()
    store = RedisIdempotencyStore(60, connection=_Conn(fake))  # type: ignore[arg-type]
    first = await store.claim("acme/alice", "k", "fp")
    assert first.kind == "new"
    assert (await store.claim("acme/alice", "k", "fp")).kind == "in_progress"
    assert (await store.claim("acme/bob", "k", "fp")).kind == "new"
    assert len(fake.data) == 2 and all(k.startswith("felix:idempotency:") for k in fake.data)
    assert not any(k.endswith(":k") or "acme" in k for k in fake.data), "scope and key are both hashed"
    assert set(fake.ttls.values()) == {60}
    assert await store.finish("acme/alice", "k", "intruder", StoredResponse(200, {})) is False
    assert (
        await store.finish("acme/alice", "k", first.token, StoredResponse(202, {"resume_token": "x"})) is True
    )
    assert (await store.claim("acme/alice", "k", "fp")) == Claim(
        "replay", StoredResponse(202, {"resume_token": "x"})
    )
    assert (await store.claim("acme/alice", "k", "zz")).kind == "mismatch"
    await store.release("acme/alice", "k", "intruder")
    assert (await store.claim("acme/alice", "k", "fp")).kind == "replay"
    await store.release("acme/alice", "k", first.token)
    assert (await store.claim("acme/alice", "k", "fp")).kind == "new"


@pytest.mark.asyncio
async def test_redis_store_degrades_once_and_recovers_once(caplog: pytest.LogCaptureFixture) -> None:
    """One line per transition, not one per request: the rate limiter's shape."""
    conn = _Conn(None)
    store = RedisIdempotencyStore(60, connection=conn)  # type: ignore[arg-type]
    with caplog.at_level("INFO", logger="felix.idempotency"):
        assert (await store.claim("t", "k", "fp")).kind == "new"
        assert (await store.claim("t", "k", "fp")).kind == "in_progress"
        conn.client = _FakeRedis()
        assert (await store.claim("t", "k2", "fp")).kind == "new"
    messages = [r.getMessage() for r in caplog.records]
    assert sum("redis unavailable" in m for m in messages) == 1
    assert sum("reachable again" in m for m in messages) == 1


@pytest.mark.asyncio
async def test_a_command_failure_against_a_reachable_redis_degrades_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """OOM under noeviction or a READONLY replica answers PING and refuses the command;
    that is a degradation like unreachable, said once, not a DEBUG line per request."""
    fake = _FakeRedis()
    fake.down = True
    conn = _Conn(fake)
    store = RedisIdempotencyStore(60, connection=conn)  # type: ignore[arg-type]
    with caplog.at_level("INFO", logger="felix.idempotency"):
        assert (await store.claim("t", "k", "fp")).kind == "new"
        assert conn.closed == 1, "a failed command hands the connection back"
        assert (await store.claim("t", "k", "fp")).kind == "in_progress"
        fake.down = False
        assert (await store.claim("t", "k2", "fp")).kind == "new"
    messages = [r.getMessage() for r in caplog.records]
    assert sum("deduplicating within this replica" in m for m in messages) == 1
    assert sum("reachable again" in m for m in messages) == 1


@pytest.mark.asyncio
async def test_once_is_the_whole_choreography() -> None:
    store = MemoryIdempotencyStore(ttl_seconds=60)
    calls = 0

    async def run() -> StoredResponse:
        nonlocal calls
        calls += 1
        return StoredResponse(200, {"n": calls})

    assert await once(store, "s", "k", "fp", run) == (StoredResponse(200, {"n": 1}), False)
    assert await once(store, "s", "k", "fp", run) == (StoredResponse(200, {"n": 1}), True)
    with pytest.raises(IdempotencyConflict) as exc:
        await once(store, "s", "k", "other", run)
    assert exc.value.kind == "mismatch"

    async def boom() -> StoredResponse:
        raise RuntimeError("turn failed")

    with pytest.raises(RuntimeError):
        await once(store, "s", "k2", "fp", boom)
    assert (await store.claim("s", "k2", "fp")).kind == "new", "a failed run released the key"


def test_store_selection_follows_redis_url() -> None:
    assert isinstance(build_idempotency_store(_settings("sel-mem")), MemoryIdempotencyStore)
    assert isinstance(
        build_idempotency_store(_settings("sel-redis", redis_url="redis://127.0.0.1:1/0")),
        RedisIdempotencyStore,
    )


def test_fingerprint_is_canonical_and_path_bound() -> None:
    assert request_fingerprint("/chat", {"a": 1, "b": [2]}) == request_fingerprint(
        "/chat", {"b": [2], "a": 1}
    )
    assert request_fingerprint("/chat", {"a": 1}) != request_fingerprint("/v1/chat/completions", {"a": 1})
    assert request_fingerprint("/chat", {"a": 1}) != request_fingerprint("/chat", {"a": 2})


def test_key_grammar() -> None:
    assert valid_key("k") and valid_key("a-b_c.1:2") and valid_key("x" * 255)
    for bad in ("", "x" * 256, "a b", "é", "{a}1", "a}"):
        assert not valid_key(bad), bad
