"""Session leases — one exclusive holder that drives a thread, and read-only observers.

A lease is keyed by thread and holds two kinds of entry, each with its own token and its own
expiry:

- **the exclusive hold**, at most one. Another holder's exclusive acquire is `lease_held`
  (409) while it lives. Re-acquiring with its token renews it; its holder id alone does not,
  because holder ids are published and a duplicated browser tab shares one.
- **observer holds**, any number. A `shared` acquire always succeeds, whoever holds the
  thread exclusively: it is how a second tab watches a thread the first one is driving.

The two kinds do not lend each other anything. An observer's acquire or renewal extends its
own entry and never the exclusive hold — it once extended the whole lease, so a client that
renewed an observer hold kept a closed tab's exclusive hold alive. An observer's token is
its own, never the exclusive one, so it cannot pass for the holder (`lease_write_refusal`).
When the exclusive hold is released or lapses, observers stay observers: nobody is promoted,
and a client that wants to drive takes the exclusive hold itself — observers never block it.

Uses Redis when available so leases work across API replicas; falls back to in-process
state for single-process / unit tests. Every transition is one pure function over the
stored payload, applied under a lock on either arm (`WATCH`/`MULTI` on Redis), so the two
arms cannot disagree about what a transition does.
"""

from __future__ import annotations

import json
import logging
import math
import secrets
import time
from collections.abc import Callable
from typing import Any

from felix.redis_conn import RedisConnection

logger = logging.getLogger("felix.session.lease")

EXCLUSIVE = "exclusive"
SHARED = "shared"

# thread_id -> stored payload (in-process fallback); the same shape the Redis arm stores.
_leases: dict[str, dict[str, Any]] = {}
_force_memory = False
# Was a hand-rolled client that latched `_redis_failed = True` for the life of the process
# after one blip, at debug — the two defects `RedisConnection` exists to remove, one module
# over from the three it already covered.
_conn = RedisConnection(
    "session leases",
    fallback_consequence="an exclusive lease is granted per replica, so two replicas can each hold it",
)

# A transition that loses its `WATCH` race re-reads and tries again; past this many it
# answers `lease_contended` rather than spin.
_CAS_ATTEMPTS = 5

# What a transition returns: the payload to store (None deletes the lease) and the answer.
Transition = Callable[[dict[str, Any] | None, float], tuple[dict[str, Any] | None, dict[str, Any]]]


def _now() -> float:
    return time.time()


def _redis_key(thread_id: str) -> str:
    return f"felix:lease:{thread_id}"


def _assert_tenant_scoped(thread_id: str) -> None:
    """Refuse a lease on a thread id that carries no tenant prefix.

    The lease key is the thread id alone, so the tenant segment of `{tenant}:{suffix}` is
    the only thing keeping one tenant's lease out of another's namespace. That held by
    convention: every id reaching here came from `effective_thread_id`, which prefixes.

    Convention is not a boundary. A caller that builds an id without the prefix — a new
    route, a job, a plugin — would silently share a lease namespace across tenants, and
    nothing would fail. The tenant id itself can no longer contain the delimiter, so a
    prefix present here is unambiguous.
    """
    if not thread_id or ":" not in thread_id:
        raise ValueError(
            f"lease requires a tenant-scoped thread id ('{{tenant}}:{{suffix}}'), got {thread_id!r}"
        )


async def _get_redis() -> Any | None:
    """The shared client, or None under `_force_memory` (tests) and on the fallback."""
    if _force_memory:
        return None
    return await _conn.get()


# --- the payload, and the pure transitions both arms apply --------------------------------


def _empty(now: float) -> dict[str, Any]:
    return {
        "holder_id": None,
        "token": None,
        "mode": None,
        "acquired_at": now,
        "expires_at": 0.0,
        "observers": {},
    }


