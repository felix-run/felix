"""The tenant skill library: save a draft, publish it through a gate, roll back, reject, archive.

A skill body is returned by `activate_skill` as *instructions* -- a higher-trust surface than
recalled memory, which is fenced as reference. So an agent's skill is a draft until something
publishes it, and publishing runs a gate no setting can open past: a failing security scan
blocks, always. The publish policy raises the bar from there (`load_publish_policy`: the
tenant's `skill_policy` row, else `FELIX_SKILL_PUBLISH_*`), including requiring a succeeded
evaluation of the version. Every state change is an audit event.

Versions are immutable semver. A save reserves its version row first and writes the bytes
after, so the primary key is the lock: two saves racing to `0.1.1` collide on the row rather
than both writing the same object keys, where the loser's bytes would have replaced the
winner's under a row that described the winner. Rollback is a pointer move to a version that
was once published.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cmp_to_key
from typing import Any, Literal

from felix.config import Settings
from felix.logging_setup import loggable
from felix.skills.binary import decode_base64, encode_base64, is_binary_asset_path
from felix.skills.format import ValidationIssue, validate_skill_bundle
from felix.skills.library_store import (
    ANY_LIVE,
    MAX_VERSIONS_PER_SKILL,
    ORIGIN_COLUMNS,
    ExpectedLive,
    ImportOrigin,
    SkillLibraryStore,
    SkillLiveMismatch,
    SkillPendingFull,
    SkillStateConflict,
    SkillStatus,
    SkillVersionExists,
    get_skill_library_store,
    is_rejected,
    library_object_key,
)
from felix.skills.publish_gate import (
    PublishPolicy,
    Verdict,
    assess,
    evaluate_files,
    gate_scenario_source,
    gate_source,
    policy_for_source,
)
from felix.skills.semver import SemverBump, compare_semver, resolve_next_semver

logger = logging.getLogger("felix.skills.library")

now_ms = lambda: int(time.time() * 1000)

# `import`: fetched from an external source (`skills/importer.py`) at a person's request.
# Third-party text, so the gate treats it at least as strictly as an agent's draft
# (`publish_gate.policy_for_source`, `publish_gate.gate_scenario_source`).
SkillSourceKind = Literal["agent", "operator", "import"]

# Strict `major.minor.patch`: a version is interpolated into an object key, and the loader's
# own key-segment rule (`loader._VERSION_RE`) is looser than this.
VERSION_RE = re.compile(r"^\d{1,6}\.\d{1,6}\.\d{1,6}\Z")
# Saves that lose a race to the same next version are retried this many times before the
# caller is told; each retry re-reads the versions and bumps past the winner.
_SAVE_ATTEMPTS = 3
_REASON_LIMIT = 2000
# The copy rule (`_lineage_import`) ignores a file shorter than this: under 32 characters of
# normalized text (or 32 bytes of a binary asset) is the boilerplate many skills share -- an empty
# file, `[]`, a license id, a heading, a coding line -- not evidence that third-party text was
# copied, and too short to hold more than a phrase. Matching it would taint every save that
# carries one. Such a file is still scanned at save and screened at activation like any other.
COPY_FLOOR_CHARS = 32


class _MustNotExist:
    """`save_draft(expect_newest=MUST_NOT_EXIST)`: the skill may hold no version yet."""

    def __repr__(self) -> str:
        return "MUST_NOT_EXIST"


MUST_NOT_EXIST = _MustNotExist()


class SkillLibraryError(Exception):
    """Base for every refusal the library makes. `code` is stable; routes map it to a status."""

    code = "skill_library_error"


class SkillBundleInvalid(SkillLibraryError):
    code = "invalid_bundle"

    def __init__(self, issues: list[ValidationIssue]) -> None:
        self.issues = issues
        super().__init__("; ".join(f"{i.path}: {i.message}" for i in issues) or "invalid bundle")


class SkillNameShadowed(SkillLibraryError):
    """The name belongs to the host catalog (or an operator-uploaded object-store skill)."""

    code = "name_shadows_host_skill"


class SkillNotFound(SkillLibraryError):
    code = "not_found"


class SkillVersionConflict(SkillLibraryError):
    """The version exists, is not newer than every existing one, or changed state underneath."""

    code = "version_conflict"


class SkillExists(SkillLibraryError):
    """A create named a skill the library already holds; a new version names its parent."""

    code = "skill_exists"


class SkillParentChanged(SkillLibraryError):
    """The skill moved past the version a save was edited from -- another version was saved,
    or the one a reviewer was shown is no longer the one it would build on."""

    code = "parent_changed"


class SkillParentRejected(SkillLibraryError):
    """An agent's save named a rejected draft as its parent. An agent builds on the newest
    version that was not rejected (`newest_buildable_versions`), so a rejected draft's files
    never ride into the next one."""

    code = "parent_rejected"


class SkillLiveChanged(SkillLibraryError):
    """A publish or rollback named the live version it expected, and another one is live."""

    code = "live_changed"


class SkillPendingCapReached(SkillLibraryError):
    code = "pending_cap_reached"


class SkillVersionCapReached(SkillLibraryError):
    code = "version_cap_reached"


class SkillVersionCorrupt(SkillLibraryError):
    """A saved file is missing from the object store, or its bytes no longer match the digest
    recorded when it was saved."""

    code = "version_corrupt"


@dataclass(slots=True, frozen=True)
class DraftProvenance:
    """Who saved a draft, from where, and why.

    ``source="agent"`` is the one thing the pending cap keys on: an agent's drafts wait on a
    person, so they are what can flood a review queue; an operator's save is already that
    person. ``principal`` is the caller behind an agent's turn, when there was one.
    """

    source: SkillSourceKind
    author: str
    reason: str = ""
    origin_manifest_id: str | None = None
    session_id: str | None = None
    principal: str | None = None
    # Set exactly when ``source="import"``.
    origin: ImportOrigin | None = None
    # Set only by `adopt`: the import-lineage version an operator vouched for. The save carries
    # that version's files byte for byte (`save_draft` checks it), and is the one save that does
    # not inherit `lineage_import` from its parent.
    adopted_from: str | None = None


class SkillOriginMismatch(SkillLibraryError):
    """An import named a skill the library holds from somewhere else: another source, or a
    version an agent or an operator wrote. An import never takes over a name."""

    code = "origin_mismatch"


class SkillNotImportLineage(SkillLibraryError):
    """An adopt named a version that carries no imported text: there is nothing to vouch for."""

    code = "not_imported"


class SkillReasonRequired(SkillLibraryError):
    """An adopt gave no reason. Vouching for third-party text is the one save that clears its
    mark, so it says why, and the audit trail keeps it."""

    code = "reason_required"


class SkillPublishBlocked(SkillLibraryError):
    code = "publish_blocked"

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("; ".join(reasons))


def _object_store(settings: Settings, object_store: Any | None) -> Any:
    if object_store is not None:
        return object_store
    from felix.storage import get_object_store

    return get_object_store(settings)


def _audit(
    settings: Settings, tenant_id: str, event_type: str, row: Mapping[str, Any], *, by: str, **extra: Any
) -> None:
    """One event per state change, written straight to the audit store.

    Not `emit_agent_audit`: that needs a request context, and an operator publishing from a
    management route or a worker task has none. The payload names the version and how it
    scored, so the trail answers "what went live, who let it, and what did the scan say"
    without the version row, which a later rollback rewrites.
    """
    from felix.audit.emit import record_offline_event

    status = str(extra.pop("status", "ok"))
    payload = {
        "skill": row.get("name"),
        "version": row.get("version"),
        "source": row.get("source"),
        "author": row.get("author"),
        "quality_score": row.get("quality_score"),
        "security_status": row.get("security_status"),
        **{k: row[k] for k in ORIGIN_COLUMNS if row.get(k) is not None},
        **extra,
    }
    record_offline_event(
        settings,
        tenant_id,
        event_type,
        principal=by,
        payload=payload,
        status=status,
        manifest_id=row.get("origin_manifest_id") or "",
    )


async def host_owns(settings: Settings, tenant_id: str, name: str, object_store: Any | None = None) -> bool:
    """True when ``name`` is a host skill or an operator-uploaded object-store skill.

    The uploaded check is the unversioned `skills/{tenant}/{name}/SKILL.md` and the shared
    `skills/{name}/SKILL.md`. Library bytes never land under `skills/` (`library_object_key`),
    so an operator's versioned upload cannot be overwritten whatever this answers.

    The library may not save one. The catalog would keep serving the host's copy (host wins),
    so a shadowing save would be inert at best -- and at worst, under `skills_declared_only`
    or after the host drops the skill, a tenant's text would start answering to a name an
    operator chose and reviewed.
    """
    from felix.skills.loader import host_catalog, operator_skill_keys

    if (await host_catalog()).get(name) is not None:
        return True
    store = _object_store(settings, object_store)
    for key in operator_skill_keys(tenant_id, name):
        try:
            if await store.exists(key):
                return True
        except Exception:
            # The key embeds a caller's skill name and version: escaped like any untrusted text.
            logger.warning("object store probe failed for %s", loggable(key, limit=300), exc_info=True)
    return False


async def shadows_operator_upload(
    settings: Settings,
    tenant_id: str,
    name: str,
    versions: Iterable[str | None] = (),
    *,
    object_store: Any | None = None,
) -> bool:
    """True when an operator upload exists under ``name`` at a key the loader reads for a ref.

    Probes the unversioned keys and, for each of ``versions``, the pinned ones -- the keys
    `loader._resolve_ref` reads, from the same helpers. Under that precedence such an upload
    splits the name: an unpinned ref gets the library's live version, a ref pinning the upload's version
    gets the upload. A reviewer should know before publishing into that.

    Limited to the versions passed in -- the one being saved, or a skill's live and newest --
    because the object store has no listing: an upload at a version the library never held
    is not found here, though a ref pinning it is still served the upload.
    """
    from felix.skills.loader import operator_skill_keys, pinned_operator_skill_keys, safe_skill_key_parts

    if not safe_skill_key_parts(name):
        return False
    keys = operator_skill_keys(tenant_id, name)
    for version in sorted({v for v in versions if v}):
        if safe_skill_key_parts(name, version):
            keys += pinned_operator_skill_keys(tenant_id, name, version)
    store = _object_store(settings, object_store)
    for key in keys:
        try:
            if await store.exists(key):
                return True
        except Exception:
            # The key embeds a caller's skill name and version: escaped like any untrusted text.
            logger.warning("object store probe failed for %s", loggable(key, limit=300), exc_info=True)
    return False


def newest_version(existing: Iterable[str]) -> str | None:
    """The highest of ``existing`` by semver, where `0.10.0` is above `0.9.0`."""
    return max(existing, key=cmp_to_key(compare_semver), default=None)


def _next_version(newest: str | None, *, explicit: str | None, bump: SemverBump) -> str:
    if explicit is not None:
        if not VERSION_RE.match(explicit):
            raise SkillVersionConflict(f"version {explicit!r} is not major.minor.patch")
        if newest is not None and compare_semver(explicit, newest) <= 0:
            raise SkillVersionConflict(f"version {explicit} must be newer than {newest}")
        return explicit
    return resolve_next_semver(newest, bump=bump)


def _stored_bytes(path: str, content: str) -> bytes:
    # A binary asset arrives base64 (the bundle is text); the object store holds the bytes.
    return decode_base64(content) if is_binary_asset_path(path) else content.encode("utf-8")


async def _write_files(store: Any, tenant_id: str, name: str, version: str, files: Mapping[str, str]) -> None:
    for path, content in files.items():
        await store.put(library_object_key(tenant_id, name, version, path), _stored_bytes(path, content))


def normalized_text(text: str) -> str:
    """``text`` as the copy rule compares it: Unicode NFKC, casefolded, every run of whitespace
    one space, stripped. A copy that only re-spaces, re-cases or swaps compatibility forms
    (full-width letters, ligatures, non-breaking spaces) normalizes to the original."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _normalized_digest(path: str, content: str) -> str | None:
    """The sha256 of a text file's `normalized_text`; None for a binary asset, whose bytes are
    compared as they are."""
    if is_binary_asset_path(path):
        return None
    return hashlib.sha256(normalized_text(content).encode("utf-8")).hexdigest()


