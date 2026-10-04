"""Imported skills against their origin: the bounded diff, the check, the update, the listing and
the worker's periodic sweep.

GitHub is `tests/skill_import_fake.py` at the transport, handed in through `http=`; the stores are
the `memory://` twins. Each test that moves upstream pushes the whole tree again, as a commit does.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from felix.config import Settings
from felix.skills import github, importer, library, upstream
from felix.skills.bundle_diff import Content, DiffBuilder, diff_bundles, git_blob_id
from felix.skills.format import MAX_DESCRIPTION_CHARS
from felix.skills.library_store import get_skill_library_store
from felix.skills.sighting_store import get_sighting_store
from felix.skills.upstream_store import get_upstream_store
from felix.storage import MemoryObjectStore

from tests.skill_import_fake import FakeRepos, blob_sha, skill_md

REPO = "acme/skills"
NAME = "invoice-triage"
SOURCE = f"github:{REPO}/skills/{NAME}"
DAY = importer.DAY_MS
T0 = 1_750_000_000_000
LOGO = b"\x89PNG\r\n\x1a\nfake"
QUEUES = b"# Queues\n\nfinance, ops\n"


@pytest.fixture
def settings() -> Settings:
    return Settings(database_url="memory://skill-upstream")


@pytest.fixture
def store() -> MemoryObjectStore:
    return MemoryObjectStore()


def _tree(skills: dict[str, dict[str, bytes]]) -> dict[str, bytes]:
    return {f"skills/{name}/{path}": data for name, files in skills.items() for path, data in files.items()}


def _files(
    body: str = "", *, queues: bytes = QUEUES, logo: bytes = LOGO, name: str = NAME
) -> dict[str, bytes]:
    return {
        "SKILL.md": skill_md(name, body=body) if body else skill_md(name),
        "references/queues.md": queues,
        "assets/logo.png": logo,
    }


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> FakeRepos:
    fake = FakeRepos()
    fake.serve(monkeypatch)
    fake.push(REPO, _tree({NAME: _files()}))
    return fake


def _deps(http: Any, store: Any, *, clock: Any = None, charge: Any = None) -> importer.ImportDeps:
    return importer.ImportDeps(
        http=http,
        object_store=store,
        charge=charge or importer.uncharged(),
        **({"clock": clock} if clock is not None else {}),
    )


async def _import(settings: Settings, store: Any, gh: FakeRepos, *, source: str = SOURCE, **kw: Any) -> Any:
    clock, tenant = kw.pop("clock", None), kw.pop("tenant", "acme")
    async with gh.client() as http:
        return await importer.import_skill(
            settings, tenant, source=source, by="ops", deps=_deps(http, store, clock=clock), **kw
        )


async def _check(
    settings: Settings, store: Any, gh: FakeRepos, name: str = NAME, **kw: Any
) -> dict[str, Any]:
    clock, charge, tenant = kw.pop("clock", None), kw.pop("charge", None), kw.pop("tenant", "acme")
    async with gh.client() as http:
        deps = _deps(http, store, clock=clock, charge=charge)
        return await upstream.check_upstream(settings, tenant, name, deps=deps, **kw)


async def _update(settings: Settings, store: Any, gh: FakeRepos, name: str = NAME, **kw: Any) -> Any:
    clock, tenant = kw.pop("clock", None), kw.pop("tenant", "acme")
    async with gh.client() as http:
        return await upstream.update_skill(
            settings, tenant, name, by="ops", deps=_deps(http, store, clock=clock), **kw
        )


async def _publish(settings: Settings, store: Any, version: str, name: str = NAME) -> None:
    await library.publish(settings, "acme", name, version, by="ops", object_store=store)


def _blob_reads(gh: FakeRepos, since: int = 0) -> list[str]:
    return [p for p in gh.paths()[since:] if "/git/blobs/" in p]


# -- the diff ------------------------------------------------------------------------------------


def test_a_diff_lists_added_removed_and_modified_files_and_omits_the_rest() -> None:
    old = {"SKILL.md": b"a\nb\n", "references/gone.md": b"bye\n", "references/same.md": b"same\n"}
    new = {"SKILL.md": b"a\nc\n", "references/new.md": b"hi\n", "references/same.md": b"same\n"}

    result = diff_bundles(old, new)

    assert [(f["path"], f["change"]) for f in result["files"]] == [
        ("SKILL.md", "modified"),
        ("references/gone.md", "removed"),
        ("references/new.md", "added"),
    ]
    modified, removed, added = result["files"]
    assert modified["diff"].splitlines() == [
        "--- a/SKILL.md",
        "+++ b/SKILL.md",
        "@@ -1,2 +1,2 @@",
        " a",
        "-b",
        "+c",
    ]
    assert removed["diff"].startswith("--- a/references/gone.md\n+++ /dev/null\n")
    assert added["diff"].startswith("--- /dev/null\n+++ b/references/new.md\n")
    assert (added["old_size"], added["new_size"], removed["old_size"], removed["new_size"]) == (
        None,
        3,
        4,
        None,
    )
    assert result["diff_truncated"] is False


def test_a_binary_asset_is_reported_by_size_alone() -> None:
    result = diff_bundles({"assets/logo.png": b"\x89PNG-old"}, {"assets/logo.png": b"\x89PNG-newer"})
    (logo,) = result["files"]
    assert (logo["change"], logo["binary"], logo["diff"]) == ("modified", True, None)
    assert (logo["old_size"], logo["new_size"], logo["truncated"]) == (8, 10, False)
    assert result["diff_truncated"] is False, "a binary file's diff is never left out: it never has one"


def test_a_long_diff_is_cut_at_a_line_boundary_per_file() -> None:
    new = "".join(f"line {i}\n" for i in range(200)).encode()

    result = diff_bundles({}, {"references/long.md": new, "references/short.md": b"x\n"}, max_file_chars=300)

    long, short = result["files"]
    assert long["truncated"] is True and len(long["diff"]) <= 300 and long["diff"].endswith("\n")
    assert short["truncated"] is False and short["diff"].endswith("+x\n")
    assert result["diff_truncated"] is True


def test_past_the_total_budget_a_file_is_listed_without_a_diff() -> None:
    new = {f"references/{n}.md": "".join(f"{n} {i}\n" for i in range(40)).encode() for n in "abc"}

    result = diff_bundles({}, new, max_file_chars=10_000, max_total_chars=500)

    a, b, c = result["files"]
    assert a["diff"] is not None and a["truncated"] is False
    assert b["truncated"] is True and len(a["diff"]) + len(b["diff"]) <= 500
    assert (c["change"], c["diff"], c["truncated"], c["new_size"]) == (
        "added",
        None,
        True,
        len(new["references/c.md"]),
    )
    assert result["diff_truncated"] is True


def test_text_that_was_not_read_is_listed_without_a_diff() -> None:
    builder = DiffBuilder()
    builder.add("references/x.md", Content(3, "ab\n"), Content(9))
    (x,) = builder.result()["files"]
    assert (x["diff"], x["truncated"], x["new_size"]) == (None, True, 9)


def test_a_side_past_the_input_bound_is_never_diffed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The output caps bound the answer, not difflib's work: an over-size side is not read."""
    from felix.skills import bundle_diff

    read: list[str] = []
    real = bundle_diff.difflib.unified_diff

    def spy(a: Any, b: Any, **kw: Any) -> Any:
        read.append(kw["tofile"])
        return real(a, b, **kw)

    monkeypatch.setattr(bundle_diff.difflib, "unified_diff", spy)
    wide = b"y" * (bundle_diff.MAX_DIFF_INPUT_BYTES + 1)
    long = b"x\n" * bundle_diff.MAX_DIFF_INPUT_LINES
    old = {"references/wide.md": b"a\n", "references/long.md": long, "references/small.md": b"a\n"}
    new = {"references/wide.md": wide, "references/long.md": b"b\n", "references/small.md": b"b\n"}

    result = diff_bundles(old, new)

    assert read == ["b/references/small.md"], "only the small file reached difflib"
    by_path = {f["path"]: f for f in result["files"]}
    for path, size in (("references/wide.md", len(wide)), ("references/long.md", 2)):
        assert (by_path[path]["diff"], by_path[path]["truncated"], by_path[path]["new_size"]) == (
            None,
            True,
            size,
        )
    assert result["diff_truncated"] is True


