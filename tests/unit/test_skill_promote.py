"""Promoting a personal skill: a version of a person's library proposed to the tenant's as a draft.

`library.promote` copies a version that went live in the caller's own library, byte for byte, into
a draft of the tenant's skill of the name (`source="promoted"`), for the tenant's ordinary review
queue. It never publishes, never edits the personal skill, and the draft is judged as an agent's
would be: no reviewer of the tenant's has read it. The library tests run on the `memory://` twins;
the route tests go through `create_app` under `auth_mode=api_key`, because a scope check under
`auth_mode=none` checks nothing.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from felix.config import Settings
from felix.skills import library
from felix.skills.copy_rule import stored_bytes
from felix.skills.format import serialize_skill_md
from felix.skills.library_keys import ORG_OWNER, library_label
from felix.skills.library_store import ImportOrigin, get_skill_library_store
from felix.storage import MemoryObjectStore
from httpx import ASGITransport, AsyncClient

from tests.support.factories import make_settings

TENANT = "acme"
NAME = "notes"
ALICE = "api_key|alice"
BOB = "api_key|bob"
BODY = "\n# Notes\n\nUse this when a meeting ends and its notes must be filed.\n\n## Steps\n\n1. File them.\n"
# Long enough for the copy rule to look at (`copy_rule.COPY_FLOOR_CHARS`).
QUEUES = "# Queues\n\nSend invoices over 500 to finance, the rest to ops, and log every one.\n"
EVALS = '[{"name": "files", "prompt": "the meeting ended"}]'


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def store() -> MemoryObjectStore:
    return MemoryObjectStore()


def _files(name: str = NAME, body: str = BODY, **extra: str) -> dict[str, str]:
    return {"SKILL.md": serialize_skill_md({"name": name, "description": f"How {name} works"}, body), **extra}


async def _personal(
    settings: Settings,
    store: MemoryObjectStore,
    files: dict[str, str] | None = None,
    *,
    owner: str = ALICE,
    publish: bool = True,
) -> dict[str, Any]:
    """Save, and by default publish, a version in ``owner``'s own library."""
    files = files or _files()
    lib = get_skill_library_store(settings, owner=owner)
    name = files["SKILL.md"].split("name: ", 1)[1].split("\n", 1)[0].strip()
    parent = library.newest_version(await lib.version_ids(TENANT, name))
    row = await library.save_draft(
        settings,
        TENANT,
        files=files,
        provenance=library.DraftProvenance(source="operator", author=owner.split("|")[1]),
        parent=parent,
        object_store=store,
        owner=owner,
    )
    if publish:
        await library.publish(
            settings, TENANT, row["name"], row["version"], by="alice", object_store=store, owner=owner
        )
    return row


async def _org(
    settings: Settings, store: MemoryObjectStore, files: dict[str, str] | None = None, **kw: Any
) -> dict[str, Any]:
    newest = await get_skill_library_store(settings, owner=ORG_OWNER).version_ids(TENANT, NAME)
    return await library.save_draft(
        settings,
        TENANT,
        files=files or _files(body=BODY + "\nThe tenant's own text.\n"),
        provenance=library.DraftProvenance(source="operator", author="ops"),
        parent=library.newest_version(newest),
        object_store=store,
        owner=ORG_OWNER,
        **kw,
    )


async def _promote(
    settings: Settings, store: MemoryObjectStore, version: str = "0.1.0", **kw: Any
) -> dict[str, Any]:
    args: dict[str, Any] = {"by": "alice", "owner": ALICE, "name": NAME, **kw}
    name = args.pop("name")
    return await library.promote(settings, TENANT, name, version, object_store=store, **args)


def _org_lib(settings: Settings) -> Any:
    return get_skill_library_store(settings, owner=ORG_OWNER)


# -- what a promotion saves --------------------------------------------------------------------


