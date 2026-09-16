"""Agent Skills, as an operator can see them.

The subsystem was complete and unreachable: a loader, a catalog, a per-manifest activation
store with a `skill_activation` table and a Postgres arm, and three tools the model calls —
and `grep -rn skill apps/api/src/felix_api/routes/` returned nothing. By this repo's own
rule a surface nothing can reach is inert, and the question an operator actually asks about
skills ("which of these is switched on, and what does the model see when it activates one?")
had no answer that did not involve opening psql.

Read-only, deliberately. Activation is a decision the *model* makes mid-turn on the evidence
of a task, and the store is keyed by `(tenant, manifest)` rather than by thread — so an
operator toggling a skill from outside would be reaching into shared state a run is
concurrently reading, with no turn to attribute it to. Inspecting is the gap; a write
surface is a different feature with a different argument, and the roadmap has it.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query, Request
from felix.auth.mgmt import (
    SCOPE_SKILLS_READ,
    require_mgmt_scopes,
    tenant_id_from_request,
)

if TYPE_CHECKING:  # imports stay lazy at runtime; the annotations are the point
    from felix.config import Settings
    from felix.skills.store import SkillActivationStore
    from felix.skills.types import SkillCatalog

router = APIRouter(tags=["Skills"])


async def _catalog_for(
    settings: Settings, tenant_id: str, manifest_name: str
) -> tuple[SkillCatalog, set[str]]:
    """The catalog a request naming this manifest would compile, and what it declared.

    Resolved the way a request resolves it — stored revision first, bundled YAML behind it
    — so what an operator is shown is what the next turn will actually see, rather than
    what the bundled file happens to say.

    The declared set is returned alongside because it is *not* the catalog.
    `load_manifest_skills` seeds every skill in the bundled directory and in
    `FELIX_SKILLS_DIR` before it resolves a single ref, so `spec.skills` adds to a
    deployment-wide library rather than restricting one: a manifest declaring one skill
    compiles a catalog holding every skill on the host, and `make_skill_tools` offers all
    of them to the model. That is invisible from inside a turn and was invisible from
    outside one too, which is most of the reason this route exists.
    """
    from felix.runtime import resolve_tenant_manifest
    from felix.skills.loader import load_manifest_skills
    from felix.storage import get_object_store

    try:
        resolved = await resolve_tenant_manifest(settings, tenant_id, manifest_name)
    except (LookupError, ValueError) as exc:
        # `LookupError` for an unknown manifest or pinned version; `ValueError` from
        # `assert_valid_manifest_name` for a name that is not one at all, which would
        # otherwise be a 500 with the caller's path segment reflected into the server log.
        # Both are "no such manifest" to a caller, and `routes/chat.py` already pairs them.
        raise HTTPException(status_code=404, detail="not_found") from exc

    refs = resolved.manifest.spec.skills or []
    catalog = await load_manifest_skills(refs, tenant_id=tenant_id, object_store=get_object_store(settings))
    declared = {str(getattr(ref, "name", "") or "") for ref in refs}
    return catalog, declared - {""}


def _active_store(settings: Settings) -> SkillActivationStore:
    from felix.skills.store import get_skill_activation_store

    return get_skill_activation_store(settings)


@router.get("/{manifest_name}")
async def list_skills(manifest_name: str, request: Request) -> dict[str, object]:
    """Every skill this manifest can reach, and which are currently active.

    `has_body` rather than the body itself: progressive disclosure is the point of the
    design — the model is shown names and descriptions, and pays for the instructions only
    when it activates one — so listing every body here would answer a question nobody asked
    with the largest payload on the surface. `GET /{manifest}/{skill}` is where a body lives.

    `model_invocable` surfaces what `catalog.list_public()` filters out: a skill with
    `disable_model_invocation` is in the catalog and *not* offered to the model, which is
    invisible from the model's side by construction and is exactly what an operator is
    checking when they ask why a skill never fires.
    """
    require_mgmt_scopes(request, SCOPE_SKILLS_READ)
    settings = request.app.state.settings
    tenant_id = tenant_id_from_request(request)

    catalog, declared = await _catalog_for(settings, tenant_id, manifest_name)
    active = await _active_store(settings).get_active(tenant_id, manifest_name)

    items = [
        {
            "name": skill.name,
            "description": skill.description,
            "version": skill.version,
            "active": skill.name in active,
            "has_body": bool(skill.body),
            "model_invocable": not skill.disable_model_invocation,
            # False means "reachable but never asked for": present because it is on the
            # host, not because this manifest named it. The model is offered it either way.
            "declared": skill.name in declared,
        }
        for skill in sorted(catalog.skills.values(), key=lambda s: s.name)
    ]
    return {"manifest": manifest_name, "items": items, "active": list(active)}


@router.get("/{manifest_name}/{skill_name}")
async def get_skill(manifest_name: str, skill_name: str, request: Request) -> dict[str, object]:
    """One skill, including the instructions `activate_skill` would return.

    The body is the thing worth inspecting: it is appended to the system prompt on
    activation, so it is prompt content an operator is accountable for and could not read
    without unpacking the object store or the bundled directory by hand.

    A skill named in `spec.skills` whose body was never found still resolves here, carrying
    the loader's placeholder description. That is not an error — the manifest compiles and
    `list_skills` shows the ref — and reporting it as one would hide the more useful fact,
    which is that `has_body` is false and activation would hand the model nothing.
    """
    require_mgmt_scopes(request, SCOPE_SKILLS_READ)
    settings = request.app.state.settings
    tenant_id = tenant_id_from_request(request)

    catalog, declared = await _catalog_for(settings, tenant_id, manifest_name)
    skill = catalog.get(skill_name)
    if skill is None:
        raise HTTPException(status_code=404, detail="not_found")

    from felix.secrets import collected_secret_values, redact_json, redact_text

    active = await _active_store(settings).get_active(tenant_id, manifest_name)
    # Redacted, for the reason `routes/manifests.py` redacts a manifest on `manifests:read`:
    # this is the lower scope and an embedded credential must not ride out on it. The same
    # bytes reaching the *model* are already masked, because `activate_skill` is bound
    # before the governance stack and `apply_secret_masking` scrubs its output -- so without
    # this the HTTP route would be the only path on which a skill body reaches anyone
    # unmasked. Nothing validates SKILL.md frontmatter, and `metadata` carries every key
    # except name and description, so an `api-key:` line ships straight out.
    secrets = collected_secret_values(settings)
    return {
        "name": skill.name,
        "description": skill.description,
        "version": skill.version,
        # The basename only. `path` is absolute and derived from the install prefix, so
        # returning it tells a tenant-scoped caller the container's filesystem layout.
        "filename": PurePosixPath(skill.path).name if skill.path else None,
        "metadata": redact_json(skill.metadata),
        "active": skill.name in active,
        "model_invocable": not skill.disable_model_invocation,
        "declared": skill.name in declared,
        "body": redact_text(skill.body, secrets),
    }


@router.get("/{manifest_name}/activations/recent")
async def recent_activations(
    manifest_name: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, object]:
    """Which skill activated on which turn, newest first.

    The activation *store* answers "what is on now" and cannot answer this: it is keyed by
    `(tenant, manifest)` and overwritten in place, so it holds no history at all. The audit
    trail does — `activate_skill` emits a `tool_call` event like any other tool — and until
    now that event carried the tool's name and not its argument, so it recorded that a skill
    was activated without recording which one. `skills/tools.py` now names it.

    Read from audit rather than from a new table for the same reason: this is a question
    about what happened on a turn, the audit log is already the answer to that class of
    question, and it already has retention, RLS and a manifest-scoped TTL. A second history
    table would be a second thing to sweep.

    Filtered in the store rather than here. Over-fetching a fixed window and narrowing in
    Python looked cheaper and was wrong in a way the caller cannot detect: a tenant whose
    activations on one manifest exceeded the window got an empty list for another, identical
    on the wire to "that manifest has never activated a skill" -- and a model calling
    `deactivate_skill` in a loop could push a real activation out of view, which is
    anti-forensics against the one question this route exists to answer.
    """
    from felix.audit import store as audit_store

    require_mgmt_scopes(request, SCOPE_SKILLS_READ)
    settings = request.app.state.settings
    tenant_id = tenant_id_from_request(request)

    events, _ = await audit_store.list_events(
        settings,
        tenant_id,
        event_type="skill_activation",
        manifest_id=manifest_name,
        limit=limit,
    )
    items = [
        # `payload_json` is what `record_event` writes and what both arms read back; a
        # payload that has been through `redact_json` may be missing a key entirely.
        {
            "ts": event.get("ts"),
            "skill": (event.get("payload_json") or {}).get("skill", ""),
            "action": (event.get("payload_json") or {}).get("action", ""),
            "thread_id": (event.get("payload_json") or {}).get("thread_id", ""),
            # Joins this row to the `tool_call` row `tool_runner` wrote for the same
            # invocation, which is the only way to tell two parallel calls in one batch apart.
            "tool_call_id": (event.get("payload_json") or {}).get("tool_call_id", ""),
            # `ok` means the name was resolved against the catalog before it was stored;
            # `unknown_skill` means the model named something that does not exist, and that
            # value is the only model-supplied text on the row.
            "status": event.get("status", ""),
        }
        for event in events
    ]
    return {"manifest": manifest_name, "items": items}
