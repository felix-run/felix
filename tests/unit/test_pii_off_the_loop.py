"""PII redaction that may reach Presidio runs off the event loop, on one dedicated thread."""

from __future__ import annotations

import threading
from typing import Any

import pytest
from felix.governance import pii
from felix.governance.pii import PiiResult


@pytest.fixture
def threads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`redact_pii`, recording which thread ran it."""
    seen: list[str] = []

    def fake(text: str) -> PiiResult:
        seen.append(threading.current_thread().name)
        return PiiResult(
            matched="555-12-3456" in text, text=text.replace("555-12-3456", "[R]"), engine="presidio"
        )

    monkeypatch.setattr(pii, "redact_pii", fake)
    return seen


async def test_a_text_that_may_reach_presidio_is_redacted_off_the_loop(
    monkeypatch: pytest.MonkeyPatch, threads: list[str]
) -> None:
    monkeypatch.setattr(pii, "_presidio_checked", False)
    result = await pii.redact_pii_async("ssn 555-12-3456")
    assert result.text == "ssn [R]"
    assert len(threads) == 1 and threads[0].startswith("felix-pii")


async def test_a_process_known_to_be_regex_only_stays_inline(
    monkeypatch: pytest.MonkeyPatch, threads: list[str]
) -> None:
    monkeypatch.setattr(pii, "_presidio_checked", True)
    monkeypatch.setattr(pii, "_analyzer", None)
    await pii.redact_pii_async("ssn 555-12-3456")
    assert threads == [threading.current_thread().name]


async def test_the_tool_output_guardrail_redacts_off_the_loop(
    monkeypatch: pytest.MonkeyPatch, threads: list[str]
) -> None:
    """Wiring: the guardrail wrapper goes through the async path, not `redact_pii` inline."""
    from felix.manifests.builder import apply_guardrails
    from felix.manifests.schema import Guardrails
    from felix.tools.types import Tool, ToolInvocationCtx

    monkeypatch.setattr(pii, "_presidio_checked", False)

    class _Leaky:
        transport = "local"

        async def execute(self, args: Any, ctx: ToolInvocationCtx | None = None) -> str:
            return "customer ssn 555-12-3456"

    tool = Tool(name="lookup", description="d", args_schema=None, executor=_Leaky())
    wrapped = apply_guardrails([tool], Guardrails(providers=["pii"]), "m")[0]
    out = await wrapped.executor.execute({}, ToolInvocationCtx(manifest_id="m"))
    assert "555-12-3456" not in str(out)
    assert threads and all(name.startswith("felix-pii") for name in threads)


def _pii_manifest() -> Any:
    from felix.manifests.loader import parse_manifest

    return parse_manifest(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "s"},
            "spec": {"guardrails": {"providers": ["pii"], "targets": ["input"], "block_on_match": False}},
        }
    )


async def test_an_inbound_turn_is_redacted_off_the_loop(
    monkeypatch: pytest.MonkeyPatch, threads: list[str]
) -> None:
    from felix.config import Settings
    from felix.governance.inbound import apply_inbound_screening
    from felix.patterns.types import ChatMessage

    monkeypatch.setattr(pii, "_presidio_checked", False)
    out = await apply_inbound_screening(
        _pii_manifest(), [ChatMessage(role="user", content="ssn 555-12-3456")], Settings(allow_insecure=True)
    )
    assert out[0].content == "ssn [R]"
    assert threads and all(name.startswith("felix-pii") for name in threads)


async def test_tool_arguments_are_redacted_off_the_loop(
    monkeypatch: pytest.MonkeyPatch, threads: list[str]
) -> None:
    from felix.config import Settings
    from felix.governance.tool_screening import screen_tool_arguments

    monkeypatch.setattr(pii, "_presidio_checked", False)
    out = await screen_tool_arguments(
        _pii_manifest(), {"note": "ssn 555-12-3456", "nested": ["555-12-3456"]}, Settings(allow_insecure=True)
    )
    assert out == {"note": "ssn [R]", "nested": ["[R]"]}
    assert threads and all(name.startswith("felix-pii") for name in threads)


async def test_presidio_is_never_analysed_from_two_threads_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent callers queue for the one PII thread rather than analysing side by side."""
    import asyncio
    import time

    monkeypatch.setattr(pii, "_presidio_checked", False)
    in_flight = peak = 0
    guard = threading.Lock()

    def slow(text: str) -> PiiResult:
        nonlocal in_flight, peak
        with guard:
            in_flight += 1
            peak = max(peak, in_flight)
        time.sleep(0.01)
        with guard:
            in_flight -= 1
        return PiiResult(matched=False, text=text, engine="presidio")

    monkeypatch.setattr(pii, "redact_pii", slow)
    await asyncio.gather(*(pii.redact_pii_async(f"text {i}") for i in range(6)))
    assert peak == 1
