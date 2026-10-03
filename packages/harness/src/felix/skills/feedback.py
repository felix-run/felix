"""Feedback on a library skill: filed by a person or an agent, decided by a person.

Feedback is what someone said a skill version should do differently. Filing it changes nothing.
A person then accepts or rejects it; an accept with ``improve`` is the only thing that lets the
worker rewrite the skill from it (`skills/improve.py`), and the rewrite is a draft that waits for
review like every agent draft. So an agent that absorbed injected text can file feedback, which
a person reads, and cannot get that text into a skill on its own.

Refusals are `library.SkillLibraryError` subclasses, so the management routes map their stable
``code`` to a status the same way they map the library's.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Mapping
from typing import Any, Literal

from felix.config import Settings
from felix.skills.library import SkillLibraryError, SkillNotFound, newest_version
from felix.skills.library_store import get_skill_library_store
from felix.skills.quality_store import FeedbackSource, SkillFeedbackConflict, get_skill_feedback_store

logger = logging.getLogger("felix.skills.feedback")

now_ms = lambda: int(time.time() * 1000)

MAX_BODY_CHARS = 8000
MAX_PATCH_CHARS = 32000
NOTE_LIMIT = 2000


class FeedbackStateConflict(SkillLibraryError):
    """The feedback was already decided -- accepted, rejected, applied or failed."""

    code = "feedback_conflict"


class FeedbackCapReached(SkillLibraryError):
    """One manifest already has as much undecided feedback as its `max_pending` allows."""

    code = "feedback_cap_reached"


def audit_feedback(
    settings: Settings, tenant_id: str, event_type: str, row: Mapping[str, Any], *, by: str, **extra: Any
) -> None:
    """One event per change to a feedback row, written straight to the audit store (as
    `library._audit`, and for its reason: the worker and the management routes have no request
    context). The body is not in the payload -- it is arbitrary text, and the row keeps it."""
    from felix.audit import store as audit_store

    status = str(extra.pop("status", "ok"))
    payload = {
        "skill": row.get("name"),
        "target_version": row.get("target_version"),
        "feedback_id": row.get("id"),
        "source": row.get("source"),
        "author": row.get("author"),
        "feedback_status": row.get("status"),
        **extra,
    }
    try:
        audit_store.record_event(
            settings,
            tenant_id,
            event_type,
            manifest_id=str(row.get("author") or "") if row.get("source") == "agent" else "",
            principal_subj=by,
            status=status,
            payload=payload,
        )
    except Exception:
        logger.warning("audit write failed for %s", event_type, exc_info=True)


async def _target(settings: Settings, tenant_id: str, name: str, version: str | None) -> str:
    """The version feedback is about: the one named, else the live one, else the newest."""
    lib = get_skill_library_store(settings)
    skill = await lib.get_skill(tenant_id, name)
    if skill is None:
        raise SkillNotFound(f"{name} is not in the library")
    target = version or skill.get("live_version") or newest_version(await lib.version_ids(tenant_id, name))
    if target is None or await lib.get_version(tenant_id, name, target) is None:
        raise SkillNotFound(f"{name}@{version or 'live'} does not exist")
    return str(target)


async def submit_feedback(
    settings: Settings,
    tenant_id: str,
    *,
    name: str,
    body: str,
    source: FeedbackSource,
    author: str,
    principal: str | None = None,
    suggested_patch: str | None = None,
    target_version: str | None = None,
    max_pending: int | None = None,
) -> dict[str, Any]:
    """File feedback on ``name`` (at ``target_version``, or its live version) as `pending`.

    ``max_pending`` caps the undecided feedback one agent's manifest (``author``) may hold, as
    `skill_authoring.max_pending` caps its drafts. Count-then-insert, so two concurrent submits
    at the cap can both land; the cap bounds a looping agent, not an exact count.
    """
    target = await _target(settings, tenant_id, name, target_version)
    store = get_skill_feedback_store(settings)
    if source == "agent" and max_pending is not None:
        held = await store.count_pending_agent(tenant_id, author)
        if held >= max_pending:
            raise FeedbackCapReached(
                f"{author} already has {held} feedback awaiting review (limit {max_pending})"
            )
    row = await store.insert(
        tenant_id,
        {
            "id": str(uuid.uuid4()),
            "name": name,
            "target_version": target,
            "source": source,
            "author": author,
            "principal": principal,
            "body": body[:MAX_BODY_CHARS],
            "suggested_patch": suggested_patch[:MAX_PATCH_CHARS] if suggested_patch else None,
            "status": "pending",
            "created_at": now_ms(),
        },
    )
    audit_feedback(
        settings,
        tenant_id,
        "skill_feedback_submitted",
        row,
        by=principal or author,
        body_chars=len(row["body"]),
        has_patch=bool(row["suggested_patch"]),
    )
    return row


async def _decide(
    settings: Settings,
    tenant_id: str,
    feedback_id: str,
    *,
    status: Literal["accepted", "rejected"],
    improve: bool,
    by: str,
    note: str | None,
) -> dict[str, Any]:
    store = get_skill_feedback_store(settings)
    if await store.get(tenant_id, feedback_id) is None:
        raise SkillNotFound(f"feedback {feedback_id} does not exist")
    try:
        row = await store.decide(
            tenant_id,
            feedback_id,
            status=status,
            improve=improve,
            by=by,
            note=(note or "")[:NOTE_LIMIT] or None,
            at=now_ms(),
        )
    except SkillFeedbackConflict as exc:
        raise FeedbackStateConflict(f"feedback {feedback_id} was already decided") from exc
    event = "skill_feedback_accepted" if status == "accepted" else "skill_feedback_rejected"
    audit_feedback(settings, tenant_id, event, row, by=by, improve=improve)
    return row


async def accept_feedback(
    settings: Settings,
    tenant_id: str,
    feedback_id: str,
    *,
    by: str,
    improve: bool = True,
    note: str | None = None,
) -> dict[str, Any]:
    """Accept pending feedback. With ``improve`` the worker rewrites the skill from it, into a
    draft; without, it is recorded as accepted and nothing runs."""
    return await _decide(
        settings, tenant_id, feedback_id, status="accepted", improve=improve, by=by, note=note
    )


async def reject_feedback(
    settings: Settings, tenant_id: str, feedback_id: str, *, by: str, note: str
) -> dict[str, Any]:
    """Reject pending feedback, recording why. Nothing runs."""
    return await _decide(settings, tenant_id, feedback_id, status="rejected", improve=False, by=by, note=note)


__all__ = [
    "MAX_BODY_CHARS",
    "MAX_PATCH_CHARS",
    "FeedbackCapReached",
    "FeedbackStateConflict",
    "accept_feedback",
    "audit_feedback",
    "reject_feedback",
    "submit_feedback",
]