async def test_a_check_never_fetches_a_text_past_the_input_bound(
    settings: Settings, store: MemoryObjectStore, gh: FakeRepos
) -> None:
    from felix.skills.bundle_diff import MAX_DIFF_INPUT_BYTES

    await _import(settings, store, gh)
    big = b"z" * (MAX_DIFF_INPUT_BYTES + 1)
    gh.push(REPO, _tree({NAME: {**_files(), "references/big.md": big}}))
    before = len(gh.requests)

    found = await _check(settings, store, gh)

    assert _blob_reads(gh, before) == []
    (entry,) = found["diff"]["files"]
    assert (entry["path"], entry["diff"], entry["new_size"]) == ("references/big.md", None, len(big))


def test_a_git_blob_id_is_computed_in_the_trees_object_format() -> None:
    data = b"# Queues\n"
    assert git_blob_id(data, "0" * 40) == blob_sha(data)
    assert git_blob_id(data, "0" * 64) == hashlib.sha256(b"blob 9\0" + data).hexdigest()


# -- the check -----------------------------------------------------------------------------------


async def test_a_check_diffs_against_the_live_version_not_the_newest(
    settings: Settings, store: MemoryObjectStore, gh: FakeRepos
) -> None:
    await _import(settings, store, gh)
    await _publish(settings, store, "0.1.0")
    gh.push(REPO, _tree({NAME: _files(queues=b"# Queues\n\nfinance\n")}))
    assert (await _import(settings, store, gh)).version["version"] == "0.1.1"
    commit = gh.push(REPO, _tree({NAME: _files(queues=b"# Queues\n\nfinance, ops, legal\n")}))

    found = await _check(settings, store, gh)

    assert found["current"]["version"] == "0.1.1" and found["current"]["live_version"] == "0.1.0"
    assert found["upstream"]["commit"] == commit and found["update_available"] is True
    assert found["diff"]["compared_with"] == "0.1.0", "against what agents are running, not the draft"
    (queues,) = found["diff"]["files"]
    assert queues["path"] == "references/queues.md"
    assert "-finance, ops\n" in queues["diff"] and "+finance, ops, legal\n" in queues["diff"]


