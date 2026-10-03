"""Load SKILL.md packages from object store, bundled dirs, and inline refs."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

from felix.skills.format import MAX_FRONTMATTER_CHARS, is_valid_skill_name
from felix.skills.format import parse_skill_md as parse_skill_md_format
from felix.skills.types import Skill, SkillCatalog

logger = logging.getLogger("felix.skills.loader")

# Looser than `format.FRONTMATTER_RE` (any whitespace may trail a fence), so a file the
# YAML path refuses for its fences still reaches the legacy reader.
_LEGACY_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
# Keys a nested `metadata:` map may not supply: they decide which skill this is, what the
# model is told about it, and whether the model is offered it at all.
_RESERVED_KEYS = frozenset({"name", "description", "disable-model-invocation"})
# What may be interpolated into an object key as a skill name. Not the skill-name rule
# (`format.is_valid_skill_name`), which also refuses `a--b`; this one only has to keep a
# key to one safe segment.
_KEY_SEGMENT_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
# A version may carry dots (`1.2.0`) which a name may not, but is otherwise the same
# shape: one segment, no separators, not a traversal.
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _safe_segment(value: str, pattern: re.Pattern[str], *, limit: int = 64) -> bool:
    """True when ``value`` may be interpolated into an object key as one segment.

    `SkillRef.name` and `version` are unvalidated manifest strings, and they were
    interpolated straight into `skills/{tenant}/{name}/SKILL.md`. No shipped backend
    could be walked with them — the fs store rejects `..` segments and S3/GCS treat
    keys as literal text — but `artifacts.py` deliberately validates its own key
    parts rather than trusting whichever store an operator configured, and this is
    the same argument.
    """
    return bool(value) and len(value) <= limit and bool(pattern.match(value)) and value not in {".", ".."}


def _legacy_frontmatter(yaml_text: str) -> dict[str, str]:
    """The pre-YAML reader: every `key: value` line, split on the first colon.

    Kept as the fallback for frontmatter YAML refuses. `description: Use it: daily` read
    fine here and is a YAML error, so a skill written against this reader must not vanish
    from the catalog. Indented lines are treated as `metadata:` children and merged under
    the same rule as YAML's.
    """
    top: dict[str, str] = {}
    nested: dict[str, str] = {}
    for line in yaml_text.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        (nested if line[:1].isspace() else top)[key.strip().lower()] = val.strip().strip("\"'")
    if nested and top.get("metadata") == "":
        del top["metadata"]  # the block's own key, as the YAML path drops it
    return _merge(top, nested)


def _merge(top: dict[str, str], nested: dict[str, str]) -> dict[str, str]:
    """Top-level keys, then nested `metadata:` children that neither override one nor are
    reserved \u2014 so a metadata block cannot rename a skill, rewrite its description, or hide
    it from the model."""
    merged = dict(top)
    for key, value in nested.items():
        if key not in _RESERVED_KEYS and key not in merged:
            merged[key] = value
    return merged


def _top_level_line_values(yaml_text: str) -> dict[str, str]:
    """`key -> text after the colon` for each unindented line, as the legacy reader saw it."""
    values: dict[str, str] = {}
    for line in yaml_text.splitlines():
        if ":" in line and not line[:1].isspace():
            key, _, val = line.partition(":")
            values[key.strip().lower()] = val.strip()
    return values


def _as_text(value: object) -> str:
    """A read-path YAML value as one string. Scalars already are (`scalars_as_text`); a
    list joins its scalar items; a map, other than `metadata:`, keeps only its key."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return ", ".join(v.strip() for v in value if isinstance(v, str))
    return ""


