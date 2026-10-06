"""Adopting an import: the one save that clears `lineage_import`, and only forward.

An operator vouches for an import-lineage version with a reason; its files are saved byte for
byte as a new operator draft whose `lineage_import` is false and whose `adopted_from` names the
version. The library tests run on the `memory://` twins; the route tests go through `create_app`
under `auth_mode=api_key`, because a scope check under `auth_mode=none` checks nothing.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from felix.config import Settings
from felix.skills import library
from felix.skills.library_store import ImportOrigin, get_skill_library_store
from felix.storage import MemoryObjectStore
from httpx import ASGITransport, AsyncClient

from tests.skill_import_fake import skill_md

NAME = "invoice-triage"
# A link to an executable is a `medium` finding: the scan is advisory, which blocks an import
# whatever the policy says and leaves an operator's version to the policy.
ADVISORY = "\nThe router binary is at https://example.test/router.sh if you need it.\n"
BODY = "# Triage\n\nUse this when an invoice arrives and must be routed.\n"
# Long enough for the copy rule to look at (`library.COPY_FLOOR_CHARS`).
QUEUES = "# Queues\n\nSend invoices over 500 to finance, the rest to ops.\n"


@pytest.fixture
def settings() -> Settings:
    return Settings(database_url="memory://skill-adopt")


@pytest.fixture
def store() -> MemoryObjectStore:
    return MemoryObjectStore()


def _files(body: str = BODY, description: str = "Route invoices to the right queue.") -> dict[str, str]:
    return {"SKILL.md": skill_md(NAME, description, body).decode(), "references/queues.md": QUEUES}


async def _import(
    settings: Settings, store: MemoryObjectStore, files: dict[str, str] | None = None, *, tree: str = "t1"
) -> dict[str, Any]:
    origin = ImportOrigin(
        source=f"github:acme/skills/skills/{NAME}", ref="main", commit="a" * 40, tree_hash=tree
    )
    newest = await get_skill_library_store(settings).version_ids("acme", NAME)
    return await library.save_draft(
        settings,
        "acme",
        files=files or _files(),
        provenance=library.DraftProvenance(source="import", author="ops", origin=origin),
        name=NAME,
        parent=library.newest_version(newest),
        object_store=store,
    )


async def _adopt(settings: Settings, store: MemoryObjectStore, version: str, **kw: Any) -> dict[str, Any]:
    args: dict[str, Any] = {"by": "alice", "reason": "read every line; ours now", **kw}
    return await library.adopt(settings, "acme", NAME, version, object_store=store, **args)


async def _save(
    settings: Settings,
    store: MemoryObjectStore,
    files: dict[str, str],
    *,
    source: str,
    parent: str | None,
    name: str | None = NAME,
) -> dict[str, Any]:
    who: dict[str, Any] = {"source": source, "author": "someone"}
    if source == "agent":
        who["origin_manifest_id"] = "contributor"
    return await library.save_draft(
        settings,
        "acme",
        files=files,
        provenance=library.DraftProvenance(**who),
        name=name,
        parent=parent,
        object_store=store,
    )


# -- what an adopt saves -----------------------------------------------------------------------


async def test_adopt_saves_an_operator_draft_of_the_same_bytes_without_the_mark(
    settings: Settings, store: MemoryObjectStore
) -> None:
    await _import(settings, store)
    adopted = await _adopt(settings, store, "0.1.0")
    lib = get_skill_library_store(settings)

    assert (adopted["version"], adopted["status"], adopted["source"]) == ("0.1.1", "draft", "operator")
    assert (adopted["parent_version"], adopted["adopted_from"], adopted["lineage_import"]) == (
        "0.1.0",
        "0.1.0",
        False,
    )
    assert (adopted["author"], adopted["reason"]) == ("alice", "read every line; ours now")
    assert adopted.get("origin_source") is None, "an operator's version has no import origin"

    def digests(rows: list[dict[str, Any]]) -> dict[str, str]:
        return {r["path"]: r["sha256"] for r in rows}

    assert digests(await lib.list_files("acme", NAME, "0.1.1")) == digests(
        await lib.list_files("acme", NAME, "0.1.0")
    )
    assert await library.read_version_files(settings, "acme", NAME, "0.1.1", object_store=store) == _files()

    # Versions are immutable: the import keeps its mark.
    original = await lib.get_version("acme", NAME, "0.1.0")
    assert original is not None and original["lineage_import"] is True and original["source"] == "import"


async def test_adopt_never_publishes_and_leaves_the_live_version_alone(
    settings: Settings, store: MemoryObjectStore
) -> None:
    await _import(settings, store)
    await library.publish(settings, "acme", NAME, "0.1.0", by="ops", object_store=store)
    adopted = await _adopt(settings, store, "0.1.0")
    lib = get_skill_library_store(settings)
    assert adopted["status"] == "draft" and adopted.get("published_at") is None
    assert (await lib.get_version("acme", NAME, "0.1.1") or {})["status"] == "draft"
    assert (await lib.get_skill("acme", NAME) or {})["live_version"] == "0.1.0"


async def test_the_gate_judges_an_adopted_version_as_an_operators(
    settings: Settings, store: MemoryObjectStore
) -> None:
    await _import(settings, store, _files(BODY + ADVISORY))
    assert settings.skill_publish_block_on_advisory is False
    with pytest.raises(library.SkillPublishBlocked, match="advisory"):
        await library.publish(settings, "acme", NAME, "0.1.0", by="ops", object_store=store)

    adopted = await _adopt(settings, store, "0.1.0")
    assert adopted["security_status"] == "advisory", "the same bytes, scanned the same"
    published = await library.publish(settings, "acme", NAME, "0.1.1", by="ops", object_store=store)
    assert published["status"] == "published", "the advisory is the tenant policy's call again"


async def test_activation_stops_screening_an_adopted_version_once_it_is_live(
    settings: Settings, store: MemoryObjectStore
) -> None:
    from felix.skills.loader import load_manifest_skills

    async def untrusted() -> bool:
        catalog = await load_manifest_skills(
            [], tenant_id="acme", object_store=store, settings=settings, bundled_dir=Path("/nonexistent")
        )
        skill = catalog.get(NAME)
        assert skill is not None
        return skill.untrusted

    await _import(settings, store)
    await library.publish(settings, "acme", NAME, "0.1.0", by="ops", object_store=store)
    assert await untrusted() is True
    await _adopt(settings, store, "0.1.0")
    assert await untrusted() is True, "a draft is not live: the import still answers"
    await library.publish(settings, "acme", NAME, "0.1.1", by="ops", object_store=store)
    assert await untrusted() is False


# -- what stays tainted, and what does not -------------------------------------------------------


async def test_edits_built_on_the_adopted_version_are_not_import_lineage(
    settings: Settings, store: MemoryObjectStore
) -> None:
    await _import(settings, store)
    await _adopt(settings, store, "0.1.0")
    edited = {"SKILL.md": skill_md(NAME, "Route invoices, our way.", BODY + "\nOurs.\n").decode()}
    operator = await _save(settings, store, edited, source="operator", parent="0.1.1")
    assert operator["lineage_import"] is False
    agent_edit = {"SKILL.md": skill_md(NAME, "Route invoices, an agent's way.", BODY + "\nMine.\n").decode()}
    agent = await _save(settings, store, agent_edit, source="agent", parent=operator["version"])
    assert agent["lineage_import"] is False


async def test_an_ordinary_operator_save_of_an_imports_exact_files_stays_tainted(
    settings: Settings, store: MemoryObjectStore
) -> None:
    """The exemption is the adopt's alone: the same bytes, saved by the same operator through the
    ordinary save, still inherit the parent's mark."""
    await _import(settings, store)
    resaved = await _save(settings, store, _files(), source="operator", parent="0.1.0")
    assert resaved["lineage_import"] is True and resaved.get("adopted_from") is None


