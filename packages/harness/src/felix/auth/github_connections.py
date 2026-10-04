"""Each person's GitHub connection: their refresh token, kept so Felix can act as them later.

GitHub login used to read two things with the person's GitHub token and drop it. Repo access
(the per-person repos of #470) needs to act as that person after the sign-in is over — clone
their repository, publish their commits — so a sign-in through a GitHub App with expiring user
tokens now keeps the **refresh token**, and mints short-lived access tokens from it on use.

What keeps that from being a liability:

- **Sealed at rest.** Refresh and access tokens are AES-GCM sealed with `FELIX_GITHUB_TOKEN_KEY`,
  with the tenant, the GitHub user id and the field bound in as associated data, so a sealed value
  copied to another row or column does not open.
- **Never handed out.** `access_token()` is for code inside the harness process. No route
  returns a GitHub token, and nothing here passes one to a process that runs repository code.
- **Refreshed under a lock.** GitHub rotates the refresh token on every use. Two replicas
  refreshing at once would each spend the same one, the second would be refused, and the person
  would be signed out of GitHub for nothing; one lock per row makes the second reuse the first's.
- **Revocable.** The person (sign-out, or `DELETE /github/connection`) and an operator can drop
  a row, and Felix withdraws the App's authorization at GitHub as it does. A refresh GitHub
  refuses marks the row `revoked` rather than retrying a dead token.

Keyed by `(tenant_id, github_user_id)`: a sign-in lands in one tenant, and the row lives there.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import time
from functools import lru_cache
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from felix.db.models import GitHubConnectionRow
from felix.db.session import _use_memory, tenant_session

if TYPE_CHECKING:
    import httpx

    from felix.auth.github import GitHubGrant
    from felix.config import Settings

logger = logging.getLogger("felix.auth.github_connections")

now_ms = lambda: int(time.time() * 1000)

SEAL_VERSION = "v1"
# An access token this close to expiry is refreshed rather than handed to a call that may
# outlast it: a clone or a publish can take minutes.
ACCESS_REFRESH_MARGIN_MS = 5 * 60 * 1000

ACTIVE = "active"
REVOKED = "revoked"


class SealError(ValueError):
    """A sealed value that does not open under this key and purpose."""


class GitHubNotConnected(LookupError):
    """This person has no stored GitHub connection in this tenant."""


class GitHubConnectionRevoked(PermissionError):
    """The stored connection no longer works: GitHub refused its refresh token, or it expired."""


# --- sealing ------------------------------------------------------------------------------


def token_key(settings: Settings) -> bytes:
    """FELIX_GITHUB_TOKEN_KEY as 32 raw bytes. Raises ValueError, naming what is wrong."""
    return _decode_key(settings.github_token_key.strip())


@lru_cache(maxsize=4)
def _decode_key(raw: str) -> bytes:
    if not raw:
        raise ValueError("is empty")
    padded = raw + "=" * (-len(raw) % 4)
    try:
        key = base64.b64decode(padded.replace("-", "+").replace("_", "/"), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("is not base64 (make one with `openssl rand -base64 32`)") from exc
    if len(key) != 32:
        raise ValueError(f"decodes to {len(key)} bytes; it must be 32 (`openssl rand -base64 32`)")
    return key


def seal(settings: Settings, purpose: str, data: bytes) -> str:
    """AES-GCM seal `data` for one purpose. `purpose` is bound as associated data, so a value
    sealed as a sign-in cookie cannot be opened as a stored token, or as another row's."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    sealed = AESGCM(token_key(settings)).encrypt(nonce, data, purpose.encode("utf-8"))
    return f"{SEAL_VERSION}.{base64.urlsafe_b64encode(nonce + sealed).decode('ascii').rstrip('=')}"


def unseal(settings: Settings, purpose: str, value: str) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    version, _, body = value.partition(".")
    if version != SEAL_VERSION or not body:
        raise SealError("not a sealed value")
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except (binascii.Error, ValueError) as exc:
        raise SealError("not a sealed value") from exc
    if len(raw) < 12 + 16:
        raise SealError("not a sealed value")
    try:
        return AESGCM(token_key(settings)).decrypt(raw[:12], raw[12:], purpose.encode("utf-8"))
    except InvalidTag as exc:
        raise SealError("does not open under this key and purpose") from exc