async def test_with_nothing_live_a_check_diffs_against_the_newest(
    settings: Settings, store: MemoryObjectStore, gh: FakeRepos
) -> None:
    await _import(settings, store, gh)
    gh.push(REPO, _tree({NAME: _files(queues=b"# Queues\n\nfinance\n")}))
    await _import(settings, store, gh)
    gh.push(REPO, _tree({NAME: _files(queues=b"# Queues\n\nlegal\n")}))

    found = await _check(settings, store, gh)

    assert found["current"]["live_version"] is None
    assert found["diff"]["compared_with"] == "0.1.1"
    assert "-finance\n" in found["diff"]["files"][0]["diff"], "the newest version's text, not 0.1.0's"


async def test_a_check_fetches_only_text_files_whose_blob_ids_moved(
    settings: Settings, store: MemoryObjectStore, gh: FakeRepos
) -> None:
    await _import(settings, store, gh)
    changed_logo = LOGO + b"-v2"
    files = {**_files(logo=changed_logo), "references/extra.md": b"# Extra\n"}
    del files["references/queues.md"]
    gh.push(REPO, _tree({NAME: files}))
    before = len(gh.requests)

    found = await _check(settings, store, gh)

    assert _blob_reads(gh, before) == [f"/repos/{REPO}/git/blobs/{blob_sha(b'# Extra\n')}"], (
        "the unchanged SKILL.md, the removed file and the binary asset are never read"
    )
    by_path = {f["path"]: f for f in found["diff"]["files"]}
    assert set(by_path) == {"assets/logo.png", "references/extra.md", "references/queues.md"}
    assert by_path["assets/logo.png"] | {} == {
        "path": "assets/logo.png",
        "change": "modified",
        "binary": True,
        "old_size": len(LOGO),
        "new_size": len(changed_logo),
        "diff": None,
        "truncated": False,
    }
    assert by_path["references/queues.md"]["change"] == "removed"
    assert by_path["references/extra.md"]["change"] == "added"


async def test_a_description_the_import_clamps_is_no_change(
    settings: Settings, store: MemoryObjectStore, gh: FakeRepos
) -> None:
    long = skill_md(NAME, description="x" * (MAX_DESCRIPTION_CHARS + 50))
    gh.push(REPO, _tree({NAME: {**_files(), "SKILL.md": long}}))
    await _import(settings, store, gh)

    found = await _check(settings, store, gh)

    assert found["update_available"] is False
    assert found["diff"]["files"] == [], "read, because the stored bytes are clamped, and found equal"


async def test_only_an_imported_skill_has_an_upstream(
    settings: Settings, store: MemoryObjectStore, gh: FakeRepos
) -> None:
    await library.save_draft(
        settings,
        "acme",
        files={"SKILL.md": skill_md("house-rules").decode()},
        provenance=library.DraftProvenance(source="operator", author="ops"),
        object_store=store,
    )
    with pytest.raises(github.SkillNotImported) as caught:
        await _check(settings, store, gh, "house-rules")
    assert caught.value.code == "not_imported"
    with pytest.raises(library.SkillNotFound):
        await _check(settings, store, gh, "no-such-skill")
    with pytest.raises(github.SkillNotImported):
        await _update(settings, store, gh, "house-rules")
    assert gh.requests == [], "refused before GitHub is asked"


