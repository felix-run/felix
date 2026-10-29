"""Each person's connections to other services: credentials Felix holds so it can act as them.

A tool that reads someone's calendar or drafts their mail needs that person's own grant, not an
operator's key. This is where it lives: one row per person, provider and account, written by a
consent flow (Google's), or a credential the person hands over (an iCloud app password), and read
by tools at run time for the person the run acts for. GitHub's connection predates it and keeps
its own table (`felix.auth.github_connections`).

What keeps it from being a liability is the GitHub store's discipline, generalised:

- **Keyed by the person, issuer and all.** A row belongs to a person key, `issuer|subject`
  (`felix.auth.persons`), worked out by the auth middleware from the verified principal and
  carried into a durable run or a job's firing (`AuthContext.person`). A subject is only unique
  within its issuer, so a token another issuer signs with the same `sub` as a Felix sign-in is a
  different person here. A credential many callers share -- a sub-less API key, a GitHub Actions
  workflow, a trigger, a machine actor acting for no one -- names no person, and holds nothing.
- **Sealed at rest.** Every credential is AES-GCM sealed with FELIX_CONNECTION_TOKEN_KEY (falling
  back to FELIX_GITHUB_TOKEN_KEY), with the tenant, the person, the provider, the account and the
  column bound in as associated data, so a sealed value copied to another row or column does not
  open.
- **Never handed out.** `access_token()` and `secret()` are for code inside the harness process.
  No route returns a credential, and a provider's failure is reported in words written here, so
  no error carries a token anywhere a model or a log could see it.
- **One writer at a time.** Refreshing, re-consenting and removing a row all take the row's lock
  (an advisory lock on Postgres), so replicas refreshing at once spend a rotating refresh token
  once, a refresh in flight cannot land on top of a new consent, and a removal never races a
  refresh into an orphaned grant.
- **Revocable.** The person and an operator can remove a row, and the provider's grant is
  withdrawn as it goes (best effort: the row goes regardless). A refresh the provider refuses
  marks the row `revoked` rather than retrying a dead token.

A provider is registered (`register_connection_provider`) with how to refresh and revoke its
tokens, so an optional package adds one without touching this file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from felix.auth.github_connections import SealError, decode_key, seal, unseal
from felix.db.models import ConnectionRow
from felix.db.session import _use_memory, tenant_session
from felix.logging_setup import loggable

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from felix.config import Settings

logger = logging.getLogger("felix.auth.connections")

now_ms = lambda: int(time.time() * 1000)

ACTIVE = "active"
REVOKED = "revoked"
OAUTH = "oauth"
SECRET = "secret"
# An access token this close to expiry is refreshed rather than handed to a call that may
# outlast it.
ACCESS_REFRESH_MARGIN_MS = 5 * 60 * 1000
# A provider's refresh, end to end, and how long a caller waits for the row's lock: a hung
# provider must not hold a pooled connection, and every caller queued behind it, indefinitely.
REFRESH_TIMEOUT_S = 30.0
LOCK_TIMEOUT = "40s"
_PROVIDER_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
MAX_ACCOUNT_LENGTH = 320
_COLUMNS = tuple(c.key for c in ConnectionRow.__table__.columns)


class NotConnected(LookupError):
    """This person has no stored connection to that provider (or that account) in this tenant."""


class ConnectionRevoked(PermissionError):
    """The stored connection no longer works: the provider refused it, or it expired. The person
    must connect again."""


class AmbiguousConnection(LookupError):
    """The person has more than one account at that provider, and the call named none."""

    def __init__(self, provider: str, accounts: list[str]) -> None:
        super().__init__(f"{provider}: {len(accounts)} accounts connected; name one")
        self.provider = provider
        self.accounts = accounts


class ConnectionRefused(Exception):
    """Raised by a provider's `refresh` when the provider refused the grant for good (revoked by
    the person, expired). Anything else a refresh raises is an outage, and revokes nothing."""


class ProviderUnavailable(LookupError):
    """No provider of that name is registered here: its package is not installed."""


class ProviderFailed(Exception):
    """The provider could not refresh the grant just now (unreachable, slow, broken). Its message
    is written here and names only the provider: whatever the provider raised may hold a token or
    a URL with one, and stays in the log by type alone."""


class StoreUnavailable(RuntimeError):
    """The connection store could not be read or written just now (a lock timeout, a lost
    connection). Written here: the database's own error quotes the statement and its parameters,
    which name the person and the account."""


@dataclass(frozen=True, slots=True)
class OAuthGrant:
    """What a consent or a refresh gives back. `refresh_token` empty on a refresh means the
    provider kept the old one (Google does; GitHub rotates). `expires_in` 0 means the provider
    did not say, and the access token is used until the provider refuses it."""

    access_token: str
    expires_in: int = 0
    refresh_token: str = ""
    refresh_expires_in: int = 0
    scopes: str = ""


class ConnectionProvider(Protocol):
    name: str

    async def refresh(self, settings: Settings, refresh_token: str) -> OAuthGrant: ...

    async def revoke(self, settings: Settings, token: str) -> None: ...


_PROVIDERS: dict[str, ConnectionProvider] = {}


def register_connection_provider(provider: ConnectionProvider) -> None:
    if not _PROVIDER_NAME.match(provider.name):
        raise ValueError(f"a provider name is lowercase letters, digits, - and _: {provider.name!r}")
    _PROVIDERS[provider.name] = provider


def unregister_connection_provider(name: str) -> None:
    _PROVIDERS.pop(name, None)


#: Providers that ship with the harness, imported on first use so nothing has to remember to:
#: a worker resuming a run refreshes through the same provider the API stored the grant with.
_BUILTIN_PROVIDERS = {"google": "felix.auth.google_connect"}


def _provider(name: str) -> ConnectionProvider | None:
    if name not in _PROVIDERS and name in _BUILTIN_PROVIDERS:
        import importlib

        # The module's `provider`, registered here rather than by the import's side effect, which
        # runs once: a lookup after an unregister would otherwise find nothing.
        register_connection_provider(importlib.import_module(_BUILTIN_PROVIDERS[name]).provider)
    return _PROVIDERS.get(name)


def connection_providers() -> list[str]:
    return sorted(_PROVIDERS)


# --- sealing ------------------------------------------------------------------------------


def connection_key(settings: Settings) -> bytes:
    """The sealing key: FELIX_CONNECTION_TOKEN_KEY, else FELIX_GITHUB_TOKEN_KEY, as 32 bytes.
    `ValueError` naming the setting when neither is usable."""
    own = settings.connection_token_key.strip()
    name, raw = (
        ("FELIX_CONNECTION_TOKEN_KEY", own)
        if own
        else ("FELIX_GITHUB_TOKEN_KEY", settings.github_token_key.strip())
    )
    if not raw:
        raise ValueError("FELIX_CONNECTION_TOKEN_KEY is not set (nor FELIX_GITHUB_TOKEN_KEY to fall back on)")
    try:
        return decode_key(raw)
    except ValueError as exc:
        raise ValueError(f"{name} {exc}") from None


def store_enabled(settings: Settings) -> bool:
    try:
        connection_key(settings)
    except ValueError:
        return False
    return True


Key = tuple[str, str, str, str]


def _purpose(key: Key, column: str) -> str:
    # JSON, not a joined string: a person key or an account may contain any separator.
    return "connection:" + json.dumps([*key, column], separators=(",", ":"))


def _seal(settings: Settings, key: Key, column: str, value: str) -> str:
    return seal(settings, _purpose(key, column), value.encode("utf-8"), key=connection_key(settings))


def _open(settings: Settings, key: Key, column: str, value: str) -> str:
    return unseal(settings, _purpose(key, column), value, key=connection_key(settings)).decode("utf-8")


# --- the store ----------------------------------------------------------------------------

_memory: dict[Key, dict[str, Any]] = {}
_memory_locks: dict[Key, asyncio.Lock] = {}


def reset_connections_for_tests() -> None:
    _memory.clear()
    _memory_locks.clear()


def is_person(person: str) -> bool:
    """Whether `person` is a person key a connection may belong to (`felix.auth.persons`)."""
    from felix.auth.persons import MAX_PERSON_KEY_CHARS, names_no_person

    # No `|` leaves the subject empty, which names no one.
    issuer, _, subject = person.partition("|")
    return bool(
        issuer
        and len(person) <= MAX_PERSON_KEY_CHARS
        and person.isprintable()
        and not names_no_person(subject)
    )


def _check(person: str, provider: str, account: str) -> None:
    if not is_person(person):
        raise ValueError("a connection belongs to one person; this caller names none")
    if not _PROVIDER_NAME.match(provider):
        raise ValueError("a provider name is lowercase letters, digits, - and _")
    if not account or len(account) > MAX_ACCOUNT_LENGTH or not account.isprintable():
        raise ValueError(f"an account is 1 to {MAX_ACCOUNT_LENGTH} printable characters")


@asynccontextmanager
async def _row_lock(settings: Settings, key: Key) -> AsyncIterator[AsyncSession | None]:
    """The row's lock, held for the block: the transaction's advisory lock on Postgres (yielding
    its session), an asyncio lock on the memory arm (yielding None)."""
    if _use_memory(settings):
        async with _memory_locks.setdefault(key, asyncio.Lock()):
            yield None
        return
    async with tenant_session(settings, key[0]) as db:
        await db.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": _purpose(key, "lock")}
        )
        yield db


def _row_dict(row: ConnectionRow | dict[str, Any]) -> dict[str, Any]:
    return dict(row) if isinstance(row, dict) else {c: getattr(row, c) for c in _COLUMNS}


def public_view(row: ConnectionRow | dict[str, Any]) -> dict[str, Any]:
    """Who, what and whether it works. Never a credential."""
    data = _row_dict(row)
    return {
        "person": data["person"],
        "provider": data["provider"],
        "account": data["account"],
        "label": data.get("label") or "",
        "kind": data["kind"],
        "status": data["status"],
        "scopes": [s for s in (data.get("scopes") or "").split() if s],
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
        "expires_at": data.get("secret_expires_at") or 0,
    }


_PRIMARY = ("tenant_id", "person", "provider", "account")


async def _upsert(settings: Settings, values: dict[str, Any]) -> dict[str, Any]:
    key: Key = cast(Key, tuple(values[k] for k in _PRIMARY))
    async with _row_lock(settings, key) as db:
        if db is None:
            if key in _memory:
                values["created_at"] = _memory[key]["created_at"]
            _memory[key] = values
            return public_view(values)
        stmt = pg_insert(cast(Any, ConnectionRow.__table__)).values(values)
        await db.execute(
            stmt.on_conflict_do_update(
                index_elements=list(_PRIMARY),
                # `created_at` stays: it is when this person first connected.
                set_={k: stmt.excluded[k] for k in values if k not in {*_PRIMARY, "created_at"}},
            )
        )
        await db.commit()
        row = await db.get(ConnectionRow, key)
        return public_view(row if row is not None else values)


def _expiry(ts: int, seconds: int) -> int:
    return ts + seconds * 1000 if seconds else 0


def _new_row(
    tenant_id: str, person: str, provider: str, account: str, *, kind: str, label: str, ts: int
) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "person": person,
        "provider": provider,
        "account": account,
        "kind": kind,
        "status": ACTIVE,
        "label": label,
        "scopes": "",
        "secret_expires_at": 0,
        "access_sealed": None,
        "access_expires_at": 0,
        "created_at": ts,
        "updated_at": ts,
    }


async def save_oauth(
    settings: Settings,
    tenant_id: str,
    *,
    person: str,
    provider: str,
    account: str,
    grant: OAuthGrant,
    label: str = "",
) -> dict[str, Any]:
    """Store (or replace) this person's OAuth connection from a fresh consent. `ValueError` for a
    grant with no refresh token: an access token alone would outlive any control Felix has."""
    _check(person, provider, account)
    if not grant.refresh_token:
        raise ValueError(f"{provider} gave no refresh token; ask for offline access")
    key: Key = (tenant_id, person, provider, account)
    ts = now_ms()
    values = _new_row(tenant_id, person, provider, account, kind=OAUTH, label=label, ts=ts)
    values |= {
        "scopes": grant.scopes,
        "secret_sealed": _seal(settings, key, "secret", grant.refresh_token),
        "secret_expires_at": _expiry(ts, grant.refresh_expires_in),
        "access_sealed": _seal(settings, key, "access", grant.access_token) if grant.access_token else None,
        "access_expires_at": _expiry(ts, grant.expires_in),
    }
    stored = await _upsert(settings, values)
    _record(settings, tenant_id, stored, "stored")
    return stored


async def save_secret(
    settings: Settings,
    tenant_id: str,
    *,
    person: str,
    provider: str,
    account: str,
    secret: str,
    label: str = "",
) -> dict[str, Any]:
    """Store (or replace) a credential the person handed over, such as an app password."""
    _check(person, provider, account)
    if not secret:
        raise ValueError("a secret connection needs its secret")
    key: Key = (tenant_id, person, provider, account)
    values = _new_row(tenant_id, person, provider, account, kind=SECRET, label=label, ts=now_ms())
    values["secret_sealed"] = _seal(settings, key, "secret", secret)
    stored = await _upsert(settings, values)
    _record(settings, tenant_id, stored, "stored")
    return stored


def _sort_key(row: dict[str, Any]) -> tuple[str, str]:
    return (row["provider"], row["account"])


async def _rows_for(settings: Settings, tenant_id: str, person: str) -> list[dict[str, Any]]:
    if _use_memory(settings):
        rows = [dict(r) for (t, p, _, _), r in _memory.items() if t == tenant_id and p == person]
    else:
        async with tenant_session(settings, tenant_id) as db:
            result = await db.scalars(
                select(ConnectionRow).where(
                    ConnectionRow.tenant_id == tenant_id, ConnectionRow.person == person
                )
            )
            rows = [_row_dict(r) for r in result]
    # In Python on both arms, so the order is by code point whatever the database's collation.
    rows.sort(key=_sort_key)
    return rows


async def list_for_person(settings: Settings, tenant_id: str, person: str) -> list[dict[str, Any]]:
    """One person's connections, by provider and account: their own view."""
    return [public_view(r) for r in await _rows_for(settings, tenant_id, person)]


