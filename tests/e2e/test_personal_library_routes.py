"""`/skill-library/~me`: a person's own library over HTTP, and an administrator's way into one.

Through the production app with real API keys. A caller writes and publishes into their own
library with no management scope; nobody else, and none of the tenant's routes, sees it; the skill
they published reaches their own turns; an administrator can list, read and archive someone's
library by its digest -- never otherwise write it -- and each look is in the audit trail; a caller
with no personal library is refused rather than handed the tenant's.
"""

from __future__ import annotations

import json
from typing import Any

from felix.manifests.loader import parse_manifest
from felix.skills.format import serialize_skill_md
from felix.skills.library_keys import library_label
from felix_ai.providers.scripted import ScriptedTurn
from felix_ai.types import ToolCall

KEYS = {
    "alice": {"tenant_id": "default", "sub": "alice", "scopes": ["skills:personal"]},
    "bob": {"tenant_id": "default", "sub": "bob", "scopes": []},
    "writer": {"tenant_id": "default", "sub": "writer", "scopes": ["skills:write"]},
    "ops": {"tenant_id": "default", "sub": "ops", "scopes": ["admin"]},
    "nosub": {"tenant_id": "default", "scopes": ["skills:write"]},
}
ENV = {
    "FELIX_AUTH_MODE": "api_key",
    "FELIX_AUTH_API_KEYS": json.dumps({f"sk-{k}-e2e-not-a-secret": v for k, v in KEYS.items()}),
}
ALICE_LIBRARY = library_label("api_key|alice")
BODY = "\n# Alice's notes\n\nUse this when Alice takes notes.\n\n## Steps\n\n1. Write it down.\n2. File it.\n"


def _h(who: str) -> dict[str, str]:
    return {"authorization": f"Bearer sk-{who}-e2e-not-a-secret"}


def _files(description: str = "Alice's own way of taking notes") -> dict[str, str]:
    return {"SKILL.md": serialize_skill_md({"name": "notes", "description": description}, BODY)}


MANIFESTS = {
    "e2e-personal": parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "e2e-personal"},
            "spec": {"personal_skills": "read"},
        }
    ),
    "e2e-personal-writer": parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "e2e-personal-writer"},
            "spec": {"personal_skills": "write", "skill_authoring": {"enabled": True}},
        }
    ),
}


