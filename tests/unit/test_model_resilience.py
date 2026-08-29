"""Retry, and keeping the cached prompt prefix stable.

There was no retry anywhere in the model layer: `_is_provider_error` existed but was only
consulted by `_FallbackClient` to advance to the next *model*, and with no
`spec.fallbacks` configured — the default in every bundled manifest — a single 429 failed
the whole run.

Separately, recalled memory facts were appended to the system prompt. Caching is a prefix
match over tools -> system -> messages, so a block that changes whenever memory writes a
fact invalidated the entire cached prefix every turn.
"""

from __future__ import annotations

import asyncio

import pytest
from felix_ai.wire.transport import (
    MODEL_MAX_RETRIES,
    _backoff_delay,
    _retry_after_seconds,
)
from felix_ai.wire.transport import post_with_retry as _post_with_retry


class _Resp:
    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self.headers = headers or {}


class _Client:
    def __init__(self, statuses: list[int]) -> None:
        self._statuses = list(statuses)
        self.calls = 0

    async def post(self, url: str, json=None, headers=None) -> _Resp:
        self.calls += 1
        return _Resp(self._statuses.pop(0) if self._statuses else 200)


# --- retry ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_is_retried_and_succeeds() -> None:
    c = _Client([429, 503, 200])
    resp = await _post_with_retry(c, "u", label="anthropic", json={}, headers={}, max_retries=2)
    assert c.calls == 3
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_non_retryable_status_is_not_retried() -> None:
    """A 400 will not succeed on a retry; retrying it only adds latency."""
    c = _Client([400])
    resp = await _post_with_retry(c, "u", label="openai", json={}, headers={}, max_retries=2)
    assert c.calls == 1
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_retries_are_bounded() -> None:
    c = _Client([429, 429, 429, 429, 429])
    resp = await _post_with_retry(c, "u", label="anthropic", json={}, headers={}, max_retries=2)
    assert c.calls == 3, "must stop after max_retries, not loop"
    assert resp.status_code == 429, "the caller raises ModelGatewayError from this"


@pytest.mark.asyncio
async def test_connection_errors_are_retried() -> None:
    import httpx

    class _Flaky:
        def __init__(self) -> None:
            self.calls = 0

        async def post(self, url, json=None, headers=None):
            self.calls += 1
            if self.calls < 3:
                raise httpx.ConnectError("boom")
            return _Resp(200)

    c = _Flaky()
    resp = await _post_with_retry(c, "u", label="anthropic", json={}, headers={}, max_retries=2)
    assert c.calls == 3
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_read_timeout_is_not_retried() -> None:
    """A read timeout is a ceiling, not backpressure.

    The request was accepted and the model is still generating; retrying re-sends identical
    input and waits out the identical timeout, so a genuinely long generation used to cost
    three full timeouts before surfacing as a failed run.
    """
    import httpx

    class _Slow:
        def __init__(self) -> None:
            self.calls = 0

        async def post(self, url, json=None, headers=None):
            self.calls += 1
            raise httpx.ReadTimeout("too slow")

    c = _Slow()
    with pytest.raises(httpx.ReadTimeout):
        await _post_with_retry(c, "u", label="anthropic", json={}, headers={}, max_retries=2)
    assert c.calls == 1, "a read timeout must not be retried"


@pytest.mark.asyncio
async def test_write_timeout_is_not_retried() -> None:
    """Same argument for the sending half: a large body is as large on the second attempt."""
    import httpx

    class _Slow:
        def __init__(self) -> None:
            self.calls = 0

        async def post(self, url, json=None, headers=None):
            self.calls += 1
            raise httpx.WriteTimeout("too slow")

    c = _Slow()
    with pytest.raises(httpx.WriteTimeout):
        await _post_with_retry(c, "u", label="anthropic", json={}, headers={}, max_retries=2)
    assert c.calls == 1