async def list_connections(settings: Settings, tenant_id: str) -> list[dict[str, Any]]:
    """Every connection in the tenant, newest first, ties by person, provider and account in code
    point order on both arms (`COLLATE "C"`): the operator's view."""
    if _use_memory(settings):
        rows = [r for (t, *_), r in _memory.items() if t == tenant_id]
        rows.sort(key=lambda r: (-r["updated_at"], r["person"], r["provider"], r["account"]))
        return [public_view(r) for r in rows]
    async with tenant_session(settings, tenant_id) as db:
        result = await db.scalars(
            select(ConnectionRow)
            .where(ConnectionRow.tenant_id == tenant_id)
            .order_by(
                ConnectionRow.updated_at.desc(),
                ConnectionRow.person.collate("C"),
                ConnectionRow.provider.collate("C"),
                ConnectionRow.account.collate("C"),
            )
        )
        return [public_view(r) for r in result]


async def _resolve_account(
    settings: Settings, tenant_id: str, person: str, provider: str, account: str | None
) -> str:
    """The account a call means: the one named, else the person's only active one there. A
    person whose every account there is revoked is told to reconnect, not that they never did."""
    if account:
        return account
    mine = [r for r in await _rows_for(settings, tenant_id, person) if r["provider"] == provider]
    active = [r["account"] for r in mine if r["status"] == ACTIVE]
    if not active:
        if mine:
            raise ConnectionRevoked(provider)
        raise NotConnected(provider)
    if len(active) > 1:
        raise AmbiguousConnection(provider, active)
    return active[0]