def _file_rows(files: Mapping[str, str]) -> list[dict[str, Any]]:
    rows = []
    for path, content in sorted(files.items()):
        data = _stored_bytes(path, content)
        rows.append(
            {
                "path": path,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
                "normalized_sha256": _normalized_digest(path, content),
            }
        )
    return rows


def _copy_digests(files: Mapping[str, str]) -> tuple[set[str], set[str]]:
    """The byte and normalized digests the copy rule looks up for ``files``, leaving out every
    file under `COPY_FLOOR_CHARS`."""
    exact: set[str] = set()
    normalized: set[str] = set()
    for path, content in files.items():
        data = _stored_bytes(path, content)
        if is_binary_asset_path(path):
            if len(data) >= COPY_FLOOR_CHARS:
                exact.add(hashlib.sha256(data).hexdigest())
            continue
        text = normalized_text(content)
        if len(text) < COPY_FLOOR_CHARS:
            continue
        exact.add(hashlib.sha256(data).hexdigest())
        normalized.add(hashlib.sha256(text.encode("utf-8")).hexdigest())
    return exact, normalized


async def _reserve(
    lib: SkillLibraryStore,
    tenant_id: str,
    row: dict[str, Any],
    files: list[dict[str, Any]],
    *,
    explicit: str | None,
    bump: SemverBump,
    expect_newest: str | _MustNotExist | None,
    buildable: bool = False,
    max_pending: int | None = None,
) -> dict[str, Any]:
    """Insert the version row under the next free version; the primary key settles a race.
    ``max_pending`` is the agent pending cap, checked by the store in the insert's transaction.

    ``expect_newest`` is checked on every attempt, so a save that loses a race to the version
    after its parent is refused as `parent_changed` rather than bumped past the winner. With
    ``buildable`` (an agent's save) it is checked against the newest version that is not a
    rejected draft, and naming a rejected one is `parent_rejected`.
    """
    for _ in range(_SAVE_ATTEMPTS):
        existing = await lib.version_ids(tenant_id, row["name"])
        if len(existing) >= MAX_VERSIONS_PER_SKILL:
            raise SkillVersionCapReached(
                f"{row['name']} already has {len(existing)} versions (limit {MAX_VERSIONS_PER_SKILL})"
            )
        newest = newest_version(existing)
        if expect_newest is MUST_NOT_EXIST:
            if newest is not None:
                raise SkillExists(f"{row['name']} is already in the library")
        elif isinstance(expect_newest, str):
            await _check_parent(
                lib, tenant_id, row["name"], expect_newest, newest, existing, buildable=buildable
            )
        row = {**row, "version": _next_version(newest, explicit=explicit, bump=bump)}
        try:
            await lib.insert_version(
                tenant_id, row, files, created_by=row["author"], at=row["created_at"], max_pending=max_pending
            )
            return row
        except SkillPendingFull as exc:
            raise _pending_refused(row.get("origin_manifest_id"), exc.held, exc.limit) from exc
        except SkillVersionExists as exc:
            if explicit is not None:
                raise SkillVersionConflict(f"version {explicit} already exists") from exc
    raise SkillVersionConflict("concurrent saves kept taking the next version; try again")