def seal_json(settings: Settings, purpose: str, data: dict[str, Any]) -> str:
    return seal(settings, purpose, json.dumps(data, separators=(",", ":")).encode("utf-8"))


def unseal_json(settings: Settings, purpose: str, value: str) -> dict[str, Any]:
    try:
        data = json.loads(unseal(settings, purpose, value))
    except ValueError as exc:
        raise SealError("does not hold an object") from exc
    if not isinstance(data, dict):
        raise SealError("does not hold an object")
    return data


def _field_purpose(tenant_id: str, github_user_id: int, field: str) -> str:
    return f"github-connection:{tenant_id}:{github_user_id}:{field}"


# --- the store ----------------------------------------------------------------------------

_memory: dict[tuple[str, int], dict[str, Any]] = {}
_memory_locks: dict[tuple[str, int], asyncio.Lock] = {}


def reset_github_connections_for_tests() -> None:
    _memory.clear()
    _memory_locks.clear()


def _connection_dict(row: GitHubConnectionRow | dict[str, Any]) -> dict[str, Any]:
    """The public view of a connection: who, since when, whether it works. Never a token."""
    data = (
        dict(row)
        if isinstance(row, dict)
        else {c.key: getattr(row, c.key) for c in GitHubConnectionRow.__table__.columns}
    )
    return {
        "tenant_id": data["tenant_id"],
        "github_user_id": data["github_user_id"],
        "github_login": data.get("github_login") or "",
        "status": data["status"],
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
        "refresh_expires_at": data.get("refresh_expires_at") or 0,
    }


def _sealed_values(
    settings: Settings, tenant_id: str, github_user_id: int, grant: GitHubGrant, ts: int
) -> dict[str, Any]:
    return {
        "refresh_token_sealed": seal(
            settings, _field_purpose(tenant_id, github_user_id, "refresh"), grant.refresh_token.encode()
        ),
        "refresh_expires_at": ts + grant.refresh_token_expires_in * 1000
        if grant.refresh_token_expires_in
        else 0,
        "access_token_sealed": seal(
            settings, _field_purpose(tenant_id, github_user_id, "access"), grant.access_token.encode()
        ),
        "access_expires_at": ts + grant.expires_in * 1000 if grant.expires_in else 0,
    }


async def save_connection(
    settings: Settings,
    tenant_id: str,
    *,
    github_user_id: int,
    github_login: str,
    grant: GitHubGrant,
    principal_subj: str = "",
) -> dict[str, Any] | None:
    """Store (or replace) this person's connection from a fresh sign-in.

    Returns None, storing nothing, when there is nothing worth keeping: no token key, or a grant
    without a refresh token (an OAuth app, or a GitHub App without expiring user tokens), whose
    access token would outlive any control Felix has over it.
    """
    if not settings.github_token_key.strip() or not grant.refresh_token or github_user_id <= 0:
        return None
    ts = now_ms()
    values = {
        "tenant_id": tenant_id,
        "github_user_id": github_user_id,
        "github_login": github_login,
        "status": ACTIVE,
        "principal_subj": principal_subj,
        "created_at": ts,
        "updated_at": ts,
        **_sealed_values(settings, tenant_id, github_user_id, grant, ts),
    }
    if _use_memory(settings):
        key = (tenant_id, github_user_id)
        if key in _memory:
            values["created_at"] = _memory[key]["created_at"]
        _memory[key] = values
        return _connection_dict(values)
    async with tenant_session(settings, tenant_id) as db:
        stmt = pg_insert(cast(Any, GitHubConnectionRow.__table__)).values(values)
        await db.execute(
            stmt.on_conflict_do_update(
                index_elements=["tenant_id", "github_user_id"],
                # `created_at` stays: it is when this person first connected.
                set_={
                    k: stmt.excluded[k]
                    for k in values
                    if k not in {"tenant_id", "github_user_id", "created_at"}
                },
            )
        )
        await db.commit()
        row = await db.get(GitHubConnectionRow, (tenant_id, github_user_id))
        return _connection_dict(row if row is not None else values)