async def test_a_promotion_saves_an_org_draft_of_the_same_bytes_and_leaves_the_personal_skill(
    settings: Settings, store: MemoryObjectStore
) -> None:
    files = _files(**{"references/queues.md": QUEUES})
    await _personal(settings, store, files)
    mine = get_skill_library_store(settings, owner=ALICE)
    before = (await mine.get_skill(TENANT, NAME), await mine.list_versions(TENANT, NAME))

    draft = await _promote(settings, store, reason="useful for the whole team")

    assert (draft["version"], draft["status"], draft["source"]) == ("0.1.0", "draft", "promoted")
    assert (draft["parent_version"], draft["promoted_from"], draft["author"]) == (None, "0.1.0", "alice")
    assert draft["reason"] == "useful for the whole team" and draft["lineage_import"] is False
    stored = await _org_lib(settings).get_version(TENANT, NAME, "0.1.0")
    assert stored is not None and stored["promoted_from"] == "0.1.0" and stored["owner"] == ORG_OWNER
    assert (
        await library.read_version_files(settings, TENANT, NAME, "0.1.0", object_store=store, owner=ORG_OWNER)
        == files
    )
    assert (await mine.get_skill(TENANT, NAME), await mine.list_versions(TENANT, NAME)) == before


async def test_a_promoted_draft_is_not_live_until_a_reviewer_publishes_it(
    settings: Settings, store: MemoryObjectStore
) -> None:
    await _personal(settings, store)
    await _promote(settings, store)
    org = _org_lib(settings)

    assert (await org.get_skill(TENANT, NAME) or {})["live_version"] is None
    assert await org.list_live(TENANT) == []
    assert [d["version"] for d in await org.list_drafts(TENANT)] == ["0.1.0"], "it is in the review queue"

    await library.publish(settings, TENANT, NAME, "0.1.0", by="ops", object_store=store, owner=ORG_OWNER)
    assert [r["name"] for r in await org.list_live(TENANT)] == [NAME]


async def test_a_promotion_follows_the_orgs_newest_version_that_was_not_rejected(
    settings: Settings, store: MemoryObjectStore
) -> None:
    await _org(settings, store)
    await library.publish(settings, TENANT, NAME, "0.1.0", by="ops", object_store=store, owner=ORG_OWNER)
    await _org(settings, store, _files(body=BODY + "\nA draft nobody wanted.\n"))
    await library.reject(settings, TENANT, NAME, "0.1.1", by="ops", note="no", owner=ORG_OWNER)
    await _personal(settings, store)

    draft = await _promote(settings, store)

    assert (draft["version"], draft["parent_version"]) == ("0.1.2", "0.1.0")
    assert (await _org_lib(settings).get_skill(TENANT, NAME) or {})["live_version"] == "0.1.0"


# -- what a promotion refuses ------------------------------------------------------------------


async def test_a_version_that_never_went_live_in_its_own_library_is_refused(
    settings: Settings, store: MemoryObjectStore
) -> None:
    await _personal(settings, store, publish=False)
    with pytest.raises(library.SkillVersionConflict, match="publish it there first"):
        await _promote(settings, store)
    await library.reject(settings, TENANT, NAME, "0.1.0", by="alice", note="no", owner=ALICE)
    with pytest.raises(library.SkillVersionConflict):
        await _promote(settings, store)
    with pytest.raises(library.SkillNotFound):
        await _promote(settings, store, "0.9.0")
    assert await _org_lib(settings).version_ids(TENANT, NAME) == []


async def test_the_tenants_own_library_has_nothing_to_promote(
    settings: Settings, store: MemoryObjectStore
) -> None:
    await _org(settings, store)
    await library.publish(settings, TENANT, NAME, "0.1.0", by="ops", object_store=store, owner=ORG_OWNER)
    with pytest.raises(library.SkillOrgOnly):
        await _promote(settings, store, owner=ORG_OWNER)
    assert await _org_lib(settings).version_ids(TENANT, NAME) == ["0.1.0"]


async def test_a_personal_save_cannot_claim_to_be_a_promotion(
    settings: Settings, store: MemoryObjectStore
) -> None:
    with pytest.raises(library.SkillOrgOnly):
        await library.save_draft(
            settings,
            TENANT,
            files=_files(),
            provenance=library.DraftProvenance(source="promoted", author="alice", promoted_from="0.1.0"),
            object_store=store,
            owner=ALICE,
        )


