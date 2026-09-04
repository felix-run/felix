"""Cost is fixed on the usage row at write time, and `GET /usage/summary` adds it up.

`record_tokens` wrote token counts and the *logical* route name and no cost, so `GET /usage`
returned raw rows and nothing could answer "what did tenant X spend last month" — and
anyone recomputing later fed the logical name to the price table and got `$0` for every
custom route. `spec.model.price` was documented as overriding cost attribution and only
decorated the `/v1/models` listing. `record_usage` is now the one pricer: by the wire id
and the manifest's override, once, and the row carries the result and the id it used.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from felix.config import Settings
from felix.context import AuthContext, RequestContext, async_run_with_context
from felix.patterns.model import ModelChatResult, TokenUsage, price_override_for, record_usage
from felix.patterns.types import ChatMessage
from felix.usage import store as usage_store
from felix.usage.pricing import usage_with_cost

WIRE = "claude-sonnet-4-5"  # priced in the bundled catalog
DAY_MS = 24 * 60 * 60 * 1000


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": "memory://usage-cost",
        "object_store": "memory",
        "redis_url": "",
        "allow_insecure": True,
        "auth_mode": "none",
        "host": "127.0.0.1",
        "environment": "development",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _ctx(tenant: str = "acme", manifest: str = "support") -> RequestContext:
    return RequestContext(
        settings=_settings(),
        auth=AuthContext(principal_sub="alice", tenant_id=tenant, anonymous=False),
        manifest_id=manifest,
    )


def _result(tokens_in: int = 1_000_000, tokens_out: int = 0) -> ModelChatResult:
    return ModelChatResult(
        message=ChatMessage(role="assistant", content="x"),
        stop_reason="end_turn",
        usage=TokenUsage(input=tokens_in, output=tokens_out),
    )


def _catalog_cost(tokens_in: int) -> float:
    return usage_with_cost(TokenUsage(input=tokens_in), model_id=WIRE)["cost"]["total"]


def _utc_day(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, UTC).strftime("%Y-%m-%d")


@pytest.fixture(autouse=True)
def _clean() -> Any:
    usage_store.pending_buffer().reset_for_tests()
    usage_store.clear_memory()
    yield
    usage_store.pending_buffer().reset_for_tests()
    usage_store.clear_memory()


def _rows() -> list[dict[str, Any]]:
    return usage_store.pending_buffer().snapshot()


# --- the row carries cost and the id it was priced by --------------------------------


def test_record_tokens_writes_the_cost_it_is_handed_and_the_wire_id() -> None:
    """The store does not price; it keeps what the one pricer decided, on the row."""
    usage_store.record_tokens(
        _settings(),
        tenant_id="acme",
        manifest_id="support",
        model_id="fast",
        wire_model_id=WIRE,
        cost_usd=1.25,
    )
    (row,) = _rows()
    assert row["wire_model_id"] == WIRE
    assert row["model_id"] == "fast", "the logical name is still what is reported"
    assert row["cost_usd"] == 1.25


def test_a_row_with_no_cost_writes_zero_not_nothing() -> None:
    usage_store.record_tokens(
        _settings(),
        tenant_id="acme",
        manifest_id="support",
        model_id="fast",
        wire_model_id="mystery",
        tokens_input=5,
    )
    (row,) = _rows()
    assert row["cost_usd"] == 0.0
    assert row["wire_model_id"] == "mystery"


# --- record_usage is the one pricer ---------------------------------------------------


@pytest.mark.asyncio
async def test_record_usage_prices_by_the_wire_id_and_a_manifest_override_wins() -> None:
    """The catalog rate for the wire model, or the manifest's `spec.model.price` when it
    sets one — never the logical route name, which the price table does not know."""
    async with async_run_with_context(_ctx()):
        record_usage(_result(), manifest_id="support", model_id="fast", wire_model_id=WIRE)
        record_usage(
            _result(),
            manifest_id="support",
            model_id="fast",
            wire_model_id="some-private-model",
            price_override={"input": 2.0},
        )
    by_catalog, by_override = _rows()
    assert by_catalog["wire_model_id"] == WIRE
    assert by_catalog["cost_usd"] == pytest.approx(_catalog_cost(1_000_000))
    assert by_catalog["cost_usd"] > 0
    assert by_override["cost_usd"] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_the_row_and_the_budget_carry_the_same_cost() -> None:
    ctx = _ctx()
    async with async_run_with_context(ctx):
        record_usage(
            _result(),
            manifest_id="support",
            model_id="fast",
            wire_model_id="mystery",
            price_override={"input": 4.0},
        )
    (row,) = _rows()
    assert row["cost_usd"] == pytest.approx(4.0)
    assert ctx.limit_state.cost_usd == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_an_unpriced_turn_raises_the_unpriced_counter_and_an_override_silences_it() -> None:
    """`felix_model_unpriced` is the signal that `limits.max_cost_usd` is failing open."""
    from felix.observability.metrics import REGISTRY

    labels = {"manifest_id": "support", "model": "fast"}

    def count() -> float:
        return REGISTRY.get_sample_value("felix_model_unpriced_total", labels) or 0.0

    before = count()
    async with async_run_with_context(_ctx()):
        record_usage(_result(), manifest_id="support", model_id="fast", wire_model_id="mystery")
        assert count() == before + 1
        record_usage(
            _result(),
            manifest_id="support",
            model_id="fast",
            wire_model_id="mystery",
            price_override={"input": 1.0},
        )
        assert count() == before + 1, "priced by the override"
        record_usage(_result(), manifest_id="support", model_id="fast", wire_model_id=WIRE)
        assert count() == before + 1, "priced by the catalog"


# --- the override rides on the built client ------------------------------------------


@dataclass
class _Spec:
    id: str = "fast"
    price: dict[str, float] | None = None
    fallbacks: list[str] | None = None
    confidence_escalation: Any = None


@dataclass
class _Escalation:
    enabled: bool = True
    escalate_to: str = "slow"
    low_confidence_markers: tuple[str, ...] = ("not sure",)
    min_response_chars: int = 0


def test_price_override_for_reads_the_manifest_price() -> None:
    assert price_override_for(_Spec()) is None
    assert price_override_for(_Spec(price={})) is None
    assert price_override_for(_Spec(price={"input": 1, "output": "2.5"})) == {"input": 1.0, "output": 2.5}


def test_build_model_carries_the_override_on_every_client_shape() -> None:
    """Every metering site reads it from the client, so the built client has to have it —
    the traced leaf, the fallback composite and the escalation composite alike."""
    from felix.patterns import model as model_mod

    settings = _settings(
        # A local provider: registered by default and constructed without a credential.
        model_routes='{"fast": {"provider": "ollama", "model": "llama3"}, "slow": {"provider": "ollama", "model": "llama3:70b"}}',
    )
    price = {"input": 3.0, "output": 9.0}
    assert getattr(model_mod.build_model(settings, _Spec(price=price)), "price_override", None) == price
    assert (
        getattr(
            model_mod.build_model(settings, _Spec(price=price, fallbacks=["slow"])), "price_override", None
        )
        == price
    )
    escalating = model_mod.build_model(settings, _Spec(price=price, confidence_escalation=_Escalation()))
    assert getattr(escalating, "price_override", None) == price
    assert getattr(model_mod.build_model(settings, _Spec()), "price_override", "unset") is None


@pytest.mark.asyncio
async def test_a_turn_through_the_loop_is_priced_by_the_clients_override() -> None:
    """The override has to travel from the built client to the metering site: a client
    whose wire model has no catalog rate, with an override, produces a priced row."""
    from felix.patterns.react import _ReactAgent
    from felix.patterns.types import InvokeInput

    @dataclass
    class _Route:
        model: str = "some-private-model"

    class _Client:
        model_id = "fast"
        route = _Route()
        price_override = {"input": 5.0, "output": 0.0}

        async def chat(self, messages: Any, tools: Any, opts: Any = None) -> ModelChatResult:
            return _result(tokens_in=1_000_000)

    agent = _ReactAgent(
        tools=[],
        pattern="react",
        manifest_id="support",
        manifest_version="1",
        system_prompt="s",
        model_spec=None,
        settings=None,
        recursion_limit=2,
        session_store=None,
        session_strategy=None,
    )
    agent._resolve_model = lambda _i: _Client()  # type: ignore[method-assign]
    ctx = _ctx()
    async with async_run_with_context(ctx):
        await agent.invoke(InvokeInput(messages=[ChatMessage(role="user", content="hi")]))
    (row,) = _rows()
    assert row["wire_model_id"] == "some-private-model"
    assert row["cost_usd"] == pytest.approx(5.0)
    assert ctx.limit_state.cost_usd == pytest.approx(5.0)


# --- the summary ------------------------------------------------------------------------


async def _seed(settings: Settings) -> None:
    for tenant, manifest, model, tokens in (
        ("acme", "support", "fast", 1_000_000),
        ("acme", "support", "fast", 500_000),
        ("acme", "deep", "big", 250_000),
        ("other", "support", "fast", 9_000_000),
    ):
        usage_store.record_tokens(
            settings,
            tenant_id=tenant,
            manifest_id=manifest,
            model_id=model,
            wire_model_id=WIRE,
            tokens_input=tokens,
            cost_usd=_catalog_cost(tokens),
        )
    await usage_store.flush_pending(settings)


@pytest.mark.asyncio
async def test_summary_groups_by_manifest_and_model_within_the_tenant() -> None:
    settings = _settings()
    await _seed(settings)
    out = await usage_store.summary(settings, "acme")
    by_key = {(i["manifest_id"], i["model_id"]): i for i in out["items"]}
    assert set(by_key) == {("support", "fast"), ("deep", "big")}
    assert by_key[("support", "fast")]["calls"] == 2
    assert by_key[("support", "fast")]["tokens_input"] == 1_500_000
    assert by_key[("support", "fast")]["cost_usd"] == pytest.approx(_catalog_cost(1_500_000))
    assert out["totals"]["calls"] == 3
    assert out["totals"]["cost_usd"] == pytest.approx(_catalog_cost(1_750_000)), (
        "the other tenant's spend is not here"
    )


@pytest.mark.asyncio
async def test_summary_buckets_by_utc_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every seeded row lands in the same millisecond, so without moving the clock the
    day bucket is shape-only and a constant date would pass."""
    settings = _settings()
    t0 = usage_store.now_ms()
    monkeypatch.setattr(usage_store, "now_ms", lambda: t0 - 2 * DAY_MS)
    usage_store.record_tokens(
        settings, tenant_id="acme", manifest_id="support", model_id="fast", cost_usd=1.0
    )
    monkeypatch.setattr(usage_store, "now_ms", lambda: t0)
    usage_store.record_tokens(
        settings, tenant_id="acme", manifest_id="support", model_id="fast", cost_usd=3.0
    )
    await usage_store.flush_pending(settings)
    out = await usage_store.summary(settings, "acme", since_ms=t0 - 3 * DAY_MS, until_ms=t0 + 1)
    assert {i["day"]: i["cost_usd"] for i in out["items"]} == {
        _utc_day(t0): 3.0,
        _utc_day(t0 - 2 * DAY_MS): 1.0,
    }
    assert out["items"][0]["day"] == _utc_day(t0), "newest day first"


