# Felix

[![CI](https://github.com/felix-run/felix/actions/workflows/ci.yml/badge.svg)](https://github.com/felix-run/felix/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Felix** is a self-hostable managed **agents harness**. You author agents as
YAML manifests (`apiVersion: felix/v1`); Felix compiles them into governed
agents with durable fibers, memory, skills, eval, approvals, and sandboxes —
served over OpenAI, A2A, MCP, and SSE. Fork, rewind, and steer live runs.
Deploy with Docker, Helm, AWS, or GCP on infrastructure you operate.

Docs: [docs.felix.run](https://docs.felix.run) ·
Web UI: [github.com/felix-run/web](https://github.com/felix-run/web)

## What you get

- **Manifests** — `felix/v1` YAML; bundled agents in `manifests/`
- **Governance** — auth, approvals, audit, usage meters
- **Durable execution** — fibers (Temporal optional), steer / follow-up
- **Session control** — fork, rewind, compacting / windowed / semantic
- **Memory and skills** — durable facts, procedural memory, Agent Skills
- **Surfaces** — REST/SSE, OpenAI-compatible `/v1`, A2A, MCP
- **Eval** — datasets, fixtures, `--mock` CI path
- **Deploy** — lean Docker, Helm, AWS, or GCP

## Quick start

```bash
cp .env.example .env
# set POSTGRES_PASSWORD (and MINIO_ROOT_PASSWORD only if using --profile full):
#   openssl rand -hex 32

make install          # lean core + dev (small VMs / CI)
make up               # api :8080, worker, pgvector, Valkey (fs object store)
# make up-lite        # tighter memory caps for ~2–4 GiB hosts
# make up-full        # + MinIO + aws extra (FELIX_DOCKER_EXTRAS=aws)
make migrate
curl -s http://localhost:8080/health | jq
```

For cloud SDKs / embeddings / browser locally: `make install-full`.

Chat against the bundled `quick` manifest (anonymous allowed):

```bash
curl -s -X POST http://localhost:8080/chat \
  -H 'content-type: application/json' \
  -d '{"manifest":"quick","messages":[{"role":"user","content":"What is 7 * 6?"}]}' | jq
```

Or use the OpenAI-compatible surface (`model` = manifest name):

```bash
curl -s http://localhost:8080/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"quick","messages":[{"role":"user","content":"hi"}]}' | jq
```

Local DX without Compose:

```bash
make install
make migrate
make dev         # Granian on :8080 with FELIX_AUTH_MODE=none, FELIX_OBJECT_STORE=fs
make cli         # httpx REPL client
make check       # ruff + ty + pytest
```

### Small VMs / lean Docker

Default images and Compose stay **lean**:

| Concern | Default | Full / cloud |
|---------|---------|--------------|
| Object store | `FELIX_OBJECT_STORE=fs` (local dir) | `s3` / `gcs` + `felix-harness[aws\|gcp]` |
| Image extras | none | `FELIX_DOCKER_EXTRAS=aws,gcp` |
| Compose | api + worker + Postgres + Valkey | `--profile full` adds MinIO |
| Memory | compose `mem_limit` caps | raise via `FELIX_*_MEM_LIMIT` |

```bash
make up-lite   # deploy/docker/compose.lite.yml — ~2–4 GiB hosts
make up-gcp    # GCE / public VM: no DB/cache host ports
```

Docker packaging lives under `deploy/docker/` (see that README). Always run Compose
from the repo root (`make up` sets `--project-directory .`).

Heavy optional deps (Playwright, sentence-transformers, DuckDB, Presidio, Temporal)
are **never** in the default image — install via extras only when needed.

### Analytics warehouse (locked)

Postgres is the system of record. The warehouse is optional append-only spill
for audit / eval analytics (worker flush after Postgres write).

| Choice | When | Extra |
|--------|------|-------|
| `none` (lean default) | No analytics spill | — |
| **`duckdb` (recommended)** | Small VMs, embedded file under `FELIX_DATA_DIR/warehouse` | `warehouse` |
| `clickhouse` | High-volume audit / events scale-out | `warehouse-clickhouse` |
| `doris` | Already operating Apache Doris / MySQL-protocol BI | `warehouse-doris` |

```bash
uv sync --extra warehouse
# or: make install-warehouse
# FELIX_WAREHOUSE=duckdb
# Docker: FELIX_DOCKER_EXTRAS=warehouse FELIX_WAREHOUSE=duckdb
```

## Architecture

```
Client → Ingress (Caddy / Traefik / nginx / Cloudflare DNS+CDN)
           ├─ felix-api        (CPython 3.14, Granian, FastAPI)
           ├─ felix-worker     (Taskiq consumer)
           └─ felix-scheduler  (Taskiq cron enqueue)
                  │
     Postgres+pgvector · Valkey · object store (fs | S3 | GCS)
```

- **`apps/api`** — HTTP: `/chat`, `/v1`, `/a2a`, `/mcp`, management APIs, OpenAPI
- **`apps/worker`** — background consumer: audit flush, scheduled jobs, memory consolidation, retention, anomaly, continuous eval, fiber resume
- **`felix-scheduler`** — enqueues labeled Taskiq cron tasks (required alongside the worker)
- **`packages/harness`** — manifests, patterns, tools, session, governance, auth, plugins
- **`packages/cli`** — `felix migrate|eval|mint-jwt|bundle-manifests|validate-manifest|doctor|version|temporal-worker`
- **`manifests/`** — bundled agents (`quick`, `deep`, `router`, `oss-only`, `hybrid-router`, `support`, `cowork`, `governed`)

Felix is **service- and cloud-agnostic**: the harness talks to Postgres, a
cache, and an object store through Protocols — not a single vendor SDK.
**AWS and GCP are first-class** (S3 / Secrets Manager / GCS / Secret Manager via
optional extras `felix-harness[aws]` and `felix-harness[gcp]`). Small VMs can
use `FELIX_OBJECT_STORE=fs` with zero cloud SDKs. Set
`FELIX_OBJECT_STORE=s3|gcs|fs|memory` and `FELIX_SECRETS_BACKEND=env|file|aws|gcp`.
Deploy notes: `deploy/aws/`, `deploy/gcp/`. Manifest secrets + opt-in SOC2 /
EU AI Act mapping: [`deploy/GOVERNANCE.md`](deploy/GOVERNANCE.md). Helm: enable
`persistence` when using
`fs` so `/data` survives restarts. Production JWT/api_key deploys need
`FELIX_CONSUMER_SHARED_SECRET` for `POST /internal/*`.

Felix runs on infrastructure **you** operate. Cloudflare DNS, CDN, TLS, and WAF
in front of your origin are fine. There is **no** Cloudflare Workers / Durable
Objects / Hyperdrive / R2-as-binding / Queues / Workflows compute in this stack.

## Protocols

| Surface | Path |
|---------|------|
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

Management: `/audit`, `/approvals`, `/plans`, `/jobs`, `/manifests`, `/eval`.

Python client: `from felix.sdk import FelixClient` (`prompt`, `stream`, `steer`, `follow_up`, `fork`, `rewind`, `set_model`).

Skills live under `skills/` (Agent Skills `SKILL.md`); declare them on a manifest with `spec.skills`. Session strategies include `compacting` (token-threshold) plus `windowed:N` / `semantic:N` / `full_replay`.

Outbound integrations from the manifest: `spec.mcp_servers` (HTTP or stdio MCP client → `server__tool` tools), `spec.peers` (A2A `peer__name` tools), `spec.browser_tools` (Playwright extra), `spec.sandboxes` / `spec.containers`, and `spec.queues` (Redis list enqueue/dequeue). Large tool outputs can spill via `spec.artifacts`; durable facts via `spec.memory.capture`; how-tos via `spec.procedural_memory`. `spec.execution.mode: durable` enqueues a fiber (Temporal optional) and returns `202` with a `resume_token`. Tool retrieval / semantic sessions / procedural recall use embeddings when `felix-harness[embeddings]` is installed.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
Security reports: [SECURITY.md](SECURITY.md).

## License

MIT — see [LICENSE](LICENSE).
