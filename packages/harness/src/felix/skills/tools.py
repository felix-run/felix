"""Skill tools — list / activate / deactivate / read a bundle file with progressive
disclosure, plus `create_skill` / `update_skill` for a manifest with `spec.skill_authoring`."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from felix.audit.emit import emit_agent_audit
from felix.logging_setup import loggable
from felix.skills.binary import is_binary_asset_path
from felix.skills.format import ALLOWED_ROOT_FILES, BUNDLE_DIRS, MAX_BUNDLE_FILES, bundle_path_issue
from felix.skills.store import SkillActivationStore
from felix.skills.types import Skill, SkillCatalog
from felix.tools.types import Tool, ToolInvocationCtx, ToolOutput, define_tool, untrusted_output

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
    skill: Skill, path: str, *, settings: Any | None, object_store: Any | None, tenant_id: str
) -> bytes | None:
    """One bundle file's bytes, or None. ``path`` must already have passed the allowlist.

    A library skill reads only a path its live version saved, checked against the digest
    recorded then (`library.read_version_file`). A store skill reads under its own key's
    directory; the allowlist's first segment is a bundle directory, so no path climbs out of
    it, and library bytes are under a prefix of their own that no `skills/` key reaches.
    """
    if skill.source == "library":
        if settings is None or not skill.version:
            return None
        from felix.skills.library import read_version_file

        text = await read_version_file(
            settings, tenant_id, skill.name, skill.version, path, object_store=object_store
        )
        return None if text is None else text.encode("utf-8")
    if skill.source == "store":
        if object_store is None or not skill.path or not skill.path.endswith("/SKILL.md"):
            return None
        return await object_store.get(f"{skill.path.removesuffix('SKILL.md')}{path}")
    root = _bundle_root(skill)
    return None if root is None else await asyncio.to_thread(_read_host_file, root, path)


def _relayed(skill: Skill, text: str) -> ToolOutput:
    """What a skill tool returns of ``skill``'s text: marked untrusted when it is imported or built
    on an import, so content screening reads it as it reads an untrusted tool's output."""
    return untrusted_output(text) if skill.untrusted else text


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

    async def _newest(names: list[str]) -> dict[str, str]:
        """Each library skill's newest version that was not rejected, which `update_skill` must
        name as its parent.

        Newer than the live version when a draft is waiting; never a rejected draft, whose files
        an edit must not inherit. One query for every name; empty when there is no library to
        ask, and a failed read only drops the field.
        """
        if settings is None or not names:
            return {}
        from felix.skills.library import newest_buildable_versions

        try:
            return await newest_buildable_versions(settings, tenant_id, names)
        except Exception:
            logger.warning("skill library versions read failed", exc_info=True)
            return {}

    async def _list(_args: dict[str, Any] | None = None, _ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        active = await activation_store.get_active(tenant_id, manifest_id)
        public = catalog.list_public()
        newest = await _newest([s.name for s in public if s.source == "library"])
        payload = [
            {
                "name": s.name,
                # The catalog's rule: an imported description carrying injection markers is
                # withheld here too, not only in the system prompt.
                "description": s.listed_description(),
                "active": s.name in active,
                "has_body": bool(s.body),
                "source": s.source,
                **({"untrusted": True} if s.untrusted else {}),
                **({"newest_version": newest[s.name]} if s.name in newest else {}),
            }
            for s in public
        ]
        # Also surface disable_model_invocation skills as inactive-only via list? skip per spec.
        # An imported skill's description is a third party's text: the listing is screened as
        # relayed output when it carries one.
        text = json.dumps(payload)
        return untrusted_output(text) if any(s.untrusted for s in public) else text

    async def _activate(args: _SkillNameArgs, _ctx: ToolInvocationCtx | None = None) -> ToolOutput:
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
        if skill.source == "library":
            result["version"] = skill.version
            newest = (await _newest([skill.name])).get(skill.name)
            if newest:
                result["newest_version"] = newest
        return _relayed(skill, json.dumps(result))

    async def _read_file(args: _ReadFileArgs, _ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        skill = catalog.get(args.name)
        if skill is None:
            return json.dumps({"error": "unknown_skill", "name": args.name})
        problem = bundle_path_issue(args.path)
        if problem is not None:
            return json.dumps({"error": "invalid_path", "path": args.path, "detail": problem})
        if is_binary_asset_path(args.path):
            return json.dumps({"error": "binary_asset", "path": args.path})
        try:
            data = await read_bundle_file(
                skill, args.path, settings=settings, object_store=object_store, tenant_id=tenant_id
            )
        except Exception:
            logger.warning("bundle read failed for %s/%s", skill.name, args.path, exc_info=True)
            data = None
        if data is None:
            return json.dumps({"error": "file_not_found", "name": skill.name, "path": args.path})
        text = data.decode("utf-8", errors="replace")
        return _relayed(
            skill,
            json.dumps(
                {
                    "name": skill.name,
                    "path": args.path,
                    "content": text[:MAX_READ_CHARS],
                    "truncated": len(text) > MAX_READ_CHARS,
                }
            ),
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
            relays_untrusted=True,
        ),
        define_tool(
            name="activate_skill",
            description=(
                "Activate a named skill and return its full instructions. "
                "Call when a task matches a skill description."
            ),
            args=_SkillNameArgs,
            handler=_activate,
            relays_untrusted=True,
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
            relays_untrusted=True,
        ),
    ]


SKILL_TOOL_NAMES = frozenset({"list_skills", "activate_skill", "deactivate_skill", "read_skill_file"})


__all__ = [
    "SKILL_TOOL_NAMES",
    "bundle_files",
    "make_skill_tools",
    "read_bundle_file",
]
