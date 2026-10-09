"""`create_skill`, `update_skill` and `submit_skill_feedback`: an agent writing to its tenant's
skill library.

Bound by `manifests/builder.py` for a manifest with `spec.skill_authoring.enabled`, before the
governance stack, so an approvals rule holds the save until a person has read the SKILL.md the
harness renders as the approval preview. Feedback changes no skill: a person decides it, and only
a person's accept lets the worker rewrite the skill from it (`skills/feedback.py`).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from felix.skills.library_keys import ORG_OWNER
from felix.skills.publish_gate import gate_source
from felix.skills.types import Skill, SkillCatalog
from felix.tools.types import Tool, ToolInput, ToolInvocationCtx, define_tool

logger = logging.getLogger("felix.skills.authoring")

SKILL_AUTHORING_TOOL_NAMES = frozenset({"create_skill", "update_skill", "submit_skill_feedback"})

_PERSONAL_REFUSAL = {
    "detail": "this is one of the caller's own skills; saving to or filing feedback on a personal "
    "skill is not available, and these tools would act on the tenant's skill of that name instead"
}


def is_personal(skill: Skill) -> bool:
    """Whether ``skill`` came from a caller's personal library rather than the tenant's."""
    from felix.skills.library_keys import ORG_OWNER

    return skill.source == "library" and skill.library_owner not in (None, ORG_OWNER)


def personal_names(catalog: SkillCatalog) -> frozenset[str]:
    """The names in ``catalog`` that are the caller's own skills."""
    return frozenset(n for n, s in catalog.skills.items() if is_personal(s))


class _CreateSkillArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=64,
        description="Lowercase letters, digits and single hyphens, e.g. invoice-triage.",
    )
    description: str = Field(
        min_length=1,
        max_length=1024,
        description="When to use the skill: the one line other agents see before activating it.",
    )
    body: str = Field(min_length=1, description="The instructions, in Markdown. Becomes the SKILL.md body.")
    reason: str = Field(min_length=1, max_length=2000, description="Why this skill is worth keeping.")


class _UpdateSkillArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64, description="An existing library skill.")
    body: str = Field(min_length=1, description="The new instructions, replacing the body entirely.")
    reason: str = Field(min_length=1, max_length=2000, description="What changed and why.")
    description: str | None = Field(
        default=None, min_length=1, max_length=1024, description="A new description; omit to keep it."
    )
    # Required, so an approval binds it: the approvals wrapper hashes a call's arguments into the
    # grant, and an argument naming the parent is the only thing that makes "the edit a person
    # approved" and "the edit that runs" the same edit, whichever process or retry runs it.
    parent_version: str = Field(
        min_length=1,
        max_length=32,
        description=(
            "The skill's newest version, which this edits: `newest_version` from list_skills or "
            "activate_skill, or `version` from your last create_skill / update_skill result. "
            "Refused if the skill has a newer version."
        ),
    )


class _ComposeError(Exception):
    """The call cannot produce a bundle; ``result`` is what the tool returns instead."""

    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__(result.get("error", "compose_error"))
        self.result = result


class _Composed(BaseModel):
    files: dict[str, str]
    parent: str | None = None
    # Whether an operator wrote or imported the parent, live or not. An agent's edit of such a
    # skill is review material in any mode (`make_skill_authoring_tools`).
    edits_operator_skill: bool = False
    # The parent's files the save keeps unchanged, as `{path, sha256}`: what an approver is
    # shown beside the SKILL.md, since the agent's arguments never name them.
    inherited: list[dict[str, str]] = []


def _review_hint(row: dict[str, Any]) -> str:
    failed = [
        str(c.get("message") or c.get("label")) for c in row.get("review_checks") or [] if not c["passed"]
    ]
    if not failed:
        return "Every review check passed."
    return ("To raise the quality score: " + "; ".join(failed))[:600]


