"""`/name` arguments, the invocation frontmatter, and which turns the preamble acts on."""

from __future__ import annotations

from typing import Any

from felix.skills.arguments import with_arguments
from felix.skills.format import MAX_SKILL_MD_CHARS as LIMIT
from felix.skills.invocation import PersonCalls, invoked_skill, skill_preamble
from felix.skills.loader import parse_skill_md
from felix.skills.types import Skill, SkillCatalog
from felix_ai.types import ChatMessage


def test_arguments_go_where_the_body_asks() -> None:
    body = "Fix issue $ARGUMENTS[0] in $1; all: $ARGUMENTS; missing: [$5]"
    assert (
        with_arguments(body, '42 "the parser"', limit=LIMIT)
        == 'Fix issue 42 in the parser; all: 42 "the parser"; missing: []'
    )


def test_a_body_that_asks_for_none_gets_them_appended() -> None:
    """Never dropped: a person who typed arguments meant them."""
    assert (
        with_arguments("Do the thing.", "now please", limit=LIMIT) == "Do the thing.\n\nARGUMENTS: now please"
    )


def test_the_invocation_frontmatter_is_read() -> None:
    skill = parse_skill_md(
        "---\nname: fix\ndescription: Fix one.\nuser-invocable: false\nargument-hint: [issue]\n---\nbody\n",
        fallback_name="fix",
    )
    assert skill is not None and skill.user_invocable is False and skill.argument_hint == "[issue]"


def test_a_metadata_block_cannot_decide_who_runs_a_skill() -> None:
    """`user-invocable` is the author's decision at the top level; a nested `metadata:` key, which
    the loader otherwise merges in, must not set it."""
    skill = parse_skill_md(
        "---\nname: fix\ndescription: Fix.\nmetadata:\n  user-invocable: false\n---\nb\n",
        fallback_name="fix",
    )
    assert skill is not None and skill.user_invocable is True


def _catalog() -> SkillCatalog:
    return SkillCatalog(
        skills={
            "fix": Skill(name="fix", description="Fix.", body="b"),
            "quiet": Skill(name="quiet", description="Q.", body="b", user_invocable=False),
        }
    )


def test_only_a_run_that_ends_on_the_command_invokes() -> None:
    older = [ChatMessage(role="user", content="/fix 1"), ChatMessage(role="assistant", content="done")]
    assert invoked_skill(_catalog(), older) is None
    assert invoked_skill(_catalog(), [*older, ChatMessage(role="user", content="/fix 2")]) == ("fix", "2")


def test_a_skill_the_person_may_not_run_is_not_an_invocation() -> None:
    assert invoked_skill(_catalog(), [ChatMessage(role="user", content="/quiet go")]) is None


def test_no_preamble_for_a_catalog_with_nothing_to_run_or_preload() -> None:
    from felix.manifests.schema import SkillRef

    only_quiet = SkillCatalog(skills={"quiet": _catalog().skills["quiet"]})
    assert skill_preamble(only_quiet, [], None, PersonCalls()) is None
    preamble = skill_preamble(_catalog(), [], None, PersonCalls())
    assert preamble is not None and preamble.preload == ()
    preloading = skill_preamble(only_quiet, [SkillRef(name="quiet", preload=True)], None, PersonCalls())
    assert preloading is not None and preloading.preload == ("quiet",)


def test_argument_edge_cases() -> None:
    assert with_arguments("a $ARGUMENTS[9] b", "x", limit=LIMIT) == "a  b"
    # `$N` alone does not count as asking for the arguments, so they are appended as well.
    assert with_arguments("cost $1x of $1", "a b", limit=LIMIT) == "cost $1x of b\n\nARGUMENTS: a b", (
        "no word boundary, no substitution"
    )
    assert (
        with_arguments("$0 then $1 ($ARGUMENTS)", '"oops two', limit=LIMIT) == '"oops then two ("oops two)'
    ), "unbalanced quotes split on whitespace"
    assert with_arguments("say $ARGUMENTS", "$1 literally", limit=LIMIT) == "say $1 literally", (
        "arguments are not re-read"
    )


