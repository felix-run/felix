"""`Idempotency-Key` for `POST /chat`: one turn per key per principal, with a TTL.

A client that times out on a turn and retries runs the turn twice — two model calls,
two usage rows, two session events — because nothing tied the retry to the first
attempt. With the header, the first request claims the key, the response is stored
against it when it completes, and a retry with the same key gets that response back
(`Idempotent-Replayed: true`) instead of a second turn. The same key with a different
body is refused, and a retry that arrives while the first attempt is still running is
told so rather than made to wait.

Scoped by `(tenant, principal)`: a key is the client's, and two callers choosing the
same one must never see each other's response. Per principal rather than per tenant
because a replay returns before the manifest's inbound auth (`required_scopes`,
`schemes`) runs — a response stored by a principal who passed those checks must not be
handed to one who would not. Redis-backed when `FELIX_REDIS_URL` is set, so a retry
landing on another replica still finds the claim; in-process otherwise, with the same
contract — the memory twin is the test path, not a mock.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from felix.redis_conn import RedisConnection

logger = logging.getLogger("felix.idempotency")

#: Visible ASCII, no whitespace, bounded so the key cannot be a payload. `{` and `}`
#: are excluded because a Redis Cluster reads the first `{...}` as the hash tag, and the
#: key is hashed into the Redis keyspace anyway.
KEY_MAX_LENGTH = 255

#: Ceiling on keys the in-process store tracks. It is also the Redis fallback, so
#: during an outage every keyed request would otherwise retain a whole response body
#: for the TTL — a memory-exhaustion path in the component meant to absorb retries.
MAX_TRACKED_KEYS = 50_000
EVICT_PER_HIT = 8

#: A response larger than this is not stored: the retry runs the turn again rather than
#: the store holding megabytes per key. Declined, never truncated.
MAX_STORED_BODY_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class StoredResponse:
    status: int
    body: dict[str, Any]


ClaimKind = Literal["new", "in_progress", "replay", "mismatch"]


@dataclass(frozen=True, slots=True)
class Claim:
    kind: ClaimKind
    #: The response, on `replay`.
    stored: StoredResponse | None = None
    #: Proof of ownership, on `new`: `finish` and `release` act only for the holder, so
    #: a claim that outlived its TTL cannot overwrite or free the next holder's.
    token: str = ""


class IdempotencyConflict(Exception):
    """The key cannot be used for this request: `in_progress` or `mismatch`."""

    def __init__(self, kind: ClaimKind) -> None:
        super().__init__(kind)
        self.kind = kind


class IdempotencyStore(Protocol):
    async def claim(self, scope: str, key: str, fingerprint: str) -> Claim: ...
    async def finish(self, scope: str, key: str, token: str, response: StoredResponse) -> bool: ...
    async def release(self, scope: str, key: str, token: str) -> None: ...


def valid_key(key: str) -> bool:
    return 0 < len(key) <= KEY_MAX_LENGTH and all(33 <= ord(ch) <= 126 and ch not in "{}" for ch in key)


def request_fingerprint(path: str, payload: Any) -> str:
    """What "the same request" means: the path and the canonical JSON of the body."""
    canonical = json.dumps(
        {"path": path, "body": payload}, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def principal_scope(tenant_id: str, principal_sub: str) -> str:
    return f"{tenant_id}/{principal_sub}"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def _redis_key(scope: str, key: str) -> str:
    # Both halves hashed. The client key so it neither selects a cluster slot nor sizes
    # the keyspace; the scope because `/` is legal in a tenant id and a `sub` is whatever
    # the IdP sent, so `acme` + `eu/alice` and `acme/eu` + `alice` would otherwise spell
    # one scope — and a `{...}` in a sub would put a hash tag back.
    return f"felix:idempotency:{_digest(scope)}:{_digest(key)}"


def _claim_from_record(record: dict[str, Any], fingerprint: str) -> Claim:
    if record.get("fingerprint") != fingerprint:
        return Claim("mismatch")
    stored = record.get("response")
    if stored is None:
        return Claim("in_progress")
    return Claim("replay", StoredResponse(int(stored["status"]), dict(stored["body"])))


def _record(fingerprint: str, token: str, response: StoredResponse | None) -> dict[str, Any]:
    body = None if response is None else {"status": response.status, "body": response.body}
    return {"fingerprint": fingerprint, "token": token, "response": body}


def storable(response: StoredResponse) -> bool:
    return (
        len(json.dumps(response.body, separators=(",", ":"), default=str).encode()) <= MAX_STORED_BODY_BYTES
    )


class MemoryIdempotencyStore:
    """Per-process twin of the Redis store. Same contract, no network, bounded."""

    def __init__(self, ttl_seconds: float, *, now: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._now = now
        #: (scope, key) -> (expires_at, record), in last-write order.
        self._records: OrderedDict[tuple[str, str], tuple[float, dict[str, Any]]] = OrderedDict()

    def _evict_some(self) -> None:
        """Expire a bounded number of the oldest entries, then hold the ceiling.

        Bounded, so the cost per call is constant however many keys are tracked; the
        ceiling drops the oldest live entry when the store is full, which turns one
        client's retry into a second turn rather than the process into an OOM.
        """
        now = self._now()
        for _ in range(EVICT_PER_HIT):
            if not self._records:
                break
            oldest, (expires_at, _) = next(iter(self._records.items()))
            if expires_at > now:
                break
            del self._records[oldest]
        while len(self._records) > MAX_TRACKED_KEYS:
            self._records.popitem(last=False)

    def _live(self, scope: str, key: str) -> dict[str, Any] | None:
        entry = self._records.get((scope, key))
        if entry is None:
            return None
        expires_at, record = entry
        if self._now() >= expires_at:
            del self._records[(scope, key)]
            return None
        return record

    def _put(self, scope: str, key: str, record: dict[str, Any]) -> None:
        self._records.pop((scope, key), None)
        self._records[(scope, key)] = (self._now() + self._ttl, record)
        self._evict_some()

    async def claim(self, scope: str, key: str, fingerprint: str) -> Claim:
        record = self._live(scope, key)
        if record is not None:
            return _claim_from_record(record, fingerprint)
        token = secrets.token_urlsafe(16)
        self._put(scope, key, _record(fingerprint, token, None))
        return Claim("new", token=token)

    async def finish(self, scope: str, key: str, token: str, response: StoredResponse) -> bool:
        record = self._live(scope, key)
        if record is None or record.get("token") != token:
            return False
        if not storable(response):
            await self.release(scope, key, token)
            return False
        self._put(scope, key, _record(record["fingerprint"], token, response))
        return True

    async def release(self, scope: str, key: str, token: str) -> None:
        record = self._live(scope, key)
        if record is not None and record.get("token") == token:
            del self._records[(scope, key)]


# Compare-and-set on the holder's token, atomically: `finish` and `release` act only on
# the record this attempt claimed. Without it a claim that outlived its TTL would
# overwrite, or free, the next holder's.
_FINISH_IF_HELD = """
local cur = redis.call('GET', KEYS[1])
if not cur then return 0 end
if cjson.decode(cur)['token'] ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
return 1
"""
_RELEASE_IF_HELD = """
local cur = redis.call('GET', KEYS[1])
if not cur then return 0 end
if cjson.decode(cur)['token'] ~= ARGV[1] then return 0 end
redis.call('DEL', KEYS[1])
return 1
"""


class RedisIdempotencyStore:
    """Claims live in Redis so a retry on another replica finds the first attempt.

    The claim is `SET NX EX`: exactly one request wins the key, atomically. The client
    comes from the shared `RedisConnection`, so a failure backs off instead of paying a
    connect timeout per request, and a loop change is noticed. While Redis is away the
    in-process twin takes over — a retry then dedupes only within this replica, which is
    weaker, not wrong; refusing every keyed request because Redis blipped would be the
    outage the header exists to smooth. The transition is logged once each way.
    """

    def __init__(self, ttl_seconds: int, *, connection: RedisConnection | None = None) -> None:
        self._ttl = ttl_seconds
        self._conn = connection or RedisConnection("idempotency")
        self._fallback = MemoryIdempotencyStore(ttl_seconds)
        self._degraded = False

    async def _client(self) -> Any | None:
        client = await self._conn.get()
        if client is None and not self._degraded:
            self._degraded = True
            logger.warning("idempotency: redis unavailable; deduplicating within this replica only")
        return client

    def _ok(self) -> None:
        """A command succeeded: the recovery transition. Not on `get()` returning a
        client — a Redis that answers PING and refuses commands would flap once per
        request between "reachable again" and "failed"."""
        if self._degraded:
            self._degraded = False
            logger.info("idempotency: redis reachable again")

    async def _failed(self, what: str) -> None:
        """A command failed against a Redis that answered `PING`: OOM under `noeviction`,
        a READONLY replica, MOVED on a cluster. Reachable-but-refusing is a degradation
        like unreachable, and `aclose()` alone would not say so once above DEBUG."""
        if not self._degraded:
            self._degraded = True
            logger.warning(
                "idempotency: redis %s failed; deduplicating within this replica only", what, exc_info=True
            )
        await self._conn.aclose()

    async def claim(self, scope: str, key: str, fingerprint: str) -> Claim:
        client = await self._client()
        if client is None:
            return await self._fallback.claim(scope, key, fingerprint)
        rkey = _redis_key(scope, key)
        try:
            # Bounded: `raw is None` means the record expired between SET and GET, so the
            # key is free again; a pathological TTL must not recurse to a 500.
            for _ in range(3):
                token = secrets.token_urlsafe(16)
                if await client.set(
                    rkey, json.dumps(_record(fingerprint, token, None)), nx=True, ex=self._ttl
                ):
                    self._ok()
                    return Claim("new", token=token)
                raw = await client.get(rkey)
                if raw is not None:
                    self._ok()
                    return _claim_from_record(json.loads(raw), fingerprint)
            raise IdempotencyConflict("in_progress")
        except IdempotencyConflict:
            raise
        except Exception:
            await self._failed("claim")
            return await self._fallback.claim(scope, key, fingerprint)

    async def finish(self, scope: str, key: str, token: str, response: StoredResponse) -> bool:
        if not storable(response):
            await self.release(scope, key, token)
            return False
        client = await self._client()
        if client is None:
            return await self._fallback.finish(scope, key, token, response)
        try:
            # The fingerprint lives in the held record; only the holder can finish, so the
            # script's token check is what makes reading it first safe.
            held = await client.get(_redis_key(scope, key))
            if held is None:
                return False
            fingerprint = json.loads(held).get("fingerprint", "")
            record = json.dumps(_record(fingerprint, token, response))
            stored = bool(
                await client.eval(_FINISH_IF_HELD, 1, _redis_key(scope, key), token, record, self._ttl)
            )
            self._ok()
            return stored
        except Exception:
            await self._failed("finish")
            return await self._fallback.finish(scope, key, token, response)

    async def release(self, scope: str, key: str, token: str) -> None:
        client = await self._client()
        if client is not None:
            try:
                await client.eval(_RELEASE_IF_HELD, 1, _redis_key(scope, key), token)
                self._ok()
            except Exception:
                await self._failed("release")
        await self._fallback.release(scope, key, token)


def build_idempotency_store(settings: Any) -> IdempotencyStore:
    """One store per app: Redis-backed when a Redis is configured, in-process otherwise."""
    ttl = int(settings.idempotency_ttl_seconds)
    if (getattr(settings, "redis_url", "") or "").strip():
        return RedisIdempotencyStore(ttl)
    return MemoryIdempotencyStore(ttl)


async def once(
    store: IdempotencyStore,
    scope: str,
    key: str,
    fingerprint: str,
    run: Callable[[], Awaitable[StoredResponse]],
) -> tuple[StoredResponse, bool]:
    """Run ``run`` once for ``(scope, key)``; ``(response, replayed)``.

    The whole choreography, HTTP-free, so every surface that takes the header gets the
    same semantics: a replay returns the stored response; `in_progress` and `mismatch`
    raise `IdempotencyConflict`; a `run` that raises releases the key so the retry runs.
    """
    claim = await store.claim(scope, key, fingerprint)
    if claim.kind == "replay" and claim.stored is not None:
        return claim.stored, True
    if claim.kind != "new":
        raise IdempotencyConflict(claim.kind)
    try:
        response = await run()
    except BaseException:
        await store.release(scope, key, claim.token)
        raise
    await store.finish(scope, key, claim.token, response)
    return response, False


__all__ = [
    "KEY_MAX_LENGTH",
    "MAX_STORED_BODY_BYTES",
    "MAX_TRACKED_KEYS",
    "Claim",
    "IdempotencyConflict",
    "IdempotencyStore",
    "MemoryIdempotencyStore",
    "RedisIdempotencyStore",
    "StoredResponse",
    "build_idempotency_store",
    "once",
    "principal_scope",
    "request_fingerprint",
    "valid_key",
]