async def _check_parent(
    lib: SkillLibraryStore,
    tenant_id: str,
    name: str,
    expected: str,
    newest: str | None,
    existing: list[str],
    *,
    buildable: bool,
) -> None:
    basis = newest
    if buildable:
        basis = newest_version((await lib.buildable_versions(tenant_id, [name])).get(name, []))
        if expected != basis and expected in existing:
            row = await lib.get_version(tenant_id, name, expected)
            if row is not None and is_rejected(row):
                raise SkillParentRejected(
                    f"{name}@{expected} was rejected; edit {basis or 'nothing'}, the newest not rejected"
                )
    if basis != expected:
        raise SkillParentChanged(
            f"{name} is at {basis or 'no version'}, not {expected}; reload and edit that"
        )


async def newest_buildable_versions(
    settings: Settings, tenant_id: str, names: Iterable[str]
) -> dict[str, str]:
    """Each named skill's newest version an agent may build on: the newest that is not a
    rejected draft. One query for every name."""
    rows = await get_skill_library_store(settings).buildable_versions(tenant_id, list(names))
    return {n: v for n, vs in rows.items() if (v := newest_version(vs)) is not None}


def _pending_cap(provenance: DraftProvenance, max_pending: int | None) -> int | None:
    """The pending cap, which applies to agent drafts with an origin manifest and nothing else
    (`DraftProvenance`)."""
    if provenance.source != "agent" or not provenance.origin_manifest_id:
        return None
    return max_pending


