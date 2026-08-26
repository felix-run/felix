"""HTTP integration — `/artifacts` is mounted, and refuses what it should.

The unit suite covers the reader; this covers the half only the app can answer:
that the router is registered at all, and that a reference which cannot become a
safe key is turned away before it reaches the store.
"""

from __future__ import annotations

import pytest
from felix.config import Settings
from httpx import ASGITransport, AsyncClient

ID = "0" * 32


@pytest.fixture
def settings() -> Settings:
    return Settings(
        allow_insecure=True,
        auth_mode="none",
        environment="development",
        object_store="memory",
        database_url="memory://artifacts",
    )


@pytest.mark.asyncio
async def test_a_spilled_output_can_be_fetched(settings: Settings) -> None:
    from felix.artifacts import artifact_key
    from felix.storage import get_object_store
    from felix_api.app import create_app

    app = create_app(settings=settings, plugins=[])
    await get_object_store(settings).put(artifact_key("default", "cowork", ID), b"the whole output")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/artifacts/cowork/{ID}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "the whole output"
    assert body["chars"] == len("the whole output")
    assert body["artifact_id"] == ID


@pytest.mark.asyncio
async def test_an_unknown_artifact_is_a_404(settings: Settings) -> None:
    from felix_api.app import create_app

    app = create_app(settings=settings, plugins=[])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/artifacts/cowork/{'1' * 32}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_a_malformed_reference_is_refused_as_absent(settings: Settings) -> None:
    # Reported as not-found rather than as malformed: which references are
    # well-formed is not a caller's business, and the distinction is a probe.
    from felix_api.app import create_app

    app = create_app(settings=settings, plugins=[])
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/artifacts/cowork/not-a-uuid")
    assert resp.status_code == 404