def _yaml_frontmatter(frontmatter: dict[Any, Any], yaml_text: str, *, source: str) -> dict[str, str]:
    top: dict[str, str] = {}
    nested: dict[str, str] = {}
    lines = _top_level_line_values(yaml_text)
    for raw_key, value in frontmatter.items():
        key = str(raw_key).strip().lower()
        if key == "metadata" and isinstance(value, dict):
            nested = {str(k).strip().lower(): _as_text(v) for k, v in value.items()}
            continue
        text = _as_text(value)
        # ` #` starts a YAML comment, so `description: Ranks issues #1 first` is "Ranks
        # issues" to YAML and was the whole line to the legacy reader. The whole line wins:
        # a description that silently loses its end is worse than one read the old way.
        line = lines.get(key, "")
        rest = line[len(text) :] if line.startswith(text) else ""
        if line != text and rest and (re.match(r"\s+#", rest) or (not text and rest.startswith("#"))):
            logger.warning(
                "skill %s: %r holds a ' #' YAML reads as a comment; kept the whole line", source, key
            )
            text = line
        top[key] = text
    return _merge(top, nested)


def _frontmatter(raw: str, *, source: str) -> tuple[dict[str, str], str] | None:
    """Flat string frontmatter and the stripped body, or None when the skill must be skipped.

    YAML first, with every scalar kept as its source text, so the version `1.10` is not the
    float 1.1. The legacy line reader runs only when YAML refuses the frontmatter. Keys are
    lowercased, as the legacy reader did.
    """
    text = raw.lstrip("\ufeff")
    fenced = _LEGACY_FRONTMATTER_RE.match(text)
    if fenced is None:
        return {}, text.strip()
    if len(fenced.group(1)) > MAX_FRONTMATTER_CHARS:
        logger.warning("skill %s: frontmatter is over %d characters; skipped", source, MAX_FRONTMATTER_CHARS)
        return None
    parsed = parse_skill_md_format(text, scalars_as_text=True)
    if parsed is not None and isinstance(parsed.frontmatter, dict):
        return _yaml_frontmatter(parsed.frontmatter, parsed.yaml_text, source=source), parsed.body.strip()
    logger.warning("skill %s: frontmatter is not spec YAML; read line by line instead", source)
    return _legacy_frontmatter(fenced.group(1)), fenced.group(2).strip()


def parse_skill_md(raw: str, *, fallback_name: str, path: str | None = None) -> Skill | None:
    """Parse a SKILL.md body into a Skill. Returns None if description is missing.

    Lenient on purpose: this is the read path for skills already on disk or in the store,
    so a bad name only warns. `felix.skills.format.validate_skill_bundle` is the strict check,
    and it belongs to the authoring path.
    """
    read = _frontmatter(raw, source=path or fallback_name)
    if read is None:
        return None
    meta, body = read
    name = (meta.get("name") or fallback_name).strip().lower()
    description = (meta.get("description") or "").strip()
    if not description:
        logger.warning("skill %s missing description; skipping", name)
        return None
    if not is_valid_skill_name(name):
        logger.warning("skill name %r invalid; loading with warnings", name)
    disable = meta.get("disable-model-invocation", "").lower() in {"true", "1", "yes"}
    return Skill(
        name=name,
        description=description[:1024],
        body=body,
        path=path,
        version=meta.get("version"),
        metadata={k: v for k, v in meta.items() if k not in {"name", "description"}},
        disable_model_invocation=disable,
    )


def _xml_escape(value: str) -> str:
    """Escape text interpolated into the skills catalog block."""
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


_CATALOG_PREAMBLE = (
    "You have access to the following skills. Use activate_skill to load full instructions "
    "when a task matches a skill description. Only names and descriptions are listed here."
)


def skill_catalog_xml(catalog: SkillCatalog) -> str:
    """Progressive-disclosure catalog block for the system prompt (agentskills.io style)."""
    # Named rather than written inline: two adjacent string literals inside a list are
    # far more often a missing comma than a deliberate concatenation, so the shape is
    # worth not using where a reader has to judge which one it is.
    public = catalog.list_public()
    if not public:
        return ""
    lines = [_CATALOG_PREAMBLE, "<available_skills>"]
    for skill in public:
        # Escaped: name and description come from a SKILL.md in the tenant object store,
        # and this block is appended to the *system prompt*. A description containing
        # "</description></skill></available_skills>" would otherwise break out of the
        # catalog and append attacker-chosen text to the highest-trust surface there is.
        name = _xml_escape(skill.name)
        description = _xml_escape(skill.description)
        lines.append(f'  <skill name="{name}">\n    <description>{description}</description>\n  </skill>')
    lines.append("</available_skills>")
    return "\n".join(lines)


