"""A plugin-registered auth mode is resolved, and cannot weaken a built-in one.

`register_authenticator` existed but nothing ever called `authenticator_builder`,
and `auth_mode` was a closed Literal, so the documented seam was doubly dead.
"""

from __future__ import annotations

from typing import Any

import pytest
from felix.auth.context import ANONYMOUS, AuthContext, Principal
from felix.auth.middleware import authenticate_request
from felix.config import Settings
from felix.plugins import PluginRegistry
from starlette.requests import Request
from starlette.responses import JSONResponse


def _request(path: str = "/chat") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
        }
    )


async def _no_token(_target: Any) -> str:
    return ""


def _sso_builder(settings: Settings) -> Any:
    _ = settings

    async def authenticate(request: Request) -> AuthContext:
        _ = request
        return AuthContext(
            principal=Principal(
                subject="sso-user",
                tenant_id="acme",
                scopes=frozenset({"admin"}),
                scheme="acme-sso",
            ),
            outbound_token=_no_token,
        )

    return authenticate


def _install(monkeypatch: pytest.MonkeyPatch, mode: str, builder: Any) -> None:
    """Register an authenticator on a registry isolated from other tests."""
    import felix.plugins as plugins_mod

    registry = PluginRegistry()
    registry.register_authenticator(mode, builder)
    monkeypatch.setattr(plugins_mod, "_registry", registry)


@pytest.mark.asyncio
async def test_plugin_auth_mode_authenticates(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, "acme-sso", _sso_builder)
    settings = Settings(auth_mode="acme-sso", host="127.0.0.1")
    settings.validate_runtime()

    result = await authenticate_request(_request(), settings)

    assert isinstance(result, AuthContext)
    assert result.principal.subject == "sso-user"
    assert result.principal.tenant_id == "acme"
    assert result.principal.scopes == frozenset({"admin"})


def test_unknown_auth_mode_is_refused_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo must not reach the request path — `auth_mode` is no longer a Literal."""
    _install(monkeypatch, "acme-sso", _sso_builder)
    with pytest.raises(RuntimeError, match="not a built-in mode"):
        Settings(auth_mode="apikey", host="127.0.0.1").validate_runtime()


@pytest.mark.asyncio
async def test_unknown_auth_mode_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """If one slips past startup validation it must 401, never fall through as anonymous."""
    _install(monkeypatch, "acme-sso", _sso_builder)
    result = await authenticate_request(_request(), Settings(auth_mode="apikey", host="127.0.0.1"))

    assert isinstance(result, JSONResponse)
    assert result.status_code == 401
    assert result is not ANONYMOUS


@pytest.mark.asyncio
async def test_a_plugin_cannot_hijack_a_builtin_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise installing a package could silently weaken `api_key` auth.

    A 401 alone would not prove this — it is also what a merely broken `api_key`
    returns. So the built-in must still *succeed* with a real key, and the plugin
    builder must never be invoked.
    """
    called: list[str] = []

    def _tracking_builder(settings: Settings) -> Any:
        called.append("builder")
        return _sso_builder(settings)

    _install(monkeypatch, "api_key", _tracking_builder)
    settings = Settings(
        auth_mode="api_key",
        auth_api_keys='{"k-real": {"tenant_id": "acme", "sub": "svc", "scopes": ["admin"]}}',
        host="127.0.0.1",
    )

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/chat",
            "headers": [(b"x-api-key", b"k-real")],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
        }
    )
    result = await authenticate_request(request, settings)

    assert isinstance(result, AuthContext), "built-in api_key must still authenticate"
    assert result.principal.scheme == "api_key"
    assert result.principal.subject == "svc"
    assert called == [], "the plugin builder must never be consulted for a built-in mode"

    # And a bad key still fails closed rather than falling through to the plugin.
    denied = await authenticate_request(_request(), settings)
    assert getattr(denied, "status_code", None) == 401
    assert called == []


@pytest.mark.asyncio
async def test_a_raising_authenticator_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _explodes(settings: Settings) -> Any:
        raise RuntimeError("misconfigured IdP")

    _install(monkeypatch, "acme-sso", _explodes)
    result = await authenticate_request(_request(), Settings(auth_mode="acme-sso", host="127.0.0.1"))

    assert isinstance(result, JSONResponse)
    assert result.status_code == 401


@pytest.mark.asyncio
async def test_a_bad_return_type_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plugin returning something else must not be coerced into a principal."""

    def _returns_junk(settings: Settings) -> Any:
        async def authenticate(request: Request) -> Any:
            return {"subject": "attacker"}

        return authenticate

    _install(monkeypatch, "acme-sso", _returns_junk)
    result = await authenticate_request(_request(), Settings(auth_mode="acme-sso", host="127.0.0.1"))

    assert isinstance(result, JSONResponse)
    assert result.status_code == 401