@pytest.mark.parametrize("org_has_skill", [False, True], ids=["new-name", "existing-name"])
async def test_a_promotion_cannot_add_or_change_evals(
    settings: Settings, store: MemoryObjectStore, org_has_skill: bool
) -> None:
    if org_has_skill:
        await _org(settings, store)
    await _personal(settings, store, _files(**{"evals/scenarios.json": EVALS}))

    with pytest.raises(library.SkillBundleInvalid) as refused:
        await _promote(settings, store)

    assert refused.value.code == "invalid_bundle"
    assert [i.path for i in refused.value.issues] == ["evals/scenarios.json"]
    assert refused.value.issues[0].message == library.PROMOTED_EVALS
    assert await _org_lib(settings).version_ids(TENANT, NAME) == (["0.1.0"] if org_has_skill else [])


async def test_a_promotion_cannot_drop_the_tenants_evals(
    settings: Settings, store: MemoryObjectStore
) -> None:
    """Dropping a scenario the tenant's reviewers wrote -- the one the version fails, say -- games
    the gate as surely as writing one."""
    await _org(
        settings,
        store,
        _files(body=BODY + "\nOrg.\n", **{"evals/scenarios.json": EVALS, "evals/edge.json": EVALS}),
    )
    await _personal(settings, store, _files(**{"evals/scenarios.json": EVALS}))

    with pytest.raises(library.SkillBundleInvalid) as refused:
        await _promote(settings, store)

    assert [(i.path, i.message) for i in refused.value.issues] == [
        ("evals/edge.json", library.PROMOTED_EVALS)
    ]
    assert await _org_lib(settings).version_ids(TENANT, NAME) == ["0.1.0"]


async def test_an_agents_save_may_still_drop_an_eval_file(
    settings: Settings, store: MemoryObjectStore
) -> None:
    """The exact rule is a promotion's; an agent's save keeps the rule it had (add or change)."""
    await _org(settings, store, _files(body=BODY + "\nOrg.\n", **{"evals/scenarios.json": EVALS}))
    saved = await library.save_draft(
        settings,
        TENANT,
        files=_files(body=BODY + "\nAgent.\n"),
        provenance=library.DraftProvenance(source="agent", author="m", origin_manifest_id="m"),
        name=NAME,
        parent="0.1.0",
        object_store=store,
        owner=ORG_OWNER,
    )
    assert saved["version"] == "0.1.1"


async def test_a_promotion_may_carry_the_orgs_own_evals_unchanged(
    settings: Settings, store: MemoryObjectStore
) -> None:
    await _org(settings, store, _files(body=BODY + "\nOrg.\n", **{"evals/scenarios.json": EVALS}))
    await _personal(settings, store, _files(**{"evals/scenarios.json": EVALS}))
    draft = await _promote(settings, store)
    assert (draft["version"], draft["parent_version"]) == ("0.1.1", "0.1.0")


# -- lineage -----------------------------------------------------------------------------------


async def _plant_personal_lineage(settings: Settings, store: MemoryObjectStore) -> None:
    """A live personal version that carries imported text, with no file the tenant's library holds:
    so only inheriting the personal row's mark -- not the copy rule -- can mark the promotion."""
    lib = get_skill_library_store(settings, owner=ALICE)
    files = _files()
    rows = library._file_rows(files)
    row = {
        "name": NAME,
        "version": "0.1.0",
        "status": "draft",
        "source": "agent",
        "author": "contributor",
        "origin_manifest_id": "contributor",
        "description": "How notes works",
        "security_status": "pass",
        "lineage_import": True,
        "created_at": 1,
    }
    await lib.insert_version(TENANT, row, rows, created_by="contributor", at=1)
    for path, content in files.items():
        await store.put(lib.object_key(TENANT, NAME, "0.1.0", path), stored_bytes(path, content))
    await lib.publish(TENANT, NAME, "0.1.0", from_statuses={"draft"}, by="alice", at=2)