def _draft_result(row: dict[str, Any], status: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status,
        "name": row["name"],
        "version": row["version"],
        "quality_score": row["quality_score"],
        "security_status": row["security_status"],
        "review_hint": _review_hint(row),
    }
    issues = row.get("security_issues") or []
    if issues:
        result["issues"] = [
            {"severity": i["severity"], "path": i["path"], "message": i["message"]} for i in issues[:20]
        ]
    return result


def _principal() -> str | None:
    """The caller behind this turn, for the audit trail; None outside a request.

    `on_behalf_of` first, as the approvals wrapper reads it: a resumed durable fiber runs as
    principal `fiber`, and the person whose work it is is the one the trail should name.
    """
    from felix.context import try_get_context

    ctx = try_get_context()
    auth = getattr(ctx, "auth", None) if ctx is not None else None
    sub = (getattr(auth, "on_behalf_of", None) or getattr(auth, "principal_sub", None)) if auth else None
    return str(sub) if sub else None


def _preview_header(name: str, composed: _Composed, source: str) -> str:
    lines = [f"update_skill {name}: edited from {composed.parent} (written by {source or 'unknown'})"]
    if composed.inherited:
        lines.append(f"Kept unchanged from {composed.parent}:")
        lines += [f"  {f['path']}  sha256:{f['sha256']}" for f in composed.inherited]
    else:
        lines.append("No other files are kept from the parent.")
    return "\n".join(lines) + "\n\n--- SKILL.md ---\n"