def operator_skill_keys(
    tenant_id: str, name: str, version: str | None = None, *, pinned_only: bool = False
) -> list[str]:
    """The object-store keys an operator's upload of ``name`` may live at, in the order they
    are read: the tenant's own before any shared one, each pinned version before unversioned.
    ``pinned_only`` keeps the two versioned keys alone.

    The only spelling of the operator layout. The skill library probes the same keys to tell
    a reviewer when a library name collides with an upload (`library.shadows_operator_upload`).
    Callers validate ``name`` and ``version`` as key segments first (`_safe_segment`).
    """
    pinned = [f"skills/{tenant_id}/{name}/{version}/SKILL.md", f"skills/{name}/{version}/SKILL.md"]
    if pinned_only:
        return pinned if version else []
    if not version:
        return [f"skills/{tenant_id}/{name}/SKILL.md", f"skills/{name}/SKILL.md"]
    return [pinned[0], f"skills/{tenant_id}/{name}/SKILL.md", pinned[1], f"skills/{name}/SKILL.md"]


def safe_skill_key_parts(name: str, version: str | None = None) -> bool:
    """True when ``name`` (and ``version``, if given) may be interpolated into an object key."""
    if not _safe_segment(name, _KEY_SEGMENT_RE):
        return False
    return version is None or _safe_segment(version, _VERSION_RE, limit=32)


async def _first_stored_skill(store: Any, keys: list[str], *, name: str) -> Skill | None:
    """The skill at the first of ``keys`` that holds one, or None."""
    for key in keys:
        try:
            data = await store.get(key)
        except Exception:
            logger.debug("object store get failed for %s", key, exc_info=True)
            continue
        if not data:
            continue
        try:
            skill = parse_skill_md(data.decode("utf-8"), fallback_name=name, path=key)
        except Exception:
            logger.warning("skill %s could not be read; skipped", key, exc_info=True)
            return None
        if skill is not None and skill.name != name:
            # The key names the skill a manifest asked for; a SKILL.md that calls itself
            # something else would enter the catalog under a name nobody declared.
            logger.warning("skill %s names itself %r, not %r; skipped", key, skill.name, name)
            return None
        if skill is not None:
            skill.source = "store"
        return skill
    return None


async def load_skill_from_store(
    store: Any,
    *,
    tenant_id: str,
    name: str,
    version: str | None = None,
) -> Skill | None:
    """Load skills/{tenant}/{name}/SKILL.md or skills/{name}/SKILL.md from an ObjectStore.

    The tenant's own skill wins. Every tenant-scoped key is tried before any shared
    one, rather than interleaving them by version — interleaved, a shared *versioned*
    skill beat the tenant's own unversioned skill and the tenant's was never read.

    The shared `skills/{name}/` namespace is an operator layer: no route lets a
    tenant write a bare object key, so it cannot be planted by another tenant.
    """
    if not _safe_segment(name, _KEY_SEGMENT_RE):
        logger.warning("skill name %r is not a usable key segment; skipped", name)
        return None
    if version is not None and not _safe_segment(version, _VERSION_RE, limit=32):
        logger.warning("skill version %r is not a usable key segment; ignored", version)
        version = None
    return await _first_stored_skill(store, operator_skill_keys(tenant_id, name, version), name=name)


async def _pinned_upload(store: Any, *, tenant_id: str, name: str, version: str) -> Skill | None:
    """An operator's upload at exactly this pinned version, or None. Never the unversioned key:
    that one does not answer a pin, so it cannot outrank a library skill on one."""
    if not safe_skill_key_parts(name, version):
        return None
    keys = operator_skill_keys(tenant_id, name, version, pinned_only=True)
    return await _first_stored_skill(store, keys, name=name)


# Object-store reads one catalog load runs at once. A library of a few hundred live skills is
# a few hundred GETs; unbounded, they would open that many connections to S3 for one request.
_LIBRARY_FETCH_CONCURRENCY = 16


