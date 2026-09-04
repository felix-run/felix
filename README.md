# Felix

[![CI](https://github.com/felix-run/felix/actions/workflows/ci.yml/badge.svg)](https://github.com/felix-run/felix/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

**Felix** is a self-hostable **agents harness**. You author agents as YAML manifests
(`apiVersion: felix/v1`); Felix compiles them into governed agents with durable fibers, memory,
skills, eval, approvals, and sandboxes — served over REST/SSE, an OpenAI-compatible API, A2A, and
MCP. Fork, rewind, and steer live runs. Deploy with Docker, Helm, AWS, or GCP on infrastructure you
operate.

📖 **[docs.felix.run](https://docs.felix.run)** — installation, concepts, manifest and API reference

| | |
|---|---|
| Web UI | [github.com/felix-run/web](https://github.com/felix-run/web) |
| Roadmap | [docs/ROADMAP.md](docs/ROADMAP.md) |
| Changelog | [CHANGELOG.md](CHANGELOG.md) |

## What you get

- **Manifests** — `felix/v1` YAML; bundled agents in `manifests/`
- **Governance** — auth, approvals, audit, usage meters
- **Durable execution** — fibers (Temporal optional), steer and follow-up
- **Session control** — fork, rewind, compacting / windowed / semantic strategies
- **Memory and skills** — durable facts, procedural memory, Agent Skills
- **Surfaces** — REST/SSE, OpenAI-compatible `/v1`, A2A, MCP
- **Eval** — datasets, fixtures, `--mock` CI path
- **Deploy** — lean Docker, Helm, AWS, or GCP

## Quick start

```bash
cp .env.example .env
# Set POSTGRES_PASSWORD (and MINIO_ROOT_PASSWORD only with --profile full):
#   openssl rand -hex 32

make install          # lean core + dev (small VMs / CI)
make up               # api :8080, worker, pgvector, Valkey (fs object store)
make migrate
curl -s http://localhost:8080/health | jq
```

Two alternatives to `make up`:

```bash
make up-lite          # tighter memory caps for ~2–4 GiB hosts
make up-full          # adds MinIO and the aws extra (FELIX_DOCKER_EXTRAS=aws)
```

`make up` runs `scripts/dev-key.sh`, which writes a local API key into `.env` on first run and
prints it. **The stack is authenticated by default** and publishes on `127.0.0.1` — set
`FELIX_BIND_ADDR` to widen it, but only behind real auth.

Export the key so the examples below work:

```bash
export FELIX_KEY=$(grep -o 'sk-felix-local-[a-f0-9]*' .env | head -1)
```

For cloud SDKs, embeddings, or browser tools locally, use `make install-full`.

### Send a request

Chat against the bundled `quick` manifest:

```bash
curl -s -X POST http://localhost:8080/chat \
  -H "authorization: Bearer $FELIX_KEY" \
  -H 'content-type: application/json' \
  -d '{"manifest":"quick","messages":[{"role":"user","content":"What is 7 * 6?"}]}' | jq
```

Or use the OpenAI-compatible surface, where `model` is the manifest name:

```bash
curl -s http://localhost:8080/v1/chat/completions \
  -H "authorization: Bearer $FELIX_KEY" \
  -H 'content-type: application/json' \
  -d '{"model":"quick","messages":[{"role":"user","content":"hi"}]}' | jq
```

### Local development without Compose

```bash
make install
make migrate
make dev                      # Granian on :8080, FELIX_AUTH_MODE=none, FELIX_OBJECT_STORE=fs
make cli                      # httpx REPL client
make check                    # ruff + ty + pytest + format check (matches CI)
./scripts/test.sh -k <expr>   # one test; sets the in-memory stores the suite needs
```

Run tests with `./scripts/test.sh`, never a bare `pytest` — the repo `.env` points at a real
Postgres, so the suite would fail on connection errors that look like code bugs. See
[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) for this and other recurring failure modes.

## Deployment

### Small VMs and lean images

Default images and Compose stay **lean**:

| Concern | Default | Full / cloud |
|---|---|---|
| Object store | `FELIX_OBJECT_STORE=fs` (local dir) | `s3` or `gcs` + `felix-harness[aws]` / `felix-harness[gcp]` |
| Image extras | none | `FELIX_DOCKER_EXTRAS=aws,gcp` |
| Compose | api + worker + Postgres + Valkey | `--profile full` adds MinIO |
| Memory | Compose `mem_limit` caps | raise via `FELIX_*_MEM_LIMIT` |

```bash
make up-lite   # deploy/docker/compose.lite.yml — ~2–4 GiB hosts
make up-gcp    # GCE / public VM: no DB or cache host ports
```

Docker packaging lives under [`deploy/docker/`](deploy/docker/). Always run Compose from the repo
root — `make up` sets `--project-directory .`.

Heavy optional dependencies (Playwright, sentence-transformers, DuckDB, Presidio, Temporal) are
**never** in the default image. Install them through extras only when needed.

### Analytics warehouse (optional)

Postgres is the system of record. The warehouse is optional append-only spill for audit and eval
analytics, flushed by the worker *after* the Postgres write.

| Choice | When to use it | Extra |
|---|---|---|
| `none` | Lean default; no analytics spill | — |
| `duckdb` | **Recommended.** Small VMs; embedded file under `FELIX_DATA_DIR/warehouse` | `warehouse` |
| `clickhouse` | High-volume audit and event scale-out | `warehouse-clickhouse` |
| `doris` | Already operating Apache Doris or MySQL-protocol BI | `warehouse-doris` |

```bash
uv sync --extra warehouse     # or: make install-warehouse
# Then set: FELIX_WAREHOUSE=duckdb
# In Docker: FELIX_DOCKER_EXTRAS=warehouse FELIX_WAREHOUSE=duckdb
```

## Architecture

```text
Client → Ingress (Caddy / Traefik / nginx / Cloudflare DNS+CDN)
           ├─ felix-api        (CPython 3.14, Granian, FastAPI)
           ├─ felix-worker     (Taskiq consumer)
           └─ felix-scheduler  (Taskiq cron enqueue)
                  │
     Postgres+pgvector · Valkey · object store (fs | S3 | GCS)
```

| Component | Responsibility |
|---|---|
| `apps/api` | HTTP: `/chat`, `/v1`, `/a2a`, `/mcp`, management APIs, OpenAPI |
| `apps/worker` | Audit flush, scheduled jobs, memory consolidation, retention, anomaly scan, continuous eval, fiber resume |
| `felix-scheduler` | Enqueues labeled Taskiq cron tasks — **required alongside the worker**, or nothing periodic fires |
| `packages/ai` | Model layer: wire formats, catalog, turn types. Imports nothing from `felix` |
| `packages/harness` | Manifests, patterns, tools, session, governance, auth, plugins |
| `packages/cli` | `felix migrate \| eval \| mint-jwt \| bundle-manifests \| validate-manifest \| doctor \| version \| temporal-worker` |
| `manifests/` | Bundled agents: `quick`, `deep`, `router`, `oss-only`, `hybrid-router`, `support`, `cowork`, `governed`, `contributor` |

### Vendor independence

Felix is **service- and cloud-agnostic**: the harness talks to Postgres, a cache, and an object store
through Protocols, not a single vendor SDK.

**AWS and GCP are first-class** — S3, Secrets Manager, GCS, and Secret Manager via the optional
`felix-harness[aws]` and `felix-harness[gcp]` extras. Small VMs can use `FELIX_OBJECT_STORE=fs` with
zero cloud SDKs.

- Set `FELIX_OBJECT_STORE` to `s3`, `gcs`, `fs`, `memory`, or a backend you register
- Set `FELIX_SECRETS_BACKEND` to `env`, `file`, `aws`, `gcp`, or a backend you register
- Deploy notes: [`deploy/aws/`](deploy/aws/), [`deploy/gcp/`](deploy/gcp/)
- Manifest secrets, plus opt-in SOC 2 and EU AI Act mapping: [`deploy/GOVERNANCE.md`](deploy/GOVERNANCE.md)
- Helm: enable `persistence` when using `fs`, so `/data` survives restarts
- Production JWT and api_key deploys need `FELIX_CONSUMER_SHARED_SECRET` for `POST /internal/*`

**Sizing.** Each worker process carries its own connection pool, so raise the two together:
`FELIX_WORKERS` (1) and `FELIX_DB_POOL_SIZE` (10) + `FELIX_DB_MAX_OVERFLOW` (20) — past that
ceiling requests queue for `FELIX_DB_POOL_TIMEOUT_SECONDS` and then fail. Set
`FELIX_DB_POOL_PRE_PING=false` against a direct Postgres; it costs a round trip per checkout and
only earns it behind PgBouncer, RDS Proxy, or Cloud SQL.

**Behind a pooler.** `WORKERS × (POOL_SIZE + MAX_OVERFLOW)` is the ceiling, and four workers on the
defaults is 120 connections against a stock Postgres `max_connections` of 100 — which is when a
transaction-mode pooler stops being optional. Set `FELIX_DB_PREPARED_STATEMENTS=false` there:
psycopg3 auto-prepares after five executions, and under transaction pooling the sixth lands on a
different server connection and fails. PgBouncer ≥ 1.21 with `max_prepared_statements > 0` tracks
them for you and needs no change; RDS Proxy instead pins the session when it sees one, defeating the
multiplexing you deployed it for, so turn preparation off there.

Felix runs on infrastructure **you** operate. Cloudflare DNS, CDN, TLS, and WAF in front of your
origin are fine. There is **no** Cloudflare Workers, Durable Objects, Hyperdrive, R2-as-binding,
Queues, or Workflows compute in this stack.

The line is **compute**, not vendor. Calling a hosted Cloudflare **API** over HTTPS — Workers AI as a model provider, R2 through its S3 endpoint — is an outbound request like any other and is fine. What Felix will not do
is *run on* Workers or Durable Objects, or depend on a binding only available inside them.

### Extending Felix

Felix is built to **not dictate your workflow**. Features other harnesses bake in are meant to
be added from outside: core stays minimal, and the seams below are open by design.

Install a package that declares a `felix.plugins` entry point and core discovers it at startup
— Felix never imports it by name:

```toml
[project.entry-points."felix.plugins"]
my-plugin = "my_plugin:register"
```

`register(registry)` is then called once. Through the registry a plugin adds **tools**, **HTTP
routes**, **cron tasks**, **auth modes**, **rate-limit keys**, **body limits**,
**self-authenticating mounts**, **startup hooks**, **audit/usage sinks**, and six
**agent-loop hooks** (`before_turn`, `filter_history`, `before_compact`, `before_tool`,
`after_tool`, `compact_failed`).

Core also exposes open registries, callable at import time, each selected by ordinary config:

| Register | Selected by |
|---|---|
| `register_pattern` | `spec.pattern` |
| `register_model_provider` | `FELIX_MODEL_ROUTES` |
| `register_object_store` | `FELIX_OBJECT_STORE` |
| `register_secrets_backend` | `FELIX_SECRETS_BACKEND` |
| `register_warehouse_backend` | `FELIX_WAREHOUSE` |
| `register_embedder_backend` | `FELIX_MEMORY_EMBEDDER` |
| `register_search_backend` | `FELIX_SEARCH_BACKEND` |
| `register_session_strategy` | `spec.session.strategy` |
| `register_checkpointer` | `spec.memory.checkpointer` |

A plugin carries its own manifest config under `spec.extensions.<name>` — the one field exempt
from the schema's `extra="forbid"` — and reads it from the pattern build context.

Agent Skills need no code at all: drop a `SKILL.md` under the directory named by
`FELIX_SKILLS_DIR`, or upload one per tenant to the object store.

**[`examples/felix-plugin-example/`](examples/felix-plugin-example/)** is a working package that
exercises every seam above.

Two things are deliberately **not** extensible: the nine-wrapper governance order in
`manifests/builder.py` (order defines precedence, so `before_tool` / `after_tool` hooks are the
sanctioned boundary instead), and `spec.guardrails.providers`. Both have their rationale
recorded in [`docs/ROADMAP.md`](docs/ROADMAP.md).

## API surfaces

| Surface | Path |
|---|---|
| Direct REST / SSE | `POST /chat`, `POST /chat/stream` |
| Durable run poll | `GET /chat/runs/{resume_token}` |
| Steer / follow-up | `POST /chat/steer` |
| Abort / continue | `POST /chat/abort`, `POST /chat/continue` |
| Thinking level | `POST /chat/thinking` |
| Session snapshot | `GET /chat/sessions`, `GET /chat/sessions/{id}` |
| Session search (FTS) | `GET /chat/sessions/search?q=` |
| Session lease | `POST /chat/sessions/lease`, `…/lease/release` |
| Session name / label / export | `POST /chat/sessions/name`, `…/label`, `GET …/export` |
| Compact / UI prompt | `POST /chat/compact`, `POST /chat/ui` |
| Session fork / rewind | `POST /chat/fork`, `POST /chat/rewind` |
| OpenAI-compatible | `POST /v1/chat/completions`, `GET /v1/models` |
| A2A JSON-RPC | `POST /a2a` |
| MCP | `POST /mcp` |
| Agent card | `GET /.well-known/agent-card.json` |
| Liveness / readiness | `GET /live` (also `/health`), `GET /ready` |

A dropped stream is recoverable: structural SSE frames carry an `id:` cursor (token-level frames do not, which per the SSE spec leaves the client's `lastEventId` on the last one it saw), and `GET /chat/stream/{thread_id}` replays what was missed (or opens with a `snapshot` frame) and then tails the thread. The run itself is still torn down on disconnect, so what you get back is the thread, not the abandoned turn.

Management surfaces: `/audit`, `/approvals`, `/plans`, `/jobs`, `/manifests`, `/eval`, `/usage`, `/memory`. `/memory` lists, searches (the same hybrid ranking the agent sees), time-travels (`/memory/as-of/{turn_seq}`), writes and forgets long-term memories — an agent that remembers across sessions otherwise accumulates a store nobody can inspect.

Python client: `from felix.sdk import FelixClient` — `prompt`, `stream`, `steer`, `follow_up`,
`fork`, `rewind`, `set_model`.

### Models

`FELIX_MODEL_TIMEOUT_SECONDS` (default `120`) bounds each HTTP request to a model provider.
Generating a large tool call — a file's contents as an argument, say — can exceed it, and the
failure surfaces as a failed run rather than a slow one. On a **streaming** call it bounds the
gap between chunks rather than the whole turn. Read and write timeouts are deliberately **not**
retried: the retry re-sends identical input and waits out the identical ceiling, so the answer
is a larger timeout, not another attempt. Connect timeouts still retry, and connect is pinned
at 10s so raising this does not also let an unreachable provider hang.

Outbound integrations carry their own ceilings: `spec.mcp_servers[].timeout_ms` (default 30s),
`spec.peers[].timeout_ms` (default 60s), and the existing `timeout_ms` on sandboxes and
containers.


Providers Felix ships, all speaking one of two wire formats:

| Provider | Endpoint | Configured with |
|---|---|---|
| `anthropic` | `api.anthropic.com` | `FELIX_ANTHROPIC_API_KEY` |
| `openai` | `api.openai.com/v1`, or `FELIX_LITELLM_BASE_URL` | `FELIX_OPENAI_API_KEY` |
| `ollama` | `FELIX_OLLAMA_BASE_URL` | — (local, and billed as free) |
| `workers_ai` | `api.cloudflare.com/…/accounts/{account_id}/ai/v1` | `api_key`, `account_id`, optional `gateway_id` |
| `groq` `together` `deepseek` `cerebras` `fireworks` `openrouter` `xai` `mistral` `google` | each vendor's OpenAI-compatible endpoint | `api_key` |

Everything past the first three is configured through `FELIX_MODEL_PROVIDER_OPTIONS` rather
than a settings field per vendor. Each is also selectable as `FELIX_MEMORY_EMBEDDER`, since
`/embeddings` is part of the same wire format.

**None of the hosted tier ships with per-token rates, deliberately.** Felix does not invent
prices: an unpriced model contributes zero to spend and a manifest that *declares*
`limits.max_cost_usd` on one is refused at compile, pointing at `spec.model.price`. Guessing
is how every unrecognised model came to be billed at Claude Sonnet's $3/$15 per Mtok.
Cloudflare bills Workers AI in neurons rather than tokens, so a per-token rate for it would
be fiction. `ollama` is exempt because a local runtime genuinely costs nothing — that is a
property of the provider, not of the model's name.

A provider is a descriptor — a wire format, an endpoint, and where its credential lives —
so adding one is a row rather than a module. Both wire formats and the HTTP transport are
public in `felix_ai.wire` (`OpenAICompletionsClient`, `AnthropicMessagesClient`,
`post_with_retry`, `map_stop`, `parse_tool_arguments`), because re-deriving retry-on-429,
SSE parsing and usage accounting is most of the work of writing a provider — and what a
provider gets wrong in usage reporting fails *open* on `limits.max_cost_usd`.

`FELIX_MODEL_PROVIDER_OPTIONS` carries a per-provider endpoint and credential as JSON. The
built-in providers have named settings, but a provider added by a plugin cannot — `Settings`
ignores unknown env vars — so this is how an installed provider is given a key. An entry
also overrides the named field, which is how a built-in is pointed at a gateway:

```
FELIX_MODEL_PROVIDER_OPTIONS={"anthropic":{"base_url":"https://gateway.internal/v1"}}
```

Every option value is added to the redaction list **except** the ones the provider consumes
as addressing — `base_url`, any `{placeholder}` its endpoint templates, and its header
options — so a credential cannot reach tool output whatever the option is called. The
converse is worth knowing: an unrecognised option is redacted, so a long, non-secret value
there will be masked out of tool results. Keep credentials out of `base_url`, which is
exempt by definition and also reaches server logs through connection errors. Providers named in `FELIX_MODEL_ROUTES` are resolved
against the registry at startup, so a typo fails immediately rather than on the first
request that happens to take that route.

Manifests reference **logical** model ids, mapped to wire ids by `FELIX_MODEL_ROUTES` (a JSON
override) or by the built-in defaults:

| Logical id | Provider | Wire model |
|---|---|---|
| `claude-opus` | anthropic | `claude-opus-5` |
| `claude-sonnet` (default) | anthropic | `claude-sonnet-5` |
| `claude-haiku` | anthropic | `claude-haiku-4-5` |
| `claude-fable` | anthropic | `claude-fable-5` |
| `gpt-4.1` / `gpt-4.1-mini` | openai | same |
| `llama-3-pro` / `llama-3-fast` | ollama | `llama3.3:70b` / `llama3.2` |

A streaming turn is one model call. `POST /chat/stream` emits deltas from the same request that
produces the turn's tool calls, usage and stop reason, so the text a client watches arrive is the
text that gets saved. A provider integration that implements only the text-oriented `stream()` still
falls back to streaming for display and calling the model again for the authoritative turn.

Side requests made during a turn — compaction summarising, memory extracting facts, inbound
screening scoring, branch summarisation — opt out of the conversation's prompt cache. Each carries
a completely different prefix, so sharing the thread's cache identity churns the cached prefix the
next real turn would have hit, and writes a cache entry that is never read again.

Model calls retry rate limits and transient upstream failures with backoff, honouring `Retry-After`
up to a ceiling; a 429 that reports a spent quota or a billing problem is returned straight away,
since that will not clear inside the request. `spec.model.fallbacks` still switches models once
retries are exhausted. Recalled memory facts are
rendered as a per-run prelude rather than folded into the system prompt, so the cached prompt prefix
stays stable across turns.

Everything Felix knows about a model — context window, max output, price, accepted request
parameters, thinking support, modalities — lives in one record per family in `felix/model_catalog.py`,
resolved by the longest key appearing in the model id. Request shaping, `/v1/models`, and cost
estimation are all views over it. The current Claude generation takes adaptive thinking plus
`output_config.effort`, while pre-4.6 models take a fixed `budget_tokens`; `spec.model.thinking_budget`
works on both — it is translated to an effort level where budgets are no longer accepted.

An id with no exact entry defaults in two directions on purpose: the **request shape** assumes the
current generation, because sending a parameter a model has removed is a hard 400 while omitting an
optional one is not, and the **context window** stays conservative, because over-advertising a window
invites a request the model will reject.

Extended thinking is stateful once tools are involved: the provider signs each thinking block, and
a later turn replaying a tool call has to replay the signed reasoning that produced it. Thinking
blocks are captured off the response, persisted on the session event, and replayed ahead of the
`tool_use` blocks on the next request. A block whose signature was not captured is dropped rather
than sent, because an unverifiable signature rejects the whole turn.

### Where manifests come from

`FELIX_MANIFEST_SOURCE` picks the posture:

| Value | Resolution order | Writes |
|---|---|---|
| `store` (default) | tenant Postgres version → bundled YAML | `PUT /manifests`, canary, rollback |
| `bundled` | bundled YAML only | routes not mounted |

`bundled` is for a single-tenant or self-hosted deployment with no use for runtime
authoring. The write routes are never registered, so the verbs are absent from the app and
from `/openapi.json` rather than present and refusing, and no manifest store is constructed
at all. `felix doctor` reports which posture is active.

Two things to know before flipping an existing deployment:

- **Stored manifests stop being served.** Every tenant collapses onto the image's file, so
  any per-tenant `spec.auth.inbound` tightening — `required_scopes` in particular — is
  dropped. Eight of the nine bundled manifests are `allow_anonymous: true`.
- **`pin_compile` threads will 409 once.** The resolved version becomes `null` and the
  content hash becomes the bundled YAML's, which is drift by design.

### Manifest capabilities

Sessions and skills:

- **Skills** live under `skills/` as Agent Skills `SKILL.md` files; declare them with `spec.skills`.
  Bundled: `calculator-help`, plus the developer set used by the `contributor` manifest —
  `felix-architecture`, `felix-conventions`, `felix-testing`, `felix-contributing`
- **Session strategies**: `compacting` (token-threshold), `windowed:N`, `semantic:N`, `full_replay`
  — `compacting` sizes itself to the model's context window unless `spec.session.context_window_tokens` says otherwise, and compacts once more if the provider
  rejects a request for length anyway

A tool declares whether it may be re-run after a crash. A run that dies mid-tool leaves a call
with no result, and the harness cannot tell from the outside whether the effect happened, so the
call is closed out with an `[error/interrupted]` result before the thread resumes — without which
the provider rejects the whole transcript for an unanswered tool call. `replay_safe=True` tells the
model the call is safe to repeat; the default is that it is not, because re-running a search costs
latency while re-running a payment charges twice.

Outbound integrations, all declared on the manifest:

| Field | Binds to |
|---|---|
| `spec.mcp_servers` | HTTP or stdio MCP client → `server__tool` tools |
| `spec.peers` | A2A peers → `peer__name` tools |
| `spec.browser_tools` | Playwright (via the `browser` extra) |
| `spec.sandboxes` / `spec.containers` | Isolated execution |
| `spec.queues` | Redis list enqueue and dequeue |

> [!WARNING]
> **stdio MCP is disabled** unless `FELIX_MCP_STDIO_ALLOWED_COMMANDS` names the exact commands
> allowed. Manifest-supplied argv is arbitrary code execution.

Storage and execution:

- Large tool outputs spill via `spec.artifacts`
- Durable facts via `spec.memory.capture`; how-tos via `spec.procedural_memory`
- `spec.execution.mode: durable` enqueues a fiber (Temporal optional) and returns `202` with a
  `resume_token`
- Tool retrieval, semantic sessions, and procedural recall use embeddings when
  `felix-harness[embeddings]` is installed

## Documentation

User-facing documentation is published at **[docs.felix.run](https://docs.felix.run)** and authored
in the separate [`felix-run/web`](https://github.com/felix-run/web) repo.

Repository documentation for contributors lives in [`docs/`](docs/):

| Document | Purpose |
|---|---|
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | What to build next; status updated in place |
| [`docs/RELEASING.md`](docs/RELEASING.md) | Version bump, changelog, tag, and what CI does |
| [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) | Recurring failure modes and the actual fix |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
Report vulnerabilities through [SECURITY.md](SECURITY.md).

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Contributions are accepted under
the same license (Apache-2.0 §5).
