Prometheus file-based service discovery for optional Compose overlays.

An overlay that brings up a scrapable service drops a `*.yml` file in here; the
`overlays` job in `../prometheus.yml` picks it up within 30s without a Prometheus restart.
Empty is the normal state — it means no optional overlay is running, which is why those
services do not appear as permanently-down static targets.

Each file sets its own `job` label. To scrape Temporal when running
`compose.temporal.yml` alongside the observability overlay, create `temporal.yml` here:

```yaml
- targets: ["temporal:9090"]
  labels:
    job: temporal
```

Why this is not shipped ready-made: a Compose overlay cannot add a volume to a service the
base file does not define, so `compose.temporal.yml` cannot mount it into Prometheus
without breaking `make up-temporal` on its own — and a file committed here unconditionally
would leave anyone running only the observability overlay with a target that never comes up.