@pytest.mark.asyncio
async def test_connect_timeout_is_still_retried() -> None:
    """The near miss that makes the boundary worth pinning.

    `ConnectTimeout` is a `TimeoutException` like the two above, so broadening the handler to
    `except httpx.TimeoutException` looks like tidying and silently stops retrying a
    genuinely transient failure — nothing was accepted, so the next attempt is a different
    bet.
    """
    import httpx

    class _Flaky:
        def __init__(self) -> None:
            self.calls = 0

        async def post(self, url, json=None, headers=None):
            self.calls += 1
            if self.calls < 3:
                raise httpx.ConnectTimeout("unreachable")
            return _Resp(200)

    c = _Flaky()
    resp = await _post_with_retry(c, "u", label="anthropic", json={}, headers={}, max_retries=2)
    assert c.calls == 3
    assert resp.status_code == 200


def test_a_timeout_does_not_advance_the_fallback_chain() -> None:
    """`_FallbackClient` must not treat a timeout as a provider error.

    If it did, a too-slow generation would be retried once per fallback model, each paying
    the full ceiling — the same amplification this change removed from `_post_with_retry`.
    """
    import httpx
    from felix.patterns.model import _is_provider_error

    assert _is_provider_error(httpx.ReadTimeout("x")) is False
    assert _is_provider_error(httpx.WriteTimeout("x")) is False


def test_model_timeout_comes_from_the_clients_own_settings() -> None:
    """The client must honour the `Settings` it was built with, not the process globals.

    `build_model` exists so a caller can pass its own `Settings`, and every other field on
    the client honours that. A timeout read from `get_settings()` would ignore it — and the
    divergence is invisible until someone's configured value quietly fails to take effect.
    """
    from felix.config import Settings
    from felix.patterns.model import provider_factory
    from felix.timeouts import DEFAULT_CONNECT_TIMEOUT_S
    from felix_ai.providers import ANTHROPIC

    client = provider_factory(ANTHROPIC)(
        "claude-sonnet",
        {"provider": "anthropic", "model": "claude-sonnet-5"},
        None,
        Settings(model_timeout_seconds=300.0, database_url="memory://t"),
    )
    timeout = client._timeout()
    assert timeout.read == 300.0
    assert timeout.write == 300.0
    # Connect must NOT scale with the read ceiling: raising the timeout for a long
    # generation would otherwise let an unreachable endpoint hang just as long, and connect
    # failures are the class this layer still retries.
    assert timeout.connect == DEFAULT_CONNECT_TIMEOUT_S


def test_model_timeout_must_be_positive() -> None:
    from felix.config import Settings
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(model_timeout_seconds=0, database_url="memory://t")


@pytest.mark.asyncio
async def test_connection_error_finally_raises() -> None:
    import httpx

    class _Dead:
        async def post(self, url, json=None, headers=None):
            raise httpx.ConnectError("boom")

    with pytest.raises(httpx.ConnectError):
        await _post_with_retry(_Dead(), "u", label="x", json={}, headers={}, max_retries=1)


def test_retry_after_seconds_is_honoured() -> None:
    assert _retry_after_seconds(_Resp(429, {"retry-after": "7"})) == 7.0
    assert _retry_after_seconds(_Resp(429, {})) is None
    assert _retry_after_seconds(_Resp(429, {"retry-after": "garbage"})) is None


def test_provider_delay_wins_over_backoff() -> None:
    assert _backoff_delay(0, 7.0) == 7.0


def test_backoff_grows_and_is_capped() -> None:
    assert _backoff_delay(0, None) < _backoff_delay(5, None)
    assert _backoff_delay(99, None) <= 30.0


def test_default_retry_budget_is_small() -> None:
    """Three attempts total — enough to ride out a blip, not enough to hang a run."""
    assert MODEL_MAX_RETRIES == 2


# --- the cached prefix ----------------------------------------------------------


def _agent(prelude: str = "") -> object:
    from felix.patterns.react import build_react_agent

    return build_react_agent(
        {
            "tools": [],
            "manifest_id": "m",
            "system_prompt": "You are Felix.",
            "context_prelude": prelude,
            "recursion_limit": 3,
        }
    )


def test_recalled_facts_do_not_enter_the_system_prompt() -> None:
    """They used to be appended, so every memory write moved the cached prefix."""
    agent = _agent("<known_facts>- a fact</known_facts>")
    assert agent.system_prompt == "You are Felix."
    assert "known_facts" not in agent.system_prompt


