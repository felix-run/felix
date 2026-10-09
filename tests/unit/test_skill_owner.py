"""The skill library's owner: who a personal library belongs to, and where its bytes and locks go.

The org's spellings are pinned to what they were before owners existed: a changed object key would
strand every stored skill's bytes, and a changed lock key would let replicas either side of a
deploy race past the pending cap.
"""

from __future__ import annotations

import pytest
from felix.skills.library_keys import (
    ORG_OWNER,
    InvalidSkillOwner,
    library_object_key,
    pending_lock_key,
    personal_owner,
    require_owner,
)
from felix.skills.library_store import get_skill_library_store


def test_a_principal_owns_issuer_and_subject_together() -> None:
    assert personal_owner("https://id.example", "alice") == "https://id.example|alice"
    assert personal_owner("api_key", "alice") != personal_owner("https://id.example", "alice")


@pytest.mark.parametrize(
    ("issuer", "subject"),
    [("anonymous", ""), ("", "alice"), ("a|b", "c"), ("iss", "x" * 600), ("iss", "line\nbreak")],
    ids=["no-subject", "no-issuer", "pipe-in-issuer", "too-long", "unprintable"],
)
def test_a_principal_that_cannot_be_told_apart_has_no_personal_library(issuer: str, subject: str) -> None:
    assert personal_owner(issuer, subject) is None


def test_a_pipe_in_the_subject_still_names_one_owner() -> None:
    # The issuer cannot hold `|`, so the first one splits the pair unambiguously.
    assert personal_owner("iss", "a|b") == "iss|a|b"
    assert personal_owner("iss|a", "b") is None


@pytest.mark.parametrize(
    ("owner", "rule"),
    [
        ("alice", "issuer|subject"),
        ("|alice", "issuer|subject"),
        ("iss|", "issuer|subject"),
        ("|", "issuer|subject"),
        ("x" * 513 + "|y", "at most 512"),
        ("iss|\x00", "printable"),
    ],
    ids=["bare", "no-issuer", "no-subject", "neither", "long", "nul"],
)
def test_a_malformed_owner_is_refused_naming_the_rule_and_never_the_value(owner: str, rule: str) -> None:
    with pytest.raises(InvalidSkillOwner, match=rule.replace("|", r"\|")) as refused:
        require_owner(owner)
    # The message is the rule, never the value (which may hold an email); `|` is in the rule.
    assert owner not in str(refused.value) or owner in rule
    with pytest.raises(InvalidSkillOwner):
        get_skill_library_store(None, owner=owner)
    with pytest.raises(InvalidSkillOwner):
        library_object_key("acme", "notes", "0.1.0", "SKILL.md", owner=owner)


def test_an_object_key_cannot_be_spelled_without_an_owner() -> None:
    """Personal and org skills share names, so a key with a forgotten owner would put a person's
    bytes over the org's. There is no default to forget."""
    with pytest.raises(TypeError):
        library_object_key("acme", "notes", "0.1.0", "SKILL.md")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        pending_lock_key("acme", "contributor")  # type: ignore[call-arg]


def test_the_org_object_key_is_spelled_as_it_was_before_owners() -> None:
    assert library_object_key("acme", "notes", "0.1.0", "SKILL.md", owner=ORG_OWNER) == (
        "skill-library/acme/notes/0.1.0/SKILL.md"
    )


def test_a_personal_object_key_is_its_own_and_carries_no_subject() -> None:
    alice = library_object_key("acme", "notes", "0.1.0", "SKILL.md", owner="iss|alice@example.com")
    bob = library_object_key("acme", "notes", "0.1.0", "SKILL.md", owner="iss|bob@example.com")
    assert alice.startswith("skill-library/acme/~") and alice.endswith("/notes/0.1.0/SKILL.md")
    assert len(alice.split("/")[2]) == 33, "`~` and 128 bits of digest"
    assert alice != bob
    assert "alice" not in alice and "example" not in alice


def test_a_store_spells_keys_with_its_own_owner() -> None:
    org, alice = (
        get_skill_library_store(None, owner=ORG_OWNER),
        get_skill_library_store(None, owner="iss|alice"),
    )
    assert org.object_key("acme", "notes", "0.1.0", "SKILL.md") == library_object_key(
        "acme", "notes", "0.1.0", "SKILL.md", owner=ORG_OWNER
    )
    assert alice.object_key("acme", "notes", "0.1.0", "SKILL.md") == library_object_key(
        "acme", "notes", "0.1.0", "SKILL.md", owner="iss|alice"
    )


def test_the_org_pending_lock_is_spelled_as_it_was_and_a_personal_one_differs() -> None:
    assert pending_lock_key("acme", "contributor", owner=ORG_OWNER) == "skill_drafts:acme:contributor"
    assert pending_lock_key("acme", "contributor", owner="iss|alice") != pending_lock_key(
        "acme", "contributor", owner=ORG_OWNER
    )


def test_a_store_cannot_be_asked_for_without_naming_whose() -> None:
    """A flow that forgot the owner would read and write the tenant's library for a person."""
    with pytest.raises(TypeError):
        get_skill_library_store(None)  # type: ignore[call-arg]
    assert get_skill_library_store(None, owner=ORG_OWNER).owner == ORG_OWNER
    assert get_skill_library_store(None, owner="iss|alice").owner == "iss|alice"


def test_the_twin_hands_back_one_store_per_owner() -> None:
    """Tests patch a method on the store the factory returns and expect the code under test to
    get that same object, as it did when the twin was one process global."""
    org = get_skill_library_store(None, owner=ORG_OWNER)
    assert org is get_skill_library_store(None, owner=ORG_OWNER)
    alice = get_skill_library_store(None, owner="iss|alice")
    assert alice is get_skill_library_store(None, owner="iss|alice")
    assert alice is not org
    assert alice.for_owner(ORG_OWNER) is org  # type: ignore[attr-defined]
