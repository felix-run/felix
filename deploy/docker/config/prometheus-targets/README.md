Prometheus file-based service discovery for optional Compose overlays.

An overlay that brings up a scrapable service drops a `*.yml` file in here; the
`overlays` job in `../prometheus.yml` picks it up within 30s without a Prometheus restart.
Empty is the normal state — it means no optional overlay is running, which is why those
services do not appear as permanently-down static targets.

Each file sets its own `job` label:

```yaml
- targets: ["temporal:9090"]
  labels:
    job: temporal
```
