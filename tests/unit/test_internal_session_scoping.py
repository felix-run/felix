"""`/internal/sessions/{id}/events` must not write into another tenant's log.

The session id came straight off the path into `append_event(tenant_id=...,
session_id=...)` with no check that the thread belongs to that tenant. Every other
router routes a client-supplied thread through `effective_thread_id`, whose module
docstring says the rule "lives in one place rather than being restated per router" —
this one restated it by omitting it.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import Settings
from httpx import ASGITransport, AsyncClient

SECRET = "s3cret"


def _settings(**over: Any) -> Settings:
    return Settings(
        allow_insecure=True,
        auth_mode="none",
        host="127.0.0.1",
        environment="development",
        object_store="memory",
        database_url="memory://internal-scope",
        consumer_shared_secret=SECRET,
        **over,
    )


async def _post(session_id: str) -> tuple[int, str]:
    from felix_api.app import create_app

    app = create_app(settings=_settings(), plugins=[])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/internal/sessions/{session_id}/events",
            json={"type": "message", "payload": {"content": "hello"}},
            headers={"x-felix-consumer-secret": SECRET},
        )
        return resp.status_code, resp.text


@pytest.mark.asyncio
async def test_a_thread_owned_by_another_tenant_is_refused() -> None:
    """With auth_mode=none the caller is tenant `default`; `acme:foo` is not theirs.

    This is the write that created the colliding row the memory-provenance
    cross-tenant read depended on.
    """
    status, body = await _post("acme:foo")

    assert status == 403, body
    assert "thread" in body.lower()


@pytest.mark.asyncio
async def test_the_callers_own_thread_is_accepted() -> None:
    status, body = await _post("default:foo")

    assert status == 200, body
    assert '"status":"ok"' in body.replace(" ", "")


@pytest.mark.asyncio
async def test_a_fiber_thread_is_accepted() -> None:
    """Fibers mint `{tenant}:fiber:{id}`, so a `:` inside the suffix is legitimate
    and the rule cannot be 'the suffix is delimiter-free'."""
    status, body = await _post("default:fiber:abc123")

    assert status == 200, body


@pytest.mark.asyncio
async def test_an_unprefixed_thread_is_refused() -> None:
    """A bare id belongs to no tenant and used to be filed under the caller's."""
    status, _ = await _post("bare-thread-id")

    assert status == 403


# --- the same rule, one router over -------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("user", ["fiber:abc123", "job:nightly-report", "a2a:task-1", "x#y"])
async def test_v1_refuses_a_user_that_forges_a_reserved_thread(user: str) -> None:
    """`/v1` hand-rolled `f"{tenant}:{body.user}"` instead of using the shared rule.

    The tenant prefix was applied, so this was never cross-tenant — but `body.user`
    was never delimiter-screened, so a client could name a durable fiber's or a
    scheduled job's thread and have its turns appended to that run's session log,
    which the run then replays as history.
    """
    from felix_api.app import create_app

    app = create_app(settings=_settings(), plugins=[])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "quick", "user": user, "messages": [{"role": "user", "content": "hi"}]},
        )

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid_user"


@pytest.mark.asyncio
async def test_v1_still_accepts_an_ordinary_user() -> None:
    """The screening must not break the feature: `user` is how /v1 addresses a thread."""
    from felix_api.threads import effective_thread_id

    assert effective_thread_id("acme", "alice") == "acme:alice"


def test_the_two_helpers_agree() -> None:
    """`effective_thread_id` builds ids; `thread_belongs_to_tenant` checks them.

    They disagreed: one rejected `#` and a delimiter-bearing tenant, the other did
    not — so `/internal` could mint ids no chat route could address.
    """
    from felix_api.threads import MAX_THREAD_ID, effective_thread_id, thread_belongs_to_tenant

    # Anything the builder produces, the checker must accept.
    built = effective_thread_id("acme", "alice")
    assert built is not None and thread_belongs_to_tenant("acme", built)

    # A tenant that would break the partition is refused by both.
    assert effective_thread_id("acme:sub", "x") is None
    assert not thread_belongs_to_tenant("acme:sub", "acme:sub:x")

    # And both bound the length.
    assert effective_thread_id("acme", "a" * MAX_THREAD_ID) is None
    assert not thread_belongs_to_tenant("acme", "acme:" + "a" * MAX_THREAD_ID)
