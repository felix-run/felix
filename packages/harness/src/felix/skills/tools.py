"""Skill tools — list / activate / deactivate / read a bundle file with progressive
disclosure, plus `create_skill` / `update_skill` for a manifest with `spec.skill_authoring`."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from felix.audit.emit import emit_agent_audit
from felix.logging_setup import loggable
from felix.skills.binary import is_binary_asset_path
from felix.skills.format import ALLOWED_ROOT_FILES, BUNDLE_DIRS, MAX_BUNDLE_FILES, bundle_path_issue
from felix.skills.store import SkillActivationStore
from felix.skills.types import Skill, SkillCatalog
from felix.tools.types import Tool, ToolInput, ToolInvocationCtx, define_tool

logger = logging.getLogger("felix.skills.tools")

# What one `read_skill_file` returns at most. A reference file is read to be used, not to
# fill the context window; the result says when it was cut.
MAX_READ_CHARS = 64 * 1024


class _SkillNameArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="Skill name to activate or deactivate.")


class _ReadFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="Skill name, as list_skills shows it.")
    path: str = Field(
        min_length=1,
        max_length=512,
        description="A file activate_skill listed, e.g. references/guide.md.",
    )


def _list_dir_bundle(root: Path) -> list[str]:
    files: list[str] = []
    for top in (*BUNDLE_DIRS, *ALLOWED_ROOT_FILES):
        base = root / top
        candidates = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.is_file())
        for p in candidates:
            rel = p.relative_to(root).as_posix()
            if bundle_path_issue(rel) is None:
                files.append(rel)
            if len(files) >= MAX_BUNDLE_FILES:
                return files
    return files


def _bundle_root(skill: Skill) -> Path | None:
    """A host skill's directory — only for a `SKILL.md` in its own folder. A root-level
    `foo.md` skill shares its directory with every other skill, so it has no bundle."""
    if skill.source != "bundled" or not skill.path or Path(skill.path).name != "SKILL.md":
        return None
    return Path(skill.path).parent


async def bundle_files(skill: Skill, *, settings: Any | None, tenant_id: str) -> list[str]:
    """The paths `read_skill_file` can serve for ``skill``, SKILL.md excluded.

    A library version lists its saved files; a host skill its directory. A raw object-store
    skill lists nothing: the store has no listing, though a path the body names still reads.
    """
    if skill.source == "library" and settings is not None and skill.version:
        from felix.skills.library_store import get_skill_library_store

        rows = await get_skill_library_store(settings).list_files(tenant_id, skill.name, skill.version)
        return [str(r["path"]) for r in rows if r["path"] != "SKILL.md"]
    root = _bundle_root(skill)
    if root is None:
        return []
    return await asyncio.to_thread(_list_dir_bundle, root)


def _read_host_file(root: Path, path: str) -> bytes | None:
    base = root.resolve()
    target = (base / path).resolve()
    # Confined after resolving, so a symlink inside the skill cannot lead out of it.
    if not target.is_relative_to(base) or not target.is_file():
        return None
    return target.read_bytes()


async def read_bundle_file(
    skill: Skill, path: str, *, object_store: Any | None, tenant_id: str
) -> bytes | None:
    """One bundle file's bytes, or None. ``path`` must already have passed the allowlist."""
    if skill.source == "library":
        if object_store is None or not skill.version:
            return None
        return await object_store.get(f"skills/{tenant_id}/{skill.name}/{skill.version}/{path}")
    if skill.source == "store":
        if object_store is None or not skill.path or not skill.path.endswith("/SKILL.md"):
            return None
        return await object_store.get(f"{skill.path.removesuffix('SKILL.md')}{path}")
    root = _bundle_root(skill)
    return None if root is None else await asyncio.to_thread(_read_host_file, root, path)