async def test_a_promotion_inherits_the_personal_versions_import_lineage(
    settings: Settings, store: MemoryObjectStore
) -> None:
    await _plant_personal_lineage(settings, store)
    draft = await _promote(settings, store)
    assert draft["lineage_import"] is True
    stored = await _org_lib(settings).get_version(TENANT, NAME, draft["version"])
    assert stored is not None and stored["lineage_import"] is True


async def test_a_promotion_copying_an_orgs_imported_file_carries_its_lineage(
    settings: Settings, store: MemoryObjectStore
) -> None:
    origin = ImportOrigin(
        source="github:acme/skills/skills/ledger", ref="main", commit="a" * 40, tree_hash="t"
    )
    await library.save_draft(
        settings,
        TENANT,
        files=_files("ledger", **{"references/queues.md": QUEUES}),
        provenance=library.DraftProvenance(source="import", author="ops", origin=origin),
        object_store=store,
        owner=ORG_OWNER,
    )
    await _personal(settings, store, _files(**{"references/queues.md": QUEUES}))
    draft = await _promote(settings, store)
    assert draft["lineage_import"] is True, "the copy rule runs on a promotion, as on an agent's save"


async def test_a_promotion_never_takes_over_an_imported_skill(
    settings: Settings, store: MemoryObjectStore
) -> None:
    """Promoted into an import's name, the draft would be the newest version an update builds on,
    and an update never replaces another origin's version: the import would stop taking updates
    and its adopt would be stale. Refused until a reviewer adopts the import."""
    origin = ImportOrigin(
        source=f"github:acme/skills/skills/{NAME}", ref="main", commit="a" * 40, tree_hash="t"
    )
    await library.save_draft(
        settings,
        TENANT,
        files=_files(body=BODY + "\nTheirs.\n"),
        provenance=library.DraftProvenance(source="import", author="ops", origin=origin),
        object_store=store,
        owner=ORG_OWNER,
    )
    await _personal(settings, store)

    with pytest.raises(library.SkillOriginMismatch) as refused:
        await _promote(settings, store)
    assert refused.value.code == "origin_mismatch" and "adopt" in str(refused.value)
    assert await _org_lib(settings).version_ids(TENANT, NAME) == ["0.1.0"]

    await library.adopt(
        settings, TENANT, NAME, "0.1.0", by="ops", reason="ours now", object_store=store, owner=ORG_OWNER
    )
    draft = await _promote(settings, store)
    assert (draft["version"], draft["parent_version"]) == ("0.1.2", "0.1.1")


async def test_an_undecided_promotion_carrying_imported_text_cannot_be_adopted(
    settings: Settings, store: MemoryObjectStore
) -> None:
    await _plant_personal_lineage(settings, store)
    await _promote(settings, store)
    with pytest.raises(library.SkillAgentDraft, match="promoted draft nobody has reviewed"):
        await library.adopt(
            settings,
            TENANT,
            NAME,
            "0.1.0",
            by="ops",
            reason="looks fine",
            object_store=store,
            owner=ORG_OWNER,
        )


# -- the review queue's bounds ---------------------------------------------------------------


async def test_one_undecided_promotion_per_tenant_skill_from_anyone(
    settings: Settings, store: MemoryObjectStore
) -> None:
    await _personal(settings, store)
    await _personal(settings, store, owner=BOB)
    await _promote(settings, store)

    with pytest.raises(library.SkillPromotionPending) as pending:
        await _promote(settings, store, by="bob", owner=BOB)
    assert pending.value.code == "promotion_pending"
    assert await _org_lib(settings).version_ids(TENANT, NAME) == ["0.1.0"]

    await library.reject(settings, TENANT, NAME, "0.1.0", by="ops", note="not yet", owner=ORG_OWNER)
    draft = await _promote(settings, store, by="bob", owner=BOB)
    assert (draft["version"], draft["author"]) == ("0.1.1", "bob")