async def _library_skill(store: Any, *, tenant_id: str, row: dict[str, Any]) -> Skill | None:
    """The live version of one library skill, or None when its SKILL.md is missing, does not
    match the digest saved with it, or cannot be read."""
    import hashlib

    from felix.skills.library_store import library_object_key

    name, version = str(row["name"]), str(row["version"])
    key = library_object_key(tenant_id, name, version, "SKILL.md")
    try:
        data = await store.get(key)
    except Exception:
        logger.warning("library skill %s could not be fetched; skipped", key, exc_info=True)
        return None
    if not data:
        logger.warning("library skill %s is live but has no SKILL.md; skipped", key)
        return None
    if not row.get("sha256") or hashlib.sha256(data).hexdigest() != row["sha256"]:
        # The bytes are not the ones that were reviewed and published.
        logger.warning("library skill %s does not match its saved digest; skipped", key)
        return None
    try:
        skill = parse_skill_md(data.decode("utf-8"), fallback_name=name, path=key)
    except Exception:
        logger.warning("library skill %s could not be read; skipped", key, exc_info=True)
        return None
    if skill is None or skill.name != name:
        return None
    skill.source = "library"
    skill.version = version
    return skill


async def _library_catalog(
    settings: Any, *, tenant_id: str, object_store: Any, wanted: Callable[[str], bool]
) -> dict[str, Skill]:
    """The live library skills `wanted` keeps, by name.

    One query for each live skill's version and SKILL.md digest, then those SKILL.md files,
    fetched concurrently. Only `live_version` is followed: a draft, a rejected draft and a
    superseded version have rows and objects, and none of them reaches a catalog.

    Fails closed. If the library store cannot be read, the catalog has no library skills --
    and nothing else can stand in for them, because library bytes live under their own
    prefix (`library_object_key`) that no other source in this module reads.
    """
    if settings is None or object_store is None:
        return {}
    from felix.skills.library_store import get_skill_library_store

    try:
        rows = await get_skill_library_store(settings).list_live(tenant_id)
    except Exception:
        # The library is an addition to the catalog, never a precondition for one.
        logger.warning("skill library unavailable; catalog built without it", exc_info=True)
        return {}
    live = [r for r in rows if wanted(str(r["name"]))]
    gate = asyncio.Semaphore(_LIBRARY_FETCH_CONCURRENCY)

    async def fetch(row: dict[str, Any]) -> Skill | None:
        async with gate:
            return await _library_skill(object_store, tenant_id=tenant_id, row=row)

    skills = await asyncio.gather(*(fetch(r) for r in live))
    return {s.name: s for s in skills if s is not None}


def _read_skill_file(path: Path, *, fallback_name: str) -> Skill | None:
    """One file's skill, or None. A file that cannot be read or parsed is logged and
    skipped; it must not take the rest of the directory's catalog down with it."""
    try:
        return parse_skill_md(path.read_text(encoding="utf-8"), fallback_name=fallback_name, path=str(path))
    except Exception:
        logger.warning("skill file %s could not be read; skipped", path, exc_info=True)
        return None


def load_skills_from_dir(root: Path) -> SkillCatalog:
    """Discover SKILL.md directories and root .md skill files under ``root``."""
    catalog = SkillCatalog()
    if not root.is_dir():
        return catalog
    for skill_md in root.rglob("SKILL.md"):
        skill = _read_skill_file(skill_md, fallback_name=skill_md.parent.name)
        if skill and skill.name not in catalog.skills:
            catalog.skills[skill.name] = skill
    for md in root.glob("*.md"):
        if md.name.upper() == "SKILL.MD":
            continue
        skill = _read_skill_file(md, fallback_name=md.stem)
        if skill and skill.name not in catalog.skills:
            catalog.skills[skill.name] = skill
    return catalog


