"""Output styles: a conversation's chosen style joins the system prompt of the agent it talks to."""

from __future__ import annotations

from typing import Any

import pytest
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.durability.runs import BACKGROUND_CHILD_EXTRA
from felix.manifests.loader import ManifestParseError, parse_manifest
from felix.output_styles import chosen_style
from felix.session.thread_state import update_thread_meta

from tests.support.factories import make_settings

TERSE = {"name": "terse", "description": "Short answers", "prompt": "Answer in one sentence."}
TEACH = {"name": "teach", "prompt": "Explain each step as you go."}


def _manifest(name: str = "styled", *, default: str | None = None, **spec: Any) -> Any:
    styles: dict[str, Any] = {"styles": [TERSE, TEACH]}
    if default is not None:
        styles["default"] = default
    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": name},
            "spec": {"pattern": "react", "output_styles": styles, **spec},
        }
    )


@pytest.mark.parametrize(
    ("styles", "error"),
    [
        ({"styles": [TERSE, {**TERSE, "name": "TERSE"}]}, "unique"),
        ({"default": "loud", "styles": [TERSE]}, "must name one of"),
        ({"styles": [{"name": "two words", "prompt": "x"}]}, "name"),
        ({"styles": [{"name": "empty", "prompt": ""}]}, "prompt"),
        ({"styles": [{"name": "f", "file": "style.md"}]}, "file"),
    ],
)
def test_a_malformed_style_list_is_refused(styles: dict[str, Any], error: str) -> None:
    with pytest.raises(ManifestParseError, match=error):
        parse_manifest(
            {
                "apiVersion": "felix/v1",
                "kind": "Agent",
                "metadata": {"name": "x"},
                "spec": {"output_styles": styles},
            }
        )


async def test_no_style_without_a_choice_or_a_default() -> None:
    assert await chosen_style(_manifest(), top_level=True) is None


async def test_the_default_applies_with_no_request_at_all() -> None:
    style = await chosen_style(_manifest(default="terse"), top_level=True)
    assert style is not None and style.name == "terse"


async def test_a_child_compile_keeps_its_own_prompt() -> None:
    assert await chosen_style(_manifest(default="terse"), top_level=False) is None


async def _chosen_on_thread(
    stored: str | None, *, default: str | None = None, extras: dict[str, Any] | None = None
) -> str | None:
    settings = make_settings()
    await update_thread_meta(settings=settings, tenant_id="default", thread_id="t1", output_style=stored)
    ctx = RequestContext(
        settings=settings, auth=AuthContext(), manifest_id="styled", thread_id="t1", extras=extras or {}
    )
    async with async_run_with_context(ctx):
        style = await chosen_style(_manifest(default=default), top_level=True)
    return style.name if style is not None else None


async def test_the_threads_stored_style_wins_over_the_default() -> None:
    assert await _chosen_on_thread("teach", default="terse") == "teach"


async def test_a_stored_style_matches_regardless_of_case() -> None:
    assert await _chosen_on_thread("TEACH") == "teach"


async def test_a_style_this_manifest_does_not_declare_reads_as_its_default() -> None:
    """The thread moved to another manifest since the style was set."""
    assert await _chosen_on_thread("pirate", default="terse") == "terse"
    assert await _chosen_on_thread("pirate") is None


async def test_the_style_reaches_only_the_top_level_agents_prompt() -> None:
    """A sub-agent answers its parent, not the person: it keeps the prompt its author wrote."""
    from felix.manifests.builder import BuildDeps, build_agent
    from felix.tools.provider import InMemoryToolProvider

    child = _manifest("helper", default="teach")
    parent = _manifest("lead", default="terse", sub_agents=["helper"])
    deps = BuildDeps(tools=InMemoryToolProvider(), settings=make_settings())

    async def build_child(name: str) -> Any:
        assert name == "helper"
        return await build_agent(child, deps=deps)

    deps.sub_agent_builder = build_child
    agent = await build_agent(parent, deps=deps)
    assert _composed(agent.system_prompt).endswith("# Output style: terse\n\nAnswer in one sentence.")
    assert "Output style" not in (deps.compiled["helper"].system_prompt or "")


async def test_a_background_task_child_keeps_its_own_prompt() -> None:
    """It compiles in the worker as a run of its own, so only the run can say it is a child."""
    assert await _chosen_on_thread("teach", default="terse", extras={BACKGROUND_CHILD_EXTRA: True}) is None


def _composed(system_prompt: str) -> str:
    """The prompt as the manifest composes it: today's date follows the style (`felix/clock.py`)."""
    composed, _, date = system_prompt.rpartition("\n\n<current_date>")
    assert composed and date, system_prompt
    return composed


async def _build(manifest: Any, children: dict[str, Any] | None = None) -> Any:
    from felix.manifests.builder import BuildDeps, build_agent
    from felix.tools.provider import InMemoryToolProvider

    deps = BuildDeps(tools=InMemoryToolProvider(), settings=make_settings())

    async def build_child(name: str) -> Any:
        return await build_agent((children or {})[name], deps=deps)

    deps.sub_agent_builder = build_child
    return await build_agent(manifest, deps=deps)


async def test_a_pattern_whose_own_prompt_does_not_answer_refuses_styles() -> None:
    """On a router the chosen child writes the answer: a style would be stored and do nothing."""
    child = parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "helper"},
            "spec": {"pattern": "react"},
        }
    )
    router = _manifest("lead", default="terse", sub_agents=["helper"])
    router = router.model_copy(update={"spec": router.spec.model_copy(update={"pattern": "router"})})
    with pytest.raises(ValueError, match=r"does not support spec\.output_styles"):
        await _build(router, {"helper": child})


async def test_the_style_ends_the_prompt_and_the_notice_still_opens_it() -> None:
    from felix.manifests.governance import transparency_notice_text

    prompt = (
        await _build(_manifest(default="terse", governance={"transparency_notice": True}))
    ).system_prompt
    assert prompt.startswith(transparency_notice_text("styled"))
    assert _composed(prompt).endswith("# Output style: terse\n\nAnswer in one sentence.")
