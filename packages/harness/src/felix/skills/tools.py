"""Skill tools — list / activate / deactivate / read a bundle file with progressive
disclosure, plus `create_skill` / `update_skill` for a manifest with `spec.skill_authoring`."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, create_model

from felix.audit.emit import emit_agent_audit
from felix.logging_setup import loggable
from felix.skills.activation import (
    ACTIVATE_TOOL,
    ACTIVATED_KEY,
    ARGUMENTS_DIGEST_KEY,
    ARGUMENTS_KEY,
    DEACTIVATE_TOOL,
    DEACTIVATED_KEY,
    DIGEST_KEY,
    INSTRUCTIONS_KEY,
    INSTRUCTIONS_SOURCE,
    LIST_TOOL,
    READ_FILE_TOOL,
    SKILL_TOOL_NAMES,
    VERSION_KEY,
    activation_instructions,
    activations_for,
    instructions_digest,
    retained_view,
)
from felix.skills.binary import is_binary_asset_path
from felix.skills.format import (
    ALLOWED_ROOT_FILES,
    BUNDLE_DIRS,
    MAX_BUNDLE_FILES,
    MAX_SKILL_MD_CHARS,
    bundle_path_issue,
    xml_escape,
)
from felix.skills.types import Skill, SkillCatalog
from felix.tools.errors import read_tool_error_code
from felix.tools.types import (
    Tool,
    ToolInvocationCtx,
    ToolOutput,
    define_tool,
    is_wrapper_deny,
    tool_output_content,
    untrusted_output,
)

if TYPE_CHECKING:
    from felix.tools.delegation import ForkRunner

logger = logging.getLogger("felix.skills.tools")

# What one `read_skill_file` returns at most. A reference file is read to be used, not to
# fill the context window; the result says when it was cut.
MAX_READ_CHARS = 64 * 1024
# What one `activate_skill` returns of a body at most: the cap a saved SKILL.md already has,
# applied to a host or operator-uploaded one too, which nothing measured before.
MAX_ACTIVATION_CHARS = MAX_SKILL_MD_CHARS
# Frontmatter the model is told about on activation, as written. `allowed-tools` is informational
# only: Felix governance decides what a tool call may do, never a SKILL.md, whose author may be a
# third party (agentskills.io marks the field experimental and leaves enforcement to clients).
_ACTIVATION_FIELDS = (
    ("license", "license"),
    ("compatibility", "compatibility"),
    ("allowed-tools", "allowed_tools"),
)


# The handlers' argument shapes. Each tool's own model is built from one of these by `with_skill_names`,
# which also writes the `name` field's description.
class _SkillNameArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)


class _ActivateArgs(_SkillNameArgs):
    arguments: str | None = Field(
        default=None,
        max_length=16_000,
        description="Leave unset: set by the harness when a person runs the skill as /name.",
    )
    task: str | None = Field(
        default=None,
        max_length=16_000,
        description='For a skill marked context="fork": the job, with every fact the subagent needs.',
    )


class _ReadFileArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    path: str = Field(
        min_length=1,
        max_length=512,
        description="A file activate_skill listed, e.g. references/guide.md.",
    )


# Past this many skills the `name` schema carries no `enum`: three tool schemas repeating a long
# list on every request would cost more than the catalog block that already names them.
MAX_ENUM_NAMES = 64


def with_skill_names(base: type[BaseModel], names: list[str], description: str) -> type[BaseModel]:
    """``base`` with the names the model may use as an `enum` on `name`, as agentskills.io's
    client guide suggests. Schema only: validation stays a plain string, so a name outside the
    catalog still reaches the handler and is answered (and audited) as `unknown_skill`."""
    extra = {"enum": names} if 0 < len(names) <= MAX_ENUM_NAMES else None
    field = Field(min_length=1, description=description, json_schema_extra=extra)
    return create_model(base.__name__, __base__=base, name=(str, field))


def _list_dir_bundle(root: Path) -> tuple[list[str], bool]:
    """At most `MAX_BUNDLE_FILES` paths, and whether there were more.

    Only what `_read_host_file` would serve: a symlink is listed when it resolves inside the
    skill, and left out when it leads elsewhere -- listing a file the reader refuses is a
    promise the next call breaks.
    """
    base = root.resolve()
    files: list[str] = []
    for top in (*BUNDLE_DIRS, *ALLOWED_ROOT_FILES):
        entry = root / top
        candidates = [entry] if entry.is_file() else sorted(p for p in entry.rglob("*") if p.is_file())
        for p in candidates:
            rel = p.relative_to(root).as_posix()
            if bundle_path_issue(rel) is None and p.resolve().is_relative_to(base):
                files.append(rel)
            if len(files) > MAX_BUNDLE_FILES:
                return files[:MAX_BUNDLE_FILES], True
    return files, False


def _library_of(skill: Skill) -> str:
    """The library a library skill was loaded from: its own, never one looked up by name."""
    from felix.skills.library_keys import ORG_OWNER

    return skill.library_owner if skill.library_owner is not None else ORG_OWNER


def _label(skill: Skill) -> str:
    """The audit label of a library skill's library; empty for a host or store skill."""
    from felix.skills.library_keys import library_label

    return library_label(_library_of(skill)) if skill.source == "library" else ""