# How long a bundled catalog is reused without re-checking the directory.
#
# Measured on this checkout: probing the three candidate directories costs 20.0 µs, the
# `rglob` walk 25.5 µs, and reading plus parsing the rest of 56.6 µs total -- per chat
# request, synchronously, on the event loop. The walk dominates, which rules out any
# cache key that needs a walk to compute: `rglob` + `stat` each is 26.7 µs, barely
# better than just doing the work.
#
# So the key is one `stat` of the root (1.0 µs), which catches a skill being added or
# removed, plus a short TTL that bounds how long an *edit to an existing* SKILL.md can
# go unnoticed -- a nested file's contents change without the root directory's mtime
# moving, and nothing cheap detects that. Five seconds is invisible in production,
# where `skills/` is baked into the image, and short enough that a local edit lands
# before you can alt-tab back to the terminal.
_CATALOG_TTL_SECONDS = 5.0

# (root mtime, monotonic expiry, catalog)
_bundled_cache: dict[str, tuple[float, float, SkillCatalog]] = {}


def _bundled_dir_candidates() -> list[Path]:
    # packages/harness/src/felix/skills/loader.py → repo root skills/
    here = Path(__file__).resolve()
    return [
        here.parents[5] / "skills",  # repo/skills
        here.parents[5] / "manifests" / "skills",
        here.parents[4] / "skills",  # packages/skills (unlikely)
    ]


def _configured_skills_dir() -> Path | None:
    """`FELIX_SKILLS_DIR`, if set and present. Not cached — settings can be reloaded."""
    from felix.config import get_settings

    try:
        raw = (get_settings().skills_dir or "").strip()
    except Exception:
        return None
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    return candidate if candidate.is_dir() else None


@lru_cache(maxsize=1)
def _default_bundled_dir() -> Path | None:
    """Resolved once. Derived from `__file__`, so it cannot change while the process
    runs -- but it was three `is_dir()` probes on every chat request, most of them
    against paths that do not exist."""
    for candidate in _bundled_dir_candidates():
        if candidate.is_dir():
            return candidate
    return None


async def _bundled_catalog(root: Path) -> SkillCatalog:
    """The bundled catalog, cached against the directory's mtime and a short TTL.

    The load runs in a thread: `rglob` plus `read_text` is blocking filesystem work,
    and on a network or container-overlay filesystem it is far worse than the numbers
    above. This repo already forbids blocking imports at module scope; blocking I/O on
    the event loop deserves the same treatment, because it stalls every other request
    on the worker rather than only the one that asked.
    """
    key = str(root)
    try:
        stamp = root.stat().st_mtime
    except OSError:
        return SkillCatalog()
    now = time.monotonic()
    hit = _bundled_cache.get(key)
    if hit is not None and hit[0] == stamp and now < hit[1]:
        return hit[2]
    catalog = await asyncio.to_thread(load_skills_from_dir, root)
    _bundled_cache[key] = (stamp, now + _CATALOG_TTL_SECONDS, catalog)
    return catalog


async def host_catalog(bundled_dir: Path | None = None) -> SkillCatalog:
    """The host's skills: the bundled directory, then `FELIX_SKILLS_DIR` (or ``bundled_dir``).

    A fresh catalog over the cached per-directory ones, so a caller may mutate it. The skill
    library refuses these names, so a tenant's skill can never shadow one the host ships.
    """
    roots: list[Path] = []
    if bundled_dir is None:
        default_dir = _default_bundled_dir()
        if default_dir is not None:
            roots.append(default_dir)
        # FELIX_SKILLS_DIR is searched after the bundled dir, so an operator (or a
        # package that ships skills) can add to the catalog without a repo checkout.
        configured = _configured_skills_dir()
        if configured is not None:
            roots.append(configured)
    else:
        roots.append(bundled_dir)

    host = SkillCatalog()
    for root in roots:
        bundled = await _bundled_catalog(root)
        # A copy: the cached catalog is shared between requests, and callers mutate theirs.
        host.skills.update(bundled.skills)
    return host


def _ref_name_and_version(ref: Any) -> tuple[str | None, Any]:
    if isinstance(ref, dict):
        return ref.get("name"), ref.get("version")
    return getattr(ref, "name", None), getattr(ref, "version", None)


