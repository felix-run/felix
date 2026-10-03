"""`create_skill` and `update_skill`: an agent writing to its tenant's skill library.

Bound by `manifests/builder.py` for a manifest with `spec.skill_authoring.enabled`, before the
governance stack, so an approvals rule holds the save until a person has read the SKILL.md the
harness renders as the approval preview.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from felix.tools.types import Tool, ToolInput, ToolInvocationCtx, define_tool

logger = logging.getLogger("felix.skills.authoring")

SKILL_AUTHORING_TOOL_NAMES = frozenset({"create_skill", "update_skill"})


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


class _ComposeError(Exception):
    """The call cannot produce a bundle; ``result`` is what the tool returns instead."""

    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__(result.get("error", "compose_error"))
        self.result = result


class _Composed(BaseModel):
    files: dict[str, str]
    parent: str | None = None
    # Whether the parent is the live version and an operator wrote it. An agent's edit of an
    # operator's skill is review material in any mode (`make_skill_authoring_tools`).
    edits_operator_skill: bool = False


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
    """The caller behind this turn, for the audit trail; None outside a request."""
    from felix.context import try_get_context

    ctx = try_get_context()
    sub = getattr(getattr(ctx, "auth", None), "principal_sub", None) if ctx is not None else None
    return str(sub) if sub else None


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
    ) -> None:
        from felix.skills.library_store import get_skill_library_store

        self.settings, self.tenant_id, self.manifest_id = settings, tenant_id, manifest_id
        self.mode, self.max_pending, self.object_store = mode, max_pending, object_store
        self.lib = get_skill_library_store(settings)

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
        return await self._edit(name, skill.get("live_version"), args, body)

    async def _edit(self, name: str, live: str | None, args: ToolInput, body: str) -> _Composed:
        from felix.skills import library
        from felix.skills.format import parse_skill_md, serialize_skill_md

        if live:
            parent = str(live)
        else:
            newest = await self.lib.list_versions(self.tenant_id, name, limit=1)
            if not newest:
                raise _ComposeError({"error": "unknown_skill", "name": name})
            parent = str(newest[0]["version"])
        parent_row = await self.lib.get_version(self.tenant_id, name, parent) or {}
        files = await library.read_version_files(
            self.settings, self.tenant_id, name, parent, object_store=self.object_store
        )
        parsed = parse_skill_md(files.get("SKILL.md", ""))
        frontmatter = dict(parsed.frontmatter) if parsed and isinstance(parsed.frontmatter, dict) else {}
        frontmatter["name"] = name
        if args.get("description"):
            frontmatter["description"] = str(args["description"])
        files["SKILL.md"] = serialize_skill_md(frontmatter, body)
        operator = bool(live) and parent_row.get("source") == "operator"
        return _Composed(files=files, parent=parent, edits_operator_skill=operator)

    async def publish(self, row: dict[str, Any], composed: _Composed) -> dict[str, Any]:
        from felix.skills import library

        if composed.edits_operator_skill:
            return {
                **_draft_result(row, "draft"),
                "review_required": "the live version was written by an operator; a person must publish this",
            }
        try:
            published = await library.publish(
                self.settings,
                self.tenant_id,
                row["name"],
                row["version"],
                by=self.manifest_id,
                object_store=self.object_store,
            )
        except library.SkillPublishBlocked as exc:
            return {**_draft_result(row, "draft"), "publish_blocked": exc.reasons}
        except library.SkillLibraryError as exc:
            return {**_draft_result(row, "draft"), "publish_blocked": [str(exc)]}
        return _draft_result(published, "published")

    async def save(self, args: ToolInput, ctx: ToolInvocationCtx | None, *, update: bool) -> str:
        from felix.skills import library

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
                max_pending=self.max_pending,
                object_store=self.object_store,
            )
        except _ComposeError as exc:
            return json.dumps(exc.result)
        except library.SkillBundleInvalid as exc:
            issues = [{"path": i.path, "message": i.message} for i in exc.issues[:20]]
            return json.dumps({"error": exc.code, "issues": issues})
        except library.SkillLibraryError as exc:
            return json.dumps({"error": exc.code, "detail": str(exc)})
        except Exception:
            logger.warning("skill save failed for %s", args.get("name"), exc_info=True)
            return json.dumps({"error": "save_failed", "name": args.get("name")})
        if self.mode != "publish":
            return json.dumps(_draft_result(row, "draft"))
        return json.dumps(await self.publish(row, composed))

    async def preview(self, args: ToolInput, *, update: bool) -> str:
        try:
            return (await self.compose(args, update=update)).files["SKILL.md"]
        except _ComposeError as exc:
            return json.dumps(exc.result)


def make_skill_authoring_tools(
    settings: Any,
    *,
    tenant_id: str,
    manifest_id: str,
    mode: Literal["draft", "publish"] = "draft",
    max_pending: int = 20,
    object_store: Any | None = None,
) -> list[Tool]:
    """`create_skill` and `update_skill`, writing drafts to the tenant's skill library.

    A draft enters no catalog. With ``mode="publish"`` the draft is published at once if the
    publish gate passes -- except an edit of a skill whose live version an operator wrote,
    which always waits for review. If the gate refuses, the draft stays and the result says
    why. Every refusal comes back as `{"error": ...}`, never as a raise into the loop.
    """
    author = _SkillAuthor(
        settings,
        tenant_id=tenant_id,
        manifest_id=manifest_id,
        mode=mode,
        max_pending=max_pending,
        object_store=object_store,
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
            f"optionally a new description); its other files are kept. {outcome}"
        ),
        args=_UpdateSkillArgs,
        handler=_update,
    )
    # What an approver reads: the SKILL.md the call would save, rendered by the harness from
    # the arguments rather than described by the model.
    create.approval_preview = lambda a: author.preview(a, update=False)
    update.approval_preview = lambda a: author.preview(a, update=True)
    return [create, update]


__all__ = ["SKILL_AUTHORING_TOOL_NAMES", "make_skill_authoring_tools"]