async def get_connection(settings: Settings, tenant_id: str, github_user_id: int) -> dict[str, Any] | None:
    if _use_memory(settings):
        row = _memory.get((tenant_id, github_user_id))
        return _connection_dict(row) if row is not None else None
    async with tenant_session(settings, tenant_id) as db:
        row = await db.get(GitHubConnectionRow, (tenant_id, github_user_id))
        return _connection_dict(row) if row is not None else None


async def list_connections(settings: Settings, tenant_id: str) -> list[dict[str, Any]]:
    """Every connection in the tenant, newest first: the operator's view."""
    if _use_memory(settings):
        rows = [r for (t, _), r in _memory.items() if t == tenant_id]
        rows.sort(key=lambda r: (-r["updated_at"], r["github_user_id"]))
        return [_connection_dict(r) for r in rows]
    async with tenant_session(settings, tenant_id) as db:
        result = await db.scalars(
            select(GitHubConnectionRow)
            .where(GitHubConnectionRow.tenant_id == tenant_id)
            .order_by(GitHubConnectionRow.updated_at.desc(), GitHubConnectionRow.github_user_id)
        )
        return [_connection_dict(r) for r in result]


async def access_token(
    settings: Settings,
    tenant_id: str,
    github_user_id: int,
    *,
    client: httpx.AsyncClient | None = None,
) -> str:
    """A GitHub access token to act as this person, for code inside the harness only.

    The stored one while it has more than `ACCESS_REFRESH_MARGIN_MS` left; otherwise refreshed
    under the row's lock, the rotated refresh token stored before the access token is returned.
    Raises `GitHubNotConnected`, `GitHubConnectionRevoked`, or `GitHubLoginError` when GitHub
    could not be reached (which revokes nothing).
    """
    if _use_memory(settings):
        lock = _memory_locks.setdefault((tenant_id, github_user_id), asyncio.Lock())
        async with lock:
            row = _memory.get((tenant_id, github_user_id))
            if row is None:
                raise GitHubNotConnected(f"github:{github_user_id}")
            fresh = await _current_or_refreshed(settings, tenant_id, github_user_id, row, client)
            row.update(fresh)
            return _open_access(settings, tenant_id, github_user_id, row)

    async with tenant_session(settings, tenant_id) as db:
        # One lock per connection for the length of this transaction: a second caller waits,
        # then reads the token the first one stored instead of spending the same refresh token.
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"github-connection:{tenant_id}:{github_user_id}"},
        )
        row = await db.get(GitHubConnectionRow, (tenant_id, github_user_id), populate_existing=True)
        if row is None:
            raise GitHubNotConnected(f"github:{github_user_id}")
        current = {c.key: getattr(row, c.key) for c in GitHubConnectionRow.__table__.columns}
        try:
            fresh = await _current_or_refreshed(settings, tenant_id, github_user_id, current, client)
        except GitHubConnectionRevoked:
            row.status = REVOKED
            row.updated_at = now_ms()
            await db.commit()
            raise
        for k, v in fresh.items():
            setattr(row, k, v)
        await db.commit()
        current.update(fresh)
        return _open_access(settings, tenant_id, github_user_id, current)


