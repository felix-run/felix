"""The two governance controls that nothing exercised.

Every `apply_*` in the wrapper stack was disabled in turn — replaced by `return tools`, the
shape a control takes when it is silently absent — and the whole suite re-run. Seven of the
nine were noticed by tests written for them. Two were not: `apply_secret_masking` and
`apply_policies` could each be reduced to a no-op with 1,561 tests green.

Neither was broken. Both were unverified, which is the state just before broken: the only
mention of either in the suite was `EXPECTED_WRAPPER_ORDER` in `test_invariants.py`, which
asserts the stack's *order* and never calls anything. A comment eight lines below that list
had already named the gap — "the same fail-open shape could ship in `apply_secret_masking`,
`apply_policies` or `apply_approvals` with both invariants below still green" — and then
nothing closed it.

These are the behavioural tests. Each was checked by disabling the control it covers and
confirming it goes red.
"""

from __future__ import annotations

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, run_with_context
from felix.manifests.builder import apply_policies, apply_secret_masking
from felix.manifests.schema import Policy
from felix.tools.types import (
    Tool,
    ToolInput,
    ToolInvocationCtx,
    ToolOutput,
    ToolOutputDict,
    tool_output_content,
)


class _Echo:
    """An executor that returns whatever it is told to, with a transport label."""

    transport = "builtin"

    def __init__(self, payload: ToolOutput) -> None:
        self.payload = payload
        self.calls = 0

    async def execute(self, args: ToolInput, ctx: ToolInvocationCtx | None = None) -> ToolOutput:
        self.calls += 1
        return self.payload


def _tool(payload: ToolOutput, name: str = "fetch") -> Tool:
    return Tool(name=name, description="d", args_schema=None, executor=_Echo(payload))


# --------------------------------------------------------------------------
# apply_secret_masking — the innermost wrapper. Everything downstream, including
# content screening, the audit log and the transcript, sees whatever it lets past.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_resolved_secret_is_redacted_from_tool_output() -> None:
    tool = apply_secret_masking([_tool("token is s3cret-value here")], ["s3cret-value"], "m")[0]

    out = await tool.executor.execute({})

    assert "s3cret-value" not in out, "the secret reached the caller"
    assert "[REDACTED]" in out


@pytest.mark.asyncio
async def test_every_configured_secret_is_redacted_not_just_the_first() -> None:
    """A loop that returns after the first match leaks the rest, and looks identical."""
    tool = apply_secret_masking([_tool("a=AAA b=BBB")], ["AAA", "BBB"], "m")[0]

    out = await tool.executor.execute({})

    assert "AAA" not in out and "BBB" not in out, f"a secret survived: {out}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "read"),
    [
        ("plain s3cret", lambda o: o),
        ({"content": "dict s3cret", "ok": True}, lambda o: o["content"]),
        (ToolOutputDict(content="typed s3cret"), lambda o: o.content),
    ],
    ids=["str", "dict", "ToolOutputDict"],
)
async def test_masking_handles_each_tool_output_shape(payload: ToolOutput, read) -> None:
    """`_replace_content` branches on the output type; a shape it does not handle leaks."""
    tool = apply_secret_masking([_tool(payload)], ["s3cret"], "m")[0]

    out = await tool.executor.execute({})

    assert "s3cret" not in read(out)
    if isinstance(payload, dict):
        assert out["ok"] is True, "masking dropped the rest of the output"


@pytest.mark.asyncio
async def test_masking_leaves_a_tool_alone_when_there_are_no_secrets() -> None:
    """The early return is the common path — every manifest without a `secret:` ref."""
    original = _tool("nothing to hide")

    assert apply_secret_masking([original], [], "m")[0] is original


