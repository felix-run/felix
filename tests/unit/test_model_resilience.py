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
from felix.patterns.model import (
    MODEL_MAX_RETRIES,
    _backoff_delay,
    _post_with_retry,
    _retry_after_seconds,
)


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