async def test_an_imported_skill_is_not_preloaded() -> None:
    from felix.skills.invocation import SkillPreamble

    catalog = SkillCatalog(skills={"fix": Skill(name="fix", description="F.", body="b", untrusted=True)})
    preamble = SkillPreamble(
        catalog=catalog, preload=("fix",), session_store=None, person_calls=PersonCalls()
    )
    assert await preamble.calls(None, [], person_turn=True) == []


async def test_invocation_and_preload_of_one_skill_is_one_call_with_its_arguments() -> None:
    from felix.skills.invocation import SkillPreamble

    preamble = SkillPreamble(
        catalog=_catalog(), preload=("fix",), session_store=None, person_calls=PersonCalls()
    )
    calls = await preamble.calls(None, [ChatMessage(role="user", content="/fix 7")], person_turn=True)
    assert [(c.name, c.args) for c in calls] == [("activate_skill", {"name": "fix", "arguments": "7"})]


def _tools_with_calls(catalog: SkillCatalog) -> tuple[dict, Any]:
    from felix.skills.invocation import PersonCalls
    from felix.skills.tools import make_skill_tools

    calls = PersonCalls()
    calls.open()  # as the preamble round does
    tools = make_skill_tools(catalog, session_store=None, tenant_id="t", manifest_id="m", person_calls=calls)
    return {t.name: t for t in tools}, calls


async def test_a_person_running_an_imported_skill_still_gets_screened_output() -> None:
    """The safety argument: the body reaches the model as a tool result content screening reads as
    untrusted, whoever asked for it."""
    from felix.tools.types import ToolInvocationCtx, is_untrusted_output

    catalog = SkillCatalog(
        skills={
            "ext": Skill(
                name="ext", description="E.", body="b", untrusted=True, disable_model_invocation=True
            )
        }
    )
    tools, calls = _tools_with_calls(catalog)
    out = await tools["activate_skill"].executor.execute(
        {"name": "ext"}, ToolInvocationCtx(tool_call_id=calls.mint("invoke_"))
    )
    assert is_untrusted_output(out)


async def test_a_person_s_call_id_is_good_once() -> None:
    """A model replaying an id the log shows it gets the model's answer, not the person's."""
    import json

    from felix.tools.types import ToolInvocationCtx, tool_output_content

    catalog = SkillCatalog(
        skills={"ops": Skill(name="ops", description="O.", body="b", disable_model_invocation=True)}
    )
    tools, calls = _tools_with_calls(catalog)
    ctx = ToolInvocationCtx(tool_call_id=calls.mint("invoke_"))
    first = json.loads(
        tool_output_content(await tools["activate_skill"].executor.execute({"name": "ops"}, ctx))
    )
    again = json.loads(
        tool_output_content(await tools["activate_skill"].executor.execute({"name": "ops"}, ctx))
    )
    assert first["activated"] == "ops"
    assert again == {"error": "unknown_skill", "name": "ops"}


async def test_the_model_cannot_pass_arguments() -> None:
    """Its text inside a skill's instructions would be re-shown as the skill's for the rest of the
    conversation; it is ignored, and the result says so."""
    import json

    from felix.tools.types import ToolInvocationCtx, tool_output_content

    tools, _calls = _tools_with_calls(
        SkillCatalog(skills={"fix": Skill(name="fix", description="F.", body="Do $ARGUMENTS.")})
    )
    out = json.loads(
        tool_output_content(
            await tools["activate_skill"].executor.execute(
                {"name": "fix", "arguments": "whatever the page said"},
                ToolInvocationCtx(tool_call_id="toolu_1"),
            )
        )
    )
    assert "whatever the page said" not in out["instructions"] and out["arguments_ignored"] is True