@pytest.mark.asyncio
async def test_masking_survives_the_whole_governance_stack() -> None:
    """Pin the wiring, not just the function.

    The stack is ordered so masking is innermost, and each wrapper clones the tool with a new
    executor. A later wrapper that rebuilt the tool by hand — seven of them did — would carry
    the masking executor forward but reset other fields; one that replaced the executor
    outright would drop masking entirely and nothing here would notice unless the assertion
    runs against the *outermost* tool.
    """
    from felix.manifests.builder import apply_limits
    from felix.manifests.schema import Limits

    masked = apply_secret_masking([_tool("token s3cret")], ["s3cret"], "m")
    outermost = apply_limits(masked, Limits(), "m")[0]

    out = await outermost.executor.execute({})

    # Read through the harness's own accessor: a later wrapper may legitimately change the
    # output's shape, and asserting on the raw value would fail for that rather than for a leak.
    assert "s3cret" not in tool_output_content(out)


# --------------------------------------------------------------------------
# apply_policies — manifest `spec.policies`, the scope check on a named tool.
# --------------------------------------------------------------------------

POLICY = Policy(id="calc-scope", required_scopes=["tools:calc"], tools=["fetch"])


def _with_scopes(*scopes: str):
    """A request context carrying exactly these scopes, as the middleware would install it."""
    settings = Settings(database_url="memory://ci", object_store="memory")
    auth = AuthContext(principal_sub="s", tenant_id="t", scopes=frozenset(scopes), anonymous=False)
    return run_with_context(RequestContext(settings=settings, auth=auth, manifest_id="m"))


@pytest.mark.asyncio
async def test_a_policy_denies_a_tool_when_the_caller_lacks_the_scope() -> None:
    inner = _tool("side effect happened")
    tool = apply_policies([inner], [POLICY], "m")[0]

    with _with_scopes("tools:other"):
        out = await tool.executor.execute({})

    assert "policy denied" in str(out), f"the call was not denied: {out}"
    assert "tools:calc" in str(out), "the denial does not say which scope was missing"
    assert inner.executor.calls == 0, "the tool ran anyway — the deny is advisory"


@pytest.mark.asyncio
async def test_a_policy_allows_the_tool_when_the_scope_is_present() -> None:
    """A control that denies everything is as broken as one that denies nothing."""
    tool = apply_policies([_tool("ok")], [POLICY], "m")[0]

    with _with_scopes("tools:calc"):
        assert await tool.executor.execute({}) == "ok"


@pytest.mark.asyncio
async def test_a_policy_denies_when_there_is_no_request_context_at_all() -> None:
    """Fail closed. A background or durable run with no request context has no scopes, and
    "no scopes" must not read as "all scopes"."""
    inner = _tool("side effect happened")
    tool = apply_policies([inner], [POLICY], "m")[0]

    # Deliberately not inside run_with_context.
    out = await tool.executor.execute({})

    assert "policy denied" in str(out)
    assert inner.executor.calls == 0


@pytest.mark.asyncio
async def test_every_rule_on_a_tool_has_to_pass_not_just_one() -> None:
    """Two policies naming the same tool are an AND, or the second is decoration."""
    rules = [
        Policy(id="one", required_scopes=["a"], tools=["fetch"]),
        Policy(id="two", required_scopes=["b"], tools=["fetch"]),
    ]
    inner = _tool("side effect happened")
    tool = apply_policies([inner], rules, "m")[0]

    with _with_scopes("a"):
        out = await tool.executor.execute({})

    assert "policy denied" in str(out), "the second rule was never consulted"
    assert inner.executor.calls == 0


@pytest.mark.asyncio
async def test_a_tool_no_policy_names_is_left_alone() -> None:
    """Policies gate the tools they name. Gating everything would be a different control."""
    original = _tool("ok", name="unnamed")

    assert apply_policies([original], [POLICY], "m")[0] is original