async def test_an_agents_byte_copy_of_adopted_text_is_still_tainted(
    settings: Settings, store: MemoryObjectStore
) -> None:
    """An operator vouched for the text once, in that skill. An agent writing it into another
    skill is still copying a third party's text."""
    await _import(settings, store)
    await _adopt(settings, store, "0.1.0")
    adopted = await library.read_version_files(settings, "acme", NAME, "0.1.1", object_store=store)
    copy = {
        "SKILL.md": skill_md("queue-notes", "Something else.").decode(),
        "references/queues.md": adopted["references/queues.md"],
    }
    laundered = await _save(settings, store, copy, source="agent", parent=None, name="queue-notes")
    assert laundered["lineage_import"] is True


async def test_an_adopt_flag_on_anything_but_an_operator_save_of_the_same_files_is_refused(
    settings: Settings, store: MemoryObjectStore
) -> None:
    await _import(settings, store)
    agent = library.DraftProvenance(
        source="agent", author="a", origin_manifest_id="contributor", adopted_from="0.1.0"
    )
    with pytest.raises(ValueError, match="operator"):
        await library.save_draft(
            settings, "acme", files=_files(), provenance=agent, name=NAME, parent="0.1.0", object_store=store
        )
    changed = library.DraftProvenance(source="operator", author="o", adopted_from="0.1.0")
    with pytest.raises(ValueError, match="exactly"):
        await library.save_draft(
            settings,
            "acme",
            files=_files(BODY + "\nchanged\n"),
            provenance=changed,
            name=NAME,
            parent="0.1.0",
            object_store=store,
        )
    assert await get_skill_library_store(settings).version_ids("acme", NAME) == ["0.1.0"]


