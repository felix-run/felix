---
name: api-surface
description: Add or change a Felix HTTP surface — REST/SSE chat routes, the OpenAI-compatible /v1 endpoints, A2A JSON-RPC, MCP, the agent card, and the scoped management APIs — including middleware order, auth scopes, streaming events, and the client contract. Use when editing anything under apps/api/src/felix_api/routes/, adding an endpoint, or changing an SSE event or response shape.
allowed-tools: Read Grep Glob Bash(./scripts/test.sh:*) Bash(curl:*)
---

# Adding an API surface

## How a request is assembled

`create_app()` (`apps/api/src/felix_api/app.py`) stacks middleware **body limit → rate limit →
`AuthMiddleware`**, puts `settings` / `tools` / `plugins` on `app.state` (eagerly, so ASGI tests
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

| Module | Surface |
|---|---|
| `chat.py` | `/chat`, `/chat/stream`, runs, steer, abort/continue, thinking, sessions, fork/rewind, compact, export |
| `openai_compat.py` | `/v1/chat/completions`, `/v1/models` (`model` = manifest name) |
| `a2a.py` | `/a2a` JSON-RPC; `well_known.py` serves `/.well-known/agent-card.json` |
| `mcp.py` | `/mcp` server surface |
| `audit.py`, `approvals.py`, `plans.py`, `jobs.py`, `manifests.py`, `eval.py`, `usage.py` | management APIs |
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
5. Body limits: core is 1 MiB (`CORE_BODY_LIMIT_BYTES`); plugins can raise it via
   `body_limit_bytes`.

## Verify

```bash
./scripts/test.sh tests/integration/test_http_surfaces.py tests/integration/test_health.py
./scripts/test.sh tests/unit/test_mgmt_rbac.py        # when scopes changed
make dev   # then curl the surface
curl -s localhost:8080/openapi.json | jq '.paths | keys'
```

Document the surface: `guide/rest-api.mdx` (public) or `guide/management-api.mdx` (scoped) in the
felix-web docs repo, plus the protocol table in this repo's README. See the docs-sync skill.
