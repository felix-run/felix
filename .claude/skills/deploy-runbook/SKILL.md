---
name: deploy-runbook
description: Deploy and operate Felix — lean Docker image and Compose overlays, small-VM tuning, Helm, AWS and GCP notes, required services, health checks, and the production configuration that must be right before going live. Use when packaging, deploying, tuning memory, debugging a container or chart, or answering how Felix runs in production.
compatibility: Requires Docker for local stacks; Helm/kubectl and cloud CLIs only for the corresponding targets.
allowed-tools: Read Grep Glob Bash(docker compose:*) Bash(helm template:*) Bash(helm lint:*) Bash(make:*)
---

# Deploy runbook

```
Client → Ingress (Caddy / Traefik / nginx / Cloudflare DNS+CDN)
           ├─ felix-api        (CPython 3.14, Granian, FastAPI)
           ├─ felix-worker     (Taskiq consumer)
           └─ felix-scheduler  (Taskiq cron enqueue)
                  │
     Postgres+pgvector · Valkey · object store (fs | S3 | GCS)
```

**`felix-scheduler` is not optional.** The worker only consumes; the scheduler enqueues the labeled
cron tasks. Without it: no audit/usage flush, no retention, no memory consolidation, no anomaly
scan, no continuous eval, no fiber resume.

## Local stacks

```bash
make up          # compose.yml — api :8080, worker, Postgres+pgvector, Valkey, fs object store
make up-lite     # + compose.lite.yml — tighter mem_limit for 2–4 GiB hosts
make up-gcp      # + compose.gcp.yml + lite — no DB/cache host ports (public VM)
make up-full     # --profile full — adds MinIO, FELIX_DOCKER_EXTRAS=aws, FELIX_OBJECT_STORE=s3
make up-observability  # + collector, Prometheus, Grafana :3000, Jaeger :16686, Loki, exporters
make down
make migrate     # uv run felix migrate head
```

Always run Compose from the repo root — the Makefile passes `--project-directory .`.

`up-observability` also rebuilds the image with the `otel` extra appended to
`FELIX_DOCKER_EXTRAS`. Without it the API logs `FELIX_OTEL_ENABLED=true but otel extra is
not installed` and exports nothing while otherwise looking healthy — check that line first
when Jaeger is empty. It runs `scripts/metrics-token.sh` too, because `/metrics` is
auth-gated and Prometheus has no env expansion in scrape configs, so the credential has to
reach it as a file. Budget ~1.7 GiB on top of the base stack; do not pair it with `up-lite`.

## Lean vs full

| Concern | Lean default | Full / cloud |
|---|---|---|
| Object store | `FELIX_OBJECT_STORE=fs` | `s3` / `gcs` + `felix-harness[aws\|gcp]` |
| Image extras | none | `FELIX_DOCKER_EXTRAS=aws,gcp,warehouse` |
| Compose | api + worker + Postgres + Valkey | `--profile full` adds MinIO |
| Warehouse | `none` | `duckdb` (small VMs) / `clickhouse` / `doris` |
| Memory | compose `mem_limit` caps | raise via `FELIX_*_MEM_LIMIT` |

Adding a heavy dependency to the base image is a regression. Extras only.

## Production configuration checklist

- [ ] `FELIX_AUTH_MODE=jwt` or `api_key` — **never** `none` with `FELIX_ALLOW_INSECURE=true`
      (`Settings.validate_runtime()` rejects `none` outside development without it).
- [ ] `FELIX_CONSUMER_SHARED_SECRET` set — required for `POST /internal/*`.
- [ ] `FELIX_ENVIRONMENT=production`; secrets via `FELIX_SECRETS_BACKEND=aws|gcp` (or `file`).
- [ ] Scale-out (`FELIX_SCALE_OUT=true`) needs Postgres **and** a shared object store
      (`s3`/`gcs`/`fs` on shared storage) — `validate_runtime()` enforces it.
- [ ] Helm with `FELIX_OBJECT_STORE=fs` ⇒ enable `persistence` so `/data` survives restarts.
- [ ] `felix-api`, `felix-worker`, **and** `felix-scheduler` all deployed.
- [ ] Migrations applied (`felix migrate head`) before the new image serves traffic.
- [ ] `FELIX_DATABASE_RLS=true` only after migration `0006_tenant_rls` is applied.

## Validate before proposing a change

```bash
docker compose -f deploy/docker/compose.yml --project-directory . config -q
docker build -f deploy/docker/Dockerfile --build-arg FELIX_EXTRAS="" -t felix:test .
helm lint deploy/helm/felix
helm template deploy/helm/felix | head -50
uv run felix doctor
curl -s localhost:8080/health | jq
```

Cloud specifics: `deploy/aws/`, `deploy/gcp/`. Governance/compliance mapping:
`deploy/GOVERNANCE.md`. Image details: `deploy/docker/README.md`.

## Rule

Never run a deploy, `helm upgrade`, `kubectl apply`, or a cloud CLI mutation on the user's behalf.
Propose the exact command; let them run it. Read-only inspection is fine.