# -- refusals ----------------------------------------------------------------------------------


async def test_a_version_with_no_imported_text_is_not_imported(
    settings: Settings, store: MemoryObjectStore
) -> None:
    await _save(settings, store, _files(), source="operator", parent=None)
    with pytest.raises(library.SkillNotImportLineage) as caught:
        await _adopt(settings, store, "0.1.0")
    assert caught.value.code == "not_imported"


async def test_an_adopted_version_cannot_be_adopted_again(
    settings: Settings, store: MemoryObjectStore
) -> None:
    await _import(settings, store)
    await _adopt(settings, store, "0.1.0")
    with pytest.raises(library.SkillNotImportLineage):
        await _adopt(settings, store, "0.1.1")


async def test_adopt_builds_on_the_newest_version_or_is_refused(
    settings: Settings, store: MemoryObjectStore
) -> None:
    await _import(settings, store)
    await _import(settings, store, _files(BODY + "\nUpstream moved.\n"), tree="t2")
    with pytest.raises(library.SkillParentChanged):
        await _adopt(settings, store, "0.1.0")
    assert await get_skill_library_store(settings).version_ids("acme", NAME) == ["0.1.0", "0.1.1"]


async def test_a_rejected_version_cannot_be_adopted(settings: Settings, store: MemoryObjectStore) -> None:
    await _import(settings, store)
    await library.reject(settings, "acme", NAME, "0.1.0", by="ops", note="no")
    with pytest.raises(library.SkillParentRejected):
        await _adopt(settings, store, "0.1.0")


@pytest.mark.parametrize("reason", ["", "   \n\t"])
async def test_adopt_needs_a_reason(reason: str, settings: Settings, store: MemoryObjectStore) -> None:
    await _import(settings, store)
    with pytest.raises(library.SkillReasonRequired):
        await _adopt(settings, store, "0.1.0", reason=reason)
    assert await get_skill_library_store(settings).version_ids("acme", NAME) == ["0.1.0"]


# -- the audit trail ---------------------------------------------------------------------------


async def test_an_adopt_is_audited_with_who_why_and_from_which_version(store: MemoryObjectStore) -> None:
    from felix.audit import store as audit_store

    secret = "-".join(["plain", "marker", "value", "zz"])
    settings = Settings(database_url="memory://skill-adopt-audit", **{"anthropic_api_key": secret})
    await _import(settings, store)
    await _adopt(settings, store, "0.1.0", reason=f"vetted against {secret}")
    await audit_store.flush_pending(settings)
    events, _ = await audit_store.list_events(settings, "acme", limit=50)
    (event,) = [e for e in events if e["event_type"] == "skill_adopted"]
    payload = event["payload_json"]
    assert event["principal_subj"] == "alice"
    assert (payload["skill"], payload["version"], payload["adopted_from"]) == (NAME, "0.1.1", "0.1.0")
    assert payload["principal"] == "alice" and payload["source"] == "operator"
    assert payload["reason"].startswith("vetted against") and secret not in payload["reason"]


# -- the route ---------------------------------------------------------------------------------

KEYS = json.dumps(
    {
        "sk-read": {"tenant_id": "acme", "sub": "reader", "scopes": ["skills:read"]},
        "sk-write": {"tenant_id": "acme", "sub": "editor", "scopes": ["skills:write"]},
        "sk-chat": {"tenant_id": "acme", "sub": "agent-ish", "scopes": ["chat:write"]},
    }
)
ROUTE = f"/skill-library/{NAME}/versions/0.1.0/adopt"