async def load_manifest_skills(
    refs: list[Any],
    *,
    tenant_id: str = "default",
    object_store: Any | None = None,
    bundled_dir: Path | None = None,
    declared_only: bool = False,
    settings: Any | None = None,
) -> SkillCatalog:
    """Resolve a SkillRef list into a SkillCatalog.

    `declared_only` is `spec.skills_declared_only`. False -- the default, and what every
    stored manifest was written against -- seeds the bundled directory and `FELIX_SKILLS_DIR`
    first, so the refs *add to* a host-wide library. True loads nothing but the refs, so the
    catalogue is exactly what the manifest names and a reviewer of that manifest can
    enumerate every prompt fragment the agent may load.

    `declared_only` changes what the host catalogue is *for*, not whether it is built. It
    stops being a seed for the returned catalogue and stays a resolution source: the bundled
    directories are still scanned, and a declared name still resolves against them by an
    in-memory lookup with no object-store round trip. So a restricted manifest costs about
    the same as an unrestricted one, and a declared bundled skill keeps its body rather than
    degrading to the empty placeholder below.

    Precedence is deliberately identical under both settings -- host directory first, tenant
    object store second -- so this narrows *which names* reach the catalogue and never
    *where a body comes from*. `load_skill_from_store` carries the reasoning for that order;
    do not reorder one without the other.

    With ``settings``, the tenant's skill library is the third source: the live version of
    each library skill, after the host directories (the host wins on a name, though the
    library refuses to save one) and ahead of the raw object-store keys. Under `declared_only`
    only declared library names are fetched. The raw keys never hold library bytes -- those
    are under their own prefix, and only a live, digest-checked version is read from it --
    so a declared name the library does not serve falls through to operator uploads only.

    One exception to library-before-uploads: **an explicit pin to an operator upload wins.** A
    ref naming `version: 0.1.0` where `skills/{tenant}/{name}/0.1.0/SKILL.md` (or the shared
    `skills/{name}/0.1.0/SKILL.md`) exists is served that upload, not the library's live
    version, whatever version the library happens to be at. A pin is a manifest author
    choosing reviewed bytes by their key; a tenant's library -- which an agent can draft into
    -- reusing the name must not answer it. An unpinned ref, and a pin no upload holds, still
    get the library's live version.
    """
    catalog = SkillCatalog()
    host = await host_catalog(bundled_dir)
    if not declared_only:
        catalog.skills.update(host.skills)

    declared = {str(n) for n, _ in map(_ref_name_and_version, refs or []) if n}
    library = await _library_catalog(
        settings,
        tenant_id=tenant_id,
        object_store=object_store,
        wanted=lambda n: n not in host.skills and (not declared_only or n in declared),
    )
    if not declared_only:
        for name, skill in library.items():
            catalog.skills.setdefault(name, skill)

    for ref in refs or []:
        name, version = _ref_name_and_version(ref)
        if not name:
            continue
        # `host` rather than `catalog` so a declared name still resolves against the
        # bundled directory when `declared_only` kept it out of the catalogue.
        skill: Skill | None = catalog.get(str(name)) or host.get(str(name)) or library.get(str(name))
        if skill is not None and skill.source == "library" and version:
            pinned = (
                await _pinned_upload(object_store, tenant_id=tenant_id, name=str(name), version=str(version))
                if object_store is not None
                else None
            )
            if pinned is not None:
                skill = pinned
            elif str(version) != skill.version:
                logger.warning(
                    "skill %s pins version %s; the library serves its live version %s",
                    name,
                    version,
                    skill.version,
                )
        if skill is None and object_store is not None:
            skill = await load_skill_from_store(
                object_store, tenant_id=tenant_id, name=str(name), version=version
            )
        if skill is None:
            # Placeholder description so list_skills still surfaces the ref.
            skill = Skill(
                name=str(name),
                description=f"Skill '{name}' (body not found; activate may be empty).",
                body="",
                version=str(version) if version else None,
            )
        catalog.skills[skill.name] = skill
    return catalog


__all__ = [
    "host_catalog",
    "load_manifest_skills",
    "load_skill_from_store",
    "load_skills_from_dir",
    "operator_skill_keys",
    "parse_skill_md",
    "safe_skill_key_parts",
    "skill_catalog_xml",
]
