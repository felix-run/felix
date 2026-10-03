"""`/skill-library` feedback, evaluation and policy routes, through `create_app` under `api_key`.

As `test_skill_library_routes.py`: the scope gate and where the tenant comes from are what a direct
call skips, and each key holds one scope, so a 2xx is evidence about that scope.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from felix.config import Settings
from httpx import ASGITransport, AsyncClient

from tests.skill_quality import NAME, published

KEYS = json.dumps(
    {
        "sk-read": {"tenant_id": "acme", "sub": "reader", "scopes": ["skills:read"]},
        "sk-write": {"tenant_id": "acme", "sub": "editor", "scopes": ["skills:write"]},
        "sk-globex": {"tenant_id": "globex", "sub": "other", "scopes": ["skills:write"]},
    }
)
READ, WRITE, GLOBEX = "sk-read", "sk-write", "sk-globex"
SHARED_SECRET = "plain-shared-value-1234"
MISSING = "00000000-0000-4000-8000-000000000000"


def _h(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


class App:
    def __init__(self, client: AsyncClient, settings: Settings) -> None:
        self.client, self.settings = client, settings

    async def feedback(self, body: str = "Say what the limit is.", key: str = WRITE, **kw: Any) -> Any:
        return await self.client.post(
            f"/skill-library/{NAME}/feedback", json={"body": body, **kw}, headers=_h(key)
        )


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[App]:
    from felix_api.app import create_app

    settings = Settings(
        allow_insecure=True,
        auth_mode="api_key",
        auth_api_keys=KEYS,
        environment="development",
        object_store="memory",
        database_url="memory://skill-quality-routes",
        data_dir=str(tmp_path),
        consumer_shared_secret=SHARED_SECRET,
    )
    transport = ASGITransport(app=create_app(settings=settings, plugins=[]))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield App(client, settings)


# -- feedback --------------------------------------------------------------------------------


async def test_feedback_is_filed_by_a_writer_and_redacted_for_a_reader(app: App) -> None:
    version = await published(app.settings)

    assert (await app.feedback(key=READ)).status_code == 403
    filed = await app.feedback(body=f"The key {SHARED_SECRET} is in step 2.")
    assert filed.status_code == 201, filed.text
    row = filed.json()
    assert (row["source"], row["author"], row["principal"], row["status"]) == (
        "human",
        "editor",
        "editor",
        "pending",
    )
    assert row["target_version"] == version and SHARED_SECRET not in filed.text

    listed = await app.client.get(f"/skill-library/{NAME}/feedback", headers=_h(READ))
    assert listed.status_code == 200, listed.text
    assert [f["id"] for f in listed.json()["items"]] == [row["id"]] and SHARED_SECRET not in listed.text
    assert (await app.client.get("/skill-library/nope/feedback", headers=_h(READ))).status_code == 404
    assert (await app.feedback(target_version="9.9.9")).status_code == 404


async def test_the_inbox_is_oldest_first_and_pages(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    from felix.skills import feedback

    await published(app.settings)
    clock = iter(range(1000, 2000))
    monkeypatch.setattr(feedback, "now_ms", lambda: next(clock))
    ids = [(await app.feedback(body=f"note {n}")).json()["id"] for n in range(3)]

    first = (await app.client.get("/skill-library/-/feedback?limit=2", headers=_h(READ))).json()
    assert [f["id"] for f in first["items"]] == ids[:2] and first["next_cursor"]
    rest = await app.client.get(f"/skill-library/-/feedback?cursor={first['next_cursor']}", headers=_h(READ))
    assert [f["id"] for f in rest.json()["items"]] == ids[2:] and rest.json()["next_cursor"] is None
    bad = await app.client.get("/skill-library/-/feedback?cursor=nope", headers=_h(READ))
    assert bad.status_code == 422 and bad.json()["error"] == "invalid_cursor"


async def test_accept_queues_an_improvement_and_reject_records_why(app: App) -> None:
    await published(app.settings)
    one, two, three = [(await app.feedback(body=f"note {n}")).json()["id"] for n in range(3)]

    accepted = await app.client.post(f"/skill-library/-/feedback/{one}/accept", headers=_h(WRITE))
    assert accepted.status_code == 200, accepted.text
    assert (accepted.json()["status"], accepted.json()["improve"], accepted.json()["decided_by"]) == (
        "accepted",
        True,
        "editor",
    )
    kept = await app.client.post(
        f"/skill-library/-/feedback/{two}/accept", json={"improve": False, "note": "noted"}, headers=_h(WRITE)
    )
    assert (kept.json()["improve"], kept.json()["decision_note"]) == (False, "noted")
    rejected = await app.client.post(
        f"/skill-library/-/feedback/{three}/reject", json={"note": "out of scope"}, headers=_h(WRITE)
    )
    assert (rejected.json()["status"], rejected.json()["decision_note"]) == ("rejected", "out of scope")

    again = await app.client.post(
        f"/skill-library/-/feedback/{one}/reject", json={"note": "x"}, headers=_h(WRITE)
    )
    assert again.status_code == 409 and again.json()["error"] == "feedback_conflict"
    pending = await app.client.get("/skill-library/-/feedback", headers=_h(READ))
    assert pending.json()["items"] == []
    accepted_list = await app.client.get("/skill-library/-/feedback?status=accepted", headers=_h(READ))
    assert {f["id"] for f in accepted_list.json()["items"]} == {one, two}


async def test_deciding_feedback_needs_write_and_the_callers_tenant(app: App) -> None:
    await published(app.settings)
    fid = (await app.feedback()).json()["id"]

    assert (
        await app.client.post(f"/skill-library/-/feedback/{fid}/accept", headers=_h(READ))
    ).status_code == 403
    other = await app.client.post(f"/skill-library/-/feedback/{fid}/accept", headers=_h(GLOBEX))
    assert other.status_code == 404, other.text
    for bad in (MISSING, "not-a-uuid"):
        assert (
            await app.client.post(f"/skill-library/-/feedback/{bad}/accept", headers=_h(WRITE))
        ).status_code == 404


# -- evaluations -----------------------------------------------------------------------------


async def test_an_evaluation_is_queued_once_per_version(app: App) -> None:
    version = await published(app.settings)
    url = f"/skill-library/{NAME}/versions/{version}/eval"

    assert (await app.client.post(url, headers=_h(READ))).status_code == 403
    queued = await app.client.post(url, headers=_h(WRITE))
    assert queued.status_code == 202, queued.text
    row = queued.json()
    assert (row["status"], row["version"], row["requested_by"], row["uplift"]) == (
        "queued",
        version,
        "editor",
        None,
    )
    duplicate = await app.client.post(url, headers=_h(WRITE))
    assert duplicate.status_code == 409 and duplicate.json()["error"] == "eval_in_progress"
    missing = await app.client.post(f"/skill-library/{NAME}/versions/9.9.9/eval", headers=_h(WRITE))
    assert missing.status_code == 404

    listed = await app.client.get(f"/skill-library/{NAME}/evals?version={version}", headers=_h(READ))
    assert [e["id"] for e in listed.json()["items"]] == [row["id"]]
    one = await app.client.get(f"/skill-library/{NAME}/evals/{row['id']}", headers=_h(READ))
    assert one.status_code == 200 and one.json()["id"] == row["id"]
    assert (
        await app.client.get(f"/skill-library/other-skill/evals/{row['id']}", headers=_h(READ))
    ).status_code == 404
    assert (
        await app.client.get(f"/skill-library/{NAME}/evals/{row['id']}", headers=_h(GLOBEX))
    ).status_code == 404


# -- policy ----------------------------------------------------------------------------------


async def test_patching_the_policy_makes_it_the_tenants(app: App) -> None:
    assert (
        await app.client.patch("/skill-library/-/policy", json={"require_eval": True}, headers=_h(READ))
    ).status_code == 403
    for bad in ({"min_quality": 101}, {"min_eval_uplift": -101}, {"unknown": 1}):
        assert (
            await app.client.patch("/skill-library/-/policy", json=bad, headers=_h(WRITE))
        ).status_code == 422

    patched = await app.client.patch(
        "/skill-library/-/policy", json={"require_eval": True, "min_eval_uplift": 5}, headers=_h(WRITE)
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert (body["source"], body["require_eval"], body["min_eval_uplift"], body["updated_by"]) == (
        "tenant",
        True,
        5,
        "editor",
    )
    assert (await app.client.get("/skill-library/-/policy", headers=_h(READ))).json() == body
    other = (await app.client.get("/skill-library/-/policy", headers=_h(GLOBEX))).json()
    assert other["source"] == "settings" and other["require_eval"] is False

    cleared = await app.client.patch(
        "/skill-library/-/policy", json={"min_eval_uplift": None}, headers=_h(WRITE)
    )
    assert (cleared.json()["min_eval_uplift"], cleared.json()["require_eval"]) == (None, True)


async def test_the_preview_reports_the_evaluation_block(app: App) -> None:
    version = await published(app.settings)
    await app.client.patch("/skill-library/-/policy", json={"require_eval": True}, headers=_h(WRITE))

    preview = (
        await app.client.get(f"/skill-library/{NAME}/versions/{version}/preview", headers=_h(READ))
    ).json()

    assert preview["policy_passes"] is False
    assert any("succeeded evaluation" in r for r in preview["reasons"]), preview["reasons"]