async def _get_row(settings: Settings, key: Key) -> dict[str, Any] | None:
    if _use_memory(settings):
        row = _memory.get(key)
        return dict(row) if row is not None else None
    async with tenant_session(settings, key[0]) as db:
        row = await db.get(ConnectionRow, key)
        return _row_dict(row) if row is not None else None


async def get_connection(
    settings: Settings, tenant_id: str, person: str, provider: str, account: str
) -> dict[str, Any] | None:
    row = await _get_row(settings, (tenant_id, person, provider, account))
    return public_view(row) if row is not None else None


@asynccontextmanager
async def _store_errors_written() -> AsyncIterator[None]:
    from sqlalchemy.exc import SQLAlchemyError

    try:
        yield
    except SQLAlchemyError as exc:
        logger.warning("connection_store_failed error=%s", type(exc).__name__)
        raise StoreUnavailable("the connection store could not be reached just now") from None


async def secret(
    settings: Settings, tenant_id: str, person: str, provider: str, account: str | None = None
) -> str:
    """The stored secret of a `secret` connection, for code inside the harness only."""
    async with _store_errors_written():
        return await _secret(settings, tenant_id, person, provider, account)


async def _secret(settings: Settings, tenant_id: str, person: str, provider: str, account: str | None) -> str:
    account = await _resolve_account(settings, tenant_id, person, provider, account)
    key: Key = (tenant_id, person, provider, account)
    row = await _get_row(settings, key)
    if row is None:
        raise NotConnected(provider)
    if row["kind"] != SECRET:
        raise ValueError(f"{provider} is an {row['kind']} connection; use access_token")
    if row["status"] != ACTIVE:
        raise ConnectionRevoked(provider)
    return _open(settings, key, "secret", row["secret_sealed"])


