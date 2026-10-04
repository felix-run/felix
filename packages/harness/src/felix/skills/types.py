"""Agent Skills types — agentskills.io-compatible skill packages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

#: Where a catalog entry came from: the host's directories (the bundled `skills/` and
#: `FELIX_SKILLS_DIR`), an object-store key an operator uploaded, or the tenant's skill library.
SkillSource = Literal["bundled", "store", "library"]


@dataclass(slots=True)
class Skill:
    """A discovered skill (progressive disclosure: name+description always; body on activate)."""

    name: str
    description: str
    body: str = ""
    path: str | None = None
    version: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    disable_model_invocation: bool = False
    source: SkillSource = "bundled"
    # Imported third-party text, or built on it (`skill_version.lineage_import`): what the skill
    # tools return of it is marked untrusted output, so content screening covers it.
    untrusted: bool = False


@dataclass(slots=True)
class SkillCatalog:
    """In-memory catalog for one agent build."""

    skills: dict[str, Skill] = field(default_factory=dict)

    def list_public(self) -> list[Skill]:
        return [s for s in self.skills.values() if not s.disable_model_invocation]

    def get(self, name: str) -> Skill | None:
        return self.skills.get(name)

    def names(self) -> list[str]:
        return sorted(self.skills)


__all__ = ["Skill", "SkillCatalog", "SkillSource"]
