"""A caller's authority, recorded for work that runs later, and replayed when it does.

Two kinds of work run after the request that asked for them: a durable fiber, resumed by the
worker (`durability/runs.py` records, `durability/fibers.py` replays), and a scheduled job, fired
by the scheduler for whoever last wrote it (`routes/jobs.py` records, `jobs/scheduler.py`
replays). Both used to spell the record and its reading by hand, and two copies of "which claims
travel, and how a stored list becomes scopes" is how one of them ends up more permissive.

What is recorded is exactly the caller's own -- the person (`acting_for`), their scopes, how they
authenticated, whether they were anonymous, and a credential's manifest pin -- never widened.
What is replayed is a *machine* actor (`fiber`, `cron`) acting on behalf of that person, so an
audit row never says the person did what the worker did later, while every check that is about
the person (`bind_principal`, approvals, `publish_commits`) reads `acting_for` and finds them.

Absent or malformed, a record replays as a principal holding nothing: no scopes, no pin, not
anonymous, scheme `anonymous`. That is the fail-closed direction, and it is what a fiber or a
job recorded before this module existed has always run as.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from felix.auth.actors import MACHINE_ACTORS, TRIGGER_RUN_PREFIX
from felix.auth.api_keys import DEFAULT_API_KEY_SUBJECT
from felix.auth.context import MANIFESTS_CLAIM
from felix.auth.jwt import SELF_ISSUER
from felix.context import AuthContext


def record_caller(auth: AuthContext, *, skill_owner: bool = False) -> dict[str, Any]:
    """`auth`'s authority, as JSON to store beside the work it asked for.

    `skill_owner` records the caller's personal skill library too, for a resume that must compile
    the catalog the request would have (a fiber). A job does not carry it: its runs follow the
    tenant's library, whose instructions every reader of the job can see.
    """
    recorded: dict[str, Any] = {
        # The person, not the worker: work started from inside a durable run is the worker
        # acting for someone, and the record should keep naming that someone.
        "principal_sub": auth.acting_for,
        "scopes": sorted(auth.scopes),
        "anonymous": bool(auth.anonymous),
        "scheme": auth.scheme,
    }
    if skill_owner:
        recorded["skill_owner"] = auth.skill_owner
    if auth.person:
        # Always, unlike the skill library: a job's firing writes the person's memory the way the
        # request would have, and only the request knew the issuer the key is qualified by.
        recorded["person"] = auth.person
    if auth.scheduled:
        # Work a scheduled firing starts stays scheduled work (`AuthContext.scheduled`).
        recorded["scheduled"] = True
    if auth.trigger_run:
        # And so does work a trigger's run starts (`AuthContext.trigger_run`): its durable run.
        recorded["trigger_run"] = True
    if auth.scheme == "self" and auth.raw_claims.get("iss") == SELF_ISSUER:
        # Which sign-in minted the token, and for a Google one the address its account entry is
        # keyed by: what a job's firing re-checks against today's configuration
        # (`jobs/scheduler.py`), since a sign-in token has no other record to check.
        for claim in ("idp", "email"):
            value = auth.raw_claims.get(claim)
            if isinstance(value, str) and value:
                recorded[claim] = value
    if MANIFESTS_CLAIM in auth.raw_claims:
        # A credential pinned to some manifests stays pinned when its work runs later. Kept as
        # written, so a malformed pin is refused then exactly as it was at the door.
        recorded[MANIFESTS_CLAIM] = auth.raw_claims[MANIFESTS_CLAIM]
    return recorded


def token_expiry_ms(auth: AuthContext) -> int | None:
    """The caller's token's own expiry (`exp`), in milliseconds, or None for a credential with none.

    A fiber clamps its run to it, so authority recorded from a 60-second token does not outlive the
    token by the run's whole TTL: there is no revocation in `felix/auth/`, so `exp` is the complete
    bound on a compromised credential.
    """
    exp = auth.raw_claims.get("exp")
    return int(exp) * 1000 if isinstance(exp, (int, float)) else None


def replay_caller(
    recorded: Mapping[str, Any] | None,
    *,
    tenant_id: str,
    actor: str,
    on_behalf_of: str | None = None,
    skill_owner: str | None = None,
    scheduled: bool = False,
) -> AuthContext:
    """The caller later work runs as: `actor` (`fiber`, `cron`), on behalf of the recorded person.

    `on_behalf_of` overrides the recorded person where the work keeps its own column for it (a
    job's `created_by`). `skill_owner` is the caller's to validate and pass; nothing here trusts a
    stored owner string. `scheduled` marks a job's own firing; a recorded caller that was scheduled
    work stays it.
    """
    record = recorded if isinstance(recorded, Mapping) else {}
    scopes = record.get("scopes")
    return AuthContext(
        tenant_id=tenant_id,
        principal_sub=actor,
        on_behalf_of=str(on_behalf_of if on_behalf_of is not None else record.get("principal_sub") or ""),
        # A list, or nothing. `frozenset("admin")` is {"a","d","m","i","n"}.
        scopes=frozenset(str(x) for x in scopes) if isinstance(scopes, list) else frozenset(),
        anonymous=bool(record.get("anonymous", False)),
        scheme=str(record.get("scheme") or "anonymous"),
        skill_owner=skill_owner,
        person=_recorded_person(record, on_behalf_of),
        # Never cleared by a replay: `scheduled` here only adds it, for a job's own firing.
        scheduled=scheduled or record.get("scheduled") is True,
        # Likewise only ever added by a replay.
        trigger_run=record.get("trigger_run") is True,
        raw_claims={MANIFESTS_CLAIM: record[MANIFESTS_CLAIM]} if MANIFESTS_CLAIM in record else {},
    )


def _recorded_person(record: Mapping[str, Any], on_behalf_of: str | None) -> str:
    """The person key recorded beside the caller, or "" when it is absent or does not fit.

    Read back from storage, so it is checked rather than trusted: it must be a well-formed key
    whose subject is the person this replay acts for -- the recorded one, or the job's own
    `created_by` -- or it names someone else's memory, and is dropped (the run then writes none).
    """
    from felix.skills.library_keys import InvalidSkillOwner, require_owner

    person = record.get("person")
    who = str(on_behalf_of if on_behalf_of is not None else record.get("principal_sub") or "")
    if not isinstance(person, str) or not person or not who:
        return ""
    try:
        require_owner(person)
    except InvalidSkillOwner:
        return ""
    issuer, _, subject = person.partition("|")
    if subject != who or not _issuer_fits_scheme(issuer, str(record.get("scheme") or "")):
        return ""
    return person


def _issuer_fits_scheme(issuer: str, scheme: str) -> bool:
    """Whether a recorded key's issuer is one the recorded scheme could have produced.

    The subject check alone let an edit of the stored issuer move a run between sign-ins that
    share a subject string -- `api_key|github:1` to `felix-self|github:1` -- since both name the
    same `principal_sub`. The two issuers Felix spells itself are bound to their schemes; any
    other scheme (a remote verifier, a plugin) may not claim either of them.
    """
    from felix.auth.jwt import SELF_ISSUER

    fixed = {"api_key": "api_key", "self": SELF_ISSUER}
    if scheme in fixed:
        return issuer == fixed[scheme]
    return issuer not in fixed.values()


def is_shared_subject(subject: str) -> bool:
    """Whether `subject` names no person: what a sub-less API key authenticates as, an inbound
    trigger's run (`trigger:<id>`, every sender's), and the machine actors when they act for no
    one (`acting_for` falls back to them). What a person's own tool refuses on -- a scheduled task
    owned by such a subject would be everyone's, and a notice addressed to one would reach every
    holder of the credential. A plugin authenticator that hands out a shared subject is not
    knowable from here; that is its operator's to avoid."""
    return (
        subject == DEFAULT_API_KEY_SUBJECT
        or subject in MACHINE_ACTORS
        or subject.startswith(TRIGGER_RUN_PREFIX)
    )


__all__ = ["is_shared_subject", "record_caller", "replay_caller", "token_expiry_ms"]