async def access_token(
    settings: Settings, tenant_id: str, person: str, provider: str, account: str | None = None
) -> str:
    """An access token to act as this person at `provider`, for code inside the harness only.

    The stored one while it has more than `ACCESS_REFRESH_MARGIN_MS` left; otherwise refreshed
    through the registered provider under the row's lock, any rotated refresh token stored before
    the access token is returned. Raises `NotConnected`, `AmbiguousConnection`,
    `ConnectionRevoked`, `ProviderUnavailable`, `ProviderFailed` for an outage (which revokes
    nothing), or `StoreUnavailable`.
    """
    async with _store_errors_written():
        return await _access_token(settings, tenant_id, person, provider, account)


async def _access_token(
    settings: Settings, tenant_id: str, person: str, provider: str, account: str | None
) -> str:
    account = await _resolve_account(settings, tenant_id, person, provider, account)
    key: Key = (tenant_id, person, provider, account)
    async with _row_lock(settings, key) as db:
        found = _memory.get(key) if db is None else await db.get(ConnectionRow, key, populate_existing=True)
        if found is None:
            raise NotConnected(provider)
        current = _row_dict(found)
        try:
            fresh = await _current_or_refreshed(settings, key, current)
        except ConnectionRevoked:
            fresh = {"status": REVOKED, "updated_at": now_ms()} if current["status"] == ACTIVE else {}
            await _write(db, found, fresh)
            raise
        await _write(db, found, fresh)
        current.update(fresh)
        return _open(settings, key, "access", current["access_sealed"])


