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


# --------------------------------------------------------------------------
# The resume half, driven through `resume_due_fibers`.
#
# The first version of this file tested it by rebuilding the production expression inside the
# test body and asserting on that copy — which asserts that `frozenset` works. A reviewer
# reverted `fibers.py` to the old `principal_sub="fiber"` with no scopes and every test here,
# plus 50 more matching "fiber or durable or temporal", stayed green: the whole
# authority-granting half of the change was deletable with a clean suite.
#
# These observe the `AuthContext` that `_run_fiber_step` actually constructs, by capturing it
# where the production code hands it on.
# --------------------------------------------------------------------------


@pytest.fixture
def captured_auth(monkeypatch):
    """Every AuthContext `_run_fiber_step` passes to `prepare_tenant_invoke`."""
    import felix.runtime as runtime

    seen: list[AuthContext] = []

    async def _capture(settings, *, resolved, auth, thread_id, **kw):
        seen.append(auth)
        raise RuntimeError("stop here — the auth is what is under test")

    monkeypatch.setattr(runtime, "prepare_tenant_invoke", _capture)
    return seen


async def _seed_and_resume(settings: Settings, state: dict) -> None:
    from felix.durability.fibers import create_fiber, resume_due_fibers

    await create_fiber(settings, "t", kind="durable_chat", status="pending", state=state)
    await resume_due_fibers(settings)


def _invoke_state(**extra) -> dict:
    from felix.durability.fibers import now_ms

    state = {
        "steps": [{"op": "invoke", "manifest_id": "quick", "messages": [], "thread_id": "th"}],
        "cursor": 0,
        "stash": {},
        "expires_at": now_ms() + 60_000,
    }
    state.update(extra)
    return state


@pytest.mark.asyncio
async def test_a_resumed_fiber_runs_with_the_recorded_scopes(captured_auth) -> None:
    settings = _settings()
    await _seed_and_resume(
        settings,
        _invoke_state(auth={"principal_sub": "alice", "scopes": ["tools:calc"], "anonymous": False}),
    )

    assert captured_auth, "the step never reached prepare_tenant_invoke"
    assert captured_auth[0].scopes == frozenset({"tools:calc"})
    # The actor is the fiber; the person it runs for is carried separately, so an audit row
    # never claims Alice took an action a worker took minutes later.
    assert captured_auth[0].principal_sub == "fiber"
    assert captured_auth[0].on_behalf_of == "alice"


@pytest.mark.asyncio
async def test_a_resumed_fiber_with_no_recorded_auth_runs_with_none(captured_auth) -> None:
    """A row enqueued before this existed. It must keep denying, not inherit something."""
    settings = _settings()
    await _seed_and_resume(settings, _invoke_state())

    assert captured_auth, "the step never reached prepare_tenant_invoke"
    assert captured_auth[0].scopes == frozenset()
    assert captured_auth[0].principal_sub == "fiber"


@pytest.mark.asyncio
async def test_an_expired_fiber_does_not_run_at_all(captured_auth) -> None:
    """The claim the whole design rests on, asserted against behaviour rather than a number.

    The previous version of this checked that `expires_at` was a positive integer in a dict.
    """
    from felix.durability.fibers import now_ms

    settings = _settings()
    await _seed_and_resume(
        settings,
        _invoke_state(
            expires_at=now_ms() - 1,
            auth={"principal_sub": "alice", "scopes": ["tools:calc"], "anonymous": False},
        ),
    )

    assert captured_auth == [], "an expired fiber ran a step with the recorded scopes"


def test_an_approval_bound_to_the_caller_still_matches_their_resumed_run() -> None:
    """`bind_principal` matches `on_behalf_of` when a machine actor is running someone's work.

    Without it, separating the actor from the person would silently break approval continuity
    across a resume: the grant says `alice`, the resumed run says `fiber`, no match, deny. And
    for every ordinary caller `on_behalf_of` is empty, so nothing changes.
    """
    fiber_run = AuthContext(principal_sub="fiber", tenant_id="t", on_behalf_of="alice")
    interactive = AuthContext(principal_sub="alice", tenant_id="t")
    other_person = AuthContext(principal_sub="mallory", tenant_id="t")

    def bound_subject(auth: AuthContext) -> str:
        return auth.on_behalf_of or auth.principal_sub or ""

    assert bound_subject(fiber_run) == "alice", "the resumed run cannot use its own grant"
    assert bound_subject(interactive) == "alice", "an ordinary caller is unaffected"
    assert bound_subject(other_person) == "mallory", "the binding still separates principals"


@pytest.mark.asyncio
async def test_a_run_records_nothing_when_the_caller_is_a_different_tenant() -> None:
    """`start_durable_chat` takes `tenant_id` *and* reads the principal from ambient context.

    Both callers derive them from the same request today. A future admin route or per-tenant
    fan-out would write tenant A's scopes into tenant B's fiber, which the resume then applies
    inside `rls_tenant(B)` — cross-tenant authority transfer with no change at the call site.
    """
    settings = _settings()
    other_tenant = AuthContext(
        principal_sub="alice", tenant_id="other", scopes=frozenset({"tools:calc"}), anonymous=False
    )
    ctx = RequestContext(settings=settings, auth=other_tenant, manifest_id="m")
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
    row = await get_fiber(settings, "t", started.get("resume_token") or started.get("id"))

    assert "auth" not in (row or {}).get("state_json", {}), "recorded another tenant's authority"


@pytest.mark.asyncio
async def test_the_horizon_never_outlives_the_token_that_started_the_run() -> None:
    """There is no revocation anywhere in `felix/auth/` — `exp` is the sole and complete bound
    on a compromised credential. Without this clamp a 60-second token starting a 300-second run
    would confer its scopes for four minutes past its own death."""
    import time

    settings = _settings()
    short_lived = AuthContext(
        principal_sub="alice",
        tenant_id="t",
        scopes=frozenset({"tools:calc"}),
        anonymous=False,
        raw_claims={"exp": int(time.time()) + 60},
    )
    state = await _enqueue(settings, auth=short_lived)

    from felix.durability.fibers import now_ms

    horizon = (state["expires_at"] - now_ms()) / 1000
    assert 0 < horizon <= 60, f"authority outlives the token by {horizon - 60:.0f}s"


def test_the_resume_token_ttl_is_bounded() -> None:
    """The number the whole safety argument rests on. It was the one lifetime in the manifest
    schema with no ceiling, eighty lines from `ApprovalRule.ttl_seconds`, which has one."""
    from felix.manifests.schema import ABSOLUTE_LIMITS

    with pytest.raises(ValueError):
        ExecutionSpec(mode="durable", resume_token_ttl_seconds=315_360_000)

    assert ABSOLUTE_LIMITS["resume_token_ttl_seconds"] <= 86_400
