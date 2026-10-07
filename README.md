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
- **Durable execution** — fibers, steer and follow-up
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
make up               # migrates, then api :8080, worker, pgvector, Valkey (fs object store)
curl -s http://localhost:8080/health | jq
```

The stack applies Alembic migrations in a one-shot `migrate` service before the api,
worker and scheduler start, so there is no separate migration step for Compose.

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

`quick` is stateless. For an assistant that remembers across sessions, send the same request to
`assistant`: it captures durable facts from each turn, shows them to the next session, and has
`recall` / `remember` / `forget` tools (`forget` waits for an approval). Memory is shared across a
tenant rather than per caller, so `assistant` refuses anonymous requests — under `make dev`
(`FELIX_AUTH_MODE=none`) it answers 401; use the Compose stack and `$FELIX_KEY`.

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

Heavy optional dependencies (Playwright, sentence-transformers, DuckDB, Presidio) are
**never** in the default image. Install them through extras only when needed.

### Observability

`prometheus-client` is a core dependency, so `GET /metrics` works on a lean image with no
extras. Traces and OTLP logs need `felix-harness[otel]` and `FELIX_OTEL_ENABLED=true`.

```bash
make up-observability   # OTel Collector, Prometheus, Grafana, Jaeger, Loki, exporters
```

To export to a backend you already run instead, set the `FELIX_OTEL_*` variables in `.env`
(they reach every process through `compose.yml`) and build with `FELIX_DOCKER_EXTRAS=otel` —
no overlay, and nothing in Felix that names the destination. Spans carry the `gen_ai.*`
semantic-convention attributes, so an LLM-observability backend renders a chat as a
generation with model, tokens and cost. See
[`deploy/docker/README.md`](deploy/docker/README.md).

Grafana lands on <http://localhost:3000> with datasources provisioned and one dashboard
built from the counters Felix actually emits — including a governance row for the controls
[`deploy/GOVERNANCE.md`](deploy/GOVERNANCE.md) tells operators to watch. Model calls are
spans carrying the OpenTelemetry **GenAI semantic conventions**, so any OTLP backend renders
them as generations with token usage and cost rather than anonymous timing bars.

`/metrics` is deliberately **not** public: its label values include tenant-supplied manifest
ids and remote MCP tool names, so an anonymous scrape would disclose every tenant's manifests
and tools. `scripts/metrics-token.sh` mints a scrape key with no scopes — `/metrics` has no
scope gate, so it reads metrics and is refused everywhere else. In Kubernetes, set
`serviceMonitor.enabled=true` and point it at a Secret holding the same kind of key.

[docs/OBSERVABILITY.md](docs/OBSERVABILITY.md) is the metric catalog and span schema.

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
| `apps/worker` | Audit flush, scheduled jobs, memory consolidation, retention, anomaly scan, continuous eval, fiber resume, skill improvement and evaluation |
| `felix-scheduler` | Enqueues labeled Taskiq cron tasks — **required alongside the worker**, or nothing periodic fires |
| `packages/ai` | Model layer: wire formats, catalog, turn types. Imports nothing from `felix` |
| `packages/harness` | Manifests, patterns, tools, session, governance, auth, plugins |
| `packages/cli` | `felix migrate \| eval \| mint-jwt \| login \| ingest-docs \| bundle-manifests \| validate-manifest \| doctor \| version` |
| `manifests/` | Bundled agents: `quick`, `assistant`, `deep`, `router`, `oss-only`, `hybrid-router`, `support`, `decider-support`, `cowork`, `governed`, `contributor`, `triage` |

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
- Retention: the worker's nightly sweep prunes `audit_events`, `usage_events`, finished `fibers`
  and `a2a_tasks`, and (off by default) idle session threads — `FELIX_AUDIT_RETENTION_DAYS` (30),
  `FELIX_USAGE_RETENTION_DAYS` (365), `FELIX_FIBER_RETENTION_DAYS` (7), `FELIX_SESSION_RETENTION_DAYS`
  (0 = keep), `FELIX_APPROVAL_RETENTION_DAYS` (0 = keep, and only *settled* approvals — a grant
  that can still authorize is never swept), `FELIX_ATTACHMENT_RETENTION_DAYS` (0 = keep; the one
  sweep that deletes *bytes* as well as rows, since `attachments/` is an object-store prefix
  nothing else collects), `FELIX_ARTIFACT_RETENTION_DAYS` (30; spilled tool outputs, bytes and
  ledger row, the same way — on by default because spill is the harness's working copy); a manifest's `governance.retention_days` shortens the audit TTL for its
  own rows
- Uploads: `FELIX_ATTACHMENTS_MAX_BYTES_PER_TENANT` (256 MiB, 0 = no ceiling) bounds what one
  tenant may store through `/files`, on top of the 600 KiB per-upload cap. Over the ceiling
  answers 409. On the default `fs` store this shares a disk with artifact spill and manifest
  storage, so the ceiling is what keeps one tenant from degrading every other one

**Sizing.** Each worker process carries its own connection pool, so raise the two together:
`FELIX_WORKERS` (1) and `FELIX_DB_POOL_SIZE` (10) + `FELIX_DB_MAX_OVERFLOW` (20) — past that
ceiling requests queue for `FELIX_DB_POOL_TIMEOUT_SECONDS` and then fail. Set
`FELIX_DB_POOL_PRE_PING=false` against a direct Postgres; it costs a round trip per checkout and
only earns it behind PgBouncer, RDS Proxy, or Cloud SQL. Connections give up after
`FELIX_DB_CONNECT_TIMEOUT_SECONDS` (10) and every connection is `application_name=felix-<role>`
in `pg_stat_activity`. A statement timeout belongs on the role (`ALTER ROLE felix SET
statement_timeout`), not in the client — a client-side one does not survive a pooler; `felix
doctor` reports what applies. On SIGTERM a worker gets `FELIX_GRACEFUL_SHUTDOWN_SECONDS` (120,
the Helm chart's grace period) to finish in-flight requests before it is killed.

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
| `register_decision_provider` | `FELIX_DECISION_ROUTES` |
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
`FELIX_SKILLS_DIR`, or upload one per tenant to the object store. A manifest with
`spec.skill_authoring.enabled` lets its agent draft skills into the tenant's library; a draft
enters no catalog until it is published, and publishing is gated — a failing security scan always
blocks, `FELIX_SKILL_PUBLISH_MIN_QUALITY` (0 = off) sets a floor on the 0-100 review score, and
`FELIX_SKILL_PUBLISH_BLOCK_ON_ADVISORY` (false) also refuses an advisory scan. Operators review
the queue, author, publish, roll back and archive through `/skill-library` (`skills:read` to
read, `skills:write` to change). The agent can also file feedback on a library skill
(`submit_skill_feedback`), as can an operator. A person's accept is the only thing that has the
worker rewrite the skill from it, with `FELIX_SKILL_IMPROVE_MODEL` (empty = the default route),
and the rewrite is a draft for review. `POST /skill-library/{name}/versions/{v}/eval` scores a
version against a baseline without it: `FELIX_SKILL_EVAL_MODEL` answers each scenario with and
without the skill, `FELIX_SKILL_EVAL_JUDGE_MODEL` scores both answers 0-100, and
`FELIX_SKILL_EVAL_MAX_SCENARIOS` (10) caps the run. For an agent-written version only an evaluation on
the bundle's own `evals/` scenarios counts. `FELIX_SKILL_PUBLISH_REQUIRE_EVAL` and
`FELIX_SKILL_PUBLISH_MIN_EVAL_UPLIFT` add an evaluation requirement for the whole deployment.
`PATCH /skill-library/-/policy` lets a tenant tighten every bar, never loosen it. Skill jobs are
bounded per call (`FELIX_SKILL_EVAL_MAX_TOKENS`, `FELIX_SKILL_IMPROVE_MAX_TOKENS`), per job
(`FELIX_SKILL_JOB_DEADLINE_SECONDS`) and per tenant (`FELIX_SKILL_JOBS_MAX_QUEUED`,
`FELIX_SKILL_JOBS_DAILY_LIMIT`).

Skills published on GitHub come in the same way: `GET /skill-library/-/browse?source=github:owner/repo`
lists the skills a repository offers (`skills/*`, `.claude/skills/*`, Claude Code plugin layouts and
the other common roots) at one resolved commit, and `POST /skill-library/-/import` (or
`felix skills add github:owner/repo/path`) fetches one as a draft, pinned to a commit of the
repository's own history, with its source, ref, commit and license on the version. An import is
never published in the same request: a person reads the draft, then publishes it. A re-import
saves a new version only when the skill's files changed. Imported text is third-party
instructions, so it -- and every version built on it -- is held to a stricter gate (an advisory
scan blocks its publish whatever the policy says), and what `activate_skill` returns of it is
screened as untrusted tool output where the manifest enables content screening.
`FELIX_SKILL_IMPORT_SOURCES` (empty = any GitHub source) limits which repositories may be named,
per tenant (`acme=github:acme/*`); `FELIX_SKILL_IMPORT_GITHUB_TOKEN` (optional) reaches private
ones and lifts the anonymous rate limit, and outside a development box refuses to boot unless every
allowlist entry names a tenant and a literal owner. `FELIX_SKILL_IMPORT_MIN_AGE_DAYS` (0 = off) is
a supply-chain cooldown counted from when the tenant first saw a skill's exact files (never from a
commit date, which the pusher sets): until then an import is refused outright, and a tenant can
raise the bar (`import_min_age_days` in its policy) but not lower it. Every GitHub call is charged
against `FELIX_SKILL_IMPORT_CALLS_PER_HOUR` (per tenant, 500) and
`FELIX_SKILL_IMPORT_CALLS_PER_HOUR_TOTAL` (4000).

An imported skill is checked against its origin with `GET /skill-library/{name}/-/upstream`
(`felix skills diff`): the commit its ref names now, whether the files moved, when the cooldown
lets them in, and a per-file diff against the live version. `POST /skill-library/{name}/-/update`
(`felix skills update`) re-imports it as a new draft, never published; `GET /skill-library/-/upstream`
(`felix skills outdated`) lists every imported skill's state, 25 at a time.
Once a person has read an import and vouches for it, `POST /skill-library/{name}/versions/{v}/adopt`
with a `reason` (`felix skills adopt <name> <v> --reason ...`, `skills:write`) saves its files
unchanged as a new operator draft that is no longer held to the import gate or screened as
third-party text; the earlier versions keep their mark, the draft is published the ordinary way,
and the skill stops following its origin.
`FELIX_SKILL_IMPORT_CHECK_HOURS` (0 = off, up to 168) has the worker check them on that cadence,
spending at most half of each budget, so the listing and the library detail can answer without
asking GitHub. `FELIX_SKILL_UPDATE_WEBHOOKS` (`acme=ops,acme=ci`, each id a
`FELIX_WEBHOOK_ENDPOINTS` endpoint open to that tenant; no wildcard) announces each new upstream
digest once as a signed `skill.update_available` event -- metadata only, sent by the worker with
the completion webhooks' signing and retries, never from the check itself. Give the API and the
worker the same `FELIX_SKILL_UPDATE_WEBHOOKS` and `FELIX_WEBHOOK_ENDPOINTS`: both run checks, and
only the worker sends.

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
| Session lease | `POST /chat/sessions/lease`, `…/lease/release`, `GET /chat/sessions/{id}/lease` |
| Session name / label / export | `POST /chat/sessions/name`, `…/label`, `GET …/export` |
| Compact / UI prompt | `POST /chat/compact`, `POST /chat/ui` |
| Session fork / rewind | `POST /chat/fork`, `POST /chat/rewind` |
| OpenAI-compatible | `POST /v1/chat/completions`, `GET /v1/models` |
| A2A JSON-RPC | `POST /a2a` |
| MCP | `POST /mcp` |
| Agent card | `GET /.well-known/agent-card.json` |
| Liveness / readiness | `GET /live` (also `/health`), `GET /ready` |
| Metrics | `GET /metrics` — **authenticated**, see below |

**Felix serves an API, not a web UI, and browsers on another origin cannot call it directly, by
design.** The chat UI and docs live in felix-run/web; `test_invariants.py` fails on any static
file, template or mounted sub-app here. There is no CORS layer.
A preflight `OPTIONS` gets a 401 (or a 405 under `auth_mode=none`) with no
`Access-Control-Allow-*` headers, so the browser blocks the request before it is sent. A plain
`GET` does reach Felix, but the page is not allowed to read the answer. Felix has no browser
login of its own (no cookies), so a page could only call it by holding a bearer token or API key,
where any script on that page can read it. Put a server-side proxy on the page's own origin in
front of Felix instead: it holds the credential and forwards `/api/*` to the API.
felix-web's chat UI does this from a Worker (`FELIX_ORIGIN`, with an optional `x-chat-key` gate
for its own clients), and any reverse proxy that adds the `Authorization` header works the same
way. Non-browser clients — the CLI, `felix-client`, curl, other services — are unaffected.

A failed send is safe to resend: `POST /chat` and `POST /chat/stream` take an `Idempotency-Key` header, and a resend under the same key never runs a second turn — `/chat` returns the stored response, `/chat/stream` replays what the first request wrote to its thread (see [deploy/GOVERNANCE.md](deploy/GOVERNANCE.md)). Session leases are advisory by default; `FELIX_LEASE_ENFORCE=strict` refuses a driving request that presents no `X-Felix-Lease-Token` while another client holds the thread.

A dropped stream is recoverable: structural SSE frames carry an `id:` cursor (token-level frames do not, which per the SSE spec leaves the client's `lastEventId` on the last one it saw), and `GET /chat/stream/{thread_id}` replays what was missed (or opens with a `snapshot` frame) and then tails the thread. The run itself is still torn down on disconnect, so what you get back is the thread, not the abandoned turn.

Management surfaces: `/audit`, `/approvals`, `/plans`, `/jobs`, `/manifests`, `/eval`, `/usage`, `/memory`, `/skill-library`. `POST /jobs/{name}/run` runs a job now instead of waiting for cron; `GET /manifests/{name}/versions` lists what a rollback can go back to. `/memory` lists, searches (the same hybrid ranking the agent sees), time-travels (`/memory/as-of/{turn_seq}`), writes and forgets long-term memories — an agent that remembers across sessions otherwise accumulates a store nobody can inspect.

Python client (**experimental**): the `felix-client` package — `from felix_client import
FelixClient` — covering chat (`prompt`, `stream`, `steer`, `follow_up`, `fork`, `rewind`,
`set_model`), durable runs with their polling, and approvals. It depends on httpx and nothing in
Felix, so installing it does not install the server; its surface may change between releases
without a deprecation period. Not yet on PyPI — install it from the repository:
`pip install "felix-client @ git+https://github.com/felix-run/felix#subdirectory=packages/client"`.
For everything else, and as the contract, use the HTTP API: each release attaches its
`openapi.json`, from which a client in any language can be generated. `from felix.sdk import
FelixClient` still works inside the harness.

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
| `workers_ai` | `api.cloudflare.com/…/accounts/{account_id}/ai/v1` | `api_key`, `account_id`, optional `gateway_id` |
| `groq` `together` `deepseek` `cerebras` `fireworks` `openrouter` `xai` `mistral` `google` | each vendor's OpenAI-compatible endpoint | `api_key` |

Everything past the first two is configured through `FELIX_MODEL_PROVIDER_OPTIONS` rather
than a settings field per vendor. Each is also selectable as `FELIX_MEMORY_EMBEDDER`, since
`/embeddings` is part of the same wire format.

**The hosted tier ships without per-token rates, deliberately, except Workers AI.** Felix
does not invent prices: an unpriced model contributes zero to spend and a manifest that
*declares* `limits.max_cost_usd` on one is refused at compile, pointing at `spec.model.price`.
Guessing is how every unrecognised model came to be billed at Claude Sonnet's $3/$15 per Mtok.
Workers AI is the exception because Cloudflare publishes a per-token rate for each model (it
meters in neurons and states the conversion), so the catalog carries those rates for the
models behind the `-cf` routes below and a few more, under `@cf/` keys, and a spend cap holds on
them. Any other `@cf/` id stays unpriced. The 10,000 free neurons a day are not subtracted, so
spend reads high rather than low.

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
| `glm-5.3-cf` | workers_ai | `@cf/zai-org/glm-5.3` (1M, text only) |
| `glm-5.3-flash-cf` | workers_ai | `@cf/zai-org/glm-5.3-flash` (1M, vision) |
| `deepseek-v4-pro-cf` / `deepseek-v4-flash-cf` | workers_ai | `@cf/deepseek-ai/deepseek-v4-pro-0813` / `@cf/deepseek-ai/deepseek-v4-flash-0731` (1M, text only) |
| `qwen3.8-27b-cf` | workers_ai | `@cf/qwen/qwen3.8-27b` (262K, vision) |
| `kimi-k2.7-code-cf` | workers_ai | `@cf/moonshotai/kimi-k2.7-code` (262K, vision) |
| `kimi-k2-cf` | workers_ai | `@cf/moonshotai/kimi-k2.6` (262K, vision) |
| `gpt-oss-120b-cf` / `gpt-oss-20b-cf` | workers_ai | `@cf/openai/gpt-oss-120b` / `@cf/openai/gpt-oss-20b` (text only) |
| `glm-flash-cf` | workers_ai | `@cf/zai-org/glm-4.7-flash` (text only) |
| `llama-3-pro` / `llama-3-fast` | workers_ai | legacy ids, now `@cf/zai-org/glm-5.3` / `@cf/zai-org/glm-5.3-flash` |

A turn that carries an image and is bound for a model the catalog marks text-only goes to
`spec.model.vision_model`, or `FELIX_DEFAULT_VISION_MODEL_ID` when the manifest names none — so a
cheap text route can stay the default and a picture still gets looked at. The image stays in the
thread's history, so every later turn of that thread also goes to the vision route, until compaction
or a window drops it. The vision route fails over to whichever of `spec.model.fallbacks` can see -- a text-only
fallback is skipped -- and has no escalation of its own. With neither set, the
request is refused with a 422 naming the route instead of reaching a model that would answer that
it cannot see. A custom route declares what it accepts with a `modalities` key, which the catalog
cannot know for an arbitrary model:
`{"vision":{"provider":"workers_ai","model":"@cf/meta/llama-3.2-11b-vision-instruct","modalities":["text","image"]}}`.
A model the catalog cannot vouch for either way is sent the image as before.

Every `-cf` route supports tool calling and is priced, so `limits.max_cost_usd` is enforced on
it. GLM-5.3, GLM-5.3 Flash, DeepSeek V4 and Kimi K2.7 Code need the Workers Paid plan (or AI
Gateway credits). `llama-3-pro` and `llama-3-fast` used to route to a local Ollama, which Felix no
longer ships, and are kept so manifests naming them still resolve.
`@cf/meta/llama-3.3-70b-instruct-fp8-fast` is priced too but has no default route: Workers AI serves
it with a 24K window, smaller than an agent's prompt with a skill catalogue.

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
`output_config.effort`, while pre-4.6 models take a fixed `budget_tokens`. A thinking level is sent as
the level's budget where a model takes one, and as an effort where it takes that instead — minimal
and low as `low`, then `medium`, `high`, `xhigh` and `max` by name, with `xhigh` clamped to `high` on a
model without that tier, and everything above `high` sent as `high` for OpenAI's `reasoning_effort`.
A `spec.model.thinking_budget` with no level works on both: where an effort is needed it takes the
effort of the highest level whose budget it reaches (1,024 → `medium`, 2,048 → `high`, 8,192 →
`xhigh`, 32,000 → `max`).

An id with no exact entry defaults in two directions on purpose: the **request shape** assumes the
current generation, because sending a parameter a model has removed is a hard 400 while omitting an
optional one is not, and the **context window** stays conservative, because over-advertising a window
invites a request the model will reject.

Extended thinking is stateful once tools are involved: the provider signs each thinking block, and
a later turn replaying a tool call has to replay the signed reasoning that produced it. Thinking
blocks are captured off the response, persisted on the session event, and replayed ahead of the
`tool_use` blocks on the next request. A block whose signature was not captured is dropped rather
than sent, because an unverifiable signature rejects the whole turn.

#### Decision models

Some model calls make a decision rather than write text — which tool fits this request, which
sub-agent should take it, whether a reply meets a criterion. A **decision model** answers those
as typed questions (`Choice`, `Score`, `Noul`, the vocabulary of `felix_ai.decide`) and returns
calibrated probabilities with a confidence, instead of prose to parse. They are routed separately
from chat models, by `FELIX_DECISION_ROUTES`, and share credentials with the model provider of
the same name in `FELIX_MODEL_PROVIDER_OPTIONS`:

| Logical id | Provider | Wire model | Configured with |
|---|---|---|---|
| `clef` | `workers_ai` (`…/accounts/{account_id}/ai/run/@cf/cloudflare/clef`) | `@cf/cloudflare/clef` | `api_key`, `account_id`, optional `gateway_id` |
| `clef-flash` | `workers_ai` (`…/ai/run/@cf/cloudflare/clef-flash`) | `@cf/cloudflare/clef-flash` | as `clef` |
| `jev` | `typesafe` (`api.typesafe.ai/v1/systemone`) | `jev-latest` | `api_key` |
| `jev-cf` | `workers_ai` (`…/accounts/{account_id}/ai/run`) | `typesafe/jev` | `api_key`, `account_id`, optional `gateway_id` |

Clef is Cloudflare's open-weight (Apache-2.0) decision model, Jev-API compatible, with a 64K
context: $0.24 per million input tokens, or $0.09 for the faster `clef-flash`, output free. It
shares the Workers AI credential the chat routes already use, so a deployment on Workers AI needs
nothing new. Jev is TypeSafe's, priced at $0.042 per million input tokens with output free.
The `llm` provider answers the same questions with any chat route —
`FELIX_DECISION_ROUTES={"haiku-decider":{"provider":"llm","model":"claude-haiku"}}` — so nothing
depends on a second vendor; it reports its pick with no confidence, because a chat model's
self-assessed certainty is not calibrated. Every decision is metered like a model turn and counts
against `limits.max_cost_usd`.

A manifest names its decider once, under `spec.decider`, and each consumer opts in —
`manifests/decider-support.yaml` switches on every one that fits a support agent, and is the one
to copy from:

```yaml
spec:
  decider: {id: jev, min_confidence: 0.5}
  tools_retrieval: {enabled: true, top_k: 12, decider: true}
```

A `router` that names a decider uses it to pick the sub-agent — one `Choice` over the
`sub_agents`, with the router's system prompt as the instructions — and classifies with its model
only when the decider is unsure or unavailable. `model.confidence_escalation.decider: true` asks
the decider whether a reply actually answers the request, instead of escalating on reply length and
phrases like "unclear"; a probability below `min_confidence` escalates. It applies to `react` and
`deep`, whose loop builds the model the decider reaches; side requests such as compaction summaries
keep the heuristic.

Judges ask the decider too. A `guardrails.judges` rule with `decider: true` — on tool output or on
the final reply — is scored by the decider's probability that the text meets its `criteria`, read
as written, so a negative criterion like "must not leak credentials" needs no `assert_absent:`
prefix; `reflect.decider: true` verifies drafts the same way, and an eval rubric names a decider
with `judge_decider: <route>`. Each falls back to its model judge, then its heuristic.

`skill_suggestion: {enabled: true}` suggests the skill a request needs: the decider ranks the
catalog, reranks a shortlist of three against each skill's body, and — when the request asks for a
task and a skill fits — adds a one-line hint naming it. The model still decides whether to
`activate_skill`. The hint is a *transient* message: sent last on the turn's first model call,
after the prompt-cache breakpoint, and never written to the session, so it costs the cached
conversation nothing. It pays off on large catalogs; with a handful of skills the model chooses
well on its own.

`content_screening.decider: true` adds the decider to injection screening: one call asks whether
the text tries to override the assistant's instructions, jailbreak it, or exfiltrate data. It runs
beside the markers and `content_screening.model` rather than instead of them — either flagging
flags, either unavailable leaves the text unscreened — because a small classifier is not
adversarially robust. See `deploy/GOVERNANCE.md`.

With `tools_retrieval.decider`, one `Choice` over the tool catalogue per user turn picks the
shortlist the model sees, in place of embedding similarity. When the decider errors, or the
shortlist holds less than `min_confidence` of the probability mass, selection falls back to
embeddings or keywords as before. An id missing from `FELIX_DECISION_ROUTES`, or a `typesafe`
route with no key, fails the compile.

What leaves the deployment: the latest user message (up to 4,000 characters) and the one before
it (1,000), plus each tool's name and the first 200 characters of its description, go to the
decider's provider — TypeSafe or Cloudflare — unmasked; with `confidence_escalation.decider`, so
does the model's reply (up to 4,000 characters), which may quote tool output. Treat enabling a decider as adding that
provider as a processor of user input. A tool description can also steer the ranking (an MCP
server describing its tool as "always choose me"); that biases which tools are offered, and every
offered tool is still governance-wrapped.

### Where manifests come from

`FELIX_MANIFEST_SOURCE` picks the posture:

| Value | Resolution order | Writes |
|---|---|---|
| `store` (default) | tenant Postgres version → bundled YAML | `PUT /manifests`, canary, rollback |
| `bundled` | bundled YAML only | routes not mounted |

An activation, a rollback or a canary change takes effect at once on the process that made it.
Each API replica and worker caches a manifest's active version for 30 seconds, so the others
follow within that window — a rollback is not instant fleet-wide, and a canary split briefly
differs between replicas.

`bundled` is for a single-tenant or self-hosted deployment with no use for runtime
authoring. The write routes are never registered, so the verbs are absent from the app and
from `/openapi.json` rather than present and refusing, and no manifest store is constructed
at all. `felix doctor` reports which posture is active — and, outside development, whether a
claim-mode JWT verifier pins `FELIX_ALLOWED_TENANTS`, whether the OTLP exporter is private or
TLS, whether prompts are kept out of spans, and whether the database schema is at the code's
Alembic head.

Two things to know before flipping an existing deployment:

- **Stored manifests stop being served.** Every tenant collapses onto the image's file, so
  any per-tenant `spec.auth.inbound` tightening — `required_scopes` in particular — is
  dropped. Seven of the eleven bundled manifests are `allow_anonymous: true`.
- **`pin_compile` threads will 409 once.** The resolved version becomes `null` and the
  content hash becomes the bundled YAML's, which is drift by design.

### Manifest capabilities

Sessions and skills:

- **Skills** live under `skills/` as Agent Skills `SKILL.md` files; declare them with `spec.skills` (which *adds to* the bundled and `FELIX_SKILLS_DIR` catalogue; set `spec.skills_declared_only: true` to make the declared names the whole set).
  Bundled: `calculator-help`, plus the developer set used by the `contributor` manifest —
  `felix-architecture`, `felix-conventions`, `felix-testing`, `felix-contributing`
- **Session strategies**: `compacting` (token-threshold), `windowed:N`, `semantic:N`, `full_replay`
  — `compacting` sizes itself to the model's context window unless `spec.session.context_window_tokens` says otherwise, and compacts once more if the provider
  rejects a request for length anyway. When the kept window starts mid-turn, that turn's opening
  user message is kept verbatim (up to 16,000 characters) and its earlier steps get a separate,
  smaller summary, so the request a long turn is working on is never reduced to a paraphrase

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
| `spec.image_tools` | Pillow resize, crop, rotate, convert, thumbnail, info and list (via the `image` extra) |
| `spec.sandboxes` / `spec.containers` | Isolated execution |
| `spec.shell_tools` | Allowlisted argv on the API host, in the workspace checkout — no shell interpreter |
| `spec.queues` | Redis list enqueue and dequeue |

> [!WARNING]
> **stdio MCP is disabled** unless `FELIX_MCP_STDIO_ALLOWED_COMMANDS` names the exact commands
> allowed, and **shell tools are disabled** unless `FELIX_SHELL_ALLOWED_COMMANDS` names the argv
> prefixes allowed. Manifest-supplied argv is arbitrary code execution. A shell tool's command
> runs as the API's user and can read the API's environment unless `FELIX_SHELL_RUNNER_URL` points
> it at a `felix-shell-runner` (with `FELIX_SHELL_RUNNER_TOKEN`), which the builder stack runs in
> a container holding no secrets — see [deploy/GOVERNANCE.md](deploy/GOVERNANCE.md#shell-tools).

Structured output — `spec.output_schema` is a JSON Schema the agent's answer must match, and the
model provider is what enforces it rather than the prompt:

```yaml
spec:
  output_schema:
    type: object
    properties:
      answer: {type: string}
      confidence: {type: number}
    required: [answer, confidence]
    additionalProperties: false
```

`message.content` is then a JSON document on every provider. Tools still work — it is the turn
that answers in text that is constrained, not the turns that call a tool on the way there. OpenAI
gets `response_format`, strict when the schema closes every object and requires every property
(the only setting under which the shape is *guaranteed*; the drop to non-strict is logged, and
`strict` goes only to endpoints whose provider row declares it, since it is an OpenAI extension
that eleven other providers share this wire without). Anthropic gets native structured outputs
(`output_config.format`) on the models that have them — Fable, Mythos and Opus 5.x, Sonnet 5.x,
Opus 4.8, Haiku 4.5 — which hold with extended thinking on, provided the schema is inside their
subset: every object closed with `additionalProperties: false`, and no numeric, string-length,
`pattern` or array-size constraints. Otherwise the schema becomes a tool the model must call,
folded back into the reply. Where that cannot be forced — extended thinking on, or Fable 5.1,
Mythos 5.1, Opus 5.5 and Sonnet 5.5, which refuse a forced tool choice — the schema is only
offered, and the log names why.

Supported on `pattern: react` and `pattern: deep`. The composite patterns — `router`,
`parallel`, `groupchat`, `reflect`, `plan_execute` — compose their answer in a turn that takes
no options yet, so a manifest declaring `output_schema` on one of those is **refused at compile**
rather than quietly answering in prose. A pattern registered by a plugin opts in with
`register_pattern(..., honours_output_schema=True)`.

A caller can ask for a shape per request too: `POST /v1/chat/completions` accepts OpenAI's
`response_format: {type: json_schema, json_schema: {schema: …}}`, so an OpenAI SDK works
unchanged. A manifest that declares `spec.output_schema` overrides it — an agent published with an
answer contract keeps answering to it.

Images, on `/chat` and on `/v1/chat/completions`, in OpenAI's content-parts shape:

```json
{"role": "user", "content": [
  {"type": "text", "text": "what is in this picture?"},
  {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}}
]}
```

A `data:` URL is sent to the provider as inline bytes — OpenAI takes the URL verbatim, Anthropic
gets a `base64` source, since it has no URL form for inline data — and an `https://` URL is
passed through as a URL for the provider to fetch. A request is bounded by the 1 MiB body limit;
there is no upload endpoint yet, so an image arrives with the message that uses it.

Storage and execution:

- Large tool outputs spill via `spec.artifacts`: the model gets a preview and pages through the
  rest with `read_artifact`, sized by `default_window_chars` / `max_window_chars`
- Durable facts via `spec.memory.capture`; how-tos via `spec.procedural_memory`
- `spec.execution.mode: durable` enqueues a fiber and returns `202` with a
  `resume_token`; a step that keeps failing backs off and is `dead` after `FELIX_FIBER_MAX_ATTEMPTS`.
  Each worker polls for due fibers every `FELIX_FIBER_POLL_SECONDS` (1.0; 0 leaves only the
  once-a-minute `fiber_scheduler` sweep) and advances up to `FELIX_FIBER_CONCURRENCY` (8) at once,
  so a new run starts within about a second and one parked on an approval holds up no other
- `spec.execution.webhooks: [ops]` announces a durable run's end to operator-registered endpoints
  (`FELIX_WEBHOOK_ENDPOINTS`, a JSON map of id → `{url, secret, tenants, private?}`): the worker
  POSTs `run.completed|failed|expired|dead` with the run view, signed per Standard Webhooks
  (`webhook-id`, `webhook-timestamp`, `webhook-signature: v1,…`), retries with backoff up to
  `FELIX_WEBHOOK_MAX_ATTEMPTS` (8) with each attempt bounded by `FELIX_WEBHOOK_TIMEOUT_SECONDS`
  (10, at most 60), and reports each endpoint's state on `GET /chat/runs/{token}`.
  A manifest names ids, never URLs; an id not registered for the caller's tenant is `422`
- Web Push wakes a subscribed browser -- an installed phone app, a backgrounded tab -- when a new
  approval or an agent's question is waiting on a person (`/push/*`, `approvals:read`). Off until
  `FELIX_PUSH_VAPID_PRIVATE_KEY` and `FELIX_PUSH_VAPID_SUBJECT` are set; endpoints are limited to
  `FELIX_PUSH_ALLOWED_HOSTS`, and a push carries the kind of wait and the thread, never the call
- `POST /chat/stream` on a durable manifest streams the run instead: `run_accepted` → `run_status`
  → `final`, interleaved with `session_event` frames tailed from the thread's session log, so tool
  calls and assistant turns arrive as they land, across replicas, with a resumable `id:` cursor.
  Completed messages only — token deltas are never persisted, so a durable run never streams them
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
| [`docs/BACKUP.md`](docs/BACKUP.md) | What holds state, how to back it up, and the restore drill |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
Report vulnerabilities through [SECURITY.md](SECURITY.md).

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Contributions are accepted under
the same license (Apache-2.0 §5).