async def _write(db: AsyncSession | None, found: Any, fresh: dict[str, Any]) -> None:
    if db is None:
        found.update(fresh)
        return
    for k, v in fresh.items():
        setattr(found, k, v)
    await db.commit()


async def _current_or_refreshed(settings: Settings, key: Key, row: dict[str, Any]) -> dict[str, Any]:
    """The columns to write: nothing while the stored access token is good, else a refresh."""
    tenant_id, _, provider_name, _ = key
    if row["kind"] != OAUTH:
        raise ValueError(f"{provider_name} is a {row['kind']} connection; use secret")
    if row["status"] != ACTIVE:
        raise ConnectionRevoked(provider_name)
    ts = now_ms()
    expires = row.get("access_expires_at") or 0
    if row.get("access_sealed") and (not expires or expires - ts > ACCESS_REFRESH_MARGIN_MS):
        return {}
    if row.get("secret_expires_at") and row["secret_expires_at"] <= ts:
        _record(settings, tenant_id, row, "refresh_expired")
        raise ConnectionRevoked(provider_name)
    provider = _provider(provider_name)
    if provider is None:
        raise ProviderUnavailable(provider_name)
    refresh = _open(settings, key, "secret", row["secret_sealed"])
    try:
        async with asyncio.timeout(REFRESH_TIMEOUT_S):
            grant = await provider.refresh(settings, refresh)
    except ConnectionRefused:
        _record(settings, tenant_id, row, "refresh_refused")
        raise ConnectionRevoked(provider_name) from None
    except Exception as exc:
        logger.warning(
            "connection_refresh_failed provider=%s error=%s",
            loggable(provider_name, limit=64),
            type(exc).__name__,
        )
        raise ProviderFailed(f"{provider_name} could not refresh this connection just now") from None
    fresh: dict[str, Any] = {
        "access_sealed": _seal(settings, key, "access", grant.access_token),
        "access_expires_at": _expiry(ts, grant.expires_in),
        "updated_at": ts,
    }
    if grant.refresh_token:
        fresh["secret_sealed"] = _seal(settings, key, "secret", grant.refresh_token)
        fresh["secret_expires_at"] = _expiry(ts, grant.refresh_expires_in)
    if grant.scopes:
        fresh["scopes"] = grant.scopes
    return fresh


