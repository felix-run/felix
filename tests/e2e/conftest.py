"""The `boot` fixture: the API booted the way production boots it, with a scripted model.

The pieces it is built from — `Booted`, `ProviderSpy`, the scripted routes and why each
ordering matters — live in `tests/support/e2e.py`, so a test outside this directory can reuse
them without importing a conftest.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from felix_ai.decide import NoulAnswer
from felix_ai.providers.scripted import ScriptedTurn
from httpx import ASGITransport, AsyncClient

from tests.support.e2e import (
    DEFAULT_ROUTE,
    Booted,
    ProviderSpy,
    assert_routes_to_the_script,
    provider_registry,
    scripted_model_routes,
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
        monkeypatch.setenv("FELIX_MODEL_ROUTES", json.dumps(scripted_model_routes()))
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
        saved_providers = dict(provider_registry())
        register_model_provider("scripted", spy.factory())
        try:
            from felix_api.main import create_application

            app = create_application()
            settings = app.state.settings
            assert_routes_to_the_script(settings)
            for name, manifest in (manifests or {}).items():
                await put_version(settings, "default", name, manifest)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://felix.test", timeout=30.0) as client:
                yield Booted(client=client, spy=spy, settings=settings)
        finally:
            # Leaving `scripted` registered would let a later test route to a fake and pass.
            registry = provider_registry()
            registry.clear()
            registry.update(saved_providers)
            # Both sides, as the docstring above claims: clearing only on entry protects these
            # tests from earlier ones and leaves later ones exposed to this suite's events.
            audit_store._pending.reset_for_tests()
            audit_store._memory_events.clear()
            usage_store.clear_memory()
            get_settings.cache_clear()

    yield _boot


@pytest.fixture
def verdict() -> Iterator[dict[str, Any]]:
    """The scripted decider's probability that the judged text meets the criterion."""
    from felix.decisions import register_builtin_deciders
    from felix_ai.decide import reset_decision_provider_registry
    from felix_ai.decide.scripted import register_scripted_decider

    state: dict[str, Any] = {"p": 0.0, "judged": []}

    def answer(judged: Any, questions: Any) -> dict[str, Any]:
        state["judged"].append(judged["text"])
        return {key: NoulAnswer(state["p"]) for key in questions}

    register_scripted_decider("scripted", answer)
    try:
        yield state
    finally:
        reset_decision_provider_registry()
        register_builtin_deciders()
