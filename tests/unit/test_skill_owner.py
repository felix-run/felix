"""The skill library's owner: who a personal library belongs to, and where its bytes and locks go.

The org's spellings are pinned to what they were before owners existed: a changed object key would
strand every stored skill's bytes, and a changed lock key would let replicas either side of a
deploy race past the pending cap.
"""

from __future__ import annotations

import pytest
from felix.skills.library_store import (
    ORG_OWNER,
    InvalidSkillOwner,
    check_owner,
    get_skill_library_store,
    library_object_key,
    pending_lock_key,
    skill_owner,
)


def test_a_principal_owns_issuer_and_subject_together() -> None:
    assert skill_owner("https://id.example", "alice") == "https://id.example|alice"
    assert skill_owner("api_key", "alice") != skill_owner("https://id.example", "alice")


@pytest.mark.parametrize(
    ("issuer", "subject"),
    [("anonymous", ""), ("", "alice"), ("a|b", "c"), ("iss", "x" * 600), ("iss", "line\nbreak")],
    ids=["no-subject", "no-issuer", "pipe-in-issuer", "too-long", "unprintable"],
)
def test_a_principal_that_cannot_be_told_apart_has_no_personal_library(issuer: str, subject: str) -> None:
    assert skill_owner(issuer, subject) is None


def test_a_pipe_in_the_subject_still_names_one_owner() -> None:
    # The issuer cannot hold `|`, so the first one splits the pair unambiguously.
    assert skill_owner("iss", "a|b") == "iss|a|b"
    assert skill_owner("iss|a", "b") is None


@pytest.mark.parametrize(
    "owner",
    ["alice", "x" * 513 + "|y", "iss|\x00", "|alice", "iss|", "|"],
    ids=["bare", "long", "nul", "no-issuer", "no-subject", "neither"],
)
def test_a_malformed_owner_is_refused_before_any_store_is_built(owner: str) -> None:
    with pytest.raises(InvalidSkillOwner):
        check_owner(owner)
    with pytest.raises(InvalidSkillOwner):
        get_skill_library_store(None, owner=owner)


def test_the_org_object_key_is_spelled_as_it_was_before_owners() -> None:
    assert (
        library_object_key("acme", "notes", "0.1.0", "SKILL.md") == "skill-library/acme/notes/0.1.0/SKILL.md"
    )
    assert library_object_key("acme", "notes", "0.1.0", "SKILL.md", owner=ORG_OWNER) == (
        "skill-library/acme/notes/0.1.0/SKILL.md"
    )


def test_a_personal_object_key_is_its_own_and_carries_no_subject() -> None:
    alice = library_object_key("acme", "notes", "0.1.0", "SKILL.md", owner="iss|alice@example.com")
    bob = library_object_key("acme", "notes", "0.1.0", "SKILL.md", owner="iss|bob@example.com")
    assert alice.startswith("skill-library/acme/~") and alice.endswith("/notes/0.1.0/SKILL.md")
    assert alice != bob
    assert "alice" not in alice and "example" not in alice


def test_the_org_pending_lock_is_spelled_as_it_was_and_a_personal_one_differs() -> None:
    assert pending_lock_key("acme", "contributor") == "skill_drafts:acme:contributor"
    assert pending_lock_key("acme", "contributor", owner="iss|alice") != pending_lock_key(
        "acme", "contributor"
    )


def test_an_unnamed_store_is_the_orgs() -> None:
    assert get_skill_library_store(None).owner == ORG_OWNER
    assert get_skill_library_store(None, owner="iss|alice").owner == "iss|alice"


def test_the_twin_hands_back_one_store_per_owner() -> None:
    """Tests patch a method on the store the factory returns and expect the code under test to
    get that same object, as it did when the twin was one process global."""
    assert get_skill_library_store(None) is get_skill_library_store(None)
    assert get_skill_library_store(None, owner="iss|alice") is get_skill_library_store(
        None, owner="iss|alice"
    )
    assert get_skill_library_store(None, owner="iss|alice") is not get_skill_library_store(None)