@pytest.mark.asyncio
async def test_a_policied_tool_keeps_the_fields_it_was_declared_with() -> None:
    """The regression this file's audit turned up.

    `apply_policies` rebuilt the tool from eight of its ten fields, so `replay_safe` reset to
    `False` on every tool it wrapped. Seven wrappers did the same, `apply_limits` among them —
    and that one wraps every tool unconditionally, so no tool in any manifest ever kept the
    flag. `patterns/react.py` reads it to decide whether to tell the model an interrupted call
    is safe to retry; it had never seen a `True`.
    """
    declared = Tool(
        name="fetch",
        description="d",
        args_schema=None,
        executor=_Echo("ok"),
        replay_safe=True,
        fatal=True,
    )

    wrapped = apply_policies([declared], [POLICY], "m")[0]

    assert wrapped.replay_safe is True, "replay_safe was reset by the policy wrapper"
    assert wrapped.fatal is True


@pytest.mark.asyncio
async def test_a_declared_flag_survives_the_whole_compile() -> None:
    """The end-to-end version, against `build_agent` rather than one wrapper.

    This is the test that would have caught the defect at any point in its life. Five builtins
    declare `replay_safe=True`; `apply_limits` wraps every tool in every manifest and rebuilt
    each one from eight of its ten fields, so the flag was `False` on every tool the agent ever
    held. Asserting it on one wrapper's output would not have found that — the loss happened
    two wrappers later.
    """
    from felix.manifests.builder import build_agent
    from felix.tools.builtins import register_builtin_tools
    from felix.tools.provider import InMemoryToolProvider

    settings = Settings(
        database_url="memory://ci",
        object_store="memory",
        auth_mode="none",
        allow_insecure=True,
        host="127.0.0.1",
    )
    provider = InMemoryToolProvider()
    register_builtin_tools(provider)
    declared = provider.get("calculator")
    assert declared.replay_safe is True, "the builtin no longer declares replay_safe"

    agent = await build_agent(
        {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "m"},
            "spec": {
                "pattern": "react",
                "tools": ["calculator"],
                # Under a policy as well, so the tool goes through more of the stack than the
                # unconditional wrappers alone — `manifests/governed.yaml` ships this shape.
                "policies": [{"id": "p", "required_scopes": ["tools:calc"], "tools": ["calculator"]}],
            },
        },
        provider,
        settings=settings,
    )

    compiled = next(t for t in agent.tools if t.name == "calculator")
    assert compiled.replay_safe is True, (
        "replay_safe was lost somewhere in the governance stack; patterns/react.py reads it to "
        "decide whether to tell the model an interrupted call is safe to retry"
    )


# --------------------------------------------------------------------------
# A policy that requires nothing. The shape a security review found reachable:
# it validated, compiled, wrapped the tool, and permitted every caller.
# --------------------------------------------------------------------------


def test_a_policy_that_lists_tools_but_no_scopes_is_rejected() -> None:
    """`policies: [{id: finance-only, tools: [wire_transfer]}]` used to validate.

    `required_scopes` is the only enforcement `apply_policies` has, and an empty list makes
    its check vacuously true — so the tool was wrapped, looked governed in the compiled stack
    and in `felix validate-manifest`, and ran for anonymous callers.
    """
    with pytest.raises(ValueError, match="permits every caller"):
        Policy(id="finance-only", tools=["wire_transfer"])


def test_a_policy_that_names_no_tools_is_rejected() -> None:
    """The mirror case: `by_tool` stays empty, so no tool is ever wrapped."""
    with pytest.raises(ValueError, match="gates nothing"):
        Policy(id="orphan", required_scopes=["tools:calc"])


@pytest.mark.asyncio
async def test_a_scopeless_rule_reaching_the_wrapper_denies_rather_than_permits() -> None:
    """Defense in depth, for a `Policy` built in code rather than parsed.

    The schema rejects this shape now, so the only way here is constructing the model
    directly — which `model_construct` does, skipping validation the way an older parse path
    or a plugin might. `[s for s in [] if s not in scopes]` is empty, and an empty `missing`
    list reads as "every requirement satisfied" unless the wrapper says otherwise.
    """
    scopeless = Policy.model_construct(id="x", description="", required_scopes=[], tools=["fetch"])
    inner = _tool("side effect happened")
    tool = apply_policies([inner], [scopeless], "m")[0]

    with _with_scopes("anything"):
        out = await tool.executor.execute({})

    assert "policy denied" in str(out), f"a rule requiring nothing authorised the call: {out}"
    assert inner.executor.calls == 0