async def _alice_publishes(app: Any) -> dict[str, Any]:
    resp = await app.client.post(
        "/skill-library/~me", json={"files": _files(), "publish": True}, headers=_h("alice")
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["published"] is True, resp.json()
    return resp.json()


async def test_a_caller_keeps_a_library_of_their_own_that_no_one_else_sees(boot: Any) -> None:
    async with boot(env=ENV) as app:
        saved = await _alice_publishes(app)
        mine = (await app.client.get("/skill-library/~me", headers=_h("alice"))).json()
        detail = await app.client.get("/skill-library/~me/notes", headers=_h("alice"))
        file = await app.client.get(
            f"/skill-library/~me/notes/versions/{saved['version']}/files/SKILL.md", headers=_h("alice")
        )
        bobs = (await app.client.get("/skill-library/~me", headers=_h("bob"))).json()
        bob_reads_it = await app.client.get("/skill-library/~me/notes", headers=_h("bob"))
        tenants = (await app.client.get("/skill-library", headers=_h("writer"))).json()
        review = (await app.client.get("/skill-library/-/review", headers=_h("writer"))).json()

    assert [s["name"] for s in mine["items"]] == ["notes"]
    assert detail.status_code == 200 and detail.json()["live_version"] == saved["version"]
    assert detail.json()["upstream"] is None
    assert "Alice takes notes" in file.json()["content"]
    assert bobs["items"] == [] and bob_reads_it.status_code == 404
    assert tenants["items"] == [] and review["items"] == [], "the tenant's routes see none of it"


async def test_what_a_caller_publishes_reaches_their_own_turns(boot: Any) -> None:
    async with boot([ScriptedTurn(content="ok")] * 2, env=ENV, manifests=MANIFESTS) as app:
        await _alice_publishes(app)
        for who in ("alice", "bob"):
            resp = await app.client.post(
                "/chat",
                json={"manifest": "e2e-personal", "messages": [{"role": "user", "content": "hi"}]},
                headers=_h(who),
            )
            assert resp.status_code == 200, resp.text
        alice, bob = (
            "\n".join(str(m.content) for m in prompt if m.role == "system") for prompt in app.spy.prompts
        )
    assert "Alice's own way of taking notes" in alice
    assert "Alice's own way of taking notes" not in bob


async def test_a_caller_with_no_library_of_their_own_is_refused(boot: Any) -> None:
    async with boot(env=ENV) as app:
        anonymous = await app.client.get("/skill-library/~me")
        nosub = await app.client.post("/skill-library/~me", json={"files": _files()}, headers=_h("nosub"))
        tenants = (await app.client.get("/skill-library", headers=_h("writer"))).json()
    assert anonymous.status_code == 401
    assert nosub.status_code == 403 and nosub.json()["detail"] == "no_personal_library"
    assert tenants["items"] == [], "the refused save did not land in the tenant's library"


async def test_the_tenants_own_routes_are_not_a_persons(boot: Any) -> None:
    """Review, policy and adopt are the tenant's library's alone; under `~me` they do not exist."""
    async with boot(env=ENV) as app:
        saved = await _alice_publishes(app)
        version = saved["version"]
        answers = [
            await app.client.get("/skill-library/~me/-/review", headers=_h("alice")),
            await app.client.get("/skill-library/~me/-/policy", headers=_h("alice")),
            await app.client.post(
                f"/skill-library/~me/notes/versions/{version}/adopt",
                json={"reason": "r"},
                headers=_h("alice"),
            ),
        ]
    assert all(a.status_code in (404, 405) for a in answers), [(a.status_code, a.text) for a in answers]


async def test_an_administrator_reads_and_archives_by_digest_and_is_audited(boot: Any) -> None:
    from felix.audit import store as audit_store

    async with boot(env=ENV) as app:
        saved = await _alice_publishes(app)
        listed = await app.client.get("/skill-library/-/personal", headers=_h("ops"))
        read = await app.client.get(f"/skill-library/{ALICE_LIBRARY}/notes", headers=_h("ops"))
        file = await app.client.get(
            f"/skill-library/{ALICE_LIBRARY}/notes/versions/{saved['version']}/files/SKILL.md",
            headers=_h("ops"),
        )
        write = await app.client.post(
            f"/skill-library/{ALICE_LIBRARY}", json={"files": _files()}, headers=_h("ops")
        )
        publish = await app.client.post(
            f"/skill-library/{ALICE_LIBRARY}/notes/versions/{saved['version']}/publish", headers=_h("ops")
        )
        not_admin = await app.client.get(f"/skill-library/{ALICE_LIBRARY}/notes", headers=_h("writer"))
        not_admin_listing = await app.client.get("/skill-library/-/personal", headers=_h("writer"))
        no_one = await app.client.get(f"/skill-library/~{'0' * 32}/notes", headers=_h("ops"))
        malformed = await app.client.get("/skill-library/~nope/notes", headers=_h("ops"))
        archived = await app.client.delete(f"/skill-library/{ALICE_LIBRARY}/notes", headers=_h("ops"))
        hers = (await app.client.get("/skill-library/~me/notes", headers=_h("alice"))).json()
        await audit_store.flush_pending(app.settings)
        events, _ = await audit_store.list_events(app.settings, "default", limit=200)

    assert listed.json() == {
        "items": [{"library": ALICE_LIBRARY, "owner": "api_key|alice", "skills": 1}],
        "truncated": False,
    }
    assert read.status_code == 200 and "Alice takes notes" in file.json()["content"]
    assert write.status_code == publish.status_code == 403
    assert not_admin.status_code == not_admin_listing.status_code == 403
    assert no_one.status_code == 404 and malformed.status_code in (404, 422)
    assert archived.status_code == 200 and hers["live_version"] is None, "archived in Alice's library"
    looks = [e for e in events if e["event_type"] == "personal_library_accessed"]
    assert {(e["principal_subj"], e["payload_json"]["library"]) for e in looks} >= {
        ("ops", ALICE_LIBRARY),
        ("ops", "*"),
    }
    assert len([e for e in looks if e["payload_json"]["library"] == ALICE_LIBRARY]) >= 3, (
        "read, file, archive"
    )
    assert all(e["principal_subj"] == "ops" for e in looks), "only the administrator's looks are recorded"


async def test_reading_your_library_is_yours_and_writing_it_takes_skills_personal(boot: Any) -> None:
    """A principal may be a credential many hold, so authoring instructions its turns will follow
    is an operator's grant per credential; reading what is there needs none."""
    async with boot(env=ENV) as app:
        read = await app.client.get("/skill-library/~me", headers=_h("bob"))
        write = await app.client.post("/skill-library/~me", json={"files": _files()}, headers=_h("bob"))
        after = (await app.client.get("/skill-library/~me", headers=_h("bob"))).json()
    assert read.status_code == 200 and read.json()["items"] == []
    assert write.status_code == 403 and "skills:personal" in write.json()["detail"]
    assert after["items"] == []


async def test_a_person_edits_rolls_back_and_rejects_in_their_own_library(boot: Any) -> None:
    async with boot(env=ENV) as app:
        first = await _alice_publishes(app)
        second = await app.client.put(
            "/skill-library/~me/notes/versions",
            json={
                "files": _files("Alice's notes, revised"),
                "parent_version": first["version"],
                "publish": True,
            },
            headers=_h("alice"),
        )
        third = await app.client.put(
            "/skill-library/~me/notes/versions",
            json={"files": _files("Alice's notes, a third take"), "parent_version": second.json()["version"]},
            headers=_h("alice"),
        )
        rejected = await app.client.post(
            f"/skill-library/~me/notes/versions/{third.json()['version']}/reject",
            json={"note": "not this"},
            headers=_h("alice"),
        )
        rolled = await app.client.post(
            f"/skill-library/~me/notes/versions/{first['version']}/rollback", headers=_h("alice")
        )
        detail = (await app.client.get("/skill-library/~me/notes", headers=_h("alice"))).json()
        tenants = (await app.client.get("/skill-library", headers=_h("writer"))).json()
    assert second.status_code == 201 and second.json()["published"] is True, second.text
    assert third.status_code == 201 and rejected.status_code == 200, (third.text, rejected.text)
    assert rolled.status_code == 200, rolled.text
    assert detail["live_version"] == first["version"]
    assert tenants["items"] == []


async def test_a_personal_bundle_gets_the_room_a_tenant_bundle_gets(boot: Any) -> None:
    """A bundle over the 1 MiB core body cap: the bundle routes' larger limit covers `~me` too."""
    big = {**_files(), "references/notes.md": "A line of reference text.\n" * 60_000}
    async with boot(env=ENV) as app:
        resp = await app.client.post("/skill-library/~me", json={"files": big}, headers=_h("alice"))
        version = resp.json().get("version")
        again = await app.client.put(
            "/skill-library/~me/notes/versions",
            json={"files": big, "parent_version": version},
            headers=_h("alice"),
        )
    assert len(json.dumps(big)) > 1024 * 1024
    assert resp.status_code == 201, resp.text[:300]
    assert again.status_code == 201, again.text[:300]


async def test_without_real_authentication_no_one_looks_into_a_personal_library(boot: Any) -> None:
    """`auth_mode=none` checks no scope anywhere and has no administrator to audit."""
    async with boot(env={"FELIX_AUTH_MODE": "none"}) as app:
        listing = await app.client.get("/skill-library/-/personal")
        digest = await app.client.get(f"/skill-library/{ALICE_LIBRARY}/notes")
        mine = await app.client.get("/skill-library/~me")
    assert listing.status_code == digest.status_code == mine.status_code == 403


async def test_an_agent_saves_into_its_callers_library_and_they_review_it(boot: Any) -> None:
    """`personal_skills: write`: the skill the agent writes lands in the caller's `~me` as a draft
    for them to review, and nowhere in the tenant's library; a caller without `skills:personal`
    gets a refusal and nothing saved."""
    create = ToolCall(
        id="c1",
        name="create_skill",
        args={
            "name": "notes",
            "description": "Alice's own way of taking notes",
            "body": BODY,
            "reason": "she asks for it every week",
        },
    )
    turns = [
        ScriptedTurn(content="", tool_calls=[create], stop_reason="tool_use"),
        ScriptedTurn(content="saved"),
    ] * 2
    async with boot(turns, env=ENV, manifests=MANIFESTS) as app:
        for who in ("alice", "bob"):
            resp = await app.client.post(
                "/chat",
                json={
                    "manifest": "e2e-personal-writer",
                    "messages": [{"role": "user", "content": "keep it"}],
                },
                headers=_h(who),
            )
            assert resp.status_code == 200, resp.text
        hers = (await app.client.get("/skill-library/~me/notes", headers=_h("alice"))).json()
        bobs = (await app.client.get("/skill-library/~me", headers=_h("bob"))).json()
        tenants = (await app.client.get("/skill-library", headers=_h("writer"))).json()
        results = [m.content for prompt in app.spy.prompts for m in prompt if m.role == "tool"]

    assert hers["live_version"] is None and len(hers["versions"]) == 1, hers
    assert hers["versions"][0]["status"] == "draft"
    assert bobs["items"] == [] and tenants["items"] == []
    assert any("missing_scope" in str(r) for r in results), results