async def _force_expiry_for_tests(settings: Settings, key: Key) -> None:
    """Make the stored access token due for refresh, on either backend."""
    if _use_memory(settings):
        _memory[key]["access_expires_at"] = 1
        return
    async with tenant_session(settings, key[0]) as db:
        row = await db.get(ConnectionRow, key)
        assert row is not None
        row.access_expires_at = 1
        await db.commit()


async def remove_connection(
    settings: Settings,
    tenant_id: str,
    person: str,
    provider: str,
    account: str,
    *,
    actor: str = "",
) -> bool:
    """Forget this connection, and withdraw the grant at the provider. The row goes first and
    regardless, under its lock so no refresh is mid-way: Felix stops holding the credential even
    when the provider cannot be reached to hear about it. True when there was a row to forget."""
    key: Key = (tenant_id, person, provider, account)
    async with _row_lock(settings, key) as db:
        if db is None:
            row = _memory.pop(key, None)
        else:
            found = await db.get(ConnectionRow, key)
            row = _row_dict(found) if found is not None else None
            await db.execute(
                delete(ConnectionRow).where(
                    *(getattr(ConnectionRow, c) == v for c, v in zip(_PRIMARY, key, strict=True))
                )
            )
            await db.commit()
    if row is None:
        return False
    _record(settings, tenant_id, row, "removed", actor=actor)
    registered = _provider(provider)
    if row["kind"] == OAUTH and row["status"] == ACTIVE and registered is not None:
        await _withdraw(settings, key, row, registered)
    return True


async def _withdraw(settings: Settings, key: Key, row: dict[str, Any], provider: ConnectionProvider) -> None:
    try:
        token = _open(settings, key, "secret", row["secret_sealed"])
    except SealError:
        # Sealed under a key this process no longer has: the grant can only be revoked at the
        # provider by the person now.
        logger.warning("connection_revoke_unsealable provider=%s", loggable(key[2], limit=64))
        return
    try:
        async with asyncio.timeout(REFRESH_TIMEOUT_S):
            await provider.revoke(settings, token)
    except Exception as exc:
        # The row is gone either way; the person can still revoke at the provider.
        logger.warning(
            "connection_revoke_failed provider=%s error=%s", loggable(key[2], limit=64), type(exc).__name__
        )


def _record(settings: Settings, tenant_id: str, row: dict[str, Any], status: str, *, actor: str = "") -> None:
    from felix.audit import store as audit_store

    audit_store.record_event(
        settings,
        tenant_id,
        "connection",
        principal_subj=actor or row["person"],
        status=status,
        payload={
            "person": row["person"],
            "provider": row["provider"],
            "account": row["account"],
            "kind": row["kind"],
        },
    )


__all__ = [
    "ACTIVE",
    "OAUTH",
    "REVOKED",
    "SECRET",
    "AmbiguousConnection",
    "ConnectionProvider",
    "ConnectionRefused",
    "ConnectionRevoked",
    "NotConnected",
    "OAuthGrant",
    "ProviderFailed",
    "ProviderUnavailable",
    "StoreUnavailable",
    "access_token",
    "connection_key",
    "connection_providers",
    "get_connection",
    "is_person",
    "list_connections",
    "list_for_person",
    "public_view",
    "register_connection_provider",
    "remove_connection",
    "reset_connections_for_tests",
    "save_oauth",
    "save_secret",
    "secret",
    "store_enabled",
    "unregister_connection_provider",
]
