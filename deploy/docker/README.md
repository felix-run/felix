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

## Image hardening

The runtime stage applies pending OS security updates and removes `pip`
(including its vendored `msgpack` and `setuptools`, which carry their own
CVEs). The virtualenv is built by uv in the builder stage and copied whole, so
nothing at runtime needs pip.

CI scans the built image with Trivy and fails on fixable HIGH/CRITICAL
findings, so this stays true. Verify locally with:

```bash
docker build -f deploy/docker/Dockerfile --build-arg FELIX_EXTRAS= -t felix:local .
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy:latest \
  image --scanners vuln --ignore-unfixed --severity HIGH,CRITICAL felix:local
```

Note the trade-off: base images are digest-pinned for reproducibility, but
`apt-get upgrade` means OS package versions can still move between builds.
Security patching wins over bit-identical rebuilds here.
