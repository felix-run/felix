---
name: api-surface
description: Add or change a Felix HTTP surface — REST/SSE chat routes, the OpenAI-compatible /v1 endpoints, A2A JSON-RPC, MCP, the agent card, and the scoped management APIs — including middleware order, auth scopes, streaming events, and the client contract. Use when editing anything under apps/api/src/felix_api/routes/, adding an endpoint, or changing an SSE event or response shape.
allowed-tools: Read Grep Glob Bash(./scripts/test.sh:*) Bash(curl:*)
metadata:
  covers: felix_api/, felix_client/, felix/a2a/
---

# Adding an API surface

## How a request is assembled

<!-- toolkit:enum middleware-order -->
`create_app()` (`apps/api/src/felix_api/app.py`) stacks middleware **request id → security headers →
body limit → rate limit → `AuthMiddleware`** (runtime order, outermost first; `add_middleware` inserts
at the front, so the code registers them in reverse — the comment above the calls says why each sits
where it does). All five are pure ASGI; `tests/unit/test_middleware_stack.py` fails a
`@app.middleware("http")`. It puts `settings` / `tools` / `plugins` on `app.state` (eagerly, so ASGI tests
work before lifespan runs), then mounts the route modules and any plugin routers.

The chat path every surface shares:

```
route → runtime.resolve_tenant_manifest()      # DB manifest store, else bundled YAML
      → runtime.prepare_tenant_invoke()        # enforce_inbound_auth + ensure_thread_pin
      → runtime.build_tenant_agent()           # session store + strategy + build_agent()
      → agent.invoke() / agent.stream_events()
```

Reuse those helpers. A new surface that builds an agent by hand skips inbound auth and compile
pinning — that is a security bug, not a shortcut.

## Route modules