class _SkillAuthor:
    """One manifest's authoring calls, bound to a tenant and the stores it writes."""

    def __init__(
        self,
        settings: Any,
        *,
        tenant_id: str,
        manifest_id: str,
        mode: Literal["draft", "publish"],
        max_pending: int,
        object_store: Any | None,
        auto_eval: bool = False,
        personal: frozenset[str] = frozenset(),
    ) -> None:
        from felix.skills.library_store import get_skill_library_store

        self.settings, self.tenant_id, self.manifest_id = settings, tenant_id, manifest_id
        self.mode, self.max_pending, self.object_store = mode, max_pending, object_store
        self.auto_eval = auto_eval
        self.personal = personal
        self.lib = get_skill_library_store(settings, owner=ORG_OWNER)

    async def queue_eval(self, row: dict[str, Any]) -> str | None:
        """`skill_authoring.auto_eval`: queue an evaluation of the draft just saved. A failure to
        queue never fails the save; the result just carries no `eval_id`."""
        if not self.auto_eval:
            return None
        from felix.skills.evaluate import queue_eval

        try:
            queued = await queue_eval(
                self.settings, self.tenant_id, row["name"], row["version"], requested_by=self.manifest_id
            )
        except Exception:
            logger.warning("auto_eval could not queue %s@%s", row["name"], row["version"], exc_info=True)
            return None
        return str(queued["id"])

    async def compose(self, args: ToolInput, *, update: bool) -> _Composed:
        """The bundle a call would save, and what it was edited from."""
        from felix.skills.format import serialize_skill_md

        name = str(args.get("name") or "")
        body = f"\n{args.get('body') or ''}"
        skill = await self.lib.get_skill(self.tenant_id, name)
        if not update:
            if skill is not None:
                raise _ComposeError({"error": "skill_exists", "name": name, "detail": "use update_skill"})
            frontmatter = {"name": name, "description": str(args.get("description") or "")}
            return _Composed(files={"SKILL.md": serialize_skill_md(frontmatter, body)})
        if skill is None:
            raise _ComposeError(
                {"error": "unknown_skill", "name": name, "detail": "not in the skill library"}
            )
        return await self._edit(name, args, body)

    async def _edit(self, name: str, args: ToolInput, body: str) -> _Composed:
        """An edit of ``parent_version``, which must be the newest version that was not
        rejected: a rejected draft's files must not ride into the next one. Checked here for the
        preview and again, atomically with the save, by `save_draft(expect_newest=...)`."""
        from felix.skills import library
        from felix.skills.format import parse_skill_md, serialize_skill_md
        from felix.skills.library_store import is_rejected

        parent = str(args.get("parent_version") or "")
        newest = (
            await library.newest_buildable_versions(
                self.settings, self.tenant_id, [name], owner=self.lib.owner
            )
        ).get(name)
        if newest is None:
            raise _ComposeError({"error": "unknown_skill", "name": name})
        if parent != newest:
            named = await self.lib.get_version(self.tenant_id, name, parent) if parent else None
            error = "parent_rejected" if named is not None and is_rejected(named) else "parent_changed"
            raise _ComposeError({"error": error, "name": name, "expected": parent, "current": newest})
        parent_row = await self.lib.get_version(self.tenant_id, name, parent) or {}
        file_rows = await self.lib.list_files(self.tenant_id, name, parent)
        files = await library.read_version_files(
            self.settings, self.tenant_id, name, parent, object_store=self.object_store, owner=self.lib.owner
        )
        parsed = parse_skill_md(files.get("SKILL.md", ""))
        frontmatter = dict(parsed.frontmatter) if parsed and isinstance(parsed.frontmatter, dict) else {}
        frontmatter["name"] = name
        if args.get("description"):
            frontmatter["description"] = str(args["description"])
        files["SKILL.md"] = serialize_skill_md(frontmatter, body)
        inherited = [
            {"path": str(r["path"]), "sha256": str(r["sha256"])} for r in file_rows if r["path"] != "SKILL.md"
        ]
        return _Composed(
            files=files,
            parent=parent,
            edits_operator_skill=gate_source(parent_row) in {"operator", "import"},
            inherited=inherited,
        )

    async def publish(self, row: dict[str, Any], composed: _Composed) -> dict[str, Any]:
        from felix.skills import library

        if composed.edits_operator_skill:
            return {
                **_draft_result(row, "draft"),
                "review_required": (
                    "the version this edits was written or imported by an operator; "
                    "a person must publish this"
                ),
            }
        try:
            published = await library.publish(
                self.settings,
                self.tenant_id,
                row["name"],
                row["version"],
                by=self.manifest_id,
                object_store=self.object_store,
                owner=self.lib.owner,
            )
        except library.SkillPublishBlocked as exc:
            return {**_draft_result(row, "draft"), "publish_blocked": exc.reasons}
        except library.SkillLibraryError as exc:
            return {**_draft_result(row, "draft"), "publish_blocked": [str(exc)]}
        return _draft_result(published, "published")

    async def save(self, args: ToolInput, ctx: ToolInvocationCtx | None, *, update: bool) -> str:
        from felix.skills import library

        if str(args.get("name") or "") in self.personal:
            # These tools write the tenant's library. A name that is the caller's own skill in
            # this catalog would be saved -- and in publish mode published -- as the tenant's,
            # built on the tenant's skill of that name rather than the one the model read.
            return json.dumps({"error": "personal_skill", **_PERSONAL_REFUSAL, "name": args.get("name")})
        try:
            composed = await self.compose(args, update=update)
            row = await library.save_draft(
                self.settings,
                self.tenant_id,
                files=composed.files,
                name=str(args["name"]),
                provenance=library.DraftProvenance(
                    source="agent",
                    author=self.manifest_id,
                    reason=str(args.get("reason") or ""),
                    origin_manifest_id=self.manifest_id,
                    session_id=getattr(ctx, "thread_id", None),
                    principal=_principal(),
                ),
                parent=composed.parent,
                expect_newest=composed.parent if update else library.MUST_NOT_EXIST,
                max_pending=self.max_pending,
                object_store=self.object_store,
                owner=self.lib.owner,
            )
        except _ComposeError as exc:
            return json.dumps(exc.result)
        except library.SkillBundleInvalid as exc:
            issues = [{"path": i.path, "message": i.message} for i in exc.issues[:20]]
            return json.dumps({"error": exc.code, "issues": issues})
        except library.SkillParentChanged as exc:
            # Lost a race between the check in `_edit` and the save: same answer as the check.
            return json.dumps({"error": exc.code, "name": args.get("name"), "detail": str(exc)})
        except library.SkillLibraryError as exc:
            return json.dumps({"error": exc.code, "detail": str(exc)})
        except Exception:
            logger.warning("skill save failed for %s", args.get("name"), exc_info=True)
            return json.dumps({"error": "save_failed", "name": args.get("name")})
        eval_id = await self.queue_eval(row)
        result = _draft_result(row, "draft") if self.mode != "publish" else await self.publish(row, composed)
        if eval_id is not None:
            result["eval_id"] = eval_id
        return json.dumps(result)

    async def preview(self, args: ToolInput, *, update: bool) -> str:
        """What an approver reads: the SKILL.md, and for an edit, the version it builds on and
        every file it keeps from that version by digest."""
        try:
            composed = await self.compose(args, update=update)
        except _ComposeError as exc:
            return json.dumps(exc.result)
        if not update or composed.parent is None:
            return composed.files["SKILL.md"]
        parent_row = await self.lib.get_version(self.tenant_id, str(args.get("name")), composed.parent) or {}
        header = _preview_header(str(args.get("name")), composed, str(parent_row.get("source") or ""))
        return header + composed.files["SKILL.md"]