async def test_checking_starts_the_cooldown_that_an_update_then_waits_out(
    store: MemoryObjectStore, gh: FakeRepos
) -> None:
    open_ = Settings(database_url="memory://skill-upstream")
    cooled = Settings(database_url="memory://skill-upstream", skill_import_min_age_days=7)
    await _import(open_, store, gh, clock=lambda: T0 - 30 * DAY)
    gh.push(REPO, _tree({NAME: _files(queues=b"# Queues\n\nlegal\n")}))

    found = await _check(cooled, store, gh, clock=lambda: T0)

    now = found["upstream"]
    assert (now["first_seen_at"], now["eligible_at"], now["eligible"]) == (T0, T0 + 7 * DAY, False)
    with pytest.raises(github.ImportTooRecent):
        await _update(cooled, store, gh, clock=lambda: T0 + 7 * DAY - 1)
    result, _ = await _update(cooled, store, gh, clock=lambda: T0 + 7 * DAY)
    assert result.version["version"] == "0.1.1", "counted from the check, not from the update"


async def test_a_check_of_the_stored_ref_is_recorded_and_one_of_another_ref_is_not(
    settings: Settings, store: MemoryObjectStore, gh: FakeRepos
) -> None:
    await _import(settings, store, gh, clock=lambda: T0)
    imported = await _row(settings, "acme", NAME)
    side = gh.push(REPO, _tree({NAME: _files(queues=b"side\n")}), ref="next")
    gh.repos[REPO].history.remove(side)
    gh.repos[REPO].history.insert(0, side)  # an ancestor of main, as a merged branch is

    other = await _check(settings, store, gh, ref="next", clock=lambda: T0 + 1)
    assert other["upstream"]["commit"] == side and other["upstream"]["ref"] == "next"
    assert await _row(settings, "acme", NAME) == imported, "a what-if"

    await _check(settings, store, gh, clock=lambda: T0 + 2)
    assert (await _row(settings, "acme", NAME))["checked_at"] == T0 + 2


async def test_a_ref_named_for_a_check_is_judged_as_an_imports(
    settings: Settings, store: MemoryObjectStore, gh: FakeRepos
) -> None:
    await _import(settings, store, gh)
    planted = gh.fork_commit(REPO, _tree({NAME: _files(queues=b"planted\n")}))
    with pytest.raises(github.ImportCommitNotInRepo):
        await _check(settings, store, gh, ref=planted)
    with pytest.raises(github.ImportSourceInvalid):
        await _check(settings, store, gh, ref="../main")


async def test_every_github_call_of_a_check_is_charged(
    settings: Settings, store: MemoryObjectStore, gh: FakeRepos
) -> None:
    await _import(settings, store, gh)
    gh.push(REPO, _tree({NAME: _files(queues=b"changed\n")}))
    charged: list[int] = []

    async def charge() -> None:
        charged.append(1)

    before = len(gh.requests)
    await _check(settings, store, gh, charge=charge)
    assert len(charged) == len(gh.requests) - before > 0


async def test_an_unchanged_import_of_another_ref_leaves_the_record_alone(
    settings: Settings, store: MemoryObjectStore, gh: FakeRepos
) -> None:
    await _import(settings, store, gh, clock=lambda: T0)
    gh.tag(REPO, "v1", gh.repos[REPO].refs["main"])

    again = await _import(settings, store, gh, ref="v1", clock=lambda: T0 + 5)

    assert again.unchanged is True
    row = await _row(settings, "acme", NAME)
    assert (row["origin_ref"], row["checked_at"]) == ("main", T0), "a v1 import is not main's state"


async def test_a_stored_default_branch_stays_the_branch_beside_a_tag_of_its_name(
    settings: Settings, store: MemoryObjectStore, gh: FakeRepos
) -> None:
    """Imported with no ref, the skill stores `main`. A tag named `main` added later must neither
    make that ambiguous nor stand in for the branch."""
    await _import(settings, store, gh)
    old = gh.repos[REPO].refs["main"]
    tip = gh.push(REPO, _tree({NAME: _files(queues=b"# Queues\n\nlegal\n")}))
    gh.tag(REPO, "main", old)

    found = await _check(settings, store, gh)
    assert (found["upstream"]["commit"], found["update_available"]) == (tip, True)
    result, _ = await _update(settings, store, gh)
    assert result.version["origin_commit"] == tip and result.version["origin_ref"] == "main"


# -- the update ----------------------------------------------------------------------------------


