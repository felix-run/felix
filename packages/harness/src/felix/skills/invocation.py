"""Skills the harness activates itself, before the model's first step: `/name` and `preload`.

agentskills.io's client guide names user-explicit activation -- a person types `/skill-name` and the
harness loads the skill -- beside the model's own judgement. Here the person's turn stays exactly as
written (`/triage ACME 900`), and the run begins with the harness calling `activate_skill` for them:
through the tool runner and so the whole governance stack, logged as an activation like any other.
The body therefore reaches the model as a screened tool result, never as text in the person's turn:
an imported skill's instructions do not gain the authority of the person's own words.

A declared prompt of the same name wins: `/chat` expands `spec.prompts` commands before the agent
runs, so a turn still written `/name` here named no prompt. A name that is no skill the person may
run (`SkillCatalog.get_for_user`) is sent as written, as `/etc/hosts is missing` is.

`SkillRef.preload` starts every conversation with a declared skill activated, the same way, once:
a preloaded skill the thread has ever activated -- even one since deactivated -- is not activated
again.

Only a turn a route marked as the person's own (`InvokeInput.person_turn`: `/chat`, `/v1`, a durable
`/chat` run) is read for `/name`. A turn a model wrote -- a `task` prompt, a plan step, an A2A
peer's message -- must not run a skill hidden from the model. What a person's call may do that the
model's may not -- reach such a skill, pass `arguments` -- is granted by call id: the preamble mints
the ids and registers them (`PersonCalls`), and `activate_skill` honours each once. Nothing ambient
carries it, so nothing the model does later inherits it.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from felix.skills.activation import ACTIVATE_TOOL, thread_history

if TYPE_CHECKING:
    from felix_ai.types import ChatMessage, ToolCall

    from felix.manifests.schema import SkillRef
    from felix.skills.types import SkillCatalog

logger = logging.getLogger("felix.skills.invocation")

INVOKE_PREFIX = "invoke_"
PRELOAD_PREFIX = "preload_"


@dataclass(slots=True)
class PersonCalls:
    """The `activate_skill` call ids the harness minted for a person (or for the manifest's
    preload), each good for one call. Shared by a compile's preamble and its skill tools."""

    _ids: set[str] = field(default_factory=set)
    _open: bool = False

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        """End the preamble round: every id it minted, spent or not, is void."""
        self._open = False
        self._ids.clear()

    def mint(self, prefix: str) -> str:
        call_id = f"{prefix}{uuid.uuid4().hex[:16]}"
        self._ids.add(call_id)
        return call_id

    def claim(self, call_id: str | None) -> str | None:
        """Who the call was made for -- `person` or `preload` -- or None for any other call. Once:
        a model replaying an id the log shows it gets nothing."""
        if not self._open or not call_id or call_id not in self._ids:
            return None
        self._ids.discard(call_id)
        return "person" if call_id.startswith(INVOKE_PREFIX) else "preload"


def invoked_skill(
    catalog: SkillCatalog, messages: list[ChatMessage], *, declared: frozenset[str] = frozenset()
) -> tuple[str, str] | None:
    """`(name, arguments)` when the run ends on a person running a skill as `/name args`. Only the
    last message: a `/v1` client resends its history, and an earlier `/name` was run when it was sent.

    A skill hidden from the model (`disable-model-invocation`) is a person's to run only when the
    manifest names it in `spec.skills`: otherwise every host skill and every published library skill
    marked so -- an operator's "off" switch or a manual-only runbook -- would be any chat caller's,
    anonymous ones included. A skill the model may use is a person's too, as the model could reach it."""
    from felix.prompts.commands import parse_command

    turn = messages[-1] if messages else None
    if turn is None or turn.role != "user" or turn.content_blocks is not None or not turn.content:
        return None
    command = parse_command(turn.content)
    if command is None or catalog.get_for_user(command[0]) is None:
        return None
    if catalog.get_invocable(command[0]) is None and command[0] not in declared:
        return None
    return command


@dataclass(slots=True)
class SkillPreamble:
    """The `activate_skill` calls a run starts with. Built by the builder for a compile with skills."""

    catalog: SkillCatalog
    preload: tuple[str, ...]
    session_store: Any | None
    person_calls: PersonCalls
    # The names `spec.skills` declares: the only skills hidden from the model a person may run.
    declared: frozenset[str] = frozenset()
    # Set by the pattern that runs it (`patterns/react.py`). A compile whose pattern never does --
    # a router, `parallel` -- is told so, rather than leaving `preload` a field that does nothing.
    consumed: bool = field(default=False)

    async def calls(
        self, thread_id: str | None, incoming: list[ChatMessage], *, person_turn: bool
    ) -> list[ToolCall]:
        from felix_ai.types import ToolCall

        invoked = invoked_skill(self.catalog, incoming, declared=self.declared) if person_turn else None
        calls: list[ToolCall] = []
        if self.preload:
            # One read of the thread, and only for a manifest that preloads.
            seen = await self._seen(thread_id)
            for name in self.preload:
                skill = self.catalog.get_activatable(name)
                if skill is None:
                    logger.warning("preloaded skill %s is not in the catalog; not activated", name)
                    continue
                if skill.untrusted:
                    # An import nobody has adopted is the model's to choose, never every conversation's
                    # opening instructions.
                    logger.warning("preloaded skill %s is imported and not adopted; not activated", name)
                    continue
                if name in seen or (invoked is not None and invoked[0] == name):
                    continue
                call_id = self.person_calls.mint(PRELOAD_PREFIX)
                calls.append(ToolCall(id=call_id, name=ACTIVATE_TOOL, args={"name": name}))
        if invoked is not None:
            name, arguments = invoked
            args: dict[str, Any] = {"name": name, **({"arguments": arguments} if arguments else {})}
            calls.append(ToolCall(id=self.person_calls.mint(INVOKE_PREFIX), name=ACTIVATE_TOOL, args=args))
        return calls

    async def _seen(self, thread_id: str | None) -> set[str]:
        if self.session_store is None or not thread_id:
            return set()
        _active, seen = await thread_history(self.session_store.open(thread_id))
        return seen


def skill_preamble(
    catalog: SkillCatalog, refs: list[SkillRef], session_store: Any | None, person_calls: PersonCalls
) -> SkillPreamble | None:
    """The preamble for a compile, or None when it could never add a call: no skill a person can
    run and nothing to preload."""
    preload = tuple(r.name for r in refs if r.preload)
    if not preload and not any(s.user_invocable and not s.unresolved for s in catalog.skills.values()):
        return None
    return SkillPreamble(
        catalog=catalog,
        preload=preload,
        session_store=session_store,
        person_calls=person_calls,
        declared=frozenset(r.name for r in refs),
    )


__all__ = [
    "INVOKE_PREFIX",
    "PRELOAD_PREFIX",
    "PersonCalls",
    "SkillPreamble",
    "invoked_skill",
    "skill_preamble",
]
