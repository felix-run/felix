"""Registrable backends: a third party can add one and have it selected.

Each of these was a Protocol behind a hardcoded if/elif with a closed `Literal`
in front of it, so implementing the interface was never enough to be chosen.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.config import Settings


@pytest.fixture
def restore_backends() -> Any:
    """Only the backend-registry tests touch these process-wide dicts."""
    from felix import secrets as secrets_mod
    from felix import storage as storage_mod
    from felix import warehouse as warehouse_mod

    saved = [(m, dict(m._backends)) for m in (storage_mod, secrets_mod, warehouse_mod)]
    yield
    for module, backends in saved:
        module._backends.clear()
        module._backends.update(backends)


@pytest.fixture
def restore_strategies() -> Any:
    from felix.session import strategies as strategies_mod

    saved = dict(strategies_mod._strategies)
    yield
    strategies_mod._strategies.clear()
    strategies_mod._strategies.update(saved)


@pytest.fixture
def clean_audit_buffer() -> Any:
    """`_pending` is process-global; do not leave events for the next test."""
    from felix.audit.store import _pending

    yield
    _pending.clear()


def test_a_registered_object_store_is_selected(restore_backends: Any) -> None:
    from felix.storage import build_object_store, register_object_store

    class _Custom:
        transport = "custom"

    register_object_store("acme-blob", lambda settings: _Custom())
    assert isinstance(build_object_store(Settings(object_store="acme-blob")), _Custom)


def test_a_registered_secrets_backend_is_selected(restore_backends: Any) -> None:
    from felix.secrets import build_secrets, register_secrets_backend

    class _Vault:
        async def get(self, name: str) -> str | None:
            return f"vault:{name}"

    register_secrets_backend("acme-vault", lambda settings: _Vault())
    assert isinstance(build_secrets(Settings(secrets_backend="acme-vault")), _Vault)


def test_a_registered_warehouse_is_selected(restore_backends: Any) -> None:
    from felix.warehouse import build_warehouse, register_warehouse_backend

    class _Snowflake:
        async def ping(self) -> bool:
            return True

    register_warehouse_backend("acme-snow", lambda settings: _Snowflake())
    assert isinstance(build_warehouse(Settings(warehouse="acme-snow")), _Snowflake)


@pytest.mark.parametrize(
    ("env", "kwargs"),
    [
        ("FELIX_OBJECT_STORE", {"object_store": "nope"}),
        ("FELIX_SECRETS_BACKEND", {"secrets_backend": "nope"}),
        ("FELIX_WAREHOUSE", {"warehouse": "nope"}),
        ("FELIX_MEMORY_EMBEDDER", {"memory_embedder": "nope"}),
    ],
)
def test_an_unregistered_backend_is_refused_at_startup(env: str, kwargs: dict[str, str]) -> None:
    """Opening these settings from Literal to str moved typo-catching here."""
    with pytest.raises(RuntimeError, match=env):
        Settings(host="127.0.0.1", **kwargs).validate_runtime()  # type: ignore[arg-type]


def test_a_registered_session_strategy_is_selected(restore_strategies: Any) -> None:
    from felix.session.strategies import get_session_strategy, register_session_strategy

    seen: dict[str, str] = {}

    class _Custom:
        pass

    def factory(arg: str, **budget: Any) -> Any:
        seen["arg"] = arg
        return _Custom()

    register_session_strategy("acme-recent", factory)

    assert isinstance(get_session_strategy("acme-recent"), _Custom)
    assert seen["arg"] == ""
    assert isinstance(get_session_strategy("acme-recent:7"), _Custom)
    assert seen["arg"] == "7"


def test_an_unknown_session_strategy_warns_instead_of_degrading_silently(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A typo used to buy full replay and an unbounded context, silently."""
    from felix.session.strategies import FullReplaySessionStrategy, get_session_strategy

    with caplog.at_level("WARNING", logger="felix.session.strategies"):
        strategy = get_session_strategy("windowed-20")

    assert isinstance(strategy, FullReplaySessionStrategy)
    assert "unknown session strategy" in caplog.text
    assert "windowed-20" in caplog.text


def test_full_replay_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    from felix.session.strategies import get_session_strategy

    with caplog.at_level("WARNING", logger="felix.session.strategies"):
        get_session_strategy("full_replay")
    assert "unknown session strategy" not in caplog.text


def test_audit_events_reach_a_plugin_sink(monkeypatch: pytest.MonkeyPatch, clean_audit_buffer: Any) -> None:
    """`register_audit_sink` accepted a factory that nothing ever called."""
    import felix.plugins as plugins_mod
    from felix.audit.store import record_event
    from felix.plugins import PluginRegistry

    received: list[dict[str, Any]] = []
    built = 0

    class _Sink:
        def __init__(self) -> None:
            nonlocal built
            built += 1

        def record(self, event: dict[str, Any]) -> None:
            received.append(event)

    registry = PluginRegistry()
    registry.register_audit_sink(_Sink)
    monkeypatch.setattr(plugins_mod, "_registry", registry)

    record_event(Settings(), "acme", "tool_call", manifest_id="quick", status="ok")
    record_event(Settings(), "acme", "tool_call", manifest_id="quick", status="ok")

    assert len(received) == 2
    assert received[0]["tenant_id"] == "acme"
    assert received[0]["event_type"] == "tool_call"
    # The sink is memoised: `record_event` runs per tool call and per turn, so a
    # factory that opens an HTTP client must not be invoked per event.
    assert built == 1


def test_a_raising_audit_sink_does_not_lose_the_event(
    monkeypatch: pytest.MonkeyPatch, clean_audit_buffer: Any
) -> None:
    """Postgres stays the system of record; a bad sink must not break the write."""
    import felix.plugins as plugins_mod
    from felix.audit.store import _pending, record_event
    from felix.plugins import PluginRegistry

    class _Broken:
        def record(self, event: dict[str, Any]) -> None:
            raise RuntimeError("sink down")

    registry = PluginRegistry()
    registry.register_audit_sink(_Broken)
    monkeypatch.setattr(plugins_mod, "_registry", registry)

    before = len(_pending)
    record_event(Settings(), "acme", "tool_call")
    assert len(_pending) == before + 1
