"""`/skill-library`: an operator's view of the tenant skill library, and the only way to change it.

Through `create_app` under `auth_mode=api_key`, because the scope gate and where the tenant comes
from are exactly what a direct call to the handler skips -- and `require_mgmt_scopes` checks
nothing at all under `auth_mode=none`, so a test there says nothing about who may reach a route.
Each key holds exactly one scope, so a 200 is evidence about that scope rather than an admin bypass.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from felix.config import Settings
from felix.skills import library
from felix.skills.format import serialize_skill_md
from felix.skills.library_keys import ORG_OWNER, library_object_key
from felix.skills.library_store import get_skill_library_store
from httpx import ASGITransport, AsyncClient

KEYS = json.dumps(
    {
        "sk-read": {"tenant_id": "acme", "sub": "reader", "scopes": ["skills:read"]},
        "sk-write": {"tenant_id": "acme", "sub": "editor", "scopes": ["skills:write"]},
        "sk-none": {"tenant_id": "acme", "sub": "nobody", "scopes": ["chat:write"]},
        "sk-globex": {"tenant_id": "globex", "sub": "other", "scopes": ["skills:write"]},
    }
)
READ, WRITE, NONE, GLOBEX = ("sk-read", "sk-write", "sk-none", "sk-globex")
NAME = "invoice-triage"
BODY = """# Invoice triage

Use this when an invoice arrives.

## Steps

