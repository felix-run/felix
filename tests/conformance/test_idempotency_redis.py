"""The Redis idempotency store against a real Redis: the CAS scripts, and SET NX EX.

The unit tests drive `RedisIdempotencyStore` with a fake whose `eval` interprets the
scripts' *intent*, so the Lua text itself — the token compare that keeps a claim that
outlived its TTL from touching the next holder's — is exercised only here.

Needs a reachable Redis (`FELIX_CONFORMANCE_REDIS_URL`). CI sets
`FELIX_CONFORMANCE_REQUIRE_REDIS` so a missing one fails rather than skipping.
"""

from __future__ import annotations

import os
import uuid

import pytest
from felix.idempotency import Claim, RedisIdempotencyStore, StoredResponse

REDIS_URL_ENV = "FELIX_CONFORMANCE_REDIS_URL"
REQUIRE_REDIS_ENV = "FELIX_CONFORMANCE_REQUIRE_REDIS"


class _Conn:
    def __init__(self, client) -> None:
        self.client = client

    async def get(self):
        return self.client

    async def aclose(self) -> None:
        pass


@pytest.fixture
async def store():
    url = os.environ.get(REDIS_URL_ENV, "").strip()
    if not url:
        if os.environ.get(REQUIRE_REDIS_ENV):
            pytest.fail(f"{REQUIRE_REDIS_ENV} is set but {REDIS_URL_ENV} is empty")
        pytest.skip(f"{REDIS_URL_ENV} not set")
    import redis.asyncio as redis

    client = redis.from_url(url, decode_responses=True, socket_connect_timeout=1.0)
    try:
        await client.ping()
    except Exception as exc:  # pragma: no cover - environment
        if os.environ.get(REQUIRE_REDIS_ENV):
            raise
        pytest.skip(f"redis at {url} unreachable: {exc}")
    try:
        yield RedisIdempotencyStore(60, connection=_Conn(client))  # type: ignore[arg-type]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_finish_and_release_act_only_for_the_holder(store: RedisIdempotencyStore) -> None:
    scope, key = "conformance/" + uuid.uuid4().hex, "k"
    first = await store.claim(scope, key, "fp")
    assert first.kind == "new" and first.token
    assert (await store.claim(scope, key, "fp")).kind == "in_progress", "SET NX: one winner"

    assert await store.finish(scope, key, "not-the-holder", StoredResponse(200, {"who": "intruder"})) is False
    assert (await store.claim(scope, key, "fp")).kind == "in_progress", "a stranger's finish stored nothing"
    await store.release(scope, key, "not-the-holder")
    assert (await store.claim(scope, key, "fp")).kind == "in_progress", "a stranger's release freed nothing"

    assert await store.finish(scope, key, first.token, StoredResponse(202, {"resume_token": "x"})) is True
    assert (await store.claim(scope, key, "fp")) == Claim(
        "replay", StoredResponse(202, {"resume_token": "x"})
    )
    assert (await store.claim(scope, key, "other")).kind == "mismatch"

    await store.release(scope, key, first.token)
    assert (await store.claim(scope, key, "fp")).kind == "new"
