"""Who a skill library belongs to, and where its bytes and locks are spelled. Pure, no I/O.

A library is one tenant's (`ORG_OWNER`) or one principal's within a tenant (`personal_owner`).
`library_store` binds a store to one of them; this module is what the store, the auth side that
names a caller's library, and anything spelling an object key or a lock agree on.

Every key takes its owner explicitly. Personal and org skills may share a name, so an object key
spelled without one would put a person's bytes over the org's; there is no default to forget.
The org's spellings are the ones every key had before owners existed (`0033_skill_owner`), so
stored bytes stay where they are and replicas either side of that deploy take the same locks.
"""

from __future__ import annotations

import hashlib

# The library's own prefix in the object store, deliberately not `skills/`. The operator's
# layout there is `skills/{tenant}/{name}[/{version}]/SKILL.md`, which `load_skill_from_store`
# reads for a declared ref; library bytes under the same keys would let an agent's draft
# overwrite an operator's pinned skill, and be served by a ref that pinned the draft's version.
LIBRARY_PREFIX = "skill-library"

# The tenant's own library. Every row written before `0033` has it.
ORG_OWNER = ""
# A personal owner is `{issuer}|{subject}`; the key column is text, so this is a sanity bound.
MAX_OWNER_LENGTH = 512


class InvalidSkillOwner(ValueError):
    """An owner that is not `ORG_OWNER` and not a usable personal owner. The message names the
    rule that failed and never the value, which may hold an email."""


def personal_owner(issuer: str, subject: str) -> str | None:
    """The personal library a principal owns, or None when it cannot have one.

    The issuer is part of it because a subject is only unique within its issuer: an API key's
    subject and a JWT's could be the same string. An anonymous caller (no subject) has no personal
    library, and neither does an issuer spelled with `|`, which would make two pairs one owner.
    """
    if not subject or not issuer or "|" in issuer:
        return None
    try:
        return require_owner(f"{issuer}|{subject}")
    except InvalidSkillOwner:
        return None


def require_owner(owner: str) -> str:
    """``owner`` when it is `ORG_OWNER` or a well-formed personal owner, else `InvalidSkillOwner`."""
    if not isinstance(owner, str):
        raise InvalidSkillOwner("an owner is a string")
    if owner == ORG_OWNER:
        return owner
    issuer, _, subject = owner.partition("|")
    if not issuer or not subject:
        raise InvalidSkillOwner("a personal owner is issuer|subject, both non-empty")
    if len(owner) > MAX_OWNER_LENGTH:
        raise InvalidSkillOwner(f"a personal owner is at most {MAX_OWNER_LENGTH} characters")
    if not owner.isprintable():
        raise InvalidSkillOwner("a personal owner is printable")
    return owner


def _owner_segment(owner: str) -> str:
    """The key segment for a personal owner: `~` and a digest of it. `~` cannot start a skill
    name, so no personal key can be an org one; the digest keeps a subject (often an email) out of
    every key. 128 bits, so no one can mint an owner whose keys land on someone else's."""
    return "~" + hashlib.sha256(owner.encode()).hexdigest()[:32]


def library_label(owner: str) -> str:
    """Which library a skill came from, fit for an audit row: `org`, or a personal library's
    digest (`~` and 128 bits) -- stable per owner, so an investigator can tell two people's
    `notes` apart and match one across rows, without the row naming the subject."""
    return "org" if require_owner(owner) == ORG_OWNER else _owner_segment(owner)


def library_object_key(tenant_id: str, name: str, version: str, path: str, *, owner: str) -> str:
    """Where one file of one library version lives. The only spelling of that key; a store's
    `object_key` is this with the store's owner."""
    if require_owner(owner) == ORG_OWNER:
        return f"{LIBRARY_PREFIX}/{tenant_id}/{name}/{version}/{path}"
    return f"{LIBRARY_PREFIX}/{tenant_id}/{_owner_segment(owner)}/{name}/{version}/{path}"


def pending_lock_key(tenant_id: str, origin_manifest_id: str, *, owner: str) -> str:
    """The advisory lock a capped draft save takes: one per origin manifest in one library."""
    if require_owner(owner) == ORG_OWNER:
        return f"skill_drafts:{tenant_id}:{origin_manifest_id}"
    return f"skill_drafts:{tenant_id}:{_owner_segment(owner)}:{origin_manifest_id}"


__all__ = [
    "LIBRARY_PREFIX",
    "MAX_OWNER_LENGTH",
    "ORG_OWNER",
    "InvalidSkillOwner",
    "library_label",
    "library_object_key",
    "pending_lock_key",
    "personal_owner",
    "require_owner",
]