def _pending_refused(origin: str | None, held: int, limit: int) -> SkillPendingCapReached:
    return SkillPendingCapReached(f"{origin} already has {held} drafts awaiting review (limit {limit})")


async def _check_pending(
    lib: SkillLibraryStore, tenant_id: str, provenance: DraftProvenance, max_pending: int | None
) -> None:
    """An early refusal at the pending cap, before the review and scan a save at the cap would
    waste. Not the cap itself: two saves can both pass this, and the one the store's capped
    `insert_version` then counts past the cap is refused there, in the transaction that would
    have written it (`library_store.SkillPendingFull`)."""
    cap = _pending_cap(provenance, max_pending)
    if cap is None:
        return
    held = await lib.count_pending(tenant_id, str(provenance.origin_manifest_id))
    if held >= cap:
        raise _pending_refused(provenance.origin_manifest_id, held, cap)


async def save_draft(
    settings: Settings,
    tenant_id: str,
    *,
    files: Mapping[str, str],
    provenance: DraftProvenance,
    name: str | None = None,
    parent: str | None = None,
    version: str | None = None,
    bump: SemverBump = "patch",
    max_pending: int | None = None,
    expect_newest: str | _MustNotExist | None = None,
    object_store: Any | None = None,
) -> dict[str, Any]:
    """Validate, review and scan a bundle, then save it as a new immutable draft.

    ``name``, when given, must be the SKILL.md's own name. ``parent`` is the version this one
    was edited from (lineage, not the bump base: the bump is from the newest version, so a
    save is always newer than everything saved before it). ``max_pending`` caps how many
    undecided agent drafts one origin manifest may hold, exactly: the store counts them and
    writes this one in a single transaction under a lock per origin manifest.

    ``expect_newest`` is optimistic concurrency: the newest version the caller saw, or
    `MUST_NOT_EXIST` for a create. A save made against anything else is refused
    (`SkillParentChanged`, `SkillExists`). None skips the check.

    The returned row carries ``shadows_operator_upload`` (`shadows_operator_upload`), which
    the audit event records too. It is a warning, not a refusal: the loader decides who
    answers each ref, and that decision is the reviewer's to know about -- except for an
    import, which is refused rather than let third-party text split a name an operator chose.

    The row's ``lineage_import`` is set for an import and for any version built on one
    (`_lineage_import`): an edit of third-party text is still judged as one.
    """
    if (provenance.source == "import") != (provenance.origin is not None):
        raise ValueError("an import's provenance carries its origin, and only an import's does")
    if provenance.adopted_from is not None and (
        provenance.source != "operator" or parent != provenance.adopted_from
    ):
        raise ValueError("an adopt is an operator's save whose parent is the version it adopts")
    validation = await asyncio.to_thread(validate_skill_bundle, files, name)
    if not validation.valid or validation.frontmatter is None:
        raise SkillBundleInvalid(validation.errors)
    skill_name = validation.frontmatter.name
    store = _object_store(settings, object_store)
    if await host_owns(settings, tenant_id, skill_name, store):
        raise SkillNameShadowed(f"{skill_name!r} is a host skill; the library cannot replace it")

    lib = get_skill_library_store(settings)
    await _check_pending(lib, tenant_id, provenance, max_pending)
    if parent is not None and await lib.get_version(tenant_id, skill_name, parent) is None:
        raise SkillNotFound(f"parent version {skill_name}@{parent} does not exist")
    if provenance.source == "agent":
        await _evals_only_inherited(lib, tenant_id, skill_name, parent, files)
    if provenance.adopted_from is not None:
        await _same_files_as(lib, tenant_id, skill_name, provenance.adopted_from, files)

    row = {
        "name": skill_name,
        "parent_version": parent,
        "status": "draft",
        "source": provenance.source,
        "author": provenance.author,
        "origin_manifest_id": provenance.origin_manifest_id,
        "session_id": provenance.session_id,
        "reason": (provenance.reason or "")[:_REASON_LIMIT],
        "description": validation.frontmatter.description,
        **(provenance.origin.as_row() if provenance.origin else {}),
        "adopted_from": provenance.adopted_from,
        "lineage_import": await _lineage_import(lib, tenant_id, skill_name, parent, provenance, files),
        **(await asyncio.to_thread(assess, files, skill_name)).as_row(),
        "created_at": now_ms(),
    }
    row = await _reserve(
        lib,
        tenant_id,
        row,
        _file_rows(files),
        explicit=version,
        bump=bump,
        expect_newest=expect_newest,
        # An import builds on the newest version that was not rejected, as an agent does: a
        # rejected draft is not the version it replaces (`importer._prior`). So does an adopt:
        # it vouches for the version a reviewer would otherwise be editing.
        buildable=provenance.source in {"agent", "import"} or provenance.adopted_from is not None,
        max_pending=_pending_cap(provenance, max_pending),
    )
    try:
        await _write_files(store, tenant_id, skill_name, row["version"], files)
    except Exception:
        # The row is a draft, so nothing loaded it in the meantime; take it and any bytes
        # already written back out.
        await _discard(lib, store, tenant_id, row, files)
        raise
    shadows = await shadows_operator_upload(
        settings, tenant_id, skill_name, [row["version"]], object_store=store
    )
    if shadows and provenance.source == "import":
        await _discard(lib, store, tenant_id, row, files)
        raise SkillNameShadowed(
            f"{skill_name!r} is an operator upload's name; an import cannot share it with an upload"
        )
    _audit(
        settings,
        tenant_id,
        "skill_draft_saved",
        row,
        by=provenance.author,
        parent=parent,
        reason=_redacted(settings, row["reason"][:200]),
        shadows_operator_upload=shadows,
        **({"principal": provenance.principal} if provenance.principal else {}),
    )
    return {**row, "tenant_id": tenant_id, "shadows_operator_upload": shadows}