def make_skill_authoring_tools(
    settings: Any,
    *,
    tenant_id: str,
    manifest_id: str,
    mode: Literal["draft", "publish"] = "draft",
    max_pending: int = 20,
    object_store: Any | None = None,
    auto_eval: bool = False,
    personal: frozenset[str] = frozenset(),
) -> list[Tool]:
    """`create_skill` and `update_skill`, writing drafts to the tenant's skill library.

    A draft enters no catalog. With ``mode="publish"`` the draft is published at once if the
    publish gate passes -- except an edit of a version an operator wrote, which always waits
    for review. `update_skill` edits the version its required `parent_version` names, which
    must be the newest; that argument is what an approval of the call binds. If the gate
    refuses, the draft stays and the result says why. Every refusal comes back as
    `{"error": ...}`, never as a raise into the loop.
    """
    author = _SkillAuthor(
        settings,
        tenant_id=tenant_id,
        manifest_id=manifest_id,
        mode=mode,
        max_pending=max_pending,
        object_store=object_store,
        auto_eval=auto_eval,
        personal=personal,
    )

    async def _create(args: _CreateSkillArgs, ctx: ToolInvocationCtx | None = None) -> str:
        return await author.save(args.model_dump(), ctx, update=False)

    async def _update(args: _UpdateSkillArgs, ctx: ToolInvocationCtx | None = None) -> str:
        return await author.save(args.model_dump(exclude_none=True), ctx, update=True)

    outcome = (
        "It is published at once if it passes the publish gate; otherwise it waits as a draft."
        if mode == "publish"
        else "It is saved as a draft and enters no catalog until an operator publishes it."
    )
    create = define_tool(
        name="create_skill",
        description=(
            "Save a reusable skill — instructions for a task you expect to repeat — to this "
            f"tenant's skill library. {outcome}"
        ),
        args=_CreateSkillArgs,
        handler=_create,
    )
    update = define_tool(
        name="update_skill",
        description=(
            "Save a new version of a skill in this tenant's library with a new body (and "
            "optionally a new description); its other files are kept. Pass the skill's newest "
            "version as parent_version (`newest_version` from list_skills or activate_skill, or "
            "the `version` your last save returned); a stale one is refused with parent_changed "
            f"and the current version. {outcome}"
        ),
        args=_UpdateSkillArgs,
        handler=_update,
    )
    # What an approver reads: the SKILL.md the call would save, rendered by the harness from
    # the arguments rather than described by the model.
    create.approval_preview = lambda a: author.preview(a, update=False)
    update.approval_preview = lambda a: author.preview(a, update=True)
    return [create, update]


