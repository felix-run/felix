# Felix Docker packaging (image + Compose).
#
# Always run Compose from the **repo root** so `.env`, build context, and
# `./workspace` resolve correctly:
#
#   make up
#   make up-lite
#   make up-gcp   # GCE / public VM (no DB/cache publish)
#
# Equivalent:
#   docker compose -f deploy/docker/compose.yml --project-directory . up --build

| File | Role |
|------|------|
| `Dockerfile` | Multi-stage CPython image (context = repo root) |
| `compose.yml` | api, worker, scheduler, Postgres, Valkey (+ MinIO profile) |
| `compose.lite.yml` | Tight mem caps; no host ports for DB/cache |
| `compose.gcp.yml` | Public VM: no DB/cache publish; workspace mount |
