"""Content screening remembers verdicts briefly, and screens a long text's windows concurrently."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from felix.config import Settings
from felix.governance import screening
from felix.governance.screening import SCREEN_CHARS, SCREEN_OVERLAP, ScreenResult

SETTINGS = Settings(database_url="memory://screening-cache")


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The model screener, scripted: "BAD" anywhere in a window flags it, "DOWN" is an outage."""
    seen: list[str] = []

    async def fake(settings: Any, text: str, model_id: str) -> ScreenResult:
        seen.append(text)
        if "DOWN" in text:
            return ScreenResult(available=False, reason="screener_unavailable")
        return ScreenResult(score=0.95 if "BAD" in text else 0.05)

    monkeypatch.setattr(screening, "_model_screen", fake)
    return seen


async def test_the_same_text_is_screened_once(calls: list[str]) -> None:
    """A resent conversation: every earlier user turn comes back on every request."""
    for _ in range(3):
        result = await screening.screen_for_injection(SETTINGS, "what is our refund policy?", "m")
        assert result.score == 0.05
    assert len(calls) == 1


async def test_an_outage_is_never_remembered(calls: list[str]) -> None:
    for _ in range(2):
        assert (await screening.screen_for_injection(SETTINGS, "DOWN", "m")).unavailable
    assert len(calls) == 2


async def test_a_verdict_is_per_screener(calls: list[str]) -> None:
    await screening.screen_for_injection(SETTINGS, "same text", "model-a")
    await screening.screen_for_injection(SETTINGS, "same text", "model-b")
    assert len(calls) == 2


async def test_a_flagged_verdict_is_remembered_as_flagged(calls: list[str]) -> None:
    for _ in range(2):
        assert (await screening.screen_for_injection(SETTINGS, "BAD", "m")).flagged
    assert len(calls) == 1


def _windows(*marks: str) -> str:
    """A text whose n-th window (and only it, overlaps aside) carries `marks[n]`."""
    step = SCREEN_CHARS - SCREEN_OVERLAP
    text = list("." * (step * len(marks) + SCREEN_OVERLAP))
    for i, mark in enumerate(marks):
        at = i * step + SCREEN_OVERLAP + 10
        text[at : at + len(mark)] = mark
    return "".join(text)


async def test_windows_are_screened_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each window waits until two are in flight; screened one at a time, this deadlocks."""
    two_in_flight = asyncio.Barrier(2)

    async def fake(settings: Any, text: str, model_id: str) -> ScreenResult:
        await two_in_flight.wait()
        return ScreenResult(score=0.0)

    monkeypatch.setattr(screening, "_model_screen", fake)
    result = await asyncio.wait_for(screening.screen_chunks(SETTINGS, _windows("a", "b"), "m"), timeout=2)
    assert result.score == 0.0


async def test_the_first_window_in_order_decides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent, but not first-to-finish: an outage in window one outranks a flag in window
    three that answered sooner, exactly as the sequential loop ordered them."""

    async def fake(settings: Any, text: str, model_id: str) -> ScreenResult:
        if "DOWN" in text:
            await asyncio.sleep(0.05)
            return ScreenResult(available=False, reason="screener_unavailable")
        return ScreenResult(score=0.95 if "BAD" in text else 0.05)

    monkeypatch.setattr(screening, "_model_screen", fake)
    result = await screening.screen_chunks(SETTINGS, _windows("DOWN", "ok", "BAD"), "m")
    assert result.unavailable and result.reason == "screener_unavailable"


async def test_window_concurrency_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    in_flight = peak = 0

    async def fake(settings: Any, text: str, model_id: str) -> ScreenResult:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return ScreenResult(score=0.0)

    monkeypatch.setattr(screening, "_model_screen", fake)
    marks = [f"w{i}" for i in range(screening.MAX_SCREEN_CHUNKS)]
    await screening.screen_chunks(SETTINGS, _windows(*marks), "m")
    assert peak == screening.SCREEN_CONCURRENCY


async def test_a_verdict_is_per_tenant(calls: list[str]) -> None:
    from felix.context import AuthContext, RequestContext, async_run_with_context

    for tenant in ("acme", "globex"):
        ctx = RequestContext(settings=SETTINGS, auth=AuthContext(tenant_id=tenant, scopes=frozenset()))
        async with async_run_with_context(ctx):
            await screening.screen_for_injection(SETTINGS, "same text", "m")
    assert len(calls) == 2


async def test_a_manifest_with_a_decider_is_not_answered_by_one_without(
    monkeypatch: pytest.MonkeyPatch, calls: list[str]
) -> None:
    """The decider is part of what a verdict depends on: a clean verdict from the model alone must
    not stand in for one the decider would have flagged."""
    from types import SimpleNamespace

    asked: list[str] = []

    async def flagging_decider(decider: Any, text: str) -> ScreenResult:
        asked.append(text)
        return ScreenResult(score=0.99)

    monkeypatch.setattr(screening, "_decider_screen", flagging_decider)
    decider: Any = SimpleNamespace(model_id="jev")
    assert not (await screening.screen_for_injection(SETTINGS, "subtle text", "m")).flagged
    assert (await screening.screen_for_injection(SETTINGS, "subtle text", "m", decider)).flagged
    assert asked == ["subtle text"]


async def test_a_verdict_lapses_after_its_ttl(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    import time
    from types import SimpleNamespace

    from felix import bounded_cache

    await screening.screen_for_injection(SETTINGS, "text", "m")
    later = time.monotonic() + screening.VERDICT_TTL_S + 1
    monkeypatch.setattr(bounded_cache, "time", SimpleNamespace(monotonic=lambda: later))
    await screening.screen_for_injection(SETTINGS, "text", "m")
    assert len(calls) == 2
