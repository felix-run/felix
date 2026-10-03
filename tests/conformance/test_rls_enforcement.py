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
    """The regression guard for `list_tenants_with_events`'s `rls_bypass()`.

    Retention, the fiber claim and the other `list_tenants_with_*` helpers declare their own,
    and this covers none of them — `list_tenants_with_jobs` can lose its bypass with this file
    and the jobs contract both green, because the contract runs as the schema owner where a
    bypass is a no-op. Under `FELIX_DATABASE_RLS` that removal makes the sweep read an empty
    tenant list and report success, which is the silent no-op the bypass was added to fix.
    Parametrising this over all twelve is a roadmap item, next to this one's entry.

    What it does cover is real, and is the change no other arm of this suite can detect,
    because there the policy never applies in the first place.

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


# --- the worker's flushes --------------------------------------------------------------------


async def test_the_usage_flush_lands_every_tenants_rows_under_the_policy(rls_settings: Any) -> None:
    """The worker's `flush_usage` has no request context, so the store must bind each row's
    tenant itself. It bound none: every flush failed the policy's `WITH CHECK`, was requeued,
    and failed again — the meter never reached Postgres on an RLS deployment, and the buffer's
    ceiling started dropping the oldest usage."""
    from felix.usage import store as usage_store

    usage_store.pending_buffer().reset_for_tests()
    try:
        for tenant in (TENANT, OTHER):
            usage_store.record_tokens(
                rls_settings, tenant_id=tenant, manifest_id="m", model_id="m", tokens_input=3, tokens_output=2
            )
        assert await usage_store.flush_pending(rls_settings) == 2
        assert usage_store.pending_count() == 0, "nothing requeued"
        for tenant in (TENANT, OTHER):
            with rls_tenant(tenant):
                rows, _ = await usage_store.query(rls_settings, tenant, limit=10)
            assert [r["tenant_id"] for r in rows] == [tenant], (tenant, rows)
    finally:
        usage_store.pending_buffer().reset_for_tests()


# --- the audit export ------------------------------------------------------------------------


async def _seed_audit(settings: Any, tenant: str, count: int) -> None:
    """Through the store, which binds each event's tenant itself — the production write path."""
    from felix.audit import store as audit_store

    for i in range(count):
        audit_store.record_event(settings, tenant, "tool_call", ts=1_700_000_000_000 + i)
    assert await audit_store.flush_pending(settings) == count


async def _export(client: Any) -> list[dict[str, Any]]:
    import json

    resp = await client.get("/audit/export")
    assert resp.status_code == 200, resp.text
    rows = [json.loads(line) for line in resp.text.splitlines()]
    assert not [r for r in rows if "error" in r], rows
    return rows