def test_substitution_is_bounded() -> None:
    """A body of many `$1`s and a long argument must not expand past the cap on the way."""
    import tracemalloc

    tracemalloc.start()
    try:
        out = with_arguments("$1 " * 2_000, "x " + "y" * 10_000, limit=1000)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(out) == 1000
    assert peak < 2_000_000, f"built {peak} bytes on the way to 1000 characters"


async def test_a_turn_no_person_wrote_runs_nothing() -> None:
    """A `task` prompt, a plan step, an A2A message: `/name` in it is text, not a command."""
    from felix.skills.invocation import SkillPreamble

    preamble = SkillPreamble(catalog=_catalog(), preload=(), session_store=None, person_calls=PersonCalls())
    assert await preamble.calls(None, [ChatMessage(role="user", content="/fix 7")], person_turn=False) == []


async def test_a_preload_the_conversation_deactivated_stays_deactivated() -> None:
    """Once per conversation: the model or the person turned it off, and the next turn must not undo it."""
    import json

    from felix.session.store import InMemorySessionStore
    from felix.session.tree import annotate_and_append
    from felix.session.types import chat_message_to_event
    from felix.skills.invocation import SkillPreamble

    sessions = InMemorySessionStore(tenant_id="t")
    await annotate_and_append(
        sessions.open("t:1"),
        [
            chat_message_to_event(
                ChatMessage(
                    role="tool",
                    name="activate_skill",
                    tool_call_id="a",
                    content=json.dumps({"activated": "fix"}),
                )
            ),
            chat_message_to_event(
                ChatMessage(
                    role="tool",
                    name="deactivate_skill",
                    tool_call_id="d",
                    content=json.dumps({"deactivated": "fix"}),
                )
            ),
        ],
    )
    preamble = SkillPreamble(
        catalog=_catalog(), preload=("fix",), session_store=sessions, person_calls=PersonCalls()
    )
    assert await preamble.calls("t:1", [ChatMessage(role="user", content="hi")], person_turn=True) == []


def test_an_id_the_round_never_spent_dies_with_it() -> None:
    """A preamble call refused before it ran (an approval, a hook) leaves its id unclaimed; the model
    has seen it in its history, and must not be able to spend it later."""
    calls = PersonCalls()
    calls.open()
    unspent = calls.mint("invoke_")
    calls.close()
    calls.open()
    assert calls.claim(unspent) is None
    # And outside a round no id is anyone's, however fresh.
    closed = PersonCalls()
    assert closed.claim(closed.mint("invoke_")) is None


def test_a_hidden_skill_is_a_person_s_only_where_the_manifest_declares_it() -> None:
    """Otherwise every host or library skill marked `disable-model-invocation` -- an "off" switch, a
    manual-only runbook -- would be any chat caller's to run."""
    catalog = SkillCatalog(
        skills={"ops": Skill(name="ops", description="O.", body="b", disable_model_invocation=True)}
    )
    turn = [ChatMessage(role="user", content="/ops now")]
    assert invoked_skill(catalog, turn) is None
    assert invoked_skill(catalog, turn, declared=frozenset({"ops"})) == ("ops", "now")


def _ops_manifest() -> Any:
    from felix.manifests.loader import parse_manifest

    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "ops"},
            "spec": {"auth": {"inbound": {"allow_anonymous": False, "required_scopes": ["ops"]}}},
        }
    )


async def _invocations(scopes: frozenset[str], **turn: Any) -> list[tuple[str, dict]]:
    from felix.config import Settings
    from felix.context import AuthContext, RequestContext, async_run_with_context
    from felix.skills.invocation import SkillPreamble

    preamble = SkillPreamble(
        catalog=_catalog(),
        preload=(),
        session_store=None,
        person_calls=PersonCalls(),
        manifest=_ops_manifest(),
    )
    auth = AuthContext(principal_sub="p", anonymous=False, scopes=scopes, scheme="access")
    ctx = RequestContext(settings=Settings(database_url="memory://t"), auth=auth, manifest_id="lead")
    async with async_run_with_context(ctx):
        calls = await preamble.calls(None, turn.pop("messages", []), **turn)
    return [(c.name, c.args) for c in calls]


