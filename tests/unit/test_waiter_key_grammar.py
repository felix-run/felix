"""A waiter name is a key, so composing one has to be injective.

Whoever can construct a waiter name can answer the wait behind it: `signal(name, payload)`
resolves whatever `wait(name)` is blocked on. So two different part-tuples producing the same
name is not untidiness, it is one conversation answering another's pending tool call.

`client:{thread_id}:{tool_call_id}` was built with a bare f-string while *both* parts can
contain the separator — `thread_id` legitimately (`{tenant}:{suffix}`, and
`{tenant}:fiber:{id}` for a durable run), and `tool_call_id` because it comes off the model
wire with no charset check. This file pins the collision that made reachable, and the general
property that prevents the next one.
"""

from __future__ import annotations

import asyncio

import pytest
from felix.tools.client_bridge import _name, complete_result, wait_for_result
from felix.waiters import waiter_name


def test_a_thread_suffix_cannot_forge_a_fiber_threads_waiter() -> None:
    """The concrete collision, spelled out.

    `fiber` is a legal thread suffix — `effective_thread_id` rejects only `:` and `#` — so
    any caller in the tenant can create `acme:fiber`. With a bare join, its client-tool call
    `F123:call_9` produced the same waiter name as the durable run on `acme:fiber:F123`
    answering its call `call_9`.
    """
    victim = _name("acme:fiber:F123", "call_9")
    forged = _name("acme:fiber", "F123:call_9")

    assert victim != forged, "a thread with the suffix 'fiber' can forge a durable run's client-tool waiter"


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # The reachable pair above, and the same shape one level shallower.
        (("acme:fiber:F123", "call_9"), ("acme:fiber", "F123:call_9")),
        (("t:a", "b:c"), ("t:a:b", "c")),
        # The escape itself must not be forgeable: a part containing the encoded form of the
        # separator must not collide with one containing the separator. This is why `%` is
        # replaced before `:` rather than after.
        (("t:a", "b"), ("t%3Aa", "b")),
        (("t", "a%3Ab"), ("t", "a:b")),
    ],
)
def test_distinct_parts_never_share_a_name(left: tuple[str, str], right: tuple[str, str]) -> None:
    assert _name(*left) != _name(*right), f"{left} and {right} share a waiter name"


def test_the_kind_is_escaped_too_so_a_plugin_cannot_reintroduce_this() -> None:
    """`waiter_name` is exported, and the plugin seam can reach it.

    All three in-tree kinds are literals without `%` or `:`, so escaping the kind is a no-op
    for them — which is exactly why it would be easy to leave out and impossible to notice. A
    plugin minting `waiter_name("commerce:refund", order_id)` against an unescaped kind
    reintroduces the collision this whole change removes, under a docstring that promises it
    cannot happen.
    """
    assert waiter_name("commerce:refund", "o1") != waiter_name("commerce", "refund", "o1")
    # And the no-op property the upgrade note depends on: real kinds are unchanged.
    assert waiter_name("approval", "abc123") == "approval:abc123"
    assert waiter_name("ui", "tok_9") == "ui:tok_9"


def test_the_name_still_identifies_its_kind_and_parts() -> None:
    """Injective, not opaque. A hash would also be injective and would make every waiter in
    `redis-cli --scan` and every log line unreadable, so the parts stay legible."""
    name = waiter_name("client", "acme:demo", "call_1")
    assert name.startswith("client:")
    assert "acme" in name and "demo" in name and "call_1" in name


def test_the_same_parts_still_reach_the_same_waiter() -> None:
    """The property the escaping must not break: a legitimate answer still arrives.

    Injectivity is worthless if it also makes the real caller miss. This drives the actual
    `wait`/`signal` pair over a thread id that contains the separator, which is every thread
    id the harness mints.
    """

    async def _round_trip() -> str:
        thread, call = "acme:fiber:F123", "call_9"

        async def _answer() -> None:
            await asyncio.sleep(0.05)
            assert await complete_result(thread, call, "pong")

        helper = asyncio.create_task(_answer())
        result = await wait_for_result(thread, call, timeout=2)
        await helper
        return result.content

    assert asyncio.run(_round_trip()) == "pong"


def test_an_answer_for_a_colliding_pair_does_not_satisfy_the_victim() -> None:
    """End to end: the forged answer must not resolve the real wait.

    The two tests above are about strings. This one is about the consequence — with the old
    join, `complete_result` on the attacker's pair returned `True` and the durable run's
    `wait_for_result` came back with the attacker's content instead of timing out.
    """

    async def _attempt() -> str:
        victim_thread, victim_call = "acme:fiber:F123", "call_9"

        async def _forge() -> None:
            await asyncio.sleep(0.05)
            await complete_result("acme:fiber", "F123:call_9", "attacker chose this")

        helper = asyncio.create_task(_forge())
        result = await wait_for_result(victim_thread, victim_call, timeout=0.4)
        await helper
        return result.content

    content = asyncio.run(_attempt())
    assert "attacker chose this" not in content, "a forged waiter answered the victim's wait"
    assert "timed out" in content, f"expected the victim to time out, got {content!r}"
