"""Session leases — exclusive coordinator vs shared observers."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any

# thread_id -> Lease
_leases: dict[str, "SessionLease"] = {}


@dataclass(slots=True)
class SessionLease:
    thread_id: str
    holder_id: str
    token: str
    mode: str  # "exclusive" | "shared"
    acquired_at: float
    expires_at: float
    observers: set[str]


def _now() -> float:
    return time.time()


def _purge(thread_id: str) -> None:
    lease = _leases.get(thread_id)
    if lease is None:
        return
    if lease.expires_at <= _now():
        _leases.pop(thread_id, None)


def lease_status(thread_id: str) -> dict[str, Any]:
    _purge(thread_id)
    lease = _leases.get(thread_id)
    if lease is None:
        return {
            "locked": False,
            "attached": False,
            "holder_id": None,
            "mode": None,
            "observers": 0,
            "expires_at": None,
        }
    return {
        "locked": lease.mode == "exclusive",
        "attached": True,
        "holder_id": lease.holder_id,
        "mode": lease.mode,
        "observers": len(lease.observers),
        "expires_at": int(lease.expires_at * 1000),
        "token_hint": lease.token[:6],
    }


def acquire_lease(
    thread_id: str,
    *,
    holder_id: str,
    mode: str = "exclusive",
    ttl_seconds: float = 300.0,
    token: str | None = None,
) -> dict[str, Any]:
    """Acquire or renew a lease. Exclusive fails if another holder owns it."""
    _purge(thread_id)
    mode_norm = "shared" if mode == "shared" else "exclusive"
    ttl = max(5.0, float(ttl_seconds))
    existing = _leases.get(thread_id)

    if existing is not None:
        if existing.mode == "exclusive" and existing.holder_id != holder_id:
            return {
                "ok": False,
                "error": "lease_held",
                "status": lease_status(thread_id),
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
                "status": lease_status(thread_id),
            }
        # Shared: add observer
        if mode_norm == "shared":
            existing.observers.add(holder_id)
            existing.expires_at = max(existing.expires_at, _now() + ttl)
            return {
                "ok": True,
                "renewed": False,
                "token": existing.token,
                "status": lease_status(thread_id),
            }
        return {
            "ok": False,
            "error": "lease_held",
            "status": lease_status(thread_id),
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
        "status": lease_status(thread_id),
    }


def release_lease(
    thread_id: str,
    *,
    holder_id: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    _purge(thread_id)
    lease = _leases.get(thread_id)
    if lease is None:
        return {"ok": True, "released": False, "status": lease_status(thread_id)}
    if token and lease.token != token:
        return {"ok": False, "error": "token_mismatch", "status": lease_status(thread_id)}
    if holder_id and lease.holder_id != holder_id and holder_id not in lease.observers:
        return {"ok": False, "error": "not_holder", "status": lease_status(thread_id)}
    if holder_id and holder_id in lease.observers and lease.holder_id != holder_id:
        lease.observers.discard(holder_id)
        return {"ok": True, "released": True, "status": lease_status(thread_id)}
    _leases.pop(thread_id, None)
    return {"ok": True, "released": True, "status": lease_status(thread_id)}


def reset_leases_for_tests() -> None:
    _leases.clear()


__all__ = [
    "SessionLease",
    "acquire_lease",
    "lease_status",
    "release_lease",
    "reset_leases_for_tests",
]
