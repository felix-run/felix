"""`remember` must read the turn ordinal from its own tenant's session log.

`_provenance` called `get_session_store(settings)` with no `tenant_id`, so it always
resolved tenant `"default"`. For every other tenant the lookup found nothing and the
fact landed at `origin_seq = 0`; where a thread id collided with one under
`"default"`, it read that tenant's log instead.
"""

from __future__ import annotations

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.memory.tools import _provenance
from felix.session.store import get_session_store
from felix.session.types import AppendableEvent

SETTINGS = Settings(database_url="memory://provenance")

# `_memory_session_stores` is keyed by tenant *alone* — the database URL is not part
# of the key — so a distinct `memory://` URL buys no isolation between test files or
# tests. Each test therefore takes its own thread id; the collision this file is
# about is between tenants, so a thread id only needs to be shared within one test.
# Without this the strongest test here passed on the unfixed code whenever it ran
# after its neighbour, which had topped up the same tenant's log.


async def _seed(tenant: str, thread: str, count: int) -> None:
    session = get_session_store(SETTINGS, tenant_id=tenant).open(thread)
    for i in range(count):
        await session.append(AppendableEvent(kind="message", role="user", content=str(i)))


@pytest.mark.asyncio
async def test_provenance_reads_the_callers_own_tenant() -> None:
    # Two tenants, same thread id, different depths — the collision that turns a
    # wrong-tenant read into a wrong answer rather than an empty one.
    thread = "th-collision"
    await _seed("default", thread, 2)
    await _seed("acme", thread, 5)

    ctx = RequestContext(
        settings=SETTINGS,
        auth=AuthContext(tenant_id="acme", principal_sub="u", anonymous=False),
        manifest_id="m",
        thread_id=thread,
    )
    async with async_run_with_context(ctx):
        thread_id, origin_seq = await _provenance(SETTINGS, tenant_id="acme")

    assert thread_id == thread
    # 5, not 2: `acme`'s own log. Before the fix this read `default` and returned 2.
    assert origin_seq == 5


@pytest.mark.asyncio
async def test_provenance_is_empty_without_a_thread() -> None:
    """Covers the no-thread early return, not tenancy.

    Unlike its neighbours this one passes on the unfixed code — it never reaches the
    store — so its green is not evidence about tenant scoping.
    """

    ctx = RequestContext(
        settings=SETTINGS,
        auth=AuthContext(tenant_id="acme", principal_sub="u", anonymous=False),
        manifest_id="m",
    )
    async with async_run_with_context(ctx):
        assert await _provenance(SETTINGS, tenant_id="acme") == ("", None)


@pytest.mark.asyncio
async def test_a_tenant_with_no_history_gets_genesis_not_another_tenants_count() -> None:
    """The non-colliding case: still wrong before the fix, just less visibly."""
    thread = "th-no-history"
    await _seed("default", thread, 3)

    ctx = RequestContext(
        settings=SETTINGS,
        auth=AuthContext(tenant_id="fresh-tenant", principal_sub="u", anonymous=False),
        manifest_id="m",
        thread_id=thread,
    )
    async with async_run_with_context(ctx):
        _, origin_seq = await _provenance(SETTINGS, tenant_id="fresh-tenant")

    assert origin_seq == 0
