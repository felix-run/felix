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
    # A ref `spec.skills` names that no source answered. It stays in the catalog so the
    # management API can report it (`has_body: false`), but the model is never offered it:
    # agentskills.io's client guide hides what cannot be activated rather than listing it.
    unresolved: bool = False

    def listed_description(self) -> str:
        """The description as the model is shown it -- in the system-prompt catalog and by
        `list_skills` alike: as written, except an imported skill's is withheld (empty) when it
        carries the markers content screening quarantines tool output for."""
        if self.untrusted and looks_injected(self.description):
            return ""
        return self.description


def _invocable(skill: Skill) -> bool:
    return not skill.disable_model_invocation and not skill.unresolved


@dataclass(slots=True)
class SkillCatalog:
    """In-memory catalog for one agent build."""

    skills: dict[str, Skill] = field(default_factory=dict)

    def list_public(self) -> list[Skill]:
        """What the model is offered, by name: sorted, so the catalog block in the system prompt
        is the same bytes whichever source answered first, and a provider's cached prefix holds."""
        return sorted(
            (s for s in self.skills.values() if _invocable(s)),
            key=lambda s: s.name,
        )

    def get(self, name: str) -> Skill | None:
        return self.skills.get(name)

    def get_invocable(self, name: str) -> Skill | None:
        """``name`` when the model may use it. A skill hidden from the catalog
        (`disable-model-invocation`, or an unresolved ref) is hidden from the skill tools too:
        a model that guesses the name is told it does not exist, as for any other name."""
        skill = self.skills.get(name)
        return skill if skill is not None and _invocable(skill) else None

    def names(self) -> list[str]:
        return sorted(self.skills)


__all__ = ["Skill", "SkillCatalog", "SkillSource", "looks_injected"]