def make_skill_tools(
    catalog: SkillCatalog,
    *,
    activation_store: SkillActivationStore,
    tenant_id: str,
    manifest_id: str,
    settings: Any | None = None,
    object_store: Any | None = None,
) -> list[Tool]:
    """Build list_skills / activate_skill / deactivate_skill / read_skill_file over a catalog.

    ``settings`` and ``object_store`` are what reach a library or object-store skill's bundle
    files; without them `read_skill_file` serves host skills only.
    """

    def _audit(action: str, skill: str, *, status: str, ctx: ToolInvocationCtx | None) -> None:
        """Name the skill in the trail, which the generic tool-call event cannot.

        `tool_runner` already emits a `tool_call` event for every tool, but its payload
        carries the tool's *name* and not its arguments -- so the trail recorded that a
        skill was activated and never which one. Deliberately: a tool's arguments are
        arbitrary caller and model text, and putting them in the audit payload wholesale is
        how a credential ends up in a retained row.

        A skill name is the exception worth making, and only because it is not arbitrary:
        both tools resolve it against the catalog first, so a row carrying `status="ok"`
        holds a name the host declared rather than anything the model typed. The
        `unknown_skill` arms are the only places a model-supplied string is recorded, which
        is why they are bounded below and marked with their own status -- a model probing
        for skills it has not been granted is itself the thing an operator wants to see.

        `tool_call_id` rides along because `tool_runner` writes it on the `tool_call` event
        for the same invocation, and it is the only thing that can join the two. Parallel
        tool calls in one batch produce rows with the same `thread_id` and adjacent `ts`;
        without this there is nothing to tell them apart. Audit payloads are the hardest
        thing here to change later -- rows are retained under a manifest TTL and fanned out
        to an operator's sink on write -- so the key goes in now or never.
        """
        emit_agent_audit(
            "skill_activation",
            status=status,
            manifest_id=manifest_id,
            payload={
                "action": action,
                "skill": loggable(skill, limit=64),
                "thread_id": getattr(ctx, "thread_id", None) or "",
                "tool_call_id": getattr(ctx, "tool_call_id", None) or "",
            },
        )

    async def _list(_args: dict[str, Any] | None = None, _ctx: ToolInvocationCtx | None = None) -> str:
        active = await activation_store.get_active(tenant_id, manifest_id)
        payload = [
            {
                "name": s.name,
                "description": s.description,
                "active": s.name in active,
                "has_body": bool(s.body),
                "source": s.source,
            }
            for s in catalog.list_public()
        ]
        # Also surface disable_model_invocation skills as inactive-only via list? skip per spec.
        return json.dumps(payload)

    async def _activate(args: _SkillNameArgs, _ctx: ToolInvocationCtx | None = None) -> str:
        skill = catalog.get(args.name)
        if skill is None:
            _audit("activate", args.name, status="unknown_skill", ctx=_ctx)
            return json.dumps({"error": "unknown_skill", "name": args.name})
        active = await activation_store.activate(tenant_id, manifest_id, skill.name)
        _audit("activate", skill.name, status="ok", ctx=_ctx)
        result: dict[str, Any] = {
            "activated": skill.name,
            "active_skills": active,
            "instructions": skill.body or "(no body)",
        }
        try:
            files = await bundle_files(skill, settings=settings, tenant_id=tenant_id)
        except Exception:
            logger.warning("bundle listing failed for skill %s", skill.name, exc_info=True)
            files = []
        if files:
            # Named, not inlined: the body says when to read one, and read_skill_file does.
            result["files"] = files
        return json.dumps(result)

    async def _read_file(args: _ReadFileArgs, _ctx: ToolInvocationCtx | None = None) -> str:
        skill = catalog.get(args.name)
        if skill is None:
            return json.dumps({"error": "unknown_skill", "name": args.name})
        problem = bundle_path_issue(args.path)
        if problem is not None:
            return json.dumps({"error": "invalid_path", "path": args.path, "detail": problem})
        if is_binary_asset_path(args.path):
            return json.dumps({"error": "binary_asset", "path": args.path})
        try:
            data = await read_bundle_file(skill, args.path, object_store=object_store, tenant_id=tenant_id)
        except Exception:
            logger.warning("bundle read failed for %s/%s", skill.name, args.path, exc_info=True)
            data = None
        if data is None:
            return json.dumps({"error": "file_not_found", "name": skill.name, "path": args.path})
        text = data.decode("utf-8", errors="replace")
        return json.dumps(
            {
                "name": skill.name,
                "path": args.path,
                "content": text[:MAX_READ_CHARS],
                "truncated": len(text) > MAX_READ_CHARS,
            }
        )

    async def _deactivate(args: _SkillNameArgs, _ctx: ToolInvocationCtx | None = None) -> str:
        # Resolved first, like `activate`. `activation_store.deactivate` is a list filter --
        # it neither validates the name nor reports whether anything was removed -- so
        # auditing the raw argument recorded a model-supplied string under `status="ok"`,
        # indistinguishable from a deactivation that happened.
        skill = catalog.get(args.name)
        active = await activation_store.deactivate(tenant_id, manifest_id, args.name)
        if skill is None:
            _audit("deactivate", args.name, status="unknown_skill", ctx=_ctx)
        else:
            _audit("deactivate", skill.name, status="ok", ctx=_ctx)
        return json.dumps({"deactivated": args.name, "active_skills": active})

    return [
        define_tool(
            name="list_skills",
            description="List available skills for this agent (name, description, active).",
            handler=_list,
        ),
        define_tool(
            name="activate_skill",
            description=(
                "Activate a named skill and return its full instructions. "
                "Call when a task matches a skill description."
            ),
            args=_SkillNameArgs,
            handler=_activate,
        ),
        define_tool(
            name="deactivate_skill",
            description="Deactivate a named skill for this agent.",
            args=_SkillNameArgs,
            handler=_deactivate,
        ),
        define_tool(
            name="read_skill_file",
            description=(
                "Read one file from a skill's bundle (references/, scripts/, assets/, evals/), "
                "as activate_skill listed it. Text only."
            ),
            args=_ReadFileArgs,
            handler=_read_file,
            replay_safe=True,
        ),
    ]