def _bundle_root(skill: Skill) -> Path | None:
    """A host skill's directory: the folder its `SKILL.md` is in."""
    if skill.source != "bundled" or not skill.path or Path(skill.path).name != "SKILL.md":
        return None
    return Path(skill.path).parent


async def bundle_files(skill: Skill, *, settings: Any | None, tenant_id: str) -> tuple[list[str], bool]:
    """The paths `read_skill_file` can serve for ``skill``, SKILL.md excluded -- at most
    `MAX_BUNDLE_FILES` of them -- and whether the listing was cut there.

    A library version lists its saved files; a host skill its directory. A raw object-store
    skill lists nothing: the store has no listing, though a path the body names still reads.
    """
    if skill.source == "library" and settings is not None and skill.version:
        from felix.skills.library_store import get_skill_library_store

        lib = get_skill_library_store(settings, owner=_library_of(skill))
        rows = await lib.list_files(tenant_id, skill.name, skill.version)
        paths = [str(r["path"]) for r in rows if r["path"] != "SKILL.md"]
        return paths[:MAX_BUNDLE_FILES], len(paths) > MAX_BUNDLE_FILES
    root = _bundle_root(skill)
    if root is None:
        return [], False
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
            settings,
            tenant_id,
            skill.name,
            skill.version,
            path,
            owner=_library_of(skill),
            object_store=object_store,
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
    session_store: Any | None,
    tenant_id: str,
    person_calls: Any | None = None,
    fork_runner: ForkRunner | None = None,
    manifest_id: str,
    settings: Any | None = None,
    object_store: Any | None = None,
) -> list[Tool]:
    """Build list_skills / activate_skill / deactivate_skill / read_skill_file over a catalog.

    Which skills are active is the calling thread's, read from its log through ``session_store``
    (`skills/activation.py`); without one, or without a thread, none is.

    ``settings`` and ``object_store`` are what reach a library or object-store skill's bundle
    files; without them `read_skill_file` serves host skills only.
    """

    def _audit(
        action: str,
        skill: str,
        *,
        status: str,
        ctx: ToolInvocationCtx | None,
        library: str = "",
        by: str = "",
    ) -> None:
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
                # `person` (their `/name`), `preload` (the manifest's) or `model`, for an activation.
                **({"by": by} if by else {}),
                "skill": loggable(skill, limit=64),
                "thread_id": getattr(ctx, "thread_id", None) or "",
                "tool_call_id": getattr(ctx, "tool_call_id", None) or "",
                # A library skill's library (`library_keys.library_label`): a personal skill and the
                # tenant's may share a name, and the row has to say whose instructions ran.
                **({"library": library} if library else {}),
            },
        )

    async def _newest(skills: list[Skill]) -> dict[str, str]:
        """Each library skill's newest version that was not rejected, which `update_skill` must
        name as its parent -- read in the skill's own library, since a personal skill and the
        tenant's may share a name.

        Newer than the live version when a draft is waiting; never a rejected draft, whose files
        an edit must not inherit. One query per library; empty when there is no library to ask,
        and a failed read only drops the field.
        """
        if settings is None or not skills:
            return {}
        from felix.skills.library import newest_buildable_versions

        by_library: dict[str, list[str]] = {}
        for s in skills:
            by_library.setdefault(_library_of(s), []).append(s.name)
        newest: dict[str, str] = {}
        try:
            for owner, names in by_library.items():
                newest.update(await newest_buildable_versions(settings, tenant_id, names, owner=owner))
        except Exception:
            logger.warning("skill library versions read failed", exc_info=True)
            return {}
        return newest

    async def _active(ctx: ToolInvocationCtx | None) -> dict[str, Any]:
        return await activations_for(session_store, getattr(ctx, "thread_id", None))

    async def _list(_args: dict[str, Any] | None = None, _ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        active = await _active(_ctx)
        public = catalog.list_public()
        newest = await _newest([s for s in public if s.source == "library"])
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
        # An imported skill's description is a third party's text: the listing is screened as
        # relayed output when it carries one.
        text = json.dumps(payload)
        return untrusted_output(text) if any(s.untrusted for s in public) else text

    async def _activate(args: _ActivateArgs, _ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        # Who asked, by the call's id (`invocation.PersonCalls`): a person's `/name` may reach a skill
        # hidden from the model and pass arguments, the manifest's preload may reach one; the model
        # may do neither. Its `arguments` would put model-chosen text inside the instructions, which
        # retention then re-shows as the skill's for the rest of the conversation.
        by = (person_calls.claim(getattr(_ctx, "tool_call_id", None)) if person_calls else None) or "model"
        if by == "person":
            skill = catalog.get_for_user(args.name)
        else:
            skill = (
                catalog.get_activatable(args.name) if by == "preload" else catalog.get_invocable(args.name)
            )
        if skill is None:
            _audit("activate", args.name, status="unknown_skill", ctx=_ctx, by=by)
            return json.dumps({"error": "unknown_skill", "name": args.name})
        arguments = args.arguments if by == "person" else None
        if skill.fork:
            return await _fork(skill, arguments, args.task, by, _ctx)
        active = await _active(_ctx)
        instructions = activation_instructions(skill, arguments)
        digest = instructions_digest(activation_instructions(skill))
        arguments_digest = instructions_digest(arguments) if arguments else None
        # Only what retention can still show whole (`activation.retained_view`): an activation past
        # its budget, of a body that has changed since, or with other arguments, is sent again.
        shown = retained_view(active, catalog).whole.get(skill.name)
        if shown is not None and shown.digest == digest and shown.arguments_digest == arguments_digest:
            # The same instructions are already in this conversation, and kept in view
            # (`activation.SkillRetentionStrategy`): sending them again only spends context.
            _audit("activate", skill.name, status="already_active", ctx=_ctx, library=_label(skill), by=by)
            return json.dumps(
                {
                    "already_active": skill.name,
                    "note": "its instructions are earlier in this conversation; follow them",
                }
            )
        _audit("activate", skill.name, status="ok", ctx=_ctx, library=_label(skill), by=by)
        result: dict[str, Any] = {
            ACTIVATED_KEY: skill.name,
            "active_skills": sorted(
                {skill.name, *(n for n in active if catalog.get_activatable(n) is not None)}
            ),
            INSTRUCTIONS_KEY: instructions,
            DIGEST_KEY: digest,
            **({ARGUMENTS_KEY: arguments, ARGUMENTS_DIGEST_KEY: arguments_digest} if arguments else {}),
            # Said rather than silently dropped: arguments are a person's to pass, through `/name`.
            **({"arguments_ignored": True} if args.arguments and not arguments else {}),
        }
        if len(skill.body) > MAX_ACTIVATION_CHARS:
            result["truncated"] = True
        result.update(
            (key, skill.metadata[field]) for field, key in _ACTIVATION_FIELDS if skill.metadata.get(field)
        )
        try:
            files, cut = await bundle_files(skill, settings=settings, tenant_id=tenant_id)
        except Exception:
            logger.warning("bundle listing failed for skill %s", skill.name, exc_info=True)
            files, cut = [], False
        if files:
            # Named, not inlined: the body says when to read one, and read_skill_file does.
            result["files"] = files
            if cut:
                result["files_truncated"] = True
        if skill.source == "library":
            result[VERSION_KEY] = skill.version
            newest = (await _newest([skill])).get(skill.name)
            if newest:
                result["newest_version"] = newest
        return _relayed(skill, json.dumps(result))

    async def _fork(
        skill: Skill, arguments: str | None, task: str | None, by: str, ctx: ToolInvocationCtx | None
    ) -> ToolOutput:
        """Run a `context: fork` skill in a delegation child and answer what it said. Never an
        activation: its instructions stay out of this conversation, so nothing is retained."""
        if skill.untrusted:
            # The instructions become the child's own turn: a third party's text must not be.
            _audit("fork", skill.name, status="fork_refused", ctx=ctx, library=_label(skill), by=by)
            return json.dumps(
                {
                    "error": "fork_refused",
                    "name": skill.name,
                    "detail": "an imported skill nobody has adopted does not run in a subagent",
                }
            )
        agents = fork_runner.agents if fork_runner is not None else ()
        agent = skill.fork_agent or (agents[0] if agents else None)
        if fork_runner is None or agent is None or agent not in agents:
            _audit("fork", skill.name, status="fork_unavailable", ctx=ctx, library=_label(skill), by=by)
            return json.dumps(
                {
                    "error": "fork_unavailable",
                    "name": skill.name,
                    "detail": f"this skill runs in a subagent ({agent or 'none named'}), and this "
                    "agent cannot delegate to it",
                }
            )
        # A person's arguments go where the body asks, as for any activation; the model says what the
        # job is instead.
        instructions = activation_instructions(skill, arguments)
        prompt = (
            f'<skill_instructions name="{xml_escape(skill.name)}">\n{instructions}\n</skill_instructions>'
        )
        if task and not arguments:
            prompt = f"{prompt}\n\n{task}"
        try:
            out = await fork_runner(
                agent, prompt, skill.fork_model, outer_call_id=getattr(ctx, "tool_call_id", None)
            )
        except Exception:
            # A wrapper on `task` raised -- screening, an approver's edit the validator refused: audited
            # as the failure it is, and answered with a fact rather than the exception's text.
            logger.warning("forked skill %s failed", skill.name, exc_info=True)
            _audit("fork", skill.name, status="fork_failed", ctx=ctx, library=_label(skill), by=by)
            return json.dumps({"error": "fork_failed", "name": skill.name, "agent": agent})
        if is_wrapper_deny(out):
            # A control on `task` refused it -- an approval, a policy, a limit: passed on as the deny it
            # is, so the run counts this call as refused like any other.
            _audit("fork", skill.name, status="fork_denied", ctx=ctx, library=_label(skill), by=by)
            return out
        answer = tool_output_content(out)
        if read_tool_error_code(out) is not None:
            # Refused or failed -- admission, a limit, the agent raising: said as an error, never
            # passed off as the agent's answer.
            _audit("fork", skill.name, status="fork_failed", ctx=ctx, library=_label(skill), by=by)
            return json.dumps({"error": "fork_failed", "name": skill.name, "agent": agent, "detail": answer})
        _audit("fork", skill.name, status="ok", ctx=ctx, library=_label(skill), by=by)
        # The child's answer can carry anything its tools read: screened as a `task` answer is.
        return untrusted_output(json.dumps({"ran": skill.name, "agent": agent, "result": answer}))

    async def _read_file(args: _ReadFileArgs, _ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        skill = catalog.get_invocable(args.name)
        if skill is None and catalog.get_activatable(args.name) is not None:
            # A skill hidden from the model that a person ran here: its own files are part of it.
            skill = catalog.get_activatable(args.name) if args.name in await _active(_ctx) else None
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
        # Resolved first, like `activate`, so the trail never records a model-supplied string
        # under `status="ok"`. The result is the record: the thread's log is what says the
        # skill is no longer active (`activation.thread_activations`).
        skill = catalog.get_activatable(args.name)
        active = await _active(_ctx)
        active.pop(args.name, None)
        if skill is None:
            _audit("deactivate", args.name, status="unknown_skill", ctx=_ctx)
        else:
            _audit("deactivate", skill.name, status="ok", ctx=_ctx, library=_label(skill))
        # As `activate`: this caller's names.
        mine = [n for n in active if catalog.get_activatable(n) is not None]
        return json.dumps({DEACTIVATED_KEY: args.name, "active_skills": mine})

    names = [s.name for s in catalog.list_public()]
    return [
        define_tool(
            name=LIST_TOOL,
            description="List available skills (name, description, and whether active in this conversation).",
            handler=_list,
            read_only=True,
            relays_untrusted=True,
        ),
        define_tool(
            name=ACTIVATE_TOOL,
            description=(
                "Activate a named skill and return its full instructions. "
                "Call when a task matches a skill description."
            ),
            args=with_skill_names(_ActivateArgs, names, "Skill name to activate."),
            handler=_activate,
            # Never spilled to an artifact: the body is the instructions, and a preview of them
            # is not something the model can follow (`artifacts.UNSPILLED_SOURCES`).
            source=INSTRUCTIONS_SOURCE,
            # Nothing outside the conversation changes: an activation is its result in the log
            # (`activation.thread_activations`), so plan mode lets a person's `/name` through.
            read_only=True,
            relays_untrusted=True,
        ),
        define_tool(
            name=DEACTIVATE_TOOL,
            description="Stop following a skill activated earlier in this conversation.",
            args=with_skill_names(_SkillNameArgs, names, "Skill name to deactivate."),
            handler=_deactivate,
            read_only=True,
        ),
        define_tool(
            name=READ_FILE_TOOL,
            description=(
                "Read one file from a skill's bundle (references/, scripts/, assets/, evals/), "
                "as activate_skill listed it. Text only."
            ),
            args=with_skill_names(_ReadFileArgs, names, "Skill name, as list_skills shows it."),
            handler=_read_file,
            replay_safe=True,
            read_only=True,
            relays_untrusted=True,
        ),
    ]


__all__ = [
    "SKILL_TOOL_NAMES",
    "bundle_files",
    "make_skill_tools",
    "read_bundle_file",
    "with_skill_names",
]
