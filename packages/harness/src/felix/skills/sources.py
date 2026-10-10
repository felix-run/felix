"""Who wrote a library version, and what follows from it: one table, read wherever a rule depends
on a version's `source`.

The rules were membership sets spread across `library.py`, `publish_gate.py` and `authoring.py`, so
a new source had to be found in each of them -- and a set it was missing from failed open. Here a
source is one row; a rule asks its row, and an unknown source answers as strictly as any row does
(`needs_review_when_agent_edits`).

A leaf module: no store, nothing from `felix.skills`, so `publish_gate` and `library` both import it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# `import`: fetched from an external source (`skills/importer.py`) at a person's request.
# Third-party text, so the gate treats it at least as strictly as an agent's draft
# (`publish_gate.policy_for_source`, `publish_gate.gate_scenario_source`).
# `promoted`: copied from a personal library into the tenant's (`library.promote`) as a draft for
# review. No reviewer of the tenant's has read it and an agent may have written it in that library,
# so the gate and the copy rule treat it as they treat an agent's draft.
SkillSourceKind = Literal["agent", "operator", "import", "promoted"]


@dataclass(slots=True, frozen=True)
class SourceTraits:
    """What a version's source decides."""

    # How a message names a version of this source: "an agent's draft", "a promoted draft".
    label: str
    # Why only the bundle's own `evals/` scenarios count toward the gate for this source -- its
    # author could steer generated or default ones (`publish_gate.eval_counts_for_gate`); None
    # when any succeeded evaluation counts.
    untrusted_eval: str | None
    # An undecided draft of this source is text no reviewer of the tenant's has read: an adopt
    # will not vouch for it, and an agent's edit built on it inherits the need for review.
    unreviewed_until_decided: bool
    # A save of this source is held to the copy rule (`library._lineage_import`): its files are
    # searched for the tenant's imported text.
    copy_rule: bool
    # Saved only into the tenant's library, never a personal one (`library._check_personal`).
    org_only: bool
    # A save builds on the newest version that was not rejected, rather than the newest of all.
    builds_on_buildable: bool
    # An agent's edit of a version of this source goes to a person in every authoring mode: a
    # person wrote, imported or promoted it (`authoring`).
    needs_review_when_agent_edits: bool


SOURCES: dict[str, SourceTraits] = {
    "agent": SourceTraits(
        label="an agent's",
        untrusted_eval="an agent wrote this version",
        unreviewed_until_decided=True,
        copy_rule=True,
        org_only=False,
        builds_on_buildable=True,
        needs_review_when_agent_edits=False,
    ),
    "operator": SourceTraits(
        label="an operator's",
        untrusted_eval=None,
        unreviewed_until_decided=False,
        copy_rule=False,
        org_only=False,
        builds_on_buildable=False,
        needs_review_when_agent_edits=True,
    ),
    "import": SourceTraits(
        label="an imported",
        untrusted_eval="this version was imported",
        unreviewed_until_decided=False,
        copy_rule=False,
        org_only=True,
        builds_on_buildable=True,
        needs_review_when_agent_edits=True,
    ),
    "promoted": SourceTraits(
        label="a promoted",
        untrusted_eval="this version was promoted from a personal library",
        unreviewed_until_decided=True,
        copy_rule=True,
        org_only=True,
        builds_on_buildable=True,
        needs_review_when_agent_edits=True,
    ),
}


def source_traits(source: str | None) -> SourceTraits | None:
    """The row for ``source``, or None for a source this table does not know."""
    return SOURCES.get(source or "")


def needs_review_when_agent_edits(source: str | None) -> bool:
    """Whether an agent's edit of a version of ``source`` must go to a person. True for a source
    the table does not know: a new source is review material until someone decides otherwise."""
    traits = source_traits(source)
    return traits is None or traits.needs_review_when_agent_edits


def unreviewed_until_decided(source: str | None) -> bool:
    traits = source_traits(source)
    return traits is not None and traits.unreviewed_until_decided


def source_label(source: str | None) -> str:
    traits = source_traits(source)
    return traits.label if traits is not None else f"a {source or 'unknown'}"


__all__ = [
    "SOURCES",
    "SkillSourceKind",
    "SourceTraits",
    "needs_review_when_agent_edits",
    "source_label",
    "source_traits",
    "unreviewed_until_decided",
]