async def test_a_promoter_holds_at_most_the_cap_of_undecided_promotions(
    settings: Settings, store: MemoryObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(library, "MAX_PENDING_PROMOTIONS", 2)
    for name in ("notes", "ledger", "agenda"):
        await _personal(settings, store, _files(name))
    await _promote(settings, store, name="notes")
    await _promote(settings, store, name="ledger")

    with pytest.raises(library.SkillPendingCapReached, match="limit 2"):
        await _promote(settings, store, name="agenda")
    await _personal(settings, store, _files("agenda"), owner=BOB)
    assert (await _promote(settings, store, name="agenda", by="bob", owner=BOB))["author"] == "bob"

    await library.publish(settings, TENANT, "notes", "0.1.0", by="ops", object_store=store, owner=ORG_OWNER)
    await _personal(settings, store, _files("minutes"))
    assert (await _promote(settings, store, name="minutes"))["status"] == "draft", "a decision frees a place"


def test_the_promotion_cap_is_the_agent_pending_caps_default() -> None:
    from felix.manifests.schema import SkillAuthoringSpec

    assert SkillAuthoringSpec.model_fields["max_pending"].default == library.MAX_PENDING_PROMOTIONS


# -- the gate ----------------------------------------------------------------------------------


async def _eval(settings: Settings, version: str, *, n: int, scenario_source: str) -> None:
    from felix.skills.eval_store import get_skill_eval_store

    evals = get_skill_eval_store(settings)
    eval_id = f"00000000-0000-4000-8000-{n:012d}"
    await evals.insert(
        TENANT, {"id": eval_id, "name": NAME, "version": version, "status": "queued", "created_at": n}
    )
    claimed = await evals.claim_next(now=100 + n)
    assert claimed is not None and claimed["id"] == eval_id
    await evals.finish(
        TENANT,
        eval_id,
        token=claimed["claim_token"],
        fields={
            "status": "succeeded",
            "uplift": 10,
            "scenario_source": scenario_source,
            "finished_at": 200 + n,
        },
    )


async def test_a_promoted_version_counts_only_an_evaluation_on_its_bundles_own_scenarios(
    store: MemoryObjectStore,
) -> None:
    settings = Settings(database_url="memory://skill-promote-gate", skill_publish_require_eval=True)
    await _personal(settings, store, publish=False)
    # A personal publish under `require_eval` is refused (evaluations are the tenant's), so plant
    # the personal version's first publish directly: what is under test is the tenant's gate.
    await get_skill_library_store(settings, owner=ALICE).publish(
        TENANT, NAME, "0.1.0", from_statuses={"draft"}, by="alice", at=1
    )
    await _promote(settings, store)

    await _eval(settings, "0.1.0", n=1, scenario_source="generated")
    with pytest.raises(library.SkillPublishBlocked, match="requires a succeeded evaluation"):
        await library.publish(settings, TENANT, NAME, "0.1.0", by="ops", object_store=store, owner=ORG_OWNER)

    await _eval(settings, "0.1.0", n=2, scenario_source="bundle")
    published = await library.publish(
        settings, TENANT, NAME, "0.1.0", by="ops", object_store=store, owner=ORG_OWNER
    )
    assert published["status"] == "published"


# -- the audit trail ---------------------------------------------------------------------------


async def test_a_promotion_is_audited_with_the_personal_library_and_version_never_the_owner(
    store: MemoryObjectStore,
) -> None:
    from felix.audit import store as audit_store

    secret = "-".join(["plain", "marker", "value", "zz"])
    settings = Settings(database_url="memory://skill-promote-audit", **{"anthropic_api_key": secret})
    await _personal(settings, store)
    await _promote(settings, store, reason=f"vetted against {secret}")
    await audit_store.flush_pending(settings)
    events, _ = await audit_store.list_events(settings, TENANT, limit=50)

    (event,) = [e for e in events if e["event_type"] == "skill_promoted"]
    payload = event["payload_json"]
    assert event["principal_subj"] == "alice"
    assert (payload["skill"], payload["version"], payload["promoted_from"]) == (NAME, "0.1.0", "0.1.0")
    assert (payload["library"], payload["source"]) == (library_label(ALICE), "promoted")
    assert payload["reason"].startswith("vetted against") and secret not in payload["reason"]
    assert ALICE not in json.dumps(payload), "the personal owner is never in the trail"


# -- the route ---------------------------------------------------------------------------------

KEYS = json.dumps(
    {
        "sk-alice": {"tenant_id": TENANT, "sub": "alice", "scopes": ["skills:personal"]},
        "sk-reader": {"tenant_id": TENANT, "sub": "reader", "scopes": []},
        "sk-write": {"tenant_id": TENANT, "sub": "editor", "scopes": ["skills:write"]},
        "sk-admin": {"tenant_id": TENANT, "sub": "ops", "scopes": ["admin"]},
    }
)
ROUTE = f"/skill-library/~me/{NAME}/versions/0.1.0/promote"


class App:
    def __init__(self, client: AsyncClient, settings: Settings) -> None:
        self.client, self.settings = client, settings

    @property
    def store(self) -> Any:
        from felix.storage import get_object_store

        return get_object_store(self.settings)

    async def post(self, key: str, path: str = ROUTE, body: Any = None) -> Any:
        return await self.client.post(path, json=body, headers={"Authorization": f"Bearer {key}"})


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[App]:
    from felix_api.app import create_app

    settings = Settings(
        allow_insecure=True,
        auth_mode="api_key",
        auth_api_keys=KEYS,
        environment="development",
        object_store="memory",
        database_url="memory://skill-promote-routes",
        data_dir=str(tmp_path),
    )
    transport = ASGITransport(app=create_app(settings=settings, plugins=[]))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield App(client, settings)


async def test_the_route_promotes_the_callers_own_version_into_the_review_queue(app: App) -> None:
    await _personal(app.settings, app.store)
    resp = await app.post("sk-alice", body={"reason": "the team wants it"})

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body["version"], body["status"], body["source"], body["author"]) == (
        "0.1.0",
        "draft",
        "promoted",
        "alice",
    )
    assert (body["promoted_from"], body["published"], body["reason"]) == ("0.1.0", False, "the team wants it")
    assert [f["path"] for f in body["files"]] == ["SKILL.md"]
    queue = (
        await app.client.get("/skill-library/-/review", headers={"Authorization": "Bearer sk-write"})
    ).json()
    assert [(d["name"], d["source"], d["promoted_from"]) for d in queue["items"]] == [
        (NAME, "promoted", "0.1.0")
    ]
    listed = await app.client.get(
        "/skill-library?source=promoted", headers={"Authorization": "Bearer sk-write"}
    )
    assert [s["name"] for s in listed.json()["items"]] == [NAME]


