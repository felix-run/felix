"""Session leases — exclusive coordinator vs shared observers.

Uses Redis when available so leases work across API replicas; falls back to
in-process state for single-process / unit tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("felix.session.lease")

# thread_id -> Lease (in-process fallback)
_leases: dict[str, SessionLease] = {}
_force_memory = False
_redis: Any | None = None
_redis_loop: int | None = None
_redis_failed = False


@dataclass(slots=True)
class SessionLease:
    thread_id: str
    holder_id: str
    token: str
    mode: str  # "exclusive" | "shared"
    acquired_at: float
    expires_at: float
    observers: set[str] = field(default_factory=set)


def _now() -> float:
    return time.time()


def _redis_key(thread_id: str) -> str:
    return f"felix:lease:{thread_id}"


async def _get_redis() -> Any | None:
    global _redis, _redis_loop, _redis_failed
    if _force_memory:
        return None
    loop_id = id(asyncio.get_running_loop())
    if _redis is not None and _redis_loop != loop_id:
        with contextlib.suppress(Exception):
            await _redis.aclose()
        _redis = None
        _redis_loop = None
        _redis_failed = False
    if _redis_failed:
        return None
    if _redis is not None:
        return _redis
    try:
        from felix.config import get_settings

        settings = get_settings()
        url = getattr(settings, "redis_url", "") or ""
        if not url:
            return None
        import redis.asyncio as redis

        client = redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=1.0,
            socket_timeout=2.0,
        )
        await client.ping()
        _redis = client
        _redis_loop = loop_id
        return _redis
    except Exception:
        logger.debug("lease redis unavailable; using in-process", exc_info=True)
        _redis_failed = True
        return None


def _status_from_payload(data: dict[str, Any] | None) -> dict[str, Any]:
    if not data:
        return {
            "locked": False,
            "attached": False,
            "holder_id": None,
            "mode": None,
            "observers": 0,
            "expires_at": None,
        }
    token = str(data.get("token") or "")
    return {
        "locked": data.get("mode") == "exclusive",
        "attached": True,
        "holder_id": data.get("holder_id"),
        "mode": data.get("mode"),
        "observers": len(data.get("observers") or []),
        "expires_at": int(float(data.get("expires_at") or 0) * 1000),
        "token_hint": token[:6] if token else None,
    }


def _purge_memory(thread_id: str) -> None:
    lease = _leases.get(thread_id)
    if lease is None:
        return
    if lease.expires_at <= _now():
        _leases.pop(thread_id, None)


def _memory_status(thread_id: str) -> dict[str, Any]:
    _purge_memory(thread_id)
    lease = _leases.get(thread_id)
    if lease is None:
        return _status_from_payload(None)
    return {
        "locked": lease.mode == "exclusive",
        "attached": True,
        "holder_id": lease.holder_id,
        "mode": lease.mode,
        "observers": len(lease.observers),
        "expires_at": int(lease.expires_at * 1000),
        "token_hint": lease.token[:6],
    }


def _memory_acquire(
    thread_id: str,
    *,
    holder_id: str,
    mode: str = "exclusive",
    ttl_seconds: float = 300.0,
    token: str | None = None,
) -> dict[str, Any]:
    _purge_memory(thread_id)
    mode_norm = "shared" if mode == "shared" else "exclusive"
    ttl = max(5.0, float(ttl_seconds))
    existing = _leases.get(thread_id)

    if existing is not None:
        if existing.mode == "exclusive" and existing.holder_id != holder_id:
            return {
                "ok": False,
                "error": "lease_held",
                "status": _memory_status(thread_id),
            }
        if existing.holder_id == holder_id:
            existing.expires_at = _now() + ttl
            existing.mode = mode_norm
            if token:
                existing.token = token
            return {
                "ok": True,
                "renewed": True,
                "token": existing.token,
                "status": _memory_status(thread_id),
            }
        if mode_norm == "shared":
            existing.observers.add(holder_id)
            existing.expires_at = max(existing.expires_at, _now() + ttl)
            return {
                "ok": True,
                "renewed": False,
                "token": existing.token,
                "status": _memory_status(thread_id),
            }
        return {
            "ok": False,
            "error": "lease_held",
            "status": _memory_status(thread_id),
        }

    new_token = token or secrets.token_urlsafe(16)
    _leases[thread_id] = SessionLease(
        thread_id=thread_id,
        holder_id=holder_id,
        token=new_token,
        mode=mode_norm,
        acquired_at=_now(),
        expires_at=_now() + ttl,
        observers={holder_id} if mode_norm == "shared" else set(),
    )
    return {
        "ok": True,
        "renewed": False,
        "token": new_token,
        "status": _memory_status(thread_id),
    }


def _memory_release(
    thread_id: str,
    *,
    holder_id: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    _purge_memory(thread_id)
    lease = _leases.get(thread_id)
    if lease is None:
        return {"ok": True, "released": False, "status": _memory_status(thread_id)}
    if token and lease.token != token:
        return {"ok": False, "error": "token_mismatch", "status": _memory_status(thread_id)}
    if holder_id and lease.holder_id != holder_id and holder_id not in lease.observers:
        return {"ok": False, "error": "not_holder", "status": _memory_status(thread_id)}
    if holder_id and holder_id in lease.observers and lease.holder_id != holder_id:
        lease.observers.discard(holder_id)
        return {"ok": True, "released": True, "status": _memory_status(thread_id)}
    _leases.pop(thread_id, None)
    return {"ok": True, "released": True, "status": _memory_status(thread_id)}


async def _load_remote(client: Any, thread_id: str) -> dict[str, Any] | None:
    raw = await client.get(_redis_key(thread_id))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except TypeError, json.JSONDecodeError:
        await client.delete(_redis_key(thread_id))
        return None
    if float(data.get("expires_at") or 0) <= _now():
        await client.delete(_redis_key(thread_id))
        return None
    return data


async def _store_remote(client: Any, thread_id: str, data: dict[str, Any], ttl: float) -> None:
    ttl_i = max(5, int(ttl))
    await client.set(_redis_key(thread_id), json.dumps(data), ex=ttl_i)


async def lease_status(thread_id: str) -> dict[str, Any]:
    client = await _get_redis()
    if client is None:
        return _memory_status(thread_id)
    try:
        data = await _load_remote(client, thread_id)
        return _status_from_payload(data)
    except Exception:
        logger.debug("lease redis status failed", exc_info=True)
        return _memory_status(thread_id)


async def acquire_lease(
    thread_id: str,
    *,
    holder_id: str,
    mode: str = "exclusive",
    ttl_seconds: float = 300.0,
    token: str | None = None,
) -> dict[str, Any]:
    """Acquire or renew a lease. Exclusive fails if another holder owns it."""
    client = await _get_redis()
    if client is None:
        return _memory_acquire(
            thread_id,
            holder_id=holder_id,
            mode=mode,
            ttl_seconds=ttl_seconds,
            token=token,
        )

    mode_norm = "shared" if mode == "shared" else "exclusive"
    ttl = max(5.0, float(ttl_seconds))
    try:
        existing = await _load_remote(client, thread_id)
        if existing is not None:
            if existing.get("mode") == "exclusive" and existing.get("holder_id") != holder_id:
                return {
                    "ok": False,
                    "error": "lease_held",
                    "status": _status_from_payload(existing),
                }
            if existing.get("holder_id") == holder_id:
                existing["expires_at"] = _now() + ttl
                existing["mode"] = mode_norm
                if token:
                    existing["token"] = token
                await _store_remote(client, thread_id, existing, ttl)
                return {
                    "ok": True,
                    "renewed": True,
                    "token": existing["token"],
                    "status": _status_from_payload(existing),
                }
            if mode_norm == "shared":
                observers = list(existing.get("observers") or [])
                if holder_id not in observers:
                    observers.append(holder_id)
                existing["observers"] = observers
                existing["expires_at"] = max(float(existing.get("expires_at") or 0), _now() + ttl)
                await _store_remote(client, thread_id, existing, ttl)
                return {
                    "ok": True,
                    "renewed": False,
                    "token": existing.get("token"),
                    "status": _status_from_payload(existing),
                }
            return {
                "ok": False,
                "error": "lease_held",
                "status": _status_from_payload(existing),
            }

        new_token = token or secrets.token_urlsafe(16)
        payload = {
            "holder_id": holder_id,
            "token": new_token,
            "mode": mode_norm,
            "acquired_at": _now(),
            "expires_at": _now() + ttl,
            "observers": [holder_id] if mode_norm == "shared" else [],
        }
        # SET NX avoids two replicas claiming exclusive at once.
        ok = await client.set(
            _redis_key(thread_id),
            json.dumps(payload),
            nx=True,
            ex=max(5, int(ttl)),
        )
        if not ok:
            raced = await _load_remote(client, thread_id)
            return {
                "ok": False,
                "error": "lease_held",
                "status": _status_from_payload(raced),
            }
        return {
            "ok": True,
            "renewed": False,
            "token": new_token,
            "status": _status_from_payload(payload),
        }
    except Exception:
        logger.debug("lease redis acquire failed; using in-process", exc_info=True)
        return _memory_acquire(
            thread_id,
            holder_id=holder_id,
            mode=mode,
            ttl_seconds=ttl_seconds,
            token=token,
        )


async def release_lease(
    thread_id: str,
    *,
    holder_id: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    client = await _get_redis()
    if client is None:
        return _memory_release(thread_id, holder_id=holder_id, token=token)

    try:
        existing = await _load_remote(client, thread_id)
        if existing is None:
            return {"ok": True, "released": False, "status": _status_from_payload(None)}
        if token and existing.get("token") != token:
            return {
                "ok": False,
                "error": "token_mismatch",
                "status": _status_from_payload(existing),
            }
        observers = list(existing.get("observers") or [])
        if holder_id and existing.get("holder_id") != holder_id and holder_id not in observers:
            return {
                "ok": False,
                "error": "not_holder",
                "status": _status_from_payload(existing),
            }
        if holder_id and holder_id in observers and existing.get("holder_id") != holder_id:
            observers = [o for o in observers if o != holder_id]
            existing["observers"] = observers
            remaining = max(5.0, float(existing.get("expires_at") or 0) - _now())
            await _store_remote(client, thread_id, existing, remaining)
            return {
                "ok": True,
                "released": True,
                "status": _status_from_payload(existing),
            }
        await client.delete(_redis_key(thread_id))
        return {"ok": True, "released": True, "status": _status_from_payload(None)}
    except Exception:
        logger.debug("lease redis release failed; using in-process", exc_info=True)
        return _memory_release(thread_id, holder_id=holder_id, token=token)


def reset_leases_for_tests() -> None:
    """Clear in-process leases and force memory backend for deterministic unit tests."""
    global _force_memory, _redis_failed
    _leases.clear()
    _force_memory = True
    _redis_failed = False


__all__ = [
    "SessionLease",
    "acquire_lease",
    "lease_status",
    "release_lease",
    "reset_leases_for_tests",
]
