"""Agent Skills types — agentskills.io-compatible skill packages."""

from __future__ import annotations

from dataclasses import dataclass, field


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


__all__ = ["Skill", "SkillCatalog"]
