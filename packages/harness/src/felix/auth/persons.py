"""Which person a caller is, for long-term memory: one person signing in two ways is still one.

A **person key** is `issuer|subject` -- the form a personal skill library already takes
(`skills/library_keys.py:personal_owner`), because a subject is only unique within its issuer: an
API key's `sub` and a JWT's can be the same string, and must not share a memory. The auth
middleware works it out once, from the verified principal (`caller_person`), into
`AuthContext.person`; the recorded caller carries it into a durable run or a job's firing
(`auth/recorded.py`), so later work resolves the same key the live request did.

A caller that names no one has no person key: anonymous, a sub-less API key, an inbound trigger's
run, a machine actor working for no one (`recorded.is_shared_subject`), and a GitHub Actions
workflow (`github-actions:...`, every run of a workflow file). Memory gives them no pool of their
own (`memory/scope.py`).

`FELIX_PRINCIPAL_ALIASES` is the operator's map from a person key to the person it belongs to, as
JSON: `{"felix-self|google:123": "felix-self|github:70701"}`. A value with no `|` means Felix's own
issuer (`SELF_ISSUER`, which signs the GitHub, Google and GitHub Actions sign-ins), so the common
case is written `{"google:123": "github:70701"}`. One hop, on purpose: a target is never
re-resolved, so the map cannot loop, and `parse_principal_aliases` refuses a target that is itself
a key. Neither side may be a subject that names no one, or one alias would hand a person's memory
to every holder of a shared credential.

Read only by long-term memory. Every other subsystem -- approvals, `bind_principal`, a person's
jobs, push devices -- keeps the raw principal.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from felix.config import Settings

# A person key is a personal skill owner, bounded the same way (`library_keys.MAX_OWNER_LENGTH`).
MAX_PERSON_KEY_CHARS = 512


def names_no_person(subject: str) -> bool:
    """Whether `subject` names nobody memory could keep a pool for (see the module docstring)."""
    from felix.auth.github_actions import ACTIONS_SUBJECT_PREFIX
    from felix.auth.recorded import is_shared_subject

    return not subject or is_shared_subject(subject) or subject.startswith(ACTIONS_SUBJECT_PREFIX)


def person_key(issuer: str, subject: str) -> str:
    """`issuer|subject` for a subject that names a person, else `""`. The caller's own values,
    compared exactly: nothing here strips or folds them."""
    from felix.skills.library_keys import personal_owner

    if names_no_person(subject):
        return ""
    return personal_owner(issuer, subject) or ""


def caller_person(*, issuer: str, subject: str, anonymous: bool) -> str:
    """The person key of a verified caller, or `""` for one that names no person."""
    return "" if anonymous else person_key(issuer, subject)


def normalize_person(value: str) -> str:
    """A configured or operator-typed person (an alias, `--to`, a route's `principal`) as a key.

    Stripped -- it is config, not a caller -- and a bare subject is under `SELF_ISSUER`.
    `ValueError` for anything that is not one person's key; the message never repeats the value.
    """
    from felix.auth.jwt import SELF_ISSUER
    from felix.skills.library_keys import InvalidSkillOwner, require_owner

    text = (value or "").strip()
    if not text:
        raise ValueError("a person may not be empty")
    key = text if "|" in text else f"{SELF_ISSUER}|{text}"
    if len(key) > MAX_PERSON_KEY_CHARS:
        raise ValueError(f"a person is at most {MAX_PERSON_KEY_CHARS} characters")
    try:
        require_owner(key)
    except InvalidSkillOwner:
        raise ValueError("a person is issuer|subject, both non-empty and printable") from None
    issuer, _, subject = key.partition("|")
    if person_key(issuer, subject) != key:
        raise ValueError("a person must name one person, not a shared credential or a machine actor")
    return key


@lru_cache(maxsize=8)
def _parsed(raw: str) -> dict[str, str]:
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError("must be a JSON object mapping a person to the person it belongs to") from None
    if not isinstance(data, dict):
        raise ValueError("must be a JSON object mapping a person to the person it belongs to")
    aliases: dict[str, str] = {}
    for alias, person in data.items():
        if not isinstance(person, str):
            raise ValueError("every alias must map to a string")
        aliases[normalize_person(alias)] = normalize_person(person)
    # One hop: a target that is itself an alias would need a second, and `a -> a` is the same refusal.
    if any(person in aliases for person in aliases.values()):
        raise ValueError(
            "a person aliases point at is itself an alias; point every alias straight at the person"
        )
    return aliases


def parse_principal_aliases(settings: Settings) -> dict[str, str]:
    """`FELIX_PRINCIPAL_ALIASES`, validated and normalised. `ValueError` naming what is wrong."""
    return dict(_parsed(str(getattr(settings, "principal_aliases", "") or "")))


def person_of(settings: Settings, key: str) -> str:
    """The person `key` belongs to: its alias target, or itself. Exact: `""` stays `""`."""
    if not key:
        return ""
    return _parsed(str(getattr(settings, "principal_aliases", "") or "")).get(key, key)


__all__ = [
    "MAX_PERSON_KEY_CHARS",
    "caller_person",
    "names_no_person",
    "normalize_person",
    "parse_principal_aliases",
    "person_key",
    "person_of",
]