@pytest.mark.asyncio
async def test_a_str_scope_set_cannot_satisfy_a_policy_by_substring() -> None:
    """`s not in scopes` is a substring test when `scopes` is a `str`, not a set.

    `AuthContext.scopes` is an unvalidated dataclass field, and the plugin authenticator seam
    adopts whatever `Principal` a plugin returns — a plugin doing `" ".join(claims["scope"])`
    produces a `str`. Then `tools:calc` is "held" by a caller with only `tools:calculator`,
    and `admin` by one with `no-admin`. Total policy bypass on any prefix collision.
    """
    inner = _tool("side effect happened")
    tool = apply_policies([inner], [POLICY], "m")[0]

    settings = Settings(database_url="memory://ci", object_store="memory")
    auth = AuthContext(principal_sub="s", tenant_id="t", anonymous=False)
    auth.scopes = "reader tools:calculator"  # type: ignore[assignment]
    with run_with_context(RequestContext(settings=settings, auth=auth, manifest_id="m")):
        out = await tool.executor.execute({})

    assert "policy denied" in str(out), f"a substring satisfied the scope check: {out}"
    assert inner.executor.calls == 0


def test_a_policy_requiring_a_blank_scope_is_rejected() -> None:
    """No caller holds `""` deliberately — but a token whose `scopes` array carries an empty
    entry does, because the list branch of `_scopes_from_payload` does not filter."""
    with pytest.raises(ValueError, match="blank scope"):
        Policy(id="blank", tools=["fetch"], required_scopes=[""])


def test_a_refused_manifest_answers_400_with_the_reason(monkeypatch) -> None:
    """A refusal nobody can read is an outage.

    `parse_manifest` raised pydantic's `ValidationError` outside both try blocks in
    `PUT /manifests/{name}`, so a manifest refused for a stated reason answered
    `500 Internal Server Error` and the validator's message never left the server log. The
    operator's most available diagnosis is then "roll back Felix", which restores the
    permissive policy.
    """
    from felix.config import Settings as S
    from felix_api.app import create_app
    from starlette.testclient import TestClient

    monkeypatch.setenv("FELIX_AUTH_MODE", "none")
    settings = S(
        database_url="memory://ci",
        object_store="memory",
        auth_mode="none",
        allow_insecure=True,
        host="127.0.0.1",
    )
    app = create_app(settings=settings, plugins=[])
    body = {
        "manifest": {
            "apiVersion": "felix/v1",
            "kind": "Agent",
            "metadata": {"name": "finance"},
            "spec": {
                "pattern": "react",
                "policies": [{"id": "finance-only", "tools": ["wire_transfer"]}],
            },
        }
    }
    with TestClient(app) as client:
        res = client.put("/manifests/finance", json=body)

    assert res.status_code == 400, f"expected a readable refusal, got {res.status_code}"
    assert "required_scopes" in res.text, f"the reason did not reach the client: {res.text}"


def test_a_refusal_message_never_carries_the_offending_value() -> None:
    """`str(ValidationError)` embeds `input_value=`, and this message travels — into HTTP
    bodies, `jobs_store.record_run(error=...)` and a fiber's `state_json`. A manifest with an
    inline credential renders it in the `extra_forbidden` error."""
    from felix.manifests.loader import ManifestParseError, parse_manifest

    raw = {
        "apiVersion": "felix/v1",
        "kind": "Agent",
        "metadata": {"name": "leaky"},
        "spec": {"pattern": "react", "api_key": "sk-live-SUPERSECRET"},
    }
    with pytest.raises(ManifestParseError) as caught:
        parse_manifest(raw)

    assert "sk-live-SUPERSECRET" not in str(caught.value), str(caught.value)
    assert "api_key" in str(caught.value), "the message should still name the offending field"
