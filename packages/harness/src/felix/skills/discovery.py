"""Find the skill packages under a host directory: the bundled `skills/` and `FELIX_SKILLS_DIR`.

agentskills.io's layout -- a directory holding `SKILL.md` -- walked as the spec's client guide asks:
bounded, skipping what is never a skill, saying when one name shadows another. Parsing each
`SKILL.md` stays in `felix.skills.loader`, which re-exports `load_skills_from_dir` and caches it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from felix.bounded_cache import BoundedCache
from felix.logging_setup import loggable
from felix.skills.types import SkillCatalog

logger = logging.getLogger("felix.skills.loader")

# Advisories already logged, by what they are about. A catalog is rebuilt on every request (the
# bundled one every few seconds), so a warning about a SKILL.md that has not changed would repeat
# on each one; it is said once an hour per subject instead.
_ADVISED = BoundedCache(1024, ttl_s=3600.0)


def advise(subject: str, message: str, *args: object) -> None:
    """``logger.warning(message, *args)``, once an hour per (``subject``, ``message``)."""
    key = f"{subject}\0{message}"
    if _ADVISED.get(key) is None:
        _ADVISED[key] = True
        logger.warning(message, *args)


# The walk's bounds, from agentskills.io's client guide ("max depth of 4-6 levels, max 2000
# directories"): `FELIX_SKILLS_DIR` is an operator's path, and pointed at a checkout or a home
# directory an unbounded walk would read every SKILL.md under it on a request.
MAX_SCAN_DEPTH = 6
MAX_SCAN_DIRS = 2000
# Never a skill, and often huge.
_SKIPPED_DIRS = frozenset({"node_modules", "__pycache__"})


def _skill_dirs(root: Path) -> list[Path]:
    """Each directory under ``root`` holding a `SKILL.md`, in a stable order.

    A skill directory is a leaf: nothing beneath it is another skill, so a `SKILL.md` an author
    keeps under `references/` as an example is a file of that skill, not a second one. ``root``
    itself is the exception, as it always was: a `SKILL.md` there is a skill and the walk still
    goes on below it. Hidden directories, `node_modules` and symlinked directories are skipped
    (`rglob` never followed a link either, and one can loop), and the walk stops at
    `MAX_SCAN_DEPTH` levels and `MAX_SCAN_DIRS` directories, with a warning when it does.
    """
    found: list[Path] = []
    queue: list[tuple[Path, int]] = [(root, 0)]
    seen = 0
    while queue:
        directory, depth = queue.pop(0)
        seen += 1
        if seen > MAX_SCAN_DIRS:
            advise(str(root), "skills dir %s: stopped after %d directories", root, MAX_SCAN_DIRS)
            break
        if (directory / "SKILL.md").is_file():
            found.append(directory)
            if depth > 0:
                continue
        if depth >= MAX_SCAN_DEPTH:
            continue
        try:
            children = sorted(p for p in directory.iterdir() if p.is_dir() and not p.is_symlink())
        except OSError:
            continue
        queue.extend(
            (child, depth + 1)
            for child in children
            if not child.name.startswith(".") and child.name not in _SKIPPED_DIRS
        )
    return found


def load_skills_from_dir(root: Path) -> SkillCatalog:
    """Discover the `<dir>/SKILL.md` packages under ``root`` (agentskills.io's layout).

    The first found of a name wins (breadth first, then by path), and a later one is logged as
    shadowed. A name that differs from its directory loads, with a warning: the spec asks them
    to match, and a `spec.skills` ref resolves by the frontmatter name, not the folder.
    """
    from felix.skills.loader import read_skill_file

    catalog = SkillCatalog()
    if not root.is_dir():
        return catalog
    for directory in _skill_dirs(root):
        skill = read_skill_file(directory / "SKILL.md", fallback_name=directory.name)
        if skill is None:
            continue
        if skill.name != directory.name and directory != root:
            advise(
                str(directory),
                "skill %s: its directory is %r; the spec asks the two to match",
                loggable(skill.name, limit=80),
                loggable(directory.name, limit=80),
            )
        kept = catalog.skills.get(skill.name)
        if kept is not None:
            advise(
                str(directory),
                "skill %s at %s is shadowed by %s",
                loggable(skill.name, limit=80),
                directory,
                kept.path,
            )
            continue
        catalog.skills[skill.name] = skill
    return catalog


__all__ = ["MAX_SCAN_DEPTH", "MAX_SCAN_DIRS", "advise", "load_skills_from_dir"]
