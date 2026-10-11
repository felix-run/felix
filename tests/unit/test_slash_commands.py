"""Slash commands: a user turn written `/name args` runs the manifest's prompt `name`."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from felix.manifests.loader import parse_manifest
from felix.prompts import PromptUnavailable
from felix.prompts import commands as commands_mod
from felix.prompts.commands import (
    MAX_COMMANDS_PER_REQUEST,
    MAX_SHELL_SPLIT_CHARS,
    expand_commands,
    parse_command,
    runnable_commands,
    split_args,
)
from felix.usage.catalog import catalog_from_manifest
from felix_ai.types import ChatMessage, ContentBlock

from tests.support.factories import make_settings


def _manifest(prompts: list[dict[str, Any]]) -> Any:
    return parse_manifest(
        {"apiVersion": "felix/v1", "kind": "Agent", "metadata": {"name": "cmd"}, "spec": {"prompts": prompts}}
    )


REVIEW = {"name": "review", "body": "Review $1 for ${2:-correctness}.", "description": "Review a file"}


async def _expand(manifest: Any, messages: list[ChatMessage]) -> list[ChatMessage]:
    return await expand_commands(manifest, messages, settings=make_settings(), tenant_id="default")


def _user(text: str) -> ChatMessage:
    return ChatMessage(role="user", content=text)


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("/review a.py perf", ("review", "a.py perf")),
        ("/review", ("review", "")),
        ("  /review a.py  ", ("review", "a.py")),
        ("/review\na.py", ("review", "a.py")),
        ("/etc/hosts is missing", None),
        ("review a.py", None),
        ("/ review", None),
        ("/-x", None),
    ],
)
def test_parse_command(text: str, parsed: tuple[str, str] | None) -> None:
    assert parse_command(text) == parsed


@pytest.mark.parametrize(
    ("rest", "args"),
    [
        ("a.py perf", ["a.py", "perf"]),
        ('"my file.py" perf', ["my file.py", "perf"]),
        ("it's broken", ["it's", "broken"]),  # the quote never closes: split on whitespace
        ("a.py\n\nfocus on X", ["a.py", "focus", "on", "X"]),  # a pasted paragraph is words
        ("", []),
    ],
)
def test_split_args(rest: str, args: list[str]) -> None:
    assert split_args(rest) == args


def test_a_long_argument_is_never_given_to_shlex(monkeypatch: pytest.MonkeyPatch) -> None:
    """`shlex` is quadratic in a token's length: a megabyte held the event loop for seconds."""

    def refuse(_: str) -> list[str]:
        raise AssertionError("shlex on a long argument")

    monkeypatch.setattr(commands_mod.shlex, "split", refuse)
    huge = '"' + "x" * (MAX_SHELL_SPLIT_CHARS + 1)
    assert split_args(huge) == [huge]


async def test_an_undeclared_name_is_never_split(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(_: str) -> list[str]:
        raise AssertionError("split an undeclared command's arguments")

    monkeypatch.setattr(commands_mod, "split_args", refuse)
    out = await _expand(_manifest([REVIEW]), [_user('/other "x y"')])
    assert out[0].content == '/other "x y"'


async def test_a_declared_command_is_replaced_by_its_expansion() -> None:
    sent = ChatMessage(role="user", content="/review a.py perf", name="dana")
    out = await _expand(_manifest([REVIEW]), [sent])
    assert out == [dataclasses.replace(sent, content="Review a.py for perf.")]


async def test_a_name_is_matched_regardless_of_case() -> None:
    out = await _expand(_manifest([REVIEW]), [_user("/Review a.py")])
    assert out[0].content == "Review a.py for correctness."


async def test_every_user_turn_is_expanded_not_only_the_newest() -> None:
    """A `/v1` client resends its history: an earlier command must read as it did when answered."""
    sent = [_user("/review a.py"), ChatMessage(role="assistant", content="ok"), _user("/review b.py perf")]
    out = await _expand(_manifest([REVIEW]), sent)
    assert [m.content for m in out] == ["Review a.py for correctness.", "ok", "Review b.py for perf."]


async def test_each_prompt_body_is_read_once_per_request(monkeypatch: pytest.MonkeyPatch) -> None:
    reads: list[str] = []
    real = commands_mod.resolve_prompt_body

    async def counting(spec: Any, **kw: Any) -> str:
        reads.append(spec.name)
        return await real(spec, **kw)

    monkeypatch.setattr(commands_mod, "resolve_prompt_body", counting)
    await _expand(_manifest([REVIEW]), [_user("/review a"), _user("/review b"), _user("/review c")])
    assert reads == ["review"]


async def test_more_commands_than_the_limit_are_refused() -> None:
    at_limit = [_user("/review a")] * MAX_COMMANDS_PER_REQUEST
    assert len(await _expand(_manifest([REVIEW]), at_limit)) == MAX_COMMANDS_PER_REQUEST
    with pytest.raises(PromptUnavailable, match=f"too_many_commands:{MAX_COMMANDS_PER_REQUEST}"):
        await _expand(_manifest([REVIEW]), [*at_limit, _user("/review a")])


async def test_an_undeclared_name_is_sent_as_written() -> None:
    out = await _expand(_manifest([REVIEW]), [_user("/shrug"), _user("/etc/hosts")])
    assert [m.content for m in out] == ["/shrug", "/etc/hosts"]


async def test_only_user_turns_in_plain_text_are_commands() -> None:
    parts = ChatMessage(
        role="user", content="/review a.py", content_blocks=[ContentBlock(type="text", text="/review a.py")]
    )
    sent = [ChatMessage(role="assistant", content="/review a.py"), parts]
    out = await _expand(_manifest([REVIEW]), sent)
    assert [m.content for m in out] == ["/review a.py", "/review a.py"]


async def test_a_declared_command_that_expands_to_nothing_is_refused() -> None:
    with pytest.raises(PromptUnavailable, match="empty_prompt:blank"):
        await _expand(_manifest([{"name": "blank", "body": ""}]), [_user("/blank")])


def test_only_prompts_a_person_can_type_are_runnable() -> None:
    manifest = _manifest([REVIEW, {"name": "two words", "body": "x"}, {"name": "fix", "body": "x"}])
    assert [p.name for p in runnable_commands(manifest)] == ["review", "fix"]
    assert runnable_commands(_manifest([])) == []


def test_the_listing_carries_each_runnable_command() -> None:
    manifest = _manifest([{**REVIEW, "argument_hint": "<file>"}, {"name": "two words", "body": "x"}])
    commands = catalog_from_manifest("cmd", manifest)["felix"]["commands"]
    assert commands == [{"name": "review", "description": "Review a file", "argumentHint": "<file>"}]
    assert catalog_from_manifest("unresolved", None)["felix"]["commands"] == []