SKILL_TOOL_NAMES = frozenset({"list_skills", "activate_skill", "deactivate_skill", "read_skill_file"})
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
    publish gate passes; if it does not, the draft stays and the result says why. Every
    refusal comes back as `{"error": ...}`, never as a raise into the loop.
    """
    from felix.skills import library
    from felix.skills.format import parse_skill_md, serialize_skill_md
    from felix.skills.library_store import get_skill_library_store

    lib = get_skill_library_store(settings)

    async def _base_version(name: str) -> str | None:
        skill = await lib.get_skill(tenant_id, name)
        if skill is None:
            return None
        if skill.get("live_version"):
            return str(skill["live_version"])
        newest = await lib.list_versions(tenant_id, name, limit=1)
        return str(newest[0]["version"]) if newest else None

    async def _compose(
        args: ToolInput, *, update: bool
    ) -> tuple[dict[str, str], str | None] | dict[str, Any]:
        """The bundle a call would save, and its parent version — or an error result."""
        name = str(args.get("name") or "")
        if not update:
            if await lib.get_skill(tenant_id, name) is not None:
                return {"error": "skill_exists", "name": name, "detail": "use update_skill to change it"}
            frontmatter = {"name": name, "description": str(args.get("description") or "")}
            return {"SKILL.md": serialize_skill_md(frontmatter, f"\n{args.get('body') or ''}")}, None
        parent = await _base_version(name)
        if parent is None:
            return {"error": "unknown_skill", "name": name, "detail": "not in the skill library"}
        files = await library.read_version_files(settings, tenant_id, name, parent, object_store=object_store)
        parsed = parse_skill_md(files.get("SKILL.md", ""))
        frontmatter = dict(parsed.frontmatter) if parsed and isinstance(parsed.frontmatter, dict) else {}
        frontmatter["name"] = name
        if args.get("description"):
            frontmatter["description"] = str(args["description"])
        files["SKILL.md"] = serialize_skill_md(frontmatter, f"\n{args.get('body') or ''}")
        return files, parent

    async def _save(args: ToolInput, ctx: ToolInvocationCtx | None, *, update: bool) -> str:
        try:
            composed = await _compose(args, update=update)
            if isinstance(composed, dict):
                return json.dumps(composed)
            files, parent = composed
            row = await library.save_draft(
                settings,
                tenant_id,
                files=files,
                name=str(args["name"]),
                source="agent",
                author=manifest_id,
                reason=str(args.get("reason") or ""),
                origin_manifest_id=manifest_id,
                session_id=getattr(ctx, "thread_id", None),
                parent=parent,
                max_pending=max_pending,
                object_store=object_store,
            )
        except library.SkillBundleInvalid as exc:
            issues = [{"path": i.path, "message": i.message} for i in exc.issues[:20]]
            return json.dumps({"error": exc.code, "issues": issues})
        except library.SkillLibraryError as exc:
            return json.dumps({"error": exc.code, "detail": str(exc)})
        except Exception:
            logger.warning("skill save failed for %s", args.get("name"), exc_info=True)
            return json.dumps({"error": "save_failed", "name": args.get("name")})
        if mode != "publish":
            return json.dumps(_draft_result(row, "draft"))
        try:
            published = await library.publish(
                settings, tenant_id, row["name"], row["version"], by=manifest_id, object_store=object_store
            )
        except library.SkillPublishBlocked as exc:
            return json.dumps({**_draft_result(row, "draft"), "publish_blocked": exc.reasons})
        except library.SkillLibraryError as exc:
            return json.dumps({**_draft_result(row, "draft"), "publish_blocked": [str(exc)]})
        return json.dumps(_draft_result(published, "published"))

    async def _preview(args: ToolInput, *, update: bool) -> str:
        composed = await _compose(args, update=update)
        if isinstance(composed, dict):
            return json.dumps(composed)
        return composed[0]["SKILL.md"]

    async def _create(args: _CreateSkillArgs, ctx: ToolInvocationCtx | None = None) -> str:
        return await _save(args.model_dump(), ctx, update=False)

    async def _update(args: _UpdateSkillArgs, ctx: ToolInvocationCtx | None = None) -> str:
        return await _save(args.model_dump(exclude_none=True), ctx, update=True)

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
    create.approval_preview = lambda a: _preview(a, update=False)
    update.approval_preview = lambda a: _preview(a, update=True)
    return [create, update]


__all__ = [
    "SKILL_AUTHORING_TOOL_NAMES",
    "SKILL_TOOL_NAMES",
    "bundle_files",
    "make_skill_authoring_tools",
    "make_skill_tools",
    "read_bundle_file",
]