async def test_the_audit_export_returns_every_page_under_the_policy(
    rls_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production stack, end to end: `create_application()` behind the real middleware.

    Under an enforcing policy a read with no tenant bound is empty, not an error — so an export
    whose later pages lost the binding would end early as a clean, short file. Five events at a
    page of two is three reads, two of them while the body streams. The other tenant's rows
    are in the table too, so a read that escaped the policy would show up as extra rows.

    Four things bind the tenant on this path, and removing any one of them leaves it green:
    the query's own `WHERE`, the export's per-read `rls_tenant`, the binding
    `async_run_with_context` makes for the whole request, and the session listener's fallback to
    the request context. Only removing every binding empties it. That is the claim here — the
    stack exports under the policy — and the test below pins the export's own guard alone.
    """
    from felix.audit import store as audit_store
    from felix.config import get_settings
    from felix_api.routes import audit as audit_route
    from httpx import ASGITransport, AsyncClient

    audit_store.pending_buffer().reset_for_tests()
    await _seed_audit(rls_settings, "default", 5)
    await _seed_audit(rls_settings, OTHER, 3)

    monkeypatch.setattr(audit_route, "_EXPORT_PAGE", 2)
    monkeypatch.setenv("FELIX_DATABASE_URL", str(rls_settings.database_url))
    monkeypatch.setenv("FELIX_DATABASE_RLS", "true")
    monkeypatch.setenv("FELIX_AUTH_MODE", "none")
    monkeypatch.setenv("FELIX_REDIS_URL", "")
    get_settings.cache_clear()
    try:
        from felix_api.main import create_application

        app = create_application()
        assert app.state.settings.database_rls, "the app must run with the policy on"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://felix.test") as client:
            rows = await _export(client)
    finally:
        get_settings.cache_clear()
        audit_store.pending_buffer().reset_for_tests()

    assert len(rows) == 5, f"the export ended early: {len(rows)} of 5"
    assert {r["tenant_id"] for r in rows} == {"default"}


async def test_the_audit_export_binds_the_tenant_on_every_read_itself(
    rls_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same export with no middleware, so nothing else binds a tenant.

    The middleware's binding covers the streamed body today because it wraps the whole ASGI
    call. The export does not rely on that: it binds per read, because its later pages are
    read after the handler has returned. This pins that second guard on its own — without it,
    every read here is unbound and the export is empty.
    """
    from fastapi import FastAPI
    from felix.audit import store as audit_store
    from felix_api.routes import audit as audit_route
    from httpx import ASGITransport, AsyncClient

    audit_store.pending_buffer().reset_for_tests()
    try:
        await _seed_audit(rls_settings, "default", 5)
        monkeypatch.setattr(audit_route, "_EXPORT_PAGE", 2)
        app = FastAPI()
        app.include_router(audit_route.router, prefix="/audit")
        app.state.settings = rls_settings.model_copy(update={"auth_mode": "none"})
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://felix.test") as client:
            rows = await _export(client)
    finally:
        audit_store.pending_buffer().reset_for_tests()

    assert len(rows) == 5, f"the export ended early: {len(rows)} of 5"


async def test_the_skill_job_sweeps_cross_tenants_and_the_reads_do_not(rls_settings: Any) -> None:
    """The worker's skill sweep claims across every tenant under an enforcing policy.

    `claim_next` on both stores declares `rls_bypass()`, as the fiber sweep does; without it the
    claim runs with no tenant bound and the policy returns nothing -- every queued evaluation and
    accepted improvement would wait forever, with the sweep reporting an empty queue. The
    per-tenant reads, heartbeats and finishes stay bound: one tenant cannot touch the other's row.
    """
    from felix.skills.eval_store import get_skill_eval_store
    from felix.skills.feedback_store import get_skill_feedback_store

    evals, feedback = get_skill_eval_store(rls_settings), get_skill_feedback_store(rls_settings)
    for n, tenant in enumerate((TENANT, OTHER), start=1):
        row_id = f"00000000-0000-4000-8000-{n:012d}"
        base = {"id": row_id, "name": "s", "created_at": n}
        await evals.insert(tenant, {**base, "version": "0.1.0", "status": "queued"})
        await feedback.insert(
            tenant,
            {**base, "target_version": "0.1.0", "source": "human", "body": "b", "status": "pending"},
        )
        await feedback.decide(tenant, row_id, status="accepted", improve=True, by="ops", note=None, at=n)

    claimed = [await evals.claim_next(now=1_000), await evals.claim_next(now=1_001)]
    taken = [await feedback.claim_next(now=1_000), await feedback.claim_next(now=1_001)]

    assert sorted(r["tenant_id"] for r in claimed if r) == [TENANT, OTHER], claimed
    assert sorted(r["tenant_id"] for r in taken if r) == [TENANT, OTHER], taken
    mine = next(r for r in claimed if r and r["tenant_id"] == TENANT)
    assert await evals.get(TENANT, mine["id"]) is not None
    assert await evals.get(OTHER, mine["id"]) is None, "a bound tenant read another tenant's evaluation"
    assert await feedback.get(OTHER, mine["id"]) is None, "a bound tenant read another tenant's feedback"
    assert not await evals.heartbeat(OTHER, mine["id"], token=mine["claim_token"], now=1_002)
    assert await evals.heartbeat(TENANT, mine["id"], token=mine["claim_token"], now=1_002)
