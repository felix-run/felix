"""An imported skill is checked against its origin, updated, and the update published, over HTTP.

The chain no unit test holds end to end: `GET /skill-library/{name}/-/upstream` and
`POST /{name}/-/update` on the zero-argument app → the scope gate → `skills/upstream.py` and the
importer, pinned to one commit → a new draft in the stores the API booted with → the publish gate,
stricter for an import; and `GET /-/upstream`, mounted ahead of `/{name}`.

GitHub is `tests/skill_import_fake.py`, served to the production path's client factory
(`github.github_client`) and nothing else; every other hop is the real one.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.skill_import_fake import FakeRepos, skill_md

NAME = "invoice-triage"
SOURCE = f"github:acme/skills/skills/{NAME}"
BODY = (
    "# Invoice triage\n\nUse this when an invoice arrives.\n\n## Steps\n\n"
    "1. Read the vendor and the amount.\n2. Route amounts over 500 to the finance queue.\n"
)
# Long enough for `collected_secret_values` (8+), and shaped like no credential a scanner knows.
SHARED = "plain-upstream-value-5678"
ENV = {"FELIX_SKILL_IMPORT_SOURCES": "github:acme/*", "FELIX_CONSUMER_SHARED_SECRET": SHARED}


def _skill(queues: bytes = b"# Queues\n\nfinance\n", body: str = BODY, name: str = NAME) -> dict[str, bytes]:
    return {
        f"skills/{name}/SKILL.md": skill_md(name, "Route incoming invoices.", body),
        f"skills/{name}/references/queues.md": queues,
    }


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> FakeRepos:
    fake = FakeRepos()
    fake.push("acme/skills", _skill())
    fake.serve(monkeypatch)
    return fake


async def test_a_skill_is_checked_updated_and_the_update_published_through_the_gate(
    boot: Any, gh: FakeRepos
) -> None:
    async with boot([], env=ENV) as app:
        assert (await app.client.post("/skill-library/-/import", json={"source": SOURCE})).status_code == 201
        assert (await app.client.post(f"/skill-library/{NAME}/versions/0.1.0/publish")).status_code == 200

        same = await app.client.get(f"/skill-library/{NAME}/-/upstream")
        assert same.status_code == 200, same.text
        assert same.json()["update_available"] is False and same.json()["diff"]["files"] == []

        commit = gh.push("acme/skills", _skill(queues=f"# Queues\n\nfinance, legal ({SHARED})\n".encode()))
        checked = await app.client.get(f"/skill-library/{NAME}/-/upstream")
        assert checked.status_code == 200, checked.text
        found = checked.json()
        assert (found["update_available"], found["upstream"]["commit"]) == (True, commit)
        assert found["current"]["live_version"] == "0.1.0" and found["diff"]["compared_with"] == "0.1.0"
        (queues,) = found["diff"]["files"]
        assert queues["path"] == "references/queues.md" and "+finance, legal (" in queues["diff"]
        assert SHARED not in checked.text, "upstream text is redacted as a file read is"

        detail = (await app.client.get(f"/skill-library/{NAME}")).json()
        assert (
            detail["upstream"]["upstream_commit"] == commit and detail["upstream"]["update_available"] is True
        )

        with_publish = await app.client.post(f"/skill-library/{NAME}/-/update", json={"publish": True})
        assert with_publish.status_code == 422, "an update has no publish field"
        updated = await app.client.post(f"/skill-library/{NAME}/-/update")
        assert updated.status_code == 201, updated.text
        row = updated.json()
        assert (row["version"], row["status"], row["published"], row["origin_commit"]) == (
            "0.1.1",
            "draft",
            False,
            commit,
        )
        assert row["reason"].startswith("updated from ") and row["lineage_import"] is True
        assert [f["path"] for f in row["diff"]["files"]] == ["references/queues.md"]
        assert SHARED not in updated.text
        assert (await app.client.get(f"/skill-library/{NAME}")).json()["live_version"] == "0.1.0"

        again = await app.client.post(f"/skill-library/{NAME}/-/update", json={})
        assert again.status_code == 200 and again.json()["unchanged"] is True

        published = await app.client.post(f"/skill-library/{NAME}/versions/0.1.1/publish")
        assert published.status_code == 200, published.text
        detail = (await app.client.get(f"/skill-library/{NAME}")).json()
        assert detail["live_version"] == "0.1.1" and detail["upstream"]["update_available"] is False

        # The stricter gate holds for an update as for an import: an advisory scan blocks it.
        risky = BODY + "\nThe router binary is at https://example.test/router.sh if you need it.\n"
        gh.push("acme/skills", _skill(body=risky))
        advisory = await app.client.post(f"/skill-library/{NAME}/-/update")
        assert advisory.status_code == 201 and advisory.json()["security_status"] == "advisory"
        blocked = await app.client.post(f"/skill-library/{NAME}/versions/0.1.2/publish")
        assert (blocked.status_code, blocked.json()["error"]) == (422, "publish_blocked")


async def test_a_skill_that_was_not_imported_has_no_upstream(boot: Any, gh: FakeRepos) -> None:
    from felix.skills import library

    async with boot([], env=ENV) as app:
        await library.save_draft(
            app.settings,
            "default",
            files={"SKILL.md": skill_md("house-rules", "The house rules.").decode()},
            provenance=library.DraftProvenance(source="operator", author="ops"),
        )
        for call in (
            app.client.get("/skill-library/house-rules/-/upstream"),
            app.client.post("/skill-library/house-rules/-/update"),
        ):
            resp = await call
            assert (resp.status_code, resp.json()["error"]) == (409, "not_imported"), resp.text
        assert (await app.client.get("/skill-library/no-such/-/upstream")).status_code == 404
        assert (await app.client.get("/skill-library/house-rules")).json()["upstream"] is None
        assert gh.requests == []


async def test_a_stored_origin_and_a_named_ref_pass_the_tenants_allowlist_again(
    boot: Any, gh: FakeRepos
) -> None:
    """A skill imported while the deployment allowed its source, and checked after the deployment
    stopped allowing it for this tenant: refused before GitHub is asked, a named ref or not."""
    keys = {"sk-e2e-acme": {"tenant_id": "acme", "sub": "a", "scopes": ["skills:write"]}}
    env = {
        **ENV,
        "FELIX_SKILL_IMPORT_SOURCES": "globex=github:acme/*",
        "FELIX_AUTH_MODE": "api_key",
        "FELIX_AUTH_API_KEYS": json.dumps(keys),
    }
    from felix.skills import library
    from felix.skills.library_store import ImportOrigin

    headers = {"Authorization": "Bearer sk-e2e-acme"}
    async with boot([], env=env) as app:
        await library.save_draft(
            app.settings,
            "acme",
            files={"SKILL.md": skill_md(NAME, "Route incoming invoices.", BODY).decode()},
            name=NAME,
            provenance=library.DraftProvenance(
                source="import",
                author="ops",
                origin=ImportOrigin(source=SOURCE, ref="main", commit="c" * 40, tree_hash="a" * 64),
            ),
        )
        for resp in (
            await app.client.get(f"/skill-library/{NAME}/-/upstream", params={"ref": "v2"}, headers=headers),
            await app.client.get(f"/skill-library/{NAME}/-/upstream", headers=headers),
            await app.client.post(f"/skill-library/{NAME}/-/update", json={"ref": "v2"}, headers=headers),
        ):
            assert (resp.status_code, resp.json()["error"]) == (403, "source_not_allowed"), resp.text
        listing = (await app.client.get("/skill-library/-/upstream", headers=headers)).json()
        assert [(i["name"], i["error"]) for i in listing["items"]] == [(NAME, "source_not_allowed")]
        assert gh.requests == [], "the allowlist refused every one before any GitHub call"


async def test_the_listing_is_capped_and_mounted_ahead_of_a_skill_name(boot: Any, gh: FakeRepos) -> None:
    names = ("alpha", "beta", "gamma")
    tree: dict[str, bytes] = {}
    for n in names:
        tree |= _skill(name=n)
    gh.push("acme/skills", tree)
    async with boot([], env=ENV) as app:
        for n in names:
            imported = await app.client.post(
                "/skill-library/-/import", json={"source": f"github:acme/skills/skills/{n}"}
            )
            assert imported.status_code == 201, imported.text

        first = await app.client.get("/skill-library/-/upstream", params={"limit": 2})
        assert first.status_code == 200, first.text
        assert [i["name"] for i in first.json()["items"]] == ["alpha", "beta"]
        assert first.json()["next_cursor"] == "beta" and first.json()["refreshed"] is True
        rest = (await app.client.get("/skill-library/-/upstream", params={"cursor": "beta"})).json()
        assert [i["name"] for i in rest["items"]] == ["gamma"] and rest["next_cursor"] is None

        over = await app.client.get("/skill-library/-/upstream", params={"limit": 26})
        assert over.status_code == 422, "at most 25 a page"
        before = len(gh.requests)
        cached = await app.client.get("/skill-library/-/upstream", params={"refresh": "false"})
        assert [i["name"] for i in cached.json()["items"]] == list(names) and len(gh.requests) == before


async def test_a_listing_with_no_budget_left_is_refused(boot: Any, gh: FakeRepos) -> None:
    """An import of this two-file skill is seven calls, which is the whole budget of seven."""
    async with boot([], env={**ENV, "FELIX_SKILL_IMPORT_CALLS_PER_HOUR": "7"}) as app:
        assert (await app.client.post("/skill-library/-/import", json={"source": SOURCE})).status_code == 201
        refused = await app.client.get("/skill-library/-/upstream")
        assert (refused.status_code, refused.json()["error"]) == (429, "rate_limited")
        checked = await app.client.get(f"/skill-library/{NAME}/-/upstream")
        assert (checked.status_code, checked.json()["error"]) == (429, "rate_limited")
