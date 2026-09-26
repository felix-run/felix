---
name: model-layer
description: How Felix talks to models and decision models — the felix_ai package boundary, wire formats, ProviderSpec rows and the provider registry, FELIX_MODEL_ROUTES and FELIX_DECISION_ROUTES, the model catalog and pricing, prompt caching, metering through record_usage, and the decider (Jev, llm) with each consumer that opts into it. Use when adding or changing a model provider, a decision provider, a route, a catalog entry, caching or thinking behaviour, or anything that calls spec.decider; and when a provider test skips or a run is metered wrong.
allowed-tools: Read Grep Glob Bash(./scripts/test.sh:*) Bash(uv run felix:*)
---

# The model layer

## The boundary

`packages/ai` (`felix_ai`) holds everything that speaks to a model: wire formats, provider
descriptors, the decision-model types and providers, the catalog, and the neutral turn types. It
**may not import `felix`** — `tests/unit/test_invariants.py` walks every import node, so a lazy
in-function import is not an escape hatch. What the harness injects arrives as a Protocol
(`ToolSchema`, `ModelConfig`) or through a sink (`felix_ai.observability`, `felix_ai.context`,
installed by `patterns/model_sinks.py`).

The harness keeps only what needs `Settings`: route resolution, metering, and the composites.

| Where | What |
|---|---|
| `felix_ai/wire/` | `openai_completions.py`, `anthropic_messages.py` (the two wire formats), `transport.py` (`post_with_retry`, `ModelGatewayError`), `base.py` (`HttpModelClient`) |
| `felix_ai/providers/` | `base.py:ProviderSpec` (one provider = one descriptor), `anthropic.py`, `compat.py` (every OpenAI-compatible endpoint as a row), `scripted.py` (the test provider) |
| `felix_ai/registry.py` | `register_model_provider` / `get_model_provider` — importable without the harness |
| `felix_ai/decide/` | `types.py` (`Choice`, `Score`, `Noul`, the `DecisionProvider` Protocol), `typesafe.py` (Jev, direct or via Workers AI), `llm.py` (any chat model), `scripted.py`, `registry.py` |
| `felix_ai/catalog.py` | one `ModelCatalogEntry` per model family: context window, quirks, pricing. Longest substring match wins |
| `felix/patterns/model.py` | `parse_model_routes`, `build_one_model`, `record_usage`, `_traced`, `register_builtin_providers` |
| `felix/patterns/model_composites.py` | fallback and confidence escalation |
| `felix/decisions.py` | `parse_decision_routes`, `build_decider` → `MeteredDecider`, `meets_criterion`, `register_builtin_deciders` |

## Routes

A manifest names a **logical id** (`spec.model.id`, `spec.decider.id`), never a vendor model.
Routes map it to `{provider, model}`:

- `FELIX_MODEL_ROUTES` (JSON) overlays `config.py:DEFAULT_MODEL_ROUTES`.
- `FELIX_DECISION_ROUTES` overlays `config.py:DEFAULT_DECISION_ROUTES` (`jev` via `typesafe`,
  `jev-cf` via `workers_ai`). Provider `llm` decides with any `FELIX_MODEL_ROUTES` id and needs
  no second vendor.
- Settings validation rejects, at boot, a route naming an unregistered provider — so a plugin
  provider must be registered before `Settings` validates.

Model and decision providers are **separate registries**. A decision provider has no `chat`; a
route from `spec.model.id` to one would build a client that fails on its first turn. `workers_ai`
is in both because it shares a credential, not an interface.

## Adding a model provider

1. **OpenAI-compatible endpoint** (most are): add a row to `felix_ai/providers/compat.py` with
   `_compat(name, base_url, …)`. Its credential comes from `FELIX_MODEL_PROVIDER_OPTIONS`, where a
   `secret:NAME` value resolves through the secrets backend; secret masking is derived from the
   descriptor, not hand-listed.
2. **A new wire format**: subclass `HttpModelClient` in `felix_ai/wire/`, give it a `ProviderSpec`,
   and add it to `builtin_provider_specs()`. It must implement `stream_turn` and report usage —
   a double without `stream_turn` looked correct in isolation and failed open on
   `limits.max_cost_usd`.
3. **From a plugin**: `PluginRegistry.register_model_provider(name, factory)`; core never names it.
4. Set `bills_per_token=False` for a local runtime, so a cost limit is enforceable without rates.
5. Add the model family to `felix_ai/catalog.py` with its context window and pricing. An unpriced
   model cannot enforce `limits.max_cost_usd`.
6. Add the arm to `WIRE_FORMATS` in `tests/conformance/test_model_provider.py` if it is a new wire
   format. The contract covers registration → route → `build_one_model` → a turn →
   `record_usage`. No arm needs infrastructure, so **a skip there is a bug**.

## Adding a decision provider

Implement the `DecisionProvider` Protocol (`felix_ai/decide/types.py`): `decide()` takes typed
questions and returns answers with a probability distribution and usage. Register it in
`BUILTIN_DECISION_PROVIDERS` (`felix_ai/decide/__init__.py`) or from a plugin, add a route, and
add the arm to `ARMS` in `tests/conformance/test_decision_provider.py`. Every call must go through
`build_decider`, which wraps the provider in `MeteredDecider`; a decider built any other way is
unmetered and bypasses the run's budgets.

## The decider's consumers

Each consumer opts in on its own and keeps its previous behaviour as the fallback when the decider
errors or is below `spec.decider.min_confidence`. Turning a decider on never removes a path.

| Consumer | Code | Manifest opt-in |
|---|---|---|
| tool selection | `tools/decider_retrieval.py` | `tools_retrieval.decider` |
| router sub-agent choice | `patterns/delegating.py` | `spec.decider.id` on a `router` |
| reflect verifier | `patterns/delegating.py` | `reflect.decider` |
| reply escalation | `patterns/model_composites.py` | `model.confidence_escalation.decider` |
| judges (tool output, and the final reply via `governance/reply.py`) | `governance/judges.py` | `guardrails.judges[].decider` |
| eval rubrics | `eval/runner.py` | `judge_decider` in the rubric |

A new consumer follows the same shape: a flag, the fallback kept, the call through
`build_decider`, and a validator in `Spec._decider_consumers_need_a_decider` so the flag without
`spec.decider.id` is an error rather than a silent no-op. End-to-end coverage lives in
`tests/e2e/test_decider_*.py`.

## Caching, thinking, metering

- `spec.model.cache: true` turns on prompt caching; the Anthropic wire format places
  `cache_control` (`wire/anthropic_messages.py:apply_anthropic_thinking_cache`). Every bundled
  manifest that talks to a hosted model sets it.
- `thinking_budget` / `thinking_level` are clamped to what the model accepts using the catalog's
  quirks (`catalog.py:clamp_effort`).
- Every model the harness builds goes through `build_one_model`, which wraps it in `_traced` —
  primary, fallbacks and escalation targets alike. Usage is recorded by `record_usage`; a client
  built around it is invisible to metering, spans and cost limits.

## Verify

```bash
./scripts/test.sh tests/conformance/test_model_provider.py tests/conformance/test_decision_provider.py -q
./scripts/test.sh tests/unit/test_invariants.py -q          # includes the felix_ai import boundary
make e2e
```

Tests never reach a vendor: `scripts/test.sh` blanks every credential, and e2e routes the model to
`felix_ai.providers.scripted`. Docs: `internals/model-client.mdx` in felix-web, and the
`DEFAULT_MODEL_ROUTES` table in the README when a logical id changes.
