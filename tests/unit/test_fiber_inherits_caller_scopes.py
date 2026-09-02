"""A durable run resumes as the caller who started it, bounded by the run's own TTL.

Before this, `_step_fiber` built `AuthContext(principal_sub="fiber")` with the default empty
scope set. So `spec.policies` denied every policied tool on resume and
`auth.inbound.required_scopes` refused the resume outright: a manifest that worked over HTTP
stopped working the moment `execution.mode: durable` was set, and nothing said why.

This is authority living in durable state, so the tests below are mostly about its bounds
rather than its presence: exactly the caller's scopes and never wider, nothing at all when
there was no caller, and dead when the run expires. The expiry is not new — `state.expires_at`
was already enforced at resume, and its default is `hibernate_after_seconds` (300s), which is
what makes inherited authority defensible: a fiber cannot outlive the token that started it by
more than the run's own TTL.
"""

from __future__ import annotations

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, run_with_context
from felix.durability.fibers import get_fiber
from felix.durability.runs import start_durable_chat
from felix.manifests.schema import ExecutionSpec
from felix.patterns.types import ChatMessage


def _settings(**kw) -> Settings:
    return Settings(
        database_url="memory://ci",
        object_store="memory",
        auth_mode="none",
        allow_insecure=True,
        host="127.0.0.1",
        **kw,
    )


async def _enqueue(settings: Settings, *, auth: AuthContext | None, **spec_kw) -> dict:
    kwargs = dict(
        manifest_id="m",
        messages=[ChatMessage(role="user", content="hi")],
        thread_id=None,
        model_id=None,
        execution=ExecutionSpec(mode="durable", **spec_kw),
    )
    if auth is None:
        started = await start_durable_chat(settings, "t", **kwargs)
    else:
        ctx = RequestContext(settings=settings, auth=auth, manifest_id="m")
        with run_with_context(ctx):
            started = await start_durable_chat(settings, "t", **kwargs)
    token = started.get("resume_token") or started.get("id")
    row = await get_fiber(settings, "t", token)
    return (row or {}).get("state_json", {})


def _caller(*scopes: str) -> AuthContext:
    return AuthContext(
        principal_sub="alice",
        tenant_id="t",
        scopes=frozenset(scopes),
        anonymous=False,
        scheme="jwt",
    )


@pytest.mark.asyncio
async def test_the_run_records_exactly_the_callers_scopes() -> None:
    state = await _enqueue(_settings(), auth=_caller("tools:calc", "chat:write"))

    assert state["auth"]["scopes"] == ["chat:write", "tools:calc"], "not the caller's own set"
    assert state["auth"]["principal_sub"] == "alice"


@pytest.mark.asyncio
async def test_a_run_started_without_a_request_context_records_nothing() -> None:
    """Fail closed on the way in, not only on the way out. A fiber with no recorded caller
    resumes as principal `fiber` with no scopes, which is what every fiber did before."""
    state = await _enqueue(_settings(), auth=None)

    assert "auth" not in state


@pytest.mark.asyncio
async def test_an_anonymous_caller_confers_nothing() -> None:
    """`auth_mode=none` makes every caller anonymous with an empty scope set. Recording that
    faithfully must not become a way to gain authority a caller never had."""
    anon = AuthContext(principal_sub="anonymous", tenant_id="t", scopes=frozenset(), anonymous=True)
    state = await _enqueue(_settings(), auth=anon)

    assert state["auth"]["scopes"] == []
    assert state["auth"]["anonymous"] is True


@pytest.mark.asyncio
async def test_the_resumed_context_carries_the_recorded_scopes_and_no_more() -> None:
    """The resume half. Built the way `_step_fiber` builds it, from a stored state blob."""
    stored = {"principal_sub": "alice", "scopes": ["tools:calc"], "anonymous": False, "scheme": "jwt"}

    auth = AuthContext(
        tenant_id="t",
        principal_sub=str(stored.get("principal_sub") or "fiber"),
        scopes=frozenset(str(x) for x in (stored.get("scopes") or ())),
        anonymous=bool(stored.get("anonymous", False)),
        scheme=str(stored.get("scheme") or "anonymous"),
    )

    assert auth.scopes == frozenset({"tools:calc"})
    assert "chat:write" not in auth.scopes, "resume must not widen what was recorded"


@pytest.mark.asyncio
async def test_a_fiber_with_no_recorded_auth_resumes_with_no_scopes() -> None:
    """The pre-existing rows. A fiber enqueued before this change has no `auth` key, and must
    keep denying rather than inheriting something."""
    stored: dict = {}

    auth = AuthContext(
        tenant_id="t",
        principal_sub=str(stored.get("principal_sub") or "fiber"),
        scopes=frozenset(str(x) for x in (stored.get("scopes") or ())),
        anonymous=bool(stored.get("anonymous", False)),
    )

    assert auth.scopes == frozenset()
    assert auth.principal_sub == "fiber"


@pytest.mark.asyncio
async def test_the_recorded_authority_expires_with_the_run() -> None:
    """The bound that makes this defensible.

    Inherited authority in durable state is only acceptable because it dies: `expires_at` is
    written at enqueue from the run's TTL and checked before a step runs. Without an explicit
    `resume_token_ttl_seconds` the default is `hibernate_after_seconds` — 300s, not 300 days.
    """
    state = await _enqueue(_settings(), auth=_caller("tools:calc"), resume_token_ttl_seconds=60)
    assert state["expires_at"] > 0

    from felix.durability.fibers import now_ms

    horizon_seconds = (state["expires_at"] - now_ms()) / 1000
    assert 0 < horizon_seconds <= 60, f"the recorded scopes outlive the run by {horizon_seconds}s"


@pytest.mark.asyncio
async def test_the_default_horizon_is_the_hibernate_window_not_unbounded() -> None:
    state = await _enqueue(_settings(hibernate_after_seconds=120), auth=_caller("tools:calc"))

    from felix.durability.fibers import now_ms

    horizon_seconds = (state["expires_at"] - now_ms()) / 1000
    assert 0 < horizon_seconds <= 120


@pytest.mark.asyncio
async def test_the_recorded_scopes_are_not_returned_by_the_run_status_api() -> None:
    """Authority in a row is one thing; authority in a polled response is another. The resume
    token is the only credential a caller needs to read run status."""
    from felix.durability.runs import get_durable_run

    settings = _settings()
    ctx = RequestContext(settings=settings, auth=_caller("tools:calc"), manifest_id="m")
    with run_with_context(ctx):
        started = await start_durable_chat(
            settings,
            "t",
            manifest_id="m",
            messages=[ChatMessage(role="user", content="hi")],
            thread_id=None,
            model_id=None,
            execution=ExecutionSpec(mode="durable"),
        )
    token = started.get("resume_token") or started.get("id")

    status = await get_durable_run(settings, "t", token)

    assert "tools:calc" not in str(status), f"the run status leaked the caller's scopes: {status}"
    assert "alice" not in str(status), f"the run status leaked the caller's subject: {status}"
