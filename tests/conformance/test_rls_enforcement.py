"""What the tenant policy does when it is actually enforced.

Every other contract in this directory connects as the database owner, which is a superuser in
CI and in the bundled compose image. Migration 0006 applies FORCE, but a superuser bypasses even
that — so the policy is unreachable from the rest of the suite, and everything it protects is
asserted only by reading the SQL. Deleting `rls_bypass()` from a cross-tenant sweep leaves the
whole suite green.

That blind spot has already cost something: the worker's per-tenant sweeps bound no tenant and
therefore read nothing under an enforcing policy, and no test could see it. This file connects as
a `NOSUPERUSER NOBYPASSRLS` role with the listener told `database_rls=True` — the shape of a
managed-Postgres application role — and pins what changes: an unbound write is refused, an
unbound read is silently empty, a bound tenant cannot reach another's rows, and the bypass that
maintenance sweeps declare is what lets them cross.

Two things this file learned the hard way and states so nobody re-learns them. The stores are
not the right layer to test the *unbound* case: `audit/store.py` binds its own tenant per event,
so it can never be reached with nothing bound, and an earlier version of these tests asserted a
refusal that could not happen. And `list_tenants_with_events` declares its own `rls_bypass()`, as
every cross-tenant sweep does — so the regression guard is that it still returns every tenant
under an enforcing policy, not that it returns nothing without an outer bypass.

Postgres only. `memory://` has no policy, so a memory arm here would assert that nothing happens.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.db.session import get_session_factory, rls_bypass, rls_tenant
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

pytestmark = pytest.mark.asyncio

TENANT = "acme"
OTHER = "globex"

_COUNT = text("SELECT count(*) FROM audit_events WHERE tenant_id = :t")
_TENANTS = text("SELECT DISTINCT tenant_id FROM audit_events ORDER BY tenant_id")


async def _raw_insert(settings: Any, tenant: str) -> None:
    """Insert through a plain session, which binds no tenant of its own.

    Deliberately not through `audit/store.py`: that store wraps every write in
    `rls_tenant(event["tenant_id"])`, so it is structurally incapable of reaching the database
    unbound and cannot exercise the case this file exists for.
    """
    import uuid

    from felix.db.models import AuditEvent

    # The ORM row rather than a hand-written column list: it carries the defaults for the five
    # columns this does not care about, and a future NOT NULL column arrives with the model
    # rather than as a not-null violation here. It still reaches the database through a plain
    # session, which is the point.
    factory = get_session_factory(settings=settings)
    async with factory() as db:
        db.add(
            AuditEvent(
                tenant_id=tenant,
                id=uuid.uuid4().hex,
                ts=1_700_000_000_000,
                event_type="tool_call",
            )
        )
        await db.commit()


async def _count(settings: Any, tenant: str) -> int:
    factory = get_session_factory(settings=settings)
    async with factory() as db:
        return int((await db.execute(_COUNT, {"t": tenant})).scalar_one())


async def _seed(settings: Any) -> None:
    for tenant in (TENANT, OTHER):
        with rls_tenant(tenant):
            await _raw_insert(settings, tenant)


# --- the arm is what it claims to be ---------------------------------------------------------


async def test_the_connection_can_neither_be_superuser_nor_bypass(rls_settings: Any) -> None:
    """Guards the guard: if this role could bypass, every assertion below would be vacuous.

    `pg_user` has no `rolbypassrls` column, so an earlier version checked `usesuper` alone — and
    a `NOSUPERUSER BYPASSRLS` role passes that while reading every tenant's rows, which is the
    one misconfiguration the check exists to catch. `pg_roles` carries both flags.
    """
    factory = get_session_factory(settings=rls_settings)
    async with factory() as db:
        row = (
            await db.execute(
                text("SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).first()

    assert row is not None, "no pg_roles row for current_user"
    assert row.rolname == "felix_conformance_rls", row
    assert row.rolsuper is False, f"the RLS arm must not run as a superuser: {row}"
    assert row.rolbypassrls is False, f"the RLS arm must not be able to bypass: {row}"


# --- writes ----------------------------------------------------------------------------------


async def test_a_write_with_the_tenant_bound_succeeds(rls_settings: Any) -> None:
    """The ordinary path: something bound a tenant, so the policy has one to match."""
    with rls_tenant(TENANT):
        await _raw_insert(rls_settings, TENANT)
        assert await _count(rls_settings, TENANT) == 1


async def test_a_write_with_no_tenant_bound_is_refused(rls_settings: Any) -> None:
    """The write half of the state the worker was in, and it is loud.

    With nothing bound, the policy's `WITH CHECK` refuses the insert. The read half of the same
    state is silent, which is what made the worker bug survive — see below.
    """
    # The type narrows it; the message is the specificity. SQLSTATE alone will not do — an RLS
    # `WITH CHECK` violation and a plain `permission denied` are both 42501, and telling those
    # apart is exactly what this test is for.
    with pytest.raises(ProgrammingError) as excinfo:
        await _raw_insert(rls_settings, TENANT)

    assert "row-level security" in str(excinfo.value).lower(), excinfo.value
    with rls_bypass():
        assert await _count(rls_settings, TENANT) == 0


async def test_a_write_cannot_land_under_another_tenant(rls_settings: Any) -> None:
    """`WITH CHECK` is what stops a bound tenant writing rows labelled with another's id."""
    with rls_tenant(TENANT), pytest.raises(ProgrammingError) as excinfo:
        await _raw_insert(rls_settings, OTHER)

    assert "row-level security" in str(excinfo.value).lower(), excinfo.value
    with rls_bypass():
        assert await _count(rls_settings, OTHER) == 0