async def _lineage_import(
    lib: SkillLibraryStore,
    tenant_id: str,
    name: str,
    parent: str | None,
    provenance: DraftProvenance,
    files: Mapping[str, str],
) -> bool:
    """Whether the version being saved carries imported text: it is an import, or the version it
    was edited from does -- the named parent, else the skill's newest version, which is what a
    save that names none still starts from.

    For an agent's save, also when any of its files is a file of an import-lineage version
    anywhere in the tenant -- byte for byte, or once both are normalized (`normalized_text`:
    compatibility forms, case and whitespace) -- an agent can read an imported skill and write its
    text into a new one under another name, and the copy is as much a third party's as the
    original. A paraphrase is not caught, and nor is a file under `COPY_FLOOR_CHARS`. A file saved
    before the normalized digest existed (migration `0032`) has none until it is saved again, and
    is matched by its bytes alone.

    An adopt (`DraftProvenance.adopted_from`, set only by `adopt`) is the one exception to the
    parent rule: an operator vouching, with a reason, for exactly the files of the version it
    names -- `save_draft` has checked they are that version's, byte for byte. It is an operator's
    save, so the copy rule does not run on it either. Nothing else reaches here with it set, and
    the version it was adopted from keeps its mark."""
    if provenance.source == "import":
        return True
    if provenance.adopted_from is not None:
        return False
    basis = parent or newest_version(await lib.version_ids(tenant_id, name))
    if basis is not None and gate_source(await lib.get_version(tenant_id, name, basis)) == "import":
        return True
    if provenance.source != "agent":
        return False
    exact, normalized = _copy_digests(files)
    return await lib.holds_imported_file(tenant_id, exact, normalized)