async def _current_or_refreshed(
    settings: Settings,
    tenant_id: str,
    github_user_id: int,
    row: dict[str, Any],
    client: httpx.AsyncClient | None,
) -> dict[str, Any]:
    """The columns to write: nothing while the stored access token is good, else a refresh."""
    from felix.auth.github import GitHubLoginError, LoginErrorCode, refresh_github_grant

    if row["status"] != ACTIVE:
        raise GitHubConnectionRevoked(f"github:{github_user_id} is {row['status']}")
    ts = now_ms()
    access_left = (row.get("access_expires_at") or 0) - ts
    if row.get("access_token_sealed") and (
        not row.get("access_expires_at") or access_left > ACCESS_REFRESH_MARGIN_MS
    ):
        return {}
    if row.get("refresh_expires_at") and row["refresh_expires_at"] <= ts:
        _record(settings, tenant_id, row, "refresh_expired")
        if _use_memory(settings):
            row["status"] = REVOKED
        raise GitHubConnectionRevoked(f"github:{github_user_id}: the refresh token expired")
    refresh = unseal(
        settings, _field_purpose(tenant_id, github_user_id, "refresh"), row["refresh_token_sealed"]
    )
    try:
        grant = await refresh_github_grant(settings, refresh.decode(), client=client)
    except GitHubLoginError as exc:
        if exc.code in {LoginErrorCode.EXPIRED_TOKEN, LoginErrorCode.ACCESS_DENIED}:
            _record(settings, tenant_id, row, "refresh_refused")
            if _use_memory(settings):
                row["status"] = REVOKED
            raise GitHubConnectionRevoked(f"github:{github_user_id}: GitHub refused the refresh") from exc
        raise
    if not grant.refresh_token:
        # GitHub always rotates it for an App with expiring tokens; one missing means the
        # stored token is about to stop working with nothing to replace it.
        _record(settings, tenant_id, row, "refresh_without_rotation")
        if _use_memory(settings):
            row["status"] = REVOKED
        raise GitHubConnectionRevoked(f"github:{github_user_id}: GitHub returned no new refresh token")
    return {**_sealed_values(settings, tenant_id, github_user_id, grant, ts), "updated_at": ts}


async def _force_expiry_for_tests(settings: Settings, tenant_id: str, github_user_id: int) -> None:
    """Make the stored access token due for refresh, on either backend."""
    if _use_memory(settings):
        _memory[(tenant_id, github_user_id)]["access_expires_at"] = 1
        return
    async with tenant_session(settings, tenant_id) as db:
        row = await db.get(GitHubConnectionRow, (tenant_id, github_user_id))
        assert row is not None
        row.access_expires_at = 1
        await db.commit()


def _open_access(settings: Settings, tenant_id: str, github_user_id: int, row: dict[str, Any]) -> str:
    return unseal(
        settings, _field_purpose(tenant_id, github_user_id, "access"), row["access_token_sealed"]
    ).decode()


async def remove_connection(
    settings: Settings,
    tenant_id: str,
    github_user_id: int,
    *,
    principal_subj: str = "",
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Forget this person's connection, and withdraw the App's authorization at GitHub.

    The row goes first and regardless: Felix stops holding the token even when GitHub cannot be
    reached to hear about it. True when there was a row to forget.
    """
    from felix.auth.github import revoke_github_grant

    if _use_memory(settings):
        row = _memory.pop((tenant_id, github_user_id), None)
    else:
        async with tenant_session(settings, tenant_id) as db:
            found = await db.get(GitHubConnectionRow, (tenant_id, github_user_id))
            row = (
                {c.key: getattr(found, c.key) for c in GitHubConnectionRow.__table__.columns}
                if found
                else None
            )
            await db.execute(
                delete(GitHubConnectionRow).where(
                    GitHubConnectionRow.tenant_id == tenant_id,
                    GitHubConnectionRow.github_user_id == github_user_id,
                )
            )
            await db.commit()
    if row is None:
        return False
    _record(settings, tenant_id, row, "removed", principal_subj=principal_subj)
    if row.get("status") == ACTIVE and row.get("access_token_sealed"):
        try:
            token = _open_access(settings, tenant_id, github_user_id, row)
        except SealError:
            return True
        await revoke_github_grant(settings, token, client=client)
    return True


def _record(
    settings: Settings, tenant_id: str, row: dict[str, Any], status: str, *, principal_subj: str = ""
) -> None:
    from felix.audit import store as audit_store

    audit_store.record_event(
        settings,
        tenant_id,
        "github_connection",
        principal_subj=principal_subj or row.get("principal_subj") or f"github:{row['github_user_id']}",
        status=status,
        payload={"github_user_id": row["github_user_id"], "github_login": row.get("github_login") or ""},
    )


def record_stored(
    settings: Settings, tenant_id: str, connection: dict[str, Any], principal_subj: str
) -> None:
    """Audit a stored connection; the route calls it beside its own `github_login` event."""
    _record(settings, tenant_id, connection, "stored", principal_subj=principal_subj)