@pytest.mark.asyncio
async def test_summary_filters_by_manifest_and_window() -> None:
    settings = _settings()
    await _seed(settings)
    only_deep = await usage_store.summary(settings, "acme", manifest_id="deep")
    assert [i["manifest_id"] for i in only_deep["items"]] == ["deep"]
    nothing = await usage_store.summary(settings, "acme", since_ms=0, until_ms=1)
    assert nothing["items"] == []
    assert nothing["totals"]["cost_usd"] == 0


@pytest.mark.asyncio
async def test_the_summary_route_reports_the_callers_tenant_only() -> None:
    """Two keys, two tenants: each sees its own rows and totals, never the other's."""
    from felix_api.app import create_app
    from httpx import ASGITransport, AsyncClient

    settings = _settings(
        auth_mode="api_key",
        auth_api_keys=(
            '{"sk-acme": {"tenant_id": "acme", "sub": "a", "scopes": ["usage:read"]},'
            ' "sk-other": {"tenant_id": "other", "sub": "o", "scopes": ["usage:read"]},'
            ' "sk-none": {"tenant_id": "acme", "sub": "n", "scopes": []}}'
        ),
    )
    await _seed(settings)
    app = create_app(settings=settings, plugins=[])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        acme = (await client.get("/usage/summary", headers={"Authorization": "Bearer sk-acme"})).json()
        other = (await client.get("/usage/summary", headers={"Authorization": "Bearer sk-other"})).json()
        assert acme["totals"]["calls"] == 3
        assert other["totals"]["calls"] == 1
        assert {"since_ms", "until_ms", "items", "totals"} <= set(acme)
        assert (
            await client.get("/usage/summary", headers={"Authorization": "Bearer sk-none"})
        ).status_code == 403
        bad = await client.get(
            "/usage/summary?since_ms=5&until_ms=5", headers={"Authorization": "Bearer sk-acme"}
        )
        assert bad.status_code == 422
        listed = (await client.get("/usage", headers={"Authorization": "Bearer sk-acme"})).json()["items"]
        assert len(listed) == 3
        assert all("cost_usd" in row and "wire_model_id" in row for row in listed)