async def _same_files_as(
    lib: SkillLibraryStore, tenant_id: str, name: str, version: str, files: Mapping[str, str]
) -> None:
    """Refuse an adopt whose files are not exactly ``version``'s: the exemption from the parent
    rule covers the bytes the operator vouched for and nothing else."""
    held = {str(r["path"]): str(r["sha256"]) for r in await lib.list_files(tenant_id, name, version)}
    sent = {p: hashlib.sha256(_stored_bytes(p, c)).hexdigest() for p, c in files.items()}
    if held != sent:
        raise ValueError(f"an adopt of {name}@{version} must carry exactly that version's files")


async def _evals_only_inherited(
    lib: SkillLibraryStore, tenant_id: str, name: str, parent: str | None, files: Mapping[str, str]
) -> None:
    """Refuse an agent's save that adds or changes a file under `evals/`.

    The bundle's own scenarios are the only evaluation an agent's version is graded on
    (`publish_gate.eval_counts_for_gate`), which is sound only if an agent cannot write them. Its
    tools carry the parent's files unchanged; this makes that a rule rather than a habit of the
    callers: every `evals/` file must be the parent's, byte for byte.
    """
    evals = {p: c for p, c in files.items() if p.startswith("evals/")}
    if not evals:
        return
    inherited = (
        {str(r["path"]): str(r["sha256"]) for r in await lib.list_files(tenant_id, name, parent)}
        if parent is not None
        else {}
    )
    changed = sorted(
        p for p, c in evals.items() if inherited.get(p) != hashlib.sha256(_stored_bytes(p, c)).hexdigest()
    )
    if changed:
        raise SkillBundleInvalid(
            [
                ValidationIssue(
                    path=p, message="an agent's save may only keep evals/ files unchanged from its parent"
                )
                for p in changed
            ]
        )


async def _discard(
    lib: SkillLibraryStore, store: Any, tenant_id: str, row: Mapping[str, Any], files: Mapping[str, str]
) -> None:
    for path in files:
        try:
            await store.delete(library_object_key(tenant_id, row["name"], row["version"], path))
        except Exception:
            logger.warning("could not remove %s of a failed save", loggable(path, limit=200), exc_info=True)
    await lib.delete_draft(tenant_id, row["name"], row["version"])


def _redacted(settings: Settings, text: str) -> str:
    from felix.secrets import collected_secret_values, redact_text

    return redact_text(text, collected_secret_values(settings))


def _checked(path: str, data: bytes | None, digest: str) -> str:
    if data is None:
        raise SkillVersionCorrupt(f"{path} is missing from the object store")
    if hashlib.sha256(data).hexdigest() != digest:
        raise SkillVersionCorrupt(f"{path} changed in the object store since it was saved")
    return encode_base64(data) if is_binary_asset_path(path) else data.decode("utf-8")


async def read_version_files(
    settings: Settings, tenant_id: str, name: str, version: str, *, object_store: Any | None = None
) -> dict[str, str]:
    """Every file of a saved version as bundle text (binary assets base64), checked against
    the digests recorded when it was saved. A missing or altered file raises
    `SkillVersionCorrupt`."""
    lib = get_skill_library_store(settings)
    store = _object_store(settings, object_store)
    files: dict[str, str] = {}
    for meta in await lib.list_files(tenant_id, name, version):
        path = str(meta["path"])
        files[path] = _checked(
            path, await store.get(library_object_key(tenant_id, name, version, path)), meta["sha256"]
        )
    return files


async def read_version_file(
    settings: Settings, tenant_id: str, name: str, version: str, path: str, *, object_store: Any | None = None
) -> str | None:
    """One file of a saved version, or None when the version holds no such path.

    Only a path the version's `skill_file` rows name is read, and its bytes must match the
    recorded digest, so a reader cannot be steered at an object the save did not write.
    """
    rows = await get_skill_library_store(settings).list_files(tenant_id, name, version)
    meta = next((r for r in rows if r["path"] == path), None)
    if meta is None:
        return None
    store = _object_store(settings, object_store)
    return _checked(path, await store.get(library_object_key(tenant_id, name, version, path)), meta["sha256"])