async def test_an_update_saves_a_draft_and_diffs_it_against_the_live_version(
    settings: Settings, store: MemoryObjectStore, gh: FakeRepos
) -> None:
    from felix.audit import store as audit_store

    await _import(settings, store, gh)
    await _publish(settings, store, "0.1.0")
    commit = gh.push(REPO, _tree({NAME: _files(queues=b"# Queues\n\nlegal\n")}))

    result, diff = await _update(settings, store, gh)

    assert (result.unchanged, result.version["version"], result.version["status"]) == (
        False,
        "0.1.1",
        "draft",
    )
    assert result.version["origin_commit"] == commit
    skill = await get_skill_library_store(settings).get_skill("acme", NAME)
    assert skill is not None and skill["live_version"] == "0.1.0", "never published by an update"
    assert diff["compared_with"] == "0.1.0"
    assert [f["path"] for f in diff["files"]] == ["references/queues.md"]
    await audit_store.flush_pending(settings)
    events, _ = await audit_store.list_events(settings, "acme", event_type="skill_draft_saved", limit=10)
    reasons = sorted(e["payload_json"]["reason"].split(" ")[0] for e in events)
    assert reasons == ["imported", "updated"]

    again, same = await _update(settings, store, gh)
    assert again.unchanged is True and again.version["version"] == "0.1.1"
    assert same["compared_with"] == "0.1.0" and [f["path"] for f in same["files"]] == ["references/queues.md"]


async def test_with_nothing_live_an_update_diffs_against_the_version_it_built_on(
    settings: Settings, store: MemoryObjectStore, gh: FakeRepos
) -> None:
    await _import(settings, store, gh)
    gh.push(REPO, _tree({NAME: _files(queues=b"# Queues\n\nlegal\n")}))

    _, diff = await _update(settings, store, gh)

    assert diff["compared_with"] == "0.1.0" and [f["path"] for f in diff["files"]] == ["references/queues.md"]


async def test_an_update_keeps_the_imports_origin_rules(
    settings: Settings, store: MemoryObjectStore, gh: FakeRepos
) -> None:
    """The newest version is an operator's edit of the import: nothing to update from."""
    await _import(settings, store, gh)
    files = await library.read_version_files(settings, "acme", NAME, "0.1.0", object_store=store)
    await library.save_draft(
        settings,
        "acme",
        files={**files, "references/queues.md": "# Queues\n\nours\n"},
        provenance=library.DraftProvenance(source="operator", author="ops"),
        name=NAME,
        parent="0.1.0",
        object_store=store,
    )
    with pytest.raises(github.SkillNotImported):
        await _update(settings, store, gh)


# -- the listing ---------------------------------------------------------------------------------


async def _three(settings: Settings, store: Any, gh: FakeRepos) -> None:
    gh.push(REPO, _tree({n: _files(name=n) for n in ("alpha", "beta", "gamma")}))
    for n in ("alpha", "beta", "gamma"):
        await _import(settings, store, gh, source=f"github:{REPO}/skills/{n}")


async def _outdated(settings: Settings, gh: FakeRepos, **kw: Any) -> dict[str, Any]:
    charge, clock = kw.pop("charge", None), kw.pop("clock", None)
    async with gh.client() as http:
        deps = _deps(http, None, clock=clock, charge=charge)
        return await upstream.outdated(settings, "acme", deps=deps, **kw)


async def test_a_listing_pages_and_checks_one_repository_once(
    settings: Settings, store: MemoryObjectStore, gh: FakeRepos
) -> None:
    await _three(settings, store, gh)
    gh.push(
        REPO,
        _tree(
            {
                "alpha": _files(name="alpha", queues=b"moved\n"),
                **{n: _files(name=n) for n in ("beta", "gamma")},
            }
        ),
    )
    before = len(gh.requests)

    first = await _outdated(settings, gh, limit=2)

    assert [(i["name"], i["update_available"]) for i in first["items"]] == [("alpha", True), ("beta", False)]
    assert first["next_cursor"] == "beta" and first["stopped"] is None
    trees = [p for p in gh.paths()[before:] if "/git/trees/" in p]
    assert len(trees) == 1, "two skills of one repository and ref resolve once"
    assert _blob_reads(gh, before) == [], "a listing reads no file"
    rest = await _outdated(settings, gh, after="beta", limit=2)
    assert [i["name"] for i in rest["items"]] == ["gamma"] and rest["next_cursor"] is None


