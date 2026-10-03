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

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from felix.config import Settings
from felix.skills.feedback_store import get_skill_feedback_store
from felix.skills.library import SkillLibraryError, SkillNotFound, newest_version
from felix.skills.library_store import get_skill_library_store
from felix.skills.quality_store import FeedbackSource, SkillFeedbackAtCap, SkillFeedbackConflict

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


@dataclass(slots=True, frozen=True)
class FeedbackProvenance:
    """Who filed feedback. ``author`` is the manifest id for an agent and the principal for a
    person; ``principal`` is the caller behind an agent's turn, when there was one.
    ``max_pending`` caps the undecided feedback an agent's manifest may hold."""

    source: FeedbackSource
    author: str
    principal: str | None = None
    max_pending: int | None = None


def audit_feedback(
    settings: Settings, tenant_id: str, event_type: str, row: Mapping[str, Any], *, by: str, **extra: Any
) -> None:
    """One event per change to a feedback row. The body is not in the payload -- it is
    arbitrary text, and the row keeps it."""
    from felix.audit.emit import record_offline_event

    status = str(extra.pop("status", "ok"))
    record_offline_event(
        settings,
        tenant_id,
        event_type,
        principal=by,
        status=status,
        manifest_id=str(row.get("author") or "") if row.get("source") == "agent" else "",
        payload={
            "skill": row.get("name"),
            "target_version": row.get("target_version"),
            "feedback_id": row.get("id"),
            "source": row.get("source"),
            "author": row.get("author"),
            "feedback_status": row.get("status"),
            **extra,
        },
    )


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
    provenance: FeedbackProvenance,
    suggested_patch: str | None = None,
    target_version: str | None = None,
) -> dict[str, Any]:
    """File feedback on ``name`` (at ``target_version``, or its live version) as `pending`.

    An agent's `max_pending` is exact: the store counts the manifest's pending feedback and
    inserts the row in one transaction, under a lock per manifest, so submits racing at the cap
    land one at a time and the one past it is refused.
    """
    target = await _target(settings, tenant_id, name, target_version)
    store = get_skill_feedback_store(settings)
    cap = provenance.max_pending if provenance.source == "agent" else None
    try:
        row = await store.insert(
            tenant_id,
            {
                "id": str(uuid.uuid4()),
                "name": name,
                "target_version": target,
                "source": provenance.source,
                "author": provenance.author,
                "principal": provenance.principal,
                "body": body[:MAX_BODY_CHARS],
                "suggested_patch": suggested_patch[:MAX_PATCH_CHARS] if suggested_patch else None,
                "status": "pending",
                "created_at": now_ms(),
            },
            max_pending=cap,
        )
    except SkillFeedbackAtCap as exc:
        raise FeedbackCapReached(
            f"{provenance.author} already has {exc.held} feedback awaiting review (limit {exc.limit})"
        ) from exc
    audit_feedback(
        settings,
        tenant_id,
        "skill_feedback_submitted",
        row,
        by=provenance.principal or provenance.author,
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
    from felix.skills.job_limits import job_caps, refused_at_cap

    try:
        with refused_at_cap():
            row = await store.decide(
                tenant_id,
                feedback_id,
                status=status,
                improve=improve,
                by=by,
                note=(note or "")[:NOTE_LIMIT] or None,
                at=now_ms(),
                caps=job_caps(settings) if improve else None,
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
    draft -- a job, so the tenant's job caps apply (`job_limits`); without, it is recorded as
    accepted and nothing runs."""
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
    "NOTE_LIMIT",
    "FeedbackCapReached",
    "FeedbackProvenance",
    "FeedbackStateConflict",
    "accept_feedback",
    "audit_feedback",
    "reject_feedback",
    "submit_feedback",
]
