"""One contract, run against every decision provider.

The counterpart of `test_model_provider.py` for `felix_ai.decide`, and for the same reason:
the chain a real decider travels — registration → `FELIX_DECISION_ROUTES` → `build_decider`
→ a decision → `record_usage` — is where a provider that satisfies the Protocol can still
land in the unmetered path, and no unit test of one link sees that.

No arm needs infrastructure: the HTTP arms run against a fake transport, `scripted` and
`llm` need none. A skip in this file is a bug.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from felix_ai.decide import Choice, ChoiceAnswer, Noul, NoulAnswer, Score, ScoreAnswer
from felix_ai.types import TokenUsage
from felix_ai.wire.transport import ModelGatewayError

ARMS = ["scripted", "typesafe", "workers_ai", "llm"]
parametrized = pytest.mark.parametrize("arm", ARMS, indirect=True)

INPUT_TOKENS = 1_000_000
QUESTIONS = {
    "pick": Choice("Which letter?", {"a": "the first", "b": "the second"}),
    "sure": Noul("The state mentions a letter."),
    "level": Score("How emphatic?", ("calm", "firm", "shouting")),
}


class _Resp:
    def __init__(self, status: int, payload: Any = None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload)
        self.headers: dict[str, str] = {}

    def json(self) -> Any:
        return self._payload


class _Transport:
    """Stands in for `httpx.AsyncClient`, recording what the provider sent."""

    def __init__(self) -> None:
        self.responses: list[_Resp] = []
        self.urls: list[str] = []
        self.sent: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.timeouts: list[Any] = []

    def __call__(self, *a: Any, **kw: Any) -> _Transport:
        self.timeouts.append(kw.get("timeout"))
        return self

    async def __aenter__(self) -> _Transport:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    async def post(self, url: str, json: Any = None, headers: Any = None) -> _Resp:
        self.urls.append(url)
        self.sent.append(json or {})
        self.headers.append(dict(headers or {}))
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def _jev_body(pick: str = "b", *, missing: str = "") -> dict[str, Any]:
    answers = {
        "pick": {"type": "choice", "choice": pick, "confidence": 0.9, "probabilities": {"a": 0.1, "b": 0.9}},
        "sure": {"type": "noul", "noul": 0.8},
        "level": {"type": "score", "score": 1.0, "confidence": 0.7, "probabilities": {"0": 0.1, "1": 0.8}},
    }
    answers.pop(missing, None)
    return {
        "model": "jev-1.13.0",
        "answers": answers,
        "usage": {"input_tokens": INPUT_TOKENS, "output_tokens": 9},
    }


class _Arm:
    def __init__(self, name: str, settings: Any, transport: _Transport | None) -> None:
        self.name = name
        self.settings = settings
        self.transport = transport

    def program(self, pick: str = "b", *, missing: str = "") -> None:
        """Make the next decision answer `pick`, optionally leaving one question out."""
        if self.name in ("typesafe", "workers_ai"):
            assert self.transport is not None
            body = _jev_body(pick, missing=missing)
            if self.name == "workers_ai":
                body = {"result": body, "success": True, "errors": [], "messages": []}
            self.transport.responses = [_Resp(200, body)]
        elif self.name == "scripted":
            from felix_ai.decide.scripted import register_scripted_decider

            answers: dict[str, Any] = {
                "pick": ChoiceAnswer(pick, {"a": 0.1, "b": 0.9}, 0.9),
                "sure": NoulAnswer(0.8),
                "level": ScoreAnswer(1.0, {0: 0.1, 1: 0.8}, 0.7),
            }
            answers.pop(missing, None)
            # The fake reports what it was given; checking completeness is the harness's job.
            register_scripted_decider(
                "scripted", lambda _s, _q: answers, usage=TokenUsage(input=INPUT_TOKENS, output=1)
            )
        else:
            from felix_ai.providers.scripted import ScriptedTurn, register_scripted_provider

            reply = {"pick": pick, "sure": 0.8, "level": 1}
            reply.pop(missing, None)
            register_scripted_provider(
                "scripted-chat",
                [ScriptedTurn(content=json.dumps(reply), usage=TokenUsage(input=INPUT_TOKENS, output=5))],
            )

    def build(self) -> Any:
        from felix.decisions import build_decider

        return build_decider(self.settings, "d")


_ROUTES = {
    "scripted": {"provider": "scripted", "model": "jev-latest"},
    "typesafe": {"provider": "typesafe", "model": "jev-latest"},
    "workers_ai": {"provider": "workers_ai", "model": "typesafe/jev"},
    "llm": {"provider": "llm", "model": "cheap"},
}


@pytest.fixture
def arm(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Any:
    import httpx
    from felix.config import Settings
    from felix.decisions import register_builtin_deciders
    from felix.patterns.model import register_builtin_providers
    from felix_ai.decide import reset_decision_provider_registry
    from felix_ai.registry import reset_model_provider_registry

    name = request.param
    transport = None
    if name in ("typesafe", "workers_ai"):
        transport = _Transport()
        monkeypatch.setattr(httpx, "AsyncClient", transport)
    settings = Settings(
        database_url="memory://conformance-decider",
        object_store="memory",
        decision_routes=json.dumps({"d": _ROUTES[name]}),
        model_routes='{"cheap":{"provider":"scripted-chat","model":"claude-haiku-4-5"}}',
        model_provider_options=json.dumps(
            {
                "typesafe": {"api_key": "ts"},
                "workers_ai": {"api_key": "cf", "account_id": "acct"},
            }
        ),
    )
    yield _Arm(name, settings, transport)
    reset_decision_provider_registry()
    register_builtin_deciders()
    reset_model_provider_registry()
    register_builtin_providers()


def _request_ctx(settings: Any) -> Any:
    from felix.context import AuthContext, RequestContext

    return RequestContext(settings=settings, auth=AuthContext(), manifest_id="m")


async def _decide_in_request(decider: Any, settings: Any, ctx: Any = None) -> tuple[Any, Any]:
    """Decide inside a request context. Pass `ctx` to read it back after a raise."""
    from felix.context import async_run_with_context

    ctx = ctx or _request_ctx(settings)
    async with async_run_with_context(ctx):
        result = await decider.decide({"text": "B! Definitely b."}, QUESTIONS, purpose="conformance")
    return result, ctx


# --- the contract -----------------------------------------------------------------------


@parametrized
@pytest.mark.asyncio
async def test_a_decision_answers_every_question_with_its_type(arm: _Arm) -> None:
    arm.program("b")
    result, _ctx = await _decide_in_request(arm.build(), arm.settings)
    pick, sure, level = result.answers["pick"], result.answers["sure"], result.answers["level"]
    assert isinstance(pick, ChoiceAnswer) and pick.choice == "b"
    assert isinstance(sure, NoulAnswer) and sure.p == pytest.approx(0.8)
    assert isinstance(level, ScoreAnswer) and level.score == pytest.approx(1.0)
    # Calibrated, or honestly absent — never a number the provider did not give.
    assert pick.confidence is None or 0.0 <= pick.confidence <= 1.0
    if arm.name == "llm":
        assert pick.confidence is None


@parametrized
@pytest.mark.asyncio
async def test_a_decision_is_metered_and_priced_against_the_run(arm: _Arm) -> None:
    """The chain end to end: spend lands on `ctx.limit_state`, or `max_cost_usd` fails open."""
    arm.program("b")
    from felix_ai.catalog import entry_for

    decider = arm.build()
    _result, ctx = await _decide_in_request(decider, arm.settings)
    assert ctx.limit_state.tokens_input == INPUT_TOKENS
    pricing = entry_for(decider.wire_model).pricing
    assert pricing is not None, f"{decider.wire_model} must be priced, or max_cost_usd fails open"
    # A million input tokens costs exactly the input rate; output is a rounding error at most.
    assert ctx.limit_state.cost_usd == pytest.approx(pricing.input, rel=1e-3)


@parametrized
@pytest.mark.asyncio
async def test_a_choice_outside_the_offered_options_is_refused(arm: _Arm) -> None:
    arm.program("z")
    ctx = _request_ctx(arm.settings)
    with pytest.raises(ValueError, match="z"):
        await _decide_in_request(arm.build(), arm.settings, ctx)
    if arm.name == "scripted":
        # The one arm whose backend does not check its own answers, so the refusal came from
        # the harness boundary after the call — and a call that answered was paid for.
        assert ctx.limit_state.tokens_input == INPUT_TOKENS


@parametrized
@pytest.mark.asyncio
async def test_an_unanswered_question_is_refused(arm: _Arm) -> None:
    arm.program("b", missing="sure")
    with pytest.raises(ValueError, match="sure"):
        await _decide_in_request(arm.build(), arm.settings)


@pytest.mark.parametrize("arm", ["llm"], indirect=True)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    ["sure, b", "[1, 2]", '{"pick": "b", "sure": 0.8, "level": 7}'],
    ids=["prose", "not-an-object", "score-out-of-range"],
)
async def test_a_chat_model_reply_that_is_not_a_decision_is_refused(arm: _Arm, reply: str) -> None:
    from felix_ai.providers.scripted import ScriptedTurn, register_scripted_provider

    register_scripted_provider(
        "scripted-chat", [ScriptedTurn(content=reply, usage=TokenUsage(input=10, output=1))]
    )
    with pytest.raises(ValueError):
        await _decide_in_request(arm.build(), arm.settings)


# --- the two Jev endpoints --------------------------------------------------------------


@pytest.mark.parametrize("arm", ["typesafe"], indirect=True)
@pytest.mark.asyncio
async def test_typesafe_sends_the_documented_request(arm: _Arm) -> None:
    arm.program("b")
    await _decide_in_request(arm.build(), arm.settings)
    assert arm.transport is not None
    assert arm.transport.urls == ["https://api.typesafe.ai/v1/systemone"]
    body = arm.transport.sent[0]
    assert body["model"] == "jev-latest"
    assert body["state"] == {"text": "B! Definitely b."}
    assert body["questions"]["pick"] == {
        "type": "choice",
        "instructions": "Which letter?",
        "criteria": {"a": "the first", "b": "the second"},
    }
    assert body["questions"]["level"]["criteria"] == ["calm", "firm", "shouting"]
    assert body["questions"]["sure"] == {"type": "noul", "instructions": "The state mentions a letter."}
    assert arm.transport.headers[0]["Authorization"] == "Bearer ts"


@pytest.mark.parametrize("arm", ["workers_ai"], indirect=True)
@pytest.mark.asyncio
async def test_workers_ai_nests_the_input_and_unwraps_the_envelope(arm: _Arm) -> None:
    arm.program("b")
    result, _ctx = await _decide_in_request(arm.build(), arm.settings)
    assert arm.transport is not None
    assert arm.transport.urls == ["https://api.cloudflare.com/client/v4/accounts/acct/ai/run"]
    body = arm.transport.sent[0]
    assert body["model"] == "typesafe/jev"
    assert set(body["input"]) == {"state", "questions"}
    assert arm.transport.headers[0]["Authorization"] == "Bearer cf"
    assert result.model == "jev-1.13.0"


@pytest.mark.parametrize("arm", ["workers_ai"], indirect=True)
@pytest.mark.asyncio
async def test_a_cloudflare_failure_envelope_is_an_error_not_an_empty_answer(arm: _Arm) -> None:
    assert arm.transport is not None
    arm.transport.responses = [_Resp(200, {"result": {}, "success": False, "errors": [{"code": 7000}]})]
    with pytest.raises(ValueError):
        await _decide_in_request(arm.build(), arm.settings)


@pytest.mark.parametrize("arm", ["typesafe", "workers_ai"], indirect=True)
@pytest.mark.asyncio
async def test_an_http_error_raises_the_gateway_error(arm: _Arm) -> None:
    assert arm.transport is not None
    arm.transport.responses = [_Resp(401, {"error": "bad key"})]
    with pytest.raises(ModelGatewayError) as exc:
        await _decide_in_request(arm.build(), arm.settings)
    assert exc.value.status == 401
    assert "bad key" not in str(exc.value), "the upstream body stays out of the relayed message"


@pytest.mark.parametrize("arm", ["typesafe"], indirect=True)
@pytest.mark.asyncio
async def test_overloaded_is_retried(arm: _Arm, monkeypatch: pytest.MonkeyPatch) -> None:
    """TypeSafe documents 529 as retry-after-a-moment; it used to fail the call outright."""
    import asyncio

    async def _no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    assert arm.transport is not None
    arm.transport.responses = [_Resp(529, {}), _Resp(200, _jev_body("b"))]
    result, _ctx = await _decide_in_request(arm.build(), arm.settings)
    assert result.answers["pick"].choice == "b"
    assert len(arm.transport.urls) == 2


@pytest.mark.parametrize("arm", ["typesafe"], indirect=True)
@pytest.mark.asyncio
async def test_persistent_overload_ends_in_the_gateway_error(
    arm: _Arm, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    async def _no_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    assert arm.transport is not None
    arm.transport.responses = [_Resp(529, {})]
    with pytest.raises(ModelGatewayError) as exc:
        await _decide_in_request(arm.build(), arm.settings)
    assert exc.value.status == 529
    assert len(arm.transport.urls) == 3, "three attempts, then give up"


@pytest.mark.parametrize("arm", ["typesafe"], indirect=True)
@pytest.mark.asyncio
async def test_a_decision_does_not_wait_out_the_generation_timeout(arm: _Arm) -> None:
    """During an outage every turn waits this long before falling back, so it is the
    decision budget (15s) rather than the 120s one generation needs."""
    from felix.config import Settings
    from felix.decisions import build_decider

    arm.program("b")
    await _decide_in_request(arm.build(), arm.settings)
    assert arm.settings.model_timeout_seconds > 15
    assert arm.transport is not None and arm.transport.timeouts[-1].read == 15.0

    tuned = Settings(
        database_url="memory://conformance-decider",
        object_store="memory",
        decision_routes=arm.settings.decision_routes,
        model_provider_options='{"typesafe": {"api_key": "ts", "timeout_seconds": "3"}}',
    )
    arm.program("b")
    await _decide_in_request(build_decider(tuned, "d"), tuned)
    assert arm.transport.timeouts[-1].read == 3.0


# --- registration -----------------------------------------------------------------------


def test_the_scripted_decider_is_not_registered_by_default() -> None:
    from felix.decisions import list_decision_providers

    assert "scripted" not in list_decision_providers()
    assert {"typesafe", "workers_ai", "llm"} <= set(list_decision_providers())


def test_an_unknown_decision_provider_fails_at_startup() -> None:
    from felix.config import Settings

    settings = Settings(
        host="127.0.0.1",
        decision_routes='{"d":{"provider":"typosafe","model":"jev-latest"}}',
    )
    # Through the boot path, not the helper: a deleted call in `validate_runtime` must fail here.
    with pytest.raises(RuntimeError, match="typosafe"):
        settings.validate_runtime()


def test_an_unrouted_decider_id_is_an_error_not_a_silent_default() -> None:
    from felix.config import Settings
    from felix.decisions import build_decider

    settings = Settings(database_url="memory://conformance-decider", object_store="memory")
    with pytest.raises(ValueError, match="nope"):
        build_decider(settings, "nope")


def test_limits_are_checked_before_the_request() -> None:
    with pytest.raises(ValueError, match="255"):
        Choice("too many", {f"t{i}": None for i in range(256)})
    with pytest.raises(ValueError, match="levels"):
        Score("one level", ("only",))