async def test_the_route_takes_no_body_too(app: App) -> None:
    await _personal(app.settings, app.store)
    resp = await app.client.post(ROUTE, headers={"Authorization": "Bearer sk-alice"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["reason"] == ""


async def test_promoting_needs_skills_personal(app: App) -> None:
    await _personal(app.settings, app.store, owner="api_key|reader")
    resp = await app.post("sk-reader")
    assert resp.status_code == 403, resp.text
    assert "skills:personal" in resp.text
    assert await _org_lib(app.settings).version_ids(TENANT, NAME) == []


async def test_neither_the_tenants_library_nor_a_digest_has_the_route(app: App) -> None:
    await _personal(app.settings, app.store)
    tenant = await app.post("sk-write", f"/skill-library/{NAME}/versions/0.1.0/promote")
    digest = library_label(ALICE)
    someone = await app.post("sk-admin", f"/skill-library/{digest}/{NAME}/versions/0.1.0/promote")

    assert tenant.status_code in {404, 405}, tenant.text
    assert someone.status_code == 422, someone.text
    assert await _org_lib(app.settings).version_ids(TENANT, NAME) == []


@pytest.mark.parametrize(
    ("setup", "status", "error"),
    [
        ("unpublished", 409, "version_conflict"),
        ("missing", 404, "not_found"),
        ("pending", 409, "promotion_pending"),
        ("cap", 429, "pending_cap_reached"),
        ("evals", 422, "invalid_bundle"),
    ],
)
async def test_the_routes_refusals(
    app: App, monkeypatch: pytest.MonkeyPatch, setup: str, status: int, error: str
) -> None:
    if setup == "unpublished":
        await _personal(app.settings, app.store, publish=False)
    elif setup == "pending":
        await _personal(app.settings, app.store, owner=BOB)
        await _promote(app.settings, app.store, by="bob", owner=BOB)
        await _personal(app.settings, app.store)
    elif setup == "cap":
        monkeypatch.setattr(library, "MAX_PENDING_PROMOTIONS", 1)
        await _personal(app.settings, app.store, _files("ledger"))
        await _promote(app.settings, app.store, name="ledger")
        await _personal(app.settings, app.store)
    elif setup == "evals":
        await _personal(app.settings, app.store, _files(**{"evals/scenarios.json": EVALS}))

    resp = await app.post("sk-alice")

    assert resp.status_code == status, resp.text
    assert resp.json()["error"] == error


# -- races and edges, pinned as intended ---------------------------------------------------------


@pytest.mark.parametrize("org_has_skill", [False, True], ids=["new-name", "existing-name"])
async def test_a_save_landing_while_a_promotion_reads_its_files_wins(
    settings: Settings, store: MemoryObjectStore, monkeypatch: pytest.MonkeyPatch, org_has_skill: bool
) -> None:
    """`expect_newest`: an org save that lands between the promotion's choice of parent and its
    write refuses the promotion (`skill_exists` for a new name, `parent_changed` otherwise), and
    no promoted draft is left behind."""
    if org_has_skill:
        await _org(settings, store)
        await library.publish(settings, TENANT, NAME, "0.1.0", by="ops", object_store=store, owner=ORG_OWNER)
    await _personal(settings, store)
    real = library.read_version_files

    async def racing(*args: Any, **kwargs: Any) -> dict[str, str]:
        await _org(settings, store, _files(body=BODY + "\nA racing save.\n"))
        return await real(*args, **kwargs)

    monkeypatch.setattr(library, "read_version_files", racing)
    expected = library.SkillParentChanged if org_has_skill else library.SkillExists
    with pytest.raises(expected):
        await _promote(settings, store)
    assert await _org_lib(settings).count_drafts(TENANT, source="promoted") == 0


async def test_an_older_superseded_personal_version_is_promoted_as_itself(
    settings: Settings, store: MemoryObjectStore
) -> None:
    await _personal(settings, store)
    await _personal(settings, store, _files(body=BODY + "\nSecond take.\n"))
    draft = await _promote(settings, store, "0.1.0")
    assert (draft["status"], draft["promoted_from"]) == ("draft", "0.1.0")
    assert (
        await library.read_version_files(
            settings, TENANT, NAME, draft["version"], object_store=store, owner=ORG_OWNER
        )
        == _files()
    )


async def test_a_version_of_a_personal_skill_its_owner_archived_can_still_be_promoted(
    settings: Settings, store: MemoryObjectStore
) -> None:
    await _personal(settings, store)
    await library.archive_skill(settings, TENANT, NAME, by="alice", owner=ALICE)
    draft = await _promote(settings, store)
    assert (draft["status"], draft["promoted_from"]) == ("draft", "0.1.0")


async def test_the_same_person_may_promote_bytes_a_reviewer_rejected_again(
    settings: Settings, store: MemoryObjectStore
) -> None:
    await _personal(settings, store)
    await _promote(settings, store)
    await library.reject(settings, TENANT, NAME, "0.1.0", by="ops", note="not yet", owner=ORG_OWNER)
    again = await _promote(settings, store)
    assert (again["version"], again["status"], again["promoted_from"]) == ("0.1.1", "draft", "0.1.0")


# -- the source table ------------------------------------------------------------------------


def test_every_source_has_one_row_in_the_traits_table() -> None:
    from typing import get_args

    from felix.skills.sources import SOURCES, SkillSourceKind

    assert set(SOURCES) == set(get_args(SkillSourceKind))


def test_an_unknown_source_is_review_material_and_held_to_its_bundles_evals() -> None:
    from felix.skills.publish_gate import gate_scenario_source
    from felix.skills.sources import needs_review_when_agent_edits

    assert needs_review_when_agent_edits("someday-source") is True
    assert needs_review_when_agent_edits("agent") is False
    assert gate_scenario_source("someday-source") == "bundle"
