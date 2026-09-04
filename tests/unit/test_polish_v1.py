"""Polish: JWKS, judges, schedule, file secrets, session notices."""

from __future__ import annotations

import pytest
from felix.auth.jwt import public_jwks
from felix.governance.judges import heuristic_judge_score
from felix.jobs.scheduler import next_run_at_ms
from felix.manifests.builder import apply_judges
from felix.manifests.schema import Guardrails, JudgeRule
from felix.patterns.types import ChatMessage
from felix.secrets import FileSecrets
from felix.session.strategies import SemanticSessionStrategy, SummarizingSessionStrategy
from felix.session.types import SessionEvent
from felix.tools.types import define_tool, is_wrapper_deny, tool_output_content


def test_next_run_at_ms_parsers() -> None:
    base = 1_000_000
    assert next_run_at_ms("", base) == base + 60_000
    assert next_run_at_ms("30", base) == base + 30_000
    assert next_run_at_ms("every:5m", base) == base + 5 * 60_000
    assert next_run_at_ms("@every 2s", base) == base + 2_000
    assert next_run_at_ms("*/10 * * * *", base) == base + 10 * 60_000


def test_heuristic_judge_score() -> None:
    assert heuristic_judge_score("hi", "nonempty") == 0.0
    assert heuristic_judge_score("hello world", "nonempty") == 1.0
    assert heuristic_judge_score("abcd", "min_chars:4") == 1.0
    assert heuristic_judge_score("ab", "min_chars:4") == 0.5
    assert heuristic_judge_score("ship the feature soon", "ship feature") == 1.0


@pytest.mark.asyncio
async def test_apply_judges_threshold() -> None:
    async def _echo(args: dict) -> str:
        return str(args.get("text") or "")

    tool = define_tool(name="echo", description="echo", handler=_echo)
    guardrails = Guardrails(
        judges=[
            JudgeRule(
                name="min",
                criteria="min_chars:5",
                threshold=1.0,
                target_tools=["echo"],
            )
        ]
    )
    wrapped = apply_judges([tool], guardrails, "m")[0]
    ok = await wrapped.executor.execute({"text": "hello!"})
    assert not is_wrapper_deny(ok)
    denied = await wrapped.executor.execute({"text": "hi"})
    assert is_wrapper_deny(denied)
    assert "judge denied" in tool_output_content(denied)


def test_public_jwks_from_json() -> None:
    doc = public_jwks('{"keys":[{"kty":"RSA","n":"x","e":"AQAB","kid":"k1"}]}')
    assert len(doc["keys"]) == 1
    assert doc["keys"][0]["kid"] == "k1"


@pytest.mark.asyncio
async def test_file_secrets_rejects_traversal(tmp_path) -> None:
    secret = tmp_path / "ok"
    secret.write_text("value-secret", encoding="utf-8")
    outside = tmp_path.parent / "outside-secret"
    outside.write_text("nope", encoding="utf-8")
    fs = FileSecrets(tmp_path)
    assert await fs.get("ok") == "value-secret"
    assert await fs.get("../outside-secret") is None
    assert await fs.get(str(outside)) is None


class _MemSession:
    def __init__(self, events: list[SessionEvent]) -> None:
        self._events = events

    async def get_events(self):
        return list(self._events)

    async def append(self, _event) -> None:
        return None


@pytest.mark.asyncio
async def test_summarizing_no_model_emits_notice() -> None:
    events = [
        SessionEvent(seq=i, ts=float(i), kind="message", role="user", content=f"msg-{i}") for i in range(6)
    ]
    session = _MemSession(events)
    strategy = SummarizingSessionStrategy(keep=2)
    out = await strategy.render(
        session,  # type: ignore[arg-type]
        [ChatMessage(role="user", content="now")],
        {"system_prompt": "sys", "model": None},
    )
    assert any("[session] summarizing unavailable" in m.content for m in out)


@pytest.mark.asyncio
async def test_semantic_emits_keyword_notice() -> None:
    events = [
        SessionEvent(seq=1, ts=1.0, kind="message", role="user", content="alpha beta"),
        SessionEvent(seq=2, ts=2.0, kind="message", role="assistant", content="gamma"),
    ]
    session = _MemSession(events)
    strategy = SemanticSessionStrategy(top_n=1)
    out = await strategy.render(
        session,  # type: ignore[arg-type]
        [ChatMessage(role="user", content="alpha")],
        {"system_prompt": "sys"},
    )
    assert any("keyword overlap" in m.content for m in out)
