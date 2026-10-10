"""What a caller is told when a route fails: the message written for them, or a fixed one — never
an exception's own text.

`tests/unit/test_route_error_text.py` holds the source to the rule; these hold the behaviour, over
real HTTP through `create_application()`. Each failure site gets two cases where it matters: the
refusal written for the caller still arrives word for word, and an exception from deeper down —
one the route used to relay because it caught a bare `ValueError` or `LookupError` — does not.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from tests.support.mgmt_keys import ADMIN, bearer, scoped_keys

LEAK = "postgresql://felix:hunter2@db.internal/felix"


def _as_a_real_client(app: Any) -> Any:
    """The same app, answering an unhandled exception the way a server does — with a 500 —
    rather than re-raising it into the test as the default ASGI transport does."""
    import httpx

    transport = httpx.ASGITransport(app=app.client._transport.app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://felix.test")


def _manifest(name: str) -> dict[str, Any]:
    return {
        "manifest": {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": name},
            "spec": {"system_prompt": {"inline": "hi"}},
        }
    }


async def test_an_unknown_canary_version_is_named_and_a_deeper_lookup_error_is_not(
    boot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from felix.manifests import store as manifest_store

    async with boot([], env=scoped_keys(reader=[])) as app:
        put = await app.client.put(
            "/manifests/e2e-canary", json=_manifest("e2e-canary"), headers=bearer(ADMIN)
        )
        assert put.status_code == 200, put.text
        unknown = await app.client.post(
            "/manifests/e2e-canary/canary",
            json={"canary_version": 99, "canary_weight": 10},
            headers=bearer(ADMIN),
        )
        assert unknown.status_code == 400
        assert unknown.json()["detail"] == "Unknown canary version: e2e-canary@99"

        async def broken(*args: Any, **kwargs: Any) -> Any:
            raise KeyError(LEAK)

        monkeypatch.setattr(manifest_store, "set_canary", broken)
        async with _as_a_real_client(app) as client:
            deeper = await client.post(
                "/manifests/e2e-canary/canary",
                json={"canary_version": 1, "canary_weight": 10},
                headers=bearer(ADMIN),
            )
        assert deeper.status_code == 500
        assert "hunter2" not in deeper.text


async def test_the_chunk_ceiling_is_named_and_a_stray_value_error_is_not(
    boot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from felix.documents import store as doc_store

    body = {"title": "t", "source": "s", "text": "words"}
    async with boot([], env=scoped_keys(reader=[])) as app:

        async def too_large(*args: Any, **kwargs: Any) -> Any:
            raise doc_store.DocumentTooLarge("document splits into 9000 chunks; the ceiling is 2000")

        monkeypatch.setattr(doc_store, "put_document", too_large)
        refused = await app.client.post("/documents", json=body, headers=bearer(ADMIN))
        assert refused.status_code == 400
        assert refused.json()["detail"] == "document splits into 9000 chunks; the ceiling is 2000"

        async def stray(*args: Any, **kwargs: Any) -> Any:
            raise ValueError(LEAK)

        monkeypatch.setattr(doc_store, "put_document", stray)
        async with _as_a_real_client(app) as client:
            deeper = await client.post("/documents", json=body, headers=bearer(ADMIN))
        assert deeper.status_code == 500
        assert "hunter2" not in deeper.text


async def test_missing_file_storage_is_reported_without_naming_the_setting(
    boot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from felix import attachments

    async def unconfigured(*args: Any, **kwargs: Any) -> Any:
        raise attachments.AttachmentError("no object store is configured; set FELIX_OBJECT_STORE")

    monkeypatch.setattr(attachments, "put_attachment", unconfigured)
    png = base64.b64encode(bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 64).decode()
    async with boot([], env=scoped_keys(reader=[])) as app:
        answer = await app.client.post(
            "/files", json={"data": png, "media_type": "image/png"}, headers=bearer(ADMIN)
        )
        assert answer.status_code == 503
        assert answer.json()["detail"] == "file storage is not available on this server"
        assert "FELIX_OBJECT_STORE" not in answer.text


async def test_a_bad_upload_still_says_what_is_wrong_with_it(boot: Any) -> None:
    async with boot([], env=scoped_keys(reader=[])) as app:
        answer = await app.client.post(
            "/files", json={"data": "not base64!", "media_type": "image/png"}, headers=bearer(ADMIN)
        )
        assert answer.status_code == 400
        assert answer.json()["detail"] == "data is not valid base64"


async def test_a_bad_response_format_still_says_what_is_wrong_with_it(boot: Any) -> None:
    async with boot([], env=scoped_keys(reader=[])) as app:
        answer = await app.client.post(
            "/v1/chat/completions",
            json={
                "model": "quick",
                "messages": [{"role": "user", "content": "hi"}],
                "response_format": {"type": "json_schema", "json_schema": {"name": "x"}},
            },
            headers=bearer(ADMIN),
        )
        assert answer.status_code == 400, answer.text
        assert answer.json()["error"]["code"] == "invalid_response_format"
        assert (
            answer.json()["error"]["message"]
            == 'response_format.json_schema must be an object with a "schema" key'
        )