class _FeedbackArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64, description="A library skill, as list_skills shows it.")
    body: str = Field(
        min_length=1,
        max_length=4000,
        description="What the skill got wrong or left out, and what it should say instead.",
    )
    suggested_patch: str | None = Field(
        default=None, max_length=16000, description="Optional: replacement text for the part that is wrong."
    )


def _feedback_result(row: dict[str, Any]) -> dict[str, Any]:
    """What `submit_skill_feedback` tells the model about the feedback it filed."""
    return {
        "status": "pending",
        "feedback_id": row["id"],
        "name": row["name"],
        "target_version": row["target_version"],
        "detail": "An operator reads feedback; the skill is unchanged until a person accepts it.",
    }


def _feedback_preview(name: str, version: str | None, args: ToolInput) -> str:
    lines = [f"submit_skill_feedback {name}@{version or '?'}", "", str(args.get("body") or "")]
    if args.get("suggested_patch"):
        lines += ["", "--- suggested patch ---", str(args["suggested_patch"])]
    return "\n".join(lines)


def make_skill_feedback_tool(
    settings: Any,
    *,
    tenant_id: str,
    manifest_id: str,
    catalog: SkillCatalog,
    max_pending: int = 20,
) -> Tool:
    """`submit_skill_feedback`: file feedback on a library skill in this agent's catalog.

    Only the library skills ``catalog`` holds -- the live versions this agent was given -- take
    feedback, and it is filed against the version the agent read. A host skill has no library
    record to improve. ``max_pending`` caps this manifest's undecided feedback, as it caps its
    drafts. Filing changes nothing: a person accepts or rejects the feedback, and only an accept
    lets the worker rewrite the skill, into a draft for review.
    """
    from felix.logging_setup import loggable

    library_skills = {s.name: s for s in catalog.skills.values() if s.source == "library"}
    personal = {n for n, s in library_skills.items() if is_personal(s)}

    async def _submit(args: _FeedbackArgs, ctx: ToolInvocationCtx | None = None) -> str:
        from felix.skills import feedback, library

        if args.name in personal:
            # Feedback is filed against, and accepted into, the tenant's skill of a name.
            return json.dumps(
                {"error": "personal_skill", **_PERSONAL_REFUSAL, "name": loggable(args.name, limit=64)}
            )
        skill = library_skills.get(args.name)
        if skill is None:
            return json.dumps(
                {
                    "error": "unknown_skill",
                    "name": loggable(args.name, limit=64),
                    "detail": "only library skills in your catalog take feedback",
                }
            )
        try:
            row = await feedback.submit_feedback(
                settings,
                tenant_id,
                name=skill.name,
                body=args.body,
                provenance=feedback.FeedbackProvenance(
                    source="agent", author=manifest_id, principal=_principal(), max_pending=max_pending
                ),
                suggested_patch=args.suggested_patch,
                target_version=skill.version,
            )
        except library.SkillLibraryError as exc:
            return json.dumps({"error": exc.code, "detail": str(exc)})
        except Exception:
            logger.warning("skill feedback failed for %s", skill.name, exc_info=True)
            return json.dumps({"error": "feedback_failed", "name": skill.name})
        return json.dumps(_feedback_result(row))

    tool = define_tool(
        name="submit_skill_feedback",
        description=(
            "Report that a skill from this tenant's library (source `library` in list_skills) is "
            "wrong, unclear or missing something, and what it should say instead. An operator "
            "reviews it; the skill does not change until a person accepts the feedback."
        ),
        args=_FeedbackArgs,
        handler=_submit,
    )

    async def _preview(args: ToolInput) -> str:
        skill = library_skills.get(str(args.get("name") or ""))
        return _feedback_preview(str(args.get("name") or ""), skill.version if skill else None, args)

    tool.approval_preview = _preview
    return tool


__all__ = ["SKILL_AUTHORING_TOOL_NAMES", "make_skill_authoring_tools", "make_skill_feedback_tool"]