class App:
    def __init__(self, client: AsyncClient, settings: Settings) -> None:
        self.client, self.settings = client, settings

    @property
    def store(self) -> Any:
        from felix.storage import get_object_store

        return get_object_store(self.settings)

    async def adopt(self, key: str, body: Any = None, path: str = ROUTE) -> Any:
        sent = {"reason": "reviewed"} if body is None else body
        return await self.client.post(path, json=sent, headers={"Authorization": f"Bearer {key}"})


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[App]:
    from felix_api.app import create_app

    settings = Settings(
        allow_insecure=True,
        auth_mode="api_key",
        auth_api_keys=KEYS,
        environment="development",
        object_store="memory",
        database_url="memory://skill-adopt-routes",
        data_dir=str(tmp_path),
    )
    transport = ASGITransport(app=create_app(settings=settings, plugins=[]))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield App(client, settings)


@pytest.mark.parametrize("key", ["sk-read", "sk-chat"])
async def test_adopt_needs_skills_write(app: App, key: str) -> None:
    await _import(app.settings, app.store)
    resp = await app.adopt(key)
    assert resp.status_code == 403, resp.text
    assert "skills:write" in resp.text
    assert await get_skill_library_store(app.settings).version_ids("acme", NAME) == ["0.1.0"]


async def test_the_route_saves_an_adopted_draft_and_names_the_operator(app: App) -> None:
    await _import(app.settings, app.store)
    resp = await app.adopt("sk-write", {"reason": "we own this now"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body["version"], body["status"], body["source"], body["author"]) == (
        "0.1.1",
        "draft",
        "operator",
        "editor",
    )
    assert (body["adopted_from"], body["lineage_import"], body["published"]) == ("0.1.0", False, False)
    assert [f["path"] for f in body["files"]] == ["SKILL.md", "references/queues.md"]
    detail = (
        await app.client.get(
            f"/skill-library/{NAME}/versions/0.1.1", headers={"Authorization": "Bearer sk-read"}
        )
    ).json()
    assert detail["adopted_from"] == "0.1.0" and detail["reason"] == "we own this now"


@pytest.mark.parametrize(
    ("body", "status", "error"),
    [
        ({}, 422, None),
        ({"reason": ""}, 422, None),
        ({"reason": "x" * 2001}, 422, None),
        ({"reason": "   "}, 422, "reason_required"),
        ({"reason": "ok", "publish": True}, 422, None),
    ],
    ids=["missing", "empty", "too-long", "blank", "no-publish-field"],
)
async def test_the_route_refuses_a_missing_or_blank_reason(
    app: App, body: dict[str, Any], status: int, error: str | None
) -> None:
    await _import(app.settings, app.store)
    resp = await app.adopt("sk-write", body)
    assert resp.status_code == status, resp.text
    if error:
        assert resp.json()["error"] == error
    assert await get_skill_library_store(app.settings).version_ids("acme", NAME) == ["0.1.0"]


async def test_the_route_maps_each_refusal_to_its_code(app: App) -> None:
    await _import(app.settings, app.store)
    await _import(app.settings, app.store, _files(BODY + "\nUpstream moved.\n"), tree="t2")
    stale = await app.adopt("sk-write")
    assert (stale.status_code, stale.json()["error"]) == (409, "parent_changed")

    house_files = {"SKILL.md": skill_md("house-rules", "Ours.").decode()}
    await _save(app.settings, app.store, house_files, source="operator", parent=None, name="house-rules")
    house = await app.adopt("sk-write", path="/skill-library/house-rules/versions/0.1.0/adopt")
    assert (house.status_code, house.json()["error"]) == (409, "not_imported")

    missing = await app.adopt("sk-write", path="/skill-library/nothing-here/versions/0.1.0/adopt")
    assert (missing.status_code, missing.json()["error"]) == (404, "not_found")


def test_only_the_operator_route_can_adopt() -> None:
    """Adopting is a person vouching for text. Across every server source tree, `adopt` is called
    and `adopted_from=` is passed in exactly two places: the operator route, and `adopt` itself
    building its provenance -- so no agent tool, worker task or other save path reaches either."""
    import ast

    root = Path(__file__).resolve().parents[2]
    found: set[str] = set()
    for tree in ("packages/harness/src", "apps"):
        for path in (root / tree).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if called == "adopt" or any(k.arg == "adopted_from" for k in node.keywords):
                    found.add(path.relative_to(root).as_posix())
    assert found == {
        "apps/api/src/felix_api/routes/skill_library.py",
        "packages/harness/src/felix/skills/library.py",
    }
