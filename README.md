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
| `packages/harness` | Manifests, patterns, tools, session, governance, auth, plugins |
| `packages/cli` | `felix migrate \| eval \| mint-jwt \| bundle-manifests \| validate-manifest \| doctor \| version \| temporal-worker` |
| `manifests/` | Bundled agents: `quick`, `deep`, `router`, `oss-only`, `hybrid-router`, `support`, `cowork`, `governed` |

### Vendor independence

Felix is **service- and cloud-agnostic**: the harness talks to Postgres, a cache, and an object store
through Protocols, not a single vendor SDK.

**AWS and GCP are first-class** — S3, Secrets Manager, GCS, and Secret Manager via the optional
`felix-harness[aws]` and `felix-harness[gcp]` extras. Small VMs can use `FELIX_OBJECT_STORE=fs` with
zero cloud SDKs.

- Set `FELIX_OBJECT_STORE` to `s3`, `gcs`, `fs`, or `memory`
- Set `FELIX_SECRETS_BACKEND` to `env`, `file`, `aws`, or `gcp`
- Deploy notes: [`deploy/aws/`](deploy/aws/), [`deploy/gcp/`](deploy/gcp/)
- Manifest secrets, plus opt-in SOC 2 and EU AI Act mapping: [`deploy/GOVERNANCE.md`](deploy/GOVERNANCE.md)
- Helm: enable `persistence` when using `fs`, so `/data` survives restarts
- Production JWT and api_key deploys need `FELIX_CONSUMER_SHARED_SECRET` for `POST /internal/*`

Felix runs on infrastructure **you** operate. Cloudflare DNS, CDN, TLS, and WAF in front of your
origin are fine. There is **no** Cloudflare Workers, Durable Objects, Hyperdrive, R2-as-binding,
Queues, or Workflows compute in this stack.

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

Management surfaces: `/audit`, `/approvals`, `/plans`, `/jobs`, `/manifests`, `/eval`.

Python client: `from felix.sdk import FelixClient` — `prompt`, `stream`, `steer`, `follow_up`,
`fork`, `rewind`, `set_model`.

### Models

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

### Manifest capabilities

Sessions and skills:

- **Skills** live under `skills/` as Agent Skills `SKILL.md` files; declare them with `spec.skills`
- **Session strategies**: `compacting` (token-threshold), `windowed:N`, `semantic:N`, `full_replay`
  — `compacting` sizes itself to the model's context window unless `spec.session.context_window_tokens` says otherwise

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
