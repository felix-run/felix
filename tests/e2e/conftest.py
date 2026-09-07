"""Boot the API the way production boots it, with a model that answers from a script.

Every HTTP test in this repo cuts the chain at one of two places. The ones that reach a route
replace `build_tenant_agent` with a stub (`tests/unit/test_sse_resume.py`,
`tests/unit/test_reply_controls.py`), so nothing below the route runs. The ones that run the
real compiler and the real loop never open a socket, and hand-build `RequestContext`
themselves. So the path a request actually takes — middleware, manifest resolution, the
compile, the governance wrappers, the pattern, the model, the reply back out — was covered in
pieces and never end to end. That is the defect shape `.claude/rules/felix-invariants.md`
names: the branch production takes is the branch nothing covers.

What makes it testable without a network is `felix_ai.providers.scripted`, which implements
the published `ModelProvider` contract and is deliberately absent from the production
registry. Registering it under the route name the bundled manifests already resolve to
redirects every model call in the process to a script and leaves everything else alone.

Three facts about ordering and teardown are load-bearing:

* `Settings.validate_runtime` → `_validate_model_route_providers` rejects a
  `FELIX_MODEL_ROUTES` naming an unregistered provider, so the provider is registered
  *before* `create_application()`, not after.
* `ASGITransport` never runs the lifespan, so the audit and usage flush loops the API starts
  do not exist here. Tests call `felix.flush.flush_all` and read the store back, which is the
  stronger assertion anyway: it exercises the write path rather than the in-process buffer.
* `ReactAgent._resolve_model` builds the model once per invoke, before the step loop — so one
  `ScriptedClient` serves every step of a turn, and a two-entry script is a tool call followed
  by an answer. The spy keeps each client past the request that built it, which is the only
  way to assert on `calls` after the response has been returned.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest
from felix_ai.providers.scripted import ScriptedClient, ScriptedTurn
from httpx import ASGITransport, AsyncClient

# The route every manifest whose spec names no model resolves to, because the fixture pins
# `Settings.default_model_id` to it. A name of its own rather than a real one: the first
# version of this file assumed the built-in default and the repo `.env` overrode it, so the
# scripted route was never taken and the suite billed live Anthropic calls instead. The wire
# id stays a real catalog entry so the turn prices at a rate the catalog knows — pricing keys
# on the wire model, not on this name.
DEFAULT_ROUTE = "e2e-scripted"
WIRE_MODEL = "claude-sonnet-5"


def _scripted_routes() -> dict[str, dict[str, str]]:
    """Every logical model id, pointed at the script — not only the default one.

    `_parse_routes_cached` overlays `FELIX_MODEL_ROUTES` onto `DEFAULT_MODEL_ROUTES` rather
    than replacing it, so overriding one key leaves `claude-opus`, `gpt-4.1` and the rest
    resolving to a real vendor. Anything naming a logical id reaches them: `spec.model.id`,
    `spec.model.fallbacks`, a judge's `model:`, a sub-agent's model, confidence escalation.
    Blank keys do not save us there — `resolve_provider_config` logs a warning and sends the
    request unauthenticated, which is still egress and still non-deterministic. Mapping the
    whole space is what makes "this suite cannot reach a vendor" true by construction.
    """
    from felix.config import DEFAULT_MODEL_ROUTES

    names = [*DEFAULT_MODEL_ROUTES, DEFAULT_ROUTE]
    return {name: {"provider": "scripted", "model": WIRE_MODEL} for name in names}


@dataclass
class ProviderSpy:
    """Every scripted client the registry built, kept after its request has finished."""

    clients: list[ScriptedClient] = field(default_factory=list)

    @property
    def calls(self) -> list[str]:
        """Model calls across every client, in order: `chat`, `stream`, or `stream_turn`."""
        return [call for client in self.clients for call in client.calls]

    #: The turns not yet consumed, shared by every client this spy builds.
    #:
    #: `scripted_factory` copies the script per client, which is right for a one-request
    #: test and wrong for every multi-request one: a second request replays the first
    #: request's turns instead of continuing past them, so a test of steer, fork or rewind
    #: cannot say what the model answers the second time. Handing every client the *same*
    #: list makes `ScriptedClient._next`'s `pop(0)` draw from one queue, so a script reads
    #: as the turns the run will take, in order, however many requests it spans.
    #:
    #: One queue assumes one model call at a time, which holds for every pattern that runs a
    #: loop. It does not hold for a fan-out: `patterns/delegating.py` gathers its sub-agents,
    #: so each builds a client and they draw from this list in whatever order they suspend.
    #: A test of that pattern wants a sub-queue per client, not this.
    queue: list[ScriptedTurn] = field(default_factory=list)

    def push(self, *turns: ScriptedTurn) -> None:
        """Add turns for the next request, mid-test."""
        self.queue.extend(turns)

    def factory(self) -> Callable[..., ScriptedClient]:
        def build(model_id: str, route: Any, spec: Any, settings: Any) -> ScriptedClient:
            client = ScriptedClient(model_id=model_id, route=route, script=self.queue)
            _make_strict(client)
            self.clients.append(client)
            return client

        return build


def _make_strict(client: ScriptedClient) -> None:
    """Raise when the script runs out instead of inventing a turn.

    `ScriptedClient._next` falls back to a default `ScriptedTurn()` — content `"ok"`, usage
    11/7 — which is right for the conformance arm and wrong here: an under-specified script
    would answer plausibly, and the metering assertions would be reading a fabricated default
    rather than the turn the test wrote. Left in the fixture so the shared double is untouched.

    It matters more now that the queue is shared across requests: without this, a test whose
    first request consumed a turn too many would be answered by an invented one on the second,
    and the failure would surface as a puzzling content mismatch two requests later.

    The raise does not become a 4xx: the react loop catches a failing model call and degrades to
    an error reply, so the request still returns 200 with no scripted content. That is enough —
    the point is that no assertion here can be satisfied by a turn the test did not write — but
    it does mean a short script shows up as a content mismatch rather than as this message.
    """
    original = client._next

    def strict() -> Any:
        if not client.script:
            raise AssertionError(
                f"the scripted model ran out of turns after {len(client.calls)} call(s); "
                "the run wanted more model calls than the test scripted"
            )
        return original()

    client._next = strict  # type: ignore[method-assign]


@dataclass
class Booted:
    """One booted application: an HTTP client into it, the model spy, and its settings."""

    client: AsyncClient
    spy: ProviderSpy
    settings: Any

    def push(self, *turns: ScriptedTurn) -> None:
        """Queue turns for the next request in this boot."""
        self.spy.push(*turns)


def _provider_registry() -> dict[str, Any]:
    """The one live provider dict, so a snapshot restores plugins as well as builtins."""
    from felix_ai import registry

    return registry._providers


def _assert_routes_to_the_script(settings: Any) -> None:
    """Fail the boot if the default route is not the scripted provider.

    Guards the guard. Every assertion in this suite is about what the stack did with a
    *scripted* answer, and each one would also pass against a live vendor that happened to say
    something similar — while silently spending money and making the suite non-deterministic.
    The first run of this file did exactly that. Checking the resolved route, rather than
    trusting the environment the fixture just set, is what makes that unrepeatable.
    """
    from felix.patterns.model import parse_model_routes

    routes = parse_model_routes(settings)
    assert routes, "no model routes resolved at all"
    assert settings.default_model_id in routes, f"no route for default_model_id {settings.default_model_id!r}"
    live = sorted(name for name, route in routes.items() if route.provider != "scripted")
    assert not live, (
        f"{len(live)} model route(s) still resolve to a real provider: {', '.join(live)}; "
        "this suite must not be able to reach a model vendor by any route"
    )


@pytest.fixture
def boot(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Any]]:
    """Return an async context manager that boots `create_application()` with a script.

    `env` overrides settings for the boot (the API reads them through `get_settings`, whose
    cache is cleared on the way in and the way out). `manifests` are written to the manifest
    store before the first request, so a test can govern a manifest without touching
    `manifests/*.yaml`; the autouse fixture in `tests/conftest.py` clears that store per test.
    """

    @asynccontextmanager
    async def _boot(
        script: list[ScriptedTurn] | None = None,
        *,
        env: dict[str, str] | None = None,
        manifests: dict[str, Any] | None = None,
    ) -> AsyncIterator[Booted]:
        from felix.audit import store as audit_store
        from felix.config import get_settings
        from felix.manifests.store import put_version
        from felix.patterns.model_registry import register_model_provider
        from felix.usage import store as usage_store

        monkeypatch.setenv("FELIX_DEFAULT_MODEL_ID", DEFAULT_ROUTE)
        monkeypatch.setenv("FELIX_MODEL_ROUTES", json.dumps(_scripted_routes()))
        # Belt and braces with `scripts/test.sh`, which blanks these for the whole suite.
        # `model_provider_options` is not redundant: `resolve_provider_config` prefers the
        # `api_key` inside it over both named fields, so a credential there re-arms a vendor.
        monkeypatch.setenv("FELIX_ANTHROPIC_API_KEY", "")
        monkeypatch.setenv("FELIX_OPENAI_API_KEY", "")
        monkeypatch.setenv("FELIX_MODEL_PROVIDER_OPTIONS", "")
        # No Redis: the snapshot, lease and steer paths consult it and would otherwise spend
        # the test retrying a refused port. Same reasoning as `tests/unit/test_sse_resume.py`.
        monkeypatch.setenv("FELIX_REDIS_URL", "")
        for key, value in (env or {}).items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()

        # These buffers are process globals that outlive a test, and the autouse fixture in
        # `tests/conftest.py` does not reach them. A leaked event would show up as another
        # test's audit assertion passing for the wrong reason.
        audit_store._pending.reset_for_tests()
        audit_store._memory_events.clear()
        # `clear_memory` resets the usage buffer too; audit has no such helper, hence the pair
        # of pokes above it.
        usage_store.clear_memory()

        spy = ProviderSpy(queue=list(script or []))
        # Snapshot rather than `reset + register_builtin_providers()`: that idiom restores the
        # builtins and silently drops every plugin-registered provider, because
        # `load_optional_plugins` has already run and will not run again. Inert in the lean CI
        # venv, not inert under `make install-full`. `felix.patterns.model` documents the bug.
        saved_providers = dict(_provider_registry())
        register_model_provider("scripted", spy.factory())
        try:
            from felix_api.main import create_application

            app = create_application()
            settings = app.state.settings
            _assert_routes_to_the_script(settings)
            for name, manifest in (manifests or {}).items():
                await put_version(settings, "default", name, manifest)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://felix.test", timeout=30.0) as client:
                yield Booted(client=client, spy=spy, settings=settings)
        finally:
            # Leaving `scripted` registered would let a later test route to a fake and pass.
            registry = _provider_registry()
            registry.clear()
            registry.update(saved_providers)
            # Both sides, as the docstring above claims: clearing only on entry protects these
            # tests from earlier ones and leaves later ones exposed to this suite's events.
            audit_store._pending.reset_for_tests()
            audit_store._memory_events.clear()
            usage_store.clear_memory()
            get_settings.cache_clear()

    yield _boot