<!-- toolkit:enum route-modules -->
| Module | Surface |
|---|---|
| `chat.py` | `/chat`, `/chat/stream`, runs, steer, abort/continue, thinking, sessions, fork/rewind, compact, export |
| `openai_compat.py` | `/v1/chat/completions`, `/v1/models` (`model` = manifest name) |
| `a2a.py` | `/a2a` JSON-RPC; `well_known.py` serves `/.well-known/agent-card.json` |
| `mcp.py` | `/mcp` server surface |
| `audit.py`, `approvals.py`, `plans.py`, `jobs.py`, `manifests.py`, `eval.py`, `usage.py` | management APIs |
| `artifacts.py`, `files.py`, `documents.py`, `memory.py`, `skills.py`, `skill_library.py` | tenant data APIs, mounted at `/artifacts`, `/files`, `/documents`, `/memory`, `/skills`, `/skill-library`; each gates on its own `<name>:read` / `<name>:write` scope (`auth/mgmt.py`) |
| `skill_import.py`, `skill_quality.py` | more of `/skill-library`: import/browse/upstream (`/-/browse`, `/{name}/-/upstream`) and feedback/evals (`/-/feedback`, `/{name}/evals`) |
| `auth_github.py` | GitHub login under `/auth/github` (device, token, actions, authorize/callback/exchange), `/auth` methods, `/github/connection` (the caller's own) and `/github/connections` (`github:admin`) |
| `repos.py` | `GET /github/repos` (the caller's repos, grouped by installation); a thread's repo checkout under `/chat/sessions` |
| `push.py` | `/push` web-push subscriptions and the VAPID key (gated on `approvals:read`) |
| `_sse.py`, `_streaming.py` | no routes: the SSE envelope and the session-log tail both stream loops share — never spell a frame by hand elsewhere |
| `_skill_library_models.py`, `_skill_library_http.py` | no routes: `/skill-library` request/response models and the helpers its route modules share |
| `internal.py` | `POST /internal/*` — requires `FELIX_CONSUMER_SHARED_SECRET` |

## Rules

1. **Management routes declare scopes explicitly**: `require_mgmt_scopes(request, "audit:read")`
   (`felix/auth/mgmt.py`). Remember the semantics — no-op when `auth_mode=none`, `admin`/`*`
   bypass, `x:write` satisfies `x:read`. Pick the narrowest scope that works.
2. **Streaming**: SSE frames come from `agent.stream_events()`. A new event type must be added to
   the harness *and* to the client contract in the felix-web repo
   (`apps/chat-ui/src/types.ts` `StreamEvent`) — that union has an open arm, so an unknown event
   compiles fine and silently does nothing.
3. **Durable runs** return `202` + `resume_token`, polled at `GET /chat/runs/{token}`. Don't invent
   a second async convention.
4. Keep FastAPI response models accurate — `/openapi.json` and the docs are generated from them.
   The checked-in wire contract (`schemas/openapi.json`, `schemas/sse-events.json`) must follow:
   run `make contract` after touching a route, a response model or a frame, and read the diff;
   `tests/unit/test_wire_contract.py` fails when either is stale.
5. Body limits: core is 1 MiB (`CORE_BODY_LIMIT_BYTES`); plugins can raise it via
   `body_limit_bytes`.

## Verify

```bash
./scripts/test.sh tests/integration/test_http_surfaces.py tests/integration/test_health.py
./scripts/test.sh tests/unit/test_mgmt_rbac.py        # when scopes changed
make contract-check                                   # wire contract still current
make dev   # then curl the surface
curl -s localhost:8080/openapi.json | jq '.paths | keys'
```

Document the surface: `guide/rest-api.mdx` (public) or `guide/management-api.mdx` (scoped) in the
felix-web docs repo, plus the protocol table in this repo's README. See the docs-sync skill.

## The Python client (`packages/client`)

`felix-client` (`felix_client`) is the experimental Python client. Its only dependency is httpx, and it
may not import any `felix*` package, so installing it never installs the server.
`tests/unit/test_invariants.py:test_the_model_layer_and_the_client_import_nothing_of_felix` walks every
import node in the package, lazy ones included; `tests/unit/test_felix_client_package.py` imports it
in a fresh interpreter and fails if FastAPI, SQLAlchemy, pydantic or any Felix package loads.

| Module | Holds |
|---|---|
| `packages/client/src/felix_client/client.py` | `FelixClient`, one method per route it wraps (chat and its controls, sessions, approvals, documents, skill import, …); `RUN_TERMINAL` and the durable-run poll pacing |
| `packages/client/src/felix_client/login.py` | GitHub device and Actions login, and the per-server token file (`token_path`, `save_token`, `bearer_for`) behind `felix login` and `FelixClient.from_login` |
| `packages/client/src/felix_client/docs_sync.py` | Markdown/MDX to the `/documents` corpus, with an opt-in prune; behind `felix ingest-docs` |
| `packages/client/src/felix_client/__init__.py` | the public names: `FelixClient`, the `RUN_*` constants and the login API |

`packages/harness/src/felix/sdk.py` keeps `from felix.sdk import FelixClient` working by re-exporting
`FelixClient` and the `RUN_*` constants — not the login API. New code imports `felix_client`.

### When a route changes

The client builds each URL and body by hand from `base_url`, and no test compares its paths or
bodies with `schemas/openapi.json`. So a route change has up to three places to land:

1. The route and its response model, then `make contract` and read the diff (rules above).
2. The `FelixClient` method that wraps it: path, body keys, query parameters. Most request models
   in `routes/chat.py` are `extra: forbid`, so a field the client still sends after a rename is a
   422 at runtime, and nothing in CI says so.
3. A run status: `RUN_TERMINAL` must hold every status in `durability/fibers.py:FIBER_TERMINAL_STATUSES`,
   which `tests/unit/test_invariants.py:test_every_consumer_of_run_status_agrees_on_what_is_terminal`
   enforces; a missing one is a run the client polls until its own deadline.

`FelixClient.stream` reads only `data:` lines and stops at `[DONE]`, so it relies on the envelope
`routes/_sse.py` defines; a frame must stay inside it.

### Tests

Unit tests drive the client through `httpx.MockTransport` and assert on the requests it sends:
`tests/unit/test_sdk_interrupts.py`, `tests/unit/test_sdk_session_list.py`,
`tests/unit/test_sdk_durable_poll.py` and `tests/unit/test_client_login.py`;
`tests/unit/test_docs_sync.py` covers the MDX reduction and page URLs. The e2e tests hand it the booted app's own client, so its requests
cross the real middleware and routes: `tests/e2e/test_github_login_client.py`,
`tests/e2e/test_github_actions_login_client.py` and `tests/e2e/test_docs_sync.py`.
