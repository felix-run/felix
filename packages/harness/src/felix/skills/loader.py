"""Load SKILL.md packages from object store, bundled dirs, and inline refs."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from felix.skills.types import Skill, SkillCatalog

logger = logging.getLogger("felix.skills.loader")

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def _parse_frontmatter(raw: str) -> tuple[dict[str, str], str]:
    text = raw.lstrip("\ufeff")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text.strip()
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        meta[key.strip().lower()] = val.strip().strip("\"'")
    return meta, m.group(2).strip()


def parse_skill_md(raw: str, *, fallback_name: str, path: str | None = None) -> Skill | None:
    """Parse a SKILL.md body into a Skill. Returns None if description is missing."""
    meta, body = _parse_frontmatter(raw)
    name = (meta.get("name") or fallback_name).strip().lower()
    description = (meta.get("description") or "").strip()
    if not description:
        logger.warning("skill %s missing description; skipping", name)
        return None
    if len(name) > 64 or not _NAME_RE.match(name):
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


def skill_catalog_xml(catalog: SkillCatalog) -> str:
    """Progressive-disclosure catalog block for the system prompt (agentskills.io style)."""
    public = catalog.list_public()
    if not public:
        return ""
    lines = [
        "You have access to the following skills. Use activate_skill to load full instructions "
        "when a task matches a skill description. Only names and descriptions are listed here.",
        "<available_skills>",
    ]
    for skill in public:
        lines.append(
            f'  <skill name="{skill.name}">\n    <description>{skill.description}</description>\n  </skill>'
        )
    lines.append("</available_skills>")
    return "\n".join(lines)


async def load_skill_from_store(
    store: Any,
    *,
    tenant_id: str,
    name: str,
    version: str | None = None,
) -> Skill | None:
    """Load skills/{tenant}/{name}/SKILL.md or skills/{name}/SKILL.md from an ObjectStore."""
    keys = [
        f"skills/{tenant_id}/{name}/SKILL.md",
        f"skills/{name}/SKILL.md",
    ]
    if version:
        keys.insert(0, f"skills/{tenant_id}/{name}/{version}/SKILL.md")
        keys.insert(1, f"skills/{name}/{version}/SKILL.md")
    for key in keys:
        try:
            data = await store.get(key)
        except Exception:
            logger.debug("object store get failed for %s", key, exc_info=True)
            continue
        if not data:
            continue
        return parse_skill_md(data.decode("utf-8"), fallback_name=name, path=key)
    return None


def load_skills_from_dir(root: Path) -> SkillCatalog:
    """Discover SKILL.md directories and root .md skill files under ``root``."""
    catalog = SkillCatalog()
    if not root.is_dir():
        return catalog
    for skill_md in root.rglob("SKILL.md"):
        try:
            raw = skill_md.read_text(encoding="utf-8")
        except OSError:
            continue
        skill = parse_skill_md(raw, fallback_name=skill_md.parent.name, path=str(skill_md))
        if skill and skill.name not in catalog.skills:
            catalog.skills[skill.name] = skill
    for md in root.glob("*.md"):
        if md.name.upper() == "SKILL.MD":
            continue
        try:
            raw = md.read_text(encoding="utf-8")
        except OSError:
            continue
        skill = parse_skill_md(raw, fallback_name=md.stem, path=str(md))
        if skill and skill.name not in catalog.skills:
            catalog.skills[skill.name] = skill
    return catalog


async def load_manifest_skills(
    refs: list[Any],
    *,
    tenant_id: str = "default",
    object_store: Any | None = None,
    bundled_dir: Path | None = None,
) -> SkillCatalog:
    """Resolve SkillRef list into a SkillCatalog."""
    catalog = SkillCatalog()
    if bundled_dir is None:
        # packages/harness/src/felix/skills/loader.py → repo root skills/
        here = Path(__file__).resolve()
        candidates = [
            here.parents[5] / "skills",  # repo/skills
            here.parents[5] / "manifests" / "skills",
            here.parents[4] / "skills",  # packages/skills (unlikely)
        ]
        for c in candidates:
            if c.is_dir():
                bundled_dir = c
                break

    if bundled_dir is not None:
        bundled = load_skills_from_dir(bundled_dir)
        catalog.skills.update(bundled.skills)

    for ref in refs or []:
        name = getattr(ref, "name", None) or (ref.get("name") if isinstance(ref, dict) else None)
        if not name:
            continue
        version = getattr(ref, "version", None)
        if isinstance(ref, dict):
            version = ref.get("version")
        skill: Skill | None = catalog.get(str(name))
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
    "load_manifest_skills",
    "load_skill_from_store",
    "load_skills_from_dir",
    "parse_skill_md",
    "skill_catalog_xml",
]