def _normalize(data: Any) -> dict[str, Any] | None:
    """The stored payload in its current shape, whichever shape it was written in.

    Before observers had entries of their own, a lease was one holder with a list of observer
    ids sharing its token and its expiry, and a `shared` lease's `holder_id` was simply its
    first observer. A payload in that shape — a Redis key written by an earlier release, live
    across the upgrade — reads as what it meant: an `exclusive` one keeps its holder, and
    every id of a `shared` one becomes an observer holding the token it was handed.
    """
    if not isinstance(data, dict):
        return None
    expires = float(data.get("expires_at") or 0)
    token = data.get("token") or None
    holder = data.get("holder_id") or None
    raw = data.get("observers") or {}
    if isinstance(raw, list):
        ids = [str(o) for o in raw]
        if data.get("mode") == SHARED and holder:
            ids.append(str(holder))
        # Only a `shared` lease's ids were handed its token; none ever joined an exclusive one.
        shared_token = (token or "") if data.get("mode") == SHARED else ""
        observers = {o: {"token": shared_token, "expires_at": expires} for o in ids}
        if data.get("mode") == SHARED:
            holder, token, expires = None, None, 0.0
    elif isinstance(raw, dict):
        observers = {
            str(k): {"token": str(v.get("token") or ""), "expires_at": float(v.get("expires_at") or 0)}
            for k, v in raw.items()
            if isinstance(v, dict)
        }
    else:
        observers = {}
    return {
        "holder_id": holder,
        "token": token if holder else None,
        "mode": data.get("mode"),
        "acquired_at": float(data.get("acquired_at") or 0),
        "expires_at": expires if holder else 0.0,
        "observers": observers,
    }


def _prune(data: dict[str, Any] | None, now: float) -> dict[str, Any] | None:
    """Drop whichever entries have lapsed — each on its own clock — and None when none is left."""
    if data is None:
        return None
    if data["holder_id"] and float(data["expires_at"]) <= now:
        data.update(holder_id=None, token=None, expires_at=0.0)
    data["observers"] = {k: v for k, v in data["observers"].items() if float(v["expires_at"]) > now}
    if not data["holder_id"] and not data["observers"]:
        return None
    data["mode"] = EXCLUSIVE if data["holder_id"] else SHARED
    return data


def _lifetime(data: dict[str, Any]) -> float:
    """When the last entry lapses: how long the stored lease has to outlive."""
    return max([float(data["expires_at"]), *(float(v["expires_at"]) for v in data["observers"].values())])


def _status(data: dict[str, Any] | None) -> dict[str, Any]:
    if not data:
        return {
            "locked": False,
            "attached": False,
            "holder_id": None,
            "mode": None,
            "observers": 0,
            "observer_holds": [],
            "expires_at": None,
            "token_hint": None,
        }
    holder = data["holder_id"]
    token = str(data.get("token") or "")
    return {
        "locked": bool(holder),
        "attached": True,
        # The exclusive holder, and only it: an observer-only lease has none.
        "holder_id": holder,
        "mode": data["mode"],
        "observers": len(data["observers"]),
        "observer_holds": [
            {"holder_id": h, "expires_at": int(float(e["expires_at"]) * 1000)}
            for h, e in sorted(data["observers"].items())
        ],
        # The exclusive hold's expiry when there is one, else the last observer's.
        "expires_at": int((float(data["expires_at"]) if holder else _lifetime(data)) * 1000),
        "token_hint": token[:6] if token else None,
    }


