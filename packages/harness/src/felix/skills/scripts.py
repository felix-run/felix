"""`run_skill_script` — run a script from an active skill's `scripts/` (`spec.skill_scripts`).

agentskills.io lets a skill bundle scripts its instructions tell the agent to run. Here one runs
where a shell tool's command would (`tools.shell.shell_place`): the shell runner, or a development
box, never the API process of a deployment, because a script is code. What the tool checks before
it runs, in order:

1. the skill is one this caller may use -- offered to the model, or hidden and active here;
2. it is not an imported skill nobody has adopted (`Skill.untrusted`): a third party's code;
   and it comes from a source the operator lets run code (`FELIX_SKILL_SCRIPT_SOURCES`, the host's
   own skills by default): writing a library or a personal skill is a grant to write
   instructions, and a script runs on a runner every tenant's workspace shares;
3. it is active in *this* conversation (`activation.thread_activations`), so a script runs only
   beside the instructions that say when to run it;
4. the path is a bundle path under `scripts/`, not a binary asset, that the bundle lists;
5. its extension maps, in the manifest, to an interpreter the operator also allows
   (`FELIX_SKILL_SCRIPT_INTERPRETERS`), read per call from the request's settings.

The bundle is then written for this one call to `.felix-skills/<skill>/<token>/` under the scope's
workspace root -- by descriptor, following no symlink, the parent directories refused if anything
replaced them with one -- and removed when the script ends. The file tools refuse that directory
(`workspace_backend.PANE_HIDDEN_DIRS`), so the model cannot write a script there; a shell tool can,
and a race with one is the boundary shell tools already have. The interpreter gets the script as
`argv[1]` and the model's arguments after it, so none of them can be an interpreter option; the
script runs from the workspace root, where the files it is asked about are. An interpreter that
looks for modules in the script's parent directories reaches that root too: node's `node_modules`
walk does, which is why `.js` is not in the default map.

The tool's transport is `shell`: command screening reads every argument, and content screening
reads the output as untrusted.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import secrets
import shutil
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from felix.audit.emit import emit_agent_audit
from felix.logging_setup import loggable
from felix.security.shell_policy import (
    SKILL_SCRIPT_TOKEN_BYTES,
    SKILL_SCRIPTS_DIR,
    allowed_interpreters,
    script_sources,
)
from felix.skills.activation import activations_for
from felix.skills.binary import is_binary_asset_path
from felix.skills.format import MAX_BUNDLE_BYTES, SKILL_NAME_RE, bundle_path_issue
from felix.skills.tools import bundle_files, read_bundle_file, with_skill_names
from felix.timeouts import timeout_seconds
from felix.tools.shell import (
    DEFAULT_SHELL_TIMEOUT_S,
    HostedPlace,
    LocalPlace,
    RunnerPlace,
    exec_in_place,
    request_settings,
    shell_place,
    shell_refusal,
)
from felix.tools.types import Tool, ToolInvocationCtx, ToolOutput, define_tool

if TYPE_CHECKING:
    from pathlib import Path

    from felix.manifests.schema import SkillScriptsSpec
    from felix.skills.types import Skill, SkillCatalog

logger = logging.getLogger("felix.skills.scripts")

RUN_SCRIPT_TOOL = "run_skill_script"
_MAX_ARGS = 64
_MAX_ARGS_BYTES = 64_000
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
# The first thing `.felix-skills/` holds, so a thread's repository checkout never shows it to git.
_GITIGNORE = b"*\n"


class _RunScriptArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    script: str = Field(
        description="The script's path in the skill, under scripts/, as activate_skill listed it."
    )
    args: list[str] = Field(
        default_factory=list, max_length=_MAX_ARGS, description="Arguments passed to the script, as written."
    )

    @field_validator("args")
    @classmethod
    def _bounded(cls, v: list[str]) -> list[str]:
        if sum(len(a.encode("utf-8")) for a in v) > _MAX_ARGS_BYTES:
            raise ValueError(f"args exceed {_MAX_ARGS_BYTES} bytes")
        if any("\0" in a for a in v):
            raise ValueError("an argument may not contain NUL")
        return v


def _refused(error: str, detail: str, **extra: Any) -> str:
    return json.dumps({"error": error, **extra, "detail": detail})


def _open_dir(parent: int, name: str) -> int:
    """`name` in `parent` as a directory, made if missing, never through a symlink."""
    with contextlib.suppress(FileExistsError):
        os.mkdir(name, 0o700, dir_fd=parent)
    return os.open(name, _DIR_FLAGS, dir_fd=parent)


def _write_bundle(top: int, files: dict[str, bytes]) -> None:
    for path, data in files.items():
        *dirs, leaf = path.split("/")
        with contextlib.ExitStack() as stack:
            here = top
            for part in dirs:
                here = _open_dir(here, part)
                stack.callback(os.close, here)
            fd = os.open(leaf, _FILE_FLAGS, 0o600, dir_fd=here)
            with os.fdopen(fd, "wb") as out:
                out.write(data)


def _opened(stack: contextlib.ExitStack, parent: int, name: str, *, make: bool) -> int:
    fd = _open_dir(parent, name) if make else os.open(name, _DIR_FLAGS, dir_fd=parent)
    stack.callback(os.close, fd)
    return fd


def materialize(root: Path, skill: str, files: dict[str, bytes]) -> str:
    """Write ``files`` to a fresh `.felix-skills/<skill>/<token>/` under ``root``; its path from ``root``.

    By descriptor from ``root``: `.felix-skills` or the skill's directory replaced by a symlink is
    refused (`O_NOFOLLOW`), not followed out of the workspace. A write that fails part way removes
    what it wrote.
    """
    token = secrets.token_hex(SKILL_SCRIPT_TOKEN_BYTES)
    with contextlib.ExitStack() as stack:
        top = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        stack.callback(os.close, top)
        base = _opened(stack, top, SKILL_SCRIPTS_DIR, make=True)
        with contextlib.suppress(FileExistsError):
            fd = os.open(".gitignore", _FILE_FLAGS, 0o600, dir_fd=base)
            with os.fdopen(fd, "wb") as out:
                out.write(_GITIGNORE)
        skill_dir = _opened(stack, base, skill, make=True)
        os.mkdir(token, 0o700, dir_fd=skill_dir)
        try:
            _write_bundle(_opened(stack, skill_dir, token, make=False), files)
        except BaseException:
            shutil.rmtree(token, dir_fd=skill_dir, ignore_errors=True)
            raise
    return f"{SKILL_SCRIPTS_DIR}/{skill}/{token}"


def remove_materialized(root: Path, rel: str) -> None:
    """Remove what `materialize` wrote, by descriptor, following nothing."""
    base, skill, token = rel.split("/")
    try:
        with contextlib.ExitStack() as stack:
            top = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            stack.callback(os.close, top)
            skill_dir = _opened(stack, _opened(stack, top, base, make=False), skill, make=False)
            shutil.rmtree(token, dir_fd=skill_dir, ignore_errors=True)
    except OSError:
        logger.warning("could not remove skill script directory %s", rel, exc_info=True)


def script_source(skill: Skill) -> str | None:
    """Which `FELIX_SKILL_SCRIPT_SOURCES` word ``skill`` comes under; None for a source that has none."""
    from felix.skills.library_keys import ORG_OWNER

    if skill.source == "bundled":
        return "host"
    if skill.source == "library":
        return "library" if skill.library_owner in (None, ORG_OWNER) else "personal"
    return None


class _ScriptRunner:
    """`run_skill_script`'s body: what it checks, in the module docstring's order, then the run."""

    def __init__(
        self,
        catalog: SkillCatalog,
        spec: SkillScriptsSpec,
        *,
        session_store: Any | None,
        tenant_id: str,
        manifest_id: str,
        settings: Any,
        object_store: Any | None,
    ) -> None:
        self.catalog = catalog
        self.spec = spec
        self.session_store = session_store
        self.tenant_id = tenant_id
        self.manifest_id = manifest_id
        self.settings = settings
        self.object_store = object_store
        self.timeout_s = timeout_seconds(spec.timeout_ms, default_s=DEFAULT_SHELL_TIMEOUT_S)
        self.refuse = functools.partial(shell_refusal, RUN_SCRIPT_TOOL)

    def _audit(
        self, skill: str, status: str, ctx: ToolInvocationCtx | None, script: str = "", *, ran: Any = None
    ) -> None:
        """Every decision about the skill -- who it is, whether it may run here -- and every run;
        not the malformed arguments the tool's schema and path checks answer."""
        emit_agent_audit(
            "skill_script",
            status=status,
            manifest_id=self.manifest_id,
            payload={
                # Resolved against the catalog before `ok` is ever written; see `skills/tools._audit`.
                "skill": loggable(skill, limit=64),
                **({"script": loggable(script, limit=200)} if script else {}),
                # What ran, not only its name: a version published since the approval or the
                # activation runs other code under the same name.
                **(ran or {}),
                "thread_id": getattr(ctx, "thread_id", None) or "",
                "tool_call_id": getattr(ctx, "tool_call_id", None) or "",
            },
        )

    async def _admitted(self, args: _RunScriptArgs, ctx: ToolInvocationCtx | None) -> Skill | str:
        """Checks 1-4 on the skill and the path: the skill, or the refusal."""
        active = await activations_for(self.session_store, getattr(ctx, "thread_id", None))
        # As `read_skill_file`: a skill hidden from the model is reachable once a person ran it here.
        skill = self.catalog.get_invocable(args.name) or (
            self.catalog.get_activatable(args.name) if args.name in active else None
        )
        if skill is None:
            self._audit(args.name, "unknown_skill", ctx)
            return _refused("unknown_skill", "no skill of that name is available here", name=args.name)
        if skill.untrusted:
            self._audit(skill.name, "script_refused", ctx, args.script)
            return _refused(
                "script_refused",
                "an imported skill nobody has adopted does not run its scripts",
                name=skill.name,
            )
        source = script_source(skill)
        if source not in script_sources(request_settings(self.settings)):
            # Whoever wrote this skill was granted writing instructions, not running code here.
            self._audit(skill.name, "script_refused", ctx, args.script)
            return _refused(
                "script_refused",
                f"this host does not run scripts from {source or 'this kind of'} skills "
                "(FELIX_SKILL_SCRIPT_SOURCES)",
                name=skill.name,
            )
        if skill.name not in active:
            self._audit(skill.name, "not_active", ctx, args.script)
            return _refused(
                "not_active",
                "activate the skill in this conversation first; its instructions say when to run its scripts",
                name=skill.name,
            )
        if not SKILL_NAME_RE.match(skill.name):
            # The name becomes a directory: only the spec's names, never one a loader kept as written.
            return _refused("script_refused", "this skill's name is not a valid skill name", name=skill.name)
        problem = bundle_path_issue(args.script)
        if problem is not None or not args.script.startswith("scripts/") or is_binary_asset_path(args.script):
            return _refused(
                "invalid_path", problem or "a script lives under scripts/ and is text", path=args.script
            )
        return skill

    def _interpreter(self, script: str, settings: Any) -> tuple[str | None, str]:
        """Check 5: the manifest's interpreter for the extension if the operator allows it, else
        None and why."""
        interpreter = self.spec.interpreters.get(PurePosixPath(script).suffix.lower())
        if interpreter is None:
            return None, f"no interpreter runs {script!r} here (skill_scripts.interpreters)"
        if interpreter not in allowed_interpreters(settings):
            return None, f"{interpreter!r} is not in FELIX_SKILL_SCRIPT_INTERPRETERS"
        return interpreter, ""

    async def _bundle(self, skill: Skill) -> dict[str, bytes] | None:
        """Every file of the skill's bundle that can be written, or None past `MAX_BUNDLE_BYTES`."""
        paths, _cut = await bundle_files(skill, settings=self.settings, tenant_id=self.tenant_id)
        files: dict[str, bytes] = {}
        total = 0
        for path in paths:
            if bundle_path_issue(path) is not None or (
                skill.source == "library" and is_binary_asset_path(path)
            ):
                # A library keeps a binary asset as text it cannot hand back as the file.
                continue
            data = await read_bundle_file(
                skill, path, settings=self.settings, object_store=self.object_store, tenant_id=self.tenant_id
            )
            if data is None:
                continue
            total += len(data)
            if total > MAX_BUNDLE_BYTES:
                return None
            files[path] = data
        return files

    async def __call__(self, args: _RunScriptArgs, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        skill = await self._admitted(args, ctx)
        if isinstance(skill, str):
            return skill
        settings = request_settings(self.settings)
        interpreter, why = self._interpreter(args.script, settings)
        if interpreter is None:
            return self.refuse("interpreter", why)
        place = shell_place(settings, self.refuse)
        if isinstance(place, HostedPlace):
            return self.refuse("hosted", "skill scripts do not run in the hosted workspace's sandbox yet")
        if not isinstance(place, RunnerPlace | LocalPlace):
            return place  # the tool error saying it runs nowhere
        try:
            files = await self._bundle(skill)
        except Exception:
            logger.warning("bundle read failed for skill %s", skill.name, exc_info=True)
            return _refused(
                "bundle_unavailable", "the skill's files could not be read; try again", name=skill.name
            )
        if files is None:
            return _refused(
                "bundle_too_large", f"the skill's files exceed {MAX_BUNDLE_BYTES} bytes", name=skill.name
            )
        if args.script not in files:
            # The object store has no listing, so a store skill has no bundle to write beside a script.
            detail = (
                "this skill's files cannot be listed, so its scripts cannot run"
                if skill.source == "store"
                else "the skill has no such script"
            )
            return _refused("file_not_found", detail, name=skill.name, path=args.script)
        try:
            rel = await asyncio.to_thread(materialize, place.root, skill.name, files)
        except OSError:
            logger.warning("writing skill %s's scripts failed", skill.name, exc_info=True)
            return _refused(
                "workspace_unavailable",
                "the skill's files could not be written to the workspace",
                name=skill.name,
            )
        import hashlib

        ran = {
            "version": skill.version or "",
            "sha256": hashlib.sha256(files[args.script]).hexdigest(),
        }
        self._audit(skill.name, "ok", ctx, args.script, ran=ran)
        try:
            return await exec_in_place(
                place,
                [interpreter, f"{rel}/{args.script}", *args.args],
                cwd=place.root,
                cwd_rel=".",
                stdin=None,
                timeout_s=self.timeout_s,
                refuse=self.refuse,
                kind="skill_script",
            )
        finally:
            await asyncio.to_thread(remove_materialized, place.root, rel)


def make_skill_script_tool(
    catalog: SkillCatalog,
    spec: SkillScriptsSpec,
    *,
    session_store: Any | None,
    tenant_id: str,
    manifest_id: str,
    settings: Any,
    object_store: Any | None = None,
) -> Tool:
    """`run_skill_script` over ``catalog``, run under ``spec``'s interpreters and timeout."""
    runner = _ScriptRunner(
        catalog,
        spec,
        session_store=session_store,
        tenant_id=tenant_id,
        manifest_id=manifest_id,
        settings=settings,
        object_store=object_store,
    )
    names = [s.name for s in catalog.list_public()]
    return define_tool(
        name=RUN_SCRIPT_TOOL,
        description=(
            "Run a script from an active skill's scripts/ directory, from the workspace root, with the "
            "arguments given. Activate the skill first; its instructions say which script and when."
        ),
        args=with_skill_names(_RunScriptArgs, names, "Skill name, active in this conversation."),
        handler=runner,
        # A script is code: command screening reads every argument, its output is screened as untrusted.
        transport="shell",
    )


__all__ = ["RUN_SCRIPT_TOOL", "make_skill_script_tool", "materialize", "remove_materialized", "script_source"]