# --- reads -----------------------------------------------------------------------------------


async def test_a_read_with_no_tenant_bound_is_empty_not_an_error(rls_settings: Any) -> None:
    """The silent half, and the reason the worker bug went unnoticed.

    A refused write raises. A filtered read returns nothing, which is indistinguishable from an
    empty table — so a sweep that binds no tenant scans nothing and reports success.
    """
    await _seed(rls_settings)

    assert await _count(rls_settings, TENANT) == 0, "unbound reads must be filtered, not error"

    with rls_tenant(TENANT):
        assert await _count(rls_settings, TENANT) == 1


async def test_a_read_bound_to_another_tenant_sees_nothing(rls_settings: Any) -> None:
    """Isolation, asserted positively in both directions so an outage cannot pass for it."""
    await _seed(rls_settings)

    with rls_tenant(TENANT):
        assert await _count(rls_settings, TENANT) == 1
        assert await _count(rls_settings, OTHER) == 0
    with rls_tenant(OTHER):
        assert await _count(rls_settings, OTHER) == 1
        assert await _count(rls_settings, TENANT) == 0


# --- the bypass ------------------------------------------------------------------------------


async def test_a_cross_tenant_sweep_still_sees_every_tenant(rls_settings: Any) -> None:
    """The regression guard for every `rls_bypass()` in the tree.

    `list_tenants_with_events` declares its own bypass, as do retention, the fiber claim and the
    other `list_tenants_with_*` helpers. This goes red the moment one of them loses it — which
    is exactly the change no other arm of this suite can detect, because there the policy never
    applies in the first place.

    The contrast below is what gives it meaning: the same query through a plain session, with
    no tenant and no bypass, sees nothing at all.
    """
    from felix.audit.store import list_tenants_with_events

    await _seed(rls_settings)

    assert sorted(await list_tenants_with_events(rls_settings)) == sorted([TENANT, OTHER])

    factory = get_session_factory(settings=rls_settings)
    async with factory() as db:
        unbound = [r[0] for r in (await db.execute(_TENANTS)).all()]
    assert unbound == [], "without the bypass a cross-tenant sweep sees nothing"


async def test_the_bypass_does_not_outlive_its_block(rls_settings: Any) -> None:
    """A leaked bypass turns every later query into a cross-tenant read."""
    await _seed(rls_settings)

    with rls_bypass():
        assert await _count(rls_settings, OTHER) == 1

    with rls_tenant(TENANT):
        assert await _count(rls_settings, TENANT) == 1
        assert await _count(rls_settings, OTHER) == 0