def _acquire(
    data: dict[str, Any] | None, now: float, *, holder_id: str, mode: str, ttl: float, token: str | None
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Take or renew a hold. A renewal proves itself with the hold's token, never its holder id.

    The holder id is published — every status carries it, and `GET /chat/sessions/{id}/lease`
    lists every observer's — and a duplicated browser tab copies its own. So a holder id that
    matches is no proof: without the token it is refused as anyone else would be, and it
    never learns the token.
    """
    lease = data or _empty(now)
    holder = lease["holder_id"]
    observer = lease["observers"].get(holder_id)
    refused = {"ok": False, "error": "lease_held", "status": _status(data)}
    if mode == EXCLUSIVE:
        if holder:
            if holder != holder_id or not token or token != lease["token"]:
                return data, refused
            renewed = True
        else:
            # Free, or held only by observers — who never block it. An observer taking the
            # exclusive hold with its own token stops being an observer.
            if observer is not None and token and token == observer["token"]:
                lease["observers"].pop(holder_id)
            lease.update(holder_id=holder_id, token=token or secrets.token_urlsafe(16), acquired_at=now)
            renewed = False
        lease["expires_at"] = now + ttl
        granted = lease["token"]
        held_by_other = False
    elif holder and holder == holder_id and token and token == lease["token"]:
        # The exclusive holder asking to watch steps down to an observer, keeping its token,
        # as a renew in the other mode always took the mode it asked for.
        lease["observers"][holder_id] = {"token": lease["token"], "expires_at": now + ttl}
        lease.update(holder_id=None, token=None, expires_at=0.0)
        granted, renewed, held_by_other = lease["observers"][holder_id]["token"], True, False
    else:
        if observer is not None:
            # Renewing an observer hold is the same proof: its own token.
            if not token or token != observer["token"]:
                return data, refused
            renewed = True
            granted = observer["token"]
        else:
            # A new observer, possibly under the exclusive holder's own id — a duplicated tab.
            # Always the server's token: a caller-chosen one could be the exclusive one.
            renewed = False
            granted = secrets.token_urlsafe(16)
        # Its own entry and nothing else: the exclusive hold's expiry is not this holder's
        # to extend.
        lease["observers"][holder_id] = {"token": granted, "expires_at": now + ttl}
        held_by_other = bool(lease["holder_id"])
    pruned = _prune(lease, now)
    return pruned, {
        "ok": True,
        "renewed": renewed,
        "token": granted,
        "mode": mode,
        "held_by_other": held_by_other,
        "status": _status(pruned),
    }


def _release(
    data: dict[str, Any] | None, now: float, *, holder_id: str | None, token: str | None
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Drop the one hold `token` names. `holder_id`, when given, must be that hold's holder.

    The token is required: a holder id is published, so it cannot be what releases a hold.
    """
    if data is None:
        return None, {"ok": True, "released": False, "status": _status(None)}
    if not token:
        return data, {"ok": False, "error": "token_required", "status": _status(data)}
    holder = data["holder_id"]
    observers = data["observers"]
    if holder and token == data["token"]:
        target, is_exclusive = holder, True
    else:
        target = next((h for h, e in observers.items() if e["token"] and e["token"] == token), None)
        is_exclusive = False
    if target is None:
        known = holder_id is None or holder_id == holder or holder_id in observers
        error = "token_mismatch" if known else "not_holder"
        return data, {"ok": False, "error": error, "status": _status(data)}
    if holder_id and holder_id != target:
        return data, {"ok": False, "error": "token_mismatch", "status": _status(data)}
    if is_exclusive:
        # Observers stay observers: nobody is promoted to drive the thread.
        data.update(holder_id=None, token=None, expires_at=0.0)
    else:
        observers.pop(target, None)
    pruned = _prune(data, now)
    return pruned, {"ok": True, "released": True, "status": _status(pruned)}


def _write_refusal(data: dict[str, Any] | None, token: str) -> str | None:
    """Why a caller presenting `token` may not drive the thread, or None when it may."""
    if data is None:
        return None
    if data["holder_id"] and token == data["token"]:
        return None
    if any(e["token"] == token for e in data["observers"].values()):
        return "lease_read_only"
    if data["holder_id"]:
        return "lease_held"
    # Nobody holds it exclusively and this is no observer's token: a lapsed hold, on a thread
    # nobody else is driving. Refusing would only punish a client for sleeping.
    return None


# --- the two arms ---------------------------------------------------------------------------


def _memory_read(thread_id: str) -> dict[str, Any] | None:
    data = _prune(_normalize(_leases.get(thread_id)), _now())
    if data is None:
        _leases.pop(thread_id, None)
    return data


def _memory_apply(thread_id: str, transition: Transition) -> dict[str, Any]:
    new, result = transition(_memory_read(thread_id), _now())
    if new is None:
        _leases.pop(thread_id, None)
    else:
        _leases[thread_id] = new
    return result


def _decode(raw: Any) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        return _normalize(json.loads(raw))
    except TypeError, json.JSONDecodeError:
        return None


async def _redis_read(client: Any, thread_id: str) -> dict[str, Any] | None:
    return _prune(_decode(await client.get(_redis_key(thread_id))), _now())


async def _redis_apply(client: Any, thread_id: str, transition: Transition) -> dict[str, Any]:
    """Apply `transition` atomically: `WATCH` the key, compute, write under `MULTI`.

    A plain read-then-write let two replicas each read a lease with no exclusive holder
    and each grant it. Losing the race re-reads, so the loser sees the winner's hold.
    """
    from redis.exceptions import WatchError

    key = _redis_key(thread_id)
    for _ in range(_CAS_ATTEMPTS):
        async with client.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                raw = await pipe.get(key)
                now = _now()
                current = _prune(_decode(raw), now)
                new, result = transition(current, now)
                if not result.get("ok"):
                    return result
                pipe.multi()
                if new is None:
                    pipe.delete(key)
                else:
                    ttl = max(5, math.ceil(_lifetime(new) - now))
                    pipe.set(key, json.dumps(new), ex=ttl)
                await pipe.execute()
                return result
            except WatchError:
                continue
    return {"ok": False, "error": "lease_contended", "status": _status(await _redis_read(client, thread_id))}


async def _apply(thread_id: str, transition: Transition, what: str) -> dict[str, Any]:
    client = await _get_redis()
    if client is None:
        return _memory_apply(thread_id, transition)
    try:
        return await _redis_apply(client, thread_id, transition)
    except Exception:
        await _conn.fallback(what)
        return _memory_apply(thread_id, transition)


async def _read(thread_id: str, what: str) -> dict[str, Any] | None:
    client = await _get_redis()
    if client is None:
        return _memory_read(thread_id)
    try:
        return await _redis_read(client, thread_id)
    except Exception:
        await _conn.fallback(what)
        return _memory_read(thread_id)


# --- the surface ----------------------------------------------------------------------------


async def lease_status(thread_id: str) -> dict[str, Any]:
    """The exclusive holder (`holder_id`, `locked`), and every observer (`observer_holds`)."""
    _assert_tenant_scoped(thread_id)
    return _status(await _read(thread_id, "lease redis status"))


async def acquire_lease(
    thread_id: str,
    *,
    holder_id: str,
    mode: str = EXCLUSIVE,
    ttl_seconds: float = 300.0,
    token: str | None = None,
) -> dict[str, Any]:
    """Take or renew a hold. `exclusive` fails while another holder has it; `shared` never does.

    The answer carries the hold's own `token`, the `mode` it was granted in, and
    `held_by_other` — whether someone else is driving the thread, which is what tells an
    observer it is one.
    """
    _assert_tenant_scoped(thread_id)
    mode_norm = SHARED if mode == SHARED else EXCLUSIVE
    ttl = max(5.0, float(ttl_seconds))

    def transition(data: dict[str, Any] | None, now: float) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        return _acquire(data, now, holder_id=holder_id, mode=mode_norm, ttl=ttl, token=token)

    return await _apply(thread_id, transition, "lease redis acquire")


async def release_lease(
    thread_id: str,
    *,
    holder_id: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """Drop the one hold `token` names, leaving every other in place. The token is required."""
    _assert_tenant_scoped(thread_id)

    def transition(data: dict[str, Any] | None, now: float) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        return _release(data, now, holder_id=holder_id, token=token)

    return await _apply(thread_id, transition, "lease redis release")


async def lease_write_refusal(thread_id: str, token: str) -> str | None:
    """Why the caller holding `token` may not drive `thread_id`: an error code, or None.

    `lease_read_only` for an observer's token, `lease_held` when someone else holds the
    thread exclusively. Only the exclusive hold's own token passes while one exists.
    """
    _assert_tenant_scoped(thread_id)
    return _write_refusal(await _read(thread_id, "lease redis write check"), token)


def reset_leases_for_tests() -> None:
    """Clear in-process leases and force memory backend for deterministic unit tests."""
    global _force_memory
    _leases.clear()
    _force_memory = True


__all__ = [
    "EXCLUSIVE",
    "SHARED",
    "acquire_lease",
    "lease_status",
    "lease_write_refusal",
    "release_lease",
    "reset_leases_for_tests",
]