async def evaluate_version(
    settings: Settings,
    tenant_id: str,
    name: str,
    version: str,
    *,
    policy: PublishPolicy | None = None,
    object_store: Any | None = None,
) -> Verdict:
    """Re-read a saved version against its digests and judge it as a publish would be.

    What `_gate` decides on, returned rather than raised, so a reviewer can see the verdict
    without causing one. Changes nothing. Bytes that no longer match their digests are a
    verdict too: invalid, with the mismatch as its reason.
    """
    if policy is None:
        from felix.skills.policy import load_publish_policy

        policy = (await load_publish_policy(settings, tenant_id)).policy
    # Who wrote the version can only tighten the policy (`policy_for_source`), and decides which
    # evaluations count (`publish_gate.eval_counts_for_gate`).
    row = await get_skill_library_store(settings).get_version(tenant_id, name, version)
    source = gate_source(row)
    policy = policy_for_source(policy, source)
    latest_eval = None
    if policy.needs_eval:
        # Only a policy that reads the evaluation pays for the lookup.
        from felix.skills.eval_store import get_skill_eval_store

        latest_eval = await get_skill_eval_store(settings).latest_succeeded(
            tenant_id, name, version, scenario_source=gate_scenario_source(source)
        )
    try:
        files = await read_version_files(settings, tenant_id, name, version, object_store=object_store)
    except SkillVersionCorrupt as exc:
        return Verdict(valid=False, reasons=[str(exc)])
    return await asyncio.to_thread(evaluate_files, files, name, policy, latest_eval)


async def _gate(
    settings: Settings, tenant_id: str, row: Mapping[str, Any], object_store: Any, *, rollback: bool
) -> None:
    """Re-read, re-validate and re-scan what is about to go live, then apply the policy.

    Re-run rather than trusting the row: the bytes are in a store an operator can write
    directly, and the scanner's rules may have grown since the draft was saved.

    A rollback is judged without the evaluation requirement. It returns to a version that was
    already live, usually in a hurry, and `require_eval` may have been set after it went live;
    the security scan, validation and the quality floor still apply to it.
    """
    from felix.skills.policy import load_publish_policy

    policy = (await load_publish_policy(settings, tenant_id)).policy
    verdict = await evaluate_version(
        settings,
        tenant_id,
        str(row["name"]),
        str(row["version"]),
        policy=policy.without_eval() if rollback else policy,
        object_store=object_store,
    )
    if not verdict.passes:
        raise SkillPublishBlocked(verdict.reasons)


# What each way of going live may start from. A publish takes a draft; a rollback takes a
# version that is, or was, live -- and `_make_live` also requires that it once went live.
_LIVE_FROM: dict[str, frozenset[SkillStatus]] = {
    "skill_published": frozenset({"draft"}),
    "skill_rolled_back": frozenset({"archived", "published"}),
}


async def _make_live(
    settings: Settings,
    tenant_id: str,
    name: str,
    version: str,
    *,
    by: str,
    event: str,
    object_store: Any | None,
    expected_live: ExpectedLive = ANY_LIVE,
) -> dict[str, Any]:
    from_statuses = _LIVE_FROM[event]
    lib = get_skill_library_store(settings)
    row = await lib.get_version(tenant_id, name, version)
    if row is None:
        raise SkillNotFound(f"{name}@{version} does not exist")
    if row["status"] not in from_statuses:
        raise SkillVersionConflict(f"{name}@{version} is {row['status']}")
    if event == "skill_rolled_back" and row.get("published_at") is None:
        # A rejected draft is `archived` too; only a version that once went live may return.
        raise SkillVersionConflict(f"{name}@{version} was never published; publish it instead")
    try:
        await _gate(
            settings,
            tenant_id,
            row,
            _object_store(settings, object_store),
            rollback=event == "skill_rolled_back",
        )
    except SkillPublishBlocked as exc:
        _audit(settings, tenant_id, event, row, by=by, status="blocked", reasons=exc.reasons)
        raise
    try:
        previous = await lib.publish(
            tenant_id,
            name,
            version,
            from_statuses=from_statuses,
            by=by,
            at=now_ms(),
            expected_live=expected_live,
        )
    except SkillLiveMismatch as exc:
        raise SkillLiveChanged(str(exc)) from exc
    except SkillStateConflict as exc:
        raise SkillVersionConflict(f"{name}@{version} changed state while it was being published") from exc
    _audit(settings, tenant_id, event, row, by=by, previous=previous)
    return await lib.get_version(tenant_id, name, version) or row


async def publish(
    settings: Settings,
    tenant_id: str,
    name: str,
    version: str,
    *,
    by: str,
    object_store: Any | None = None,
    expected_live: ExpectedLive = ANY_LIVE,
) -> dict[str, Any]:
    """Publish a draft: it becomes `live_version`, and the version it replaces is archived.

    ``expected_live`` is the live version the caller saw (None: nothing was live). Given, the
    publish is refused with `SkillLiveChanged` if another version is live by the time it lands --
    checked under the skill row's lock -- so two reviewers cannot both move the pointer."""
    return await _make_live(
        settings,
        tenant_id,
        name,
        version,
        by=by,
        event="skill_published",
        object_store=object_store,
        expected_live=expected_live,
    )