async def test_a_listing_is_capped_whatever_limit_it_is_asked_for(
    settings: Settings, store: MemoryObjectStore, gh: FakeRepos, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _three(settings, store, gh)
    monkeypatch.setattr(upstream, "MAX_OUTDATED", 2)
    listing = await _outdated(settings, gh, limit=1_000)
    assert [i["name"] for i in listing["items"]] == ["alpha", "beta"] and listing["next_cursor"] == "beta"


async def test_a_cached_listing_asks_github_nothing(
    settings: Settings, store: MemoryObjectStore, gh: FakeRepos
) -> None:
    await _three(settings, store, gh)
    await library.save_draft(
        settings,
        "acme",
        files={"SKILL.md": skill_md("house-rules").decode()},
        provenance=library.DraftProvenance(source="operator", author="ops"),
        object_store=store,
    )
    before = len(gh.requests)
    listing = await _outdated(settings, gh, refresh=False)
    assert gh.requests[before:] == []
    assert [i["name"] for i in listing["items"]] == ["alpha", "beta", "gamma"], "imports only"
    assert all(i["checked_at"] is not None and i["update_available"] is False for i in listing["items"])


async def test_a_spent_budget_ends_a_listing_part_way_or_refuses_it(
    settings: Settings, store: MemoryObjectStore, gh: FakeRepos
) -> None:
    for n in ("alpha", "beta", "gamma"):
        gh.push(f"acme/{n}", _tree({n: _files(name=n)}))
        await _import(settings, store, gh, source=f"github:acme/{n}/skills/{n}")
    left = [4]

    async def charge() -> None:
        if left[0] <= 0:
            raise github.ImportBudgetExhausted("spent")
        left[0] -= 1

    # One check of a bare ref in a repository of its own is four calls (repository, tag, branch,
    # tree); the second skill's first call is refused.
    listing = await _outdated(settings, gh, charge=charge)
    assert [i["name"] for i in listing["items"]] == ["alpha"]
    assert (listing["stopped"], listing["next_cursor"]) == ("rate_limited", "alpha")

    with pytest.raises(github.ImportBudgetExhausted):
        await _outdated(settings, gh, charge=charge, after="alpha")


async def test_a_refused_check_is_listed_with_its_code_and_the_last_good_state(
    settings: Settings, store: MemoryObjectStore, gh: FakeRepos
) -> None:
    await _import(settings, store, gh, clock=lambda: T0)
    good = await _row(settings, "acme", NAME)
    gh.rate_limited = True

    listing = await _outdated(settings, gh, clock=lambda: T0 + 5)

    (item,) = listing["items"]
    assert (item["error"], item["checked_at"]) == ("upstream_rate_limited", T0 + 5)
    assert item["upstream_commit"] == good["upstream_commit"]


# -- the sweep -----------------------------------------------------------------------------------


def _swept(hours: int = 6, **kw: Any) -> Settings:
    return Settings(database_url="memory://skill-upstream", skill_import_check_hours=hours, **kw)


async def _sweep(
    settings: Settings, gh: FakeRepos, *, at: int, limiter: Any = None, batch: int = upstream.SWEEP_BATCH
) -> dict[str, int]:
    from felix.security.rate_limit import InMemoryRateLimiter

    async with gh.client() as http:
        # Uncharged here, and replaced by the sweep's own budget all the same.
        deps = importer.ImportDeps(http=http, clock=lambda: at, charge=importer.uncharged())
        return await upstream.run_upstream_checks(
            settings, limiter=limiter or InMemoryRateLimiter(), deps=deps, batch=batch
        )


async def _spread(settings: Settings, store: Any, gh: FakeRepos, skills: dict[str, tuple[str, ...]]) -> None:
    """Each skill in a repository of its own (`acme/<name>`), imported into its tenant at T0: no
    two checks share a resolve, so every check costs its own three calls."""
    for tenant, names in skills.items():
        for n in names:
            gh.push(f"acme/{n}", _tree({n: _files(name=n)}))
            await _import(
                settings, store, gh, source=f"github:acme/{n}/skills/{n}", tenant=tenant, clock=lambda: T0
            )


async def _row(settings: Settings, tenant: str, name: str) -> dict[str, Any]:
    rows = await get_upstream_store(settings).get(tenant, [name])
    assert name in rows, f"{tenant}/{name} has no upstream row"
    return rows[name]


async def test_the_sweep_is_off_at_zero(settings: Settings, store: MemoryObjectStore, gh: FakeRepos) -> None:
    await _import(settings, store, gh, clock=lambda: T0)
    before = len(gh.requests)
    counts = await _sweep(_swept(0), gh, at=T0 + 30 * DAY)
    assert counts["checked"] == 0 and gh.requests[before:] == []


async def test_the_sweep_checks_what_is_due_and_records_it(
    settings: Settings, store: MemoryObjectStore, gh: FakeRepos
) -> None:
    await _import(settings, store, gh, clock=lambda: T0)
    commit = gh.push(REPO, _tree({NAME: _files(queues=b"moved\n")}))

    assert (await _sweep(_swept(6), gh, at=T0 + 6 * 3_600_000 - 1))["checked"] == 0, "not due yet"
    counts = await _sweep(_swept(6), gh, at=T0 + 6 * 3_600_000)

    assert (counts["checked"], counts["updates"]) == (1, 1)
    row = await _row(settings, "acme", NAME)
    assert (row["upstream_commit"], row["checked_at"], row["error"]) == (commit, T0 + 6 * 3_600_000, None)


async def test_a_sighting_the_sweep_stamps_is_the_one_a_later_import_counts_from(
    store: MemoryObjectStore, gh: FakeRepos
) -> None:
    cooled = _swept(1, skill_import_min_age_days=7)
    await _import(Settings(database_url="memory://skill-upstream"), store, gh, clock=lambda: T0 - 30 * DAY)
    gh.push(REPO, _tree({NAME: _files(queues=b"# Queues\n\nlegal\n")}))

    await _sweep(cooled, gh, at=T0)

    row = await _row(cooled, "acme", NAME)
    assert row["first_seen_at"] == T0
    with pytest.raises(github.ImportTooRecent):
        await _update(cooled, store, gh, clock=lambda: T0 + 7 * DAY - 1)
    result, _ = await _update(cooled, store, gh, clock=lambda: T0 + 7 * DAY)
    assert result.unchanged is False, "the sweep's sighting started the clock nobody asked for"


async def test_the_sweep_spends_half_the_budget_and_stops(store: MemoryObjectStore, gh: FakeRepos) -> None:
    """Three calls a check (repository, default branch, tree). A tenant's share of 10 is 5: acme's
    alpha is checked, beta's third call is refused and acme is left out; globex's delta is checked,
    and gamma's first call meets the deployment's share of 16 (8) and ends the tick, so initech's
    epsilon is never tried."""
    from felix.security.rate_limit import InMemoryRateLimiter

    settings = _swept(1, skill_import_calls_per_hour=10, skill_import_calls_per_hour_total=16)
    await _spread(
        settings,
        store,
        gh,
        {"acme": ("alpha", "beta"), "globex": ("gamma", "delta"), "initech": ("epsilon",)},
    )
    limiter = InMemoryRateLimiter()
    before = len(gh.requests)

    counts = await _sweep(settings, gh, at=T0 + 3_600_000, limiter=limiter)

    assert (counts["checked"], counts["budget_stopped"]) == (2, 2)
    assert len(gh.requests) - before == 3 + 2 + 3, "every call charged, and none sent past a refusal"
    assert (await _row(settings, "acme", "alpha"))["checked_at"] == T0 + 3_600_000
    assert (await _row(settings, "globex", "delta"))["checked_at"] == T0 + 3_600_000
    assert (await _row(settings, "globex", "gamma"))["checked_at"] == T0, "the tick ended before it"
    assert (await _row(settings, "initech", "epsilon"))["checked_at"] == T0, "another tenant too"

    # A person still has the rest of acme's hour: 5 of its 10 calls.
    budget = importer.github_call_budget(limiter, settings, "acme")
    for _ in range(5):
        await budget()
    with pytest.raises(github.ImportBudgetExhausted):
        await budget()


async def test_a_tenant_past_its_share_does_not_starve_the_others(
    store: MemoryObjectStore, gh: FakeRepos
) -> None:
    """acme has more due skills than a tick tries and a share (4) of one check; the tick, three
    wide, still reaches globex -- whose skills sort after all of acme's."""
    settings = _swept(1, skill_import_calls_per_hour=8)
    await _spread(settings, store, gh, {"acme": ("a1", "a2", "a3", "a4"), "globex": ("g1",)})

    counts = await _sweep(settings, gh, at=T0 + 3_600_000, batch=3)

    assert (counts["checked"], counts["budget_stopped"]) == (2, 1)
    assert (await _row(settings, "globex", "g1"))["checked_at"] == T0 + 3_600_000
    assert [(await _row(settings, "acme", n))["checked_at"] for n in ("a2", "a3", "a4")] == [T0] * 3


async def test_a_sweep_resolves_a_repository_once_per_tenant(
    settings: Settings, store: MemoryObjectStore, gh: FakeRepos
) -> None:
    gh.push(REPO, _tree({n: _files(name=n) for n in ("alpha", "beta")}))
    for tenant, n in (("acme", "alpha"), ("acme", "beta"), ("globex", "alpha")):
        await _import(
            settings, store, gh, source=f"github:{REPO}/skills/{n}", tenant=tenant, clock=lambda: T0
        )
    before = len(gh.requests)

    assert (await _sweep(_swept(1), gh, at=T0 + 3_600_000))["checked"] == 3
    trees = [p for p in gh.paths()[before:] if "/git/trees/" in p]
    assert len(trees) == 2, "acme's two skills share one resolve; globex resolves its own"


async def test_the_sweep_keeps_tenants_sightings_apart(store: MemoryObjectStore, gh: FakeRepos) -> None:
    from felix.skills.sighting_store import InMemorySightingStore

    plain = Settings(database_url="memory://skill-upstream")
    await _import(plain, store, gh, clock=lambda: T0)
    gh.push("globex/skills", _tree({"refunds": _files(name="refunds")}))
    await _import(
        plain, store, gh, source="github:globex/skills/skills/refunds", tenant="globex", clock=lambda: T0
    )
    gh.push(REPO, _tree({NAME: _files(queues=b"moved\n")}))

    counts = await _sweep(_swept(1), gh, at=T0 + 3_600_000)

    assert counts["checked"] == 2
    sightings = get_sighting_store(plain)
    assert isinstance(sightings, InMemorySightingStore)
    seen = {(tenant, at) for (tenant, source, _), at in sightings._rows.items() if source == SOURCE}
    assert seen == {("acme", T0), ("acme", T0 + 3_600_000)}, "globex never saw acme's files"


async def test_an_origin_taken_off_the_allowlist_is_refused_without_a_call(
    store: MemoryObjectStore, gh: FakeRepos
) -> None:
    plain = Settings(database_url="memory://skill-upstream")
    await _import(plain, store, gh, clock=lambda: T0)
    rebound = _swept(1, skill_import_sources="globex=github:acme/*")
    before = len(gh.requests)

    counts = await _sweep(rebound, gh, at=T0 + 3_600_000)

    assert (counts["checked"], counts["failed"]) == (0, 1)
    assert gh.requests[before:] == [], "refused by the allowlist before any call"
    row = await _row(plain, "acme", NAME)
    assert (row["error"], row["checked_at"]) == ("source_not_allowed", T0 + 3_600_000)


async def test_githubs_own_rate_limit_ends_the_tick(store: MemoryObjectStore, gh: FakeRepos) -> None:
    settings = _swept(1)
    await _spread(settings, store, gh, {"acme": ("alpha",), "globex": ("gamma",)})
    gh.rate_limited = True
    before = len(gh.requests)

    counts = await _sweep(settings, gh, at=T0 + 3_600_000)

    assert len(gh.requests) - before == 1, "the shared token's limit is everyone's: no second try"
    assert (counts["failed"], counts["budget_stopped"]) == (0, 1)
    assert (await _row(settings, "globex", "gamma"))["checked_at"] == T0


async def test_a_folder_past_the_import_caps_is_refused_not_offered(
    settings: Settings, store: MemoryObjectStore, gh: FakeRepos, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _import(settings, store, gh, clock=lambda: T0)
    gh.push(REPO, _tree({NAME: _files(queues=b"moved\n")}))
    monkeypatch.setattr(importer, "MAX_BUNDLE_FILES", 1)

    counts = await _sweep(_swept(1), gh, at=T0 + 3_600_000)

    assert counts["failed"] == 1
    listing = await _outdated(settings, gh, refresh=False)
    (item,) = listing["items"]
    assert (item["error"], item["update_available"]) == ("source_too_large", False)


async def test_a_skill_that_stops_being_an_import_keeps_its_row_and_comes_back(
    settings: Settings, store: MemoryObjectStore, gh: FakeRepos
) -> None:
    await _import(settings, store, gh, clock=lambda: T0)
    files = await library.read_version_files(settings, "acme", NAME, "0.1.0", object_store=store)
    await library.save_draft(
        settings,
        "acme",
        files={**files, "references/queues.md": "# Queues\n\nours\n"},
        provenance=library.DraftProvenance(source="operator", author="ops"),
        name=NAME,
        parent="0.1.0",
        object_store=store,
    )
    before = len(gh.requests)
    counts = await _sweep(_swept(1), gh, at=T0 + 3_600_000)
    assert counts["not_imported"] == 1 and gh.requests[before:] == []
    assert (await _row(settings, "acme", NAME))["error"] == "not_imported"

    # The operator's draft is rejected: the import is the head again, and the sweep checks it.
    await library.reject(settings, "acme", NAME, "0.1.1", by="ops", note="no")
    counts = await _sweep(_swept(1), gh, at=T0 + 2 * 3_600_000)
    assert counts["checked"] == 1
    row = await _row(settings, "acme", NAME)
    assert (row["error"], row["checked_at"]) == (None, T0 + 2 * 3_600_000)


async def test_one_sweep_runs_at_a_time(settings: Settings, store: MemoryObjectStore, gh: FakeRepos) -> None:
    from felix.skills.quality_store import sweep_lock

    await _import(settings, store, gh, clock=lambda: T0)
    async with sweep_lock(settings, name=upstream.SWEEP_LEASE, lease_ms=60_000) as held:
        assert held is not None
        counts = await _sweep(_swept(1), gh, at=T0 + 3_600_000)
    assert counts["skipped"] == 1 and counts["checked"] == 0
    async with sweep_lock(settings) as jobs:
        assert jobs is not None, "the skill_jobs lease is another row"
        assert (await _sweep(_swept(1), gh, at=T0 + 3_600_000))["checked"] == 1
