"""Two small hardening fixes in the model and workspace layers.

`_post_with_retry` treated every 429 as backpressure. A spent quota or a billing failure
also returns 429 and will not clear inside a request, so retrying it just adds the full
backoff ladder to a failure the caller sees anyway.

`_write_file` had no interlock while `spec.tool_execution: parallel` runs a batch under
`asyncio.gather`, so two calls in one batch could interleave on one file.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from felix.tools.workspace import _write_lock, resolve_under_root
from felix_ai.wire.transport import _is_exhausted_quota
from felix_ai.wire.transport import post_with_retry as _post_with_retry


class _Resp:
    def __init__(self, status: int, text: str = "", headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self.text = text
        self.headers = headers or {}


class _Client:
    def __init__(self, responses: list[_Resp]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def post(self, url: str, json=None, headers=None) -> _Resp:
        self.calls += 1
        return self._responses.pop(0) if self._responses else _Resp(200)


# --- retry classification -------------------------------------------------------


def test_quota_exhaustion_is_recognised() -> None:
    assert _is_exhausted_quota(_Resp(429, '{"error":{"code":"insufficient_quota"}}'))
    assert _is_exhausted_quota(_Resp(429, "You exceeded your current quota"))
    assert _is_exhausted_quota(_Resp(429, "Monthly usage limit reached"))


def test_transient_overload_is_not_mistaken_for_quota() -> None:
    assert not _is_exhausted_quota(_Resp(429, '{"error":{"type":"overloaded_error"}}'))
    assert not _is_exhausted_quota(_Resp(429, "rate_limit_error: slow down"))


def test_response_without_a_body_is_treated_as_retryable() -> None:
    """A body we cannot read is not evidence of a spent quota."""

    class _NoText:
        status_code = 429
        headers: dict[str, str] = {}

        @property
        def text(self) -> str:
            raise RuntimeError("stream consumed")

    assert not _is_exhausted_quota(_NoText())


@pytest.mark.asyncio
async def test_spent_quota_is_not_retried() -> None:
    c = _Client([_Resp(429, "insufficient_quota"), _Resp(200)])
    resp = await _post_with_retry(c, "u", label="openai", json={}, headers={}, max_retries=2)
    assert c.calls == 1, "a spent quota will not clear inside this request"
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_transient_rate_limit_still_retries() -> None:
    c = _Client([_Resp(429, "overloaded_error"), _Resp(200)])
    resp = await _post_with_retry(c, "u", label="anthropic", json={}, headers={}, max_retries=2)
    assert c.calls == 2
    assert resp.status_code == 200


# --- workspace write races ------------------------------------------------------


def test_paths_that_resolve_together_share_one_lock(tmp_path: Path) -> None:
    """Two spellings of one file must serialize against each other, not run free."""
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_text("")
    direct = resolve_under_root(tmp_path, "sub/a.txt")
    indirect = resolve_under_root(tmp_path, "./sub/../sub/a.txt")
    assert direct == indirect
    assert _write_lock(direct) is _write_lock(indirect)


def test_distinct_files_do_not_share_a_lock(tmp_path: Path) -> None:
    a = resolve_under_root(tmp_path, "a.txt")
    b = resolve_under_root(tmp_path, "b.txt")
    assert _write_lock(a) is not _write_lock(b)


@pytest.mark.asyncio
async def test_lock_serializes_overlapping_writers(tmp_path: Path) -> None:
    """Without the lock the two bodies interleave and the file ends up mixed."""
    target = resolve_under_root(tmp_path, "log.txt")
    order: list[str] = []

    async def writer(tag: str) -> None:
        async with _write_lock(target):
            order.append(f"{tag}-start")
            await asyncio.sleep(0)  # yield; an unlocked writer would slip in here
            order.append(f"{tag}-end")

    await asyncio.gather(writer("a"), writer("b"))
    assert order in (
        ["a-start", "a-end", "b-start", "b-end"],
        ["b-start", "b-end", "a-start", "a-end"],
    ), f"writers interleaved: {order}"
