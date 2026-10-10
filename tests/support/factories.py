"""Builders for the objects nearly every test makes: settings, and a client into the app.

`make_settings` pins what makes a test independent of the repo `.env` — the `memory://` stores
and no inbound auth — and takes everything else as an override, so a test that cares about a
setting still names it at the call site. It leans on `scripts/test.sh` for the rest of the
baseline: the dead Redis port, blank vendor credentials, the loopback host and no embedder.
"""

from __future__ import annotations

from typing import Any

from felix.config import Settings
from httpx import ASGITransport, AsyncClient


def make_settings(**overrides: Any) -> Settings:
    """`Settings` on the `memory://` stores, with no inbound auth.

    There is one database name because the name does nothing: every store checks only the
    `memory://` prefix and keeps one process-global twin, so `memory://a` and `memory://b`
    see the same rows. Tests are isolated by `tests/conftest.py:_isolate_process_global_stores`,
    which clears every twin around each test — not by the URL.
    """
    base: dict[str, Any] = {
        "database_url": "memory://test",
        "object_store": "memory",
        "auth_mode": "none",
        "allow_insecure": True,
        "environment": "development",
    }
    return Settings(**(base | overrides))


def app_client(settings: Settings | None = None) -> AsyncClient:
    """An HTTP client into `create_app(settings=...)` over ASGI — no socket, no lifespan.

    Use it as `async with app_client(settings) as client:`. For the production boot with a
    scripted model behind it, use the e2e `boot` fixture instead.
    """
    from felix_api.app import create_app

    app = create_app(settings=settings or make_settings(), plugins=[])
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=30.0)
