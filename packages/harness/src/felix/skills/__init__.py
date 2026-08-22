"""Agent Skills — progressive disclosure capability packages."""

from __future__ import annotations

from felix.skills.loader import load_manifest_skills, parse_skill_md, skill_catalog_xml
from felix.skills.store import get_skill_activation_store
from felix.skills.tools import SKILL_TOOL_NAMES, make_skill_tools
from felix.skills.types import Skill, SkillCatalog

__all__ = [
    "SKILL_TOOL_NAMES",
    "Skill",
    "SkillCatalog",
    "get_skill_activation_store",
    "load_manifest_skills",
    "make_skill_tools",
    "parse_skill_md",
    "skill_catalog_xml",
]