1. Read the vendor and the amount.
2. Route amounts over the limit to finance.
"""
# Long enough for `collected_secret_values` (8+), and shaped like no credential the scan knows.
SHARED_SECRET = "plain-shared-value-1234"


def _h(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _files(name: str = NAME, body: str = BODY, **extra: str) -> dict[str, str]:
    return {
        "SKILL.md": serialize_skill_md({"name": name, "description": "Route invoices."}, f"\n{body}"),
        **extra,
    }


class App:
    def __init__(self, client: AsyncClient, settings: Settings) -> None:
        self.client, self.settings = client, settings

    @property
    def store(self) -> Any:
        from felix.storage import get_object_store

        return get_object_store(self.settings)

    async def create(self, key: str = WRITE, **body: Any) -> Any:
        return await self.client.post("/skill-library", json={"files": _files(), **body}, headers=_h(key))


def _settings(tmp_path: Path, **kw: Any) -> Settings:
    base: dict[str, Any] = {
        "allow_insecure": True,
        "auth_mode": "api_key",
        "auth_api_keys": KEYS,
        "environment": "development",
        "object_store": "memory",
        "database_url": "memory://skill-library",
        # Part of the object store's cache key, so each test gets a store of its own.
        "data_dir": str(tmp_path),
        "consumer_shared_secret": SHARED_SECRET,
    }
    return Settings(**{**base, **kw})


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[App]:
    from felix_api.app import create_app

    settings = _settings(tmp_path)
    transport = ASGITransport(app=create_app(settings=settings, plugins=[]))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield App(client, settings)


async def _agent_draft(app: App, name: str = NAME, body: str = BODY) -> dict[str, Any]:
    return await library.save_draft(
        app.settings,
        "acme",
        files=_files(name, body),
        provenance=library.DraftProvenance(
            source="agent", author="contributor", origin_manifest_id="contributor"
        ),
        object_store=app.store,
        owner=ORG_OWNER,
    )


# -- scopes and tenancy ----------------------------------------------------------------------


WRITES = [
    ("post", "/skill-library", {"files": _files()}),
    ("put", f"/skill-library/{NAME}/versions", {"files": _files(), "parent_version": "0.1.0"}),
    ("post", f"/skill-library/{NAME}/versions/0.1.0/publish", None),
    ("post", f"/skill-library/{NAME}/versions/0.1.0/rollback", None),
    ("post", f"/skill-library/{NAME}/versions/0.1.0/reject", {"note": "no"}),
    ("post", f"/skill-library/{NAME}/versions/0.1.0/adopt", {"reason": "vetted"}),
    ("delete", f"/skill-library/{NAME}", None),
]


@pytest.mark.parametrize(("method", "path", "body"), WRITES, ids=[f"{m} {p}" for m, p, _ in WRITES])
async def test_every_write_refuses_a_read_only_key(app: App, method: str, path: str, body: Any) -> None:
    await _agent_draft(app)
    kwargs: dict[str, Any] = {"headers": _h(READ)}
    if body is not None:
        kwargs["json"] = body
    resp = await app.client.request(method.upper(), path, **kwargs)
    assert resp.status_code == 403, resp.text
    assert "skills:write" in resp.text
    row = await get_skill_library_store(app.settings, owner=ORG_OWNER).get_version("acme", NAME, "0.1.0")
    assert row is not None and row["status"] == "draft", "a refused write changed the library"


READS = [
    "/skill-library",
    "/skill-library/-/review",
    "/skill-library/-/policy",
    f"/skill-library/{NAME}",
    f"/skill-library/{NAME}/versions/0.1.0",
    f"/skill-library/{NAME}/versions/0.1.0/files/SKILL.md",
    f"/skill-library/{NAME}/versions/0.1.0/preview",
]


@pytest.mark.parametrize("path", READS)
async def test_reads_need_skills_read_and_skills_write_implies_it(app: App, path: str) -> None:
    await _agent_draft(app)
    assert (await app.client.get(path, headers=_h(NONE))).status_code == 403
    assert (await app.client.get(path, headers=_h(READ))).status_code == 200
    assert (await app.client.get(path, headers=_h(WRITE))).status_code == 200


async def test_auth_mode_none_checks_no_scope(tmp_path: Path) -> None:
    from felix_api.app import create_app

    settings = _settings(tmp_path, auth_mode="none")
    transport = ASGITransport(app=create_app(settings=settings, plugins=[]))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/skill-library", json={"files": _files()})
    assert resp.status_code == 201, resp.text
    assert resp.json()["author"] == "anonymous"


async def test_another_tenant_can_neither_see_nor_change_the_library(app: App) -> None:
    created = await app.create()
    assert created.status_code == 201, created.text

    listing = await app.client.get("/skill-library", headers=_h(GLOBEX))
    assert listing.status_code == 200 and listing.json()["items"] == []
    for path in READS[3:]:
        assert (await app.client.get(path, headers=_h(GLOBEX))).status_code == 404, path
    for method, path, body in WRITES[1:]:
        kwargs: dict[str, Any] = {"headers": _h(GLOBEX)}
        if body is not None:
            kwargs["json"] = body
        resp = await app.client.request(method.upper(), path, **kwargs)
        assert resp.status_code == 404, (path, resp.text)
    assert (await app.client.get("/skill-library/-/review", headers=_h(GLOBEX))).json()["items"] == []

    row = await get_skill_library_store(app.settings, owner=ORG_OWNER).get_version("acme", NAME, "0.1.0")
    assert row is not None and row["status"] == "draft"


# -- the operator flow -----------------------------------------------------------------------


async def test_an_operator_creates_reviews_and_publishes_a_skill(app: App) -> None:
    from felix.audit import store as audit_store

    created = await app.create(reason="we triage by hand")
    assert created.status_code == 201, created.text
    body = created.json()
    assert (body["name"], body["version"], body["status"]) == (NAME, "0.1.0", "draft")
    assert (body["source"], body["author"], body["published"]) == ("operator", "editor", False)
    assert [f["path"] for f in body["files"]] == ["SKILL.md"]
    assert body["shadows_operator_upload"] is False

    listing = (await app.client.get("/skill-library", headers=_h(READ))).json()
    (item,) = listing["items"]
    assert (item["live_version"], item["pending_drafts"], item["latest"]["version"]) == (None, 1, "0.1.0")
    assert item["latest"]["source"] == "operator" and listing["next_cursor"] is None

    detail = (await app.client.get(f"/skill-library/{NAME}", headers=_h(READ))).json()
    assert [v["version"] for v in detail["versions"]] == ["0.1.0"]
    version = (await app.client.get(f"/skill-library/{NAME}/versions/0.1.0", headers=_h(READ))).json()
    assert version["review_checks"] and version["security_status"] == "pass"
    assert version["reason"] == "we triage by hand"

    published = await app.client.post(f"/skill-library/{NAME}/versions/0.1.0/publish", headers=_h(WRITE))
    assert published.status_code == 200, published.text
    assert (published.json()["status"], published.json()["decided_by"]) == ("published", "editor")
    item = (await app.client.get("/skill-library", headers=_h(READ))).json()["items"][0]
    assert (item["live_version"], item["pending_drafts"]) == ("0.1.0", 0)

    await audit_store.flush_pending(app.settings)
    events, _ = await audit_store.list_events(app.settings, "acme", limit=50)
    by_type = {e["event_type"]: e for e in events}
    assert by_type["skill_draft_saved"]["principal_subj"] == "editor"
    assert by_type["skill_published"]["principal_subj"] == "editor"


async def test_create_can_publish_in_the_same_request(app: App) -> None:
    resp = await app.create(publish=True)
    assert resp.status_code == 201, resp.text
    assert (resp.json()["published"], resp.json()["status"]) == (True, "published")
    skill = await get_skill_library_store(app.settings, owner=ORG_OWNER).get_skill("acme", NAME)
    assert skill is not None and skill["live_version"] == "0.1.0"


async def test_a_blocked_publish_in_the_same_request_keeps_the_draft_and_says_why(app: App) -> None:
    resp = await app.create(files=_files(**{"scripts/clean.sh": "rm -rf /tmp/work\n"}), publish=True)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["published"] is False and body["status"] == "draft"
    blocked = body["publish_blocked"]
    assert blocked["error"] == "publish_blocked" and "security scan failed" in blocked["reasons"][0]


async def test_a_new_version_needs_the_newest_version_as_its_parent(app: App) -> None:
    await app.create()
    put = f"/skill-library/{NAME}/versions"
    edited = {
        "files": _files(body=BODY + "\n3. Then file it.\n"),
        "parent_version": "0.1.0",
        "reason": "more",
    }

    first = await app.client.put(put, json=edited, headers=_h(WRITE))
    assert first.status_code == 201, first.text
    assert (first.json()["version"], first.json()["parent_version"]) == ("0.1.1", "0.1.0")

    # A second editor who loaded 0.1.0 too: refused, not silently saved past 0.1.1.
    stale = await app.client.put(put, json=edited, headers=_h(WRITE))
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"] == "parent_changed"
    assert await get_skill_library_store(app.settings, owner=ORG_OWNER).version_ids("acme", NAME) == [
        "0.1.0",
        "0.1.1",
    ]

    minor = await app.client.put(
        put, json={**edited, "parent_version": "0.1.1", "bump": "minor"}, headers=_h(WRITE)
    )
    assert minor.status_code == 201 and minor.json()["version"] == "0.2.0"
    explicit = await app.client.put(
        put, json={**edited, "parent_version": "0.2.0", "version": "1.0.0"}, headers=_h(WRITE)
    )
    assert explicit.status_code == 201 and explicit.json()["version"] == "1.0.0"
    both = await app.client.put(
        put,
        json={**edited, "parent_version": "1.0.0", "version": "2.0.0", "bump": "major"},
        headers=_h(WRITE),
    )
    assert both.status_code == 422


async def test_reject_and_archive(app: App) -> None:
    await app.create(publish=True)
    await app.client.put(
        f"/skill-library/{NAME}/versions",
        json={"files": _files(body=BODY + "\nMore.\n"), "parent_version": "0.1.0"},
        headers=_h(WRITE),
    )
    rejected = await app.client.post(
        f"/skill-library/{NAME}/versions/0.1.1/reject", json={"note": "too vague"}, headers=_h(WRITE)
    )
    assert rejected.status_code == 200, rejected.text
    assert (rejected.json()["status"], rejected.json()["decision_note"]) == ("archived", "too vague")
    assert (await app.client.get("/skill-library/-/review", headers=_h(READ))).json()["items"] == []

    archived = await app.client.delete(f"/skill-library/{NAME}", headers=_h(WRITE))
    assert archived.status_code == 200 and archived.json() == {"name": NAME, "live_version": None}
    rolled = await app.client.post(f"/skill-library/{NAME}/versions/0.1.0/rollback", headers=_h(WRITE))
    assert rolled.status_code == 200 and rolled.json()["status"] == "published"


# -- refusals --------------------------------------------------------------------------------


async def test_every_library_error_code_has_a_status() -> None:
    from felix_api.routes._skill_library_http import STATUS as _STATUS

    def codes(cls: type) -> set[str]:
        return {cls.code} | {c for sub in cls.__subclasses__() for c in codes(sub)}

    assert codes(library.SkillLibraryError) - {"skill_library_error"} <= set(_STATUS)


async def test_an_unmapped_refusal_is_a_server_error() -> None:
    from felix_api.routes._skill_library_http import refusal as _refusal

    class Novel(library.SkillLibraryError):
        code = "something_new"

    assert _refusal(Novel("x")).status_code == 500


@pytest.mark.parametrize(
    ("files", "status", "error"),
    [
        ({"SKILL.md": "no frontmatter"}, 422, "invalid_bundle"),
        (_files("calculator-help"), 409, "name_shadows_host_skill"),
    ],
    ids=["invalid", "host-name"],
)
async def test_a_refused_create_names_its_reason(app: App, files: Any, status: int, error: str) -> None:
    resp = await app.create(files=files)
    assert resp.status_code == status, resp.text
    assert resp.json()["error"] == error
    if error == "invalid_bundle":
        assert resp.json()["issues"][0]["path"] == "SKILL.md"
    assert await get_skill_library_store(app.settings, owner=ORG_OWNER).list_skills("acme") == []


async def test_create_refuses_a_name_already_in_the_library(app: App) -> None:
    await app.create()
    again = await app.create()
    assert again.status_code == 409 and again.json()["error"] == "skill_exists"


async def test_state_conflicts_are_409_and_missing_is_404(app: App) -> None:
    await app.create(publish=True)
    republish = await app.client.post(f"/skill-library/{NAME}/versions/0.1.0/publish", headers=_h(WRITE))
    assert republish.status_code == 409 and republish.json()["error"] == "version_conflict"
    missing = await app.client.post(f"/skill-library/{NAME}/versions/9.9.9/publish", headers=_h(WRITE))
    assert missing.status_code == 404 and missing.json()["error"] == "not_found"
    bad = await app.client.get("/skill-library/Not_A_Name", headers=_h(READ))
    assert bad.status_code == 404
    assert (await app.client.delete("/skill-library/nothing-here", headers=_h(WRITE))).status_code == 404


async def test_a_blocked_publish_is_422_with_the_gates_reasons(app: App) -> None:
    await app.create(files=_files(**{"scripts/clean.sh": "rm -rf /tmp/work\n"}))
    resp = await app.client.post(f"/skill-library/{NAME}/versions/0.1.0/publish", headers=_h(WRITE))
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"] == "publish_blocked" and resp.json()["reasons"]


# -- reading files and previews --------------------------------------------------------------


async def test_a_file_is_digest_checked_redacted_and_confined_to_the_bundle(app: App) -> None:
    files = _files(
        body=BODY + f"\nCall it with {SHARED_SECRET}.\n",
        **{"references/notes.md": "Notes.\n", "assets/logo.png": "iVBORw0KGgo="},
    )
    await app.create(files=files)
    base = f"/skill-library/{NAME}/versions/0.1.0/files"

    skill_md = await app.client.get(f"{base}/SKILL.md", headers=_h(READ))
    assert skill_md.status_code == 200, skill_md.text
    assert SHARED_SECRET not in skill_md.text and "Call it with" in skill_md.json()["content"]
    assert skill_md.json()["encoding"] == "utf-8"
    asset = (await app.client.get(f"{base}/assets/logo.png", headers=_h(READ))).json()
    assert (asset["encoding"], asset["content_type"], asset["content"]) == (
        "base64",
        "image/png",
        "iVBORw0KGgo=",
    )

    # `%2E%2E` so the client sends the traversal rather than normalising it away; the server decodes it.
    for path in (
        "secrets/x.md",
        "references/%2E%2E/SKILL.md",
        "references/%2E%2E/%2E%2E/x.md",
        "plugin.json/x",
    ):
        resp = await app.client.get(f"{base}/{path}", headers=_h(READ))
        assert resp.status_code == 422 and resp.json()["error"] == "invalid_path", (path, resp.text)
    assert (await app.client.get(f"{base}/references/missing.md", headers=_h(READ))).status_code == 404

    await app.store.put(
        library_object_key("acme", NAME, "0.1.0", "references/notes.md", owner=ORG_OWNER), b"Tampered.\n"
    )
    corrupt = await app.client.get(f"{base}/references/notes.md", headers=_h(READ))
    assert corrupt.status_code == 500 and corrupt.json()["error"] == "version_corrupt"
    assert "Tampered" not in corrupt.text


async def test_preview_reruns_the_gate_and_changes_nothing(app: App) -> None:
    from felix.audit import store as audit_store

    await app.create(files=_files(**{"scripts/clean.sh": "rm -rf /tmp/work\n"}))
    lib = get_skill_library_store(app.settings, owner=ORG_OWNER)
    before = await lib.get_version("acme", NAME, "0.1.0")
    await audit_store.flush_pending(app.settings)
    events_before, _ = await audit_store.list_events(app.settings, "acme", limit=50)

    resp = await app.client.get(f"/skill-library/{NAME}/versions/0.1.0/preview", headers=_h(READ))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["valid"], body["policy_passes"], body["security_status"]) == (True, False, "fail")
    assert any(i["path"] == "scripts/clean.sh" for i in body["security_issues"])

    assert await lib.get_version("acme", NAME, "0.1.0") == before
    await audit_store.flush_pending(app.settings)
    events_after, _ = await audit_store.list_events(app.settings, "acme", limit=50)
    assert len(events_after) == len(events_before)


async def test_the_policy_route_reports_the_settings(tmp_path: Path) -> None:
    from felix_api.app import create_app

    settings = _settings(tmp_path, skill_publish_min_quality=40, skill_publish_block_on_advisory=True)
    transport = ASGITransport(app=create_app(settings=settings, plugins=[]))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/skill-library/-/policy", headers=_h(READ))
    assert resp.json() == {
        "min_quality": 40,
        "block_on_advisory": True,
        "security_fail_blocks": True,
        "require_eval": False,
        "min_eval_uplift": None,
        "import_min_age_days": 0,
        "source": "settings",
        "tenant_values": None,
        "updated_at": None,
        "updated_by": None,
    }


# -- listing and the review queue ------------------------------------------------------------


async def test_the_review_queue_is_oldest_first_across_skills_and_pages(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticks = iter(range(100, 200))
    monkeypatch.setattr(library, "now_ms", lambda: next(ticks))
    for name in ("zeta-skill", "alpha-skill", "mid-skill"):
        await _agent_draft(app, name)
    await app.client.post("/skill-library/alpha-skill/versions/0.1.0/publish", headers=_h(WRITE))
    await _agent_draft(app, "alpha-skill", BODY + "\nNewer.\n")

    first = (await app.client.get("/skill-library/-/review?limit=2", headers=_h(READ))).json()
    assert [(i["name"], i["version"]) for i in first["items"]] == [
        ("zeta-skill", "0.1.0"),
        ("mid-skill", "0.1.0"),
    ]
    rest = (
        await app.client.get(f"/skill-library/-/review?cursor={first['next_cursor']}", headers=_h(READ))
    ).json()
    (newest,) = rest["items"]
    assert (newest["name"], newest["version"], newest["live_version"]) == ("alpha-skill", "0.1.1", "0.1.0")
    assert newest["source"] == "agent" and rest["next_cursor"] is None


async def test_the_listing_filters_and_pages_by_name(app: App) -> None:
    await _agent_draft(app, "agent-draft")
    await app.client.post(
        "/skill-library", json={"files": _files("live-one"), "publish": True}, headers=_h(WRITE)
    )
    await app.client.post(
        "/skill-library", json={"files": _files("gone-one"), "publish": True}, headers=_h(WRITE)
    )
    await app.client.delete("/skill-library/gone-one", headers=_h(WRITE))

    async def names(query: str) -> list[str]:
        resp = await app.client.get(f"/skill-library?{query}", headers=_h(READ))
        assert resp.status_code == 200, resp.text
        return [i["name"] for i in resp.json()["items"]]

    assert await names("") == ["agent-draft", "gone-one", "live-one"]
    assert await names("status=live") == ["live-one"]
    assert await names("status=draft") == ["agent-draft"]
    assert await names("status=archived") == ["gone-one"]
    assert await names("source=agent") == ["agent-draft"]
    assert await names("source=operator") == ["gone-one", "live-one"]

    page = (await app.client.get("/skill-library?limit=2", headers=_h(READ))).json()
    assert [i["name"] for i in page["items"]] == ["agent-draft", "gone-one"]
    assert await names(f"cursor={page['next_cursor']}") == ["live-one"]


async def test_a_name_an_operator_uploaded_is_flagged_where_a_reviewer_looks(app: App) -> None:
    await app.store.put(
        f"skills/acme/{NAME}/0.1.0/SKILL.md", b"---\nname: invoice-triage\ndescription: Ops.\n---\nOps.\n"
    )
    created = await app.create()
    assert created.status_code == 201 and created.json()["shadows_operator_upload"] is True
    listing = (await app.client.get("/skill-library", headers=_h(READ))).json()
    assert listing["items"][0]["shadows_operator_upload"] is True
    detail = (await app.client.get(f"/skill-library/{NAME}", headers=_h(READ))).json()
    assert detail["shadows_operator_upload"] is True
    version = (await app.client.get(f"/skill-library/{NAME}/versions/0.1.0", headers=_h(READ))).json()
    assert version["shadows_operator_upload"] is True

    await app.client.post("/skill-library", json={"files": _files("other-skill")}, headers=_h(WRITE))
    other = (await app.client.get("/skill-library/other-skill", headers=_h(READ))).json()
    assert other["shadows_operator_upload"] is False


# -- review fixes ----------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["review", "policy"])
async def test_a_skill_may_be_named_like_a_collection_route(app: App, name: str) -> None:
    created = await app.create(files=_files(name))
    assert created.status_code == 201, created.text
    resp = await app.client.get(f"/skill-library/{name}", headers=_h(READ))
    assert resp.status_code == 200 and resp.json()["name"] == name
    assert (
        await app.client.get(f"/skill-library/{name}/versions/0.1.0", headers=_h(READ))
    ).status_code == 200


@pytest.mark.parametrize("cursor", ["\u00b2:a-skill:0.1.0", "abc", "12", "1:x"])
async def test_a_malformed_review_cursor_is_refused_not_a_crash(app: App, cursor: str) -> None:
    await _agent_draft(app)
    resp = await app.client.get("/skill-library/-/review", params={"cursor": cursor}, headers=_h(READ))
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"] == "invalid_cursor"


async def test_another_tenants_cursors_page_through_nothing_of_ours(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticks = iter(range(100, 200))
    monkeypatch.setattr(library, "now_ms", lambda: next(ticks))
    for name in ("alpha-skill", "beta-skill", "gamma-skill"):
        await _agent_draft(app, name)
    listing = (await app.client.get("/skill-library?limit=1", headers=_h(READ))).json()
    queue = (await app.client.get("/skill-library/-/review?limit=1", headers=_h(READ))).json()
    assert listing["next_cursor"] and queue["next_cursor"]

    other_listing = await app.client.get(
        "/skill-library", params={"cursor": listing["next_cursor"]}, headers=_h(GLOBEX)
    )
    other_queue = await app.client.get(
        "/skill-library/-/review", params={"cursor": queue["next_cursor"]}, headers=_h(GLOBEX)
    )
    assert other_listing.status_code == 200 and other_listing.json()["items"] == []
    assert other_queue.status_code == 200 and other_queue.json()["items"] == []


async def test_a_filtered_page_can_be_empty_and_still_lead_on(app: App) -> None:
    await _agent_draft(app, "aa-draft")
    await _agent_draft(app, "ab-draft")
    await app.client.post(
        "/skill-library", json={"files": _files("zz-live"), "publish": True}, headers=_h(WRITE)
    )

    first = (await app.client.get("/skill-library?status=live&limit=2", headers=_h(READ))).json()
    assert first["items"] == [] and first["next_cursor"] == "ab-draft"
    rest = (
        await app.client.get(
            "/skill-library",
            params={"status": "live", "limit": 2, "cursor": first["next_cursor"]},
            headers=_h(READ),
        )
    ).json()
    assert [i["name"] for i in rest["items"]] == ["zz-live"] and rest["next_cursor"] is None


async def test_preview_after_the_bytes_were_altered_names_the_digest(app: App) -> None:
    await app.create()
    await app.store.put(
        library_object_key("acme", NAME, "0.1.0", "SKILL.md", owner=ORG_OWNER),
        b"---\nname: x\n---\nAltered.\n",
    )
    body = (await app.client.get(f"/skill-library/{NAME}/versions/0.1.0/preview", headers=_h(READ))).json()
    assert (body["valid"], body["policy_passes"]) == (False, False)
    assert body["reasons"] and "changed in the object store since it was saved" in body["reasons"][0]
    assert body["quality_score"] is None and body["security_issues"] == []


async def test_an_operator_save_is_not_held_to_an_agents_pending_cap(app: App) -> None:
    for i in range(2):
        await library.save_draft(
            app.settings,
            "acme",
            files=_files(f"agent-{i}"),
            provenance=library.DraftProvenance(
                source="agent", author="contributor", origin_manifest_id="contributor"
            ),
            max_pending=2,
            object_store=app.store,
            owner=ORG_OWNER,
        )
    with pytest.raises(library.SkillPendingCapReached):
        await library.save_draft(
            app.settings,
            "acme",
            files=_files("agent-2"),
            provenance=library.DraftProvenance(
                source="agent", author="contributor", origin_manifest_id="contributor"
            ),
            max_pending=2,
            object_store=app.store,
            owner=ORG_OWNER,
        )
    resp = await app.create(files=_files("operator-one"))
    assert resp.status_code == 201, resp.text


async def test_a_publish_that_fails_for_a_state_reason_still_returns_the_saved_draft(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def raced(*_args: Any, **_kw: Any) -> Any:
        raise library.SkillVersionConflict("changed state while it was being published")

    monkeypatch.setattr(library, "publish", raced)
    resp = await app.create(publish=True)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body["status"], body["published"]) == ("draft", False)
    assert body["publish_blocked"]["error"] == "version_conflict"


async def test_every_read_redacts_text_a_saver_or_the_scan_wrote(app: App) -> None:
    leaky = BODY + f"\nFetch https://example.com/setup.sh?token={SHARED_SECRET} first.\n"
    created = await app.create(files=_files(body=leaky), reason=f"key is {SHARED_SECRET}")
    assert created.status_code == 201, created.text
    assert any("setup.sh" in i["message"] for i in created.json()["security_issues"]), created.text
    await app.client.post(
        f"/skill-library/{NAME}/versions/0.1.0/reject",
        json={"note": f"leaked {SHARED_SECRET}"},
        headers=_h(WRITE),
    )
    await app.client.put(
        f"/skill-library/{NAME}/versions",
        json={"files": _files(body=leaky), "parent_version": "0.1.0", "reason": SHARED_SECRET},
        headers=_h(WRITE),
    )
    reads = [
        created,
        await app.client.get(f"/skill-library/{NAME}", headers=_h(READ)),
        await app.client.get(f"/skill-library/{NAME}/versions/0.1.0", headers=_h(READ)),
        await app.client.get(f"/skill-library/{NAME}/versions/0.1.1/preview", headers=_h(READ)),
        await app.client.get("/skill-library/-/review", headers=_h(READ)),
        await app.client.get("/skill-library", headers=_h(READ)),
    ]
    for resp in reads:
        assert resp.status_code in {200, 201}, resp.text
        assert SHARED_SECRET not in resp.text, resp.request.url
    detail = reads[2].json()
    assert detail["reason"] == "key is [REDACTED]" and detail["decision_note"] == "leaked [REDACTED]"
    assert any("[REDACTED]" in i["message"] for i in detail["security_issues"])


async def test_list_and_activate_name_the_newest_version_an_update_must_cite(app: App) -> None:
    from felix.skills.loader import load_manifest_skills
    from felix.skills.store import InMemorySkillActivationStore
    from felix.skills.tools import make_skill_tools
    from felix.tools.types import ToolInvocationCtx, tool_output_content

    await app.create(publish=True)
    await _agent_draft(app, body=BODY + "\nA draft on top.\n")  # 0.1.1, not live
    catalog = await load_manifest_skills(
        [], tenant_id="acme", object_store=app.store, settings=app.settings, owner=None
    )
    tools = {
        t.name: t
        for t in make_skill_tools(
            catalog,
            activation_store=InMemorySkillActivationStore(),
            tenant_id="acme",
            manifest_id="contributor",
            settings=app.settings,
            object_store=app.store,
        )
    }
    ctx = ToolInvocationCtx(thread_id="acme:t1", tool_call_id="c1")
    listed = json.loads(tool_output_content(await tools["list_skills"].executor.execute({}, ctx)))
    entry = next(s for s in listed if s["name"] == NAME)
    assert entry["newest_version"] == "0.1.1"
    activated = json.loads(
        tool_output_content(await tools["activate_skill"].executor.execute({"name": NAME}, ctx))
    )
    assert (activated["version"], activated["newest_version"]) == ("0.1.0", "0.1.1")
