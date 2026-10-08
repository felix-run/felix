"""Agent Skills types — agentskills.io-compatible skill packages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

#: Where a catalog entry came from: the host's directories (the bundled `skills/` and
#: `FELIX_SKILLS_DIR`), an object-store key an operator uploaded, or the tenant's skill library.
SkillSource = Literal["bundled", "store", "library"]


def looks_injected(text: str) -> bool:
    """Whether ``text`` carries the injection markers content screening quarantines output for."""
    from felix.governance.content_screening import _INJECTION

    return any(rx.search(text) for rx in _INJECTION)


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
    # A library skill's library (`library_keys.ORG_OWNER` or a personal owner): where its bundle
    # files and versions are read from. None for every other source. A personal skill may share
    # its name and version with the tenant's, so the name alone does not say whose files are whose.
    library_owner: str | None = None

    def listed_description(self) -> str:
        """The description as the model is shown it -- in the system-prompt catalog and by
        `list_skills` alike: as written, except an imported skill's is withheld (empty) when it
        carries the markers content screening quarantines tool output for."""
        if self.untrusted and looks_injected(self.description):
            return ""
        return self.description


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


__all__ = ["Skill", "SkillCatalog", "SkillSource", "looks_injected"]