def test_prelude_is_user_role_reference_material() -> None:
    msgs = _agent("<known_facts>- a fact</known_facts>")._prelude_messages()
    assert len(msgs) == 1
    assert msgs[0].role == "user", "facts must not be developer-tier instructions"
    assert "a fact" in msgs[0].content


def test_no_prelude_when_memory_is_empty() -> None:
    assert _agent("")._prelude_messages() == []


def test_system_prompt_is_identical_across_differing_preludes() -> None:
    """The point of the change: the cached prefix is stable while facts churn."""
    a = _agent("<known_facts>- one</known_facts>")
    b = _agent("<known_facts>- one\n- two</known_facts>")
    assert a.system_prompt == b.system_prompt


# --- the prelude has to survive assembly, not just be built ---------------------
#
# Every test above checks that the block is *constructed* correctly. None checked
# that it reaches the model, and it did not: a session strategy builds a fresh list
# from the session log and returns that, rather than extending the one it was handed,
# so the prelude was discarded on every turn that had a thread.


def _threaded_agent(prelude: str):
    from felix.patterns.react import build_react_agent
    from felix.session.store import InMemorySessionStore
    from felix.session.strategies import get_session_strategy

    return build_react_agent(
        {
            "tools": [],
            "manifest_id": "m",
            "system_prompt": "You are Felix.",
            "context_prelude": prelude,
            "recursion_limit": 3,
            "session_store": InMemorySessionStore(tenant_id="default"),
            "session_strategy": get_session_strategy("full_replay"),
        }
    )


async def _assemble(agent, *, thread_id: str | None):
    from felix.patterns.types import ChatMessage, InvokeInput

    return await agent._assemble_messages(
        InvokeInput(messages=[ChatMessage(role="user", content="hi")], thread_id=thread_id),
        model=None,
        tenant_id="default",
    )


@pytest.mark.asyncio
async def test_prelude_survives_a_threaded_session_render() -> None:
    """The regression: render replaces the list, so the prelude must be re-applied."""
    messages = await _assemble(
        _threaded_agent("<known_facts>- the sky is green</known_facts>"), thread_id="t1"
    )
    assert any("the sky is green" in (m.content or "") for m in messages), (
        "recalled facts were dropped by the session render"
    )


@pytest.mark.asyncio
async def test_prelude_reaches_the_model_without_a_thread_too() -> None:
    messages = await _assemble(
        _threaded_agent("<known_facts>- the sky is green</known_facts>"), thread_id=None
    )
    assert any("the sky is green" in (m.content or "") for m in messages)


@pytest.mark.asyncio
async def test_prelude_sits_after_the_system_prompt_not_at_the_tail() -> None:
    """Framing, not the user's latest turn — and never inside the system block."""
    messages = await _assemble(
        _threaded_agent("<known_facts>- the sky is green</known_facts>"), thread_id="t1"
    )
    found = [i for i, m in enumerate(messages) if "the sky is green" in (m.content or "")]
    assert found, "recalled facts were dropped by the session render"
    idx = found[0]
    assert messages[0].role == "system"
    assert "the sky is green" not in (messages[0].content or "")
    assert idx == 1, "prelude belongs directly after the system prompt"
    assert messages[idx].role == "user"
    assert messages[-1].content == "hi", "the user's own turn stays last"


@pytest.mark.asyncio
async def test_no_phantom_prelude_message_when_memory_is_empty() -> None:
    messages = await _assemble(_threaded_agent(""), thread_id="t1")
    assert [m.role for m in messages] == ["system", "user"]


def test_backoff_does_not_block_the_loop() -> None:
    """Sanity: the retry sleep is awaited, not time.sleep()."""
    import inspect

    from felix.patterns import model

    src = inspect.getsource(model._post_with_retry)
    assert "await asyncio.sleep" in src
    assert "time.sleep" not in src


@pytest.mark.asyncio
async def test_retry_is_actually_awaited() -> None:
    c = _Client([429, 200])
    task = asyncio.create_task(
        _post_with_retry(c, "u", label="anthropic", json={}, headers={}, max_retries=1)
    )
    # If the sleep blocked the loop this would never get a chance to run.
    await asyncio.sleep(0)
    assert not task.done()
    assert (await task).status_code == 200