async def rollback(
    settings: Settings,
    tenant_id: str,
    name: str,
    version: str,
    *,
    by: str,
    object_store: Any | None = None,
    expected_live: ExpectedLive = ANY_LIVE,
) -> dict[str, Any]:
    """Make a once-published version live again, through the same gate as a publish
    (without its evaluation requirement). ``expected_live`` as for `publish`."""
    return await _make_live(
        settings,
        tenant_id,
        name,
        version,
        by=by,
        event="skill_rolled_back",
        object_store=object_store,
        expected_live=expected_live,
    )


async def adopt(
    settings: Settings,
    tenant_id: str,
    name: str,
    version: str,
    *,
    by: str,
    reason: str,
    object_store: Any | None = None,
) -> dict[str, Any]:
    """An operator vouches for an import-lineage version: its files, byte for byte, saved as a new
    operator draft built on it that does not carry `lineage_import`.

    The only way the mark is cleared, and only forward: versions are immutable, so ``version``
    and every version before it keep theirs. The new version is a draft -- it goes live through
    the ordinary publish gate, which now judges it as an operator's -- and adopt never publishes.
    No agent tool reaches this; it is an operator route.

    Refused with ``reason_required`` for a blank reason; ``not_found``; ``parent_rejected`` for a
    rejected draft; ``not_imported`` for a version that carries no imported text; and
    ``parent_changed`` unless ``version`` is the skill's newest version that was not rejected
    (the rule every other save builds on).
    """
    reason = (reason or "").strip()
    if not reason:
        raise SkillReasonRequired("say why this imported text is now the operator's own")
    lib = get_skill_library_store(settings)
    row = await lib.get_version(tenant_id, name, version)
    if row is None:
        raise SkillNotFound(f"{name}@{version} does not exist")
    if is_rejected(row):
        raise SkillParentRejected(f"{name}@{version} was rejected; adopt a version that was not")
    if gate_source(row) != "import":
        raise SkillNotImportLineage(f"{name}@{version} carries no imported text; there is nothing to adopt")
    store = _object_store(settings, object_store)
    saved = await save_draft(
        settings,
        tenant_id,
        files=await read_version_files(settings, tenant_id, name, version, object_store=store),
        provenance=DraftProvenance(
            source="operator", author=by, reason=reason, principal=by, adopted_from=version
        ),
        name=name,
        parent=version,
        expect_newest=version,
        object_store=store,
    )
    _audit(
        settings,
        tenant_id,
        "skill_adopted",
        saved,
        by=by,
        adopted_from=version,
        reason=_redacted(settings, reason[:200]),
        principal=by,
    )
    return saved


async def reject(
    settings: Settings, tenant_id: str, name: str, version: str, *, by: str, note: str
) -> dict[str, Any]:
    """Archive a draft without publishing it, recording why."""
    lib = get_skill_library_store(settings)
    row = await lib.get_version(tenant_id, name, version)
    if row is None:
        raise SkillNotFound(f"{name}@{version} does not exist")
    try:
        await lib.reject(tenant_id, name, version, by=by, note=(note or "")[:_REASON_LIMIT], at=now_ms())
    except SkillStateConflict as exc:
        raise SkillVersionConflict(f"{name}@{version} is not a draft") from exc
    _audit(settings, tenant_id, "skill_rejected", row, by=by, note=(note or "")[:200])
    return await lib.get_version(tenant_id, name, version) or row


async def archive_skill(settings: Settings, tenant_id: str, name: str, *, by: str) -> dict[str, Any]:
    """Take a skill out of every catalog: `live_version` is cleared, its history kept."""
    lib = get_skill_library_store(settings)
    try:
        previous = await lib.archive_skill(tenant_id, name, by=by, at=now_ms())
    except SkillStateConflict as exc:
        raise SkillNotFound(f"{name} is not in the library") from exc
    row = (await lib.get_version(tenant_id, name, previous) if previous else None) or {"name": name}
    _audit(settings, tenant_id, "skill_archived", row, by=by, previous=previous)
    return await lib.get_skill(tenant_id, name) or {"name": name, "live_version": None}


__all__ = [
    "COPY_FLOOR_CHARS",
    "MUST_NOT_EXIST",
    "ORIGIN_COLUMNS",
    "VERSION_RE",
    "DraftProvenance",
    "ImportOrigin",
    "SkillBundleInvalid",
    "SkillExists",
    "SkillLibraryError",
    "SkillLiveChanged",
    "SkillNameShadowed",
    "SkillNotFound",
    "SkillNotImportLineage",
    "SkillOriginMismatch",
    "SkillParentChanged",
    "SkillParentRejected",
    "SkillPendingCapReached",
    "SkillPublishBlocked",
    "SkillReasonRequired",
    "SkillVersionCapReached",
    "SkillVersionConflict",
    "SkillVersionCorrupt",
    "adopt",
    "archive_skill",
    "evaluate_version",
    "host_owns",
    "newest_buildable_versions",
    "newest_version",
    "normalized_text",
    "publish",
    "read_version_file",
    "read_version_files",
    "reject",
    "rollback",
    "save_draft",
    "shadows_operator_upload",
]