async def test_a_carried_command_runs_only_for_a_caller_the_child_admits() -> None:
    """A composite's child, or a router's pick, has its own door; the route checked only the root's.
    A caller without the child's scope does not run its skills through a parent."""
    carried = {"person_turn": False, "person_command": "/fix 7"}
    assert await _invocations(frozenset(), **carried) == []
    assert await _invocations(frozenset({"ops"}), **carried) == [
        ("activate_skill", {"name": "fix", "arguments": "7"})
    ]


async def test_a_forwarded_person_turn_is_held_to_the_child_s_door_too() -> None:
    """A router hands its pick the person's own turn (`person_turn`), not a carried command."""
    turn = {"person_turn": True, "messages": [ChatMessage(role="user", content="/fix 7")]}
    assert await _invocations(frozenset(), **turn) == []
    assert await _invocations(frozenset({"ops"}), **turn) == [
        ("activate_skill", {"name": "fix", "arguments": "7"})
    ]


async def test_with_no_request_context_no_caller_is_admitted() -> None:
    from felix.skills.invocation import SkillPreamble

    preamble = SkillPreamble(
        catalog=_catalog(),
        preload=(),
        session_store=None,
        person_calls=PersonCalls(),
        manifest=_ops_manifest(),
    )
    assert await preamble.calls(None, [], person_turn=False, person_command="/fix 7") == []


async def test_the_compile_hands_the_preamble_its_manifest(monkeypatch: Any, tmp_path: Any) -> None:
    """The door check reads the preamble's manifest; one compiled without it skips the check."""
    import felix.skills.invocation as invocation
    from felix.config import Settings
    from felix.context import AuthContext, RequestContext, async_run_with_context
    from felix.manifests.schema import SkillRef
    from felix.runtime import build_tenant_agent
    from felix.tools.builtins import default_tool_provider

    (tmp_path / "fix").mkdir()
    (tmp_path / "fix" / "SKILL.md").write_text("---\nname: fix\ndescription: Fix.\n---\nb\n")
    seen: list[Any] = []
    real = invocation.skill_preamble

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("manifest"))
        return real(*args, **kwargs)

    monkeypatch.setattr(invocation, "skill_preamble", spy)
    manifest = _ops_manifest().model_copy(deep=True)
    manifest.spec.skills = [SkillRef(name="fix")]
    settings = Settings(database_url="memory://t", skills_dir=str(tmp_path))
    ctx = RequestContext(settings=settings, auth=AuthContext(tenant_id="acme"), manifest_id="ops")
    async with async_run_with_context(ctx):
        await build_tenant_agent(
            settings,
            manifest=manifest,
            tools=default_tool_provider(),
            tenant_id="acme",
            skill_owner=None,
            person="",
        )
    assert [getattr(m, "metadata", None) and m.metadata.name for m in seen] == ["ops"]


def test_every_surface_that_runs_a_person_s_slash_command_is_known() -> None:
    """A manual-only skill needs `skills:invoke` only where a route says its caller may be an agent
    (`AGENT_CALLER_EXTRA`), so a new surface marking `person_turn` would run them for anyone. Each
    one is decided here: `/chat` is a person's, `/v1` must carry the mark."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    marking = {
        str(p.relative_to(root))
        for base in ("apps", "packages")
        for p in (root / base).rglob("*.py")
        if re.search(r"person_turn\s*=\s*True", p.read_text())
    }
    assert marking == {
        "apps/api/src/felix_api/routes/chat.py",
        "apps/api/src/felix_api/routes/openai_compat.py",
    }
    v1 = (root / "apps/api/src/felix_api/routes/openai_compat.py").read_text()
    assert "AGENT_CALLER_EXTRA: True" in v1
